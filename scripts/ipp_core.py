# -*- coding: utf-8 -*-
"""
纯 Python RFC 8011 IPP (Internet Printing Protocol) 核心协议客户端
通用企业级网络打印机驱动引擎：
- 适配各类网络复合机/打印机（佳能 Canon、惠普 HP、理光 Ricoh、联想 Lenovo、施乐 Xerox、奔图 Pantum 等）
- 查询物理机硬件状态 (Get-Printer-Attributes)
- 实时获取硬件打印队列与作业详情 (Get-Jobs: not-completed / completed)
- 提取任务提交人 (job-originating-user-name)、文件名、页数、状态
- 取消硬件上的打印作业 (Cancel-Job)
- 直接投递打印任务 (Print-Job)
零第三方 C 库依赖，跨平台纯标准库构建。
"""

import socket
import struct
import urllib.request
import urllib.error
from typing import Dict, List, Any, Optional

from .config import get_printer

# IPP 状态码
IPP_STATUS_OK = 0x0000
IPP_STATUS_CLIENT_ERROR_NOT_FOUND = 0x0406

# IPP 标签常量
TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04
TAG_UNSUPPORTED = 0x05
TAG_TEXTLANG = 0x35
TAG_NAMELANG = 0x36

TAG_INT = 0x21
TAG_BOOL = 0x22
TAG_ENUM = 0x23
TAG_OCTET_STR = 0x30
TAG_DATETIME = 0x31
TAG_RESOLUTION = 0x32
TAG_RANGE = 0x33
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_CHARSET = 0x47
TAG_LANGUAGE = 0x48
TAG_MIME = 0x49

# 作业状态映射
JOB_STATE_MAP = {
    3: "排队中 (Pending)",
    4: "已挂起 (Pending-Held)",
    5: "正在打印 (Processing)",
    6: "打印暂停 (Processing-Stopped)",
    7: "已取消 (Canceled)",
    8: "异常终止 (Aborted)",
    9: "已完成 (Completed)"
}

