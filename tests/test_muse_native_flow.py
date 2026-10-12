"""Original-account startup and real shared custody for a composed native adapter."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError
from clients import muse
from muse_browser import MuseBrowser, claim_request, digest, read_json, respond_request
from muse_native_provider import MuseNativeReadProvider
from test_muse_client import wait_for
from test_muse_native_provider import ACCOUNT, observation


class NativeCompositionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-flow-")
        self.addCleanup(temporary.cleanup)
        self.ops = Path(temporary.name).resolve() / "ops"
        self.ops.mkdir(mode=0o700)
        self.broker = self.ops / "browser"
        self.broker.mkdir(mode=0o700)
        for name in ("requests", "claims", "consumed", "responses", "endings", "closed"):
            (self.broker / name).mkdir(mode=0o700)
        self.provider = MuseNativeReadProvider(self.ops, "synthetic-original-task", accepted_account=ACCOUNT)
        self.browser = MuseBrowser(self.broker, "synthetic-original-task", self.provider)

    def test_financial_adapter_borrows_native_operation_and_excludes_other_owner(self):
        competing = MuseNativeReadProvider(self.ops, "synthetic-original-task", accepted_account=ACCOUNT)
        with self.browser._custody():
            with self.provider.operation():
                self.assertTrue(self.provider._operation.held)
                with self.assertRaises(HouseholdError), competing.operation():
                    self.fail("another owner acquired the existing native custody")
        with competing.operation():
            self.assertFalse(self.provider._operation.held)
        with self.assertRaisesRegex(ValueError, "synthetic failure"), self.browser._custody():
            raise ValueError("synthetic failure")
        with competing.operation():
            self.assertFalse(self.provider._operation.held)

    def test_financial_adapter_cannot_borrow_another_task_chain(self):
        browser = MuseBrowser(self.broker, "synthetic-other-task", self.provider)
        with self.assertRaisesRegex(HouseholdError, "binding differ"), browser._custody():
            self.fail("another task chain borrowed provider custody")
        with self.provider.operation():
            pass

    def test_process_local_account_adoption_is_insufficient_for_financial_custody(self):
        provider = MuseNativeReadProvider(self.ops, "synthetic-original-task")
        browser = MuseBrowser(self.broker, "synthetic-original-task", provider)
        with self.assertRaisesRegex(HouseholdError, "original accepted account"), browser._custody():
            self.fail("unbound native provider admitted financial custody")

    def test_financial_observation_compares_entire_account_and_latches_before_another_request(self):
        accepted = deepcopy(ACCOUNT)
        accepted["edit_urls"].append("https://oda.com/no/account/delivery/edit/202/")
        for operation in ("checkout_review", "order_binding", "cancellation_review", "cancelled_checkout_binding"):
            for links, complete in ((ACCOUNT["edit_urls"], True),
                    (accepted["edit_urls"] + ["https://oda.com/no/account/delivery/edit/303/"], True),
                    (list(reversed(accepted["edit_urls"])), True), (ACCOUNT["edit_urls"], False)):
                with self.subTest(operation=operation, links=links, complete=complete):
                    provider = MuseNativeReadProvider(self.ops, "synthetic-original-task", accepted_account=accepted)
                    browser = MuseBrowser(self.broker, "synthetic-original-task", provider)
                    before = set((self.broker / "requests").glob("*.json"))
                    sections = (["account", "items", "warnings", "amounts", "delivery", "payment", "submit"]
                                if operation == "checkout_review" else ["receipt", "account"])
                    receipt = {"account": {**ACCOUNT, "edit_urls": links},
                               "complete_sections": sections if complete else []}
                    facts = {"receipt": receipt} if operation in {"cancellation_review", "cancelled_checkout_binding"} else receipt
                    errors = []

                    def produce():
                        try:
                            path = wait_for(lambda: next(iter(set((self.broker / "requests").glob("*.json")) - before), None))
                            record = read_json(path)
                            self.assertEqual(record["payload"]["accepted_account"], accepted)
                            claim_request(self.broker, record["request_id"], record["task_id"])
                            respond_request(self.broker, record["request_id"], record["task_id"], {
                                "request_id": record["request_id"], "request_digest": digest(record),
                                "task_id": record["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
                                "task_state": "completed", "facts": facts})
                        except Exception as error:
                            errors.append(error)

                    identity = (nullcontext() if sys.platform.startswith("linux") else
                                mock.patch("muse_browser.process_start", return_value="12345"))
                    with identity:
                        worker = threading.Thread(target=produce)
                        worker.start()
                        try:
                            with browser._custody():
                                if complete and set(links) == set(accepted["edit_urls"]):
                                    self.assertEqual(browser._observe(operation, {}, None), facts)
                                else:
                                    with self.assertRaises(HouseholdError):
                                        browser._observe(operation, {}, None)
                        finally:
                            worker.join(6)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [])
                    latched = complete and set(links) != set(accepted["edit_urls"])
                    self.assertEqual(bool(provider.terminal_failure), latched)
                    if latched:
                        count = len(list((self.broker / "requests").glob("*.json")))
                        with self.assertRaisesRegex(HouseholdError, "account evidence changed"), browser._custody():
                            self.fail("financial custody continued after an observed account change")
                        self.assertEqual(len(list((self.broker / "requests").glob("*.json"))), count)
                    self.assertEqual(provider._accepted_account, accepted)

    def test_invalid_accepted_file_cannot_fall_back_to_first_observation(self):
        home = self.ops.parent / "h"
        muse.initialize(home, "oda", "Synthetic household", credential_name="custom.synthetic",
                        operation_directory=self.ops)
        marker = (home / "muse-client.json").read_bytes()
        accepted = self.ops.parent / "accepted.json"
        for invalid in (None, {}, {**ACCOUNT, "edit_urls": []}):
            accepted.write_text(json.dumps(invalid))
            accepted.chmod(0o600)
            result = subprocess.run([sys.executable, "-I", "-B", str(Path(muse.__file__)), "run",
                "--home", str(home), "--provider-transport", "browser_readonly",
                "--browser-directory", str(self.broker), "--browser-task-id", "synthetic-original-task",
                "--accepted-account", str(accepted)], capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse((home / "service.sock").exists())
            self.assertEqual(list((self.broker / "requests").glob("*.json")), [])
            self.assertEqual((home / "muse-client.json").read_bytes(), marker)

    def test_first_real_broker_observation_cannot_adopt_foreign_account(self):
        accepted = deepcopy(ACCOUNT)
        accepted["edit_urls"].append("https://oda.com/no/account/delivery/edit/202/")
        provider = MuseNativeReadProvider(self.ops, "synthetic-original-task", accepted_account=accepted)
        errors = []

        def produce():
            try:
                path = wait_for(lambda: next((self.broker / "requests").glob("*.json"), None))
                record = read_json(path)
                self.assertEqual(record["payload"]["accepted_account"], accepted)
                claim_request(self.broker, record["request_id"], record["task_id"])
                # Deliver a genuine positive observation of only the other account's set.
                # The ordinary consumer must reject it even without producer prevalidation.
                respond_request(self.broker, record["request_id"], record["task_id"], {
                    "request_id": record["request_id"], "request_digest": digest(record),
                    "task_id": record["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
                    "task_state": "completed", "facts": observation("get_delivery_addresses", {})})
            except Exception as error:
                errors.append(error)

        # Only Linux has the cloud's process identity API. Other hosts exercise
        # the real broker and locks with a synthetic identity, without a browser/network.
        identity = (nullcontext() if sys.platform.startswith("linux") else
                    mock.patch("muse_browser.process_start", return_value="12345"))
        with identity:
            worker = threading.Thread(target=produce)
            worker.start()
            try:
                with self.assertRaisesRegex(HouseholdError, "account evidence changed"):
                    provider.probe()
            finally:
                worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            paths = list((self.broker / "requests").glob("*.json"))
            self.assertEqual(len(paths), 1)
            with self.assertRaisesRegex(HouseholdError, "account evidence changed"):
                provider.probe()
            self.assertEqual(list((self.broker / "requests").glob("*.json")), paths)
        self.assertEqual(provider._account_ids, {101, 202})


if __name__ == "__main__":
    unittest.main()


class NativeFinancialFlowTests(unittest.TestCase):
    """Real RPC, shared state and producer custody; synthetic merchant only."""
    def setUp(self):
        from core import StateStore
        from muse_native_provider import MuseNativeCartProvider
        NativeCompositionTests.setUp(self)
        self.home = self.ops.parent / "home"
        muse.initialize(self.home, "oda", "Synthetic household", credential_name="custom.synthetic",
                        operation_directory=self.ops)
        self.store = StateStore(self.home / "state", muse.load_home(self.home))
        self.provider = MuseNativeCartProvider(self.ops, "synthetic-original-task", accepted_account=ACCOUNT)
        self.browser = MuseBrowser(self.broker, "synthetic-original-task", self.provider,
                                   state_store=self.store, action_mode="native_approval")
        self.patch = mock.patch("muse_browser.process_start", return_value="12345") if not sys.platform.startswith("linux") else nullcontext()
        self.patch.__enter__(); self.addCleanup(self.patch.__exit__, None, None, None)
        self.amount = "141,00 kr"
        self.rows = [("29829", "Synthetic squash", "250 g", 3),
                     ("29830", "Synthetic rice", "500 g", 1),
                     ("9999", "Synthetic baseline", "1 pack", 1)]
        self.paid = self.cancelled = False
        self.click_only = self.defer = self.cancel_ack_only = False
        self.receipt_changes = {}
        self.entry_target = "NATIVE-NEW"
        self.effects, self.errors, self.records = [], [], []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.produce, daemon=True)
        self.thread.start(); self.addCleanup(self.finish)
        self.app = muse.NativeCartMuseApplication(self.store, self.provider, self.browser, external_recipe_sources={})

    def finish(self):
        self.stop.set(); self.thread.join(5)
        self.assertFalse(self.thread.is_alive()); self.assertEqual(self.errors, [])

    def delivery(self):
        from test_muse_native_delivery import picker
        return picker()["held_delivery_text"]

    def receipt(self, reference="NATIVE-NEW"):
        return {"url": f"https://oda.com/no/account/orders/{reference}/", "order_id": reference,
                "currency": "NOK", "receipt_address": "Synthetic street 1, 0001 Synthetic city",
                "account": ACCOUNT, "delivery_sections": [self.delivery()],
                "total_rows": [self.amount], "complete_sections": ["receipt", "account"]}

    def facts(self, record):
        operation, payload = record["operation"], record["payload"]
        if operation == "provider_read":
            tool, args = payload["tool"], payload["arguments"]
            if tool == "get_delivery_slots":
                from test_muse_native_delivery import picker
                return picker()
            value = observation(tool, args)
            if tool == "get_cart":
                value.update(empty=False, items=[{"url": f"https://oda.com/no/products/{pid}-synthetic/",
                    "title": name, "subtitle": pack, "quantity": qty, "price": None}
                    for pid, name, pack, qty in self.rows],
                    amount_rows=[{"label": "Total inkl. MVA", "value": self.amount}],
                    address="Synthetic street 1, 0001 Synthetic city", delivery_text=self.delivery())
            elif tool == "get_orders":
                value.update(orders=[], hasMore=False)
            elif tool in {"get_order", "order_tracking"}:
                value["delivery_text"] = self.delivery()
                if tool == "get_order":
                    value.update(status="Kansellert" if self.cancelled else "Bestilling bekreftet",
                        amount_rows=[{"label": "Total inkl. MVA", "value": self.amount}],
                        goods_complete=True, payment_warnings=[],
                        items=[{"url": None, "name": name+" "+pack, "quantity": qty,
                                "price": None, "labels": []} for _, name, pack, qty in self.rows])
                    value.update(self.receipt_changes)
            return value
        if operation == "checkout_review":
            return {"url": "https://oda.com/no/checkout/confirm/", "account": ACCOUNT,
                "address": "Synthetic street 1, 0001 Synthetic city", "delivery_sections": [self.delivery()],
                "items": [{"product_id": None, "title": name, "subtitle": pack, "quantity": qty}
                          for _, name, pack, qty in self.rows], "warnings": [],
                "amount_rows": [{"label": "5 varer", "value": "42,00 kr"},
                    {"label": "Delsum", "value": "42,00 kr"}, {"label": "Levering", "value": "99,00 kr"},
                    {"label": "Total inkl. MVA", "value": self.amount}],
                "payment": {"display": "•••• 1234", "selected": True},
                "submit_controls": [{"label": "Bekreft og betal "+self.amount, "enabled": True}],
                "complete_sections": ["account", "items", "warnings", "amounts", "delivery", "payment", "submit"]}
        if operation == "order_binding": return self.receipt(payload["order_id"])
        if operation == "cancellation_review":
            return {"receipt": self.receipt(payload["order_id"]),
                "entry": {"url": "https://oda.com/no/", "order_url": f"https://oda.com/no/account/orders/{self.entry_target}/",
                          "reference": None, "menu_control": {"label": "Rediger bestilling", "enabled": True},
                          "preview_control": {"label": "Kanseller bestillingen", "enabled": True}},
                "dialog": {"text": "Kansellere denne bestillingen?", "closed": True,
                    "final_controls": [{"label": "Kanseller bestillingen min", "enabled": True}],
                    "dismiss_controls": [{"label": "Nei, ikke kanseller", "enabled": True}]}}
        from muse_browser import bind_native_task, consume_request
        self.assertIn(operation, {"checkout_delegate", "cancellation_delegate"})
        consume_request(self.broker, record["request_id"], record["task_id"])
        bind_native_task(self.broker, record["request_id"], record["task_id"],
                         {"ok": True, "status": "accepted", "browser_task_id": "synthetic-child-"+record["request_id"]})
        self.effects.append(operation)
        if operation == "cancellation_delegate":
            self.cancelled = not self.cancel_ack_only
            return {"dispatch": "clicked_once"}
        self.paid = True
        return ({"dispatch": "clicked_once"} if self.click_only else
                {"dispatch": "clicked_once", "reference": "NATIVE-NEW",
                 "order_url": "https://oda.com/no/account/orders/NATIVE-NEW/"})

    def produce(self):
        served = set()
        while not self.stop.wait(.005):
            for path in (self.broker / "requests").glob("*.json"):
                if path.name in served: continue
                served.add(path.name)
                try:
                    record = read_json(path); self.records.append(record)
                    claim_request(self.broker, record["request_id"], record["task_id"])
                    facts = self.facts(record)
                    now = datetime.now(timezone.utc).isoformat()
                    response = {"request_id": record["request_id"], "request_digest": digest(record),
                                "task_id": record["task_id"], "observed_at": now, "task_state": "completed", "facts": facts}
                    if record["operation"] == "checkout_delegate" and self.defer:
                        response["task_state"] = "waiting_for_information"
                    elif record["operation"] in {"checkout_delegate", "cancellation_delegate"}:
                        response["native_completion"] = {"browser_task_id": "synthetic-child-"+record["request_id"],
                                                          "outcome": "completed", "completed_at_utc": now}
                    respond_request(self.broker, record["request_id"], record["task_id"], response)
                except Exception as error:
                    self.errors.append(repr(error))

    def rpc(self, request, ok=True):
        import os, socket
        from service import Server
        client, connection = socket.socketpair()
        worker = threading.Thread(target=Server(self.broker / "unused", os.getgid(), os.getuid(), self.app)._serve,
                                  args=(connection,), daemon=True)
        worker.start(); client.settimeout(10)
        try:
            client.sendall((json.dumps({**request, "contract": 1})+"\n").encode())
            reply = bytearray()
            while not reply.endswith(b"\n"): reply.extend(client.recv(65536))
        finally:
            client.close(); worker.join(10)
        self.assertFalse(worker.is_alive()); value = json.loads(reply)
        self.assertEqual(value["ok"], ok, value)
        return value.get("result", value)

    def checkout(self):
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        return self.rpc({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})

    def test_original_child_reference_and_independent_paid_receipt_confirm_once(self):
        result = self.checkout()
        self.assertTrue(result["confirmed"]); self.assertEqual(result["order_id"], "NATIVE-NEW")
        self.assertIsNone(result["tracking_status"])
        self.assertEqual(result["payment"], {"provider_status": "Betalt", "source": "native_order_detail",
                                           "authorization": "unknown", "charge": "unknown"})
        self.assertIsNone(self.store.read()["pending_checkout"])
        self.assertEqual(self.effects, ["checkout_delegate"])
        count = len(self.records)
        again = self.rpc({"operation": "checkout", "action": "reconcile", "confirmation_id": result["confirmation_id"]})
        self.assertTrue(again["idempotent"]); self.assertEqual(len(self.records), count)

    def test_click_only_never_adopts_matching_history_or_replays(self):
        self.click_only = True; result = self.checkout()
        self.assertFalse(result["confirmed"]); self.assertFalse(result["retry_allowed"])
        self.assertEqual(self.effects, ["checkout_delegate"])
        self.rpc({"operation": "checkout", "action": "confirm", "confirmation_id": result["confirmation_id"]}, ok=False)
        self.assertEqual(self.effects, ["checkout_delegate"])

    def test_literal_payment_warning_or_truncated_goods_keeps_original_uncertain(self):
        self.receipt_changes = {"payment_warnings": ["Betaling kreves"]}
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.rpc({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]}, ok=False)
        self.assertEqual(self.effects, ["checkout_delegate"])
        self.assertIsNotNone(self.store.read()["pending_checkout"])
        self.receipt_changes = {"items": [{"url": None, "name": name, "quantity": qty, "price": None, "labels": []}
                                          for _, name, _, qty in self.rows]}
        self.rpc({"operation": "checkout", "action": "reconcile", "confirmation_id": prepared["confirmation_id"]}, ok=False)
        self.assertEqual(self.effects, ["checkout_delegate"])

    def test_normal_home_cancel_rereads_metadata_and_exact_cancelled_receipt(self):
        self.checkout()
        prepared = self.rpc({"operation": "orders", "action": "cancel_prepare", "order_id": "NATIVE-NEW"})
        result = self.rpc({"operation": "orders", "action": "cancel_confirm", "order_id": "NATIVE-NEW",
                           "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["payment_resolution"], {"authorization_release": "unknown", "refund": "unknown"})
        self.assertEqual(self.effects, ["checkout_delegate", "cancellation_delegate"])
        self.assertIsNone(self.store.read()["pending_cancellation"])

    def test_other_home_target_prevents_preview_authority_and_final_effect(self):
        self.checkout(); self.entry_target = "ANOTHER"
        self.rpc({"operation": "orders", "action": "cancel_prepare", "order_id": "NATIVE-NEW"}, ok=False)
        self.assertEqual(self.effects, ["checkout_delegate"])

    def test_cancel_click_alone_is_uncertain_then_exact_receipt_reconciles_without_replay(self):
        self.checkout(); self.cancel_ack_only = True
        prepared = self.rpc({"operation": "orders", "action": "cancel_prepare", "order_id": "NATIVE-NEW"})
        result = self.rpc({"operation": "orders", "action": "cancel_confirm", "order_id": "NATIVE-NEW",
                           "confirmation_id": prepared["confirmation_id"]})
        self.assertFalse(result["cancelled"])
        self.assertIsNotNone(self.store.read()["pending_cancellation"])
        self.cancelled = True
        result = self.rpc({"operation": "orders", "action": "cancel_reconcile", "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["cancelled"]); self.assertEqual(self.effects.count("cancellation_delegate"), 1)

    def test_waiting_original_child_blocks_replacements_and_wrong_child_ending(self):
        from muse_browser import end_request
        self.defer = True
        prepared = self.rpc({"operation": "checkout", "action": "prepare"})
        self.rpc({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]}, ok=False)
        record = next(r for r in self.records if r["operation"] == "checkout_delegate")
        count = len(self.records)
        self.rpc({"operation": "checkout", "action": "reconcile", "confirmation_id": prepared["confirmation_id"]}, ok=False)
        self.assertEqual(len(self.records), count)
        now = datetime.now(timezone.utc).isoformat()
        ending = {"request_id": record["request_id"], "request_digest": digest(record),
            "task_id": record["task_id"], "observed_at": now, "task_state": "completed",
            "facts": {"dispatch": "clicked_once", "reference": "NATIVE-NEW",
                      "order_url": "https://oda.com/no/account/orders/NATIVE-NEW/"},
            "native_completion": {"browser_task_id": "wrong-child", "outcome": "completed", "completed_at_utc": now}}
        with self.assertRaisesRegex(HouseholdError, "another native task"):
            end_request(self.broker, record["request_id"], record["task_id"], ending)
        ending["native_completion"]["browser_task_id"] = "synthetic-child-"+record["request_id"]
        end_request(self.broker, record["request_id"], record["task_id"], ending)
        result = self.rpc({"operation": "checkout", "action": "reconcile", "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.effects, ["checkout_delegate"])


class NativeReceiptDateTests(unittest.TestCase):
    def test_late_receipt_uses_original_absolute_slot_and_actual_observation_day(self):
        from muse_browser import native_receipt_delivery_matches
        from test_muse_native_delivery import NativeDeliveryFactTests, picker
        day = datetime.fromisoformat("2027-01-01").date()
        slots = NativeDeliveryFactTests().parse(picker(day), "2026-12-31T08:00:00Z")["slots"]
        slot = slots[0]
        self.assertTrue(native_receipt_delivery_matches(slot, "Hjemlevering 16-21, i dag", "2027-01-01T08:00:00Z"))
        self.assertFalse(native_receipt_delivery_matches(slot, "Hjemlevering 16-21, i morgen", "2027-01-01T08:00:00Z"))
        self.assertFalse(native_receipt_delivery_matches(slot, "Hjemlevering 16-21, i dag", "2028-01-01T08:00:00Z"))
        self.assertTrue(native_receipt_delivery_matches(slot, "Hjemlevering 16-21, 1. januar", "2027-01-02T08:00:00Z"))
        self.assertFalse(native_receipt_delivery_matches(slot, "Hjemlevering 16-21, 1. januar 2026", "2027-01-02T08:00:00Z"))
        self.assertFalse(native_receipt_delivery_matches(slot, "Hjemlevering 16-21, 2. januar", "2027-01-02T08:00:00Z"))
