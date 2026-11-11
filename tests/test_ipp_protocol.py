import struct
import unittest
from unittest.mock import patch
from scripts.ipp_core import IPPClient, TAG_KEYWORD, TAG_INT, TAG_JOB, TAG_RESOLUTION, TAG_RANGE


class ProtocolTests(unittest.TestCase):
    def test_multivalue_attributes_use_zero_name_length(self):
        payload = IPPClient()._build_request(11, [(TAG_KEYWORD, 'requested-attributes', 'printer-state'), (TAG_KEYWORD, 'requested-attributes', 'sides-supported')])
        self.assertEqual(payload.count(b'requested-attributes'), 1)
        parsed = IPPClient().parse_attributes(payload)
        self.assertEqual(parsed['groups'][0]['requested-attributes'], ['printer-state', 'sides-supported'])

    def test_template_attributes_are_separate_job_group(self):
        payload = IPPClient()._build_request(2, [], [(TAG_KEYWORD, 'sides', 'two-sided-long-edge'), (TAG_INT, 'copies', 2)])
        parsed = IPPClient().parse_attributes(payload)
        self.assertEqual(parsed['groups'][1], {'sides':'two-sided-long-edge', 'copies':2})

    def test_resolution_and_range_are_decoded(self):
        payload = IPPClient()._build_request(11, [(TAG_RESOLUTION, 'resolution', struct.pack('>IIB', 300, 300, 3)), (TAG_RANGE, 'copies', struct.pack('>II', 1, 999))])
        group = IPPClient().parse_attributes(payload)['groups'][0]
        self.assertEqual(group['resolution'], {'x':300,'y':300,'units':3})
        self.assertEqual(group['copies'], [1,999])

    def test_hp_ignored_sides_are_identified(self):
        response = bytes.fromhex('010100010000000401470012617474726962757465732d6368617273657400057574662d3848001b617474726962757465732d6e61747572616c2d6c616e67756167650002656e054400057369646573001374776f2d73696465642d6c6f6e672d6564676503')
        result = IPPClient().parse_attributes(response)
        self.assertEqual(result['unsupported_attributes'], {'sides':'two-sided-long-edge'})
        self.assertTrue(result['success'])

    def test_names_with_language_exclude_binary_language_prefix(self):
        value = struct.pack('>H', 5) + b'zh-cn' + struct.pack('>H', 6) + '测试'.encode()
        for tag in (0x35, 0x36):
            payload = IPPClient()._build_request(11, [(tag, 'job-name', value)])
            self.assertEqual(IPPClient().parse_attributes(payload)['groups'][0]['job-name'], '测试')

    def test_error_response_is_not_online(self):
        response = b'\x01\x01\x04\x01\x00\x00\x00\x01\x03'
        with patch.object(IPPClient, '_send_ipp', return_value=response):
            self.assertFalse(IPPClient().get_printer_status()['online'])

    def test_send_timeout_is_uncertain_and_not_retried(self):
        with patch.object(IPPClient, '_send_ipp', side_effect=TimeoutError('timeout')) as send:
            result = IPPClient().print_raw_payload(b'%PDF-', doc_format='application/pdf')
            self.assertFalse(result['success'])
            self.assertTrue(result['uncertain'])
            self.assertEqual(send.call_count,1)

    def test_bad_response_does_not_claim_success(self):
        with patch.object(IPPClient, '_send_ipp', return_value=b''):
            self.assertTrue(IPPClient().print_raw_payload(b'test')['uncertain'])


if __name__ == '__main__':
    unittest.main()
