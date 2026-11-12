"""Local, format-specific conversion checks shared by the Mac print entry points."""

import importlib.metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from . import bootstrap
from .doc_converter import (OFFICE_CONVERSION_TIMEOUT, find_soffice, libreoffice_process_options,
                            libreoffice_temp_directory, office_to_pdf_libreoffice)


FORMATS = {'.doc': 'word', '.docx': 'word', '.xls': 'excel', '.xlsx': 'excel'}
SAMPLES = {'word': 'office-check.docx', 'excel': 'office-check.xlsx'}
VERSION_TIMEOUT = 5
LOCK_WAIT = 3
CLI_TIMEOUT = LOCK_WAIT + VERSION_TIMEOUT + len(SAMPLES) * OFFICE_CONVERSION_TIMEOUT + 10
_launch_lock = threading.Lock()
_thread = None
_progress = {}
_outcome = None


def result(state, message, **details):
    return {**bootstrap.office_state(state, message), **details}


def signature(executable):
    files = [Path(executable).resolve(), Path(sys.executable).resolve(), Path(sys.prefix) / 'pyvenv.cfg',
             Path(__file__), Path(office_to_pdf_libreoffice.__code__.co_filename)]
    files.extend(bootstrap.ROOT / 'templates' / filename for filename in SAMPLES.values())
    stamps = []
    for path in files:
        try:
            info = path.stat()
            stamps.append([str(path), info.st_size, info.st_mtime_ns])
        except OSError:
            stamps.append([str(path), None, None])
    return [OFFICE_CONVERSION_TIMEOUT, sys.prefix, sys.version, stamps,
            [importlib.metadata.version(name) for name in ('pypdf', 'Pillow', 'PyMuPDF')]]


def check(executable, progress=None):
    started = time.monotonic()
    try:
        import PIL
        import pypdf
        import pymupdf
    except (ImportError, OSError):
        return result('needs_action', '技能 Python 依赖无法加载，请重新运行首次配置。')
    formats = {}
    with libreoffice_temp_directory('office-check-') as folder:
        work = Path(folder)
        try:
            probe = subprocess.run([executable, '--version'], capture_output=True, text=True,
                                   errors='replace', timeout=VERSION_TIMEOUT, stdin=subprocess.DEVNULL,
                                   **libreoffice_process_options(work))
            version = probe.stdout.strip().splitlines()
            if probe.returncode or not version:
                raise OSError('版本检测失败')
        except subprocess.TimeoutExpired:
            return result('needs_action', 'LibreOffice 启动检查超时。请在「应用程序」中打开一次并处理系统提示。')
        except OSError:
            return result('needs_action', '无法启动 LibreOffice。请在「应用程序」中打开一次并处理系统提示。')
        for kind, filename in SAMPLES.items():
            stage = time.monotonic()
            if progress:
                progress(kind, formats)
            try:
                pdf = work / (kind + '.pdf')
                office_to_pdf_libreoffice(str(bootstrap.ROOT / 'templates' / filename), str(pdf),
                                          executable=executable, timeout=OFFICE_CONVERSION_TIMEOUT)
                with pymupdf.open(pdf) as document:
                    if not document.is_pdf or not document.page_count:
                        raise RuntimeError('没有生成有效 PDF 页面')
                    if '打印环境检查' not in ''.join(page.get_text() for page in document):
                        raise RuntimeError('测试 PDF 缺少预期中文内容，请检查中文字体')
                formats[kind] = {'ready': True, 'message': '转换通过'}
            except (RuntimeError, OSError, ValueError) as exc:
                message = str(exc)
                if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                    message = f'转换检查超时（{OFFICE_CONVERSION_TIMEOUT} 秒），尚未创建打印任务'
                formats[kind] = {'ready': False, 'message': message}
            formats[kind]['elapsed_seconds'] = round(time.monotonic() - stage, 2)
    word, excel = (formats[key]['ready'] for key in SAMPLES)
    state = 'ready' if word and excel else 'partial' if word or excel else 'needs_action'
    return result(state, '已完成本机转换检查。', word=word, excel=excel,
                  version=' '.join(version[0].split()[:2]), formats=formats,
                  elapsed_seconds=round(time.monotonic() - started, 2))


def environment():
    executable = find_soffice()
    if not executable:
        return None, None, result('missing', '请将 LibreOffice 安装到「应用程序」，然后重新检测。')
    if sys.platform == 'darwin':
        executable = os.path.abspath(executable)
    try:
        return executable, signature(executable), None
    except importlib.metadata.PackageNotFoundError:
        return None, None, result('needs_action', '技能 Python 依赖缺失，请重新运行首次配置。')


