import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import app as native


ROUTE = '/office-converter/forms/libreoffice/convert'
SAMPLES = Path(__file__).resolve().parents[3] / 'templates'


class NativeOfficeTests(unittest.TestCase):
    def setUp(self):
        self.client = native.app.test_client()

    def upload(self, extension='docx', data=None):
        content = (SAMPLES / f'office-check.{extension}').read_bytes() if data is None else data
        return self.client.post(ROUTE, data={'files': (io.BytesIO(content), f'../sample.{extension}')})

    def test_returns_pdf_and_removes_job_directory(self):
        workspaces = []

        def converter(source, work):
            self.assertEqual(source.parent, work)
            self.assertIn(source.name, ('document.docx', 'document.xlsx'))
            workspaces.append(work)
            pdf = source.with_suffix('.pdf')
            pdf.write_bytes(b'%PDF-1.7\n')
            return pdf

        with patch.object(native, 'convert', side_effect=converter):
            for ext in ('docx', 'xlsx'):
                with self.subTest(extension=ext):
                    result = self.upload(ext)
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.content_type, 'application/pdf')
                    self.assertEqual(result.data, b'%PDF-1.7\n')
        self.assertEqual(len(workspaces), 2)
        self.assertFalse(any(path.exists() for path in workspaces))

    def test_rejects_invalid_requests_before_running_libreoffice(self):
        with patch.object(native, 'convert') as converter:
            self.assertEqual(self.client.get(ROUTE).status_code, 405)
            self.assertEqual(self.upload(data=b'renamed text').status_code, 422)
            self.assertEqual(self.upload('pdf', b'%PDF-1.7\n').status_code, 415)
            with native.conversion_slot:
                self.assertEqual(self.upload().status_code, 429)
            converter.assert_not_called()

    def test_timeout_releases_slot_and_removes_upload(self):
        def timeout(source, work):
            self.work = work
            raise subprocess.TimeoutExpired('soffice', 90)

        with patch.object(native, 'convert', side_effect=timeout):
            self.assertEqual(self.upload().status_code, 504)
        self.assertFalse(native.conversion_slot.locked())
        self.assertFalse(self.work.exists())


if __name__ == '__main__':
    unittest.main()
