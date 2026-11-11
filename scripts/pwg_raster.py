"""Portable PWG Raster (RaS2) writer, 8-bit sRGB/sGray, PWG 5102.4.

PDF rendering uses PyMuPDF; the stream is readable by the CUPS raster API.
No printer access occurs in this module.
"""
import struct
from pathlib import Path


def _encode_row(row: bytes, channels: int) -> bytes:
    """PWG pixel PackBits: 0..127 repeat a pixel, 129..255 literal pixels."""
    output = bytearray()
    pixels = len(row) // channels
    x = 0
    def pixel(index):
        return row[index * channels:(index + 1) * channels]
    while x < pixels:
        count = 1
        value = pixel(x)
        while count < 128 and x + count < pixels and pixel(x + count) == value:
            count += 1
        if count > 1:
            output.append(count - 1)
            output.extend(value)
            x += count
            continue
        start = x
        x += 1
        while x - start < 128 and x < pixels:
            if x + 1 < pixels and pixel(x) == pixel(x + 1):
                break
            x += 1
        count = x - start
        output.append(0 if count == 1 else 257 - count)
        output.extend(row[start * channels:x * channels])
    return bytes(output)


def _header(width, height, dpi, channels, pages, sides):
    header = bytearray(1796)
    header[:9] = b'PwgRaster'
    def integer(offset, *values):
        struct.pack_into('>' + 'I' * len(values), header, offset, *values)
    integer(272, int(sides != 'one-sided'))
    integer(276, dpi, dpi)
    integer(340, 1)  # Copies are an IPP job attribute; do not multiply here.
    integer(352, round(width * 72 / dpi), round(height * 72 / dpi))
    integer(368, int(sides == 'two-sided-short-edge'))
    integer(372, width, height, 0, 8, channels * 8, width * channels, 0, 19 if channels == 3 else 18)
    integer(420, channels)
    integer(452, pages, 1, 1, 0, 0, width, height)
    return bytes(header)


def pdf_to_pwg(pdf_path: str, output_path: str, dpi=300, grayscale=False, sides='one-sided') -> str:
    try:
        import pymupdf
    except (ImportError, OSError) as exc:
        raise RuntimeError('打印机需要光栅数据，但当前技能的 Python 无法加载 PyMuPDF。请重新运行本机打印环境配置入口。') from exc
    channels = 1 if grayscale else 3
    try:
        with pymupdf.open(pdf_path) as document:
            if document.needs_pass or not document.page_count:
                raise RuntimeError('PDF 已加密或没有页面，无法生成打印数据')
            with open(output_path, 'wb') as output:
                output.write(b'RaS2')
                for page in document:
                    # Bound a single render before allocating its pixels.
                    if page.rect.width * page.rect.height * (dpi / 72) ** 2 > 100_000_000:
                        raise RuntimeError('页面过大，超过光栅转换上限，请调整纸张尺寸')
                    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY if grayscale else pymupdf.csRGB, alpha=False)
                    output.write(_header(pixmap.width, pixmap.height, dpi, channels, document.page_count, sides))
                    samples = pixmap.samples
                    y = 0
                    while y < pixmap.height:
                        row = samples[y * pixmap.stride:y * pixmap.stride + pixmap.width * channels]
                        repeats = 1
                        while repeats < 256 and y + repeats < pixmap.height:
                            start = (y + repeats) * pixmap.stride
                            if samples[start:start + len(row)] != row:
                                break
                            repeats += 1
                        output.write(bytes([repeats - 1]))
                        output.write(_encode_row(row, channels))
                        y += repeats
    except Exception:
        Path(output_path).unlink(missing_ok=True)
        raise
    return output_path
