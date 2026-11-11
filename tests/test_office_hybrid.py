"""Unit tests for hybrid dual-track Office conversion (remote-first with local fallback)."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import doc_converter, office_local, office_remote


FIXTURES = Path(__file__).resolve().parents[1] / 'templates'


class HybridOfficeConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.work = Path(self.temp_dir.name)
        self.source = self.work / 'test_document.docx'
        self.source.write_bytes(b'PK\x03\x04test-dummy-content')

    def test_remote_configured_and_successful(self):
        """When remote microservice is configured and responds 200, use remote, do not call local."""
        with patch.object(office_remote, 'is_remote_configured', return_value=True), \
             patch.object(office_remote, 'download_pdf') as mock_download, \
             patch.object(doc_converter, 'validate_pdf_glyphs', return_value=3), \
             patch.object(office_local, 'office_to_pdf_local') as mock_local:

            pdf_path, pages = doc_converter.office_to_pdf(str(self.source), output_pdf=str(self.work / 'out.pdf'))
            self.assertEqual(pages, 3)
            self.assertTrue(mock_download.called)
            self.assertFalse(mock_local.called)

    def test_remote_unconfigured_falls_back_to_local(self):
        """When remote is not configured, automatically fall back to local engine."""
        def fake_local(src, dst):
            Path(dst).write_bytes(b'%PDF-1.4\nlocal-pdf')
            return dst

        with patch.object(office_remote, 'is_remote_configured', return_value=False), \
             patch.object(office_remote, 'download_pdf') as mock_download, \
             patch.object(office_local, 'office_to_pdf_local', side_effect=fake_local) as mock_local, \
             patch.object(doc_converter, 'validate_pdf_glyphs', return_value=2):

            pdf_path, pages = doc_converter.office_to_pdf(str(self.source), output_pdf=str(self.work / 'out.pdf'))
            self.assertEqual(pages, 2)
            self.assertFalse(mock_download.called)
            self.assertTrue(mock_local.called)

    def test_remote_retryable_error_falls_back_to_local_when_enabled(self):
        """When remote encounters 504 / timeout / network drop and fallback is enabled, fall back to local engine."""
        def fake_local(src, dst):
            Path(dst).write_bytes(b'%PDF-1.4\nlocal-pdf')
            return dst

        retryable_errors = [
            office_remote.OfficeConversionError("超时", status=504, retryable=True),
            office_remote.OfficeConversionError("连接中断", retryable=True),
            office_remote.OfficeConversionError("服务繁忙", status=429, retryable=True),
        ]

        for err in retryable_errors:
            with self.subTest(err=str(err)):
                with patch.object(office_remote, 'is_remote_configured', return_value=True), \
                     patch.object(office_remote, 'download_pdf', side_effect=err), \
                     patch.object(office_local, 'office_to_pdf_local', side_effect=fake_local) as mock_local, \
                     patch.object(doc_converter, 'validate_pdf_glyphs', return_value=5):

                    pdf_path, pages = doc_converter.office_to_pdf(str(self.source), output_pdf=str(self.work / 'out.pdf'), fallback_to_local=True)
                    self.assertEqual(pages, 5)
                    self.assertTrue(mock_local.called)

    def test_remote_non_retryable_error_does_not_fall_back(self):
        """When remote returns 401 / 403 / 413, do not attempt local fallback, raise immediately."""
        non_retryable_err = office_remote.OfficeConversionError("鉴权拒绝", status=401, retryable=False)
        with patch.object(office_remote, 'is_remote_configured', return_value=True), \
             patch.object(office_remote, 'download_pdf', side_effect=non_retryable_err), \
             patch.object(office_local, 'office_to_pdf_local') as mock_local:

            with self.assertRaises(office_remote.OfficeConversionError) as ctx:
                doc_converter.office_to_pdf(str(self.source), fallback_to_local=True)
            self.assertIn("鉴权拒绝", str(ctx.exception))
            self.assertFalse(mock_local.called)

    def test_both_unavailable_raises_actionable_error(self):
        """When remote is unconfigured and local conversion fails, raise actionable error."""
        local_fail = office_local.LocalConversionError("本机无可用的 Office 转换组件")
        with patch.object(office_remote, 'is_remote_configured', return_value=False), \
             patch.object(office_local, 'office_to_pdf_local', side_effect=local_fail):

            with self.assertRaises(office_remote.OfficeConversionError) as ctx:
                doc_converter.office_to_pdf(str(self.source))
            self.assertIn("本地 Office 转换失败", str(ctx.exception))

    def test_submission_error_preflight(self):
        """Verify preflight check accurately accepts or rejects files under hybrid policies."""
        self.assertIsNone(office_remote.submission_error("test.pdf"))
        self.assertIsNone(office_remote.submission_error("photo.png"))

        # Remote configured -> allowed
        with patch.object(office_remote, 'load_settings'):
            self.assertIsNone(office_remote.submission_error("doc.docx", size=1024))
            self.assertIn("25 MiB", office_remote.submission_error("doc.docx", size=30 * 1024 * 1024))

        # Remote unconfigured, but local available -> allowed
        with patch.object(office_remote, 'is_remote_configured', return_value=False), \
             patch.object(office_local, 'detect_local_engines', return_value={'word': True, 'excel': True}):
            self.assertIsNone(office_remote.submission_error("doc.docx", size=1024))

        # Remote unconfigured AND local unavailable -> rejected with actionable hint
        with patch.object(office_remote, 'is_remote_configured', return_value=False), \
             patch.object(office_local, 'detect_local_engines', return_value={'word': False, 'excel': False}):
            err = office_remote.submission_error("doc.docx", size=1024)
            self.assertIsNotNone(err)
            self.assertIn("未配置 Office 转换服务", err)
            self.assertIn("可先导出为 PDF 再打印", err)


if __name__ == '__main__':
    unittest.main()
