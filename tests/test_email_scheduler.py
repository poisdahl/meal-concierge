"""Synthetic scheduler adapters through the actual Application; no provider writes."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
import json
import os
import io
import smtplib
import socketserver
from email import policy
from email.parser import BytesParser
from unittest import mock
from PIL import Image

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
from core import HouseholdError, StateStore
from service import Application, Server
from service_common import email_automation_ack
from service_common import menu_email_html

from recipe_assets import RecipeAssets, asset_filename
from recipe_email import build_message


class LocalSMTP(socketserver.StreamRequestHandler):
    """Minimal loopback SMTP receiver; no forwarding and no upstream transport."""
    def handle(self):
        self.wfile.write(b"220 localhost synthetic SMTP\r\n")
        while line := self.rfile.readline():
            verb = line.split(b" ", 1)[0].strip().upper()
            if verb in {b"EHLO", b"HELO", b"MAIL", b"RCPT", b"RSET"}:
                self.wfile.write(b"250 OK\r\n")
            elif verb == b"DATA":
                self.wfile.write(b"354 end with dot\r\n")
                lines = []
                while (body := self.rfile.readline()) not in {b".\r\n", b""}:
                    lines.append(body[1:] if body.startswith(b"..") else body)
                self.server.messages.append(b"".join(lines))
                self.wfile.write(b"250 queued synthetic-receipt-1\r\n")
            elif verb == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"500 unsupported\r\n")


class SyntheticProvider:
    def __init__(self):
        self.delivery = date.today().isoformat()
        self.reads = []

    def probe(self, **kwargs):
        return {"server": {"name": "synthetic-oda"}, "tool_count": 2}

    def call(self, tool, arguments, **kwargs):
        self.reads.append(tool)
        if tool == "order_tracking":
            return {"order_id": arguments["order_number"], "status": "delivered"}
        if tool == "get_order":
            return {"order_number": arguments["order_number"], "delivery_date": self.delivery}
        raise AssertionError(f"unexpected provider effect: {tool}")


class EmailSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = {"provider": "oda", "household": "Synthetic scheduler"}
        self.store = StateStore(self.temp.name, self.settings)
        self.provider = SyntheticProvider()
        self.app = Application(self.store, self.provider, None)
        with self.store.locked() as state:
            state["email_recipient"] = "synthetic@example.test"
            state["menu"] = {"order_id": "test-order", "week": "2026-W36", "dishes": [{
                "name": "Frozen soup", "ingredients": ["2 carrots"], "steps": ["Boil."],
                "source": {"publisher": "Synthetic source", "relationship": "original"},
                "rights": {"credit": "Frozen credit"},
            }]}
        self.scheduled = self.call("schedule", delivery_date=self.provider.delivery)
        self.binding = {"platform": "synthetic-timer", "scope": "private-fixture", "job_id": "job-1"}

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, **kwargs):
        return self.app.handle({"operation": "email", "action": action,
                                "provider": "oda", "order_id": "test-order", **kwargs})

    def plan(self, **kwargs):
        return self.call("scheduler_plan", scheduler={"binding": self.binding,
            "inventory": {"platform": "synthetic-timer", "scope": "private-fixture", "verified": True, "matching_jobs": 0}, **kwargs})

    def ack(self, plan, **kwargs):
        return self.call("ack_scheduler", automation_digest=plan["automation_digest"], scheduler={
            "binding": plan["scheduler"]["binding"], "generation": plan["scheduler"]["generation"],
            "state": "active", "verified": True, "previous_binding": plan["scheduler"].get("previous_binding"), **kwargs})

    def active(self):
        plan = self.plan()
        self.ack(plan)
        return plan["invocation"]

    def begin(self, invocation, **kwargs):
        claim = self.call("due", scheduler=invocation, **kwargs)
        return self.call("begin_send", scheduler=invocation, claim_token=claim["claim_token"], **kwargs)

    def cover_schedule(self):
        assets = RecipeAssets(Path(self.temp.name) / "recipe-assets")
        output = io.BytesIO()
        with Image.new("RGB", (40, 20), "red") as image:
            image.save(output, "PNG")
        asset_id = assets.import_bytes(output.getvalue())
        with self.store.locked() as state:
            state["menu"]["order_id"] = "cover-order"
            state["menu"]["dishes"][0]["image"] = {
                "asset_id": asset_id, "alt": "Frozen <soup>", "creator": "Frozen photographer",
                "credit": "Independent image credit", "license": "Synthetic fixture only",
                "source_url": "https://example.invalid/frozen-cover", "changes": "Test crop",
            }
        self.call("schedule", order_id="cover-order", delivery_date=self.provider.delivery)
        plan = self.call("scheduler_plan", order_id="cover-order", scheduler={
            "binding": {**self.binding, "job_id": "cover-job"},
            "inventory": {"platform": "synthetic-timer", "scope": "private-fixture", "verified": True, "matching_jobs": 0}})
        self.call("ack_scheduler", order_id="cover-order", automation_digest=plan["automation_digest"], scheduler={
            "binding": plan["scheduler"]["binding"], "generation": plan["scheduler"]["generation"],
            "state": "active", "verified": True, "previous_binding": None})
        return assets, asset_id, plan["invocation"]

    def smtp_payload(self, payload, assets):
        message = build_message(payload, assets, sender="local-sender@example.test")
        with socketserver.TCPServer(("127.0.0.1", 0), LocalSMTP) as receiver:
            receiver.messages = []
            thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            thread.start()
            try:
                with smtplib.SMTP(*receiver.server_address, timeout=5) as smtp:
                    smtp.send_message(message)
                self.assertEqual(len(receiver.messages), 1)
                return BytesParser(policy=policy.default).parsebytes(receiver.messages[0])
            finally:
                receiver.shutdown()
                thread.join(5)

    def assert_frozen_credits(self, message):
        for subtype in ("plain", "html"):
            body = message.get_body(preferencelist=(subtype,)).get_content()
            for text in ("Frozen soup", "Frozen credit", "Frozen photographer", "Independent image credit", "Synthetic fixture only"):
                self.assertIn(text, body)
            self.assertNotIn("Later recipe", body)

    def test_cover_dispatch_resolves_frozen_cid_bytes_and_credits(self):
        assets, asset_id, invocation = self.cover_schedule()
        original = assets.read(asset_id)
        with self.store.locked() as state:
            state["menu"]["dishes"][0]["name"] = "Later recipe"
            state["menu"]["dishes"][0]["image"] = None
        payload = self.begin(invocation, order_id="cover-order", images_supported=True)
        self.assertEqual(payload["inline_images"][0]["asset_id"], asset_id)
        self.assertNotIn("cid:", payload["html_without_images"])
        self.assertNotIn(str(assets.root), json.dumps(payload))
        self.assertIn('alt="Frozen &lt;soup&gt;"', payload["html"])
        message = self.smtp_payload(payload, assets)
        self.assert_frozen_credits(message)
        parts = [part for part in message.walk() if part.get_content_type() == "image/jpeg"]
        self.assertEqual(len(parts), 1)
        descriptor = payload["inline_images"][0]
        self.assertEqual(parts[0]["Content-ID"], "<" + descriptor["content_id"] + ">")
        self.assertEqual(parts[0].get_payload(decode=True), original)
        self.call("mark_sent", order_id="cover-order", claim_token=payload["claim_token"])

    def test_cover_missing_or_corrupt_at_sender_uses_frozen_readable_fallback(self):
        assets, asset_id, invocation = self.cover_schedule()
        payload = self.begin(invocation, order_id="cover-order", images_supported=True)
        leaf = assets.root / asset_filename(asset_id)
        leaf.unlink()
        for corrupted in (False, True):
            with self.subTest(corrupted=corrupted):
                if corrupted:
                    leaf.write_bytes(b"synthetic-corrupt-cover")
                # Construct locally twice to inspect two synthetic failures;
                # only the positive case above actually dispatches via SMTP.
                message = BytesParser(policy=policy.default).parsebytes(build_message(payload, assets).as_bytes())
                self.assert_frozen_credits(message)
                self.assertNotIn("cid:", message.get_body(preferencelist=("html",)).get_content())
                self.assertFalse([part for part in message.walk() if part.get_content_type().startswith("image/")])

    def test_optional_cover_absent_at_prepare_and_unsupported_destination(self):
        assets, asset_id, _ = self.cover_schedule()
        before = self.store.read()["email_jobs"]
        with mock.patch.object(RecipeAssets, "read", side_effect=AssertionError("unsupported images must not access assets")):
            unsupported = self.call("test", order_id="cover-order")
        self.assertFalse(unsupported["inline_images"])
        self.assertNotIn("html_without_images", unsupported)
        self.assertIn("Independent image credit", unsupported["html"])
        (assets.root / asset_filename(asset_id)).unlink()
        absent = self.call("test", order_id="cover-order", images_supported=True)
        self.assertFalse(absent["inline_images"])
        self.assertEqual(absent["image_warnings"][0]["reason"], "cover_missing_or_invalid")
        self.assertIn("Frozen photographer", absent["html"])
        self.assertIn("TEST:", absent["html"])
        self.assertEqual(before, self.store.read()["email_jobs"])

    def test_oversized_optional_cover_payload_falls_back_before_dispatch(self):
        assets, _, invocation = self.cover_schedule()
        preview = self.call("test", order_id="cover-order", images_supported=True)
        plain_size = len(json.dumps({"html": preview["html_without_images"]}, ensure_ascii=True).encode())
        with mock.patch("email_operations.MAX_REQUEST", plain_size + 2500):
            payload = self.begin(invocation, order_id="cover-order", images_supported=True)
        self.assertFalse(payload["inline_images"])
        self.assertNotIn("html_without_images", payload)
        self.assertIn({"reason": "optional_image_payload_exceeds_transport"}, payload["image_warnings"])
        self.assert_frozen_credits(BytesParser(policy=policy.default).parsebytes(build_message(payload, assets).as_bytes()))

    def test_queued_pre_renderer_html_gains_frozen_image_credit_in_both_alternatives(self):
        assets, asset_id, invocation = self.cover_schedule()
        with self.store.locked() as state:
            job = next(job for job in state["email_jobs"] if job["order_id"] == "cover-order")
            legacy = deepcopy(job["menu_snapshot"])
            legacy["dishes"][0].pop("image")
            job["html"] = menu_email_html(legacy)
            state["menu"]["dishes"][0]["name"] = "Later recipe"
        payload = self.begin(invocation, order_id="cover-order", images_supported=True)
        self.assertIn("Frozen photographer", payload["html_without_images"])
        self.assert_frozen_credits(BytesParser(policy=policy.default).parsebytes(build_message(payload, assets).as_bytes()))
        (assets.root / asset_filename(asset_id)).unlink()
        self.assert_frozen_credits(BytesParser(policy=policy.default).parsebytes(build_message(payload, assets).as_bytes()))

    def test_optional_test_images_fit_actual_socket_transport(self):
        _, _, _ = self.cover_schedule()
        preview = self.call("test", order_id="cover-order", images_supported=True)
        plain_size = len(json.dumps({"html": preview["html_without_images"]}, ensure_ascii=True).encode())
        limit = plain_size + 2500
        before = self.store.read()["email_jobs"]
        server = Server(Path(self.temp.name) / "unused.sock", os.getgid(), os.getuid(), self.app)
        client, accepted = socket.socketpair()
        with mock.patch("email_operations.MAX_REQUEST", limit), mock.patch("service.MAX_REQUEST", limit):
            thread = threading.Thread(target=server._serve, args=(accepted,))
            thread.start()
            with client:
                client.sendall((json.dumps({"operation": "email", "action": "test", "provider": "oda",
                    "order_id": "cover-order", "images_supported": True, "contract": 1}) + "\n").encode())
                with client.makefile("rb") as stream:
                    wire = stream.readline()
            thread.join(5)
        response = json.loads(wire)
        self.assertTrue(response["ok"])
        self.assertLessEqual(len(wire), limit)
        self.assertFalse(response["result"]["inline_images"])
        self.assertIn("Frozen photographer", response["result"]["html"])
        self.assertEqual(before, self.store.read()["email_jobs"])

    def test_unsupported_image_warnings_cannot_block_otherwise_fitting_text(self):
        self.cover_schedule()
        preview = self.call("test", order_id="cover-order")
        required = {key: value for key, value in preview.items() if key not in {"inline_images", "image_warnings"}}
        limit = len(json.dumps({"ok": True, "result": required}, ensure_ascii=True).encode()) + 1024
        with mock.patch("email_operations.MAX_REQUEST", limit):
            payload = self.call("test", order_id="cover-order")
        self.assertNotIn("image_warnings", payload)
        self.assertNotIn("inline_images", payload)
        self.assertIn("Independent image credit", payload["html"])

    def test_exact_identity_and_lost_registration_ack_survive_reopen(self):
        plan = self.plan()
        repeated = self.plan()
        self.assertEqual(plan, repeated)
        self.ack(plan)
        self.app = Application(StateStore(self.temp.name, self.settings), self.provider, None)
        self.assertTrue(self.ack(plan)["idempotent"])
        for changed in ({}, {**plan["invocation"], "generation": "stale"},
                        {**plan["invocation"], "binding": {**self.binding, "scope": "another-household"}},
                        {**plan["invocation"], "occurrence_id": "another-order"}):
            with self.assertRaisesRegex(HouseholdError, "scheduler"):
                self.call("due", scheduler=changed)
        self.assertEqual(self.provider.reads, [])
        self.assertTrue(self.call("due", scheduler=plan["invocation"])["claim"])

    def test_unverified_inventory_and_pending_adoption_cannot_use_legacy(self):
        with self.assertRaisesRegex(HouseholdError, "inventory"):
            self.call("scheduler_plan", scheduler={"binding": self.binding})
        self.plan()
        with self.assertRaisesRegex(HouseholdError, "ack_scheduler"):
            self.call("ack_automation", **{k: v for k, v in self.scheduled["automation_ack"].items() if k not in {"action", "provider", "order_id"}})
        with self.assertRaisesRegex(HouseholdError, "paused"):
            self.call("due")
        self.assertEqual(self.provider.reads, [])

    def test_pause_revokes_old_claim_and_stale_ack_cannot_reactivate(self):
        plan = self.plan()
        self.ack(plan)
        claim = self.call("due", scheduler=plan["invocation"])
        paused = self.call("pause_scheduler", scheduler=plan["invocation"])
        with self.assertRaisesRegex(HouseholdError, "claim_token"):
            self.call("begin_send", scheduler=plan["invocation"], claim_token=claim["claim_token"])
        with self.assertRaisesRegex(HouseholdError, "stale"):
            self.ack(plan)
        self.ack(paused)
        self.assertTrue(self.call("due", scheduler=paused["invocation"])["claim"])

    def test_handover_requires_exact_removed_old_job_and_preserves_occurrence(self):
        old = self.active()
        self.binding = {"platform": "new-native-timer", "scope": "other-private-scope", "job_id": "new-job"}
        plan = self.plan(generation=old["generation"])
        self.assertEqual(plan["scheduler"]["previous_binding"], old["binding"])
        with self.assertRaisesRegex(HouseholdError, "old removal"):
            self.ack(plan)
        with self.assertRaisesRegex(HouseholdError, "old removal"):
            self.ack(plan, previous_binding={**old["binding"], "scope": "wrong"}, previous_job_removed=True)
        paused = self.call("pause_scheduler", scheduler=plan["invocation"])
        replanned = self.plan(generation=paused["invocation"]["generation"])
        self.assertEqual(replanned["scheduler"]["previous_binding"], old["binding"])
        self.ack(replanned, previous_job_removed=True)
        with self.assertRaisesRegex(HouseholdError, "scheduler"):
            self.call("due", scheduler=old)
        self.assertEqual(replanned["invocation"]["occurrence_id"], old["occurrence_id"])

    def test_handover_ack_binds_target_and_digest(self):
        plan = self.plan()
        with self.assertRaisesRegex(HouseholdError, "exact plan"):
            self.ack(plan, binding={**self.binding, "job_id": "different"})
        with self.assertRaisesRegex(HouseholdError, "exact plan"):
            self.ack({**plan, "automation_digest": "wrong"})
        self.ack(plan)
        with self.assertRaisesRegex(HouseholdError, "differs"):
            self.ack(plan, state="paused")

    def test_native_job_cannot_be_reassigned_over_another_followup(self):
        invocation = self.active()
        with self.store.locked() as state:
            state["menu"] = {**state["menu"], "order_id": "another-order"}
        self.app.handle({"operation": "email", "action": "schedule", "provider": "oda",
                         "order_id": "another-order", "delivery_date": self.provider.delivery})
        with self.assertRaisesRegex(HouseholdError, "another email follow-up"):
            self.app.handle({"operation": "email", "action": "scheduler_plan", "provider": "oda",
                             "order_id": "another-order", "scheduler": {"binding": invocation["binding"],
                             "inventory": {"platform": "synthetic-timer", "scope": "private-fixture", "verified": True, "matching_jobs": 0}}})
        with self.assertRaisesRegex(HouseholdError, "another email follow-up"):
            self.app.handle({"operation": "email", "action": "scheduler_plan", "provider": "oda",
                             "order_id": "another-order", "scheduler": {
                             "binding": {**self.binding, "job_id": "job-2"}, "previous_binding": invocation["binding"],
                             "inventory": {"platform": "synthetic-timer", "scope": "private-fixture", "verified": True, "matching_jobs": 1}}})
        self.call("cancel_followup", owner_confirmed_cancelled=True)
        with self.assertRaisesRegex(HouseholdError, "another email follow-up"):
            self.app.handle({"operation": "email", "action": "scheduler_plan", "provider": "oda",
                             "order_id": "another-order", "scheduler": {"binding": invocation["binding"],
                             "inventory": {"platform": "synthetic-timer", "scope": "private-fixture", "verified": True, "matching_jobs": 0}}})

    def test_cancel_cleanup_does_not_remove_a_previously_retired_binding(self):
        invocation = self.active()
        self.binding = {**self.binding, "job_id": "replacement"}
        plan = self.plan(generation=invocation["generation"])
        self.ack(plan, previous_job_removed=True)
        self.call("pause_scheduler", scheduler=plan["invocation"])
        cancelled = self.call("cancel_followup", owner_confirmed_cancelled=True)
        self.assertIsNone(cancelled["automation_cleanup"]["scheduler"]["previous_binding"])
        self.assertIsNone(self.call("automation_plan")["removals"][0]["scheduler"]["previous_binding"])

    def test_lost_dispatch_and_pause_keep_uncertainty_locked(self):
        invocation = self.active()
        payload = self.begin(invocation)
        token = payload["claim_token"]
        self.call("pause_scheduler", scheduler=invocation)
        with self.assertRaisesRegex(HouseholdError, "reconciliation"):
            self.plan()
        with self.assertRaisesRegex(HouseholdError, "uncertainty"):
            self.call("release", claim_token=token)
        before = self.store.read()
        self.assertEqual(self.call("reconcile_send", claim_token=token, send_outcome="unknown"),
                         {"resolved": False, "status": "sending", "retry_allowed": False})
        self.assertEqual(before, self.store.read())
        with self.assertRaisesRegex(HouseholdError, "sender_receipt"):
            self.call("reconcile_send", claim_token=token, send_outcome="not_sent")
        self.call("reconcile_send", claim_token=token, send_outcome="sent", sender_receipt="local-sender:receipt-1")
        self.assertTrue(self.call("mark_sent", claim_token=token)["idempotent"])
        with self.assertRaisesRegex(HouseholdError, "claim_token"):
            self.call("mark_sent", claim_token="wrong")
        self.assertEqual(self.store.read()["email_jobs"][0]["status"], "sent")

    def test_definite_not_sent_can_retry_same_occurrence_with_new_attempt(self):
        invocation = self.active()
        first = self.begin(invocation)
        self.call("reconcile_send", claim_token=first["claim_token"], send_outcome="not_sent", sender_receipt="local-sender:rejected-before-data")
        second = self.begin(invocation)
        self.assertEqual(first["occurrence_id"], second["occurrence_id"])
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        with self.assertRaisesRegex(HouseholdError, "original dispatch"):
            self.call("reconcile_send", claim_token=first["claim_token"], send_outcome="sent", sender_receipt="stale")
        self.call("mark_sent", claim_token=second["claim_token"])
        self.assertNotIn("sender_receipt", self.store.read()["email_jobs"][0])

    def test_delivery_change_invalidates_old_ack_without_changing_occurrence(self):
        plan = self.plan()
        self.call("schedule", delivery_date="2026-12-01")
        with self.assertRaisesRegex(HouseholdError, "exact plan"):
            self.ack(plan)
        replacement = self.plan()
        self.assertNotEqual(replacement["invocation"]["generation"], plan["invocation"]["generation"])
        self.assertEqual(replacement["invocation"]["occurrence_id"], plan["invocation"]["occurrence_id"])
        self.ack(replacement)

    def test_active_delivery_changes_cannot_use_legacy_ack(self):
        for moved_by_provider in (False, True):
            with self.subTest(moved_by_provider=moved_by_provider):
                # Keep each case independent without resetting any outcome journal.
                if moved_by_provider:
                    self.tearDown()
                    self.setUp()
                invocation = self.active()
                if moved_by_provider:
                    self.provider.delivery = "2026-12-01"
                    result = self.call("due", scheduler=invocation)
                    self.assertEqual(result["reason"], "delivery moved")
                else:
                    result = self.call("schedule", delivery_date="2026-12-01")
                self.assertTrue(result["scheduler_update_required"])
                self.assertNotIn("automation_ack", result)
                discovery = self.call("automation_plan")
                self.assertEqual(discovery["updates"], [])
                self.assertEqual(len(discovery["scheduler_updates"]), 1)
                self.assertEqual(discovery["scheduler_updates"][0]["scheduler"], invocation)
                self.assertNotIn("cron_prompt", discovery["scheduler_updates"][0])
                legacy = email_automation_ack("oda", "test-order", "2026-12-01", invocation["occurrence_id"])
                with self.assertRaisesRegex(HouseholdError, "ack_scheduler"):
                    self.app.handle({"operation": "email", **legacy})
                with self.assertRaisesRegex(HouseholdError, "scheduler_plan"):
                    self.call("due", scheduler=invocation)
                replacement = self.plan(generation=invocation["generation"])
                self.ack(replacement)
                self.assertEqual(replacement["invocation"]["occurrence_id"], invocation["occurrence_id"])

    def test_provider_move_to_today_requires_new_verified_native_plan(self):
        self.call("schedule", delivery_date="2026-12-01")
        invocation = self.active()
        result = self.call("due", scheduler=invocation)
        self.assertFalse(result["send"])
        self.assertTrue(result["scheduler_update_required"])
        self.assertEqual(result["delivery_date"], self.provider.delivery)
        self.assertEqual(self.store.read()["email_jobs"][0]["status"], "pending")
        replacement = self.plan(generation=invocation["generation"])
        self.ack(replacement)
        self.assertTrue(self.call("due", scheduler=replacement["invocation"])["claim"])

    def test_frozen_recipe_and_credit_through_real_socket_dispatch(self):
        invocation = self.active()
        with self.store.locked() as state:
            state["menu"]["dishes"][0]["name"] = "Later soup"
            state["email_recipient"] = "later@example.test"
        claim = self.call("due", scheduler=invocation)
        server = Server(Path(self.temp.name) / "unused.sock", os.getgid(), os.getuid(), self.app)
        client, accepted = socket.socketpair()
        thread = threading.Thread(target=server._serve, args=(accepted,))
        thread.start()
        with client:
            client.sendall((json.dumps({"operation": "email", "action": "begin_send", "provider": "oda",
                "order_id": "test-order", "claim_token": claim["claim_token"], "scheduler": invocation, "contract": 1}) + "\n").encode())
            with client.makefile("rb") as stream:
                response = json.loads(stream.readline())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(response["ok"])
        payload = response["result"]
        self.assertEqual(payload["recipient"], "synthetic@example.test")
        self.assertIn("Frozen soup", payload["html"])
        self.assertIn("Frozen credit", payload["html"])
        self.assertNotIn("Later soup", payload["html"])

    def test_actual_payload_mime_local_smtp_and_lost_send_ack(self):
        invocation = self.active()
        with self.store.locked() as state:
            state["menu"]["dishes"][0]["name"] = "Later recipe"
        payload = self.begin(invocation)
        message = build_message(payload, RecipeAssets(Path(self.temp.name) / "recipe-assets"), sender="local-sender@example.test")
        with socketserver.TCPServer(("127.0.0.1", 0), LocalSMTP) as receiver:
            receiver.messages = []
            thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            thread.start()
            try:
                with smtplib.SMTP(*receiver.server_address, timeout=5) as smtp:
                    smtp.send_message(message)
                # The process loses its success acknowledgment after SMTP
                # acceptance. Reopening actual state must never resend.
                self.app = Application(StateStore(self.temp.name, self.settings), self.provider, None)
                retry = self.call("due", scheduler=invocation)
                self.assertFalse(retry["send"])
                self.assertIn("reconciliation", retry["reason"])
                with self.assertRaisesRegex(HouseholdError, "claimed job"):
                    self.call("begin_send", scheduler=invocation, claim_token=payload["claim_token"])
                self.assertEqual(len(receiver.messages), 1)
                received = BytesParser(policy=policy.default).parsebytes(receiver.messages[0])
                self.assertEqual(received["To"], "synthetic@example.test")
                for subtype in ("plain", "html"):
                    body = received.get_body(preferencelist=(subtype,)).get_content()
                    self.assertIn("Frozen soup", body)
                    self.assertIn("Frozen credit", body)
                    self.assertNotIn("Later recipe", body)
                self.call("reconcile_send", claim_token=payload["claim_token"], send_outcome="sent",
                          sender_receipt="loopback-smtp:synthetic-receipt-1")
                self.assertTrue(self.call("mark_sent", claim_token=payload["claim_token"])["idempotent"])
            finally:
                receiver.shutdown()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()
