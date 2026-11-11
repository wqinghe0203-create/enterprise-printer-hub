"""Check both remote and local Office conversion capabilities with synthetic samples."""

import json
from pathlib import Path
import tempfile
import threading
import time
from typing import Dict, Any

from . import bootstrap, office_remote, office_local
from .doc_converter import office_to_pdf


SAMPLES = {'word': 'office-check.docx', 'excel': 'office-check.xlsx'}
_guard = threading.Lock()
_worker = None
_progress = None
_last_result = None


def result(state: str, message: str, *, formats: Dict[str, Any] = None, **details) -> Dict[str, Any]:
    formats = formats or {}
    return {
        'enabled': True,
        'state': state,
        'message': message,
        'formats': formats,
        **{kind: formats.get(kind, {}).get('ready', False) for kind in SAMPLES},
        **details
    }


def stamp(path: Path):
    info = path.stat()
    return [info.st_mtime_ns, info.st_size, info.st_ino]


def cache_file() -> Path:
    return office_remote.ROOT / 'data' / 'office-check.json'


def status() -> Dict[str, Any]:
    """Return unified office conversion status (remote + local fallback)."""
    local_engines = office_local.detect_local_engines()
    is_configured = office_remote.is_remote_configured()

    if not is_configured:
        # Remote not configured, evaluate local engines
        word_ok = local_engines.get('word', False)
        excel_ok = local_engines.get('excel', False)
        if word_ok and excel_ok:
            state = 'ready'
            msg = "本机 Office / WPS 已就绪，可直接打印。"
        elif word_ok or excel_ok:
            state = 'partial'
            msg = f"本机 Office 部分可用（{local_engines.get('details', '')}）。"
        else:
            state = 'needs_action'
            msg = "未配置远程 Office 微服务，且本机未检测到可用 Office 组件。可先导出为 PDF 再打印。"

        return result(
            state, msg,
            formats={
                'word': {'ready': word_ok, 'message': '已就绪' if word_ok else '未安装组件'},
                'excel': {'ready': excel_ok, 'message': '已就绪' if excel_ok else '未安装组件'},
            },
            mode='local' if (word_ok or excel_ok) else 'none',
            local=local_engines,
            remote={'configured': False, 'message': '未配置微服务'}
        )

    # Remote is configured; check cached or live status
    config = office_remote.ROOT / 'data' / 'office-converter.json'
    settings = office_remote.load_settings()
    try:
        key = [1, stamp(config), stamp(settings.ca_file) if settings.ca_file else None]
    except OSError:
        key = [1, None, None]

    with _guard:
        if _progress is not None and _progress[0] == key:
            _, started, kind, formats = _progress
            return result('checking', '正在用内置样例检查远程转换。', formats=dict(formats),
                          checking_format=kind, elapsed_seconds=int(time.monotonic() - started),
                          mode='remote', local=local_engines, remote={'configured': True, 'state': 'checking'})
        if _last_result is not None and _last_result[0] == key:
            res = dict(_last_result[1])
            res['local'] = local_engines
            return res
        try:
            saved = json.loads(cache_file().read_text(encoding='utf-8'))
            if saved['key'] == key and saved['result']['state'] in ('ready', 'partial', 'needs_action'):
                res = dict(saved['result'])
                res['local'] = local_engines
                return res
        except (OSError, ValueError, TypeError, KeyError):
            pass

    return result(
        'unchecked',
        '已配置远程转换服务。点击「重新检测」可验证 Word 和 Excel（支持本机自动兜底）。',
        mode='remote',
        local=local_engines,
        remote={'configured': True, 'state': 'unchecked'}
    )


def check_samples(settings, report) -> Dict[str, Any]:
    import pymupdf

    formats = {}
    with tempfile.TemporaryDirectory(prefix='office-check-') as folder:
        for kind, filename in SAMPLES.items():
            started = time.monotonic()
            report(kind, formats)
            sample_path = office_remote.ROOT / 'templates' / filename
            try:
                if not sample_path.is_file():
                    raise RuntimeError(f"缺少检查样本文件: {filename}")
                path, _ = office_to_pdf(str(sample_path), output_pdf=Path(folder) / f'{kind}.pdf', settings=settings)
                with pymupdf.open(path) as document:
                    text = ''.join(page.get_text() for page in document)
                    if '打印环境检查' not in text:
                        raise RuntimeError('样例 PDF 缺少预期中文内容，请检查字体配置')
                detail = {'ready': True, 'message': '转换通过'}
            except (OSError, RuntimeError, ValueError) as exc:
                detail = {'ready': False, 'message': str(exc)}
            formats[kind] = {**detail, 'elapsed_seconds': round(time.monotonic() - started, 2)}

    passed = sum(detail['ready'] for detail in formats.values())
    state = {0: 'needs_action', 1: 'partial', 2: 'ready'}[passed]
    return result(state, 'Office 转换检查完成。', formats=formats, mode='remote')


def start_check(*, background: bool = False) -> Dict[str, Any]:
    global _worker, _progress

    if not office_remote.is_remote_configured():
        return status()

    try:
        config = office_remote.ROOT / 'data' / 'office-converter.json'
        settings = office_remote.load_settings()
        key = [1, stamp(config), stamp(settings.ca_file) if settings.ca_file else None]
    except Exception as exc:
        return result('needs_action', f'无法读取 Office 转换配置: {exc}', mode='none')

    def report(kind, formats):
        global _progress
        with _guard:
            _progress = (key, started, kind, dict(formats))

    def run():
        global _last_result, _progress
        try:
            with bootstrap.bootstrap_lock('office-check.lock', wait=0):
                outcome = check_samples(settings, report)
                try:
                    cache = cache_file()
                    with tempfile.NamedTemporaryFile(dir=cache.parent, delete=False) as stream:
                        temporary = Path(stream.name)
                    try:
                        temporary.write_text(json.dumps({'key': key, 'result': outcome}, ensure_ascii=False), encoding='utf-8')
                        temporary.replace(cache)
                    finally:
                        temporary.unlink(missing_ok=True)
                except OSError:
                    outcome = {**outcome, 'message': 'Office 转换检查完成，无法保存结果缓存。'}
        except bootstrap.SetupBusy:
            outcome = result('needs_action', '另一进程正在检查 Office 转换，请稍后查看。')
        except Exception as exc:
            outcome = result('needs_action', f'无法执行 Office 检查: {exc}')
        finally:
            with _guard:
                _progress = None
                if 'outcome' in locals():
                    _last_result = (key, outcome)

    with _guard:
        if _worker is None or not _worker.is_alive():
            started = time.monotonic()
            _progress = (key, started, None, {})
            _worker = threading.Thread(target=run, name='office-check', daemon=True)
            _worker.start()
        worker = _worker
    if not background:
        worker.join()
    return status()