# 打印机状态映射
PRINTER_STATE_MAP = {
    3: "空闲就绪 (Idle)",
    4: "正在打印 (Processing)",
    5: "暂停/故障 (Stopped)"
}


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class IPPClient:
    def __init__(self, printer_cfg: Optional[Dict[str, Any]] = None,
                 ip: Optional[str] = None, port: Optional[int] = None, path: Optional[str] = None, follow_redirects: bool = True):
        cfg = printer_cfg or get_printer()
        self.printer_id = cfg.get("id", "default")
        self.model_name = cfg.get("model", "Network Printer")
        self.display_name = cfg.get("name", "网络打印机")
        self.ip = ip or cfg.get("ip", "10.1.16.228")
        self.port = port or cfg.get("ipp_port", 631)
        self.path = path or cfg.get("ipp_path", "/ipp/print")
        self.printer_uri = f"ipp://{self.ip}:{self.port}{self.path}"
        self.http_url = f"http://{self.ip}:{self.port}{self.path}"
        self.req_id = 1
        self.follow_redirects = follow_redirects

    def _next_req_id(self) -> int:
        self.req_id += 1
        return self.req_id

    def _safe_decode(self, raw_bytes: bytes) -> str:
        """多重编码容错解码（UTF-8 -> GB18030/GBK -> 降级忽略）"""
        if not raw_bytes:
            return ""
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return raw_bytes.decode("gb18030")
            except UnicodeDecodeError:
                try:
                    return raw_bytes.decode("gbk", errors="replace")
                except Exception:
                    return repr(raw_bytes)

    def _build_request(self, op_id: int, attrs: List[tuple], job_attrs: Optional[List[tuple]] = None) -> bytes:
        """构建标准 IPP 1.1 请求二进制包"""
        req_id = self._next_req_id()
        # Header: Version 1.1 (0x01, 0x01) + OpCode (2 bytes) + RequestID (4 bytes)
        buf = bytearray([0x01, 0x01])
        buf.extend(struct.pack(">h", op_id))
        buf.extend(struct.pack(">i", req_id))

        # Operation Attributes Tag
        buf.append(TAG_OPERATION)

        previous_name = None
        def append_attr(tag: int, name: str, val: Any):
            nonlocal previous_name
            buf.append(tag)
            name_bytes = b'' if name == previous_name else name.encode("utf-8")
            previous_name = name
            buf.extend(struct.pack(">h", len(name_bytes)))
            buf.extend(name_bytes)
            if isinstance(val, str):
                val_bytes = val.encode("utf-8")
            elif isinstance(val, bytes):
                val_bytes = val
            elif isinstance(val, bool):
                val_bytes = b"\x01" if val else b"\x00"
            elif isinstance(val, int):
                val_bytes = struct.pack(">i", val)
            else:
                val_bytes = str(val).encode("utf-8")
            buf.extend(struct.pack(">h", len(val_bytes)))
            buf.extend(val_bytes)

        append_attr(TAG_CHARSET, "attributes-charset", "utf-8")
        append_attr(TAG_LANGUAGE, "attributes-natural-language", "zh-cn")
        append_attr(TAG_URI, "printer-uri", self.printer_uri)

        for tag, name, val in attrs:
            append_attr(tag, name, val)

        if job_attrs:
            buf.append(TAG_JOB)
            previous_name = None
            for tag, name, val in job_attrs:
                append_attr(tag, name, val)
        buf.append(TAG_END)
        return bytes(buf)

    def _send_ipp(self, payload: bytes, timeout: float = 6.0) -> bytes:
        """发送 IPP POST 请求并获取响应（强制绕过系统代理，直接连接局域网）"""
        req = urllib.request.Request(
            self.http_url,
            data=payload,
            headers={
                "Content-Type": "application/ipp",
                "User-Agent": f"Enterprise-Printer-Hub/1.0 ({self.model_name})"
            }
        )
        # 核心：必须绕过系统代理 (ProxyHandler({}))，否则如果同事电脑开启了系统代理/VPN，10.* 内网请求会被代理拦截导致超时或离线！
        handlers = [urllib.request.ProxyHandler({})]
        if not self.follow_redirects:
            handlers.append(NoRedirectHandler())
        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()

    def parse_attributes(self, data: bytes) -> Dict[str, Any]:
        """解析 IPP 响应数据"""
        if len(data) < 8:
            return {}

        status_code = struct.unpack(">h", data[2:4])[0]
        idx = 8
        groups = []
        curr_group = {}
        unsupported = {}
        curr_tag = None
        curr_name = ""

        while idx < len(data):
            tag = data[idx]
            idx += 1
            if tag in (TAG_OPERATION, TAG_JOB, TAG_PRINTER, TAG_UNSUPPORTED, TAG_END):
                if curr_group:
                    groups.append(curr_group)
                    if curr_tag == TAG_UNSUPPORTED:
                        unsupported.update(curr_group)
                    curr_group = {}
                curr_name = ""
                curr_tag = tag
                if tag == TAG_END:
                    break
                continue

            if idx + 2 > len(data):
                break
            name_len = struct.unpack(">h", data[idx:idx+2])[0]
            idx += 2
            name_raw = data[idx:idx+name_len]
            idx += name_len
            name = self._safe_decode(name_raw)

            if idx + 2 > len(data):
                break
            val_len = struct.unpack(">h", data[idx:idx+2])[0]
            idx += 2
            val_raw = data[idx:idx+val_len]
            idx += val_len

            if name:
                curr_name = name

            val = val_raw
            if tag in (TAG_INT, TAG_ENUM):
                if len(val_raw) == 4:
                    val = struct.unpack(">i", val_raw)[0]
            elif tag == TAG_RESOLUTION and len(val_raw) == 9:
                x, y, units = struct.unpack('>IIB', val_raw)
                val = {'x': x, 'y': y, 'units': units}
            elif tag == TAG_RANGE and len(val_raw) == 8:
                val = list(struct.unpack('>II', val_raw))
            elif tag in (TAG_TEXTLANG, TAG_NAMELANG):
                # RFC 8010: language length, language, text length, text.
                val = ''
                if len(val_raw) >= 4:
                    language_len = struct.unpack('>H', val_raw[:2])[0]
                    offset = 2 + language_len
                    if offset + 2 <= len(val_raw):
                        text_len = struct.unpack('>H', val_raw[offset:offset+2])[0]
                        if offset + 2 + text_len <= len(val_raw):
                            val = self._safe_decode(val_raw[offset+2:offset+2+text_len])
            elif tag == TAG_BOOL:
                val = (val_raw == b"\x01")
            else:
                val = self._safe_decode(val_raw)

            if curr_name in curr_group:
                if not isinstance(curr_group[curr_name], list):
                    curr_group[curr_name] = [curr_group[curr_name]]
                curr_group[curr_name].append(val)
            else:
                curr_group[curr_name] = val

        if curr_group:
            groups.append(curr_group)
            if curr_tag == TAG_UNSUPPORTED:
                unsupported.update(curr_group)

        return {
            "status_code": status_code,
            "success": (0 <= status_code <= 0x00ff),
            "unsupported_attributes": unsupported,
            "groups": groups
        }

    def get_printer_status(self) -> Dict[str, Any]:
        """查询打印机硬件状态、型号与就绪情况"""
        attrs = [
            (TAG_KEYWORD, "requested-attributes", "printer-name"),
            (TAG_KEYWORD, "requested-attributes", "printer-make-and-model"),
            (TAG_KEYWORD, "requested-attributes", "printer-state"),
            (TAG_KEYWORD, "requested-attributes", "printer-state-reasons"),
            (TAG_KEYWORD, "requested-attributes", "queued-job-count"),
            (TAG_KEYWORD, "requested-attributes", "document-format-supported"),
            (TAG_KEYWORD, "requested-attributes", "pwg-raster-document-resolution-supported"),
            (TAG_KEYWORD, "requested-attributes", "pwg-raster-document-type-supported"),
            (TAG_KEYWORD, "requested-attributes", "pwg-raster-document-sheet-back"),
            (TAG_KEYWORD, "requested-attributes", "sides-supported"),
            (TAG_KEYWORD, "requested-attributes", "sides-default"),
            (TAG_KEYWORD, "requested-attributes", "copies-supported"),
        ]
        try:
            req_bytes = self._build_request(0x000B, attrs)
            resp_bytes = self._send_ipp(req_bytes)
            res = self.parse_attributes(resp_bytes)
            if not res.get("success"):
                raise RuntimeError("IPP 状态查询失败: " + hex(res.get("status_code", -1)))
            groups = res.get("groups", [])
            info = {}
            for g in groups:
                info.update(g)

            state_code = info.get("printer-state", 3)
            if isinstance(state_code, list):
                state_code = state_code[0] if state_code else 3

            return {
                "online": True,
                "printer_id": self.printer_id,
                "display_name": self.display_name,
                "model": info.get("printer-make-and-model", self.model_name),
                "name": info.get("printer-name", self.display_name),
                "state_code": state_code,
                "state_desc": PRINTER_STATE_MAP.get(state_code, f"状态码: {state_code}"),
                "reasons": info.get("printer-state-reasons", "none"),
                "queued_jobs": info.get("queued-job-count", 0),
                "ip": self.ip,
                "port": self.port,
                "capabilities": info
            }
        except Exception as e:
            err_msg = str(e)
            clean_desc = "离线或网络不可达"
            err_lower = err_msg.lower()
            if "timed out" in err_lower or "10060" in err_lower:
                clean_desc = "连接超时 (打印机休眠或跨网段路由不通)"
            elif "refused" in err_lower or "10061" in err_lower:
                clean_desc = "连接被拒 (631端口未开放或IP白名单过滤)"
            elif "proxy" in err_lower:
                clean_desc = "代理冲突 (系统代理拦截了局域网请求)"
            elif "host is down" in err_lower or "no route" in err_lower:
                clean_desc = "无法路由到主机 (请检查Wi-Fi/网线是否连接)"

            return {
                "online": False,
                "printer_id": self.printer_id,
                "display_name": self.display_name,
                "model": self.model_name,
                "error": err_msg,
                "ip": self.ip,
                "port": self.port,
                "state_desc": clean_desc
            }

    def get_jobs(self, which_jobs: str = "completed") -> List[Dict[str, Any]]:
        """获取打印机硬件队列中的作业"""
        attrs = [
            (TAG_KEYWORD, "which-jobs", which_jobs),
            (TAG_KEYWORD, "requested-attributes", "all")
        ]
        try:
            req_bytes = self._build_request(0x000A, attrs)
            resp_bytes = self._send_ipp(req_bytes)
            parsed = self.parse_attributes(resp_bytes)
            jobs = []
            for g in parsed.get("groups", []):
                if "job-id" in g:
                    state_id = g.get("job-state", 0)
                    jobs.append({
                        "job_id": g.get("job-id"),
                        "job_uri": g.get("job-uri"),
                        "job_name": g.get("job-name", "未命名作业"),
                        "user": g.get("job-originating-user-name", "未知用户"),
                        "state_code": state_id,
                        "state_desc": JOB_STATE_MAP.get(state_id, f"状态{state_id}"),
                        "state_reasons": g.get("job-state-reasons", []),
                        "state_message": g.get("job-state-message", ""),
                        "sheets_completed": g.get("job-media-sheets-completed"),
                        "k_octets": g.get("job-k-octets-processed", 0),
                        "printer_id": self.printer_id,
                        "printer_name": self.display_name,
                        "source": "hardware"
                    })
            return jobs
        except Exception:
            return []

    def cancel_job(self, job_id: int) -> Dict[str, Any]:
        """取消硬件上的打印作业 (Cancel-Job)"""
        attrs = [
            (TAG_INT, "job-id", job_id)
        ]
        try:
            req_bytes = self._build_request(0x0008, attrs)
            resp_bytes = self._send_ipp(req_bytes)
            parsed = self.parse_attributes(resp_bytes)
            return {
                "success": parsed.get("success", False),
                "status_code": hex(parsed.get("status_code", -1)),
                "error": None if parsed.get("success") else
                         f'打印机未接受取消请求（IPP {hex(parsed.get("status_code", -1))}）',
                "job_id": job_id,
                "printer_id": self.printer_id
            }
        except Exception as e:
            return {"success": False, "error": str(e), "job_id": job_id, "printer_id": self.printer_id}

    def get_job(self, job_id: int) -> Dict[str, Any]:
        """Read a particular hardware job; absence never means completed."""
        try:
            response = self._send_ipp(self._build_request(0x0009, [(TAG_INT, 'job-id', job_id), (TAG_KEYWORD, 'requested-attributes', 'all')]))
            parsed = self.parse_attributes(response)
            info = {}
            for group in parsed.get('groups', []):
                info.update(group)
            return {'success': parsed.get('success', False), 'status_code': parsed.get('status_code'), **info}
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

    def validate_job(self, doc_format: str, sides='one-sided', copies=1) -> Dict[str, Any]:
        attrs = [(TAG_MIME, 'document-format', doc_format)]
        job_attrs = [(TAG_KEYWORD, 'sides', sides), (TAG_INT, 'copies', copies)]
        return self.parse_attributes(self._send_ipp(self._build_request(0x0004, attrs, job_attrs)))

    def print_raw_payload(self, payload: bytes, doc_format: str = 'application/octet-stream',
                          job_name: str = 'Doc', user: str = 'User', sides='one-sided', copies=1) -> Dict[str, Any]:
        attrs = [(TAG_NAME, 'job-name', job_name), (TAG_NAME, 'requesting-user-name', user), (TAG_MIME, 'document-format', doc_format)]
        job_attrs = [(TAG_KEYWORD, 'sides', sides), (TAG_INT, 'copies', copies)]
        try:
            response = self._send_ipp(self._build_request(0x0002, attrs, job_attrs) + payload, timeout=60.0)
            parsed = self.parse_attributes(response)
            info = {}
            for group in parsed.get('groups', []):
                info.update(group)
            if 'status_code' not in parsed or (parsed.get('success') and not info.get('job-id')):
                return {'success': False, 'uncertain': True, 'status_code': parsed.get('status_code'),
                        'error': '打印响应不完整，请核对设备记录，勿自动重投'}
            return {
                'success': parsed.get('success', False), 'job_id': info.get('job-id'), 'job_uri': info.get('job-uri'),
                'status_code': hex(parsed.get('status_code', -1)), 'printer_id': self.printer_id,
                'error': info.get('status-message', '打印机拒绝接收作业'),
            }
        except urllib.error.HTTPError as exc:
            return {'success': False, 'uncertain': exc.code >= 500, 'status_code': f'HTTP {exc.code}',
                    'error': f'打印服务 HTTP {exc.code}，请检查设备日志', 'printer_id': self.printer_id}
        except Exception as exc:
            # The printer may already have received the request. Never resend automatically.
            return {'success': False, 'uncertain': True, 'error': '发送结果未确认，请核对硬件队列后再决定是否重投: ' + str(exc), 'printer_id': self.printer_id}
