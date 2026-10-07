"""Observed native checkout money must agree without inferred rows."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError
from clients import muse
from muse_browser import (ACTION_OPERATIONS, DELEGATION_OPERATIONS, MuseBrowser,
                          NativeBridge, checkout_amounts, claim_request,
                          consume_request, digest, durable_publish, end_request,
                          original_checkout_completed, process_start, read_json,
                          request_record, respond_request)
from service import Server
import test_meal_concierge as core_fixtures


ROWS = [
    {"label": "34 varer", "value": "1 296,80 kr"},
    {"label": "Du sparer", "value": "−29,90 kr"},
    {"label": "Delsum", "value": "1 266,90 kr"},
    {"label": "Leveringsemballasje", "value": "11,70 kr"},
    {"label": "Total inkl. MVA", "value": "1 278,60 kr"},
]


class NativeAmountTests(unittest.TestCase):
    def test_complete_observed_bill_and_absent_fees(self):
        result = checkout_amounts(ROWS, 34, 127860)
        self.assertEqual(result["provider_total"], 1278.60)
        self.assertEqual(result["discounts"], -29.90)
        self.assertIsNone(result["delivery_price"])
        self.assertIsNone(result["other_fees"])

    def test_delivery_and_small_order_fee_belong_to_payable(self):
        rows = deepcopy(ROWS)
        rows.extend([{"label": "Levering", "value": "29,00 kr"},
                     {"label": "Tillegg for mindre bestilling", "value": "15,00 kr"}])
        rows[4]["value"] = "1 322,60 kr"
        result = checkout_amounts(rows, 34, 132260)
        self.assertEqual(result["other_fees"], {"Tillegg for mindre bestilling": 15.0})
        self.assertEqual(result["delivery_price"], 29.0)

    def test_native_compact_currency_and_one_unlabeled_zero_preserve_fee_unknown(self):
        rows = [{"label": "34 varer", "value": "1309,10kr"},
                {"label": "Du sparer", "value": "−29,90kr"},
                {"label": "Delsum", "value": "1279,20kr"},
                {"label": "Leveringsemballasje", "value": "30,15kr"},
                {"label": "", "value": "0,00kr"},
                {"label": "Total inkl. MVA", "value": "1309,35kr"}]
        result = checkout_amounts(rows, 34, 130935)
        self.assertEqual(result["provider_total"], 1309.35)
        self.assertEqual(result["bags"], 30.15)
        self.assertIsNone(result["delivery_price"])
        for extra in ({"label": None, "value": "1,00kr"},
                      {"label": "", "value": "0,00kr"},
                      {"label": "Unknown charge", "value": "0,00kr"}):
            with self.subTest(extra=extra), self.assertRaises(HouseholdError):
                checkout_amounts(rows + [extra], 34, 130935)

    def test_ambiguous_missing_and_unknown_rows_are_not_filled(self):
        cases = [ROWS + [ROWS[4]], ROWS[:2] + ROWS[3:],
                 ROWS + [{"label": "Extra charge", "value": "10,00 kr"}]]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(HouseholdError):
                checkout_amounts(rows, 34, 127860)

    def test_wrong_product_count_subtotal_total_and_sign_stop_review(self):
        for index, value in [(0, "1 300,00 kr"), (1, "29,90 kr"),
                             (2, "1 270,00 kr"), (4, "1 279,60 kr")]:
            rows = deepcopy(ROWS)
            rows[index]["value"] = value
            with self.subTest(index=index), self.assertRaises(HouseholdError):
                checkout_amounts(rows, 34, 127860)
        with self.assertRaises(HouseholdError):
            checkout_amounts(ROWS, 33, 127860)
        with self.assertRaises(HouseholdError):
            checkout_amounts(ROWS, 34, 127870)


def wait_for(condition):
    cutoff = time.monotonic() + 5
    while time.monotonic() < cutoff:
        result = condition()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("synthetic native host did not reach its bounded condition")


@unittest.skipUnless(sys.platform.startswith("linux"), "Muse cloud process identity is Linux")
class NativeFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mb-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        os.chmod(self.directory, 0o700)
        for name in ("requests", "claims", "consumed", "responses", "endings", "closed"):
            (self.directory / name).mkdir(mode=0o700)
        self.bridge = NativeBridge(self.directory, "original-task")

    def record(self, operation="checkout_click", **changes):
        now = datetime.now(timezone.utc)
        result = {"version": 1, "request_id": str(uuid.uuid4()), "provider": "oda",
                  "operation": operation, "task_id": "original-task", "owner_pid": os.getpid(),
                  "owner_start": process_start(os.getpid()), "issued_at": now.isoformat(),
                  "expires_at": (now + timedelta(seconds=30)).isoformat(), "payload": {}}
        result.update(changes)
        durable_publish(self.directory / "requests" / (result["request_id"] + ".json"), result)
        return result

    def response(self, record, state="completed", facts=None):
        return {"request_id": record["request_id"], "request_digest": digest(record),
                "task_id": record["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
                "task_state": state, "facts": facts or {}}


class NativeBrokerTests(NativeFixture):
    def test_ending_and_response_are_serialized_terminal_alternatives(self):
        record = self.record("checkout_review")
        key = record["request_id"]
        claim_request(self.directory, key, "original-task")
        barrier = threading.Barrier(2)
        outcomes = []
        def terminal(function, name):
            barrier.wait(2)
            try:
                function(self.directory, key, "original-task", self.response(record))
                outcomes.append(name)
            except HouseholdError:
                outcomes.append("refused")
        ending = threading.Thread(target=terminal, args=(end_request, "ending"))
        response = threading.Thread(target=terminal, args=(respond_request, "response"))
        ending.start()
        response.start()
        ending.join(3)
        response.join(3)
        self.assertFalse(ending.is_alive())
        self.assertFalse(response.is_alive())
        self.assertEqual(outcomes.count("refused"), 1)
        self.assertEqual(sum((self.directory / kind / (key + ".json")).exists()
                             for kind in ("responses", "endings")), 1)

    def test_symlinked_ending_also_prevents_new_response(self):
        record = self.record("checkout_review")
        key = record["request_id"]
        claim_request(self.directory, key, "original-task")
        (self.directory / "endings" / (key + ".json")).symlink_to(self.directory / "missing")
        with self.assertRaises(HouseholdError):
            respond_request(self.directory, key, "original-task", self.response(record))
        self.assertFalse((self.directory / "responses" / (key + ".json")).exists())

    def test_long_read_stays_live_but_overlong_action_is_rejected(self):
        issued = datetime.now(timezone.utc) - timedelta(seconds=181)
        record = self.record("checkout_review", issued_at=issued.isoformat(),
            expires_at=(issued + timedelta(seconds=540)).isoformat())
        claim_request(self.directory, record["request_id"], "original-task")
        respond_request(self.directory, record["request_id"], "original-task", self.response(record))
        for operation, seconds in [("checkout_review", 541), *[(op, 31) for op in ACTION_OPERATIONS]]:
            with self.subTest(operation=operation):
                now = datetime.now(timezone.utc)
                record = self.record(operation, issued_at=now.isoformat(),
                    expires_at=(now + timedelta(seconds=seconds)).isoformat())
                with self.assertRaises(HouseholdError):
                    claim_request(self.directory, record["request_id"], "original-task")

    def test_read_window_is_capped_by_remaining_core_deadline(self):
        for remaining, expected in [(900, 540), (470, 470), (15, 15)]:
            with self.subTest(remaining=remaining):
                prior = set((self.directory / "requests").glob("*.json"))
                results = []
                def waiter():
                    try:
                        with self.bridge.custody():
                            results.append(self.bridge.request("checkout_review", {},
                                deadline=time.monotonic() + remaining))
                    except Exception as exc:
                        results.append(exc)
                thread = threading.Thread(target=waiter)
                thread.start()
                path = wait_for(lambda: next((p for p in (self.directory / "requests").glob("*.json")
                                             if p not in prior), None))
                record = read_json(path)
                duration = (datetime.fromisoformat(record["expires_at"]) - datetime.fromisoformat(record["issued_at"])).total_seconds()
                claim_request(self.directory, record["request_id"], "original-task")
                respond_request(self.directory, record["request_id"], "original-task", self.response(record))
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, [{}])
                self.assertAlmostEqual(duration, expected, delta=0.1)

    def test_effect_permit_cannot_extend_original_confirmation_expiry(self):
        original_expiry = datetime.now(timezone.utc) + timedelta(seconds=2)
        errors = []
        def waiter():
            try:
                with self.bridge.custody():
                    self.bridge.request("checkout_click", {"journal_binding": {
                        "confirmation_id": "original-confirmation", "expires_at": original_expiry.isoformat()}},
                        expires_at=original_expiry.isoformat())
            except HouseholdError as exc:
                errors.append(exc)
        thread = threading.Thread(target=waiter)
        thread.start()
        path = wait_for(lambda: next((self.directory / "requests").glob("*.json"), None))
        record = read_json(path)
        self.assertLessEqual(datetime.fromisoformat(record["expires_at"]), original_expiry)
        claim_request(self.directory, record["request_id"], "original-task")
        time.sleep(max(0, (original_expiry - datetime.now(timezone.utc)).total_seconds()) + 0.05)
        with self.assertRaises(HouseholdError):
            consume_request(self.directory, record["request_id"], "original-task")
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertFalse(list((self.directory / "consumed").glob("*.json")))
        end_request(self.directory, record["request_id"], "original-task",
                    self.response(record, facts={"dispatch": "not_dispatched"}))
        with self.bridge.custody():
            pass

    def test_unconsumed_refusal_closes_custody_and_revokes_live_permit(self):
        record = self.record()
        key = record["request_id"]
        claim_request(self.directory, key, "original-task")
        with self.assertRaises(HouseholdError):
            respond_request(self.directory, key, "original-task",
                            self.response(record, facts={"dispatch": "clicked_once"}))
        respond_request(self.directory, key, "original-task",
                        self.response(record, facts={"dispatch": "not_dispatched"}))
        with self.assertRaises(HouseholdError):
            consume_request(self.directory, key, "original-task")
        self.assertFalse(list((self.directory / "consumed").glob("*.json")))
        with self.bridge.custody():
            pass

    def test_consumed_action_cannot_claim_pre_dispatch_refusal(self):
        record = self.record()
        key = record["request_id"]
        claim_request(self.directory, key, "original-task")
        consume_request(self.directory, key, "original-task")
        refusal = self.response(record, facts={"dispatch": "not_dispatched"})
        for publish in (respond_request, end_request):
            with self.subTest(publish=publish.__name__), self.assertRaises(HouseholdError):
                publish(self.directory, key, "original-task", refusal)
        with self.assertRaises(HouseholdError), self.bridge.custody():
            pass
        respond_request(self.directory, key, "original-task", self.response(record, facts={"dispatch": "clicked_once"}))
        with self.bridge.custody():
            pass

    def test_duplicate_consume_and_wrong_owner_identity_refuse(self):
        record = self.record()
        key = record["request_id"]
        claim_request(self.directory, key, "original-task")
        consume_request(self.directory, key, "original-task")
        with self.assertRaises(HouseholdError):
            consume_request(self.directory, key, "original-task")
        for changes in ({"version": True}, {"operation": []}, {"owner_pid": True},
                        {"owner_start": "0"}, {"payload": None},
                        {"expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}):
            bad = self.record(**changes)
            with self.subTest(changes=changes), self.assertRaises(HouseholdError):
                claim_request(self.directory, bad["request_id"], "original-task")
        with self.assertRaises(HouseholdError):
            claim_request(self.directory, key, "wrong-task")
        with self.assertRaises(HouseholdError):
            request_record(self.directory, "../not-a-uuid", "original-task")

    def test_waiting_consumed_action_blocks_until_actual_ending(self):
        record = self.record()
        key = record["request_id"]
        claim_request(self.directory, key, "original-task")
        consume_request(self.directory, key, "original-task")
        prior = self.response(record, "waiting_for_information")
        respond_request(self.directory, key, "original-task", prior)
        with self.assertRaises(HouseholdError), self.bridge.custody():
            pass
        end_request(self.directory, key, "original-task", self.response(record))
        self.assertEqual(read_json(self.directory / "responses" / (key + ".json")), prior)
        with self.bridge.custody():
            pass

    def test_read_pause_cannot_be_adopted_by_another_task(self):
        record = self.record("checkout_review")
        key = record["request_id"]
        claim_request(self.directory, key, "original-task")
        respond_request(self.directory, key, "original-task", self.response(record, "waiting_for_information"))
        with self.bridge.custody():
            pass
        with self.assertRaises(HouseholdError), NativeBridge(self.directory, "replacement").custody():
            pass

    def test_early_waiter_failure_revokes_late_producer_with_live_service_pid(self):
        errors = []
        def waiter():
            try:
                with self.bridge.custody():
                    self.bridge.request("checkout_click", {})
            except HouseholdError as exc:
                errors.append(exc)
        thread = threading.Thread(target=waiter)
        thread.start()
        path = wait_for(lambda: next((self.directory / "requests").glob("*.json"), None))
        record = read_json(path)
        # Invalid external producer data ends this specific waiter early.
        bad = self.response(record)
        bad["request_digest"] = "0" * 64
        durable_publish(self.directory / "responses" / path.name, bad)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(process_start(os.getpid()), record["owner_start"])
        with self.assertRaises(HouseholdError):
            claim_request(self.directory, record["request_id"], "original-task")

    def test_killed_waiter_preserves_consumed_custody_until_terminal_receipt(self):
        self._killed_waiter("checkout_click")

    def test_delegation_survives_owner_loss_without_replay_until_actual_ending(self):
        self._killed_waiter("checkout_delegate")

    def _killed_waiter(self, operation):
        root = str(Path(__file__).resolve().parents[1])
        code = "import sys;sys.path.insert(0,sys.argv[1]);from muse_browser import NativeBridge;b=NativeBridge(sys.argv[2],'original-task');\nwith b.custody(): b.request(sys.argv[3],{})"
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", code, root, str(self.directory), operation],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            path = wait_for(lambda: next((self.directory / "requests").glob("*.json"), None))
            record = read_json(path)
            claim_request(self.directory, record["request_id"], "original-task")
            consume_request(self.directory, record["request_id"], "original-task")
            child.terminate()
            child.communicate(timeout=5)
            with self.assertRaises(HouseholdError):
                consume_request(self.directory, record["request_id"], "original-task")
            with self.assertRaises(HouseholdError), self.bridge.custody():
                pass
            respond_request(self.directory, record["request_id"], "original-task", self.response(record))
            with self.bridge.custody():
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


class SyntheticOda(core_fixtures.FakeOda):
    terminal_failure = None
    provider = "oda"
    def call(self, tool, arguments, **kwargs):
        if tool == "get_delivery_addresses":
            return {"result": [{"id": 7, "address": "Eksempelveien 1", "isSelected": True}]}
        return super().call(tool, arguments, **kwargs)


@unittest.skipUnless(sys.platform.startswith("linux"), "Muse cloud process identity is Linux")
class NativeCoreRpcTests(NativeFixture):
    def setUp(self):
        super().setUp()
        self.store = core_fixtures.StateStore(self.directory / "state", core_fixtures.CONFIG)
        self.shop = SyntheticOda()
        self.shop.cart["items"][0].update(description="500 g", brand="Synthetic")
        local = datetime.now(ZoneInfo("Europe/Oslo")) + timedelta(days=2)
        month = ("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des")[local.month - 1]
        self.delivery = f"{local.day}. {month} 09:00 - 12:00"
        self.delivery_date = local.date().isoformat()
        self.shop.cart["delivery"]["display"] = f"Hjemlevering mellom kl 09 og 12, {local.day}. {month}"
        self.shop.order_delivery = self.delivery_date
        for offset, slot in enumerate(self.shop.delivery_slots["slots"]):
            day = local + timedelta(days=7 * offset)
            slot.update(slot_ref=f"oda:{day.date().isoformat()}:{slot['provider_slot_id']}",
                start_at=day.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
                end_at=day.replace(hour=12, minute=0, second=0, microsecond=0).isoformat(), price_ore=0)
        self.browser = MuseBrowser(self.directory, "original-task", self.shop, state_store=self.store)
        self.app = muse.ProtectedMuseApplication(self.store, self.shop, self.browser, external_recipe_sources={})
        self.app._now = lambda: datetime.now(timezone.utc)
        self.produced = set()
        self.effects = []
        self.read_windows = []
        self.corrupt_effect_reply = False
        self.defer_delegation = False
        self.skip_delegation = False
        self.deny_delegation = False
        self.pending_delegation = None
        self.final_label = "Bekreft og betal 35,00kr"
        self.checkout_items = [{"product_id": 10, "title": "Fullkornspasta",
                                "subtitle": "500 g, Synthetic", "quantity": 1}]
        self.checkout_amount_rows = [{"label": "1 vare", "value": "35,00kr"},
            {"label": "Delsum", "value": "35,00kr"}, {"label": None, "value": "0,00kr"},
            {"label": "Total inkl. MVA", "value": "35,00kr"}]

    def facts(self, record):
        operation = record["operation"]
        account = {"url": "https://oda.com/no/account/delivery/", "edit_urls": ["https://oda.com/no/account/delivery/edit/7/"]}
        if operation == "checkout_review":
            self.read_windows.append((datetime.fromisoformat(record["expires_at"])
                                      - datetime.fromisoformat(record["issued_at"])).total_seconds())
            return {"url": "https://oda.com/no/checkout/confirm/", "account": account,
                    "address": "Eksempelveien 1", "delivery_sections": [self.delivery],
                    "items": deepcopy(self.checkout_items),
                    "warnings": [], "amount_rows": deepcopy(self.checkout_amount_rows),
                    "payment": {"display": "•••• 1234", "selected": True},
                    "submit_controls": [{"label": self.final_label, "enabled": True}],
                    "complete_sections": ["account", "items", "warnings", "amounts", "delivery", "payment", "submit"]}
        if operation in ACTION_OPERATIONS:
            pending = self.store.read()["pending_checkout" if operation.startswith("checkout_") else "pending_cancellation"]
            self.assertEqual(pending["status"], "clicking")
            self.assertEqual(record["payload"]["journal_binding"], {"confirmation_id": pending["confirmation_id"],
                "expires_at": pending["expires_at"], "journal_digest": digest(pending)})
            self.assertLessEqual(record["expires_at"], pending["expires_at"])
            if operation in DELEGATION_OPERATIONS:
                self.assertEqual(record["payload"]["authorization"], {"mode": "native_approval",
                    "expiry_role": "admission", "purchase_approval_required": operation == "checkout_delegate"})
            else:
                self.assertNotIn("authorization", record["payload"])
            consume_request(self.directory, record["request_id"], "original-task")
            if operation in DELEGATION_OPERATIONS and (self.defer_delegation or self.deny_delegation):
                self.pending_delegation = record
                return {"dispatch": "approval_denied" if self.deny_delegation else "awaiting_purchase_approval"}
            self.apply_effect(record)
            return {"dispatch": "clicked_once"}
        receipt = {"url": "https://oda.com/no/account/orders/new-order/", "order_id": "new-order",
                   "currency": "NOK", "receipt_address": "Eksempelveien 1", "account": account,
                   "delivery_sections": [self.delivery], "total_rows": ["Total 35,00 kr"],
                   "complete_sections": ["receipt", "account"]}
        if operation == "order_binding":
            return receipt
        if operation == "cancelled_checkout_binding":
            return {"receipt": receipt, "checkout_completion": {
                "request_id": record["payload"]["original_checkout_request_id"], "order_id": "new-order"}}
        if operation == "cancellation_review":
            return {"receipt": receipt, "dialog": {"text": "Vil du kansellere bestillingen? Ingen gebyr.",
                "final_controls": [{"label": "Kanseller bestillingen min", "enabled": True}],
                "dismiss_controls": [{"label": "Nei, ikke kanseller", "enabled": True}], "closed": True}}
        return {"status": "unknown"}

    def apply_effect(self, record):
        operation = record["operation"]
        self.effects.append(operation)
        if operation.startswith("checkout_"):
            self.shop.orders.append({"order_number": "new-order", "grossAmount": 35.0,
                "deliveryDate": self.delivery_date, "deliverySlotDisplay": self.delivery,
                "deliveryAddress": "Eksempelveien 1", "products": [{"product": {"id": 10, "name": "Fullkornspasta"}, "quantity": 1, "totalGrossAmount": "35.00"}]})
        else:
            self.shop.tracking = "cancelled"

    def test_native_core_read_budget_fits_cli_without_changing_other_defaults(self):
        from rpc_client import rpc_timeout
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.assertIn("confirmation_id", prepared)
        self.assertTrue(self.read_windows)
        self.assertTrue(all(500 < seconds <= 540 for seconds in self.read_windows))
        self.assertLess(self.app._checkout_operation_timeout(), rpc_timeout("checkout", {}))
        self.app.browser = None
        self.assertEqual(self.app._checkout_operation_timeout(), 240)
        self.app.provider = "meny"
        self.assertEqual(self.app._checkout_operation_timeout(), 600)

    def test_public_producer_serves_ordinary_cli_preparation_over_real_socket(self):
        source = Path(__file__).resolve().parents[1]
        producer = source / "muse_browser_producer.py"
        sock = self.directory / "rpc.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(sock))
        listener.listen(1)
        listener.settimeout(15)
        server = Server(sock, os.getgid(), os.getuid(), self.app)
        # A failed synthetic handoff must not leave an unbounded core worker.
        self.app._checkout_operation_timeout = lambda: 10
        server_errors, cli_results = [], []
        def serve_once():
            try:
                # Ordinary RPC first verifies health, then sends checkout.
                for _ in range(2):
                    connection, _ = listener.accept()
                    server._serve(connection)
            except Exception as error:
                server_errors.append(error)
        serving = threading.Thread(target=serve_once)
        serving.start()
        self.addCleanup(serving.join, 16)
        def invoke_cli():
            cli_results.append(subprocess.run(
                [sys.executable, "-I", "-B", str(source / "cli.py")],
                input=b'{"operation":"checkout","action":"prepare"}',
                env={**os.environ, "MEAL_CONCIERGE_SOCKET": str(sock)},
                capture_output=True, timeout=15))
        caller = threading.Thread(target=invoke_cli)
        caller.start()
        self.addCleanup(caller.join, 16)
        path = wait_for(lambda: next((self.directory / "requests").glob("*.json"), None))
        record = read_json(path)
        common = ["--directory", str(self.directory), "--request-id", path.stem,
                  "--task-id", "original-task"]
        def command(name, data=b"", extra=()):
            result = subprocess.run([sys.executable, "-I", "-B", str(producer), name,
                                     *common, *extra], input=data, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        command("claim")
        # This is the synthetic transport control, not proof of native browser inspection.
        command("respond", json.dumps(self.facts(record), ensure_ascii=False).encode(),
                ["--observed-at", datetime.now(timezone.utc).isoformat(),
                 "--ending-state", "completed"])
        caller.join(5)
        serving.join(5)
        self.assertFalse(caller.is_alive())
        self.assertFalse(serving.is_alive())
        self.assertEqual(server_errors, [])
        self.assertEqual(cli_results[0].returncode, 0, cli_results[0].stderr)
        reply = json.loads(cli_results[0].stdout)
        self.assertIs(reply["ok"], True)
        prepared = reply["result"]
        self.assertIn("confirmation_id", prepared)
        self.assertIsNotNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.effects, [])
        self.assertEqual(list((self.directory / "consumed").glob("*.json")), [])

    def rpc(self, request, *, expect_ok=True):
        client, connection = socket.socketpair()
        server = Server(self.directory / "unused.sock", os.getgid(), os.getuid(), self.app)
        thread = threading.Thread(target=server._serve, args=(connection,))
        thread.start()
        client.settimeout(5)
        try:
            client.sendall((json.dumps({**request, "contract": 1}) + "\n").encode())
            while thread.is_alive():
                for path in (self.directory / "requests").glob("*.json"):
                    if path.name not in self.produced:
                        record = read_json(path)
                        if record["operation"] == "checkout_delegate" and self.skip_delegation:
                            if self.skip_delegation in {"claim", "consume"}:
                                claim_request(self.directory, record["request_id"], "original-task")
                            if self.skip_delegation == "consume":
                                consume_request(self.directory, record["request_id"], "original-task")
                            self.produced.add(path.name)
                            continue
                        claim_request(self.directory, record["request_id"], "original-task")
                        facts = self.facts(record)
                        state = ("waiting_for_information" if record["operation"] in DELEGATION_OPERATIONS
                                 and self.defer_delegation else "completed")
                        reply = self.response(record, state=state, facts=facts)
                        if record["operation"] in {"checkout_click", "checkout_delegate"} and self.corrupt_effect_reply:
                            reply["request_digest"] = "0" * 64
                            durable_publish(self.directory / "responses" / path.name, reply)
                        else:
                            respond_request(self.directory, record["request_id"], "original-task", reply)
                        self.produced.add(path.name)
                thread.join(0.01)
            raw = b""
            while b"\n" not in raw:
                raw += client.recv(65536)
            result = json.loads(raw)
            self.assertEqual(result["ok"], expect_ok, result)
            return result["result"] if result["ok"] else result
        finally:
            client.close()
            thread.join(5)

    def test_real_rpc_new_order_and_cancellation_dispatch_once(self):
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        confirmed = self.rpc({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(confirmed["confirmed"])
        repeated = self.rpc({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertEqual(repeated["order_id"], "new-order")
        cancelled = self.rpc({"operation": "orders", "action": "cancel_prepare", "order_id": "new-order"})
        result = self.rpc({"operation": "orders", "action": "cancel_confirm", "order_id": "new-order", "confirmation_id": cancelled["confirmation_id"]})
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.effects, ["checkout_click", "cancellation_click"])
        self.assertIsNone(self.store.read()["pending_checkout"])
        self.assertIsNone(self.store.read()["pending_cancellation"])

    def test_native_approval_delegates_once_and_reconciles_exact_own_cancellation(self):
        self.browser.action_mode = "native_approval"
        self.checkout_items[0]["product_id"] = None
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.assertIsNone(self.store.read()["pending_checkout"]["browser_review"]["surface"]["items"][0]["product_id"])
        confirmation = {"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]}
        self.assertTrue(self.rpc(confirmation)["confirmed"])
        self.assertTrue(self.rpc(confirmation)["confirmed"])
        cancelled = self.rpc({"operation": "orders", "action": "cancel_prepare", "order_id": "new-order"})
        cancellation = {"operation": "orders", "action": "cancel_confirm", "order_id": "new-order",
                        "confirmation_id": cancelled["confirmation_id"]}
        self.assertTrue(self.rpc(cancellation)["cancelled"])
        self.assertTrue(self.rpc(cancellation)["cancelled"])
        self.assertEqual(self.effects, ["checkout_delegate", "cancellation_delegate"])

    def unserved_delegation(self, skip=True):
        self.browser.action_mode = "native_approval"
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.skip_delegation = skip
        with core_fixtures.mock.patch("muse_browser.ACTION_OPERATION_TIMEOUT", 0.3):
            self.rpc({"operation": "checkout", "action": "confirm",
                      "confirmation_id": prepared["confirmation_id"]}, expect_ok=False)
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")
        self.assertEqual(self.effects, [])
        record = next(read_json(p) for p in (self.directory / "requests").glob("*.json")
                      if read_json(p)["operation"] == "checkout_delegate")
        return prepared, record

    def test_expired_unclaimed_delegate_retires_without_replaying_confirmation(self):
        prepared, record = self.unserved_delegation()
        original = (self.directory / "requests" / (record["request_id"] + ".json")).read_bytes()
        request = {"operation": "checkout", "action": "reconcile",
                   "confirmation_id": prepared["confirmation_id"]}
        result = self.rpc(request)
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["payment_dispatched"])
        self.assertFalse(result["retry_allowed"])
        self.assertTrue(result["preparation_available"])
        self.assertEqual(result["nondispatch_evidence"]["request_digest"], digest(record))
        self.assertIsNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.rpc(request)["nondispatch_evidence"], result["nondispatch_evidence"])
        self.rpc({**request, "action": "confirm"}, expect_ok=False)
        self.assertEqual(self.effects, [])
        self.assertEqual((self.directory / "requests" / (record["request_id"] + ".json")).read_bytes(), original)
        fresh = self.rpc({"operation": "checkout", "action": "prepare"})
        self.assertNotEqual(fresh["confirmation_id"], prepared["confirmation_id"])

    def test_new_merchant_order_keeps_original_delegation_uncertain(self):
        prepared, _ = self.unserved_delegation()
        self.shop.orders.append({"order_number": "unrelated-order", "grossAmount": 70.0,
            "deliveryDate": self.delivery_date, "deliverySlotDisplay": self.delivery,
            "deliveryAddress": "Eksempelveien 1", "products": [{"product": {"id": 10},
            "quantity": 2, "totalGrossAmount": "70.00"}]})
        result = self.rpc({"operation": "checkout", "action": "reconcile",
                           "confirmation_id": prepared["confirmation_id"]})
        self.assertFalse(result["preparation_available"])
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_matching_paid_order_cannot_be_owned_by_an_unclaimed_delegation(self):
        prepared, _ = self.unserved_delegation()
        self.shop.orders.append({"order_number": "other-actor-order", "grossAmount": 35.0,
            "deliveryDate": self.delivery_date, "deliverySlotDisplay": self.delivery,
            "deliveryAddress": "Eksempelveien 1", "products": [{"product": {"id": 10},
            "quantity": 1, "totalGrossAmount": "35.00"}]})
        self.shop.tracking = "paid_and_modifiable"
        before_requests = set((self.directory / "requests").glob("*.json"))
        result = self.rpc({"operation": "checkout", "action": "reconcile",
                           "confirmation_id": prepared["confirmation_id"]})
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["preparation_available"])
        self.assertNotIn(prepared["confirmation_id"], self.store.read()["protected_results"])
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")
        self.assertEqual(set((self.directory / "requests").glob("*.json")), before_requests)

    def test_conflicting_current_order_identity_preserves_uncertainty(self):
        self.shop.orders.append({"orderNumber": "old-order", "order_number": "old-order"})
        prepared, _ = self.unserved_delegation()
        self.shop.orders[-1]["order_number"] = "new-order"
        self.rpc({"operation": "checkout", "action": "reconcile",
                  "confirmation_id": prepared["confirmation_id"]}, expect_ok=False)
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")

    def test_malformed_retained_order_list_preserves_uncertainty(self):
        # A malformed baseline is present before preparation, so the actual
        # generated delegation still binds the unchanged original journal.
        self.shop.orders.append({})
        prepared, _ = self.unserved_delegation()
        self.rpc({"operation": "checkout", "action": "reconcile",
                  "confirmation_id": prepared["confirmation_id"]}, expect_ok=False)
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")

    def test_changed_journal_binding_cannot_retire_unclaimed_delegate(self):
        prepared, _ = self.unserved_delegation()
        with self.store.locked() as state:
            state["pending_checkout"]["browser_review"]["surface"]["items"][0]["title"] = "Other item"
        result = self.rpc({"operation": "checkout", "action": "reconcile",
                           "confirmation_id": prepared["confirmation_id"]})
        self.assertFalse(result["retry_allowed"])
        self.assertNotIn("preparation_available", result)
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")

    def test_claimed_delegate_does_not_gain_nondispatch_disposition(self):
        prepared, _ = self.unserved_delegation(skip="claim")
        self.rpc({"operation": "checkout", "action": "reconcile",
                  "confirmation_id": prepared["confirmation_id"]}, expect_ok=False)
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")

    def test_consumed_delegate_preserves_uncertainty(self):
        prepared, _ = self.unserved_delegation(skip="consume")
        self.rpc({"operation": "checkout", "action": "reconcile",
                  "confirmation_id": prepared["confirmation_id"]}, expect_ok=False)
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")

    def test_dangling_claim_and_changed_closure_refuse_disposition(self):
        prepared, record = self.unserved_delegation()
        closed = self.directory / "closed" / (record["request_id"] + ".json")
        original = read_json(closed)
        closed.write_text(json.dumps({**original, "request_digest": "0" * 64}))
        result = self.rpc({"operation": "checkout", "action": "reconcile",
                           "confirmation_id": prepared["confirmation_id"]})
        self.assertNotIn("preparation_available", result)
        self.assertIsNotNone(self.store.read()["pending_checkout"])
        closed.write_text(json.dumps(original))
        (self.directory / "claims" / closed.name).symlink_to(self.directory / "missing")
        self.rpc({"operation": "checkout", "action": "reconcile",
                  "confirmation_id": prepared["confirmation_id"]}, expect_ok=False)
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_multiple_matching_delegations_cannot_retire_confirmation(self):
        prepared, record = self.unserved_delegation()
        other = self.record("checkout_delegate", payload=record["payload"],
                            issued_at=record["issued_at"], expires_at=record["expires_at"])
        durable_publish(self.directory / "closed" / (other["request_id"] + ".json"),
                        {"request_digest": digest(other), "task_id": other["task_id"],
                         "closed_at": datetime.now(timezone.utc).isoformat()})
        self.produced.add(other["request_id"] + ".json")
        result = self.rpc({"operation": "checkout", "action": "reconcile",
                           "confirmation_id": prepared["confirmation_id"]})
        self.assertNotIn("preparation_available", result)
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_retirement_and_late_admission_cannot_both_succeed(self):
        prepared, record = self.unserved_delegation()
        outcomes = []
        barrier = threading.Barrier(2)
        def late_claim():
            barrier.wait(5)
            try:
                claim_request(self.directory, record["request_id"], "original-task")
                outcomes.append("claimed")
            except HouseholdError:
                outcomes.append("refused")
        contender = threading.Thread(target=late_claim)
        contender.start()
        barrier.wait(5)
        result = self.rpc({"operation": "checkout", "action": "reconcile",
                           "confirmation_id": prepared["confirmation_id"]})
        contender.join(5)
        self.assertFalse(contender.is_alive())
        self.assertEqual(outcomes, ["refused"])
        self.assertTrue(result["preparation_available"])
        self.assertEqual(self.effects, [])

    def test_unknown_id_requires_complete_matching_labels_and_exact_quantity(self):
        original = {**self.checkout_items[0], "product_id": None}
        for changes in ({"title": "Ris"}, {"subtitle": "1 kg, Synthetic"},
                        {"subtitle": "500 g, Other"}, {"quantity": 2},
                        {"product_id": 11}, {"product_id": "10"},
                        {"product_id": True}, {"product_id": 0}):
            self.checkout_items = [{**original, **changes}]
            with self.subTest(changes=changes):
                self.rpc({"operation": "checkout", "action": "prepare"}, expect_ok=False)
                self.assertIsNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.effects, [])
        self.assertFalse(list((self.directory / "consumed").glob("*.json")))

    def test_indistinguishable_unknown_id_rows_cannot_authorize_checkout(self):
        self.shop.cart["items"].append({**self.shop.cart["items"][0], "product_id": 11})
        self.shop.cart.update(count=2, subtotal=70.0)
        self.checkout_items = [{**self.checkout_items[0], "product_id": None}] * 2
        self.checkout_amount_rows = [{"label": "2 varer", "value": "70,00kr"},
            {"label": "Delsum", "value": "70,00kr"}, {"label": None, "value": "0,00kr"},
            {"label": "Total inkl. MVA", "value": "70,00kr"}]
        self.final_label = "Bekreft og betal 70,00kr"
        self.rpc({"operation": "checkout", "action": "prepare"}, expect_ok=False)
        self.assertIsNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.effects, [])
        self.assertFalse(list((self.directory / "consumed").glob("*.json")))

    def test_unknown_id_becoming_proven_changes_the_frozen_review_before_dispatch(self):
        self.browser.action_mode = "native_approval"
        self.checkout_items[0]["product_id"] = None
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.checkout_items[0]["product_id"] = 10
        self.rpc({"operation": "checkout", "action": "confirm",
                  "confirmation_id": prepared["confirmation_id"]}, expect_ok=False)
        self.assertEqual(self.effects, [])
        self.assertFalse(list((self.directory / "consumed").glob("*.json")))

    def test_rpc_exposes_display_review_and_accepts_bound_continuation(self):
        self.browser.action_mode = "native_approval"
        self.checkout_items[0].update(product_id=None, title="Pasta",
                                     subtitle="Fullkorn, 500 g, Synthetic")
        failed = self.rpc({"operation": "checkout", "action": "prepare"}, expect_ok=False)
        prefix = "Muse checkout product identity or quantity differs from the current cart "
        self.assertTrue(failed["error"].startswith(prefix))
        issue = json.loads(failed["error"][len(prefix):])["line_difference"]
        self.assertEqual([row["index"] for row in issue["expected"]], [0])
        self.assertEqual([row["index"] for row in issue["actual"]], [0])
        self.assertIsNone(issue["actual"][0]["product_id"])
        self.assertIsNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.effects, [])

        review = {"digest": issue["digest"], "decisions": [{
            "expected_index": 0, "actual_index": 0,
            "reason": "The checkout splits full-grain pasta wording between title and subtitle; brand, pack size and quantity agree.",
        }]}
        self.checkout_items[0]["title"] = "Spaghetti"
        stale = self.rpc({"operation": "checkout", "action": "prepare",
                          "identity_review": review}, expect_ok=False)
        self.assertEqual(stale["error"], "Muse checkout identity review is stale or invalid")
        self.assertIsNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.effects, [])
        self.checkout_items[0]["title"] = "Pasta"
        prepared = self.rpc({"operation": "checkout", "action": "prepare",
                             "identity_review": review})
        self.assertEqual(self.store.read()["pending_checkout"]["browser_review"]["identity_review"], review)
        confirmed = self.rpc({"operation": "checkout", "action": "confirm",
                              "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(self.effects, ["checkout_delegate"])

    def test_existing_timed_confirmation_cannot_be_reinterpreted_as_native_approval(self):
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.browser.action_mode = "native_approval"
        self.rpc({"operation": "checkout", "action": "confirm",
                  "confirmation_id": prepared["confirmation_id"]}, expect_ok=False)
        self.assertEqual(self.effects, [])
        self.assertIsNone(self.store.read()["pending_checkout"])
        self.assertFalse([p for p in (self.directory / "requests").glob("*.json")
                          if read_json(p)["operation"] in ACTION_OPERATIONS])

    def test_delayed_approval_after_admission_expiry_preserves_one_attempt_until_reconciled(self):
        self.browser.action_mode = "native_approval"
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        with self.store.locked() as state:
            state["pending_checkout"]["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
        self.defer_delegation = True
        confirmation = {"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]}
        self.rpc(confirmation, expect_ok=False)
        record = self.pending_delegation
        self.assertIsNotNone(record)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")
        time.sleep(max(0, (datetime.fromisoformat(record["expires_at"]) - datetime.now(timezone.utc)).total_seconds()) + 0.05)
        self.rpc(confirmation, expect_ok=False)
        with self.assertRaises(HouseholdError), self.bridge.custody():
            pass
        with self.assertRaises(HouseholdError):
            consume_request(self.directory, record["request_id"], "original-task")
        self.apply_effect(record)
        end_request(self.directory, record["request_id"], "original-task",
                    self.response(record, facts={"dispatch": "clicked_once"}))
        result = self.rpc({"operation": "checkout", "action": "reconcile", "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["order_id"], "new-order")
        self.assertEqual(self.effects, ["checkout_delegate"])
        self.assertIsNone(self.store.read()["pending_checkout"])

    def test_denial_never_means_click_or_authorizes_another_delegation(self):
        self.browser.action_mode = "native_approval"
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.deny_delegation = True
        confirmation = {"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]}
        self.rpc(confirmation, expect_ok=False)
        self.rpc(confirmation, expect_ok=False)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")
        self.assertEqual(len([p for p in (self.directory / "requests").glob("*.json")
                              if read_json(p)["operation"] == "checkout_delegate"]), 1)

    def cancelled_before_first_attribution(self, *, rich_completion=False):
        self.browser.action_mode = "native_approval"
        self.defer_delegation = True
        self.shop.orders.append({"order_number": "preexisting-order", "currency": "NOK",
            "grossAmount": 20.0, "deliveryDate": self.delivery_date,
            "deliverySlotDisplay": self.delivery, "deliveryAddress": "Eksempelveien 1",
            "products": [{"product": {"id": 20, "name": "Synthetic oats"},
                          "quantity": 1, "totalGrossAmount": "20.00"}]})
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.rpc({"operation": "checkout", "action": "confirm",
                  "confirmation_id": prepared["confirmation_id"]}, expect_ok=False)
        record = self.pending_delegation
        self.apply_effect(record)
        self.shop.tracking = "cancelled"
        completion = (self.rich_checkout_completion() if rich_completion
                      else {"dispatch": "clicked_once"})
        end_request(self.directory, record["request_id"], "original-task",
                    self.response(record, facts=completion))
        self.assertNotIn("unpaid_order_id", self.store.read()["pending_checkout"])
        return {"operation": "checkout", "action": "reconcile",
                "confirmation_id": prepared["confirmation_id"]}, record

    def rich_checkout_completion(self):
        return {"outcome": "completed", "order_number": "new-order",
                "confirmation_url": "https://oda.com/no/checkout/success/?orderNumber=new-order",
                "total": "35.00", "currency": "NOK",
                "delivery_slot": f"{self.delivery_date} 09:00-12:00",
                "delivery_address": "Eksempelveien 1", "payment_method": "saved Visa **** 1234",
                "clicked_once": True, "receipt": "genuine browser-observed order confirmation page"}

    def test_cancelled_rich_completion_closes_without_rewriting_or_replaying(self):
        request, record = self.cancelled_before_first_attribution(rich_completion=True)
        ending = self.directory / "endings" / (record["request_id"] + ".json")
        original_bytes = ending.read_bytes()
        preexisting = deepcopy(self.shop.orders[:-1])
        result = self.rpc(request)
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["retry_allowed"])
        self.assertEqual(result["payment_resolution"], {"authorization_release": "unknown", "refund": "unknown"})
        request_count = len(list((self.directory / "requests").glob("*.json")))
        provider_calls = len(self.shop.calls)
        self.assertEqual(self.rpc(request), {**result, "idempotent": True})
        self.assertEqual(len(list((self.directory / "requests").glob("*.json"))), request_count)
        self.assertEqual(len(self.shop.calls), provider_calls)
        self.assertEqual(ending.read_bytes(), original_bytes)
        self.assertEqual(self.shop.orders[:-1], preexisting)
        self.assertEqual(self.effects, ["checkout_delegate"])

    def test_rich_completion_rejects_contradictory_or_ambiguous_historical_facts(self):
        request, _ = self.cancelled_before_first_attribution(rich_completion=True)
        pending = self.store.read()["pending_checkout"]
        facts = self.rich_checkout_completion()
        changes = [
            {"clicked_once": 1}, {"clicked_once": False}, {"outcome": "unknown"},
            {"order_number": "another-order"}, {"currency": "SEK"}, {"total": "34.00"},
            {"total": "35e0"}, {"delivery_address": "Another address"},
            {"payment_method": "saved Visa **** 5678"}, {"receipt": ""},
            {"delivery_slot": f"{self.delivery_date} 09:00-13:00"},
            {"delivery_slot": f"{int(self.delivery_date[:4]) + 1}{self.delivery_date[4:]} 09:00-12:00"},
            {"confirmation_url": facts["confirmation_url"] + "&orderNumber=another-order"},
            {"confirmation_url": facts["confirmation_url"].replace("oda.com", "oda.com.example.org")},
            {"extra": "field"},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(original_checkout_completed({**facts, **change}, pending, "new-order"))
        for key in facts:
            with self.subTest(missing=key):
                incomplete = dict(facts)
                del incomplete[key]
                self.assertFalse(original_checkout_completed(incomplete, pending, "new-order"))
        wrong_pending = deepcopy(pending)
        wrong_pending["summary"]["payment_method"] = "vipps"
        self.assertFalse(original_checkout_completed(facts, wrong_pending, "new-order"))
        with self.corrupt_cancelled_facts(lambda observed: observed["checkout_completion"].update(order_id="another-order")):
            self.rpc(request, expect_ok=False)
        self.assertIsNotNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.effects, ["checkout_delegate"])

    def test_cancelled_before_first_attribution_closes_original_rpc_without_replay(self):
        request, _ = self.cancelled_before_first_attribution()
        preexisting = deepcopy(self.shop.orders[:-1])
        self.assertEqual(len(preexisting), 1)
        result = self.rpc(request)
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["retry_allowed"])
        self.assertEqual(result["order_id"], "new-order")
        self.assertEqual(result["payment_resolution"], {"authorization_release": "unknown", "refund": "unknown"})
        request_count = len(list((self.directory / "requests").glob("*.json")))
        provider_calls = len(self.shop.calls)
        self.assertEqual(self.rpc(request), {**result, "idempotent": True})
        self.rpc({**request, "action": "confirm"}, expect_ok=False)
        self.rpc({"operation": "orders", "action": "cancel_prepare", "order_id": "new-order"}, expect_ok=False)
        self.assertEqual(len(list((self.directory / "requests").glob("*.json"))), request_count)
        self.assertEqual(len(self.shop.calls), provider_calls)
        state = self.store.read()
        self.assertIsNone(state["pending_checkout"])
        self.assertIsNone(state["pending_cancellation"])
        self.assertFalse(state["email_jobs"])
        self.assertFalse(state["recurring_fulfilled"])
        self.assertEqual(self.shop.orders[:-1], preexisting)
        self.assertEqual(self.effects, ["checkout_delegate"])

    def corrupt_cancelled_facts(self, change):
        original = self.facts
        def changed(record):
            facts = original(record)
            if record["operation"] == "cancelled_checkout_binding":
                change(facts)
            return facts
        return core_fixtures.mock.patch.object(self, "facts", side_effect=changed)

    def test_cancelled_candidate_requires_actual_original_completion_order(self):
        request, _ = self.cancelled_before_first_attribution()
        with self.corrupt_cancelled_facts(lambda facts: facts["checkout_completion"].update(order_id="another-order")):
            self.rpc(request, expect_ok=False)
        self.assertIsNotNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.effects, ["checkout_delegate"])

    def test_cancelled_candidate_requires_current_original_account(self):
        request, _ = self.cancelled_before_first_attribution()
        with self.corrupt_cancelled_facts(lambda facts: facts["receipt"]["account"].update(
                edit_urls=["https://oda.com/no/account/delivery/edit/8/"])):
            self.rpc(request, expect_ok=False)
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_cancelled_candidate_requires_genuine_original_ending(self):
        request, record = self.cancelled_before_first_attribution()
        ending = self.directory / "endings" / (record["request_id"] + ".json")
        ending.unlink()
        self.rpc(request, expect_ok=False)
        self.assertIsNotNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.effects, ["checkout_delegate"])

    def test_cancelled_candidate_rechecks_status_after_binding(self):
        request, _ = self.cancelled_before_first_attribution()
        with self.corrupt_cancelled_facts(lambda facts: setattr(self.shop, "tracking", "paid_and_modifiable")):
            self.rpc(request, expect_ok=False)
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_cancelled_candidate_rechecks_original_journal_after_binding(self):
        request, _ = self.cancelled_before_first_attribution()
        def change(facts):
            with self.store.locked() as state:
                state["pending_checkout"]["candidate_binding_ambiguous"] = True
        with self.corrupt_cancelled_facts(change):
            self.rpc(request, expect_ok=False)
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_cancelled_candidate_does_not_choose_between_identical_new_orders(self):
        request, _ = self.cancelled_before_first_attribution()
        another = deepcopy(self.shop.orders[-1])
        another["order_number"] = "another-order"
        self.shop.orders.append(another)
        result = self.rpc(request)
        self.assertFalse(result.get("cancelled", False))
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_cancelled_candidate_rejects_conflicting_provider_aliases(self):
        request, _ = self.cancelled_before_first_attribution()
        self.shop.orders[-1]["orderNumber"] = "conflicting-order"
        self.rpc(request, expect_ok=False)
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_cancelled_candidate_rejects_duplicate_merchant_identities(self):
        request, _ = self.cancelled_before_first_attribution()
        self.shop.orders.append(deepcopy(self.shop.orders[0]))
        self.rpc(request, expect_ok=False)
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_cancelled_candidate_rejects_changed_currency(self):
        request, _ = self.cancelled_before_first_attribution()
        self.shop.orders[-1]["currency"] = "SEK"
        result = self.rpc(request)
        self.assertFalse(result.get("cancelled", False))
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_cancelled_candidate_rejects_changed_goods(self):
        request, _ = self.cancelled_before_first_attribution()
        self.shop.orders[-1]["products"][0]["quantity"] = 2
        result = self.rpc(request)
        self.assertFalse(result.get("cancelled", False))
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_cancelled_candidate_rejects_changed_amount(self):
        request, _ = self.cancelled_before_first_attribution()
        self.shop.orders[-1]["grossAmount"] = 36
        result = self.rpc(request)
        self.assertFalse(result.get("cancelled", False))
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_cancelled_candidate_rejects_changed_delivery(self):
        request, _ = self.cancelled_before_first_attribution()
        self.shop.orders[-1]["deliverySlotDisplay"] = self.delivery.replace("09:00", "08:00")
        result = self.rpc(request)
        self.assertFalse(result.get("cancelled", False))
        self.assertIsNotNone(self.store.read()["pending_checkout"])

    def test_unsupported_or_foreign_target_rejected_before_provider_or_host(self):
        before = len(self.shop.calls)
        for request in ({"operation": "checkout", "action": "prepare", "order_id": "other"},
                        {"operation": "checkout", "action": "prepare", "automatic_checkout": True},
                        {"operation": "checkout", "action": "prepare", "checkout_payment": {"method": "vipps"}},
                        {"operation": "checkout", "action": "reconcile"},
                        {"operation": "orders", "action": "cancel_prepare", "order_id": "foreign-order"}):
            with self.subTest(request=request), self.assertRaises(HouseholdError):
                self.app.handle(request)
        self.assertEqual(len(self.shop.calls), before)
        self.assertFalse(list((self.directory / "requests").glob("*.json")))

    def test_final_label_does_not_accept_matching_suffix_of_malformed_amount(self):
        self.final_label = "Bekreft og betal 1 35,00kr"
        self.rpc({"operation": "checkout", "action": "prepare"}, expect_ok=False)
        self.assertEqual(self.effects, [])
        self.assertIsNone(self.store.read()["pending_checkout"])

    def test_amended_preexisting_or_foreign_provider_order_cannot_be_cancelled(self):
        before = len(self.shop.calls)
        for changed, provider in ((True, "oda"), (False, "mathem")):
            with self.store.locked() as state:
                state["protected_results"]["legacy-confirmation"] = {"kind": "checkout", "target_id": "existing-order",
                    "result": {"confirmed": True, "order_id": "existing-order", "changed_existing_order": changed}}
                state["order_snapshot_providers"]["existing-order"] = provider
            with self.subTest(changed=changed, provider=provider), self.assertRaises(HouseholdError):
                self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "existing-order"})
        self.assertEqual(len(self.shop.calls), before)
        self.assertFalse(list((self.directory / "requests").glob("*.json")))

    def test_applied_effect_invalid_reply_preserves_uncertainty_and_reconciles_without_replay(self):
        self._lost_effect_ack("checkout_click")

    def test_delegated_effect_invalid_reply_preserves_uncertainty_and_reconciles_without_replay(self):
        self.browser.action_mode = "native_approval"
        self._lost_effect_ack("checkout_delegate")

    def _lost_effect_ack(self, operation):
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.corrupt_effect_reply = True
        confirmation = {"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]}
        self.rpc(confirmation, expect_ok=False)
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")
        self.rpc(confirmation, expect_ok=False)
        self.assertEqual(self.effects, [operation])
        effect = next(read_json(p) for p in (self.directory / "requests").glob("*.json")
                      if read_json(p)["operation"] == operation)
        path = self.directory / "responses" / (effect["request_id"] + ".json")
        bad_reply = path.read_bytes()
        end_request(self.directory, effect["request_id"], "original-task", self.response(effect, facts={"dispatch": "clicked_once"}))
        self.assertEqual(path.read_bytes(), bad_reply)
        result = self.rpc({"operation": "checkout", "action": "reconcile", "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["order_id"], "new-order")
        self.assertEqual(self.effects, [operation])
        self.assertIsNone(self.store.read()["pending_checkout"])


if __name__ == "__main__":
    unittest.main()
