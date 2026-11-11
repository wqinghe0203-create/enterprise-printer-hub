"""
文档预处理与格式转换器
- 支持将用户发送的常见文件（PDF, Word docx/doc, Excel xlsx/xls, 图片 JPG/PNG）标准化为打印机就绪格式
- 自动提取文档页数
- 双轨容灾路由：配置内网微服务时优先使用；未配置或设置 fallback 时自动使用本地引擎 (Word/WPS COM 或 Headless LibreOffice)
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Tuple, Optional

from . import office_remote, office_local
from .office_remote import (
    OFFICE_SUFFIXES, OfficeConversionError, is_remote_configured,
)
from .office_local import (
    office_to_pdf_local, office_to_pdf_win32, office_to_pdf_libreoffice,
    find_soffice, detect_local_engines, LocalConversionError,
)
from .runtime_paths import ensure_directory


CONVERSION_ROOT = Path(__file__).resolve().parents[1] / 'data'

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from PIL import Image
except ImportError:
    Image = None


def get_pdf_pages(pdf_path: str) -> int:
    """提取 PDF 页数"""
    if not os.path.exists(pdf_path):
        return 1
    if PdfReader:
        try:
            reader = PdfReader(pdf_path)
            return len(reader.pages)
        except Exception:
            pass
    try:
        import pymupdf
        with pymupdf.open(pdf_path) as document:
            return document.page_count
    except Exception:
        return 1


def image_to_pdf(img_path: str, output_pdf: Optional[str] = None) -> str:
    """将图片文件 (PNG/JPG/BMP) 转换为标准 PDF"""
    if not output_pdf:
        output_pdf = os.path.splitext(img_path)[0] + "_converted.pdf"

    if Image:
        with Image.open(img_path) as im:
            rgb_im = im.convert("RGB")
            rgb_im.save(output_pdf, "PDF", resolution=150.0)
            return output_pdf

    raise RuntimeError("缺少 Pillow 库，无法转换图片")


def validate_pdf_glyphs(pdf_path: str, *, require_pdf: bool = False):
    """Reject visible CJK text mapped to .notdef; text extraction can still look correct."""
    try:
        import pymupdf
    except (ImportError, OSError) as exc:
        raise RuntimeError('当前技能的 Python 无法加载 PyMuPDF，无法检查中文缺字。请重新运行本机打印环境配置入口。') from exc
    try:
        if require_pdf:
            # A parser error must not keep the staging file locked on Windows.
            document = pymupdf.open(stream=Path(pdf_path).read_bytes(), filetype='pdf')
        else:
            document = pymupdf.open(pdf_path)
    except pymupdf.FileDataError:
        if require_pdf:
            raise OfficeConversionError('Office 转换服务未生成可打印的 PDF 页面') from None
        raise
    with document as pdf:
        if require_pdf and (not pdf.is_pdf or pdf.is_encrypted or pdf.is_repaired or not pdf.page_count):
            raise OfficeConversionError('Office 转换服务未生成可打印的 PDF 页面')
        for page_number, page in enumerate(pdf, 1):
            for span in page.get_texttrace():
                if span.get('type') == 3:  # Invisible OCR layer is not painted.
                    continue
                for codepoint, glyph_id, *_ in span.get('chars', []):
                    if (0x3400 <= codepoint <= 0x9fff or 0x20000 <= codepoint <= 0x323af) and glyph_id == 0:
                        raise RuntimeError(f'第 {page_number} 页存在中文缺字，未发送打印；请检查中文字体，或从 Word / WPS 导出正常显示的 PDF')
        return pdf.page_count if require_pdf else None


def office_to_pdf(office_path: str, *, output_pdf: Optional[str] = None, settings=None, fallback_to_local: Optional[bool] = None) -> Tuple[str, int]:
    """
    双轨容灾 Office 转换路由：
    1. 远程优先：已配置且服务正常，调用内网微服务转换并下载 PDF；
    2. 本地兜底：未配置微服务，或开启了 fallback_to_local 且远程遇到可重试异常时，自动平滑调用本地引擎。
    """
    source = Path(office_path).resolve()
    destination = (Path(output_pdf).resolve() if output_pdf is not None else
                   ensure_directory(CONVERSION_ROOT / 'converted') / f'{uuid.uuid4().hex}.pdf')
    destination.parent.mkdir(parents=True, exist_ok=True)

    use_remote = office_remote.is_remote_configured() or settings is not None

    if use_remote:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.pdf.tmp', delete=False) as output:
            temporary = Path(output.name)
        try:
            office_remote.download_pdf(source, temporary, settings=settings)
            pages = validate_pdf_glyphs(str(temporary), require_pdf=True)
            temporary.replace(destination)
            return str(destination), pages
        except office_remote.OfficeConversionError as exc:
            allow_fallback = fallback_to_local
            if allow_fallback is None:
                try:
                    s = settings or office_remote.load_settings()
                    allow_fallback = getattr(s, 'fallback_to_local', False)
                except Exception:
                    allow_fallback = False
            if not allow_fallback or not exc.retryable:
                raise
        finally:
            temporary.unlink(missing_ok=True)

    # 本地转换（未配置远程服务，或远程故障且启用了本地容灾降级）
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.pdf.tmp', delete=False) as output:
        temporary = Path(output.name)
    try:
        office_local.office_to_pdf_local(str(source), str(temporary))
        pages = validate_pdf_glyphs(str(temporary), require_pdf=True)
        temporary.replace(destination)
        return str(destination), pages
    except (office_local.LocalConversionError, RuntimeError, OSError) as local_err:
        raise office_remote.OfficeConversionError(f"本地 Office 转换失败: {local_err}") from None
    finally:
        temporary.unlink(missing_ok=True)


def prepare_printable_file(input_path: str) -> Tuple[str, int]:
    """
    通用预处理函数：
    接收任意文件路径，返回 (可打印的PDF文件路径, 总页数)
    """
    abs_path = os.path.abspath(input_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"文件不存在: {abs_path}")

    ext = os.path.splitext(abs_path)[1].lower()

    if ext == ".pdf":
        validate_pdf_glyphs(abs_path)
        pages = get_pdf_pages(abs_path)
        return abs_path, pages

    elif ext in (".jpg", ".jpeg", ".png", ".bmp"):
        pdf_path = image_to_pdf(abs_path)
        pages = get_pdf_pages(pdf_path)
        return pdf_path, pages

    elif ext in OFFICE_SUFFIXES:
        return office_to_pdf(abs_path)

    else:
        # 其他文本格式
        return abs_path, 1
