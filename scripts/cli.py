# -*- coding: utf-8 -*-
"""
企业级智能打印机集群管理 CLI
支持全公司打印机大盘监控、谁在打印、智能优先级调度及跨设备免驱路由
优化特性：移动端自适应排版 (Mobile-First Card Stream)，彻底消除手机横向滚动截断
"""

import sys
import os
import argparse
import json

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scripts.printer_service import PrinterService
from scripts.config import get_printers, get_default_printer_id, set_default_printer
from scripts.net_utils import get_host_lan_ip


def cmd_printers(args):
    """查看全公司所有打印机状态与负荷概览 (移动优先·卡片流排版)"""
    svc = PrinterService()
    cluster = svc.get_cluster_overview()

    total_devices = len(cluster)
    online_devices = sum(1 for p in cluster if p["online"])
    total_queued = sum(p["queued_count"] for p in cluster)

    # 统计谁正在打印
    active_printers = []
    for p in cluster:
        if p["online"]:
            data = svc.get_full_dashboard_data(p["id"])
            if data.get("active_printing"):
                active_printers.append((p, data["active_printing"]))

    printing_count = len(active_printers)

    print("# 🏢 企业智能文印调度驾驶舱 · 集群大盘\n")
    # 手机端友好：3列紧凑核心指标卡，绝不横向溢出
    print("| 🟢 在线设备 | ⚡ 正在出纸 | ⏳ 排队待办 |")
    print("| :---: | :---: | :---: |")
    print(f"| **{online_devices} / {total_devices} 台** | **{printing_count} 人** | **{total_queued} 个任务** |\n")

    print("### 🖨️ 设备拓扑与运行状态\n")
    # 彻底弃用超宽8列表格，改用“移动端自适应纵向卡片流”
    for p in cluster:
        def_tag = " `🌟主力`" if p["is_default"] else ""
        status_tag = "`🟢 正常就绪`" if p["online"] else "`🔴 离线报警`"
        queue_text = "📭 空闲 (0 排队)" if p["queued_count"] == 0 else f"⏳ 忙碌 ({p['queued_count']} 任务排队中)"

        print(f"> #### {p['name']}{def_tag} · {status_tag}")
        print(f"> - 📍 **物理位置**：{p['location']}")
        print(f"> - 🌐 **节点 IP**：`{p['ip']}` ｜ **网关**：`{p.get('gateway', '-')}`")
        print(f"> - ⚙️ **协议通道**：IPP 631 / RAW 9100")
        print(f"> - 📊 **实时负荷**：{queue_text}")
        print(">")
    print()

    # 谁在打印作业透视
    print("### 🎯 实时出纸透视")
    if active_printers:
        for p, act in active_printers:
            print(f"> 📄 **文档名称**：`{act['job_name']}`  ")
            print(f"> 👤 **当前打印人**：`{act['user']}` ｜ **设备**：{p['name']} ｜ **状态**：`🟢 出纸中` ｜ **进度**：已出纸 {act.get('sheets', 0)} 张")
    else:
        print("> 🟢 **全司设备空闲待命中**：当前全公司无占用作业，任何新文档可提交，实际出纸以设备状态为准，无需排队。\n")

    print("---\n")
    print("### ⚡ 快捷交互指令")
    print("- 📄 **免驱直打**：将任意 `PDF / Word / Excel / 图片` 发到对话框，附带 `「帮我双面打印」`")
    print("- 🏢 **跨楼调度**：发送文件并指定位置，如 `「打到3楼」` 或 `「打到财务室」`")
    print("- 🚀 **紧急插队**：回复 `「将任务 #ID 设为紧急插队」` 即可置顶至首位出纸")
    host_ip = get_host_lan_ip()
    print(f"- 📱 **手机控制台**：局域网访问 `http://{host_ip}:18828` 或本机 `http://localhost:18828`")


