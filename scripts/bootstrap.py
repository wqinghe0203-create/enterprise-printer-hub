"""Prepare this installation and open its local console without submitting a job."""

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import webbrowser

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.runtime_paths import ensure_directory


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == 'win32'
PYTHON = ROOT / '.venv' / ('Scripts/python.exe' if WINDOWS else 'bin/python')
READY_PATH = '/api/local-ready'
OFFICE_PATH = '/api/office-status'
MAC = sys.platform == 'darwin'
CONSOLE_HOST = '127.0.0.1'
# Windows CREATE_BREAKAWAY_FROM_JOB is not exported by Python's subprocess module.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
OFFICE_HELP = 'https://www.libreoffice.org/installation-instructions/'


class SetupBusy(RuntimeError):
    pass


def installation_id(root=ROOT):
    return hashlib.sha256(os.fsencode(os.path.normcase(str(Path(root).resolve())))).hexdigest()


@contextmanager
def bootstrap_lock(name='bootstrap.lock', wait=3):
    lock_path = ensure_directory(ROOT / 'data') / name
    with lock_path.open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        deadline = time.monotonic() + wait
        while True:
            try:
                if WINDOWS:
                    import msvcrt
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SetupBusy('另一个配置任务正在进行。')
                time.sleep(0.25)
        yield  # Closing the handle releases the OS lock, including on process exit.


def run(command, timeout=12):
    return subprocess.run(command, cwd=ROOT, timeout=timeout, check=False).returncode == 0


def python_check(code):
    if not PYTHON.is_file():
        return False
    try:
        result = subprocess.run([str(PYTHON), '-c', code], cwd=ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def prepare_python():
    imports = 'import sys; assert sys.version_info >= (3, 9); import pypdf, PIL, pymupdf'
    if WINDOWS:
        imports += ', pythoncom, win32com.client'
    if python_check(imports):
        return
    if not python_check('import sys; assert sys.version_info >= (3, 9)'):
        if not run([sys.executable, '-m', 'venv', str(ROOT / '.venv')]):
            raise RuntimeError('创建技能 Python 环境失败，请检查目录权限后重试。')
    if not python_check('import pip') and not run([str(PYTHON), '-m', 'ensurepip']):
        raise RuntimeError('无法恢复 pip，请检查技能目录的 Python 环境。')
    wheel_setting = os.environ.get('PRINT_HUB_WHEEL_DIR')
    index = os.environ.get('PRINT_HUB_PIP_INDEX_URL')
    offline = bool(wheel_setting)
    options = {}
    command = [str(PYTHON), '-m', 'pip', 'install', '--disable-pip-version-check']
    if offline or index:
        options['env'] = {key: value for key, value in os.environ.items() if not key.startswith('PIP_')}
        options['env']['PIP_CONFIG_FILE'] = os.devnull
    if offline:
        wheels = Path(wheel_setting).expanduser().resolve()
        if not wheels.is_dir() or wheels.is_relative_to(ROOT.resolve()):
            raise RuntimeError('PRINT_HUB_WHEEL_DIR 必须指向技能目录外的现有离线依赖目录。')
        command += ['--no-index', '--only-binary=:all:', '--find-links', str(wheels)]
    else:
        command += ['--timeout', '8', '--retries', '1']
        if index:
            options['env']['PIP_INDEX_URL'] = index
    command += ['-r', 'requirements.txt']
    timeout = 90 if WINDOWS or offline else 45
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=timeout, **options)
        if result.returncode == 0 and python_check(imports):
            return
        diagnostics = result.stdout + result.stderr
        reason = ('离线依赖无法安装或导入，请检查 wheel 与当前 Python 版本和系统架构是否匹配'
                  if offline else '下载源或代理返回 HTTP 502' if b'502' in diagnostics else '安装或导入失败')
    except subprocess.TimeoutExpired as error:
        diagnostics = (error.stdout or b'') + (error.stderr or b'')
        reason = (f'离线依赖安装超过 {timeout} 秒' if offline else
                  f'下载源或代理返回 HTTP 502，且下载超过 {timeout} 秒' if b'502' in diagnostics
                  else f'安装超过 {timeout} 秒')
    modules = ['pypdf', 'PIL', 'pymupdf'] + (['pythoncom', 'win32com.client'] if WINDOWS else [])
    missing = [name for name in modules if not python_check('import ' + name)]
    detail = '，未通过导入检查：' + '、'.join(missing) if missing else ''
    hint = '请重新运行技能配置入口。' if offline else '请检查网络后重试。'
    raise RuntimeError(f'Python 依赖配置：{reason}{detail}。{hint}')


