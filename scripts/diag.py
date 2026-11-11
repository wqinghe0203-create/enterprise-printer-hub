# -*- coding: utf-8 -*-
"""
企业打印机网络连通性一键深度诊断模块 (Deep Diagnostics)
精准排查：
1. 本机环境（Python版本、工作目录、局域网 IP 与子网）
2. 操作系统代理检测（Clash/VPN/系统代理是否拦截了 10.* 局域网请求）
3. 物理层网络（ICMP Ping）
4. 传输层端口（IPP 631、Remote UI 8000/80、RAW 9100）
5. 协议层（纯 Direct Socket IPP Get-Printer-Attributes 探测）
6. 佳能打印机休眠唤醒测试与 IP 白名单排查
"""

import os
import sys
import socket
import struct
import subprocess
import time
import urllib.request
from typing import Dict, Any, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scripts.config import get_printers, get_printer, get_default_printer_id
from scripts.net_utils import get_host_lan_ip

try:
    import winreg
except ImportError:
    winreg = None


def check_system_proxy() -> Dict[str, Any]:
    """检测 Windows 系统代理与环境变量代理配置"""
    result = {
        "proxy_enabled": False,
        "proxy_server": "",
        "proxy_override": "",
        "env_proxies": {},
        "has_interception_risk": False
    }

    # 检查环境变量代理
    for env_key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        val = os.environ.get(env_key)
        if val:
            result["env_proxies"][env_key] = val
            result["has_interception_risk"] = True

    # 检查 Windows 注册表系统代理 (Internet Settings)
    if winreg:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                result["proxy_enabled"] = bool(enable)
                if result["proxy_enabled"]:
                    try:
                        server, _ = winreg.QueryValueEx(key, "ProxyServer")
                        result["proxy_server"] = str(server)
                    except FileNotFoundError:
                        pass
                    try:
                        override, _ = winreg.QueryValueEx(key, "ProxyOverride")
                        result["proxy_override"] = str(override)
                    except FileNotFoundError:
                        pass
                    result["has_interception_risk"] = True
        except Exception:
            pass

    return result


def ping_host(ip: str, timeout_ms: int = 1500) -> bool:
    """使用系统 ping 命令快速探测主机网络层是否可达"""
    if not ip:
        return False
    try:
        if sys.platform.startswith("win"):
            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False


def test_tcp_port(ip: str, port: int, timeout: float = 3.0) -> Dict[str, Any]:
    """测试单个 TCP 端口的握手连通性并测量握手耗时"""
    start_t = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        code = s.connect_ex((ip, port))
        latency = round((time.time() - start_t) * 1000, 1)
        s.close()
        return {
            "port": port,
            "open": (code == 0),
            "code": code,
            "latency_ms": latency if code == 0 else None,
            "error": None if code == 0 else f"ErrCode_{code}"
        }
    except Exception as e:
        return {
            "port": port,
            "open": False,
            "code": -1,
            "latency_ms": None,
            "error": str(e)
        }


def test_ipp_query(ip: str, port: int = 631, ipp_path: str = "/ipp/print", timeout: float = 5.0) -> Dict[str, Any]:
    """测试纯直连（绕过代理）IPP Get-Printer-Attributes 握手"""
    url = f"http://{ip}:{port}{ipp_path}"
    printer_uri = f"ipp://{ip}:{port}{ipp_path}"

    buf = bytearray()
    buf.extend(b"\x01\x01\x00\x0b\x00\x00\x00\x01")  # IPP 1.1, Get-Printer-Attributes, req_id 1
    buf.append(0x01)  # operation-attributes-tag

    def add_attr(tag, name, val):
        buf.append(tag)
        nb = name.encode("utf-8")
        buf.extend(struct.pack(">h", len(nb)))
        buf.extend(nb)
        vb = val.encode("utf-8")
        buf.extend(struct.pack(">h", len(vb)))
        buf.extend(vb)

    add_attr(0x47, "attributes-charset", "utf-8")
    add_attr(0x48, "attributes-natural-language", "zh-cn")
    add_attr(0x45, "printer-uri", printer_uri)
    buf.append(0x03)  # end-of-attributes-tag

    req = urllib.request.Request(
        url,
        data=bytes(buf),
        headers={
            "Content-Type": "application/ipp",
            "User-Agent": "Enterprise-Printer-Hub/Diag-2.0"
        }
    )

    # 严格绕过系统代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    start_t = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read()
            latency = round((time.time() - start_t) * 1000, 1)
            status_code = -1
            if len(data) >= 4:
                status_code = struct.unpack(">h", data[2:4])[0]
            return {
                "success": (status_code == 0),
                "http_status": resp.status,
                "ipp_status_code": status_code,
                "latency_ms": latency,
                "response_size": len(data),
                "error": None
            }
    except Exception as e:
        return {
            "success": False,
            "http_status": None,
            "ipp_status_code": None,
            "latency_ms": None,
            "response_size": 0,
            "error": str(e)
        }


