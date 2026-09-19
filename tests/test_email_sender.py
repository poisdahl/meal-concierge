"""Real menu/order journals and MIME deliveries; network only to a loopback sink."""
from copy import deepcopy
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import subprocess
import time
import asyncio
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from email_sender import EmailSender
from email_transports import GmailTransport, NotSent, SMTPTransport, check_message, capability
from menu_planning import menu_ref
from test_recipe_delivery import fixture, SMTPHandler
from test_email_scheduler import EmailSchedulerTests


class Mailbox:
    """An observable synthetic provider, including acceptance with a lost response."""
    def __init__(self):
        self.sender = "sender@example.test"
        self.messages = []
        self.failure = None

    def inspect(self):
        return capability(self.sender, "gmail", "synthetic:profile-and-grant", reconciliation=True)

    def send(self, raw, binding):
        check_message(raw, binding)
        if self.failure == "before":
            raise NotSent("synthetic refusal")
        self.messages.append(raw)
        if self.failure == "after":
            raise ConnectionError("synthetic lost response after acceptance")
        return {"outcome": "accepted", "evidence": "synthetic:message:1"}

    def reconcile(self, raw, binding):
        if self.sender != binding["account"]:
            raise ValueError("wrong account")
        return ({"outcome": "accepted", "evidence": "synthetic:message:1"}
                if raw in self.messages else {"outcome": "unknown"})


class SenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="email-sender-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app, self.menu, _ = fixture(self.root)
        self.mailbox = Mailbox()
        self.config = {"runtime_id": "test-host", "receipt_dir": str(self.root / "receipts"),
                       "connections": [{"id": "gmail", "type": "command", "unattended": True}]}
        self.runner = self.new_runner()
        self.runner.execute({"action": "configure", "recipient": "recipient@example.test", "timing": "both"})

    def rpc(self, operation, **request):
        return self.app.handle({"operation": operation, **request})

    def new_runner(self, rpc=None):
        return EmailSender(rpc or self.rpc, self.config, transport_factory=lambda c: self.mailbox)

    def send(self, runner=None, action="send"):
        return (runner or self.runner).execute({"action": action, "delivery_requested": True,
                                                "request_id": "menu-1", "menu_ref": menu_ref(self.menu)})

    def test_setup_reused_after_restart_email_only_preserves_chat_pdf_and_message_id(self):
        self.assertEqual([], self.mailbox.messages)
        self.assertTrue(self.send(self.new_runner())["sent"])
        self.assertTrue(self.send(self.new_runner())["sent"])
        self.assertEqual(1, len(self.mailbox.messages))
        message = BytesParser(policy=policy.default).parsebytes(self.mailbox.messages[0])
        self.assertTrue(message["Message-ID"])
        self.assertTrue(message["Date"])
        self.assertTrue(any(p.get_payload(decode=True).startswith(b"%PDF") for p in message.walk() if p.get_content_type() == "application/pdf"))
        self.assertTrue(self.rpc("recipe_delivery", action="status")["preferences"]["chat"]["enabled"])

    def test_positive_receipt_survives_ack_loss_without_second_send(self):
        def rpc(operation, **request):
            if request.get("action") == "reconcile":
                raise ConnectionError("socket lost")
            return self.rpc(operation, **request)
        result = self.send(self.new_runner(rpc))
        self.assertTrue(result["sent"])
        self.assertFalse(result["acknowledged"])
        self.assertTrue(self.send(self.new_runner())["acknowledged"])
        self.assertEqual(1, len(self.mailbox.messages))

    def test_lost_provider_response_reconciles_original_message(self):
        self.mailbox.failure = "after"
        self.assertEqual("unknown", self.send()["outcome"])
        self.assertTrue(self.send(self.new_runner(), "reconcile")["sent"])
        self.assertEqual(1, len(self.mailbox.messages))
        self.assertEqual("accepted", self.rpc("recipe_delivery", action="get", job_id="menu-1")["parts"][0]["status"])

    def test_definite_failure_requires_explicit_same_occurrence_retry(self):
        self.mailbox.failure = "before"
        self.assertEqual("not_sent", self.send()["outcome"])
        self.mailbox.failure = None
        self.assertEqual("not_sent", self.send()["outcome"])
        self.assertTrue(self.send(action="retry")["sent"])
        self.assertEqual(1, len(self.mailbox.messages))
        self.assertEqual(1, self.rpc("recipe_delivery", action="status")["job_count"])

    def test_pre_export_interruption_can_retry_without_claiming_foreign_token(self):
        with mock.patch("email_sender.export_part", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.send()
        self.assertEqual("not_sent", self.send()["outcome"])
        self.assertTrue(self.send(action="retry")["sent"])

    def test_foreign_begin_never_becomes_our_no_send_evidence(self):
        def rpc(operation, **request):
            if request.get("action") == "begin":
                self.rpc(operation, **{**request, "executor_attempt": "other-executor"})
                raise ConnectionError("our begin response lost")
            return self.rpc(operation, **request)
        with self.assertRaises(ConnectionError):
            self.send(self.new_runner(rpc))
        self.assertEqual("unknown", self.send()["outcome"])
        part = self.rpc("recipe_delivery", action="get", job_id="menu-1")["parts"][0]
        self.assertEqual("attempting", part["status"])
        self.assertEqual("other-executor", part["executor_attempt"])

    def test_our_lost_begin_response_proves_no_network_and_can_retry(self):
        def rpc(operation, **request):
            result = self.rpc(operation, **request)
            if request.get("action") == "begin":
                raise ConnectionError("lost begin ack")
            return result
        with self.assertRaises(ConnectionError):
            self.send(self.new_runner(rpc))
        self.assertEqual("not_sent", self.send()["outcome"])
        self.assertTrue(self.send(action="retry")["sent"])

    def test_lost_core_retry_ack_is_idempotent_for_original_token(self):
        self.mailbox.failure = "before"
        self.assertEqual("not_sent", self.send()["outcome"])
        def rpc(operation, **request):
            result = self.rpc(operation, **request)
            if request.get("action") == "retry":
                raise ConnectionError("lost retry ack")
            return result
        with self.assertRaises(ConnectionError):
            self.send(self.new_runner(rpc), action="retry")
        self.mailbox.failure = None
        self.assertTrue(self.send(action="retry")["sent"])
        self.assertEqual(1, len(self.mailbox.messages))

    def test_account_switch_and_pause_never_send(self):
        self.mailbox.sender = "other@example.test"
        with self.assertRaisesRegex(ValueError, "account changed"):
            self.send()
        self.mailbox.sender = "sender@example.test"
        self.rpc("recipe_delivery", action="pause")
        with self.assertRaisesRegex(Exception, "paused"):
            self.send()
        self.assertEqual([], self.mailbox.messages)

    def test_local_lock_rejects_concurrent_attempt(self):
        with self.runner.locked({"kind": "menu", "request_id": "menu-1"}):
            with self.assertRaises(BlockingIOError):
                self.send(self.new_runner())
        self.assertEqual([], self.mailbox.messages)

    def test_two_households_same_request_id_never_share_receipts(self):
        self.assertTrue(self.send()["sent"])
        other, menu, _ = fixture(self.root / "other-household")
        def rpc(operation, **request):
            return other.handle({"operation": operation, **request})
        runner = self.new_runner(rpc)
        runner.execute({"action": "configure", "recipient": "recipient@example.test"})
        result = runner.execute({"action": "send", "request_id": "menu-1", "menu_ref": menu_ref(menu), "delivery_requested": True})
        self.assertTrue(result["sent"])
        self.assertEqual(2, len(self.mailbox.messages))
        self.assertEqual("accepted", rpc("recipe_delivery", action="get", job_id="menu-1")["parts"][0]["status"])

    def test_smtp_identity_includes_endpoint_and_authenticated_username(self):
        row = {"host": "smtp.example.test", "sender": "sender@example.test", "username": "account-1"}
        original = SMTPTransport(row).account()
        self.assertNotEqual(original, SMTPTransport({**row, "username": "account-2"}).account())
        self.assertNotEqual(original, SMTPTransport({**row, "host": "different.example.test"}).account())

    def test_mime_actual_headers_reject_hidden_duplicate_and_changed_recipients(self):
        self.send()
        raw = self.mailbox.messages[0]
        binding = self.rpc("recipe_delivery", action="sender")["binding"]
        for header in (b"Bcc: other@example.test\r\n", b"Cc: other@example.test\r\n", b"To: recipient@example.test\r\n",
                       b"Resent-To: other@example.test\r\n", b"From: other@example.test\r\n"):
            with self.subTest(header=header), self.assertRaises(ValueError):
                check_message(header + raw, binding)
        with self.assertRaises(ValueError):
            check_message(raw, {**binding, "recipient": "other@example.test"})

    def test_gmail_adapter_exact_send_and_positive_reconciliation(self):
        import base64
        self.send()
        raw = self.mailbox.messages[0]
        binding = self.rpc("recipe_delivery", action="sender")["binding"]
        service = mock.MagicMock()
        service.users().getProfile().execute.return_value = {"emailAddress": binding["sender"]}
        service.users().messages().send().execute.return_value = {"id": "gmail1"}
        transport = GmailTransport(service, ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"])
        self.assertEqual("accepted", transport.send(raw, binding)["outcome"])
        service.users().messages().send().execute.assert_called_once_with(num_retries=0)
        service.users().messages().list().execute.return_value = {"messages": [{"id": "gmail1"}]}
        service.users().messages().get().execute.return_value = {"labelIds": ["SENT"], "raw": base64.urlsafe_b64encode(raw).decode()}
        self.assertEqual("accepted", transport.reconcile(raw, binding)["outcome"])
        service.users().messages().list().execute.return_value = {}
        self.assertEqual("unknown", transport.reconcile(raw, binding)["outcome"])
        with self.assertRaises(ValueError):
            GmailTransport(service, []).inspect()

    def test_real_smtp_second_transport(self):
        with socketserver.TCPServer(("127.0.0.1", 0), SMTPHandler) as sink:
            sink.messages = []
            worker = threading.Thread(target=sink.serve_forever, daemon=True)
            worker.start()
            try:
                config = deepcopy(self.config)
                config["connections"] = [{"id": "smtp", "type": "smtp", "host": "127.0.0.1", "port": sink.server_address[1],
                                          "tls": "none", "sender": "sender@example.test", "unattended": True}]
                runner = EmailSender(self.rpc, config)
                runner.execute({"action": "configure", "recipient": "recipient@example.test"})
                self.assertTrue(self.send(runner)["sent"])
                self.assertTrue(self.send(runner)["sent"])
                self.assertEqual(1, len(sink.messages))
                self.assertIn(b"application/pdf", sink.messages[0])
            finally:
                sink.shutdown()
                worker.join(timeout=5)

    def test_gmail_rewritten_message_id_requires_exact_frozen_marker_and_content(self):
        import base64
        self.send()
        raw = self.mailbox.messages[0]
        binding = self.rpc("recipe_delivery", action="sender")["binding"]
        original = BytesParser(policy=policy.default).parsebytes(raw)
        marker_name = "X-Meal-Concierge-Delivery-ID"
        marker = str(original[marker_name])
        self.assertEqual(48, len(marker))
        for change in ("none", "missing", "duplicate", "other_marker", "body", "date", "recipient", "content_id", "multipart", "not_sent"):
            with self.subTest(change=change):
                found = BytesParser(policy=policy.default).parsebytes(raw)
                found.replace_header("Message-ID", "<rewritten@mail.gmail.com>")
                if change == "missing":
                    del found[marker_name]
                elif change == "duplicate":
                    found[marker_name] = marker
                elif change == "other_marker":
                    found.replace_header(marker_name, "0" * 48)
                elif change == "body":
                    found.get_payload()[0].get_payload()[0].set_content("Other content")
                elif change == "date":
                    found.replace_header("Date", "Sat, 12 Sep 2026 10:00:00 +0000")
                elif change == "recipient":
                    found.replace_header("To", "other@example.test")
                elif change == "content_id":
                    found.get_payload()[-1]["Content-ID"] = "<changed>"
                elif change == "multipart":
                    found.set_type("multipart/alternative")
                labels = [] if change == "not_sent" else ["SENT"]
                service = mock.MagicMock()
                service.users().getProfile().execute.return_value = {"emailAddress": binding["sender"]}
                service.users().messages().list().execute.side_effect = [{}, {"messages": [{"id": "rewritten"}], "nextPageToken": "more"}]
                metadata = {"labelIds": labels, "payload": {"headers": [{"name": marker_name, "value": v} for v in found.get_all(marker_name, [])]}}
                service.users().messages().get().execute.side_effect = [metadata, {"labelIds": labels, "raw": base64.urlsafe_b64encode(found.as_bytes(policy=policy.SMTP)).decode()}]
                transport = GmailTransport(service, ["https://www.googleapis.com/auth/gmail.modify"])
                self.assertEqual("accepted" if change == "none" else "unknown", transport.reconcile(raw, binding)["outcome"])
                service.users().messages().list.assert_called_with(userId="me", q='in:sent from:"sender@example.test" to:"recipient@example.test"', maxResults=50)
                if change in {"missing", "duplicate", "other_marker", "not_sent"}:
                    self.assertEqual(1, service.users().messages().get().execute.call_count)

    def test_gmail_old_message_without_marker_does_not_scan_unrelated_sent_mail(self):
        self.send()
        message = BytesParser(policy=policy.default).parsebytes(self.mailbox.messages[0])
        del message["X-Meal-Concierge-Delivery-ID"]
        service = mock.MagicMock()
        binding = self.rpc("recipe_delivery", action="sender")["binding"]
        service.users().getProfile().execute.return_value = {"emailAddress": binding["sender"]}
        service.users().messages().list().execute.return_value = {}
        transport = GmailTransport(service, ["https://www.googleapis.com/auth/gmail.modify"])
        self.assertEqual("unknown", transport.reconcile(message.as_bytes(), binding)["outcome"])
        service.users().messages().list().execute.assert_called_once_with(num_retries=0)
    def test_real_mcp_configure_send_then_cli_recovery(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="mc-email-", dir="/tmp") as temporary:
            root = Path(temporary)
            with (root / "service.log").open("w+") as log, socketserver.TCPServer(("127.0.0.1", 0), SMTPHandler) as sink:
                sink.messages = []
                worker = threading.Thread(target=sink.serve_forever, daemon=True)
                worker.start()
                process = subprocess.Popen([sys.executable, "-I", str(source / "tests/test_recipe_delivery.py"), "--serve", str(root)], stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 15
                    while not (root / "s.sock").exists():
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.02)
                    config = {"runtime_id": "native", "receipt_dir": str(root / "receipts"), "connections": [
                        {"id": "smtp", "type": "smtp", "host": "127.0.0.1", "port": sink.server_address[1], "tls": "none", "sender": "sender@example.test"}]}
                    path = root / "email.json"
                    path.write_text(json.dumps(config))
                    env = {**os.environ, "MEAL_CONCIERGE_SOCKET": str(root / "s.sock"), "MEAL_CONCIERGE_EMAIL_CONFIG": str(path)}
                    async def native():
                        from mcp import ClientSession
                        from mcp.client.stdio import StdioServerParameters, stdio_client
                        parameters = StdioServerParameters(command=sys.executable, args=["-I", str(source / "mcp_server.py")], env=env)
                        async with stdio_client(parameters, errlog=log) as (read, write):
                            async with ClientSession(read, write, read_timeout_seconds=30) as client:
                                await client.initialize()
                                configured = await client.call_tool("meal_concierge_email_sender", {"action": "configure", "recipient": "recipient@example.test"})
                                self.assertFalse(configured.is_error, configured.content)
                                rejected = await client.call_tool("meal_concierge_email_sender", {
                                    "action": "send", "request_id": "missing-intent",
                                    "delivery_requested": False,
                                })
                                self.assertTrue(rejected.is_error, rejected.content)
                                self.assertIn("explicit request", rejected.content[0].text)
                                self.assertNotIn("ToolError", rejected.content[0].text)
                                menu_result = await client.call_tool("meal_concierge_menu", {"action": "get"})
                                self.assertFalse(menu_result.is_error, menu_result.content)
                                menu_payload = menu_result.structured_content or json.loads(menu_result.content[0].text)
                                ref = menu_ref(menu_payload["menu"])
                                sent = await client.call_tool("meal_concierge_email_sender", {"action": "send", "request_id": "native-1", "menu_ref": ref, "delivery_requested": True})
                                self.assertFalse(sent.is_error, sent.content)
                                self.assertTrue(sent.structured_content["sent"])
                    asyncio.run(native())
                    result = subprocess.run([sys.executable, "-I", str(source / "cli.py")], env=env, text=True, capture_output=True,
                                            input=json.dumps({"operation": "email_sender", "action": "reconcile", "request_id": "native-1"}), timeout=20)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertTrue(json.loads(result.stdout)["result"]["sent"])
                    self.assertEqual(1, len(sink.messages))
                finally:
                    process.terminate()
                    process.wait(timeout=10)
                    sink.shutdown()
                    worker.join(timeout=5)


class OrderSenderTests(EmailSchedulerTests):
    def runner(self):
        self.mailbox = Mailbox()
        config = {"runtime_id": "order-host", "receipt_dir": str(Path(self.temp.name) / "receipts"),
                  "connections": [{"id": "gmail", "unattended": True}]}
        def rpc(operation, **request):
            return self.app.handle({"operation": operation, **request})
        runner = EmailSender(rpc, config, transport_factory=lambda c: self.mailbox)
        runner.execute({"action": "configure", "recipient": "synthetic@example.test", "timing": "delivery_day"})
        runner.execute({"action": "adopt_order", "provider": "oda", "order_id": "test-order"})
        return runner

    def test_durable_order_path_and_scheduler_gates(self):
        runner = self.runner()
        invocation = self.active()
        request = {"action": "send_order", "provider": "oda", "order_id": "test-order", "scheduler": invocation}
        self.assertTrue(runner.execute(request)["sent"])
        self.assertTrue(runner.execute(request)["sent"])
        self.assertEqual(1, len(self.mailbox.messages))
        self.assertIn(b"application/pdf", self.mailbox.messages[0])
        self.assertEqual("sent", self.store.read()["email_jobs"][0]["status"])

    def test_order_failed_begin_releases_claim_before_retry(self):
        runner = self.runner()
        invocation = self.active()
        original = runner.rpc
        def rpc(operation, **request):
            if request.get("action") == "begin_send":
                raise ConnectionError("failed before begin")
            return original(operation, **request)
        runner.rpc = rpc
        request = {"action": "send_order", "provider": "oda", "order_id": "test-order", "scheduler": invocation}
        with self.assertRaises(ConnectionError):
            runner.execute(request)
        runner.rpc = original
        result = runner.execute({**request, "action": "reconcile_order"})
        self.assertEqual("not_sent", result["outcome"])
        self.assertTrue(result["acknowledged"])
        self.assertTrue(runner.execute({**request, "action": "retry_order", "delivery_requested": True})["sent"])


if __name__ == "__main__":
    unittest.main()
