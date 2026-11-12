# -*- coding: utf-8 -*-
"""
文档预处理与格式转换器
- 支持将用户发送的常见文件（PDF, Word docx/doc, Excel xlsx/xls, 图片 JPG/PNG）标准化为打印机就绪格式
- 自动提取文档页数
- Windows 使用本地 COM；macOS / Linux 使用 LibreOffice 无界面转换
"""

import logging
import os
import sys
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from xml.etree import ElementTree
from typing import Tuple, Optional
from .config import get_converter_config
from .gotenberg_client import (
    GotenbergError,
    convert_html as gotenberg_convert_html,
    convert_markdown as gotenberg_convert_markdown,
    convert_office as gotenberg_convert_office,
)
from .runtime_paths import ensure_directory

logger = logging.getLogger(__name__)

OFFICE_EXTS = {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf", ".txt", ".csv",
}
MARKDOWN_EXTS = {".md", ".markdown"}
HTML_EXTS = {".html", ".htm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
SUPPORTED_EXTS = {".pdf"} | OFFICE_EXTS | MARKDOWN_EXTS | HTML_EXTS | IMAGE_EXTS


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


def office_to_pdf_win32(office_path: str, output_pdf: Optional[str] = None) -> str:
    """利用 Windows COM (Word/WPS) 将 Office 文档无损另存为 PDF"""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    abs_path = os.path.abspath(office_path)
    if not output_pdf:
        output_pdf = os.path.splitext(abs_path)[0] + "_converted.pdf"
    out_abs = os.path.abspath(output_pdf)

    ext = os.path.splitext(abs_path)[1].lower()

    if ext in (".docx", ".doc"):
        # 尝试 Word 或 WPS 文字
        app = None
        for prog_id in ("Word.Application", "kwps.Application", "wps.Application"):
            try:
                app = win32com.client.Dispatch(prog_id)
                break
            except Exception:
                continue

        if not app:
            raise RuntimeError("本机未检测到 Microsoft Word 或 WPS，无法自动转换 Docx")

        try:
            app.Visible = False
            app.DisplayAlerts = 0
            doc = app.Documents.Open(abs_path, ReadOnly=True)
            # wdFormatPDF = 17
            doc.SaveAs(out_abs, FileFormat=17)
            doc.Close(False)
            return out_abs
        finally:
            try:
                app.Quit()
            except Exception:
                pass
            pythoncom.CoUninitialize()

    elif ext in (".xlsx", ".xls"):
        app = None
        for prog_id in ("Excel.Application", "ket.Application", "et.Application"):
            try:
                app = win32com.client.Dispatch(prog_id)
                break
            except Exception:
                continue

        if not app:
            raise RuntimeError("本机未检测到 Microsoft Excel 或 WPS 表格")

        try:
            app.Visible = False
            app.DisplayAlerts = False
            wb = app.Workbooks.Open(abs_path, ReadOnly=True)
            # xlTypePDF = 0
            wb.ExportAsFixedFormat(0, out_abs)
            wb.Close(False)
            return out_abs
        finally:
            try:
                app.Quit()
            except Exception:
                pass
            pythoncom.CoUninitialize()

    return abs_path


def find_soffice() -> Optional[str]:
    """优先读取 PATH，再检查 macOS 的标准安装目录。"""
    for name in ("soffice", "libreoffice"):
        executable = shutil.which(name)
        if executable:
            return executable
    for root in (Path("/Applications"), Path.home() / "Applications"):
        for app in ("LibreOffice.app", "LibreOfficeDev.app"):
            executable = root / app / "Contents/MacOS/soffice"
            if executable.is_file() and os.access(executable, os.X_OK):
                return str(executable)
    return None


def libreoffice_environment(work: Path):
    """Expose current macOS font assets to headless Fontconfig, scoped to this child."""
    environment = dict(os.environ)
    if sys.platform != 'darwin':
        return environment
    config = ElementTree.Element('fontconfig')
    inherited = environment.get('FONTCONFIG_FILE')
    if inherited and Path(inherited).is_file():
        ElementTree.SubElement(config, 'include', {'ignore_missing':'yes'}).text = inherited
    for directory in (Path('/System/Library/Fonts'), Path('/System/Library/AssetsV2'),
                      Path('/Library/Fonts'), Path.home() / 'Library/Fonts'):
        if directory.is_dir():
            ElementTree.SubElement(config, 'dir').text = str(directory)
    # Reuse font indexes across conversions without sharing LibreOffice profiles.
    cache = Path.home() / 'Library' / 'Caches' / 'enterprise-printer-hub' / 'fontconfig'
    try:
        cache.mkdir(parents=True, exist_ok=True)
        if not os.access(cache, os.W_OK | os.X_OK):
            cache = work / 'font-cache'
    except OSError:
        cache = work / 'font-cache'
    ElementTree.SubElement(config, 'cachedir').text = str(cache)
    config_path = work / 'fonts.conf'
    ElementTree.ElementTree(config).write(config_path, encoding='utf-8', xml_declaration=True)
    environment['FONTCONFIG_FILE'] = str(config_path)
    return environment


def validate_pdf_glyphs(pdf_path: str):
    """Reject visible CJK text mapped to .notdef; text extraction can still look correct."""
    try:
        import pymupdf
    except (ImportError, OSError) as exc:
        raise RuntimeError('当前技能的 Python 无法加载 PyMuPDF，无法检查中文缺字。请重新运行本机打印环境配置入口。') from exc
    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, 1):
            for span in page.get_texttrace():
                if span.get('type') == 3:  # Invisible OCR layer is not painted.
                    continue
                for codepoint, glyph_id, *_ in span.get('chars', []):
                    if (0x3400 <= codepoint <= 0x9fff or 0x20000 <= codepoint <= 0x323af) and glyph_id == 0:
                        raise RuntimeError(f'第 {page_number} 页存在中文缺字，未发送打印；请检查中文字体，或从 Word / WPS 导出正常显示的 PDF')


