# -*- coding: utf-8 -*-
"""
环境自检与初始化安装脚本 (Setup)
检测：
1. 记录当前运行的 Python 路径到 _local.json
2. 遍历全公司已注册打印机并测试连通性
"""

import sys
import os
import json
import socket

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scripts.config import get_printers, get_printer

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("══════════════════════════════════════════════════════════════════════════════")
    print("🛠️ 开始初始化 企业智能打印调度中心 (Enterprise Print Hub) 环境...")
    print("══════════════════════════════════════════════════════════════════════════════")

    # 1. 记录 Python 解释器绝对路径到 _local.json (供 WorkBuddy 持久信任)
    local_meta = {
        "python": sys.executable,
        "base_dir": BASE_DIR
    }
    local_json_path = os.path.join(BASE_DIR, "_local.json")
    with open(local_json_path, "w", encoding="utf-8") as f:
        json.dump(local_meta, f, ensure_ascii=False, indent=2)
    print(f"✅ 已绑定当前 Python 运行时：{sys.executable}")

    # 2. 遍历测试已配置的打印机连通性
    printers = get_printers()
    print(f"📡 正在自检已接入的 {len(printers)} 台企业打印机连通性...")
    for p in printers:
        ip = p.get("ip")
        port = p.get("ipp_port", 631)
        name = p.get("name")
        loc = p.get("location")

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        res = s.connect_ex((ip, port))
        s.close()
        if res == 0:
            print(f"  🟢 [{name}] ({loc} - {ip}:{port}) 通信正常！")
        else:
            print(f"  🔴 [{name}] ({loc} - {ip}:{port}) 连通失败，请确认设备已开机或网络可达。")

    print("\n🎉 企业级技能初始化完成！WorkBuddy 已可无缝调用。")


if __name__ == "__main__":
    main()
