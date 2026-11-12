# -*- coding: utf-8 -*-
"""
Gotenberg 文档转换客户端、转换路由、远程瘦客户端与鉴权逻辑单元测试
使用本机临时 HTTP 桩服务隔离测试，不依赖外部 Docker 或真实打印机。
"""

import base64
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from scripts import config as cfg_mod
from scripts import doc_converter
from scripts import gotenberg_client
from scripts import remote_cli
from scripts import web_server


class _StubGotenbergHandler(http.server.BaseHTTPRequestHandler):
    routes_seen = []
    auth_seen = []
    bodies_seen = []
    mode = "ok"  # ok | bad_400 | timeout_503 | bad_pdf | down

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.auth_seen.append(self.headers.get("Authorization"))
            payload = json.dumps({"status": "up"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.routes_seen.append(self.path)
        self.auth_seen.append(self.headers.get("Authorization"))
        self.bodies_seen.append(body)

        if self.mode == "bad_400":
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"bad document")
            return
        if self.mode == "timeout_503":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"timeout")
            return
        if self.mode == "bad_pdf":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"NOT_A_PDF")
            return

        # 返回最小合法 1 页 PDF（使用 PyMuPDF 或原始 PDF 文本供页数解析）
        pdf_bytes = _build_minimal_pdf()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(pdf_bytes)))
        self.end_headers()
        self.wfile.write(pdf_bytes)


def _build_minimal_pdf() -> bytes:
    """生成仅包含 ASCII 字符的最小有效单页 PDF。"""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >> endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n"
        b"0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer << /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF\n"
    )


class GotenbergAndRemoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubGotenbergHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _StubGotenbergHandler.routes_seen.clear()
        _StubGotenbergHandler.auth_seen.clear()
        _StubGotenbergHandler.bodies_seen.clear()
        _StubGotenbergHandler.mode = "ok"
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "PRINT_HUB_CONVERTER_MODE": "gotenberg",
                "PRINT_HUB_GOTENBERG_URL": f"http://127.0.0.1:{self.port}",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def test_health_and_basic_auth(self):
        res = gotenberg_client.health()
        self.assertTrue(res["up"])
        self.assertIsNone(_StubGotenbergHandler.auth_seen[-1])

        with mock.patch.dict(
            os.environ,
            {"PRINT_HUB_GOTENBERG_USER": "u1", "PRINT_HUB_GOTENBERG_PASSWORD": "p1"},
        ):
            res2 = gotenberg_client.health()
            self.assertTrue(res2["up"])
            expected = "Basic " + base64.b64encode(b"u1:p1").decode("ascii")
            self.assertEqual(_StubGotenbergHandler.auth_seen[-1], expected)

    def test_office_markdown_html_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            docx = tmp_dir / "报告.docx"
            docx.write_bytes(b"fake-docx")
            md = tmp_dir / "笔记.md"
            md.write_text("# 标题\n正文", encoding="utf-8")
            html = tmp_dir / "页面.html"
            html.write_text("<h1>Hi</h1>", encoding="utf-8")

            with mock.patch.object(doc_converter, "validate_pdf_glyphs"):
                pdf1, pages1 = doc_converter.prepare_printable_file(str(docx))
                self.assertEqual(pages1, 1)
                self.assertTrue(Path(pdf1).read_bytes().startswith(b"%PDF-"))
                self.assertEqual(_StubGotenbergHandler.routes_seen[-1], "/forms/libreoffice/convert")

                pdf2, _ = doc_converter.prepare_printable_file(str(md))
                self.assertEqual(_StubGotenbergHandler.routes_seen[-1], "/forms/chromium/convert/markdown")
                self.assertIn(b'{{ toHTML "content.md" }}', _StubGotenbergHandler.bodies_seen[-1])
                self.assertIn(b'filename="content.md"', _StubGotenbergHandler.bodies_seen[-1])
                Path(pdf2).unlink(missing_ok=True)

                pdf3, _ = doc_converter.prepare_printable_file(str(html))
                self.assertEqual(_StubGotenbergHandler.routes_seen[-1], "/forms/chromium/convert/html")
                self.assertIn(b'filename="index.html"', _StubGotenbergHandler.bodies_seen[-1])
                Path(pdf3).unlink(missing_ok=True)
                Path(pdf1).unlink(missing_ok=True)

    def test_error_mapping_and_no_fallback_in_gotenberg_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / "note.txt"
            txt.write_text("hello", encoding="utf-8")

            _StubGotenbergHandler.mode = "bad_400"
            with self.assertRaises(gotenberg_client.GotenbergError) as ctx400:
                doc_converter.prepare_printable_file(str(txt))
            self.assertIn("不支持或文件已损坏", str(ctx400.exception))

            _StubGotenbergHandler.mode = "timeout_503"
            with self.assertRaises(gotenberg_client.GotenbergError) as ctx503:
                doc_converter.prepare_printable_file(str(txt))
            self.assertIn("超时", str(ctx503.exception))

            _StubGotenbergHandler.mode = "bad_pdf"
            with self.assertRaises(gotenberg_client.GotenbergError) as ctx_pdf:
                doc_converter.prepare_printable_file(str(txt))
            self.assertIn("有效的 PDF", str(ctx_pdf.exception))

            with mock.patch.dict(os.environ, {"PRINT_HUB_GOTENBERG_URL": "http://127.0.0.1:1"}):
                with self.assertRaises(gotenberg_client.GotenbergError) as ctx_down:
                    doc_converter.prepare_printable_file(str(txt))
                self.assertIn("暂不可用", str(ctx_down.exception))

    def test_unsupported_extension_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "archive.zip"
            bad.write_bytes(b"PK")
            with self.assertRaises(ValueError):
                doc_converter.prepare_printable_file(str(bad))

    def test_auto_mode_fallback_to_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            docx = Path(tmp) / "a.docx"
            docx.write_bytes(b"fake")
            fallback_pdf = Path(tmp) / "fallback.pdf"
            fallback_pdf.write_bytes(_build_minimal_pdf())

            _StubGotenbergHandler.mode = "bad_400"
            with mock.patch.dict(os.environ, {"PRINT_HUB_CONVERTER_MODE": "auto"}):
                with mock.patch.object(doc_converter, "_convert_locally", return_value=str(fallback_pdf)) as m_local:
                    with mock.patch.object(doc_converter, "validate_pdf_glyphs"):
                        out, pages = doc_converter.prepare_printable_file(str(docx))
                        self.assertEqual(out, str(fallback_pdf))
                        self.assertEqual(pages, 1)
                        m_local.assert_called_once()

    def test_config_auth_from_env(self):
        with mock.patch.dict(
            os.environ,
            {"PRINT_HUB_API_TOKEN": "sec_api", "PRINT_HUB_ADMIN_TOKEN": "sec_adm"},
        ):
            auth = cfg_mod.get_auth_config()
            self.assertEqual(auth["api_token"], "sec_api")
            self.assertEqual(auth["admin_token"], "sec_adm")

    def test_remote_cli_server_resolution_and_print_upload(self):
        with mock.patch.dict(os.environ, {"PRINT_HUB_SERVER": "http://10.0.0.5:18828/"}):
            self.assertEqual(remote_cli.resolve_server_url(), "http://10.0.0.5:18828")
            self.assertEqual(remote_cli.resolve_server_url("10.0.0.8:18828"), "http://10.0.0.8:18828")

    def test_web_server_converter_status_auth_and_upload_validation(self):
        hub_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), web_server.PrinterWebHandler)
        hub_thread = threading.Thread(target=hub_srv.serve_forever, daemon=True)
        hub_thread.start()
        base_url = f"http://127.0.0.1:{hub_srv.server_port}"
        try:
            # 1. /api/converter-status 在线检查
            res = remote_cli._request_json(base_url, "/api/converter-status")
            self.assertEqual(res["mode"], "gotenberg")
            self.assertTrue(res["up"])

            # 2. 模拟非本机客户端：服务端开启令牌，客户端未带令牌 -> 401；带上令牌 -> 200
            with mock.patch.object(
                web_server,
                "get_auth_config",
                return_value={"api_token": "api-secret", "admin_token": "admin-secret"},
            ), mock.patch.object(web_server.PrinterWebHandler, "_is_loopback_client", return_value=False):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("PRINT_HUB_API_TOKEN", None)
                    os.environ.pop("PRINT_HUB_ADMIN_TOKEN", None)
                    with self.assertRaises(RuntimeError) as ctx_unauth:
                        remote_cli._request_json(base_url, "/api/converter-status")
                    self.assertIn("401", str(ctx_unauth.exception))

                with mock.patch.dict(
                    os.environ,
                    {"PRINT_HUB_API_TOKEN": "api-secret", "PRINT_HUB_ADMIN_TOKEN": "admin-secret"},
                ):
                    ok_payload = remote_cli._request_json(base_url, "/api/converter-status")
                    self.assertTrue(ok_payload["up"])

                    # 远程设置默认设备：未携带 admin header 被拒绝 (403)，携带 admin=True 放行
                    with mock.patch.object(
                        web_server,
                        "set_default_printer",
                        return_value={"id": "p1", "name": "TestPrinter"},
                    ):
                        with self.assertRaises(RuntimeError) as ctx_403:
                            remote_cli._request_json(
                                base_url, "/api/default-printer?printer_id=p1", method="POST", admin=False
                            )
                        self.assertIn("403", str(ctx_403.exception))

                        adm_res = remote_cli._request_json(
                            base_url, "/api/default-printer?printer_id=p1", method="POST", admin=True
                        )
                        self.assertEqual(adm_res["default_printer_id"], "p1")

            # 3. 上传不支持的扩展名被拦截（不落盘、不入队）
            with tempfile.TemporaryDirectory() as tmp:
                bad_file = Path(tmp) / "evil.exe"
                bad_file.write_bytes(b"MZ")
                boundary = "testboundary123"
                body = remote_cli._encode_upload_multipart(
                    boundary,
                    {"user": "tester", "priority": "50", "duplex": "simplex", "copies": "1"},
                    bad_file.name,
                    bad_file.read_bytes(),
                )
                with self.assertRaises(RuntimeError) as ctx_bad_ext:
                    remote_cli._request_json(
                        base_url,
                        "/api/upload_print",
                        method="POST",
                        raw_body=body,
                        content_type=f"multipart/form-data; boundary={boundary}",
                    )
                self.assertIn("400", str(ctx_bad_ext.exception))

            # 4. 正常上传 .md 文件走通 remote_cli.cmd_print (mock 队列与状态查询，不碰真实打印机)
            saved_uploads = []

            def _fake_enqueue(**kwargs):
                saved_uploads.append(Path(kwargs["filepath"]))
                return {
                    "success": True,
                    "job": {
                        "id": 999,
                        "filepath": os.path.abspath(kwargs["filepath"]),
                        "filename": kwargs["filename"],
                        "user": kwargs["user"],
                        "printer_id": "p1",
                        "printer_name": "测试打印机",
                        "pages": 1,
                        "copies": kwargs["copies"],
                        "duplex": kwargs["duplex"],
                        "status": "completed",
                        "completion_verified": True,
                    },
                }

            fake_job = _fake_enqueue(
                filepath="notes.md", filename="notes.md", user="张三", copies=1, duplex="duplex_long"
            )["job"]
            saved_uploads.clear()
            with tempfile.TemporaryDirectory() as tmp, \
                 mock.patch.object(web_server.svc, "resolve_printer_by_keyword", return_value="p1"), \
                 mock.patch.object(web_server, "get_printer", return_value={"id": "p1", "duplex_supported": True}), \
                 mock.patch.object(web_server.svc.scheduler, "get_request", return_value=None), \
                 mock.patch.object(web_server.svc, "enqueue_upload", side_effect=_fake_enqueue) as m_enq, \
                 mock.patch.object(web_server.svc, "sync_hardware_jobs"), \
                 mock.patch.object(web_server.svc.scheduler, "get_job", return_value=fake_job):
                md_file = Path(tmp) / "notes.md"
                md_file.write_text("# 测试", encoding="utf-8")
                args = remote_cli.build_parser().parse_args(
                    ["--server", base_url, "print", "--file", str(md_file), "--user", "张三", "--wait", "2"]
                )
                with mock.patch("builtins.print") as m_print:
                    remote_cli.cmd_print(args, base_url)
                    printed = json.loads(m_print.call_args[0][0])
                    self.assertTrue(printed["success"])
                    self.assertEqual(printed["job"]["id"], 999)
                    m_enq.assert_called_once()
                    for p in saved_uploads:
                        p.unlink(missing_ok=True)
        finally:
            hub_srv.shutdown()
            hub_srv.server_close()


if __name__ == "__main__":
    unittest.main()
