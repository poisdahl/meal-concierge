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
                          process_start, read_json, request_record, respond_request)
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
                  "expires_at": (now + timedelta(seconds=30 if operation in ACTION_OPERATIONS else 60)).isoformat(), "payload": {}}
        result.update(changes)
        durable_publish(self.directory / "requests" / (result["request_id"] + ".json"), result)
        return result

    def response(self, record, state="completed", facts=None):
        return {"request_id": record["request_id"], "request_digest": digest(record),
                "task_id": record["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
                "task_state": state, "facts": facts or {}}


class NativeBrokerTests(NativeFixture):
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
