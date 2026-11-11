"""Lightweight local Office conversion engine with graceful fallback.

Supports:
- Windows COM automation (Microsoft Word/Excel or WPS Office)
- macOS / Linux Headless LibreOffice
- Automatic engine discovery and isolated execution
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional, Tuple, Dict, Any


OFFICE_SUFFIXES = frozenset({'.doc', '.docx', '.xls', '.xlsx'})
LOCAL_CONVERSION_TIMEOUT = 45


class LocalConversionError(RuntimeError):
    """Raised when local conversion cannot complete."""
    pass


def find_soffice() -> Optional[str]:
    """Find LibreOffice executable from PATH or common standard paths."""
    for name in ("soffice", "libreoffice"):
        executable = shutil.which(name)
        if executable:
            return executable

    candidates = [
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        Path.home() / "Applications/LibreOffice.app/Contents/MacOS/soffice",
        Path("C:/Program Files/LibreOffice/program/soffice.exe"),
        Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
        Path("/usr/bin/soffice"),
        Path("/usr/local/bin/soffice"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None


def detect_local_engines() -> Dict[str, Any]:
    """Detect available local conversion engines on current OS without launching apps."""
    engines: Dict[str, Any] = {
        'word': False,
        'excel': False,
        'engine': None,
        'details': '未检测到可用本地 Office 转换组件',
    }

    # 1. Check Windows COM if on Windows
    if sys.platform == 'win32':
        try:
            import winreg
            word_prog_ids = ("Word.Application", "KWPS.Application", "WPS.Application")
            excel_prog_ids = ("Excel.Application", "KET.Application", "ET.Application")

            word_ok = any(_is_prog_id_registered(p, winreg) for p in word_prog_ids)
            excel_ok = any(_is_prog_id_registered(p, winreg) for p in excel_prog_ids)

            # Check if pywin32 is importable
            pywin32_ok = False
            try:
                import pythoncom  # noqa: F401
                import win32com.client  # noqa: F401
                pywin32_ok = True
            except ImportError:
                pywin32_ok = False

            if word_ok or excel_ok:
                if pywin32_ok:
                    engines['word'] = word_ok
                    engines['excel'] = excel_ok
                    engines['engine'] = 'win32_com'
                    engines['details'] = 'Windows Office/WPS COM 组件已就绪'
                    return engines
                else:
                    engines['details'] = '检测到 Office/WPS，但未安装 pywin32 库'
        except Exception:
            pass

    # 2. Check LibreOffice
    soffice = find_soffice()
    if soffice:
        engines['word'] = True
        engines['excel'] = True
        engines['engine'] = 'libreoffice'
        engines['details'] = f'LibreOffice 已就绪 ({soffice})'
        return engines

    return engines


def _is_prog_id_registered(prog_id: str, winreg_module) -> bool:
    try:
        with winreg_module.OpenKey(winreg_module.HKEY_CLASSES_ROOT, prog_id + r'\CLSID'):
            return True
    except OSError:
        return False


def office_to_pdf_win32(office_path: str, output_pdf: str, timeout: int = LOCAL_CONVERSION_TIMEOUT) -> str:
    """Convert Office document to PDF using Windows COM (Word/WPS/Excel) with timeout watchdog."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        raise LocalConversionError("未安装 pywin32 依赖，无法调用本地 Windows Office COM。请安装 pywin32 或配置内网转换微服务。")

    abs_source = os.path.abspath(office_path)
    out_abs = os.path.abspath(output_pdf)
    ext = os.path.splitext(abs_source)[1].lower()

    exc_holder = []

    def _convert_thread():
        pythoncom.CoInitialize()
        app = None
        try:
            if ext in (".docx", ".doc"):
                for prog_id in ("Word.Application", "kwps.Application", "wps.Application"):
                    try:
                        app = win32com.client.Dispatch(prog_id)
                        break
                    except Exception:
                        continue
                if not app:
                    raise LocalConversionError("本机未检测到 Microsoft Word 或 WPS，无法自动转换 Doc/Docx")

                app.Visible = False
                app.DisplayAlerts = 0
                doc = app.Documents.Open(abs_source, ReadOnly=True)
                doc.SaveAs(out_abs, FileFormat=17)  # wdFormatPDF = 17
                doc.Close(False)

            elif ext in (".xlsx", ".xls"):
                for prog_id in ("Excel.Application", "ket.Application", "et.Application"):
                    try:
                        app = win32com.client.Dispatch(prog_id)
                        break
                    except Exception:
                        continue
                if not app:
                    raise LocalConversionError("本机未检测到 Microsoft Excel 或 WPS 表格，无法自动转换 Xls/Xlsx")

                app.Visible = False
                app.DisplayAlerts = False
                wb = app.Workbooks.Open(abs_source, ReadOnly=True)
                wb.ExportAsFixedFormat(0, out_abs)  # xlTypePDF = 0
                wb.Close(False)
            else:
                raise LocalConversionError(f"不支持的本地 Office 格式: {ext}")
        except Exception as e:
            exc_holder.append(e)
        finally:
            if app:
                try:
                    app.Quit()
                except Exception:
                    pass
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=_convert_thread, name="office-win32-convert", daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        raise LocalConversionError(f"本地 Office COM 转换超时（{timeout} 秒），请检查是否有未关闭的 Office 弹窗或文档损坏")

    if exc_holder:
        raise LocalConversionError(f"本地 Office 转换失败: {exc_holder[0]}")

    if not os.path.isfile(out_abs) or os.path.getsize(out_abs) == 0:
        raise LocalConversionError("本地 Office 转换未能生成有效的 PDF 文件")

    return out_abs


