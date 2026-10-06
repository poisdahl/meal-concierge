"""Transport bounds must not cancel or retry an already dispatched operation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import Server


class RpcResilienceTests(unittest.TestCase):
    def server(self, app=None, capacity=2):
        with mock.patch("service.MAX_RPC_CONNECTIONS", capacity):
            return Server(Path("unused.sock"), os.getgid(), os.getuid(), app or mock.Mock())

    def pair(self):
        client, accepted = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(accepted.close)
        client.settimeout(2)
        return client, accepted

    def wait_released(self, server, count=1):
        for _ in range(count):
            self.assertTrue(server._connections.acquire(timeout=2), "worker did not release admission")
        for _ in range(count):
            server._connections.release()

    def test_partial_frame_expires_before_dispatch(self):
        server = self.server(capacity=1)
        client, accepted = self.pair()
        client.sendall(b'{"operation":')
        with mock.patch("service.RPC_FRAME_TIMEOUT", 0.05):
            self.assertTrue(server._start_worker(accepted))
            self.assertEqual(client.recv(1), b"")
            self.wait_released(server)
        server.app.handle.assert_not_called()

    def test_slow_drip_does_not_restart_frame_deadline(self):
        server = self.server()
        connection = mock.MagicMock()
        connection.recv.side_effect = [b'{', b' ', b' ', b'}\n']
        with (mock.patch("service.peer_uid", return_value=os.getuid()),
              mock.patch("service.time.monotonic", side_effect=[100, 101, 105, 109, 111])):
            server._serve(connection)
        self.assertEqual(connection.settimeout.call_args_list,
                         [mock.call(9), mock.call(5), mock.call(1)])
        self.assertEqual(connection.recv.call_count, 3)
        server.app.handle.assert_not_called()

    def test_capacity_rejects_without_dispatch_and_recovers_after_eof(self):
        server = self.server(capacity=2)
        clients = []
        for _ in range(2):
            client, accepted = self.pair()
            clients.append(client)
            self.assertTrue(server._start_worker(accepted))
        excess, accepted = self.pair()
        self.assertFalse(server._start_worker(accepted))
        self.assertEqual(excess.recv(1), b"")
        server.app.handle.assert_not_called()
        for client in clients:
            client.close()
        self.wait_released(server, 2)
        client, accepted = self.pair()
        server.app.handle.return_value = {"healthy": True}
        self.assertTrue(server._start_worker(accepted))
        client.sendall(b'{"operation":"status"}\n')
        self.assertEqual(json.loads(client.recv(4096))["result"], {"healthy": True})
        self.wait_released(server, 2)
        server.app.handle.assert_called_once_with({"operation": "status"})

    def test_thread_start_failure_closes_connection_and_releases_admission(self):
        server = self.server(capacity=1)
        client, accepted = self.pair()
        with mock.patch("service.threading.Thread.start", side_effect=RuntimeError("cannot start")):
            with self.assertRaisesRegex(RuntimeError, "cannot start"):
                server._start_worker(accepted)
        self.assertEqual(client.recv(1), b"")
        self.wait_released(server)

    def test_unexpected_application_failure_releases_admission(self):
        server = self.server(capacity=1)
        self.assertTrue(server._connections.acquire(blocking=False))
        with mock.patch.object(server, "_serve", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                server._serve_admitted(mock.Mock())
        self.wait_released(server)

    def test_disconnect_after_dispatch_does_not_cancel_or_retry_operation(self):
        entered = threading.Event()
        finish = threading.Event()
        outcomes = []

        def handle(_request):
            outcomes.append("dispatch journal")
            entered.set()
            if not finish.wait(2):
                raise AssertionError("test did not release operation")
            outcomes.append("confirmed outcome journal")
            return {"confirmed": True}

        app = mock.Mock(handle=mock.Mock(side_effect=handle))
        server = self.server(app, capacity=1)
        client, accepted = self.pair()
        self.addCleanup(finish.set)
        with mock.patch("service.RPC_FRAME_TIMEOUT", 0.01):
            client.sendall(b'{"operation":"synthetic_purchase"}\n')
            self.assertTrue(server._start_worker(accepted))
            self.assertTrue(entered.wait(2))
            client.close()
            self.assertIsNone(accepted.gettimeout())
            self.assertFalse(server._connections.acquire(blocking=False))
            finish.set()
            self.wait_released(server)
        self.assertEqual(outcomes, ["dispatch journal", "confirmed outcome journal"])
        app.handle.assert_called_once()

    def test_nonreading_client_hits_write_timeout_and_releases_admission(self):
        app = mock.Mock()
        app.handle.return_value = {"padding": "x" * 200_000}
        server = self.server(app, capacity=1)
        client, accepted = self.pair()
        accepted.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        with mock.patch("service.RPC_WRITE_TIMEOUT", 0.05):
            self.assertTrue(server._start_worker(accepted))
            client.sendall(b'{"operation":"synthetic_large_response"}\n')
            self.wait_released(server)
        app.handle.assert_called_once()
        self.assertEqual(accepted.fileno(), -1)

    def test_unauthorized_peer_releases_admission_without_dispatch(self):
        server = self.server(capacity=1)
        client, accepted = self.pair()
        with mock.patch("service.peer_uid", return_value=os.getuid() + 1000):
            self.assertTrue(server._start_worker(accepted))
            self.assertEqual(client.recv(1), b"")
            self.wait_released(server)
        server.app.handle.assert_not_called()

    def test_malformed_request_releases_admission_without_dispatch(self):
        server = self.server(capacity=1)
        client, accepted = self.pair()
        self.assertTrue(server._start_worker(accepted))
        client.sendall(b'not json\n')
        self.assertFalse(json.loads(client.recv(4096))["ok"])
        self.wait_released(server)
        server.app.handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
