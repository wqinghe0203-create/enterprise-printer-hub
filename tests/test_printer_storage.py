import copy
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from scripts import config, queue_scheduler
from tools.build_skill_package import ROOT, ROOT_FILES, build_package


def fixture():
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg['printers'] = [
        {'id': 'existing', 'ip': '10.11.12.13', 'name': '原有设备', 'location': '3F', 'custom_field': '保留'},
        {'id': 'server_added', 'ip': '10.11.12.14', 'name': '服务器新增设备', 'ipp_port': 1631, 'ipp_path': '/print'},
    ]
    cfg['system']['default_printer_id'] = 'server_added'
    return cfg


class PrinterStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / 'config.json'
        self.db_path = self.root / 'data' / 'printers.sqlite3'
        self.original = fixture()
        self.write_config(self.original)
        for name, value in (('CONFIG_PATH', str(self.config_path)), ('PRINTER_DB_PATH', str(self.db_path))):
            override = patch.object(config, name, value)
            override.start()
            self.addCleanup(override.stop)

    def write_config(self, value):
        self.config_path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_migration_keeps_ids_fields_default_and_source_backup(self):
        (self.root / 'config.example.json').write_text(json.dumps(config.DEFAULT_CONFIG), encoding='utf-8')
        raw = self.config_path.read_bytes()
        self.assertEqual(config.get_printers(), self.original['printers'])
        self.assertEqual(config.get_default_printer_id(), 'server_added')
        self.assertEqual(self.config_path.read_bytes(), raw)
        self.assertEqual((self.db_path.parent / 'printers.config.pre-sqlite.json').read_bytes(), raw)
        self.write_config(config.DEFAULT_CONFIG)
        self.assertEqual(config.get_printers(), self.original['printers'])
        self.config_path.write_text('broken JSON', encoding='utf-8')
        self.assertEqual(config.get_default_printer_id(), 'server_added')

    def test_fresh_install_imports_example_and_existing_config_has_priority(self):
        example = self.root / 'config.example.json'
        example.write_text(json.dumps(config.DEFAULT_CONFIG), encoding='utf-8')
        self.config_path.unlink()
        self.assertEqual(config.get_printers(), config.DEFAULT_CONFIG['printers'])
        self.write_config(self.original)
        self.assertEqual(config.get_printers(), config.DEFAULT_CONFIG['printers'])

    def test_single_printer_migration_repairs_deleted_default(self):
        single = fixture()
        single['printers'] = single['printers'][:1]
        self.write_config(single)
        raw = self.config_path.read_bytes()
        self.assertEqual(config.get_default_printer_id(), 'existing')
        self.assertEqual(config.get_printers(), single['printers'])
        self.assertEqual(self.config_path.read_bytes(), raw)
        self.assertEqual((self.db_path.parent / 'printers.config.pre-sqlite.json').read_bytes(), raw)

    def test_ambiguous_missing_default_requires_selection(self):
        invalid = fixture()
        invalid['system']['default_printer_id'] = 'deleted_device'
        self.write_config(invalid)
        with self.assertRaisesRegex(ValueError, '首次迁移失败'):
            config.get_printers()
        self.assertEqual(json.loads(self.config_path.read_text(encoding='utf-8')), invalid)

    def test_invalid_migration_can_be_corrected_without_partial_catalog(self):
        invalid = fixture()
        invalid['printers'][1]['id'] = 'existing'
        self.write_config(invalid)
        with self.assertRaisesRegex(ValueError, '首次迁移失败'):
            config.get_printers()
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sqlite_master WHERE type="table"').fetchone()[0], 0)
        self.write_config(self.original)
        self.assertEqual(config.get_printers(), self.original['printers'])

    def test_create_edit_and_default_persist_without_rewriting_json(self):
        config.get_printers()
        raw = self.config_path.read_bytes()
        new = {'id': 'new', 'ip': '10.11.12.15', 'name': '新增'}
        config.create_printer(new)
        with self.assertRaises(ValueError):
            config.create_printer({**new, 'id': 'duplicate'})
        version = config.editable_printer(config.get_printer('new'))['version']
        config.update_printer('new', version, {'name': '更新名称', 'ipp_port': 2631})
        with self.assertRaises(config.PrinterEditConflict):
            config.update_printer('new', version, {'name': '过期修改'})
        config.set_default_printer('new')
        self.assertEqual(config.get_printer()['name'], '更新名称')
        self.assertEqual(config.get_printer()['ipp_port'], 2631)
        self.assertEqual(config.get_printer('existing')['custom_field'], '保留')
        self.assertEqual(self.config_path.read_bytes(), raw)

    def test_failed_write_rolls_back_catalog_and_default(self):
        before = config.get_printers()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("CREATE TRIGGER reject_bad BEFORE INSERT ON printers WHEN NEW.id='bad' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            config.create_printer({'id': 'bad', 'ip': '10.11.12.16', 'name': '写入失败'})
        self.assertEqual(config.get_printers(), before)
        self.assertEqual(config.get_default_printer_id(), 'server_added')

    def test_corrupt_database_is_preserved_and_does_not_reseed(self):
        self.db_path.parent.mkdir()
        self.db_path.write_bytes(b'corrupt database')
        with self.assertRaises(sqlite3.DatabaseError):
            config.get_printers()
        self.assertEqual(self.db_path.read_bytes(), b'corrupt database')

    def test_legacy_save_and_upsert_still_work(self):
        config.add_or_update_printer({'id': 'existing', 'name': '旧接口改名'})
        self.assertEqual(config.get_printer('existing')['ip'], '10.11.12.13')
        cfg = config.load_config_strict()
        cfg['server']['web_ui_port'] = 18829
        config.save_config(cfg)
        self.assertEqual(json.loads(self.config_path.read_text(encoding='utf-8'))['server']['web_ui_port'], 18829)
        self.assertEqual(config.get_printer('existing')['name'], '旧接口改名')

    def test_queue_history_retains_device_identity(self):
        with patch.object(queue_scheduler, 'STATE_FILE', str(self.db_path.parent / 'queue_state.json')):
            scheduler = queue_scheduler.QueueScheduler()
            job = scheduler.enqueue(str(self.root / 'test.pdf'), '测试', printer_id='server_added')
            config.update_printer('server_added', config.editable_printer(config.get_printer())['version'], {'name': '新名称'})
            self.assertEqual(scheduler.get_job(job['id'])['printer_id'], 'server_added')
            self.assertEqual(scheduler.get_job(job['id'])['printer_name'], '服务器新增设备')

    def test_concurrent_processes_keep_all_devices(self):
        worker = '''from pathlib import Path
import sys
from scripts import config
root = Path(sys.argv[1])
config.CONFIG_PATH = str(root / 'config.json')
config.PRINTER_DB_PATH = str(root / 'data/printers.sqlite3')
index = sys.argv[2]
config.create_printer({'id':'worker_' + index, 'ip':'10.11.13.' + index, 'name':'Worker ' + index})
'''
        processes = [subprocess.Popen([sys.executable, '-B', '-c', worker, str(self.root), str(index)], cwd=ROOT,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for index in range(1, 7)]
        for process in processes:
            _, error = process.communicate(timeout=45)
            self.assertEqual(process.returncode, 0, error.decode('utf-8', errors='replace'))
        self.assertEqual(len(config.get_printers()), len(self.original['printers']) + 6)


class ReleaseUpgradeTests(unittest.TestCase):
    def test_release_excludes_databases_and_retains_server_data_on_two_upgrades(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            for name in ROOT_FILES:
                shutil.copyfile(ROOT / name, source / name)
            for name in ('scripts', 'templates', 'deploy', 'tools'):
                shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copyfile(ROOT / 'config.json', source / 'config.json')
            (source / 'data').mkdir()
            for name in ('data/printers.sqlite3', 'data/queue_state.sqlite3', 'accidental.db', 'data/printers.sqlite3-wal', 'config.json.bak'):
                (source / name).write_bytes(b'LOCAL FILE MUST NOT SHIP')
            package = build_package(source)
            with ZipFile(package) as archive:
                names = archive.namelist()
                self.assertIn('enterprise-printer-hub/config.example.json', names)
                self.assertNotIn('enterprise-printer-hub/config.json', names)
                self.assertFalse(any('/data/' in name or '.sqlite' in name or name.endswith('.db') for name in names))
                server_root = root / 'server'
                server = server_root / 'enterprise-printer-hub'
                server.mkdir(parents=True)
                original = fixture()
                (server / 'config.json').write_text(json.dumps(original), encoding='utf-8')
                archive.extractall(server_root)
            run = '''import json
import http.client
import os
import threading
import time
from scripts import config
from scripts.queue_scheduler import QueueScheduler
from scripts import printer_registration, printer_service, web_server

class ReadOnlyIPP:
    def __init__(self, printer_cfg, **kwargs):
        pass
    def get_printer_status(self):
        return {'online':True, 'name':'API device', 'model':'PDF printer', 'capabilities':{
            'document-format-supported':['application/pdf'], 'sides-supported':['one-sided']}}
    def get_jobs(self, *args):
        return []

printer_registration.IPPClient = ReadOnlyIPP
printer_registration.is_local_address = lambda address: False
printer_service.IPPClient = ReadOnlyIPP
class Handler(web_server.PrinterWebHandler):
    def log_message(self, *args):
        pass

config.get_printers()
config.create_printer({'id':'after_migration', 'ip':'10.11.14.10', 'name':'Added in production'})
config.set_default_printer('after_migration')
scheduler = QueueScheduler()
scheduler.enqueue('retained.pdf', 'test', printer_id='after_migration', request_id='upgrade-test')
with web_server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
    origin = 'http://127.0.0.1:' + str(server.server_port)
    os.environ['PRINT_HUB_REGISTRATION_ORIGIN'] = origin
    for key in ('PRINT_HUB_ADMIN_TOKEN', 'PRINT_HUB_API_TOKEN'):
        os.environ.pop(key, None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                               headers={'Content-Type':'application/json', 'Origin':origin})
            response = connection.getresponse()
            payload = json.loads(response.read())
            assert response.status < 300, payload
            return payload
        finally:
            connection.close()
    try:
        probe = request('POST', '/api/printer-probes', {'ip':'10.11.14.11'})
        for attempt in range(100):
            result = request('GET', '/api/printer-probes/' + probe['probe_id'])
            if result['status'] != 'pending':
                break
            time.sleep(.01)
        assert result['can_save'], result
        body = {'probe_id':probe['probe_id'], 'name':'API registered', 'location':'4F'}
        first = request('POST', '/api/printers', body)
        second = request('POST', '/api/printers', body)
        assert first['printer']['id'] == second['printer']['id'] and not second['created']
        cluster = request('GET', '/api/cluster')
        dashboard = request('GET', '/api/dashboard')
        assert any(p['id'] == first['printer']['id'] for p in cluster)
        assert any(p['id'] == first['printer']['id'] for p in dashboard['cluster'])
    finally:
        server.shutdown()
        thread.join()
'''
            result = subprocess.run([sys.executable, '-B', '-c', run], cwd=server, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            with ZipFile(package) as archive:
                archive.extractall(server_root)
            # Even an accidental replacement of the JSON cannot reset an initialized catalog.
            shutil.copyfile(source / 'config.json', server / 'config.json')
            check = '''from scripts import config
from scripts.queue_scheduler import QueueScheduler
printers = config.get_printers()
assert [p['id'] for p in printers[:3]] == ['existing', 'server_added', 'after_migration']
assert len(printers) == 4 and printers[3]['name'] == 'API registered'
assert config.get_default_printer_id() == 'after_migration'
assert QueueScheduler().get_request('upgrade-test')['printer_id'] == 'after_migration'
'''
            result = subprocess.run([sys.executable, '-B', '-c', check], cwd=server, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
