# -*- coding: utf-8 -*-
"""
Gotenberg 文档转换客户端（零第三方依赖）
- 使用标准库 urllib.request 直接与 Gotenberg REST API 通信
- 强制绕过本机 HTTP 代理，避免局域网/本机 Docker 调用被系统代理劫持
- 统一采用临时文件写入 + 魔数校验 + 原子替换，确保落盘 PDF 完整有效
"""

import base64
import json
import logging
import mimetypes
import os
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import get_converter_config

logger = logging.getLogger(__name__)

# 绕过系统代理直连 Gotenberg，防止企业电脑全局代理拦截 127.0.0.1 或内网地址
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_MD_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
  body {
    font-family: "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", "WenQuanYi Micro Hei", sans-serif;
    font-size: 13.5px;
    line-height: 1.68;
    color: #1a202c;
    max-width: 100%;
    margin: 0;
  }
  h1, h2, h3, h4 { color: #111827; margin-top: 1.2em; margin-bottom: 0.5em; }
  pre, code {
    font-family: "JetBrains Mono", Consolas, monospace;
    background: #f3f4f6;
    border-radius: 4px;
  }
  pre { padding: 10px 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }
  code { padding: 1px 4px; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; }
  th, td { border: 1px solid #d1d5db; padding: 6px 10px; text-align: left; }
  th { background: #f9fafb; }
  blockquote { border-left: 3px solid #9ca3af; margin: 10px 0; padding: 4px 12px; color: #4b5563; }
  img { max-width: 100%; height: auto; }
</style>
</head>
<body>
{{ toHTML "content.md" }}
</body>
</html>
"""

_A4_FIELDS = {
    "paperWidth": "8.27",
    "paperHeight": "11.69",
    "marginTop": "0.55",
    "marginBottom": "0.55",
    "marginLeft": "0.55",
    "marginRight": "0.55",
    "printBackground": "true",
}


class GotenbergError(RuntimeError):
    """Gotenberg 文档转换失败；异常消息面向终端用户，可直接展示。"""


def _build_auth_header(basic_auth: Optional[Tuple[str, str]]) -> Optional[str]:
    if not basic_auth:
        return None
    token = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _encode_multipart(
    boundary: str,
    files: List[Tuple[str, str, bytes]],
    fields: Optional[Dict[str, str]] = None,
) -> bytes:
    """组装 multipart/form-data 请求体。"""
    chunks: List[bytes] = []
    for key, value in (fields or {}).items():
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        chunks.append(f"{value}\r\n".encode("utf-8"))

    for field_name, filename, content in files:
        safe_fn = filename.replace('"', "_")
        ctype = mimetypes.guess_type(safe_fn)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_fn}"\r\n'.encode("utf-8")
        )
        chunks.append(f"Content-Type: {ctype}\r\n\r\n".encode("ascii"))
        chunks.append(content)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks)


def _friendly_http_error(status_code: int) -> str:
    if status_code in (400, 415):
        return "文档格式不支持或文件已损坏，未发送到打印机"
    if status_code in (401, 403):
        return "文档转换服务鉴权失败，请检查 Gotenberg 账号密码配置"
    if status_code in (503, 504):
        return "文档转换超时，文件未发送到打印机；请稍后重试或拆分文档"
    return f"文档转换服务返回异常状态（HTTP {status_code}），未发送到打印机"


def _write_pdf_atomically(resp: Any, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(dst.parent), prefix=".gotenberg-", suffix=".pdf.tmp", delete=False) as f:
            tmp_path = Path(f.name)
            while True:
                buf = resp.read(65536)
                if not buf:
                    break
                f.write(buf)
            f.flush()
            os.fsync(f.fileno())
        with tmp_path.open("rb") as verify:
            if verify.read(5) != b"%PDF-":
                raise GotenbergError("转换结果不是有效的 PDF，未发送到打印机")
        os.replace(tmp_path, dst)
        tmp_path = None
        return dst
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _post_files(
    route: str,
    files: List[Tuple[str, str, bytes]],
    dst: Path,
    fields: Optional[Dict[str, str]] = None,
) -> Path:
    cfg = get_converter_config()
    url = cfg["gotenberg_url"] + route
    boundary = uuid.uuid4().hex
    body = _encode_multipart(boundary, files, fields)

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    auth_header = _build_auth_header(cfg.get("basic_auth"))
    if auth_header:
        req.add_header("Authorization", auth_header)

    try:
        with _DIRECT_OPENER.open(req, timeout=cfg["timeout_sec"]) as resp:
            return _write_pdf_atomically(resp, dst)
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        logger.warning("Gotenberg %s 返回 HTTP %s: %s", route, exc.code, detail)
        raise GotenbergError(_friendly_http_error(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.error("无法连接 Gotenberg (%s): %s", cfg["gotenberg_url"], exc)
        raise GotenbergError("文档转换服务暂不可用，文件未发送到打印机；请确认 Gotenberg 容器已启动") from exc


def health(timeout: float = 3.0) -> Dict[str, Any]:
    """探测 Gotenberg 服务健康状态。

    Args:
        timeout: 单次健康检查超时秒数
    Returns:
        Dict[str, Any]: 包含 up(bool)、url(str)、status(str)、detail(dict/str)
    Example:
        >>> sorted(health(timeout=0.1).keys())  # doctest: +SKIP
        ['detail', 'status', 'up', 'url']
    """
    cfg = get_converter_config()
    url = cfg["gotenberg_url"]
    req = urllib.request.Request(url + "/health", method="GET")
    auth_header = _build_auth_header(cfg.get("basic_auth"))
    if auth_header:
        req.add_header("Authorization", auth_header)

    try:
        with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
            status_val = str(payload.get("status", "unknown")).lower()
            return {"up": status_val == "up", "url": url, "status": status_val, "detail": payload}
    except Exception as exc:
        return {"up": False, "url": url, "status": "down", "detail": str(exc)}


def convert_office(src_path: Path, dst_path: Path) -> Path:
    """调用 Gotenberg LibreOffice 引擎转换 Office/文本类文件为 PDF。

    Args:
        src_path: 源文件路径（doc/docx/xls/xlsx/ppt/pptx/txt/csv/rtf/odt 等）
        dst_path: 目标 PDF 输出路径
    Returns:
        Path: 生成的 PDF 文件路径
    Example:
        >>> convert_office(Path("a.docx"), Path("a.pdf"))  # doctest: +SKIP
    """
    # 避免中文/特殊字符在部分容器中引起 LibreOffice 路径异常，统一保留扩展名重命名上传
    safe_upload_name = "document" + src_path.suffix.lower()
    content = src_path.read_bytes()
    return _post_files("/forms/libreoffice/convert", [("files", safe_upload_name, content)], dst_path)


def convert_markdown(src_path: Path, dst_path: Path) -> Path:
    """调用 Gotenberg Chromium 引擎将 Markdown 文件渲染为带样式的 A4 PDF。

    Args:
        src_path: 源 .md / .markdown 文件路径
        dst_path: 目标 PDF 输出路径
    Returns:
        Path: 生成的 PDF 文件路径
    Example:
        >>> convert_markdown(Path("readme.md"), Path("readme.pdf"))  # doctest: +SKIP
    """
    files = [
        ("files", "index.html", _MD_HTML_TEMPLATE.encode("utf-8")),
        ("files", "content.md", src_path.read_bytes()),
    ]
    return _post_files("/forms/chromium/convert/markdown", files, dst_path, fields=_A4_FIELDS)


def convert_html(src_path: Path, dst_path: Path) -> Path:
    """调用 Gotenberg Chromium 引擎将 HTML 文件渲染为 A4 PDF。

    Args:
        src_path: 源 .html / .htm 文件路径
        dst_path: 目标 PDF 输出路径
    Returns:
        Path: 生成的 PDF 文件路径
    Example:
        >>> convert_html(Path("page.html"), Path("page.pdf"))  # doctest: +SKIP
    """
    # Gotenberg Chromium 路由要求主入口文件必须命名为 index.html
    files = [("files", "index.html", src_path.read_bytes())]
    return _post_files("/forms/chromium/convert/html", files, dst_path, fields=_A4_FIELDS)