def run_full_diagnosis(printer_id: Optional[str] = None) -> Dict[str, Any]:
    """执行完整的网络诊断，并输出诊断报告与可执行排查方案"""
    p_cfg = get_printer(printer_id)
    target_ip = p_cfg.get("ip", "10.1.16.228")
    ipp_port = p_cfg.get("ipp_port", 631)
    remote_ui_port = p_cfg.get("remote_ui_port", 8000)
    web_port = p_cfg.get("web_port", 80)
    raw_port = p_cfg.get("raw_port", 9100)

    host_lan_ip = get_host_lan_ip()
    proxy_info = check_system_proxy()

    # 1. 物理 Ping
    ping_ok = ping_host(target_ip)

    # 2. 核心端口测试
    port_results = {
        "ipp_631": test_tcp_port(target_ip, ipp_port),
        "remote_ui": test_tcp_port(target_ip, remote_ui_port),
        "web_80": test_tcp_port(target_ip, web_port),
        "raw_9100": test_tcp_port(target_ip, raw_port)
    }

    # 3. IPP 协议交互
    ipp_test = test_ipp_query(target_ip, port=ipp_port, ipp_path=p_cfg.get("ipp_path", "/ipp/print"))

    # 4. 分析诊断结论与修复建议
    conclusions: List[str] = []
    recommendations: List[str] = []
    overall_status = "UNKNOWN"

    if ipp_test["success"]:
        overall_status = "HEALTHY"
        conclusions.append(f"✅ 通信完全正常！已成功完成 IPP 协议握手（响应耗时 {ipp_test['latency_ms']} ms）。")
    elif port_results["ipp_631"]["open"]:
        overall_status = "PORT_OPEN_IPP_FAILED"
        err_detail = ipp_test.get("error") or f"状态码 {ipp_test.get('ipp_status_code')}"
        conclusions.append(f"⚠️ 端口已联通但 IPP 协议返回异常: {err_detail}。")
        recommendations.append("打印机目前可能处于深睡眠状态或作业处理繁忙，请尝试重新发送请求。")
    elif ping_ok:
        overall_status = "PING_OK_PORT_BLOCKED"
        conclusions.append(f"❌ 能 ping 通打印机 IP（{target_ip}），但 IPP 631 端口 TCP 握手被拒或超时！")
        recommendations.append("1. 【打印机 IP 白名单排查】：佳能 imageRUNNER 复合机很可能开启了「IPv4 地址筛选」，只允许了部分网段/IP，未将您的本机 IP 加入允许列表。")
        recommendations.append(f"2. 请用浏览器尝试访问打印机后台：http://{target_ip}:{remote_ui_port}/portal_top.html 或 http://{target_ip}/ 查看是否能打开。")
        recommendations.append("3. 检查公司办公网路由器或本机防火墙是否阻断了出站 631 端口。")
    else:
        overall_status = "NETWORK_UNREACHABLE"
        conclusions.append(f"❌ 物理网络不可达：本机（{host_lan_ip}）完全无法连通打印机（{target_ip}）！")
        recommendations.append("1. 【网络连接确认】：请确认当前电脑是否连接了公司办公局域网（有线内网或办公专用 Wi-Fi），请勿使用访客 Wi-Fi 或手机热点。")
        recommendations.append("2. 【网段跨路由检查】：您的电脑处于局域网网段，打印机处于 10.1.16.* 网段，可能存在跨网段路由器 ACL 访问控制。")
        recommendations.append("3. 【打印机电源状态】：请确认打印机主机电源已开启且网线插接正常。")

    if proxy_info["has_interception_risk"]:
        if proxy_info["proxy_enabled"]:
            conclusions.append(f"ℹ️ 检测到本机已开启系统代理（{proxy_info['proxy_server']}），本技能已自动强制绕过代理直连局域网。")
        if proxy_info["env_proxies"]:
            conclusions.append(f"ℹ️ 检测到存在代理环境变量（{', '.join(proxy_info['env_proxies'].keys())}）。")

    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": {
            "lan_ip": host_lan_ip,
            "os": sys.platform,
            "python_executable": sys.executable,
            "base_dir": BASE_DIR
        },
        "printer": {
            "id": p_cfg.get("id"),
            "name": p_cfg.get("name"),
            "location": p_cfg.get("location"),
            "ip": target_ip,
            "gateway": p_cfg.get("gateway")
        },
        "proxy": proxy_info,
        "ping_ok": ping_ok,
        "ports": port_results,
        "ipp_test": ipp_test,
        "overall_status": overall_status,
        "conclusions": conclusions,
        "recommendations": recommendations
    }


