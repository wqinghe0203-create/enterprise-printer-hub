"""Exercise the new handler routes without sockets or printer mutations."""
import io
import json
import unittest
import tempfile
from pathlib import Path
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch, Mock
from zipfile import ZipFile

from scripts import web_server
from scripts import queue_scheduler
from scripts.printer_service import PrinterService
from scripts.printer_registration import RegistrationError


class RegistrationHTTPTests(unittest.TestCase):
    def test_excel_upload_delivery_with_simulated_printer(self):
        import pymupdf

        workbook = io.BytesIO()
        sample = Path(__file__).resolve().parents[1] / 'templates' / 'office-check.xlsx'
        with ZipFile(sample) as original, ZipFile(workbook, 'w') as fixture:
            for name in original.namelist():
                data = original.read(name)
                if name == '[Content_Types].xml':
                    entry = data[data.index(b'<Override PartName="/xl/worksheets/sheet1.xml"'):data.index(b'</Types>')]
                    data = data.replace(b'</Types>', entry.replace(b'sheet1.xml', b'sheet2.xml') + b'</Types>')
                if name == 'xl/workbook.xml':
                    data = data.replace(b'</sheets>', b'<sheet name="Sheet2" sheetId="2" r:id="rId2"/></sheets>'
                                        b'<definedNames><definedName name="_xlnm.Print_Area" localSheetId="0">'
                                        b'Sheet1!$A$1:$B$40</definedName></definedNames>')
                if name == 'xl/_rels/workbook.xml.rels':
                    entry = data[data.index(b'<Relationship Id='):data.index(b'</Relationships>')]
                    data = data.replace(b'</Relationships>', entry.replace(b'rId1', b'rId2').replace(
                        b'sheet1.xml', b'sheet2.xml') + b'</Relationships>')
                fixture.writestr(name, data)
            fixture.writestr('xl/worksheets/sheet2.xml', original.read('xl/worksheets/sheet1.xml'))
        body = (b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="sample.xlsx"\r\n'
                b'Content-Type: application/octet-stream\r\n\r\n' + workbook.getvalue() +
                b'\r\n--boundary\r\nContent-Disposition: form-data; name="request_id"\r\n\r\nexcel-62'
                b'\r\n--boundary--\r\n')
        for fmt, outcome in [('application/pdf', 'completed'), ('image/pwg-raster', 'completed'),
                             ('application/pdf', 'failed'), ('application/pdf', 'unknown'),
                             ('application/pdf', 'validate_failed')]:
            with self.subTest(fmt=fmt, outcome=outcome), tempfile.TemporaryDirectory() as folder, \
                    patch.object(queue_scheduler, 'STATE_FILE', str(Path(folder) / 'queue.json')):
                pdf = Path(folder) / 'converted.pdf'
                with pymupdf.open() as document:
                    for _ in range(62):
                        document.new_page()
                    document.save(pdf)
                service = PrinterService()
                handler = self.handler()
                handler.path = '/api/upload_print'
                handler.headers.replace_header('Content-Type', 'multipart/form-data; boundary=boundary')
                handler.headers.replace_header('Content-Length', str(len(body)))
                with patch.object(web_server, 'svc', service), patch.object(web_server, 'UPLOAD_DIR', folder), \
                        patch.object(web_server, 'submission_error', return_value=None), \
                        patch('scripts.printer_service.prepare_printable_file', return_value=(str(pdf), 62)) as convert, \
                        patch('scripts.printer_service.IPPClient') as ipp, \
                        patch('scripts.printer_service.pdf_to_pwg') as raster:
                    client = ipp.return_value
                    client.get_printer_status.return_value = {'online': True, 'capabilities': {
                        'document-format-supported': [fmt], 'sides-supported': ['one-sided', 'two-sided-long-edge'],
                        'copies-supported': [1, 99], 'pwg-raster-document-type-supported': ['sgray_8'],
                        'pwg-raster-document-resolution-supported': [{'x': 300, 'y': 300, 'units': 3}]}}
                    client.validate_job.return_value = {'success': outcome != 'validate_failed',
                                                       'status_code': 0x400 if outcome == 'validate_failed' else 0}
                    if outcome == 'unknown':
                        client.print_raw_payload.side_effect = TimeoutError('mock timeout')
                    else:
                        client.print_raw_payload.return_value = {'success': outcome == 'completed',
                            'job_id': 77 if outcome == 'completed' else None, 'status_code': '0x0'}
                    client.get_job.return_value = {'success': True, 'job-state': 9}

                    def fake_raster(_, output, **options):
                        Path(output).write_bytes(b'RaS2 fixture')
                        return output

                    raster.side_effect = fake_raster
                    for _ in range(2):
                        handler.rfile = io.BytesIO(body)
                        handler.do_POST()
                        self.assertTrue(handler._send_json.call_args.args[0]['success'])
                        service.dispatch_top_job()
                    convert.assert_called_once()
                    client.validate_job.assert_called_once()
                    self.assertEqual(client.print_raw_payload.call_count, 0 if outcome == 'validate_failed' else 1)
                    job = service.scheduler.get_request('excel-62')
                    if outcome == 'completed':
                        self.assertEqual(job['status'], 'submitted')
                        service.sync_hardware_jobs(job['printer_id'], client)
                        job = service.scheduler.get_job(job['id'])
                        self.assertTrue(job['completion_verified'])
                    self.assertEqual(job['status'], 'failed' if outcome == 'validate_failed' else outcome)
                    self.assertEqual(job['pages'], 62)
                    self.assertEqual(job['delivery_trace']['document_format'], fmt)
                    self.assertGreater(job['delivery_trace']['payload_bytes'], 0)

    def handler(self, origin='http://127.0.0.1:18828', host='127.0.0.1:18828', peer='127.0.0.1', data=None):
        handler=web_server.PrinterWebHandler.__new__(web_server.PrinterWebHandler)
        handler.client_address=(peer,12345)
        handler.server=SimpleNamespace(server_port=18828)
        handler.headers=Message();handler.headers['Host']=host
        if origin is not None:handler.headers['Origin']=origin
        body=json.dumps(data or {'ip':'10.22.33.44'}).encode()
        handler.headers['Content-Type']='application/json';handler.headers['Content-Length']=str(len(body))
        handler.rfile=io.BytesIO(body);handler._send_json=Mock();handler.path='/api/printer-probes'
        return handler

    def test_upload_duplex_selection_compatibility_and_validation(self):
        for mode, expected in [(None,'duplex_long'), ('auto','auto'), ('simplex','simplex'), ('duplex_short','duplex_short'), ('invalid',None)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                parts = [b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="test.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-fixture\r\n']
                if mode is not None:
                    parts.append(('--boundary\r\nContent-Disposition: form-data; name="duplex"\r\n\r\n'+mode+'\r\n').encode())
                body = b''.join(parts) + b'--boundary--\r\n'
                handler = self.handler()
                handler.path = '/api/upload_print'
                handler.headers.replace_header('Content-Type','multipart/form-data; boundary=boundary')
                handler.headers.replace_header('Content-Length',str(len(body)))
                handler.rfile = io.BytesIO(body)
                with patch.object(web_server,'UPLOAD_DIR',directory), patch.object(web_server,'svc') as service, patch.object(web_server,'get_printer',return_value={'default_duplex':'duplex_long'}):
                    service.enqueue_upload.side_effect=lambda **kw: {'success':True,'job':{'filepath':str(Path(kw['filepath']).resolve())}}
                    handler.do_POST()
                    if expected is None:
                        service.enqueue_upload.assert_not_called()
                        self.assertEqual(list(Path(directory).iterdir()),[])
                        self.assertEqual(handler._send_json.call_args.args[1],400)
                    else:
                        self.assertEqual(service.enqueue_upload.call_args.kwargs['duplex'],expected)

    def test_local_same_origin_probe_returns_202(self):
        handler=self.handler()
        with patch.object(web_server.probes,'start',return_value={'probe_id':'test','status':'pending'}) as start:
            handler._registration_request('/api/printer-probes',True)
            start.assert_called_once_with({'ip':'10.22.33.44'})
        self.assertEqual(handler._send_json.call_args.args[1],202)

    def test_office_progress_visible_only_on_local_console(self):
        for peer in ('127.0.0.1', '10.1.2.3'):
            handler = self.handler(peer=peer)
            handler.path = '/api/office-status'
            handler.send_error = Mock()
            with patch.object(web_server.office_check, 'status', return_value={'enabled':True, 'state':'checking'}) as read:
                handler._get()
            if peer == '127.0.0.1':
                handler._send_json.assert_called_once_with({'enabled':True, 'state':'checking'})
            else:
                handler.send_error.assert_called_once_with(404)
                read.assert_not_called()

    def test_foreign_origins_hosts_remote_and_missing_origin_rejected(self):
        for kwargs in [dict(origin='http://evil.test'),dict(origin=None),dict(peer='10.1.2.3'),dict(host='evil.test:18828',origin='http://evil.test:18828'),dict(host='127.0.0.1:9000',origin='http://127.0.0.1:9000')]:
            with self.subTest(kwargs=kwargs),patch.object(web_server.probes,'start') as start:
                handler=self.handler(**kwargs);handler._registration_request('/api/printer-probes',True)
                start.assert_not_called();self.assertEqual(handler._send_json.call_args.args[1],403)

    def test_local_get_without_origin_allowed(self):
        handler=self.handler(origin=None)
        with patch.object(web_server.probes,'get',return_value={'status':'pending'}):
            handler._registration_request('/api/printer-probes/test')
        self.assertEqual(handler._send_json.call_args.args[0]['status'],'pending')

    def test_default_printer_write_requires_local_same_origin(self):
        for headers, allowed in [({}, True), ({'peer': '10.1.2.3'}, False),
                                 ({'origin': None}, False), ({'origin': 'http://evil.test'}, False)]:
            with self.subTest(headers=headers), patch.object(web_server, 'set_default_printer', return_value={'id':'p2'}) as save:
                handler = self.handler(**headers)
                handler.path = '/api/default-printer?printer_id=p2'
                handler.do_POST()
                if allowed:
                    save.assert_called_once_with('p2')
                    self.assertEqual(handler._send_json.call_args.args[0]['default_printer_id'], 'p2')
                else:
                    save.assert_not_called()
                    self.assertEqual(handler._send_json.call_args.args[1], 403)

    def test_json_content_type_and_size_checked(self):
        for name,value in [('Content-Type','text/plain'),('Content-Length','20000'),('Content-Length','invalid')]:
            handler=self.handler();handler.headers.replace_header(name,value)
            handler._registration_request('/api/printer-probes',True)
            self.assertFalse(handler._send_json.call_args.args[0]['success'])

    def test_bad_json_is_rejected(self):
        handler=self.handler();handler.rfile=io.BytesIO(b'not json')
        handler._registration_request('/api/printer-probes',True)
        self.assertEqual(handler._send_json.call_args.args[1],400)

    def test_save_returns_created(self):
        handler=self.handler(data={'probe_id':'test','name':'Name'})
        with patch.object(web_server.probes,'save',return_value=({'id':'new'},True)):
            handler._registration_request('/api/printers',True)
        self.assertEqual(handler._send_json.call_args.args[1],201)

    def test_repeated_save_returns_existing_without_duplicate(self):
        handler=self.handler(data={'probe_id':'test','name':'Name'})
        with patch.object(web_server.probes,'save',return_value=({'id':'new'},False)):
            handler._registration_request('/api/printers',True)
        self.assertEqual(handler._send_json.call_args.args[1],200)
        self.assertFalse(handler._send_json.call_args.args[0]['created'])

    def test_expired_result_is_reported(self):
        handler=self.handler(origin=None)
        with patch.object(web_server.probes,'get',side_effect=RegistrationError('expired',410)):
            handler._registration_request('/api/printer-probes/test')
        self.assertEqual(handler._send_json.call_args.args[1],410)

    def test_disk_error_never_claims_success(self):
        handler=self.handler()
        with patch.object(web_server.probes,'save',side_effect=PermissionError('denied')):
            handler._registration_request('/api/printers',True)
        self.assertEqual(handler._send_json.call_args.args[1],500)
        self.assertFalse(handler._send_json.call_args.args[0]['success'])


if __name__=='__main__':unittest.main()
