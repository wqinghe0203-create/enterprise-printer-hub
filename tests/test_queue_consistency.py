"""Isolated storage and mocked delivery; never contacts a printer."""
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch, Mock

from scripts import queue_scheduler as queue
from scripts.printer_service import PrinterService


def enqueue_process(state, count):
    queue.STATE_FILE = state
    scheduler = queue.QueueScheduler()
    for n in range(count):
        job = scheduler.enqueue('test.pdf', 'worker', filename=f'file-{n}.pdf')
        scheduler.set_priority(job['id'], 80)
        scheduler.cancel_job(job['id'])


def abandoned_process(state, pid):
    queue.STATE_FILE = state
    scheduler = queue.QueueScheduler()
    with scheduler.dispatch_slot(pid) as acquired:
        if not acquired:
            raise RuntimeError('lock not acquired')
        scheduler.claim_next_job(pid)
        os._exit(0)


def dispatch_process(state, sent_file, gate):
    import time
    queue.STATE_FILE = state
    service = PrinterService()
    def send(*args, **kwargs):
        with open(sent_file, 'a') as output:
            output.write('sent\n')
        time.sleep(.15)
        return {'success':True, 'job_id':17}
    gate.wait(10)
    with patch.object(service, '_send_to_physical_printer', side_effect=send):
        service.dispatch_top_job()


class ConsistencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = str(Path(temp.name) / 'queue.json')
        patcher = patch.object(queue, 'STATE_FILE', self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.scheduler = queue.QueueScheduler()
        self.pid = queue.get_default_printer_id()

    def test_multiprocess_writes_keep_unique_ids_and_updates(self):
        processes = [multiprocessing.Process(target=enqueue_process, args=(self.state, 12)) for _ in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        jobs = self.scheduler.get_jobs(include_completed=True, limit=None)
        self.assertEqual(len(jobs), 48)
        self.assertEqual(len({j['id'] for j in jobs}), 48)
        self.assertTrue(all(j['status'] == 'canceled' and j['priority'] == 80 for j in jobs))

    def test_legacy_migration_backup_once_and_preserve_ids(self):
        legacy = dict(next_id=411, active_job_id=None, jobs=[dict(id=410, filename='旧记录.pdf',
                      status='completed', printer_id=self.pid, user='同事', created_at='2026-11-01 12:00:00')])
        Path(self.scheduler.db_path).unlink()
        Path(self.state).write_text(json.dumps(legacy), encoding='utf-8')
        migrated = queue.QueueScheduler()
        self.assertEqual(migrated.get_job(410)['status'], 'unconfirmed')
        self.assertEqual(migrated.enqueue('next.pdf', 'u')['id'], 411)
        self.assertEqual(queue.QueueScheduler().get_job(411)['id'], 411)
        self.assertEqual(json.loads(Path(self.state+'.pre-sqlite.bak').read_text()), legacy)

    def test_corrupt_or_duplicate_legacy_never_initializes_empty(self):
        Path(self.scheduler.db_path).unlink()
        for data in ('broken', '{"jobs": [], "next_id": "bad"}',
                     json.dumps({'next_id':101,'jobs':[{'id':101},{'id':101}]})):
            Path(self.state).write_text(data)
            with self.assertRaisesRegex(RuntimeError, '迁移失败'):
                queue.QueueScheduler()
            self.assertEqual(Path(self.state).read_text(), data)
        Path(self.state).write_text('{"next_id":101,"jobs":[]}')
        self.assertEqual(queue.QueueScheduler().get_jobs(), [])

    def test_migration_failure_rolls_back_and_retry_is_not_duplicate(self):
        Path(self.scheduler.db_path).unlink()
        Path(self.state).write_text(json.dumps({'next_id':102,'jobs':[dict(id=101,status='queued',printer_id=self.pid)]}))
        with patch.object(queue.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaises(RuntimeError):
                queue.QueueScheduler()
        restored = queue.QueueScheduler()
        self.assertEqual(len(restored.get_jobs()), 1)
        self.assertEqual(len(queue.QueueScheduler().get_jobs()), 1)

    def test_idempotent_concurrent_submissions(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = list(pool.map(lambda _: self.scheduler.enqueue('same.pdf','u', request_id='same-request'), range(20)))
        self.assertEqual(len({j['id'] for j in jobs}), 1)
        self.assertEqual(len(self.scheduler.get_jobs()), 1)

    def test_concurrent_processes_send_exactly_once(self):
        self.scheduler.enqueue('ready.pdf','u')
        sent_file=self.state+'.sent'
        gate=multiprocessing.Event()
        workers=[multiprocessing.Process(target=dispatch_process,args=(self.state,sent_file,gate)) for _ in range(4)]
        for worker in workers:
            worker.start()
        gate.set()
        for worker in workers:
            worker.join(20)
            self.assertEqual(worker.exitcode,0)
        self.assertEqual(Path(sent_file).read_text(),'sent\n')

    def test_only_one_sender_and_cancel_before_claim(self):
        service = PrinterService()
        job = self.scheduler.enqueue('same.pdf', 'u')
        entered, finish = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            entered.set()
            finish.wait(5)
            return {'success':True,'job_id':17}
        with patch.object(service, '_send_to_physical_printer', side_effect=send) as transport:
            worker = threading.Thread(target=service.dispatch_top_job)
            worker.start()
            self.assertTrue(entered.wait(5))
            try:
                self.assertFalse(service.dispatch_top_job()['dispatched'])
                self.assertFalse(service.cancel_job(job['id'])['success'])
            finally:
                finish.set()
                worker.join(5)
            self.assertEqual(transport.call_count, 1)
        second = self.scheduler.enqueue('cancel.pdf', 'u')
        self.assertTrue(self.scheduler.cancel_job(second['id'])['success'])
        with patch.object(service, '_send_to_physical_printer') as transport:
            service.dispatch_top_job()
            transport.assert_not_called()

    def test_crashed_sender_is_unknown_and_never_retried(self):
        job = self.scheduler.enqueue('same.pdf','u')
        process = multiprocessing.Process(target=abandoned_process, args=(self.state,self.pid))
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 0)
        service = PrinterService()
        with patch.object(service,'_send_to_physical_printer') as send:
            service.dispatch_top_job()
            send.assert_not_called()
        self.assertEqual(self.scheduler.get_job(job['id'])['status'], 'unknown')

    def test_stale_update_cannot_override_cancel_or_completion(self):
        job = self.scheduler.enqueue('same.pdf','u')
        self.scheduler.cancel_job(job['id'])
        with self.assertRaises(ValueError):
            self.scheduler.record_delivery(job['id'],'submitted',expected_version=job['version'])

    def test_full_history_filters_and_independent_database(self):
        for i in range(45):
            job=self.scheduler.enqueue('report.pdf','Alice' if i%2 else 'Bob')
            self.scheduler.cancel_job(job['id'])
        result=self.scheduler.history(self.pid,page=9,size=5)
        self.assertEqual(result['total'],45)
        self.assertEqual(len(result['items']),5)
        self.assertEqual(self.scheduler.history(self.pid,user='Alice')['total'],22)
        self.assertEqual(self.scheduler.history(self.pid,search='#101')['total'],1)
        self.assertEqual(self.scheduler.history(self.pid,status='completed')['total'],0)
        self.assertEqual(self.scheduler.history(self.pid,start='2099-01-01')['total'],0)
        with patch.object(queue,'STATE_FILE',self.state+'.other.json'):
            self.assertEqual(queue.QueueScheduler().history()['total'],0)

    def test_conversion_failure_is_durable_and_never_sent(self):
        service=PrinterService()
        result=service.enqueue_upload('invalid.doc','u',request_id='upload-1')
        with patch('scripts.printer_service.prepare_printable_file',side_effect=ValueError('conversion failed')), patch.object(service,'_send_to_physical_printer') as send:
            service.dispatch_top_job()
            send.assert_not_called()
        job=self.scheduler.get_job(result['job']['id'])
        self.assertEqual(job['status'],'failed')
        self.assertIn('conversion failed',job['error_msg'])

    def test_cancel_during_conversion_never_sends(self):
        service=PrinterService()
        job=service.enqueue_upload('test.doc','u')['job']
        def convert(path):
            self.assertTrue(service.cancel_job(job['id'])['success'])
            return 'converted.pdf', 2
        with patch('scripts.printer_service.prepare_printable_file',side_effect=convert), patch.object(service,'_send_to_physical_printer') as send:
            service.dispatch_top_job()
            send.assert_not_called()
        self.assertEqual(self.scheduler.get_job(job['id'])['status'],'canceled')

    def test_background_consumer_continues_without_new_submission(self):
        import time
        service=PrinterService()
        for i in range(3):
            service.enqueue_upload('test.pdf','u')
        stop=threading.Event()
        with patch('scripts.printer_service.prepare_printable_file',return_value=('ready.pdf',2)), patch.object(service,'_send_to_physical_printer',return_value={'success':True,'job_id':17}) as send, patch.object(service,'sync_hardware_jobs'):
            worker=threading.Thread(target=service.run_worker,args=(stop,))
            worker.start()
            try:
                deadline=time.monotonic()+8
                while send.call_count<3 and time.monotonic()<deadline:
                    time.sleep(.05)
                self.assertEqual(send.call_count,3)
            finally:
                stop.set()
                worker.join(5)

    def test_offline_device_fails_before_transport(self):
        service=PrinterService()
        job=self.scheduler.enqueue('ready.pdf','u')
        with patch('scripts.printer_service.IPPClient') as ipp:
            ipp.return_value.get_printer_status.return_value={'online':False}
            service.dispatch_top_job()
            ipp.return_value.print_raw_payload.assert_not_called()
        self.assertEqual(self.scheduler.get_job(job['id'])['status'],'failed')

    def test_device_records_merge_only_when_identity_matches(self):
        service=PrinterService()
        job=self.scheduler.enqueue('same.pdf','u')
        self.scheduler.record_delivery(job['id'],'completed',hardware_job_id=17,hardware_job_uri='ipp://device/job/17',completion_verified=True)
        hw=[dict(job_id=17,job_uri='ipp://device/job/17'),dict(job_id=18,job_name='same.pdf')]
        with patch.object(service,'get_cluster_overview',return_value=[]), patch('scripts.printer_service.IPPClient') as ipp:
            ipp.return_value.get_printer_status.return_value={'online':True}
            ipp.return_value.get_jobs.side_effect=[[],hw]
            dashboard=service.get_full_dashboard_data(self.pid)
        self.assertEqual([j['job_id'] for j in dashboard['history']['hw_history']],[18])


if __name__ == '__main__':
    unittest.main()
