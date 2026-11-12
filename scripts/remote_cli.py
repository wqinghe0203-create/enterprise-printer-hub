# -*- coding: utf-8 -*-
"""
企业智能文印中心 · 同事侧远程瘦客户端 (Remote CLI)
- 零第三方依赖（仅用 Python 标准库），在 WorkBuddy 沙箱中秒开执行
- 通过 HTTP 调用中心打印服务器，不再依赖本机 Office / LibreOffice / 本地队列
- 环境变量：
  PRINT_HUB_SERVER       中心服务器地址（如 http://10.1.16.100:18828）
  PRINT_HUB_API_TOKEN    可选访问令牌
  PRINT_HUB_ADMIN_TOKEN  可选管理员令牌（添加打印机、设为默认时使用）
"""

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parents[1]
REMOTE_CFG_PATH = BASE_DIR / "remote.json"
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_TERMINAL_STATES = {"completed", "failed", "canceled", "unknown"}


def _load_remote_file() -> Dict[str, Any]:
    if REMOTE_CFG_PATH.is_file():
        try:
            return json.loads(REMOTE_CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def resolve_server_url(explicit_server: Optional[str] = None) -> str:
    """解析中心打印服务器基础 URL（命令行 > 环境变量 > remote.json）。

    Args:
        explicit_server: 命令行传入的 --server 地址
    Returns:
        str: 去除末尾斜杠的服务端地址
    Example:
        >>> resolve_server_url("http://10.1.16.100:18828/")
        'http://10.1.16.100:18828'
    """
    raw = (
        explicit_server
        or os.environ.get("PRINT_HUB_SERVER", "")
        or str(_load_remote_file().get("server", ""))
    ).strip().rstrip("/")
    if not raw:
        raise ValueError(
            "未配置中心打印服务器地址；请设置环境变量 PRINT_HUB_SERVER，"
            "或在技能目录创建 remote.json，或传入 --server http://服务器IP:18828"
        )
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw


def _auth_headers(admin: bool = False) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    api_token = os.environ.get("PRINT_HUB_API_TOKEN", "").strip()
    admin_token = os.environ.get("PRINT_HUB_ADMIN_TOKEN", "").strip()
    if api_token:
        headers["X-Print-Hub-Token"] = api_token
    if admin and admin_token:
        headers["X-Print-Hub-Admin-Token"] = admin_token
    return headers


def _request_json(
    server: str,
    path: str,
    *,
    method: str = "GET",
    json_body: Optional[Dict[str, Any]] = None,
    raw_body: Optional[bytes] = None,
    content_type: Optional[str] = None,
    admin: bool = False,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    data = raw_body
    headers = _auth_headers(admin=admin)
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type

    req = urllib.request.Request(server + path, data=data, headers=headers, method=method)
    try:
        with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body)
            msg = err_json.get("error") or err_json.get("message") or body
        except Exception:
            msg = body or f"HTTP {exc.code}"
        raise RuntimeError(f"服务端返回错误 ({exc.code}): {msg}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"无法连接中心打印服务器 ({server}): {exc}") from exc


def _encode_upload_multipart(
    boundary: str, fields: Dict[str, str], filename: str, file_bytes: bytes
) -> bytes:
    chunks: List[bytes] = []
    for k, v in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
        chunks.append(f"{v}\r\n".encode("utf-8"))

    safe_name = os.path.basename(filename).replace('"', "_")
    ctype = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    chunks.append(f"--{boundary}\r\n".encode("ascii"))
    chunks.append(
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode("utf-8")
    )
    chunks.append(f"Content-Type: {ctype}\r\n\r\n".encode("ascii"))
    chunks.append(file_bytes)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks)


def _match_printer_id(cluster: List[Dict[str, Any]], keyword: Optional[str]) -> Optional[str]:
    if not keyword or not keyword.strip():
        return None
    kw = keyword.strip().casefold()
    for field in ("id", "name"):
        exact = [p["id"] for p in cluster if str(p.get(field, "")).casefold() == kw]
        if len(exact) == 1:
            return exact[0]
    fuzzy = [
        p for p in cluster
        if any(kw in str(p.get(f, "")).casefold() for f in ("name", "location", "model"))
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]["id"]
    if len(fuzzy) > 1:
        choices = "；".join(f"{p.get('name')}（{p.get('location')}，ID：{p.get('id')}）" for p in fuzzy)
        raise ValueError(f"匹配到多台打印机：{choices}；请提供完整名称或设备 ID")
    raise ValueError(f"未找到指定打印机：{keyword}；请选择已配置的设备")


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_printers(args: argparse.Namespace, server: str) -> None:
    cluster = _request_json(server, "/api/cluster")
    converter = _request_json(server, "/api/converter-status")
    _print_json({"server": server, "converter": converter, "printers": cluster})


def cmd_status(args: argparse.Namespace, server: str) -> None:
    cluster = _request_json(server, "/api/cluster")
    pid = _match_printer_id(cluster, args.printer)
    qs = f"?printer_id={urllib.parse.quote(pid)}" if pid else ""
    data = _request_json(server, "/api/dashboard" + qs)
    _print_json({"printer": data.get("printer"), "active_printing": data.get("active_printing")})


def cmd_queue(args: argparse.Namespace, server: str) -> None:
    cluster = _request_json(server, "/api/cluster")
    pid = _match_printer_id(cluster, args.printer)
    qs = f"?printer_id={urllib.parse.quote(pid)}" if pid else ""
    data = _request_json(server, "/api/dashboard" + qs)
    _print_json({
        "printer": data.get("printer"),
        "active_printing": data.get("active_printing"),
        "virtual_queue": data.get("virtual_queue", []),
        "hw_active_jobs": data.get("hw_active_jobs", []),
    })


def cmd_print(args: argparse.Namespace, server: str) -> None:
    file_path = Path(args.file)
    if not file_path.is_file():
        raise ValueError(f"找不到本地文件：{args.file}")

    request_id = args.request_id or uuid.uuid4().hex
    fields = {
        "user": args.user or str(_load_remote_file().get("default_user") or "普通用户"),
        "priority": str(args.priority),
        "duplex": args.duplex,
        "copies": str(args.copies),
        "request_id": request_id,
    }
    if args.printer:
        fields["printer"] = args.printer

    boundary = uuid.uuid4().hex
    body = _encode_upload_multipart(boundary, fields, file_path.name, file_path.read_bytes())
    res = _request_json(
        server,
        "/api/upload_print",
        method="POST",
        raw_body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
        timeout=60.0,
    )
    job = res.get("job") or {}
    job_id = job.get("id")

    # 短暂轮询等待 Gotenberg 转换与 IPP 投递结果，但绝不重复上传
    if job_id and args.wait > 0:
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline and job.get("status") not in _TERMINAL_STATES:
            time.sleep(0.8)
            task_res = _request_json(server, f"/api/task?job_id={job_id}&verify=1")
            if task_res.get("job"):
                job = task_res["job"]

    _print_json({"success": job.get("status") != "failed", "server": server, "job": job})
    if job.get("status") == "failed":
        sys.exit(1)


def cmd_task(args: argparse.Namespace, server: str) -> None:
    verify = "&verify=1" if args.verify else ""
    if args.request_id:
        path = f"/api/task?request_id={urllib.parse.quote(args.request_id)}{verify}"
    else:
        path = f"/api/task?job_id={args.job_id}{verify}"
    _print_json(_request_json(server, path))


def cmd_priority(args: argparse.Namespace, server: str) -> None:
    res = _request_json(server, f"/api/priority?job_id={args.job_id}&priority={args.priority}", method="POST")
    _print_json(res)


def cmd_cut_in(args: argparse.Namespace, server: str) -> None:
    res = _request_json(server, f"/api/cut_in?job_id={args.job_id}", method="POST")
    _print_json(res)


def cmd_cancel(args: argparse.Namespace, server: str) -> None:
    is_hw = "true" if args.hw else "false"
    qs = f"job_id={args.job_id}&is_hw={is_hw}"
    if args.printer:
        cluster = _request_json(server, "/api/cluster")
        pid = _match_printer_id(cluster, args.printer)
        if pid:
            qs += f"&printer_id={urllib.parse.quote(pid)}"
    res = _request_json(server, "/api/cancel?" + qs, method="POST")
    _print_json(res)


def cmd_diag(args: argparse.Namespace, server: str) -> None:
    cluster = _request_json(server, "/api/cluster")
    pid = _match_printer_id(cluster, args.printer)
    qs = f"?printer_id={urllib.parse.quote(pid)}" if pid else ""
    _print_json(_request_json(server, "/api/diag" + qs))


def cmd_set_default(args: argparse.Namespace, server: str) -> None:
    cluster = _request_json(server, "/api/cluster")
    pid = _match_printer_id(cluster, args.printer)
    if not pid:
        raise ValueError("请指定要设为默认的打印机")
    res = _request_json(
        server, f"/api/default-printer?printer_id={urllib.parse.quote(pid)}", method="POST", admin=True
    )
    _print_json(res)


def cmd_add_printer(args: argparse.Namespace, server: str) -> None:
    probe = _request_json(
        server,
        "/api/printer-probes",
        method="POST",
        json_body={"ip": args.ip, "ipp_port": args.ipp_port, "ipp_path": args.ipp_path},
        admin=True,
    )
    probe_id = probe["probe_id"]
    deadline = time.monotonic() + 15.0
    result = probe
    while time.monotonic() < deadline:
        result = _request_json(server, f"/api/printer-probes/{probe_id}", admin=True)
        if result.get("status") != "pending":
            break
        time.sleep(0.5)
    if args.save and result.get("can_save"):
        saved = _request_json(
            server,
            "/api/printers",
            method="POST",
            json_body={
                "probe_id": probe_id,
                "name": args.name or result.get("name") or "网络打印机",
                "location": args.location,
            },
            admin=True,
        )
        _print_json(saved)
    else:
        _print_json(result)
        if not result.get("can_save"):
            sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="企业智能文印中心 · 同事侧远程客户端 CLI")
    parser.add_argument("--server", "-S", default=None, help="中心服务器地址（覆盖 PRINT_HUB_SERVER）")
    sub = parser.add_subparsers(dest="subcommand")

    sub.add_parser("printers", help="查看公司所有打印机与转换服务状态").set_defaults(func=cmd_printers)

    p_st = sub.add_parser("status", help="查询打印机状态")
    p_st.add_argument("--printer", "-P", default=None)
    p_st.set_defaults(func=cmd_status)

    p_q = sub.add_parser("queue", help="查看打印队列与实时出纸")
    p_q.add_argument("--printer", "-P", default=None)
    p_q.set_defaults(func=cmd_queue)

    p_pr = sub.add_parser("print", help="上传本地文件到中心打印服务器")
    p_pr.add_argument("--file", "-f", required=True)
    p_pr.add_argument("--printer", "-P", default=None)
    p_pr.add_argument("--user", "-u", default=None)
    p_pr.add_argument("--priority", "-p", type=int, default=50)
    p_pr.add_argument("--duplex", "-d", default="duplex_long")
    p_pr.add_argument("--copies", "-c", type=int, default=1)
    p_pr.add_argument("--request-id", default=None)
    p_pr.add_argument("--wait", type=float, default=10.0, help="等待转换与投递结果秒数，0 表示立即返回")
    p_pr.set_defaults(func=cmd_print)

    p_tk = sub.add_parser("task", help="查询任务状态")
    p_tk.add_argument("--job-id", "-j", type=int, default=0)
    p_tk.add_argument("--request-id", default=None)
    p_tk.add_argument("--verify", action="store_true")
    p_tk.set_defaults(func=cmd_task)

    p_pri = sub.add_parser("priority", help="调整任务优先级")
    p_pri.add_argument("--job-id", "-j", type=int, required=True)
    p_pri.add_argument("--priority", "-p", type=int, required=True)
    p_pri.set_defaults(func=cmd_priority)

    p_cut = sub.add_parser("cut-in", help="紧急插队置顶")
    p_cut.add_argument("--job-id", "-j", type=int, required=True)
    p_cut.set_defaults(func=cmd_cut_in)

    p_can = sub.add_parser("cancel", help="取消打印任务")
    p_can.add_argument("--job-id", "-j", type=int, required=True)
    p_can.add_argument("--printer", "-P", default=None)
    p_can.add_argument("--hw", action="store_true")
    p_can.set_defaults(func=cmd_cancel)

    p_dg = sub.add_parser("diag", help="服务器侧网络与 Gotenberg 体检")
    p_dg.add_argument("--printer", "-P", default=None)
    p_dg.set_defaults(func=cmd_diag)

    p_def = sub.add_parser("set-default", help="设置默认打印机（需管理员令牌）")
    p_def.add_argument("--printer", "-P", required=True)
    p_def.set_defaults(func=cmd_set_default)

    p_add = sub.add_parser("add-printer", help="远程检测并添加打印机（需管理员令牌）")
    p_add.add_argument("--ip", required=True)
    p_add.add_argument("--ipp-port", type=int, default=631)
    p_add.add_argument("--ipp-path", default="/ipp/print")
    p_add.add_argument("--name", default=None)
    p_add.add_argument("--location", default="")
    p_add.add_argument("--save", action="store_true")
    p_add.set_defaults(func=cmd_add_printer)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        server = resolve_server_url(args.server)
        func = getattr(args, "func", cmd_printers)
        func(args, server)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