def office_status():
    if WINDOWS:
        import winreg
        groups = [('Word', ('Word.Application', 'KWPS.Application', 'WPS.Application')),
                  ('Excel', ('Excel.Application', 'KET.Application', 'ET.Application'))]
        registered = {}
        for name, prog_ids in groups:
            registered[name.lower()] = False
            for prog_id in prog_ids:
                try:
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + '\\CLSID'):
                        registered[name.lower()] = True
                        break
                except OSError:
                    continue
        return {**registered, 'message': 'Office/WPS 组件以实际转换为准。缺少组件时请先导出 PDF。'}

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if find_office_executable():
        return office_state('unchecked', '已找到 LibreOffice，请在管理台查看或重新检测 Word 和 Excel。')
    return office_state('missing', '请将 LibreOffice 安装到「应用程序」，然后重新检测。')


def office_state(state, message):
    return {'state': state, 'message': message, 'word': state == 'ready',
            'excel': state == 'ready', 'help_url': OFFICE_HELP}


def find_office_executable():
    from scripts.doc_converter import find_soffice
    return find_soffice()


def probe_console(port):
    connection = http.client.HTTPConnection(CONSOLE_HOST, port, timeout=1)
    try:
        connection.request('GET', READY_PATH)
        response = connection.getresponse()
        if response.status != 200:
            return 'occupied'
        payload = json.loads(response.read(4096))
        return 'ready' if payload.get('installation') == installation_id() else 'occupied'
    except (http.client.HTTPException, ValueError, AttributeError):
        return 'occupied'
    except OSError:
        return 'unavailable'
    finally:
        connection.close()


def console_office_status(port, *, retry=False):
    if probe_console(port) != 'ready':
        return None
    connection = http.client.HTTPConnection(CONSOLE_HOST, port, timeout=5 if retry else 2)
    try:
        origin = f'http://{CONSOLE_HOST}:{port}'
        connection.request('POST' if retry else 'GET', OFFICE_PATH, headers={'Origin': origin} if retry else {})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f'无法读取控制台的 Office 检查状态（HTTP {response.status}）。')
        state = json.loads(response.read(16384))
        if not isinstance(state, dict) or not state.get('enabled') or not state.get('state'):
            raise RuntimeError('控制台未返回有效的 Office 检查状态。')
        return state
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        if retry:
            raise RuntimeError('无法确认复检请求，请在控制台查看当前状态。') from exc
        return None
    finally:
        connection.close()


def ensure_console(port):
    url = f'http://{CONSOLE_HOST}:{port}'
    status = probe_console(port)
    if status == 'ready':
        return url, True
    if status == 'occupied':
        raise RuntimeError(f'端口 {port} 已被其他服务占用，请使用 --port 指定其他端口。')
    log_path = ROOT / 'data' / 'console.log'
    flags = ({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
              | subprocess.CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB}
             if WINDOWS else {'start_new_session': True})
    with log_path.open('ab') as log:
        try:
            process = subprocess.Popen([str(PYTHON), '-u', 'scripts/web_server.py', str(port), '--host', CONSOLE_HOST],
                                       cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **flags)
        except OSError as exc:
            if WINDOWS and getattr(exc, 'winerror', None) == 5:
                raise RuntimeError('Windows 拒绝创建独立后台进程，请检查 WorkBuddy 的执行环境权限。') from exc
            raise
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        status = probe_console(port)
        if status == 'ready':
            # A server can listen briefly and then exit during startup.
            time.sleep(0.75)
            if process.poll() is None and probe_console(port) == 'ready':
                return url, False
        if status == 'occupied' or process.poll() is not None:
            break
        time.sleep(0.2)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
    raise RuntimeError(f'控制台未就绪（进程退出码 {process.poll()}），请查看日志 {log_path}。端口冲突时可用 --port 指定其他端口。')


def console_port(override):
    from scripts.config import get_server_config

    port = override if override is not None else int(get_server_config().get('web_ui_port', 18828))
    if not 1 <= port <= 65535:
        raise ValueError('配置的控制台端口应为 1–65535。')
    return port


def check_console(port, open_browser, wait=8):
    """Verify the console from a separate command without spawning a server."""
    deadline = time.monotonic() + wait
    while True:
        state = probe_console(port)
        if state == 'ready':
            break
        if state == 'occupied':
            raise RuntimeError(f'端口 {port} 被其他服务占用。')
        if time.monotonic() >= deadline:
            raise RuntimeError(f'本机打印后台未运行或已退出（端口 {port}），请查看后台任务输出。')
        time.sleep(0.25)
    connection = http.client.HTTPConnection(CONSOLE_HOST, port, timeout=2)
    try:
        connection.request('GET', '/')
        response = connection.getresponse()
        if response.status != 200 or 'text/html' not in response.getheader('Content-Type', ''):
            raise RuntimeError('打印后台已监听，但控制台页面无法加载。')
    finally:
        connection.close()
    url = f'http://{CONSOLE_HOST}:{port}/'
    opened = False
    if open_browser:
        try:
            opened = run(['/usr/bin/open', url], timeout=5) if MAC else webbrowser.open_new_tab(url)
        except (webbrowser.Error, OSError, subprocess.TimeoutExpired):
            pass
    return {'status': 'ready', 'url': url, 'console_verified': True, 'browser_opened': opened}


