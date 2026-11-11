# -*- coding: utf-8 -*-
"""
企业级打印管理核心服务层 (Printer Service)
支持全公司多设备集群管理、跨设备负载监控、动态路由与智能优先级调度。
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

from .config import BASE_DIR, get_printers, get_printer, get_default_printer_id
from .ipp_core import IPPClient
from .queue_scheduler import QueueScheduler
from .doc_converter import prepare_printable_file
from .office_remote import submission_error
from .pwg_raster import pdf_to_pwg

class PrinterService:
    def __init__(self):
        self.scheduler = QueueScheduler()

    def get_ipp_client(self, printer_id: Optional[str] = None) -> IPPClient:
        p_cfg = get_printer(self.resolve_printer_by_keyword(printer_id))
        return IPPClient(printer_cfg=p_cfg)

    def get_cluster_overview(self) -> List[Dict[str, Any]]:
        """获取全公司所有打印机的实时连通与负荷概览 (集群大盘)"""
        printers = get_printers()
        cluster_info = []
        for p in printers:
            pid = p.get("id")
            client = IPPClient(printer_cfg=p)
            hw = client.get_printer_status()
            q_jobs = self.scheduler.get_jobs(printer_id=pid, include_completed=False)
            cluster_info.append({
                "id": pid,
                "name": p.get("name", "打印机"),
                "location": p.get("location", "未指定位置"),
                "model": p.get("model", ""),
                "ip": p.get("ip", ""),
                "gateway": p.get("gateway", ""),
                "online": hw.get("online", False),
                "state_code": hw.get("state_code", 3 if hw.get("online") else 0),
                "state_desc": hw.get("state_desc", "未知"),
                "reasons": hw.get("reasons", []),
                "error": hw.get("error", ""),
                "queued_count": len([j for j in q_jobs if not j.get("hardware_job_id")]) + (hw.get("queued_jobs", 0) if hw.get("online") else 0),
                "is_default": (pid == get_default_printer_id()),
                "color_supported": p.get("color_supported", True)
            })
        return cluster_info

    def get_full_dashboard_data(self, selected_printer_id: Optional[str] = None) -> Dict[str, Any]:
        """获取综合看板数据：集群列表 + 当前选中打印机详情 + 正在打印人 + 队列"""
        target_pid = self.resolve_printer_by_keyword(selected_printer_id)
        p_cfg = get_printer(target_pid)
        cluster = self.get_cluster_overview()
        client = IPPClient(printer_cfg=p_cfg)

        hw_status = client.get_printer_status()
        hw_active_jobs = client.get_jobs("not-completed") if hw_status.get("online") else []
        hw_completed_jobs = client.get_jobs("completed") if hw_status.get("online") else []

        self.sync_hardware_jobs(target_pid, client)

        virtual_active_queue = self.scheduler.get_jobs(printer_id=target_pid, include_completed=False)
        virtual_all_jobs = self.scheduler.get_jobs(printer_id=target_pid, include_completed=True, limit=None)

        # Only correlate exact device job identities, never document names.
        def external_only(jobs):
            return [hw for hw in jobs if not any(
                local.get('hardware_job_id') == hw.get('job_id') and
                local.get('hardware_job_uri') and local.get('hardware_job_uri') == hw.get('job_uri')
                for local in virtual_all_jobs)]

        # 判定“谁在打印”
        active_printing = None
        if hw_active_jobs:
            first_hw = hw_active_jobs[0]
            active_printing = {
                "source": "物理硬件任务",
                "job_id": first_hw.get("job_id"),
                "job_name": first_hw.get("job_name"),
                "user": first_hw.get("user"),
                "state": first_hw.get("state_desc"),
                "sheets": first_hw.get("sheets_completed", 0),
                "printer_name": p_cfg.get("name")
            }
        else:
            for vj in virtual_active_queue:
                if vj.get("status") == "printing":
                    active_printing = {
                        "source": "智能调度任务",
                        "job_id": vj.get("id"),
                        "job_name": vj.get("filename"),
                        "user": vj.get("user"),
                        "state": "正在打印推送中",
                        "sheets": vj.get("pages", 1),
                        "printer_name": vj.get("printer_name", p_cfg.get("name"))
                    }
                    break

        return {
            "cluster": cluster,
            "selected_printer_id": target_pid,
            "printer": {
                "id": p_cfg.get("id"),
                "name": p_cfg.get("name"),
                "location": p_cfg.get("location"),
                "model": p_cfg.get("model", "Network Printer"),
                "ip": p_cfg.get("ip", ""),
                "gateway": p_cfg.get("gateway", ""),
                "online": hw_status.get("online", False),
                "state_code": hw_status.get("state_code", 3 if hw_status.get("online") else 0),
                "state_desc": hw_status.get("state_desc", "未知"),
                "error": hw_status.get("error", ""),
                "reasons": hw_status.get("reasons", []),
                "queued_count": len([j for j in virtual_active_queue if not j.get("hardware_job_id")]) + len(hw_active_jobs),
                "remote_ui_url": f"http://{p_cfg.get('ip')}:{p_cfg.get('remote_ui_port', 8000)}/portal_top.html" if p_cfg.get("remote_ui_port") else None
            },
            "active_printing": active_printing,
            "virtual_queue": virtual_active_queue,
            "hw_active_jobs": external_only(hw_active_jobs),
            "history": {
                "virtual_history": self.scheduler.history(target_pid)["items"],
                "hw_history": external_only(hw_completed_jobs)
            }
        }

    def resolve_printer_by_keyword(self, keyword: Optional[str] = None) -> str:
        """Resolve explicit input unambiguously; only omitted input uses the default."""
        printers = get_printers()
        kw = (keyword or '').strip().casefold()
        if not kw:
            return get_default_printer_id()
        def choose(matches):
            if len(matches) == 1:
                return matches[0]['id']
            choices = '；'.join(f"{p.get('name', p['id'])}（{p.get('location') or '位置未设置'}，ID：{p['id']}）" for p in matches)
            raise ValueError(f'匹配到多台打印机：{choices}；请提供完整名称或设备 ID')
        exact_ids = [p for p in printers if p['id'].casefold() == kw]
        if exact_ids:
            return choose(exact_ids)
        exact_names = [p for p in printers if p.get('name', '').casefold() == kw]
        if exact_names:
            return choose(exact_names)
        matches = [p for p in printers if any(kw in p.get(field, '').casefold() for field in ('name', 'location', 'model'))]
        if matches:
            return choose(matches)
        raise ValueError(f'未找到指定打印机：{keyword}；请选择已配置的设备')

    def submit_document(self, filepath: str, user: str = "普通用户",
                        printer_target: Optional[str] = None,
                        filename: Optional[str] = None, priority: int = 50,
                        duplex: str = "duplex_long", copies: int = 1,
                        auto_dispatch: bool = True) -> Dict[str, Any]:
        """用户或移动端提交打印任务（免驱动，支持自动路由）"""
        target_pid = self.resolve_printer_by_keyword(printer_target)
        printer_info = get_printer(target_pid)

        from .office_remote import submission_error
        error = submission_error(filepath)
        if error:
            return {'success': False, 'error': error}

        job = self.scheduler.enqueue(filepath, user, target_pid, filename, priority,
                                     pages=None, duplex=duplex, copies=copies, needs_conversion=True)
        dispatch = None
        if auto_dispatch:
            while job['status'] == 'queued':
                dispatch = self.dispatch_top_job(target_pid)
                job = self.scheduler.get_job(job['id'])
                if not dispatch.get('job_id'):
                    break
        if job['status'] in ('failed', 'unknown'):
            return {'success': False, 'job': job, 'error': job.get('error_msg', '发送失败'), 'dispatch': dispatch}

        return {
            "success": True,
            "job": job,
            "target_printer": printer_info,
            "message": f"任务 #{job['id']} 当前状态：{job['status']}，目标 [{printer_info.get('name')}]；接收不代表完成，请核查最终状态"
        }

    def enqueue_upload(self, filepath, user, printer_target=None, filename=None,
                       priority=50, duplex='auto', copies=1, request_id=None):
        pid = self.resolve_printer_by_keyword(printer_target)
        job = self.scheduler.enqueue(filepath, user, pid, filename, priority,
                                     pages=None, duplex=duplex, copies=copies,
                                     request_id=request_id, needs_conversion=True)
        return {'success': True, 'job': job, 'message': f"任务 #{job['id']} 已接收，等待后台处理"}

    def run_worker(self, stop):
        import logging
        import threading
        workers = {}
        def consume(pid):
            while not stop.is_set():
                try:
                    self.dispatch_top_job(pid)
                    if not stop.is_set():
                        self.sync_hardware_jobs(pid)
                except Exception:
                    logging.exception('后台打印处理失败: %s', pid)
                stop.wait(1)
        while not stop.is_set():
            for printer in get_printers():
                pid = printer['id']
                if pid not in workers:
                    workers[pid] = threading.Thread(target=consume, args=(pid,), daemon=True)
                    workers[pid].start()
            stop.wait(1)
        for worker in workers.values():
            worker.join(timeout=2)

    def dispatch_top_job(self, printer_id: Optional[str] = None) -> Dict[str, Any]:
        pid = printer_id or get_default_printer_id()
        with self.scheduler.dispatch_slot(pid) as acquired:
            if not acquired:
                return {'dispatched': False, 'reason': '该设备已有发送流程，任务保持排队'}
            return self._dispatch_claimed(pid)

    def _dispatch_claimed(self, printer_id: Optional[str] = None) -> Dict[str, Any]:
        """调度并发送指定打印机最高优先级的任务"""
        target_pid = printer_id or get_default_printer_id()
        p_cfg = get_printer(target_pid)
        top_job = self.scheduler.claim_next_job(target_pid)

        if not top_job:
            return {"dispatched": False, "reason": "该打印机队列中无等待任务"}

        job_id = top_job["id"]
        filepath = top_job["filepath"]
        user = top_job.get("user", "User")
        doc_name = top_job.get("filename", "Doc")

        trace = {'stage': 'conversion' if top_job.get('needs_conversion') else 'capabilities'}
        stage_start = time.monotonic()
        try:
            if top_job.get('needs_conversion'):
                filepath, pages = prepare_printable_file(filepath)
                trace['conversion_seconds'] = round(time.monotonic() - stage_start, 3)
                self.scheduler.record_delivery(job_id, 'sending', expected=('converting',),
                                               filepath=filepath, pages=pages, needs_conversion=False,
                                               delivery_trace=trace)
            trace['stage'] = 'capabilities'
            stage_start = time.monotonic()
            result = self._send_to_physical_printer(p_cfg, filepath, job_name=doc_name, user=user,
                                                    duplex=top_job.get('duplex', 'duplex_long'), copies=top_job.get('copies', 1),
                                                    trace=trace, before_send=lambda details: self.scheduler.record_delivery(
                                                        job_id, 'sending', expected=('sending',), delivery_trace=details))
            trace.update(delivery_seconds=round(time.monotonic() - stage_start, 3),
                         print_ipp_status=result.get('status_code'))
            if result.get('success'):
                trace['stage'] = 'confirmation'
                trace['accepted_at'] = time.time()
                self.scheduler.record_delivery(job_id, 'submitted', hardware_job_id=result.get('job_id'), hardware_job_uri=result.get('job_uri'), dispatch_active=False,
                                               document_format=result.get('document_format'), error_msg=None, delivery_trace=trace)
                return {'dispatched': True, 'job_id': job_id, 'status': 'submitted'}
            state = 'unknown' if result.get('uncertain') else 'failed'
            error = result.get('error') or '打印机拒绝接收作业'
            self.scheduler.record_delivery(job_id, state, error_msg=error, dispatch_active=False,
                                           document_format=result.get('document_format') or trace.get('document_format'),
                                           delivery_trace=trace)
            return {'dispatched': False, 'job_id': job_id, 'error': error}
        except Exception as exc:
            if self.scheduler.get_job(job_id)['status'] == 'canceled':
                return {'dispatched': False, 'job_id': job_id, 'reason': '任务已在发送前取消'}
            key = 'conversion_seconds' if trace['stage'] == 'conversion' else 'delivery_seconds'
            trace[key] = round(time.monotonic() - stage_start, 3)
            self.scheduler.record_delivery(job_id, 'unknown' if trace['stage'] == 'print_job' else 'failed',
                                           error_msg=str(exc), dispatch_active=False, delivery_trace=trace)
            return {'dispatched': False, 'job_id': job_id, 'error': str(exc)}

    def sync_hardware_jobs(self, printer_id: str, client=None):
        client = client or self.get_ipp_client(printer_id)
        for job in self.scheduler.get_jobs(printer_id=printer_id):
            if job.get('dispatch_active') or job.get('status') not in ('submitted', 'printing', 'unknown') or not job.get('hardware_job_id'):
                continue
            result = client.get_job(job['hardware_job_id'])
            if not result.get('success'):
                # Offline/expired job: retain pending state, never infer completion.
                trace = dict(job.get('delivery_trace') or {})
                reason = result.get('error') or '设备暂未返回该作业状态'
                if trace.get('confirmation_error') != reason or trace.get('confirmation_ipp_status') != result.get('status_code'):
                    trace.update(confirmation_error=reason, confirmation_ipp_status=result.get('status_code'))
                    try:
                        self.scheduler.record_delivery(job['id'], job['status'], expected_version=job.get('version', 0),
                                                       delivery_trace=trace)
                    except ValueError:
                        pass
                continue
            state = result.get('job-state')
            status = {3:'submitted', 4:'submitted', 5:'printing', 6:'submitted', 7:'canceled', 8:'failed', 9:'completed'}.get(state)
            if status:
                if job.get('hardware_job_uri') and result.get('job-uri') != job['hardware_job_uri']:
                    continue
                reasons = result.get('job-state-reasons', [])
                if isinstance(reasons, str):
                    reasons = [reasons]
                error = '; '.join(reasons) if state in (7, 8) else None
                trace = dict(job.get('delivery_trace') or {})
                trace.pop('confirmation_error', None)
                trace.update(device_job_state=state, confirmation_ipp_status=result.get('status_code'))
                if state in (7, 8, 9) and trace.get('accepted_at') is not None:
                    trace['confirmation_seconds'] = round(max(0, time.time() - trace['accepted_at']), 3)
                try:
                    self.scheduler.record_delivery(job['id'], status, expected_version=job.get('version', 0),
                                                   error_msg=error, state_reasons=reasons,
                                                   sheets_completed=result.get('job-media-sheets-completed'),
                                                   completion_verified=(state == 9), delivery_trace=trace)
                except ValueError:
                    pass  # Another actor changed the task while the printer was queried.

    def _send_to_physical_printer(self, printer_cfg: Dict[str, Any], pdf_path: str, job_name: str,
                                  user: str, duplex='duplex_long', copies=1, trace=None, before_send=None) -> Dict[str, Any]:
        """Negotiate the actual document format before submitting exactly once."""
        trace = {} if trace is None else trace
        trace['stage'] = 'capabilities'
        client = IPPClient(printer_cfg=printer_cfg)
        status = client.get_printer_status()
        if not status.get('online'):
            raise RuntimeError('打印机离线，未发送文档: ' + status.get('error', status.get('state_desc', '')))
        capabilities = status.get('capabilities', {})
        formats = capabilities.get('document-format-supported', [])
        if isinstance(formats, str):
            formats = [formats]
        sides = {'simplex':'one-sided', 'duplex_long':'two-sided-long-edge', 'duplex_short':'two-sided-short-edge'}.get(duplex)
        if duplex == 'auto':
            sides = capabilities.get('sides-default', 'one-sided')
            if isinstance(sides, list):
                sides = sides[0] if sides else 'one-sided'
        if not sides:
            raise ValueError('不支持的单双面选项')
        supported_sides = capabilities.get('sides-supported', ['one-sided'])
        if sides not in supported_sides:
            raise RuntimeError('目标设备不支持所选单双面模式，请调整后提交')
        copies = int(copies)
        copies_range = capabilities.get('copies-supported', [1, 1])
        if not isinstance(copies_range, list) or len(copies_range) != 2 or not copies_range[0] <= copies <= copies_range[1]:
            raise RuntimeError('份数超出设备支持范围，请调整份数后重新提交')
        with open(pdf_path, 'rb') as source:
            if source.read(5) != b'%PDF-':
                raise RuntimeError('仅支持转换为 PDF 后的文档，当前文件格式无法安全打印')
        trace['stage'] = 'format'
        with tempfile.TemporaryDirectory(prefix='print-raster-') as work:
            if 'application/pdf' in formats:
                doc_format, output = 'application/pdf', pdf_path
            elif 'image/pwg-raster' in formats:
                types = capabilities.get('pwg-raster-document-type-supported', [])
                grayscale = 'srgb_8' not in types or not printer_cfg.get('color_supported', True)
                if (grayscale and 'sgray_8' not in types) or (not grayscale and 'srgb_8' not in types):
                    raise RuntimeError('设备未声明兼容的 PWG Raster 色彩类型')
                resolutions = capabilities.get('pwg-raster-document-resolution-supported', [])
                if isinstance(resolutions, dict):
                    resolutions = [resolutions]
                dpi_options = [r['x'] for r in resolutions if isinstance(r, dict) and r.get('units') == 3 and r.get('x') == r.get('y') and 72 <= r['x'] <= 600]
                if not dpi_options:
                    raise RuntimeError('设备未声明可用的 PWG Raster 分辨率')
                dpi = min(dpi_options, key=lambda value: abs(value - 300))
                if sides != 'one-sided' and capabilities.get('pwg-raster-document-sheet-back', 'normal') != 'normal':
                    raise RuntimeError('该设备双面背页变换尚未支持，请选择单面或使用系统打印驱动')
                output = pdf_to_pwg(pdf_path, str(Path(work) / 'document.pwg'), dpi=dpi, grayscale=grayscale, sides=sides)
                doc_format = 'image/pwg-raster'
            else:
                raise RuntimeError('设备不支持 PDF 或兼容的 PWG Raster；未发送文件，请使用系统驱动打印')
            # Validate-Job does not create or print a job.
            trace.update(stage='validate_job', document_format=doc_format, payload_bytes=Path(output).stat().st_size)
            validation = client.validate_job(doc_format, sides=sides, copies=copies)
            trace['validate_ipp_status'] = validation.get('status_code')
            if not validation.get('success') or validation.get('status_code') != 0:
                unsupported = validation.get('unsupported_attributes', {})
                if 'sides' in unsupported:
                    detail = '设备不接受所选单双面设置，请选择单面或检查设备双面配置'
                elif 'copies' in unsupported:
                    detail = '设备不接受所选份数，请调整份数'
                elif 'document-format' in unsupported:
                    detail = '设备不接受所选文档格式，请使用系统驱动打印'
                else:
                    detail = '设备要求替换打印设置' if validation.get('status_code') == 1 else '设备拒绝所选格式或打印设置'
                code = validation.get('status_code')
                raise RuntimeError(detail + '，未发送文件（IPP ' + (hex(code) if isinstance(code, int) else '无有效响应') + '）')
            trace['stage'] = 'prepare_payload'
            payload = Path(output).read_bytes()
            if before_send:
                before_send({**trace, 'stage': 'print_job'})
            trace['stage'] = 'print_job'
            result = client.print_raw_payload(payload, doc_format=doc_format,
                                              job_name=job_name, user=user, sides=sides, copies=copies)
            result['document_format'] = doc_format
            return result

    def adjust_priority(self, job_id: int, new_priority: int) -> Dict[str, Any]:
        return self.scheduler.set_priority(job_id, new_priority)

    def emergency_cut_in(self, job_id: int) -> Dict[str, Any]:
        return self.scheduler.cut_in_line(job_id)

    def cancel_job(self, job_id: int, printer_id: Optional[str] = None, is_hardware: bool = False) -> Dict[str, Any]:
        if is_hardware:
            if not printer_id:
                return {'success': False, 'error': '取消设备作业时请指定对应的打印机'}
            return self.get_ipp_client(printer_id).cancel_job(job_id)
        job = self.scheduler.get_job(job_id)
        if not job:
            return {'success': False, 'error': f'找不到本机任务 #{job_id}'}
        if job['status'] == 'unknown' and job.get('hardware_job_id'):
            self.sync_hardware_jobs(job['printer_id'])
            job = self.scheduler.get_job(job_id)
        if job['status'] == 'canceled':
            return {'success': True, 'job': job, 'status': 'canceled'}
        if job['status'] in ('unknown', 'sending'):
            return {'success': False, 'job': job, 'error': '发送结果尚未确认，请先核查设备队列'}
        if job.get('hardware_job_id') and job['status'] in ('submitted', 'printing'):
            reply = self.get_ipp_client(job['printer_id']).cancel_job(job['hardware_job_id'])
            if not reply.get('success'):
                self.sync_hardware_jobs(job['printer_id'])
                reply['job'] = self.scheduler.get_job(job_id)
                return reply
            result = self.scheduler.cancel_job(job_id, expected=('submitted', 'printing'),
                                               expected_version=job.get('version', 0))
        else:
            result = self.scheduler.cancel_job(job_id, expected_version=job.get('version', 0))
        if not result['success']:
            current = self.scheduler.get_job(job_id)
            labels = {'queued': '排队中', 'paused': '已暂停', 'converting': '正在转换', 'sending': '正在发送',
                      'submitted': '等待设备确认', 'printing': '正在打印', 'completed': '设备已确认完成',
                      'failed': '打印失败', 'canceled': '已取消', 'unknown': '结果未知', 'unconfirmed': '未核实'}
            if current:
                if current['status'] == 'canceled':
                    return {'success': True, 'job': current, 'status': 'canceled'}
                result.update(job=current, error='当前状态为「' + labels.get(current['status'], '待核查') +
                              '」，未确认取消，请核查后再操作。')
        return result