OFFICE_CONVERSION_TIMEOUT = 90
MAC_LOCAL_TMP = Path('/private/tmp')


def libreoffice_temp_directory(prefix, *, dir=None):
    # TMPDIR can point into the skill or onto the source volume.
    return tempfile.TemporaryDirectory(prefix=prefix, dir=MAC_LOCAL_TMP if sys.platform == 'darwin' else dir)


def libreoffice_process_options(work):
    environment = libreoffice_environment(work)
    if sys.platform == 'darwin':
        for name in ('TMPDIR', 'TMP', 'TEMP'):
            environment[name] = str(work)
        return {'env': environment, 'cwd': work}
    return {'env': environment}


def office_to_pdf_libreoffice(office_path: str, output_pdf: Optional[str] = None, *,
                            executable=None, timeout=OFFICE_CONVERSION_TIMEOUT) -> str:
    """在隔离的用户配置和输出目录中转换，避免已打开的 Office 实例和旧文件干扰。"""
    executable = executable or find_soffice()
    if not executable:
        raise RuntimeError("未检测到 LibreOffice；请安装 LibreOffice，或先将 Word / Excel 导出为 PDF 再上传")
    if sys.platform == 'darwin':
        executable = os.path.abspath(executable)

    source = Path(office_path).resolve()
    scratch = None if sys.platform == 'darwin' else ensure_directory(CONVERSION_ROOT / 'tmp')
    destination = (Path(output_pdf).resolve() if output_pdf else
                   ensure_directory(CONVERSION_ROOT / 'converted') / f'{uuid.uuid4().hex}.pdf')
    with libreoffice_temp_directory(".office-convert-", dir=scratch) as work_dir:
        work = Path(work_dir)
        local_source = work / source.name
        shutil.copyfile(source, local_source)
        command = [
            executable, "-env:UserInstallation=" + (work / "profile").as_uri(),
            "--headless", "--convert-to", "pdf", "--outdir", str(work), str(local_source)
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                    **libreoffice_process_options(work))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"LibreOffice 转换超时（{timeout} 秒），文件未发送到打印机") from exc
        except OSError as exc:
            raise RuntimeError("无法启动 LibreOffice，请在「应用程序」中打开一次并处理系统提示") from exc

        converted = work / (source.stem + ".pdf")
        if result.returncode != 0 or not converted.is_file():
            raise RuntimeError("LibreOffice 未能生成 PDF，请检查文档是否损坏、加密，或先导出为 PDF 再上传")
        with converted.open("rb") as pdf:
            if pdf.read(5) != b"%PDF-":
                raise RuntimeError("LibreOffice 转换结果不是有效的 PDF，请先导出为 PDF 再上传")
        validate_pdf_glyphs(str(converted))
        if output_pdf or sys.platform == 'darwin':
            # Publish on the output volume, even if conversion ran on another disk.
            try:
                with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.pdf.tmp', delete=False) as stream:
                    staged = Path(stream.name)
                try:
                    shutil.copyfile(converted, staged)
                    staged.replace(destination)
                finally:
                    staged.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(f'无法保存转换后的 PDF：{exc}') from exc
        else:
            converted.replace(destination)
    return str(destination)


