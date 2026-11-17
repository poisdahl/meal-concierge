"""Queued requests expire before dispatch; admitted operations finish journals."""
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import rpc_client
import test_uncertain_error_response as fixtures


class RequestAdmissionTests(unittest.TestCase):
    setUp = fixtures.UncertainErrorResponseTests.setUp
    wire = fixtures.UncertainErrorResponseTests.wire

    def test_queued_mutation_expires_before_provider_or_state_work(self):
        result = {}
        self.app.product_plan_lock.acquire()
        try:
            worker = threading.Thread(target=lambda: result.update(self.wire({
                'operation': 'cart', 'action': 'change',
                'operations': [{'product_id': 10, 'quantity': 1}],
                'request_not_after': time.monotonic() + .03})))
            worker.start()
            time.sleep(.08)
        finally:
            self.app.product_plan_lock.release()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result['ok'])
        self.assertIn('expired', result['error'])
        self.assertFalse(self.shop.calls)
        self.assertFalse(self.store.read().get('pending_cart_change'))

    def test_started_write_finishes_readback_after_admission_deadline(self):
        original = self.shop.call
        clock = [100.0]
        def slow(tool, arguments, **kwargs):
            if tool == 'manipulate_cart':
                clock[0] = 1000.0
            return original(tool, arguments, **kwargs)
        self.shop.call = slow
        with mock.patch('service.time.monotonic', side_effect=lambda: clock[0]):
            result = self.wire({'operation': 'cart', 'action': 'change',
                                'operations': [{'product_id': 10, 'quantity': 1}],
                                'request_not_after': 200.0})
        self.assertTrue(result['ok'], result)
        self.assertFalse(self.store.read().get('pending_cart_change'))
        self.assertEqual(sum(tool == 'manipulate_cart' for tool, _ in self.shop.calls), 1)

    def test_invalid_deadline_is_rejected_and_never_enters_application(self):
        for value in (True, 'later'):
            with self.subTest(deadline=value):
                result = self.wire({'operation': 'cart', 'action': 'change', 'request_not_after': value})
                self.assertFalse(result['ok'])
                self.assertIn('finite', result['error'])
        self.assertFalse(self.shop.calls)

    def test_queued_checkout_expires_at_the_inner_browser_owner_before_reads(self):
        result = {}
        self.app.browser_lock.acquire()
        try:
            worker = threading.Thread(target=lambda: result.update(self.wire({
                'operation': 'checkout', 'action': 'prepare',
                'request_not_after': time.monotonic() + .03})))
            worker.start()
            time.sleep(.08)
        finally:
            self.app.browser_lock.release()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result['ok'], result)
        self.assertIn('expired', result['error'])
        self.assertFalse(self.shop.calls)
        self.assertFalse(self.store.read().get('pending_checkout'))

    def test_client_uses_server_clock_and_stays_compatible_with_old_cores(self):
        for health in ({'admission_deadline_clock': 1234.5}, {}):
            with self.subTest(health=health):
                sent = []
                replies = [health, {'done': True}]
                class Connection:
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def settimeout(self, value): pass
                    def connect(self, value): pass
                    def sendall(self, value): sent.append(json.loads(value))
                    def recv(self, size):
                        return (json.dumps({'ok': True, 'contract': 1, 'result': replies.pop(0)}) + '\n').encode()
                with mock.patch('rpc_client.socket.socket', side_effect=lambda *_args: Connection()):
                    self.assertTrue(rpc_client.rpc('cart', action='change')['done'])
                if health:
                    self.assertEqual(sent[1]['request_not_after'], 1234.5 + rpc_client.rpc_timeout('cart', {'action': 'change'}))
                else:
                    self.assertNotIn('request_not_after', sent[1])


if __name__ == '__main__':
    unittest.main()
