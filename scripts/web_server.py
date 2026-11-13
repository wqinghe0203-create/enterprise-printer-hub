# -*- coding: utf-8 -*-
"""
企业级智能文印中心 Web 服务
- 支持全公司多打印机大盘与设备切换
- 局域网自适应访问 (动态获取运行主机的真实局域网 IP，谁安装谁启动)
- 零第三方 Web 依赖，兼容 Python 3.9 ~ 3.13+ (无废弃 cgi 模块依赖)
- 提供可视化监控面板与 REST API
"""

import os
import argparse
import hmac
import ipaddress
import sys
import json
import uuid
import threading
import sqlite3
import urllib.parse
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from email.parser import BytesParser
from email import policy

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scripts.printer_service import PrinterService
from scripts.config import (
    get_auth_config,
    get_converter_config,
    get_printer,
    get_server_config,
    set_default_printer,
)
from scripts.doc_converter import SUPPORTED_EXTS
from scripts.gotenberg_client import health as gotenberg_health
from scripts.net_utils import get_host_lan_ip
from scripts.bootstrap import MAC, OFFICE_PATH, READY_PATH, installation_id
from scripts import mac_office_check
from scripts.runtime_paths import ensure_directory

TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")
UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")
ensure_directory(UPLOAD_DIR)

from scripts.printer_registration import ProbeRegistry, RegistrationError

svc = PrinterService()
probes = ProbeRegistry()


class PrinterWebHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if not self.path.startswith(('/api/printer-probes', '/api/printers')):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def _is_loopback_client(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _provided_token(self, header_name="X-Print-Hub-Token"):
        token = self.headers.get(header_name, "").strip()
        if token:
            return token
        auth_val = self.headers.get("Authorization", "").strip()
        if auth_val.lower().startswith("bearer "):
            return auth_val[7:].strip()
        return ""

    def _api_authorized(self):
        """校验非本机 API 请求令牌；未配置 PRINT_HUB_API_TOKEN 时默认放行。"""
        auth = get_auth_config()
        api_token = auth["api_token"]
        if not api_token or self._is_loopback_client():
            return True
        provided = self._provided_token("X-Print-Hub-Token")
        if provided and hmac.compare_digest(provided, api_token):
            return True
        admin_token = auth["admin_token"]
        admin_provided = self.headers.get("X-Print-Hub-Admin-Token", "").strip()
        return bool(admin_token and admin_provided and hmac.compare_digest(admin_provided, admin_token))

    def _require_api_auth(self):
        if self._api_authorized():
            return True
        self._send_json({"success": False, "code": "unauthorized", "error": "缺少或无效的访问令牌 (PRINT_HUB_API_TOKEN)"}, 401)
        return False

    def _management_allowed(self, mutation=False):
        admin_token = get_auth_config()["admin_token"]
        admin_provided = self.headers.get("X-Print-Hub-Admin-Token", "").strip()
        if admin_token and admin_provided and hmac.compare_digest(admin_provided, admin_token):
            return True
        try:
            if not self._is_loopback_client():
                return False
            host = self.headers.get('Host', '')
            parsed = urllib.parse.urlsplit('http://' + host)
            if parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.path or parsed.query or parsed.fragment:
                return False
            if (parsed.port or 80) != self.server.server_port:
                return False
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + host:
                return False
            return not mutation or origin == 'http://' + host
        except (ValueError, TypeError):
            return False

    def _registration_origin(self):
        return os.environ.get('PRINT_HUB_REGISTRATION_ORIGIN', '').strip().rstrip('/')

    def _registration_allowed(self, mutation=False):
        if self._management_allowed(mutation):
            return True
        registration_origin = self._registration_origin()
        if not registration_origin:
            return False
        origin = self.headers.get('Origin')
        if mutation or origin is not None:
            return origin == registration_origin
        # Same-origin GETs usually omit Origin. Reject cross-site browser reads.
        return self.headers.get('Sec-Fetch-Site') in (None, 'same-origin')

    def _registration_request(self, path, mutation=False):
        if not self._require_api_auth():
            return
        if not self._registration_allowed(mutation):
            self._send_json({'success':False,'error':'请从允许添加设备的后台地址打开打印设备页面，或使用管理员令牌'}, 403)
            return
        try:
            if mutation:
                if self.headers.get_content_type() != 'application/json':
                    raise RegistrationError('请求必须使用 application/json', 415)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise RegistrationError('请求长度无效')
                if not 0 < length <= 16384:
                    raise RegistrationError('请求内容过大或为空')
                try:
                    data = json.loads(self.rfile.read(length).decode('utf-8'))
                except (ValueError, UnicodeError):
                    raise RegistrationError('JSON 格式不正确')
                if not isinstance(data, dict):
                    raise RegistrationError('请求内容必须为 JSON 对象')
                if path == '/api/printer-probes':
                    self._send_json({'success':True, **probes.start(data)}, 202)
                else:
                    printer, created = probes.save(data)
                    self._send_json({'success':True,'printer':printer,'created':created}, 201 if created else 200)
            else:
                result = probes.get(path.rsplit('/', 1)[-1])
                self._send_json({'success':True, **result})
        except RegistrationError as exc:
            self._send_json({'success':False,'error':str(exc)}, exc.status)
        except (OSError, ValueError):
            self._send_json({'success':False,'error':'配置保存失败，原配置未替换；请检查 config.json 及目录权限后重试'}, 500)

    def do_GET(self):
        try:
            self._get()
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            self._send_json({'success': False, 'error': str(exc)}, 400 if isinstance(exc, ValueError) else 500)

    def do_POST(self):
        try:
            self._post()
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            self._send_json({'success': False, 'error': str(exc)}, 400 if isinstance(exc, ValueError) else 500)

    def _get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith('/api/printer-probes/'):
            self._registration_request(path)
        elif path == READY_PATH:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                self.send_error(404)
            elif not os.path.isfile(TEMPLATE_PATH):
                self.send_error(503, 'Template Not Found')
            else:
                self._send_json({'installation': installation_id()})
        elif path == OFFICE_PATH:
            if not self._management_allowed():
                self.send_error(404)
            else:
                local_mode = MAC and get_converter_config()['mode'] == 'local'
                self._send_json({'enabled': local_mode, **(mac_office_check.status() if local_mode else {})})
        elif path == '/api/converter-status':
            if not self._require_api_auth():
                return
            cfg = get_converter_config()
            if cfg['mode'] == 'local':
                self._send_json({'mode': 'local', 'up': True, 'status': 'local'})
            else:
                status_info = gotenberg_health()
                payload = {'mode': cfg['mode'], 'up': status_info['up'], 'status': status_info['status']}
                if self._management_allowed():
                    payload['url'] = status_info['url']
                self._send_json(payload)
        elif path in ("/", "/index.html"):
            if os.path.exists(TEMPLATE_PATH):
                with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                    content = f.read().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404, "Template Not Found")
        elif path == "/api/dashboard":
            if not self._require_api_auth():
                return
            printer_id = query.get("printer_id", [None])[0]
            data = svc.get_full_dashboard_data(selected_printer_id=printer_id)
            data['printer_management_allowed'] = self._management_allowed()
            data['printer_registration_origin'] = self._registration_origin() if self._registration_allowed() else ''
            data["host_lan_ip"] = get_host_lan_ip()
            data["web_ui_port"] = self.server.server_port
            self._send_json(data)
        elif path == '/api/history':
            if not self._require_api_auth():
                return
            args = {key: query.get(key, [''])[0] for key in ('search', 'user', 'status', 'start', 'end')}
            result = svc.scheduler.history(query.get('printer_id', [None])[0],
                int(query.get('page', ['1'])[0]), int(query.get('size', ['5'])[0]), **args)
            self._send_json({'success': True, **result})
        elif path == '/api/task':
            if not self._require_api_auth():
                return
            if 'request_id' in query:
                job = svc.scheduler.get_request(query['request_id'][0])
            else:
                job = svc.scheduler.get_job(int(query.get('job_id', ['0'])[0]))
            if job and query.get('verify', ['0'])[0] == '1':
                svc.sync_hardware_jobs(job['printer_id'])
                job = svc.scheduler.get_job(job['id'])
            self._send_json({'success': True, 'job': job})
        elif path == "/api/cluster":
            if not self._require_api_auth():
                return
            cluster = svc.get_cluster_overview()
            self._send_json(cluster)
        elif path == "/api/diag":
            if not self._require_api_auth():
                return
            printer_id = query.get("printer_id", [None])[0]
            from scripts.diag import run_full_diagnosis
            diag_res = run_full_diagnosis(svc.resolve_printer_by_keyword(printer_id))
            self._send_json(diag_res)
        else:
            self.send_error(404, "Not Found")

    def _post(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == OFFICE_PATH:
            if not (MAC and get_converter_config()['mode'] == 'local') or not self._management_allowed(mutation=True):
                self.send_error(404)
            else:
                self._send_json({'enabled': True, **mac_office_check.start_check(force=True, background=True)})
        elif path in ('/api/printer-probes', '/api/printers'):
            self._registration_request(path, mutation=True)
        elif path == '/api/default-printer':
            if not self._management_allowed(mutation=True):
                self._send_json({'success': False, 'error': '请在服务电脑打开本机控制台，或提供管理员令牌后设置默认打印机'}, 403)
                return
            printer = set_default_printer(query.get('printer_id', [''])[0])
            self._send_json({'success': True, 'default_printer_id': printer['id']})
        elif path == "/api/cut_in":
            if not self._require_api_auth():
                return
            job_id = int(query.get("job_id", [0])[0])
            res = svc.emergency_cut_in(job_id)
            self._send_json(res)

        elif path == "/api/priority":
            if not self._require_api_auth():
                return
            job_id = int(query.get("job_id", [0])[0])
            prio = int(query.get("priority", [50])[0])
            res = svc.adjust_priority(job_id, prio)
            self._send_json(res)

        elif path == "/api/cancel":
            if not self._require_api_auth():
                return
            job_id = int(query.get("job_id", [0])[0])
            is_hw = (query.get("is_hw", ["false"])[0].lower() == "true")
            pid = query.get("printer_id", [None])[0]
            res = svc.cancel_job(job_id, printer_id=pid, is_hardware=is_hw)
            self._send_json(res)

        elif path == "/api/upload_print":
            if not self._require_api_auth():
                return
            c_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in c_type:
                content_len = int(self.headers.get("Content-Length", 0))
                max_bytes = get_converter_config()["max_upload_mb"] * 1024 * 1024 + 1024 * 1024
                if content_len <= 0 or content_len > max_bytes:
                    self._send_json({"success": False, "error": f"文件大小须在 {get_converter_config()['max_upload_mb']} MB 以内"}, 413)
                    return
                body_bytes = self.rfile.read(content_len)

                # 使用标准库 email.parser 解析 multipart，兼容 Python 3.9 ~ 3.13+
                raw_msg = f"Content-Type: {c_type}\r\n\r\n".encode("utf-8") + body_bytes
                msg = BytesParser(policy=policy.default).parsebytes(raw_msg)

                user = "用户"
                priority = 50
                printer_target = None
                duplex = None
                copies_text = '1'
                request_id = None
                file_bytes = b""
                orig_filename = "document.pdf"

                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name == "user":
                        payload = part.get_payload(decode=True)
                        user = payload.decode("utf-8", errors="replace").strip() if payload else "用户"
                    elif name == "priority":
                        payload = part.get_payload(decode=True)
                        try:
                            priority = int(payload.decode("utf-8").strip())
                        except Exception:
                            priority = 50
                    elif name == "printer":
                        payload = part.get_payload(decode=True)
                        printer_target = payload.decode("utf-8", errors="replace").strip() if payload else None
                    elif name == "duplex":
                        payload = part.get_payload(decode=True)
                        duplex = payload.decode("utf-8", errors="replace").strip() if payload else ""
                    elif name == 'request_id':
                        request_id = (part.get_payload(decode=True) or b'').decode('ascii').strip()
                    elif name == 'copies':
                        copies_text = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace').strip()
                    elif name == "file":
                        part_fn = part.get_filename()
                        if part_fn:
                            orig_filename = part_fn
                        file_bytes = part.get_payload(decode=True) or b""

                if not file_bytes:
                    self._send_json({"success": False, "error": "未接收到有效文件内容"}, 400)
                    return

                if duplex is not None and duplex not in ("auto", "simplex", "duplex_long", "duplex_short"):
                    self._send_json({"success": False, "error": "不支持的单双面选项"}, 400)
                    return

                if not (1 <= len(copies_text) <= 2 and copies_text.isascii() and copies_text.isdigit()
                        and 1 <= int(copies_text) <= 99):
                    self._send_json({'success': False, 'error': '打印份数须为 1–99 的整数'}, 400)
                    return

                if request_id and (len(request_id) > 100 or not all(c.isalnum() or c in '-_' for c in request_id)):
                    raise ValueError('提交标识无效')
                existing = svc.scheduler.get_request(request_id) if request_id else None
                if existing:
                    self._send_json({'success': True, 'job': existing, 'message': '已找到本次提交，未重复创建任务'})
                    return
                if MAC and get_converter_config()['mode'] == 'local':
                    office_error = mac_office_check.submission_error(orig_filename)
                    if office_error:
                        self._send_json({'success': False, 'code': 'office_not_ready', 'error': office_error}, 409)
                        return
                ext = os.path.splitext(orig_filename)[1].lower()
                if ext not in SUPPORTED_EXTS:
                    self._send_json({'success': False, 'error': f'暂不支持打印 {ext or "无扩展名"} 文件；支持 PDF、Office、TXT、Markdown、HTML 与常见图片'}, 400)
                    return
                target_pid = svc.resolve_printer_by_keyword(printer_target)
                target_config = get_printer(target_pid)
                # 保存文件
                safe_name = os.path.basename(orig_filename)
                save_filename = f"upload_{uuid.uuid4().hex}_{safe_name}"
                dest_path = os.path.join(UPLOAD_DIR, save_filename)
                with open(dest_path, "wb") as f:
                    f.write(file_bytes)
                    f.flush()
                    os.fsync(f.fileno())

                default_duplex = target_config.get('default_duplex', 'duplex_long' if target_config.get('duplex_supported', True) else 'simplex')
                res = svc.enqueue_upload(
                    filepath=dest_path,
                    user=user,
                    printer_target=target_pid,
                    filename=orig_filename,
                    priority=priority,
                    duplex=duplex if duplex is not None else default_duplex,
                    copies=int(copies_text),
                    request_id=request_id
                )
                # Concurrent idempotent uploads can leave an unused unique file.
                if res['job']['filepath'] != os.path.abspath(dest_path):
                    os.unlink(dest_path)
                self._send_json(res, 202)
            else:
                self._send_json({"success": False, "error": "Invalid Content-Type"}, 400)
        else:
            self.send_error(404, "Endpoint Not Found")

    def log_message(self, format, *args):
        pass


def run_server(port=None, host="0.0.0.0"):
    cfg = get_server_config()
    server_port = port or cfg.get("web_ui_port", 18828)
    server_address = (host, server_port)
    httpd = ThreadingHTTPServer(server_address, PrinterWebHandler)
    stop = threading.Event()
    worker = threading.Thread(target=svc.run_worker, args=(stop,), daemon=True, name='print-worker')
    worker.start()
    if MAC and get_converter_config()['mode'] == 'local':
        mac_office_check.start_check(background=True)
    local_ip = get_host_lan_ip()

    print("==========================================================")
    print("  Enterprise Print Hub Web Server Started")
    print(f"  Local URL : http://localhost:{server_port}")
    if host == "0.0.0.0":
        print(f"  LAN URL   : http://{local_ip}:{server_port}")
    else:
        print(f"  Bind URL  : http://{host}:{server_port}")
    print("==========================================================")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        stop.set()
        httpd.server_close()
        worker.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="企业智能文印 Web 控制台")
    parser.add_argument("port", nargs="?", type=int, default=None)
    parser.add_argument("--host", default="0.0.0.0", help="监听地址；仅本机访问使用 127.0.0.1")
    args = parser.parse_args()
    run_server(args.port, host=args.host)