def format_diag_report(diag: Dict[str, Any]) -> str:
    """格式化为移动端友好、高清晰度的 Markdown 报告"""
    h = diag["host"]
    p = diag["printer"]
    pts = diag["ports"]
    ipp = diag["ipp_test"]

    def fmt_ms(ms):
        return f"{ms} ms" if ms is not None else "-"

    lines = [
        "# 🩺 企业文印网络深度体检诊断报告\n",
        f"**体检时间**：`{diag['timestamp']}`  ",
        f"**本机调度节点**：`{h['lan_ip']}` ｜ **目标打印机**：`{p['name']} ({p['ip']})`\n",
        "---",
        "\n### 📊 链路探测体检表\n",
        "| 检测项 | 目标端口/协议 | 检测结果 | 响应延迟 |",
        "| :--- | :---: | :---: | :---: |",
        f"| **物理网络 (ICMP)** | 节点连通 | {'🟢 正常可达' if diag['ping_ok'] else '🔴 不可达/超时'} | - |",
        f"| **IPP 打印服务** | TCP 631 | {'🟢 端口开放' if pts['ipp_631']['open'] else '🔴 端口关闭/受阻'} | {fmt_ms(pts['ipp_631']['latency_ms'])} |",
        f"| **佳能 Remote UI** | TCP {p.get('remote_ui_port', 8000)} | {'🟢 端口开放' if pts['remote_ui']['open'] else '🔴 端口关闭'} | {fmt_ms(pts['remote_ui']['latency_ms'])} |",
        f"| **RAW 打印通道** | TCP 9100 | {'🟢 端口开放' if pts['raw_9100']['open'] else '🔴 端口关闭'} | {fmt_ms(pts['raw_9100']['latency_ms'])} |",
        f"| **IPP 协议握手** | RFC 8011 | {'🟢 握手成功' if ipp['success'] else '🔴 握手失败'} | {fmt_ms(ipp['latency_ms'])} |\n",
        "### 🎯 诊断结论\n"
    ]

    for c in diag["conclusions"]:
        lines.append(f"> {c}")

    if diag["recommendations"]:
        lines.append("\n### 🛠️ 建议排查步骤\n")
        for idx, r in enumerate(diag["recommendations"], 1):
            lines.append(f"{idx}. {r}")

    return "\n".join(lines)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    diag_res = run_full_diagnosis()
    print(format_diag_report(diag_res))
