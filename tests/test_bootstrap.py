"""Bootstrap regressions using isolated files and a loopback readiness stub."""
import contextlib
import errno
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from scripts import bootstrap
from scripts.runtime_paths import ensure_directory


class BootstrapTests(unittest.TestCase):
    def test_posix_lock_permission_error_is_not_reported_as_busy(self):
        for number in (errno.EAGAIN, errno.EPERM, errno.EACCES):
            with self.subTest(errno=number), tempfile.TemporaryDirectory() as folder, \
                    patch.object(bootstrap, 'ROOT', Path(folder)), \
                    patch.object(bootstrap, 'WINDOWS', False), \
                    patch.dict('sys.modules', {'fcntl': Mock(LOCK_EX=2, LOCK_NB=4,
                                                           flock=Mock(side_effect=OSError(number, 'test')))}):
                expected = bootstrap.SetupBusy if number == errno.EAGAIN else PermissionError
                with self.assertRaises(expected) as failure:
                    with bootstrap.bootstrap_lock(wait=0):
                        self.fail('Lock acquisition should have failed')
                if number != errno.EAGAIN:
                    self.assertIn('授权', str(failure.exception))

    def test_managed_serve_runs_in_callers_task(self):
        with patch('sys.argv', ['bootstrap', '--serve', '--port', '18829']), \
                patch.object(bootstrap, 'probe_console', return_value='unavailable'), \
                patch.object(bootstrap.subprocess, 'call', return_value=0) as serve:
            self.assertEqual(bootstrap.main(), 0)
        self.assertEqual(serve.call_args.args[0][-4:], ['scripts/web_server.py', '18829', '--host', '127.0.0.1'])
        self.assertNotIn('creationflags', serve.call_args.kwargs)

    def test_first_use_opens_once_and_partial_can_start(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(bootstrap, 'ROOT', Path(temp)), \
                patch.object(bootstrap, 'MAC', False), \
                patch.object(bootstrap, 'prepare_python'), \
                patch.object(bootstrap, 'probe_console', return_value='ready'), \
                patch.object(bootstrap, 'office_status', return_value={'word': False, 'excel': False}) as office, \
                patch.object(bootstrap, 'ensure_console', side_effect=[('http://127.0.0.1:18829', False),
                                                                     ('http://127.0.0.1:18829', True)]), \
                patch.object(bootstrap.webbrowser, 'open_new_tab', return_value=True) as browser, \
                patch('sys.argv', ['bootstrap', '--first-use', '--port', '18829']), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bootstrap.main(), 0)
            self.assertEqual(bootstrap.main(), 0)
        first, second = map(json.loads, output.getvalue().splitlines())
        self.assertEqual(first['status'], 'partial')
        self.assertTrue(first['pdf_and_images'])
        self.assertTrue(first['browser_opened'])
        self.assertTrue(second['reused'])
        browser.assert_called_once_with(first['url'])
        self.assertEqual([call.args for call in office.call_args_list], [(), ()])

    def test_mac_open_reads_actual_background_check_status(self):
        events = []
        with tempfile.TemporaryDirectory() as temp, patch.object(bootstrap, 'ROOT', Path(temp)), \
                patch.object(bootstrap, 'MAC', True), patch.object(bootstrap, 'prepare_python'), \
                patch.object(bootstrap, 'probe_console', return_value='ready'), \
                patch.object(bootstrap, 'ensure_console', return_value=('http://127.0.0.1:18829', False)), \
                patch.object(bootstrap, 'run', side_effect=lambda *_args, **_kwargs: events.append('browser') or True), \
                patch.object(bootstrap, 'console_office_status', side_effect=lambda _: events.append('office') or
                             {'enabled': True, 'state': 'checking', 'word': False, 'excel': False}), \
                patch('sys.argv', ['bootstrap', '--first-use', '--port', '18829']), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bootstrap.main(), 0)
        self.assertEqual(events, ['browser', 'office'])
        self.assertEqual(json.loads(output.getvalue())['office']['state'], 'checking')

    def test_office_status_uses_get_and_only_retry_uses_post(self):
        response = Mock(status=200)
        response.read.return_value = b'{"enabled": true, "state": "unchecked"}'
        with patch.object(bootstrap, 'probe_console', return_value='ready'), \
                patch.object(bootstrap.http.client, 'HTTPConnection') as connect:
            connection = connect.return_value
            connection.getresponse.return_value = response
            self.assertEqual(bootstrap.console_office_status(18829)['state'], 'unchecked')
            connection.request.assert_called_once_with('GET', bootstrap.OFFICE_PATH, headers={})
            connection.request.reset_mock()
            bootstrap.console_office_status(18829, retry=True)
            connection.request.assert_called_once_with(
                'POST', bootstrap.OFFICE_PATH, headers={'Origin': 'http://127.0.0.1:18829'})

    def test_pip_502_is_bounded_and_output_is_not_exposed(self):
        failure = subprocess.CompletedProcess([], 1, b'', b'HTTP 502 https://user:secret@proxy.test')
        with patch.object(bootstrap, 'python_check', side_effect=lambda code: code in
                          ('import pip', 'import sys; assert sys.version_info >= (3, 9)')), \
                patch.object(bootstrap.subprocess, 'run', return_value=failure) as run:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 502.*pymupdf') as error:
                bootstrap.prepare_python()
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(run.call_args.kwargs['timeout'], 90 if bootstrap.WINDOWS else 45)
        self.assertIn('--retries', run.call_args.args[0])

    def test_directory_eexist_rechecks_type_and_permissions(self):
        with patch.object(Path, 'is_dir', side_effect=[False, True]), \
                patch.object(Path, 'mkdir', side_effect=FileExistsError(errno.EEXIST, 'exists')):
            self.assertEqual(ensure_directory('data'), Path('data'))
        for error in (FileExistsError(errno.EEXIST, 'exists'), PermissionError(errno.EACCES, 'denied')):
            with patch.object(Path, 'is_dir', return_value=False), patch.object(Path, 'mkdir', side_effect=error):
                with self.assertRaisesRegex(OSError, '无法使用目录 data'):
                    ensure_directory('data')

    def test_reuse_requires_installation_identity(self):
        identity = [bootstrap.installation_id()]

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    self.wfile.write(b'<html></html>')
                    return
                self.send_response(200 if self.path == bootstrap.READY_PATH else 404)
                self.end_headers()
                self.wfile.write(json.dumps({'installation': identity[0]}).encode())

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_port
                self.assertEqual(bootstrap.ensure_console(port), (f'http://127.0.0.1:{port}', True))
                self.assertTrue(bootstrap.check_console(port, False)['console_verified'])
                identity[0] = 'another-installation'
                with self.assertRaisesRegex(RuntimeError, '--port'):
                    bootstrap.ensure_console(port)
            finally:
                server.shutdown()
                thread.join()

    @unittest.skipUnless(bootstrap.WINDOWS, 'Windows process flags')
    def test_windows_starts_independent_process_and_rechecks(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(bootstrap, 'ROOT', Path(folder)), \
                patch.object(bootstrap, 'probe_console', side_effect=['unavailable', 'ready', 'ready']), \
                patch.object(bootstrap.subprocess, 'Popen') as start, \
                patch.object(bootstrap.time, 'sleep'):
            (Path(folder) / 'data').mkdir()
            start.return_value.poll.return_value = None
            self.assertEqual(bootstrap.ensure_console(18829), ('http://127.0.0.1:18829', False))
            self.assertTrue(start.call_args.kwargs['creationflags'] & bootstrap.CREATE_BREAKAWAY_FROM_JOB)

    def test_process_that_exits_after_first_probe_is_not_ready(self):
        process = Mock()
        process.poll.return_value = 1
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(bootstrap, 'ROOT', Path(folder)), \
                patch.object(bootstrap, 'probe_console', side_effect=['unavailable', 'ready']), \
                patch.object(bootstrap.subprocess, 'Popen', return_value=process), \
                patch.object(bootstrap.time, 'sleep'):
            (Path(folder) / 'data').mkdir()
            with self.assertRaisesRegex(RuntimeError, '未就绪'):
                bootstrap.ensure_console(18829)
