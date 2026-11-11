import struct
import tempfile
import unittest
from pathlib import Path

from scripts.pwg_raster import _encode_row, pdf_to_pwg


def decode_row(encoded, width, channels):
    result = bytearray()
    pos = 0
    while len(result) < width * channels:
        count = encoded[pos]
        pos += 1
        if count <= 127:
            pixel = encoded[pos:pos + channels]
            pos += channels
            result.extend(pixel * (count + 1))
        elif count >= 129:
            size = (257 - count) * channels
            result.extend(encoded[pos:pos + size])
            pos += size
        else:
            raise AssertionError('reserved PackBits code')
    return bytes(result), pos


class RasterTests(unittest.TestCase):
    def test_pixel_encoding_roundtrip_boundaries(self):
        import random
        randomizer = random.Random(19)
        for channels in (1, 3):
            for size in (1, 2, 127, 128, 129, 257, 1000):
                for row in (bytes([255]) * size * channels, bytes(randomizer.randrange(256) for _ in range(size * channels))):
                    with self.subTest(channels=channels, size=size):
                        encoded = _encode_row(row, channels)
                        restored, consumed = decode_row(encoded, size, channels)
                        self.assertEqual(restored, row)
                        self.assertEqual(consumed, len(encoded))

    def test_two_page_pdf_renders_all_pixels(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'two.pdf'
            output = Path(directory) / 'two.pwg'
            document = pymupdf.open()
            for n in range(2):
                page = document.new_page(width=72, height=72)
                page.draw_rect(pymupdf.Rect(5, 5, 20 + n, 30), color=(1, 0, 0), fill=(1, 0, 0))
            document.save(pdf)
            document.close()
            for gray, channels in ((False, 3), (True, 1)):
                pdf_to_pwg(str(pdf), str(output), dpi=300, grayscale=gray)
                data = output.read_bytes()
                self.assertEqual(data[:4], b'RaS2')
                position = 4
                with pymupdf.open(pdf) as doc:
                    for page in doc:
                        header = data[position:position+1796]
                        position += 1796
                        self.assertEqual(header[:9], b'PwgRaster')
                        width, height = struct.unpack_from('>II', header, 372)
                        self.assertEqual((width, height), (300, 300))
                        pixels = bytearray()
                        while len(pixels) < width * height * channels:
                            repeats = data[position] + 1
                            position += 1
                            row, consumed = decode_row(data[position:], width, channels)
                            position += consumed
                            pixels.extend(row * repeats)
                        expected = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY if gray else pymupdf.csRGB, alpha=False)
                        self.assertEqual(bytes(pixels), expected.samples)
                self.assertEqual(position, len(data))

    @unittest.skipUnless(__import__('sys').platform == 'darwin', 'independent CUPS decoder is available on macOS')
    def test_stream_readable_by_native_cups(self):
        import ctypes
        import pymupdf
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / 'native.pdf'
            output = Path(directory) / 'native.pwg'
            with pymupdf.open() as doc:
                for n in range(2):
                    page = doc.new_page(width=72, height=72)
                    page.insert_text((10, 30), f'Page {n+1}')
                doc.save(pdf)
            pdf_to_pwg(str(pdf), str(output))
            cups = ctypes.CDLL('/usr/lib/libcups.2.dylib')
            cups.cupsRasterOpen.argtypes = [ctypes.c_int, ctypes.c_int]
            cups.cupsRasterOpen.restype = ctypes.c_void_p
            cups.cupsRasterReadHeader2.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            cups.cupsRasterReadPixels.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
            cups.cupsRasterClose.argtypes = [ctypes.c_void_p]
            with output.open('rb') as source, pymupdf.open(pdf) as doc:
                raster = cups.cupsRasterOpen(source.fileno(), 0)
                self.assertTrue(raster)
                try:
                    for page in doc:
                        header = ctypes.create_string_buffer(1796)
                        self.assertEqual(cups.cupsRasterReadHeader2(raster, header), 1)
                        width, height = struct.unpack_from('=II', header.raw, 372)
                        self.assertEqual((width, height), (300, 300))
                        pixels = ctypes.create_string_buffer(width * height * 3)
                        self.assertEqual(cups.cupsRasterReadPixels(raster, pixels, len(pixels)), len(pixels))
                        self.assertEqual(pixels.raw, page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False).samples)
                finally:
                    cups.cupsRasterClose(raster)


if __name__ == '__main__':
    unittest.main()
