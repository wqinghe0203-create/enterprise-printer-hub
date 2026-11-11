"""Configuration/probe regressions; all device access is mocked, configuration isolated."""
import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import config
from scripts.printer_registration import (ProbeRegistry, RegistrationError, inspect_printer,
                                           save_inspected_printer, validate_endpoint)


ENDPOINT = {'ip':'10.22.33.44','ipp_port':631,'ipp_path':'/ipp/print'}
RESULT = {'status':'succeeded','compatible':True,'can_save':True,'name':'测试设备','model':'Test Printer',
          'color_supported':True,'duplex_supported':False}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='中文 配置 ')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'config.json'
        self.original = copy.deepcopy(config.DEFAULT_CONFIG)
        self.original['custom'] = {'unchanged':True}
        self.path.write_text(json.dumps(self.original,ensure_ascii=False),encoding='utf-8')
        self.before = self.path.read_bytes()
        patcher = patch.object(config,'CONFIG_PATH',str(self.path))
        patcher.start();self.addCleanup(patcher.stop)
        local = patch('scripts.printer_registration.is_local_address',return_value=False)
        local.start();self.addCleanup(local.stop)

    def new_printer(self, ip='10.22.33.44'):
        return {'id':'new_'+ip,'ip':ip,'ipp_port':631,'ipp_path':'/ipp/print','name':'新增打印机'}

    def test_create_preserves_defaults_devices_unknown_fields_and_backup(self):
        config.create_printer(self.new_printer())
        after = config.load_config_strict()
        self.assertEqual(after['printers'][:-1],self.original['printers'])
        self.assertEqual(after['system'],self.original['system'])
        self.assertEqual(after['custom'],self.original['custom'])
        self.assertEqual(Path(str(self.path)+'.bak').read_bytes(),self.before)

    def test_duplicate_endpoint_cannot_overwrite(self):
        config.create_printer(self.new_printer())
        snapshot=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'已存在'):
            config.create_printer({**self.new_printer(),'id':'different','name':'overwrite'})
        self.assertEqual(snapshot,self.path.read_bytes())

    def test_duplicate_id_cannot_overwrite(self):
        with self.assertRaisesRegex(ValueError,'标识已存在'):
            config.create_printer({**self.new_printer(),'id':self.original['printers'][0]['id']})
        self.assertEqual(self.before,self.path.read_bytes())

    def test_corrupt_config_is_never_replaced_with_defaults(self):
        self.path.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):config.create_printer(self.new_printer())
        self.assertEqual(self.path.read_bytes(),b'corrupt')

    def test_invalid_schema_is_rejected(self):
        self.path.write_text('{"printers":[],"system":{}}')
        with self.assertRaises(ValueError):config.create_printer(self.new_printer())

    def test_replace_failure_preserves_original(self):
        real_replace=config.os.replace
        def replace(source,dest):
            if str(dest)==str(self.path):raise PermissionError('test write failure')
            return real_replace(source,dest)
        with patch.object(config.os,'replace',side_effect=replace):
            with self.assertRaises(PermissionError):config.create_printer(self.new_printer())
        self.assertEqual(self.path.read_bytes(),self.before)
        self.assertEqual(list(self.path.parent.glob('.printer-config-*')),[])

    def test_backup_failure_preserves_original(self):
        with patch.object(config.os,'replace',side_effect=PermissionError('backup failure')):
            with self.assertRaises(PermissionError):config.create_printer(self.new_printer())
        self.assertEqual(self.path.read_bytes(),self.before)

    def test_concurrent_process_additions_do_not_lose_updates(self):
        command='import sys; from scripts import config; config.CONFIG_PATH=sys.argv[1]; config.create_printer({"id":sys.argv[2],"ip":sys.argv[2],"name":"并发"})'
        processes=[subprocess.Popen([sys.executable,'-c',command,str(self.path),f'10.22.33.{n}'],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for n in range(50,54)]
        for process in processes:
            out,error=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0,error.decode())
        self.assertEqual(len(config.load_config_strict()['printers']),5)
        self.assertEqual(config.load_config_strict()['system'],self.original['system'])

    def test_endpoint_validation(self):
        for ip in ['127.0.0.1','169.254.1.1','8.8.8.8','224.0.0.1','::1','localhost','10.1.2.3/path','http://10.1.2.3']:
            with self.subTest(ip=ip),self.assertRaises(RegistrationError):validate_endpoint({'ip':ip})
        self.assertEqual(validate_endpoint(ENDPOINT),ENDPOINT)
        for value in [0,65536,True,1.5,'not-a-port']:
            with self.subTest(port=value),self.assertRaises(RegistrationError):validate_endpoint({**ENDPOINT,'ipp_port':value})
        for value in ['/../','//redirect','/ipp?next=http://evil','/a%2fb','/a\nX: bad']:
            with self.subTest(path=value),self.assertRaises(RegistrationError):validate_endpoint({**ENDPOINT,'ipp_path':value})

    def test_local_address_rejected(self):
        with patch('scripts.printer_registration.is_local_address',return_value=True):
            with self.assertRaises(RegistrationError):validate_endpoint(ENDPOINT)

    def test_probe_only_reads_and_blocks_redirects(self):
        with patch('scripts.printer_registration.IPPClient') as client:
            client.return_value.get_printer_status.return_value={'online':True,'name':'Name','model':'Model','capabilities':{'document-format-supported':['application/pdf'],'sides-supported':['one-sided']}}
            result=inspect_printer(ENDPOINT)
            self.assertTrue(result['can_save'])
            self.assertFalse(result['duplex_supported'])
            self.assertFalse(client.call_args.kwargs['follow_redirects'])
            client.return_value.print_raw_payload.assert_not_called()
            client.return_value.validate_job.assert_not_called()

    def test_unsupported_and_offline_are_not_saveable(self):
        with patch('scripts.printer_registration.IPPClient') as client:
            client.return_value.get_printer_status.return_value={'online':True,'capabilities':{'document-format-supported':['application/octet-stream']}}
            self.assertFalse(inspect_printer(ENDPOINT)['can_save'])
            client.return_value.get_printer_status.return_value={'online':False,'state_desc':'连接超时'}
            self.assertEqual(inspect_printer(ENDPOINT)['status'],'failed')

    def test_raster_compatibility_matches_sender(self):
        base={'document-format-supported':['image/pwg-raster'],'pwg-raster-document-type-supported':['sgray_8'],'pwg-raster-document-resolution-supported':{'x':300,'y':300,'units':3}}
        with patch('scripts.printer_registration.IPPClient') as client:
            client.return_value.get_printer_status.return_value={'online':True,'capabilities':base}
            self.assertTrue(inspect_printer(ENDPOINT)['can_save'])
            base['pwg-raster-document-resolution-supported']['x']=1200
            self.assertFalse(inspect_printer(ENDPOINT)['can_save'])

    def test_invalid_print_settings_are_not_saveable(self):
        with patch('scripts.printer_registration.IPPClient') as client:
            for capability in [{'document-format-supported':['application/pdf'],'sides-supported':[]},
                               {'document-format-supported':['application/pdf'],'copies-supported':[2,99]}]:
                client.return_value.get_printer_status.return_value={'online':True,'capabilities':capability}
                self.assertFalse(inspect_printer(ENDPOINT)['can_save'])

    def test_unsupported_raster_duplex_uses_safe_simplex_default(self):
        capability={'document-format-supported':['image/pwg-raster'],'pwg-raster-document-type-supported':['srgb_8'],
                    'pwg-raster-document-resolution-supported':{'x':300,'y':300,'units':3},
                    'sides-supported':['one-sided','two-sided-long-edge'],'pwg-raster-document-sheet-back':'rotated'}
        with patch('scripts.printer_registration.IPPClient') as client:
            client.return_value.get_printer_status.return_value={'online':True,'capabilities':capability}
            result=inspect_printer(ENDPOINT)
            self.assertTrue(result['can_save'])
            self.assertEqual(result['default_duplex'],'simplex')
            printer=save_inspected_printer(ENDPOINT,result,'Test','Room')
            self.assertEqual(printer['default_duplex'],'simplex')

    def test_names_and_location_validated(self):
        for name in ['', 'x'*81,'bad\nname']:
            with self.assertRaises(RegistrationError):save_inspected_printer(ENDPOINT,RESULT,name,'位置')
        self.assertEqual(self.before,self.path.read_bytes())

    def test_registry_async_busy_expiry_and_idempotence(self):
        registry=ProbeRegistry();block=threading.Event()
        def inspect(endpoint):
            block.wait(2)
            return dict(RESULT)
        with patch('scripts.printer_registration.inspect_printer',side_effect=inspect):
            started=time.monotonic();probe=registry.start(ENDPOINT)
            self.assertLess(time.monotonic()-started,0.5)
            self.assertEqual(registry.get(probe['probe_id'])['status'],'pending')
            with self.assertRaises(RegistrationError):registry.start(ENDPOINT)
            block.set()
            deadline=time.monotonic()+2
            while registry.get(probe['probe_id'])['status']=='pending' and time.monotonic()<deadline:time.sleep(0.01)
            record={'probe_id':probe['probe_id'],'name':'新设备','location':'27F','ip':'10.99.99.99','id':'overwrite'}
            printer,created=registry.save(record)
            self.assertTrue(created);self.assertEqual(printer['ip'],ENDPOINT['ip'])
            same,created=registry.save(record)
            self.assertFalse(created);self.assertEqual(same,printer)
            self.assertEqual(len(config.load_config_strict()['printers']),2)
            registry._records[probe['probe_id']]['expires']=0
            with self.assertRaises(RegistrationError) as error:registry.get(probe['probe_id'])
            self.assertEqual(error.exception.status,410)


if __name__=='__main__':unittest.main()