def cmd_status(args):
    svc = PrinterService()
    target_pid = svc.resolve_printer_by_keyword(args.printer)
    data = svc.get_full_dashboard_data(target_pid)
    printer = data["printer"]

    if not printer["online"]:
        status_tag = "`🔴 离线报警`"
    elif printer.get("state_code") == 5 or any(k in str(printer.get("state_desc", "")) for k in ("暂停", "故障", "Stopped")):
        status_tag = f"`🔴 {printer.get('state_desc', '暂停/故障')}`"
    elif printer.get("state_code") == 4 or "Processing" in str(printer.get("state_desc", "")):
        status_tag = "`🔵 正在出纸`"
    else:
        status_tag = "`🟢 在线就绪`"
    print(f"# 🖨️ 设备状态卡片 · {printer['name']}\n")
    print(f"> #### {printer['name']} · {status_tag}")
    print(f"> - 📍 **物理位置**：{printer['location']}")
    print(f"> - 🏭 **设备型号**：`{printer['model']}`")
    print(f"> - 🌐 **网络参数**：IP `{printer['ip']}` ｜ 网关 `{printer['gateway']}`")
    print(f"> - 📋 **待办作业**：**{printer['queued_count']}** 个任务排队中")
    if printer.get("remote_ui_url"):
        print(f"> - ⚙️ **原厂控制台**：[{printer['remote_ui_url']}]({printer['remote_ui_url']})")
    print()


def cmd_queue(args):
    svc = PrinterService()
    target_pid = svc.resolve_printer_by_keyword(args.printer)
    data = svc.get_full_dashboard_data(target_pid)
    printer = data["printer"]
    active = data["active_printing"]
    queue = data["virtual_queue"]
    hw_jobs = data["hw_active_jobs"]
    total_q = len(queue) + len(hw_jobs)

    print(f"# 🖨️ [{printer['name']}] 智能调度控制台\n")
    if not printer["online"]:
        st_desc = "`🔴 离线`"
    elif printer.get("state_code") == 5 or any(k in str(printer.get("state_desc", "")) for k in ("暂停", "故障", "Stopped")):
        st_desc = f"`🔴 {printer.get('state_desc', '暂停/故障')}`"
    else:
        st_desc = "`🟢 连通就绪`"
    print(f"📍 **位置**：`{printer['location']}` ｜ **状态**：{st_desc}\n")

    print("| 🎯 实时出纸 | ⏳ 排队待办 | 🏭 设备型号 |")
    print("| :---: | :---: | :---: |")
    print(f"| **{'1 人出纸中' if active else '🟢 空闲待命'}** | **{total_q} 个任务** | **{printer['model']}** |\n")

    # 1. 谁在打印
    print("### 🎯 实时出纸透视")
    if active:
        act_state = active.get('state', '') or ''
        state_icon = "🔴" if any(k in str(act_state) for k in ("暂停", "故障", "Stopped", "Aborted", "异常", "失败")) else "🟢"
        print(f"> 📄 **文档名称**：`{active['job_name']}`  ")
        print(f"> 👤 **当前打印人**：`{active['user']}` ｜ **作业状态**：`{state_icon} {act_state}` ｜ **已出纸**：{active.get('sheets', 0)} 张 ｜ **来源**：{active['source']}")
    else:
        print("> 🟢 **当前无人打印**：设备处于就绪待机中，新提交的公文无需等待，直接出纸。")
    print()

    # 2. 队列列表 (移动端卡片化)
    print("### 📋 排队等待序列 (按优先级动态调度)")
    if not queue and not hw_jobs:
        print("> 📭 **排队序列为空**：暂无等待中的打印作业。\n")
    else:
        idx = 1
        for j in queue:
            p = j.get("priority", 50)
            p_badge = f"`🔥 {p} 紧急插队`" if p >= 90 else (f"`⚡ {p} 优先`" if p >= 70 else f"`📄 {p} 普通`")
            st_text = "⏳ 排队等待" if j.get("status") == "queued" else j.get("status")
            print(f"> **第 {idx} 位 · 任务 `#{j['id']}`** · {p_badge}")
            print(f"> - 📄 **文档**：**{j['filename']}** ({j.get('pages', 1)} 页)")
            print(f"> - 👤 **提交人**：`{j['user']}` ｜ **设备**：{j.get('printer_name')}")
            print(f"> - 📶 **状态**：{st_text}")
            print(">")
            idx += 1

        for hj in hw_jobs:
            hj_state = hj.get('state_desc', '') or ''
            hj_icon = "🔴" if any(k in str(hj_state) for k in ("暂停", "故障", "Stopped", "Aborted", "异常", "失败")) else "🔵"
            print(f"> **物理机在机 · 任务 `硬件#{hj['job_id']}`** · `⚡ 硬件排队`")
            print(f"> - 📄 **文档**：**{hj['job_name']}**")
            print(f"> - 👤 **提交人**：`{hj['user']}` ｜ **状态**：{hj_icon} {hj_state}")
            print(">")
            idx += 1
        print()

    print("---\n")
    print("### ⚡ 调度操作指令")
    print("- 🚀 **紧急插队** ➜ 回复 `「帮我把任务 #ID 设为紧急插队」`，置顶至首位出纸")
    print("- 🔼 **调整权重** ➜ 回复 `「将任务 #ID 优先级调为 80」`")
    print("- ❌ **撤销任务** ➜ 回复 `「取消任务 #ID」`")
    if printer.get("remote_ui_url"):
        print(f"- ⚙️ **原厂后台** ➜ [{printer['remote_ui_url']}]({printer['remote_ui_url']})")
    host_ip = get_host_lan_ip()
    print(f"- 📱 **手机控制台** ➜ 局域网访问 `http://{host_ip}:18828` 或本机 `http://localhost:18828`")