def main():
    parser = argparse.ArgumentParser(description='配置技能环境并启动本机打印控制台')
    parser.add_argument('--port', type=int)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--no-wait', action='store_true', help='检查控制台时只探测一次')
    parser.add_argument('--install-only', action='store_true')
    parser.add_argument('--first-use', action='store_true')
    parser.add_argument('--office-status', action='store_true')
    parser.add_argument('--retry-office', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--serve', action='store_true', help='在调用方管理的任务中持续运行网页服务')
    mode.add_argument('--check-console', action='store_true', help='核查现有服务和网页')
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error('端口应为 1–65535')
    started = time.monotonic()
    phase = '配置锁'
    try:
        if args.serve or args.check_console:
            phase = '网页控制台'
            port = console_port(args.port)
            if args.check_console:
                print(json.dumps(check_console(port, not args.no_browser, wait=0 if args.no_wait else 8),
                                 ensure_ascii=False))
                return 0
            state = probe_console(port)
            if state == 'ready':
                print(json.dumps({'status': 'already_running', 'url': f'http://{CONSOLE_HOST}:{port}/'}))
                return 0
            if state == 'occupied':
                raise RuntimeError(f'端口 {port} 被其他服务占用。')
            return subprocess.call([str(PYTHON), '-u', 'scripts/web_server.py', str(port),
                                    '--host', CONSOLE_HOST], cwd=ROOT)
        if args.office_status or args.retry_office:
            if MAC:
                from scripts.mac_office_check import CLI_TIMEOUT, start_check, status

                phase = 'Office 检查'
                port = console_port(args.port)
                office = console_office_status(port, retry=args.retry_office)
                if office is not None:
                    print(json.dumps(office, ensure_ascii=False))
                    return 0
                if Path(sys.prefix).resolve() != PYTHON.parent.parent.resolve():
                    if not python_check('import pypdf, PIL, pymupdf'):
                        print(json.dumps(office_state('needs_action', '技能 Python 依赖缺失，请重新运行首次配置。'), ensure_ascii=False))
                        return 1
                    flag = '--retry-office' if args.retry_office else '--office-status'
                    return subprocess.run([str(PYTHON), str(Path(__file__).resolve()), flag, '--port', str(port)],
                                          timeout=CLI_TIMEOUT).returncode
                office = start_check(force=True) if args.retry_office else status()
            else:
                office = office_status()
            print(json.dumps(office, ensure_ascii=False))
            return 0
        with bootstrap_lock():
            marker = ROOT / 'data' / 'bootstrap-ready'
            already_initialized = marker.exists()
            phase = 'Python 依赖'
            prepare_python()
            result = {'python': str(PYTHON), 'pdf_and_images': True,
                      'python_elapsed_seconds': round(time.monotonic() - started, 2)}
            if not args.install_only:
                phase = '网页控制台'
                port = console_port(args.port)
                console_started = time.monotonic()
                url, reused = ensure_console(port)
                result.update(url=url, reused=reused, browser_opened=False, console_verified=True,
                              console_elapsed_seconds=round(time.monotonic() - console_started, 2))
                if not args.no_browser and (not args.first_use or not already_initialized):
                    try:
                        result['browser_opened'] = (run(['/usr/bin/open', url], timeout=5)
                                                    if MAC else webbrowser.open_new_tab(url))
                    except (webbrowser.Error, OSError, subprocess.TimeoutExpired):
                        pass
                if not already_initialized:
                    marker.touch()
            phase = 'Office 检查'
            office = (console_office_status(port) if MAC and result.get('console_verified') else None) or office_status()
            if result.get('console_verified') and probe_console(port) != 'ready':
                raise RuntimeError('控制台进程在启动后退出或失去响应，请查看 data/console.log。')
            result.update(office=office, status='ready' if office['word'] and office['excel'] else 'partial',
                          elapsed_seconds=round(time.monotonic() - started, 2))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SetupBusy as error:
        print(json.dumps({'status': 'busy', 'phase': phase, 'message': str(error),
                          'office': office_status() if MAC else None}, ensure_ascii=False))
        return 2
    except (OSError, RuntimeError, ValueError, http.client.HTTPException, subprocess.TimeoutExpired) as error:
        print(json.dumps({'status': 'failed', 'phase': phase, 'message': str(error),
                          'elapsed_seconds': round(time.monotonic() - started, 2)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
