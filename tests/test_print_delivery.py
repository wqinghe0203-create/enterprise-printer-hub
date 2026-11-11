"""No physical printing: mocked transport with an isolated queue."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.printer_service import PrinterService
from scripts import queue_scheduler


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / 'queue.json'
        patcher = patch.object(queue_scheduler, 'STATE_FILE', str(state))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = PrinterService()
        self.pdf = Path(self.tmp.name) / 'test.pdf'
        self.pdf.write_bytes(b'%PDF-1.4\nfixture')
        self.job = self.service.scheduler.enqueue(str(self.pdf), 'test')

    def test_accepted_job_is_not_completed(self):
        with patch.object(self.service, '_send_to_physical_printer', return_value={'success': True, 'job_id': 17}):
            self.service.dispatch_top_job()
        job = self.service.scheduler.get_job(self.job['id'])
        self.assertEqual(job['status'], 'submitted')
        self.assertEqual(job['hardware_job_id'], 17)

    def test_explicit_rejection_is_failed(self):
        with patch.object(self.service, '_send_to_physical_printer', return_value={'success': False, 'error': 'format rejected'}):
            result = self.service.dispatch_top_job()
        self.assertFalse(result['dispatched'])
        self.assertEqual(self.service.scheduler.get_job(self.job['id'])['status'], 'failed')

    def test_cancel_race_reports_latest_state_and_original_device(self):
        scheduler = self.service.scheduler
        job = scheduler.record_delivery(self.job['id'], 'submitted', hardware_job_id=77)
        with patch.object(self.service, 'get_ipp_client') as get_client:
            def finished_before_cancel(_):
                scheduler.record_delivery(job['id'], 'completed', completion_verified=True)
                return {'success': True}

            get_client.return_value.cancel_job.side_effect = finished_before_cancel
            result = self.service.cancel_job(job['id'], printer_id='wrong-device')
        get_client.assert_called_once_with(job['printer_id'])
        get_client.return_value.cancel_job.assert_called_once_with(77)
        self.assertFalse(result['success'])
        self.assertIn('设备已确认完成', result['error'])
        self.assertEqual(result['job']['status'], 'completed')

    def test_ambiguous_delivery_never_claims_completed_or_retries(self):
        with patch.object(self.service, '_send_to_physical_printer', return_value={'success': False, 'uncertain': True, 'error': 'timeout'}) as send:
            self.service.dispatch_top_job()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.service.scheduler.get_job(self.job['id'])['status'], 'unknown')

    def test_legacy_completed_records_are_unverified(self):
        self.service.scheduler.mark_completed(self.job['id'])
        job = self.service.scheduler.get_jobs(include_completed=True)[0]
        self.assertEqual(job['status'], 'unconfirmed')

    def test_hardware_abort_updates_local_state(self):
        self.service.scheduler.record_delivery(self.job['id'], 'submitted', hardware_job_id=17)
        with patch('scripts.printer_service.IPPClient') as mock:
            mock.return_value.get_job.return_value = {'success': True, 'job-state':8, 'job-state-reasons':['document-format-error']}
            self.service.sync_hardware_jobs(self.job['printer_id'])
        job = self.service.scheduler.get_job(self.job['id'])
        self.assertEqual(job['status'],'failed')
        self.assertIn('document-format-error',job['error_msg'])

    def test_hardware_completion_is_required(self):
        self.service.scheduler.record_delivery(self.job['id'], 'submitted', hardware_job_id=17)
        with patch('scripts.printer_service.IPPClient') as mock:
            mock.return_value.get_job.return_value = {'success': True, 'job-state':9, 'job-media-sheets-completed':1}
            self.service.sync_hardware_jobs(self.job['printer_id'])
        self.assertEqual(self.service.scheduler.get_jobs(include_completed=True)[0]['status'],'completed')

    def test_disappeared_hardware_job_does_not_mean_success(self):
        self.service.scheduler.record_delivery(self.job['id'], 'submitted', hardware_job_id=17)
        with patch('scripts.printer_service.IPPClient') as mock:
            mock.return_value.get_job.return_value = {'success': False}
            self.service.sync_hardware_jobs(self.job['printer_id'])
        self.assertEqual(self.service.scheduler.get_job(self.job['id'])['status'],'submitted')

    def test_pdf_is_not_sent_to_raster_only_printer(self):
        capability = {'online':True,'capabilities': {'document-format-supported':['image/pwg-raster'], 'sides-supported':['two-sided-long-edge'], 'copies-supported':[1,99], 'pwg-raster-document-type-supported':['srgb_8'], 'pwg-raster-document-resolution-supported':{'x':300,'y':300,'units':3}}}
        def raster(source, destination, **options):
            Path(destination).write_bytes(b'RaS2fixture')
            return destination
        with patch('scripts.printer_service.IPPClient') as mock, patch('scripts.printer_service.pdf_to_pwg', side_effect=raster):
            mock.return_value.get_printer_status.return_value = capability
            mock.return_value.validate_job.return_value = {'success':True,'status_code':0}
            mock.return_value.print_raw_payload.return_value = {'success':True,'job_id':17}
            result = self.service._send_to_physical_printer({},str(self.pdf),'test','test')
            call = mock.return_value.print_raw_payload.call_args
            self.assertEqual(call.args[0],b'RaS2fixture')
            self.assertEqual(call.kwargs['doc_format'],'image/pwg-raster')
            self.assertEqual(result['job_id'],17)

    def test_device_default_and_explicit_duplex_do_not_silently_substitute(self):
        with patch('scripts.printer_service.IPPClient') as mock:
            client = mock.return_value
            client.get_printer_status.return_value = {'online':True,'capabilities':{
                'document-format-supported':['application/pdf'], 'sides-default':'one-sided',
                'sides-supported':['one-sided','two-sided-long-edge'], 'copies-supported':[1,999]}}
            client.validate_job.return_value = {'success':True,'status_code':1,
                'unsupported_attributes':{'sides':'two-sided-long-edge'}}
            with self.assertRaisesRegex(RuntimeError, '单双面'):
                self.service._send_to_physical_printer({},str(self.pdf),'test','test',duplex='duplex_long')
            client.print_raw_payload.assert_not_called()
            client.validate_job.return_value = {'success':True,'status_code':0}
            client.print_raw_payload.return_value = {'success':True,'job_id':18}
            self.service._send_to_physical_printer({},str(self.pdf),'test','test',duplex='auto')
            self.assertEqual(client.validate_job.call_args.kwargs['sides'],'one-sided')
            self.assertEqual(client.print_raw_payload.call_args.kwargs['sides'],'one-sided')
            self.assertEqual(client.print_raw_payload.call_count,1)

    def test_unsupported_copies_are_rejected_before_print_job(self):
        with patch('scripts.printer_service.IPPClient') as mock:
            client = mock.return_value
            client.get_printer_status.return_value = {'online':True,'capabilities':{
                'document-format-supported':['application/pdf'], 'sides-supported':['one-sided'],
                'copies-supported':[1,1]}}
            with self.assertRaisesRegex(RuntimeError, '调整份数'):
                self.service._send_to_physical_printer({}, str(self.pdf), 'test', 'test', duplex='simplex', copies=3)
            client.print_raw_payload.assert_not_called()

    def test_unsupported_format_is_rejected_before_print_job(self):
        with patch('scripts.printer_service.IPPClient') as mock:
            mock.return_value.get_printer_status.return_value = {'online':True,'capabilities':{'sides-supported':['two-sided-long-edge'],'copies-supported':[1,1],'document-format-supported':['application/octet-stream']}}
            with self.assertRaisesRegex(RuntimeError,'不支持 PDF'):
                self.service._send_to_physical_printer({},str(self.pdf),'test','test')
            mock.return_value.print_raw_payload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
