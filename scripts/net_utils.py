# -*- coding: utf-8 -*-
"""
网络工具模块 - 动态智能探测当前主机的真实局域网 IP
确保：无论哪个同事安装启动本 Skill，均自动获取该同事电脑的真实内网 IP，绝不写死。
"""

import socket
from typing import List


def get_host_lan_ip() -> str:
    """
    智能获取当前主机的局域网 IPv4 地址。
    自动过滤 127.0.0.1、APIPA (169.254.*)、代理虚拟网卡 (198.18.* / 198.19.*) 等，
    优先匹配企业内网网段 (10.*, 172.16-31.*, 192.168.*)。
    """
    candidates: List[str] = []

    # 1. 尝试通过 UDP socket 探测（连接公司内网网关或常用内网地址，不发包）
    for probe_target in ("10.1.16.228", "10.1.16.254", "192.168.1.1", "172.16.0.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect((probe_target, 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and _is_valid_private_ip(ip):
                return ip
        except Exception:
            pass

    # 2. 遍历本机全部网络适配器
    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for a in addrs:
            ip = a[4][0]
            if _is_valid_private_ip(ip):
                candidates.append(ip)
    except Exception:
        pass

    # 3. 按企业局域网优先级排序
    # 优先匹配 10.* 或 192.168.*
    for ip in candidates:
        if ip.startswith("10.") or ip.startswith("192.168."):
            return ip
    for ip in candidates:
        parts = ip.split(".")
        if len(parts) == 4 and parts[0] == "172" and 16 <= int(parts[1]) <= 31:
            return ip

    if candidates:
        return candidates[0]

    return "127.0.0.1"


def _is_valid_private_ip(ip: str) -> bool:
    """过滤无效/回环/虚拟假IP"""
    if not ip:
        return False
    if ip.startswith("127.") or ip.startswith("169.254."):
        return False
    # 过滤常见代理软件生成的 Fake-IP / TUN 网卡网段 (RFC 2544)
    if ip.startswith("198.18.") or ip.startswith("198.19."):
        return False
    return True