def cached(cache, key, *, fresh=False):
    try:
        saved = json.loads(cache.read_text(encoding='utf-8'))
        outcome = saved['result']
        if (saved['signature'] == key and outcome['state'] in ('ready', 'partial', 'needs_action')
                and (not fresh or outcome['state'] != 'ready' or time.time() - saved['checked_at'] < 86400)):
            return outcome
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def current_progress(key):
    if _progress['key'] != key:
        return result('needs_action', '转换环境已变更，请等待当前检查结束后重新检测。')
    formats = _progress['formats']
    readiness = {kind: formats.get(kind, {}).get('ready', False) for kind in SAMPLES}
    now = time.monotonic()
    if now - _progress['check_started'] >= CLI_TIMEOUT:
        return result('needs_action', '转换环境检查已超时，后台仍在收尾。请稍后重新检测。',
                      formats=formats, **readiness)
    return result('checking', '正在用测试文件检查 Word 和 Excel 转换。',
                  checking_format=_progress.get('kind'), elapsed_seconds=int(now - _progress['started']),
                  formats=formats, **readiness)


def status():
    """Read progress or a result for the current environment without starting a check."""
    _, key, error = environment()
    if error:
        return error
    with _launch_lock:
        if _thread is not None and _thread.is_alive():
            return current_progress(key)
        if _outcome is not None and _outcome[0] == key:
            return _outcome[1]
        cache = bootstrap.ROOT / 'data' / 'mac-office-check.json'
        return cached(cache, key) or result('unchecked', '尚无有效的 Word／Excel 检查结果，请点击「重新检测」。')


def start_check(*, force=False, background=False):
    global _thread, _outcome
    executable, key, error = environment()
    if error:
        return error
    cache = bootstrap.ROOT / 'data' / 'mac-office-check.json'

    def report(kind, formats):
        with _launch_lock:
            _progress.update(kind=kind, started=time.monotonic(), formats=dict(formats))

    def run():
        outcome = None
        try:
            with bootstrap.bootstrap_lock('mac-office-check.lock', wait=LOCK_WAIT):
                if not force and (saved := cached(cache, key, fresh=True)) is not None:
                    return saved
                outcome = check(executable, progress=report)
                with tempfile.NamedTemporaryFile(dir=cache.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                try:
                    temporary.write_text(json.dumps({'signature': key, 'checked_at': time.time(),
                                                     'result': outcome}, ensure_ascii=False), encoding='utf-8')
                    temporary.replace(cache)
                finally:
                    temporary.unlink(missing_ok=True)
                return outcome
        except bootstrap.SetupBusy:
            return result('needs_action', '另一项 Word／Excel 检查仍在运行，请等待它结束后重新检测。')
        except OSError:
            logging.exception('Mac Office check could not access its files')
            if outcome is None:
                return result('needs_action', '无法访问检查所需的文件，请检查技能目录和临时目录权限。')
            return {**outcome, 'state': 'needs_action',
                    'message': '无法保存检查结果，请检查技能目录权限。'}
        except Exception:
            logging.exception('Mac Office check failed')
            return result('needs_action', '转换环境检查异常，请重新检测。详细原因见后台日志。')

    def run_background():
        global _outcome
        outcome = run()
        with _launch_lock:
            _outcome = (key, outcome)

    with _launch_lock:
        if _thread is None or not _thread.is_alive():
            if not force:
                if _outcome is not None and _outcome[0] == key:
                    return _outcome[1]
                if (saved := cached(cache, key, fresh=True)) is not None:
                    _outcome = (key, saved)
                    return saved
            _progress.clear()
            started = time.monotonic()
            _progress.update(key=key, check_started=started, started=started, formats={})
            _thread = threading.Thread(target=run_background, daemon=True, name='mac-office-check')
            _thread.start()
        worker = _thread
        if background:
            return current_progress(key)
    worker.join(CLI_TIMEOUT)
    return status()


def submission_error(path, *, use_console=False):
    kind = FORMATS.get(Path(path).suffix.lower())
    if not kind:
        return None
    current = (bootstrap.console_office_status(bootstrap.console_port(None)) if use_console else None) or status()
    if current.get(kind):
        return None
    return current.get('formats', {}).get(kind, {}).get('message') or current['message']
