"""Font-loss regression: extracted text is insufficient to prove visible glyphs."""
import unittest
from unittest.mock import MagicMock, patch

from scripts import doc_converter as converter


class FontValidationTests(unittest.TestCase):
    def test_missing_visible_chinese_glyph_is_rejected(self):
        document = MagicMock()
        document.__enter__.return_value = [MagicMock()]
        document.__enter__.return_value[0].get_texttrace.return_value = [{'type':0, 'chars':[(ord('打'),0,(1,1),(1,1,5,5))]}]
        with patch('pymupdf.open',return_value=document):
            with self.assertRaisesRegex(RuntimeError,'缺字'):
                converter.validate_pdf_glyphs('test.pdf')

    def test_invisible_ocr_text_is_not_rejected(self):
        document = MagicMock()
        document.__enter__.return_value = [MagicMock()]
        document.__enter__.return_value[0].get_texttrace.return_value = [{'type':3, 'chars':[(ord('打'),0,(1,1),(1,1,5,5))]}]
        with patch('pymupdf.open',return_value=document):
            converter.validate_pdf_glyphs('test.pdf')

if __name__ == '__main__':
    unittest.main()