def office_to_pdf_libreoffice(office_path: str, output_pdf: str, timeout: int = LOCAL_CONVERSION_TIMEOUT) -> str:
    """Convert Office document to PDF using local Headless LibreOffice in an isolated sandbox."""
    executable = find_soffice()
    if not executable:
        raise LocalConversionError("本机未检测到 LibreOffice，请安装 LibreOffice 或配置内网转换微服务")

    source = Path(office_path).resolve()
    out_path = Path(output_pdf).resolve()

    with tempfile.TemporaryDirectory(prefix="local-libreoffice-") as work_dir:
        work = Path(work_dir)
        profile = work / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        local_source = work / source.name
        shutil.copyfile(source, local_source)

        command = [
            executable,
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--convert-to", "pdf",
            "--outdir", str(work),
            str(local_source),
        ]

        env = dict(os.environ)
        if sys.platform == 'darwin':
            for name in ('TMPDIR', 'TMP', 'TEMP'):
                env[name] = str(work)

        try:
            result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise LocalConversionError(f"LibreOffice 转换超时（{timeout} 秒）")
        except OSError as exc:
            raise LocalConversionError(f"无法启动 LibreOffice: {exc}")

        converted = work / (source.stem + ".pdf")
        if result.returncode != 0 or not converted.is_file():
            raise LocalConversionError("LibreOffice 未能生成 PDF，请检查文档是否损坏或加密")

        with converted.open("rb") as f:
            if f.read(5) != b"%PDF-":
                raise LocalConversionError("LibreOffice 转换结果不是有效的 PDF")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(converted), str(out_path))

    return str(out_path)


def office_to_pdf_local(office_path: str, output_pdf: str) -> str:
    """Top-level entry for local conversion, automatically picking the best available engine."""
    engines = detect_local_engines()
    engine = engines.get('engine')

    if engine == 'win32_com':
        return office_to_pdf_win32(office_path, output_pdf)
    elif engine == 'libreoffice':
        return office_to_pdf_libreoffice(office_path, output_pdf)
    else:
        # If neither detected clearly, try soffice as last resort
        if find_soffice():
            return office_to_pdf_libreoffice(office_path, output_pdf)
        raise LocalConversionError(f"本机无可用的 Office 转换组件（{engines.get('details', '')}）。可先导出为 PDF 再打印。")
