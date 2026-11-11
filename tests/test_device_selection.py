"""Selection regressions with isolated configuration/queue and mocked transport."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import cli, config, queue_scheduler, web_server
from scripts.printer_service import PrinterService


class DeviceSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'config.json'
        self.cfg = copy.deepcopy(config.DEFAULT_CONFIG)
        self.cfg['printers'] = [
            dict(id='device-a', name='财务打印机', location='27F 东区', model='Shared Model', ip='10.0.0.1'),
            dict(id='device-b', name='研发打印机', location='28F 西区', model='Shared Model', ip='10.0.0.1'),
            dict(id='device-c', name='财务打印机彩色', location='29F 东区', model='Color', ip='10.0.0.1'),
        ]
        self.cfg['system']['default_printer_id'] = 'device-a'
        self.save()
        for target, name, value in [(config, 'CONFIG_PATH', str(self.path)),
                                    (queue_scheduler, 'STATE_FILE', str(self.root/'queue.json'))]:
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = PrinterService()

    def save(self):
        self.path.write_text(json.dumps(self.cfg), encoding='utf-8')

    def test_default_exact_id_name_and_unique_keyword(self):
        for value, expected in [(None,'device-a'), ('','device-a'), (' DEVICE-B ','device-b'),
                                ('财务打印机','device-a'), ('28F','device-b'), ('Color','device-c')]:
            with self.subTest(value=value):
                self.assertEqual(self.service.resolve_printer_by_keyword(value),expected)

    def test_complete_name_precedes_partial_name_and_ambiguous_model(self):
        self.assertEqual(self.service.resolve_printer_by_keyword('财务打印机'),'device-a')
        with self.assertRaisesRegex(ValueError,'多台打印机') as error:
            self.service.resolve_printer_by_keyword('Shared Model')
        self.assertIn('财务打印机',str(error.exception))
        self.assertIn('28F 西区',str(error.exception))

    def test_duplicate_complete_name_is_not_first_match(self):
        self.cfg['printers'][1]['name']='财务打印机'
        self.save()
        with self.assertRaisesRegex(ValueError,'多台打印机'):
            self.service.resolve_printer_by_keyword('财务打印机')
        self.assertEqual(self.service.resolve_printer_by_keyword('device-b'),'device-b')

    def test_no_match_or_invalid_id_does_not_fall_back(self):
        with self.assertRaisesRegex(ValueError,'未找到指定打印机'):
            self.service.resolve_printer_by_keyword('不存在的楼层')
        with self.assertRaisesRegex(ValueError,'未找到指定打印机'):
            config.get_printer('deleted-id')
        self.assertEqual(config.get_printer()['id'],'device-a')

    def test_broken_missing_and_invalid_default_configuration_stop(self):
        for corrupt in ('broken', '{}', json.dumps({**self.cfg,'system':{'default_printer_id':'missing'}})):
            self.path.write_text(corrupt)
            for operation in (config.get_printers, config.get_default_printer_id,
                              lambda:config.get_printer('device-b'),self.service.resolve_printer_by_keyword):
                with self.assertRaises(ValueError):
                    operation()
        self.path.unlink()
        with self.assertRaisesRegex(ValueError,'无法读取'):
            self.service.resolve_printer_by_keyword('device-a')

    def test_invalid_selection_never_converts_enqueues_or_sends(self):
        with patch.object(self.service.scheduler,'enqueue') as enqueue, patch('scripts.printer_service.prepare_printable_file') as convert, patch.object(self.service,'_send_to_physical_printer') as send:
            for target in ('不存在','Shared Model'):
                with self.assertRaises(ValueError):
                    self.service.submit_document('test.docx',printer_target=target)
                with self.assertRaises(ValueError):
                    self.service.enqueue_upload('test.docx','test',printer_target=target)
            enqueue.assert_not_called();convert.assert_not_called();send.assert_not_called()

    def test_invalid_dashboard_queries_no_device(self):
        with patch('scripts.printer_service.IPPClient') as ipp:
            with self.assertRaises(ValueError):
                self.service.get_full_dashboard_data('deleted-id')
            ipp.assert_not_called()

    def test_valid_target_keeps_settings_through_dispatch(self):
        with patch('scripts.printer_service.submission_error', return_value=None), \
                patch('scripts.printer_service.prepare_printable_file',return_value=('converted.pdf',3)), patch.object(self.service,'_send_to_physical_printer',return_value={'success':True,'job_id':9}) as send:
            result=self.service.submit_document('test.docx','张三','研发打印机',priority=80,duplex='duplex_short',copies=2)
        self.assertTrue(result['success'])
        args=send.call_args
        self.assertEqual(args.args[0]['id'],'device-b')
        self.assertEqual(args.kwargs['duplex'],'duplex_short')
        self.assertEqual(args.kwargs['copies'],2)
        job=result['job']
        self.assertEqual((job['pages'],job['priority'],job['user']),(3,80,'张三'))
        self.assertEqual(job['status'],'submitted')

    def upload(self, target, copies=None, request_id=None):
        fields = {'printer': target}
        if copies is not None:
            fields['copies'] = copies
        if request_id is not None:
            fields['request_id'] = request_id
        prefix = ''.join(f'--test\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                         for name, value in fields.items())
        body=(prefix +
              '--test\r\nContent-Disposition: form-data; name="file"; filename="test.pdf"\r\n'
              'Content-Type: application/pdf\r\n\r\n%PDF-fixture\r\n--test--\r\n').encode()
        handler=web_server.PrinterWebHandler.__new__(web_server.PrinterWebHandler)
        handler.path='/api/upload_print';handler.headers=Message()
        handler.headers['Content-Type']='multipart/form-data; boundary=test'
        handler.headers['Content-Length']=str(len(body))
        handler.rfile=io.BytesIO(body);handler._send_json=Mock()
        with patch.object(web_server,'svc',self.service),patch.object(web_server,'UPLOAD_DIR',str(self.root/'uploads')):
            (self.root/'uploads').mkdir(exist_ok=True)
            handler.do_POST()
        return handler._send_json.call_args.args

    def test_web_copies_validated_and_original_value_reused(self):
        for value in ('', '0', '-1', '1.5', '100', '1e1'):
            with self.subTest(value=value):
                _, status = self.upload('device-a', value)
                self.assertEqual(status, 400)
                self.assertEqual(list((self.root/'uploads').iterdir()), [])
        self.assertEqual(self.service.scheduler.get_jobs(), [])
        first, status = self.upload('device-a', '3', 'same-request')
        self.assertEqual(status, 202)
        retry, = self.upload('device-a', '9', 'same-request')
        self.assertEqual(retry['job']['id'], first['job']['id'])
        self.assertEqual(retry['job']['copies'], 3)
        with patch('scripts.printer_service.prepare_printable_file', return_value=('converted.pdf', 1)), \
                patch.object(self.service, '_send_to_physical_printer', return_value={'success': True, 'job_id': 9}) as send:
            self.service.dispatch_top_job('device-a')
            self.service.dispatch_top_job('device-a')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs['copies'], 3)
        legacy, _ = self.upload('device-a')
        self.assertEqual(legacy['job']['copies'], 1)

    def test_cli_default_persists_and_ambiguous_selection_does_not_write(self):
        for target, expected in [('Shared Model', 'device-a'), ('28F', 'device-b')]:
            with patch('sys.argv', ['cli.py', 'set-default', '--printer', target]), contextlib.redirect_stdout(io.StringIO()):
                if target == 'Shared Model':
                    with self.assertRaises(SystemExit):
                        cli.main()
                else:
                    cli.main()
            self.assertEqual(config.get_default_printer_id(), expected)
            self.assertEqual(PrinterService().resolve_printer_by_keyword(), expected)
        job = self.service.submit_document('test.pdf', auto_dispatch=False)['job']
        self.assertEqual(job['printer_id'], 'device-b')

    def test_bad_web_selection_returns_400_without_upload_or_job(self):
        for target in ('不存在','Shared Model'):
            body,status=self.upload(target)
            self.assertEqual(status,400)
            self.assertFalse(body['success'])
            self.assertEqual(list((self.root/'uploads').iterdir()),[])
            self.assertEqual(self.service.scheduler.get_jobs(),[])

    def test_cli_error_is_short_nonzero_and_diagnostic_uses_resolver(self):
        with patch('sys.argv',['cli.py','status','--printer','不存在']),contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                cli.main()
        self.assertEqual(result.exception.code,1)
        self.assertIn('未找到指定打印机',output.getvalue())
        self.assertNotIn('Traceback',output.getvalue())
        with patch('sys.argv',['cli.py','diag','--printer','28F']),patch('scripts.diag.run_full_diagnosis',return_value={}) as diag,patch('scripts.diag.format_diag_report',return_value='ok'),contextlib.redirect_stdout(io.StringIO()):
            cli.main()
            diag.assert_called_once_with('device-b')


if __name__=='__main__':
    unittest.main()