def cmd_print(args):
    svc = PrinterService()
    if not os.path.exists(args.file):
        print(f"❌ 错误：找不到文件 `{args.file}`")
        sys.exit(1)

    user = args.user or "用户"
    priority = args.priority if args.priority is not None else 50
    duplex = args.duplex or "duplex_long"
    copies = args.copies or 1

    res = svc.submit_document(
        filepath=args.file,
        user=user,
        printer_target=args.printer,
        priority=priority,
        duplex=duplex,
        copies=copies
    )

    if res.get("success"):
        job = res["job"]
        target_p = res.get("target_printer", {})
        p_badge = "🔥 紧急插队" if job['priority'] >= 90 else ("⚡ 优先出纸" if job['priority'] >= 70 else "📄 正常排队")
        print("# ✅ 打印任务已创建并提交；出纸结果待设备确认\n")
        print(f"> #### 任务编号 `#{job['id']}` · `已提交，等待硬件确认`")
        print(f"> - 🖨️ **目标设备**：**{target_p.get('name')}** (📍 {target_p.get('location')})")
        print(f"> - 📄 **打印文档**：`{job['filename']}` (共 {job['pages']} 页，{job['copies']} 份)")
        print(f"> - 👤 **提交人员**：`{job['user']}`")
        print(f"> - ⚡ **调度权重**：`{job['priority']} [{p_badge}]`")
        print(">\n> 💡 如需插队出纸，可直接回复 `「把任务 #" + str(job['id']) + " 设为紧急插队」`")
    else:
        print(f"❌ 提交打印任务失败：{res.get('error')}")
        sys.exit(1)


def cmd_priority(args):
    svc = PrinterService()
    res = svc.adjust_priority(args.job_id, args.priority)
    if res.get("success"):
        p = res['priority']
        p_badge = "🔥 紧急插队" if p >= 90 else ("⚡ 优先" if p >= 70 else "📄 普通")
        print(f"✅ **任务 `#{args.job_id}` 优先级已调整为 `{p} [{p_badge}]`！** 队列已动态重排。")
    else:
        print(f"❌ 调整失败：{res.get('error')}")
        sys.exit(1)


def cmd_cut_in(args):
    svc = PrinterService()
    res = svc.emergency_cut_in(args.job_id)
    if res.get("success"):
        print(f"🚀 **任务 `#{args.job_id}` 已成功设置为【🔥 紧急插队】（优先级 100）！**")
        print("> ⚡ **调度生效**：该任务已强势置顶至队列首位，目标打印机将**第一优先出纸**。")
    else:
        print(f"❌ 插队失败：{res.get('error')}")
        sys.exit(1)


def cmd_cancel(args):
    svc = PrinterService()
    res = svc.cancel_job(args.job_id, printer_id=args.printer, is_hardware=args.hw)
    if res.get("success"):
        print(f"🗑️ 打印任务 `#{args.job_id}` 已被成功取消并从调度队列中移除。")
    else:
        print(f"❌ 取消失败：{res.get('error') or '未收到打印机的有效取消回执'}")
        sys.exit(1)


def cmd_diag(args):
    """一键诊断打印机网络连通性、系统代理与端口状态"""
    from scripts.diag import run_full_diagnosis, format_diag_report
    svc = PrinterService()
    diag_res = run_full_diagnosis(svc.resolve_printer_by_keyword(args.printer))
    print(format_diag_report(diag_res))


def cmd_add_printer(args):
    from scripts.printer_registration import validate_endpoint, inspect_printer, save_inspected_printer
    try:
        endpoint = validate_endpoint({'ip':args.ip, 'ipp_port':args.ipp_port, 'ipp_path':args.ipp_path})
        result = inspect_printer(endpoint)
        print(json.dumps({'endpoint':endpoint, **result}, ensure_ascii=False, indent=2))
        if not result.get('can_save'):
            sys.exit(1)
        if args.save:
            printer = save_inspected_printer(endpoint, result, args.name or result['name'], args.location)
            print(json.dumps({'success':True,'printer':printer}, ensure_ascii=False, indent=2))
        else:
            print('只检测，未保存；确认添加后附加 --save，可指定 --name 和 --location。')
    except (ValueError, OSError) as exc:
        print('添加失败: ' + str(exc))
        sys.exit(1)


