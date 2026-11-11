"""Exercise Office conversion and submission without a live converter or printer."""
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pymupdf

from scripts import bootstrap, doc_converter, office_check, office_remote, queue_scheduler
from scripts.printer_service import PrinterService


FIXTURES = Path(__file__).resolve().parents[1] / 'templates'


class OfficeConversionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.config = self.data / 'office-converter.json'
        self.config.write_text(json.dumps({
            'url': 'https://convert.example:8443/office-converter',
            'username': 'test-user', 'password': 'fixture-secret',
        }))
        self.source = self.root / '中文表格.XLSX'
        self.source.write_bytes((FIXTURES / 'office-check.xlsx').read_bytes())
        for module, key, value in (
                (office_remote, 'ROOT', self.root), (doc_converter, 'CONVERSION_ROOT', self.data),
                (bootstrap, 'ROOT', self.root), (queue_scheduler, 'STATE_FILE', str(self.data / 'queue.json'))):
            patcher = patch.object(module, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with pymupdf.open() as document:
            document.new_page()
            document.new_page()
            self.pdf = document.tobytes()

    def connection(self, response_code=200, payload=None, media_type='application/pdf', length=None):
        payload = self.pdf if payload is None else payload
        response = MagicMock(status=response_code)
        stream = io.BytesIO(payload)
        response.read.side_effect = stream.read
        response.getheader.side_effect = {
            'Content-Type': media_type, 'Content-Length': str(len(payload)) if length is None else length,
        }.get
        conn = MagicMock()
        conn.getresponse.return_value.__enter__.return_value = response
        return conn

    def test_each_office_extension_uploads_only_sanitized_name_and_validates_pages(self):
        for suffix in office_remote.OFFICE_SUFFIXES:
            with self.subTest(suffix=suffix):
                source = self.source.with_suffix(suffix)
                if suffix in ('.docx', '.xlsx'):
                    payload = (FIXTURES / ('office-check' + suffix)).read_bytes()
                else:
                    payload = office_remote.COMPOUND_FILE_HEADER.ljust(512, b'\0')
                source.write_bytes(payload)
                conn = self.connection()
                sent = []
                conn.request.side_effect = lambda *args, body, **kwargs: sent.extend(body)
                with patch.object(office_remote.http.client, 'HTTPSConnection', return_value=conn) as connect:
                    destination, pages = doc_converter.prepare_printable_file(str(source))
                self.assertEqual(pages, 2)
                self.assertEqual(Path(destination).read_bytes(), self.pdf)
                self.assertIn(b'filename="document' + suffix.encode() + b'"', sent[0])
                self.assertEqual(sent[1], payload)
                self.assertNotIn(self.source.stem.encode(), b''.join(sent))
                self.assertEqual(conn.request.call_args.args,
                                 ('POST', '/office-converter/forms/libreoffice/convert'))
                headers = conn.request.call_args.kwargs['headers']
                self.assertEqual(int(headers['Content-Length']), len(b''.join(sent)))
                self.assertTrue(headers['Authorization'].startswith('Basic '))
                self.assertEqual(connect.call_args.args, ('convert.example', 8443))
                self.assertTrue(connect.call_args.kwargs['context'].check_hostname)
                conn.close.assert_called_once()

    def test_bad_response_does_not_publish_pdf(self):
        cases = [
            (self.connection(401), '拒绝访问'),
            (self.connection(302), 'HTTP 302'),
            (self.connection(400), '损坏或加密'),
            (self.connection(413), '上传上限'),
            (self.connection(429), '繁忙'),
            (self.connection(504), '超时'),
            (self.connection(503), '暂不可用'),
            (self.connection(media_type='text/html'), '有效的 PDF'),
            (self.connection(payload=b'%PDF-corrupt'), '可打印的 PDF'),
            (self.connection(length=str(len(self.pdf) + 1)), '下载不完整'),
        ]
        for conn, reason in cases:
            with self.subTest(reason=reason), patch.object(office_remote.http.client, 'HTTPSConnection', return_value=conn):
                with self.assertRaisesRegex(office_remote.OfficeConversionError, reason) as error:
                    doc_converter.prepare_printable_file(str(self.source))
                self.assertNotIn('fixture-secret', str(error.exception))
                self.assertEqual(list((self.data / 'converted').iterdir()), [])
                conn.request.assert_called_once()

    def test_tls_connection_and_timeout_errors_stop_printing(self):
        for failure, reason in (
                (ssl.SSLCertVerificationError('test'), '证书验证失败'),
                (ConnectionRefusedError(), '内网或 VPN'),
                (socket.timeout(), '超时')):
            conn = self.connection()
            conn.request.side_effect = failure
            service = PrinterService()
            with self.subTest(reason=reason), patch.object(office_remote.http.client, 'HTTPSConnection', return_value=conn), \
                    patch.object(service, '_send_to_physical_printer') as send:
                result = service.submit_document(str(self.source))
                self.assertFalse(result['success'])
                self.assertIn(reason, result['error'])
                self.assertEqual(result['job']['status'], 'failed')
                send.assert_not_called()

    def test_missing_configuration_and_oversize_are_rejected_without_network(self):
        service = PrinterService()
        self.config.unlink()
        with patch.object(office_remote.http.client, 'HTTPSConnection') as connect, \
                patch.object(doc_converter.office_local, 'detect_local_engines', return_value={'word': False, 'excel': False}):
            result = service.submit_document(str(self.source))
            self.assertIn('未配置', result['error'])
            self.assertEqual(service.scheduler.get_jobs(), [])
            self.config.write_text('{"url":"http://example","username":"user","password":"secret"}')
            self.assertIn('配置无效', office_remote.submission_error(self.source))
            self.config.write_text('{"url":"https://example","username":"user","password":"secret"}')
            with self.source.open('wb') as stream:
                stream.truncate(office_remote.MAX_SOURCE_BYTES + 1)
            with self.assertRaisesRegex(office_remote.OfficeConversionError, '25 MiB'):
                doc_converter.prepare_printable_file(str(self.source))
            connect.assert_not_called()

    def test_renamed_text_is_rejected_without_upload(self):
        with patch.object(office_remote.http.client, 'HTTPSConnection') as connect:
            for suffix in office_remote.OFFICE_SUFFIXES:
                source = self.source.with_suffix(suffix)
                source.write_bytes(b'plain text, not an Office document')
                with self.subTest(suffix=suffix), self.assertRaisesRegex(office_remote.OfficeConversionError, '扩展名不符'):
                    doc_converter.prepare_printable_file(str(source))
            connect.assert_not_called()

    def test_status_reads_cache_and_recheck_runs_both_samples(self):
        self.assertEqual(office_check.status()['state'], 'unchecked')

        def samples(settings, report):
            report('word', {})
            self.assertEqual(office_check.status()['checking_format'], 'word')
            return office_check.result('partial', 'checked', formats={'word': {'ready': True}})

        with patch.object(office_check, 'check_samples', side_effect=samples) as check:
            self.assertEqual(office_check.start_check()['state'], 'partial')
            self.assertTrue(office_check.status()['word'])
            check.assert_called_once()
        self.assertNotIn('fixture-secret', office_check.cache_file().read_text(encoding='utf-8'))
        self.config.write_text(self.config.read_text() + '\n')
        self.assertEqual(office_check.status()['state'], 'unchecked')


if __name__ == '__main__':
    unittest.main()