def _convert_via_gotenberg(abs_path: str, ext: str) -> str:
    """将各类可渲染文档委托给远程 Gotenberg 容器转为 PDF。"""
    src = Path(abs_path)
    dst = ensure_directory(CONVERSION_ROOT / "converted") / f"{uuid.uuid4().hex}.pdf"
    if ext in MARKDOWN_EXTS:
        gotenberg_convert_markdown(src, dst)
    elif ext in HTML_EXTS:
        gotenberg_convert_html(src, dst)
    else:
        gotenberg_convert_office(src, dst)
    return str(dst)


def _convert_locally(abs_path: str, ext: str) -> str:
    """使用本机 Office COM 或 LibreOffice 转换文档（仅限本地模式）。"""
    if ext not in (".docx", ".doc", ".xlsx", ".xls"):
        raise ValueError(f"本机转换模式暂不支持 {ext} 文件，请启用 Gotenberg 模式或先导出为 PDF")
    if sys.platform == "win32":
        return office_to_pdf_win32(abs_path)
    return office_to_pdf_libreoffice(abs_path)


def _convert_document(abs_path: str, ext: str) -> str:
    """根据 converter.mode 配置分派文档转换路由。"""
    mode = get_converter_config()["mode"]
    if mode == "gotenberg":
        return _convert_via_gotenberg(abs_path, ext)
    if mode == "local":
        return _convert_locally(abs_path, ext)
    try:
        return _convert_via_gotenberg(abs_path, ext)
    except GotenbergError as exc:
        logger.warning("Gotenberg 转换失败，尝试回退本机转换: %s", exc)
        return _convert_locally(abs_path, ext)


def prepare_printable_file(input_path: str) -> Tuple[str, int]:
    """
    通用预处理函数：
    接收任意支持格式的文件路径，返回 (可打印的PDF文件路径, 总页数)

    Args:
        input_path: 待打印文件的本地路径
    Returns:
        Tuple[str, int]: 转换后的 PDF 路径与总页数
    Example:
        >>> prepare_printable_file("demo.pdf")  # doctest: +SKIP
    """
    abs_path = os.path.abspath(input_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"文件不存在: {abs_path}")

    ext = os.path.splitext(abs_path)[1].lower()

    if ext == ".pdf":
        validate_pdf_glyphs(abs_path)
        pages = get_pdf_pages(abs_path)
        return abs_path, pages

    if ext in IMAGE_EXTS:
        pdf_path = image_to_pdf(abs_path)
        pages = get_pdf_pages(pdf_path)
        return pdf_path, pages

    if ext in OFFICE_EXTS | MARKDOWN_EXTS | HTML_EXTS:
        pdf_path = _convert_document(abs_path, ext)
        validate_pdf_glyphs(pdf_path)
        pages = get_pdf_pages(pdf_path)
        return pdf_path, pages

    raise ValueError(f"暂不支持打印 {ext or '无扩展名'} 文件；支持 PDF、Office、TXT、Markdown、HTML 与常见图片")