def cmd_set_default(args):
    if not args.printer.strip():
        raise ValueError('请指定要设为默认的打印机')
    printer_id = PrinterService().resolve_printer_by_keyword(args.printer)
    printer = set_default_printer(printer_id)
    print(f"已将 {printer.get('name', printer_id)}（{printer_id}）设为默认打印机。")


def main():
    parser = argparse.ArgumentParser(description="企业级多打印机智能管理与调度中心 CLI")
    subparsers = parser.add_subparsers(dest="subcommand", help="子命令")

    p_add = subparsers.add_parser('add-printer', help='检测并添加网络打印机；默认仅检测，--save 确认新增')
    p_add.add_argument('--ip', required=True)
    p_add.add_argument('--ipp-port', type=int, default=631)
    p_add.add_argument('--ipp-path', default='/ipp/print')
    p_add.add_argument('--name', default=None)
    p_add.add_argument('--location', default='')
    p_add.add_argument('--save', action='store_true')
    p_add.set_defaults(func=cmd_add_printer)

    p_printers = subparsers.add_parser("printers", help="查看公司所有打印机列表与状态大盘")
    p_printers.set_defaults(func=cmd_printers)

    p_default = subparsers.add_parser('set-default', help='设置本机技能的默认打印机')
    p_default.add_argument('--printer', '-P', required=True, help='设备 ID、名称或位置')
    p_default.set_defaults(func=cmd_set_default)

    p_diag = subparsers.add_parser("diag", help="网络连通性与代理一键深度体检诊断")
    p_diag.add_argument("--printer", "-P", default=None, help="打印机ID/位置/模糊关键词")
    p_diag.set_defaults(func=cmd_diag)

    p_status = subparsers.add_parser("status", help="查询指定或默认打印机连通状态")
    p_status.add_argument("--printer", "-P", default=None, help="打印机ID/位置/模糊关键词 (如 '3楼', '财务')")
    p_status.set_defaults(func=cmd_status)

    p_queue = subparsers.add_parser("queue", help="查看打印队列与谁在打印")
    p_queue.add_argument("--printer", "-P", default=None, help="打印机ID/位置/模糊关键词")
    p_queue.set_defaults(func=cmd_queue)

    p_print = subparsers.add_parser("print", help="免驱动发送打印文件")
    p_print.add_argument("--file", "-f", required=True, help="文件路径")
    p_print.add_argument("--printer", "-P", default=None, help="目标打印机 (如 '3楼', '财务')")
    p_print.add_argument("--user", "-u", default="用户", help="提交人")
    p_print.add_argument("--priority", "-p", type=int, default=50, help="优先级 (1-100)")
    p_print.add_argument("--duplex", "-d", default="duplex_long", help="单双面模式：auto / simplex / duplex_long / duplex_short（默认 duplex_long）")
    p_print.add_argument("--copies", "-c", type=int, default=1, help="份数")
    p_print.set_defaults(func=cmd_print)

    p_prio = subparsers.add_parser("priority", help="调整任务优先级")
    p_prio.add_argument("--job-id", "-j", type=int, required=True, help="任务 ID")
    p_prio.add_argument("--priority", "-p", type=int, required=True, help="新优先级 (1-100)")
    p_prio.set_defaults(func=cmd_priority)

    p_cut = subparsers.add_parser("cut-in", help="紧急插队置顶")
    p_cut.add_argument("--job-id", "-j", type=int, required=True, help="任务 ID")
    p_cut.set_defaults(func=cmd_cut_in)

    p_cancel = subparsers.add_parser("cancel", help="取消打印任务")
    p_cancel.add_argument("--job-id", "-j", type=int, required=True, help="任务 ID")
    p_cancel.add_argument("--printer", "-P", default=None, help="打印机ID")
    p_cancel.add_argument("--hw", action="store_true", help="是否取消硬件任务")
    p_cancel.set_defaults(func=cmd_cancel)

    args = parser.parse_args()
    try:
        if hasattr(args, "func"):
            args.func(args)
        else:
            cmd_printers(args)
    except ValueError as exc:
        print(f"操作未执行：{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
