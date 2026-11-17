"""A lost write response must survive the service, RPC and presentation layers."""
from copy import deepcopy
import fcntl
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import threading
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from core import HouseholdError, StateStore
from service import Application, Server
from test_meal_concierge import CONFIG, FakeBrowser, MutableFakeOda
from retail_mcp import RetailMcpClient, RetailPreDispatchError
import cli
import mcp_server
import rpc_client


class UncertainErrorResponseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name), CONFIG)
        self.shop = MutableFakeOda()
        self.app = Application(self.store, self.shop, FakeBrowser())

    def wire(self, request):
        client, peer = socket.socketpair()
        server = Server(Path(self.temp.name) / 'unused.sock', 0, 0, self.app)
        with mock.patch('service.peer_uid', return_value=0):
            worker = threading.Thread(target=server._serve, args=(peer,))
            worker.start()
            try:
                client.settimeout(3)
                client.sendall(json.dumps(request).encode() + b'\n')
                data = b''
                while b'\n' not in data:
                    data += client.recv(65536)
                return json.loads(data)
            finally:
                client.close()
                worker.join(3)
                self.assertFalse(worker.is_alive())

    def test_dispatched_cart_write_remains_unknown_and_is_not_resent(self):
        original = self.shop.call
        writes = []
        def lost_reply(tool, arguments, **kwargs):
            result = original(tool, arguments, **kwargs)
            if tool == 'manipulate_cart':
                writes.append(deepcopy(arguments))
                raise HouseholdError('synthetic reply lost after dispatch')
            return result
        self.shop.call = lost_reply
        request = {'operation': 'cart', 'action': 'change',
                   'operations': [{'product_id': 10, 'quantity': 1}]}
        failed = self.wire(request)
        self.assertFalse(failed['ok'])
        self.assertEqual(failed['outcome'], 'unknown')
        self.assertIn('pending_cart_change', self.store.read())
        repeated = self.wire(request)
        self.assertEqual(repeated['outcome'], 'unknown')
        self.assertEqual(len(writes), 1)
        different = {**request, "operations": [{"product_id": 11, "quantity": 1}]}
        self.assertNotIn("outcome", self.wire(different))

    def test_predispatch_validation_error_is_still_an_ordinary_rejection(self):
        result = self.wire({'operation': 'cart', 'action': 'change', 'operations': []})
        self.assertFalse(result['ok'])
        self.assertNotIn('outcome', result)

    def test_proven_local_predispatch_stop_does_not_leave_a_cart_journal(self):
        original = self.shop.call
        def stopped(tool, arguments, **kwargs):
            if tool == 'manipulate_cart':
                raise RetailPreDispatchError('another synthetic operation is active')
            return original(tool, arguments, **kwargs)
        self.shop.call = stopped
        request = {'operation': 'cart', 'action': 'change',
                   'operations': [{'product_id': 10, 'quantity': 1}]}
        failed = self.wire(request)
        self.assertFalse(failed['ok'])
        self.assertNotIn('outcome', failed)
        self.assertFalse(self.store.read().get('pending_cart_change'))
        self.shop.call = original
        self.assertTrue(self.wire(request)['ok'])

    def test_local_transport_errors_are_typed_before_entering_async_transport(self):
        client = RetailMcpClient.__new__(RetailMcpClient)
        client.label = 'Synthetic'
        client.provider = 'oda'
        client.token_directory = Path(self.temp.name) / 'missing'
        with mock.patch.object(client, '_run_async', side_effect=AssertionError('transport entered')):
            with self.assertRaises(RetailPreDispatchError):
                client._run('manipulate_cart', {}, 10)
            client.token_directory.mkdir()
            def held_lock(_fd, flags):
                if flags != fcntl.LOCK_UN:
                    raise BlockingIOError()
            with mock.patch('retail_mcp.fcntl.flock', side_effect=held_lock):
                with self.assertRaises(RetailPreDispatchError):
                    client._run('manipulate_cart', {}, .02)

    def test_short_transport_contention_spends_the_original_timeout(self):
        client = RetailMcpClient(self.temp.name)
        budgets = []
        async def synthetic(_tool, _args, timeout):
            budgets.append(timeout)
            return {"ok": True}
        release = threading.Event()
        acquired = threading.Event()
        def hold():
            with client._lock():
                acquired.set()
                release.wait(2)
        owner = threading.Thread(target=hold)
        owner.start()
        try:
            self.assertTrue(acquired.wait(1))
            timer = threading.Timer(.08, release.set)
            timer.start()
            with mock.patch.object(client, "_run_async", synthetic):
                self.assertTrue(client.call("get_cart", {}, deadline=time.monotonic()+1)["ok"])
            self.assertEqual(len(budgets), 1)
            self.assertGreater(budgets[0], 0)
            self.assertLess(budgets[0], .98)
        finally:
            release.set()
            owner.join(2)

    def test_unrelated_journals_cannot_label_an_error_uncertain(self):
        # Exercise Application's common exception boundary without inventing
        # provider confirmation evidence or dispatching a checkout.
        for name in ('pending_checkout', 'pending_cancellation', 'order_change'):
            for status, unknown in [('awaiting_confirmation', False), ('clicking', True), ('uncertain', True)]:
                with self.subTest(journal=name, status=status):
                    state = {name: {'status': status}}
                    with mock.patch.object(self.store, 'read', return_value=state), \
                         mock.patch.object(self.app, '_handle', side_effect=HouseholdError('synthetic stop')):
                        result = self.wire({'operation': 'checkout', 'action': 'confirm'})
                    self.assertNotIn('outcome', result)  # Unrelated state is never evidence of this call.

    def test_socket_fallback_preserves_completed_and_uncertain_commerce_outcomes(self):
        cases = [
            ({"operation": "checkout", "action": "confirm"}, {"confirmed": True, "order_id": "order-1",
                "confirmation_id": "token-1", "detail": "x" * 8000}),
            ({"operation": "products", "action": "apply"}, {"applied": False, "outcome_unknown": True,
                "operation_id": "original-1", "detail": float("nan")}),
        ]
        for request, payload in cases:
            with self.subTest(request=request), mock.patch.object(self.app, "handle", return_value=payload), \
                    mock.patch("service.MAX_REQUEST", 1024):
                result = self.wire(request)["result"]
                for key, value in payload.items():
                    if key != "detail":
                        self.assertEqual(result[key], value)
                self.assertNotIn("detail", result)
                self.assertTrue(result["details_omitted"])
        with mock.patch.object(self.app, "handle", return_value={"detail": "x" * 8000}), mock.patch("service.MAX_REQUEST", 1024):
            self.assertEqual(self.wire({"operation":"cart", "action":"change"})["outcome"], "unknown")
            self.assertNotIn("outcome", self.wire({"operation":"cart", "action":"get"}))

    def test_rpc_after_send_eof_marks_only_possible_merchant_mutation(self):
        for action, expected in (("change", rpc_client.ServiceError), ("get", RuntimeError)):
            with self.subTest(action=action), mock.patch("rpc_client.socket.socket") as connection:
                sock = connection.return_value.__enter__.return_value
                sock.recv.side_effect = [b'{"ok":true,"contract":1,"result":{}}\n', b""]
                with self.assertRaises(expected) as raised:
                    rpc_client.rpc("cart", action=action)
                self.assertEqual(getattr(raised.exception, "outcome_unknown", False), action == "change")
                self.assertEqual(sock.sendall.call_count, 2)  # Health plus the one original request.

    def test_rpc_and_mcp_and_cli_preserve_unknown_disposition(self):
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): pass
            def connect(self, value): pass
            def sendall(self, value): pass
            def recv(self, count):
                return b'{"ok":false,"error":"synthetic response lost","outcome":"unknown"}\n'
        with mock.patch('rpc_client.socket.socket', return_value=Connection()):
            with self.assertRaises(rpc_client.ServiceError) as raised:
                rpc_client.rpc('health')
        error = raised.exception
        with mock.patch('mcp_server.service_rpc', side_effect=error):
            result = mcp_server.rpc('cart', action='change')
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertIn('original', result['recovery'])
        stdin = io.TextIOWrapper(io.BytesIO(b'{"operation":"cart","action":"change"}'))
        stdout = io.StringIO()
        with mock.patch('cli.rpc', side_effect=error), mock.patch('sys.stdin', stdin), \
             mock.patch('sys.stdout', stdout), mock.patch('sys.argv', ['cli.py']):
            self.assertEqual(cli.main(), 1)
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'outcome_unknown')
        with mock.patch('mcp_server.service_rpc', side_effect=rpc_client.ServiceError('invalid request')):
            self.assertEqual(mcp_server.rpc('cart', action='change')['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
