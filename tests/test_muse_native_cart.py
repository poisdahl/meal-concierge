"""Synthetic real RPC/producer unit changes; never merchant capability evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from core import HouseholdError, StateStore
from clients import muse
from muse_browser import claim_request, consume_request, digest, end_request, read_json, respond_request
from muse_native_provider import MuseNativeCartProvider
from service import Application
from test_muse_client import RUNNER
import test_muse_native_provider as read_fixture
from test_meal_concierge import FakeOda


class NativeCartTests(unittest.TestCase):
    transport = "browser_cart"
    runner = RUNNER.replace("sys.argv = sys.argv[1:]", """
sys.path.insert(0, str(__import__('pathlib').Path(sys.argv[1]).resolve().parents[1]))
import muse_browser
muse_browser.CART_ACTION_TIMEOUT = 1.0
sys.argv = sys.argv[1:]
""")
    finish = read_fixture.NativeReadTests.finish

    def setUp(self):
        self.quantity = 0
        self.action_result = "dispatched"
        self.action_records = []
        if sys.platform.startswith("linux"):
            read_fixture.NativeReadTests.setUp(self)
            self.store = StateStore(self.home / "state", muse.load_home(self.home))
            return
        # The cloud process-identity boundary is exercised by Linux CI. The Mac
        # fixture runs the real household/broker/producer path in this process.
        self.temp = tempfile.TemporaryDirectory(prefix="nc-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ops = self.root / "ops"
        self.ops.mkdir(mode=0o700)
        self.broker = self.ops / "browser"
        self.broker.mkdir(mode=0o700)
        for name in ("requests", "claims", "consumed", "responses", "endings", "closed"):
            (self.broker / name).mkdir(mode=0o700)
        self.home = self.root / "h"
        muse.initialize(self.home, "oda", "Synthetic household", credential_name="custom.synthetic",
                        operation_directory=self.ops)
        self.marker = (self.home / "muse-client.json").read_bytes()
        self.history, self.seen, self.failures = {}, [], []
        self.transform = lambda tool, facts: facts
        self.stop = threading.Event()
        identity = mock.patch("muse_browser.process_start", return_value="12345")
        identity.start()
        self.addCleanup(identity.stop)
        self.store = StateStore(self.home / "state", muse.load_home(self.home))
        self.thread = threading.Thread(target=self.produce)
        self.thread.start()
        self.addCleanup(self.finish)
        self.app = muse.NativeCartMuseApplication(self.store,
            MuseNativeCartProvider(self.ops, "synthetic-original-task"), None, external_recipe_sources={})

    def rpc(self, request, *, ok=True):
        if sys.platform.startswith("linux"):
            return read_fixture.NativeReadTests.rpc(self, request, ok=ok)
        try:
            result = self.app.handle(request)
        except HouseholdError as error:
            self.assertFalse(ok, str(error))
            return {"ok": False, "error": str(error)}
        self.assertTrue(ok, result)
        return result

    def produce(self):
        served = {path.name for path in self.history if path.parent.name == "requests"}
        while not self.stop.wait(0.01):
            for path in (self.broker / "requests").glob("*.json"):
                if path.name in served:
                    continue
                served.add(path.name)
                try:
                    record = read_json(path)
                    claim_request(self.broker, record["request_id"], record["task_id"])
                    if record["operation"] == "cart_change":
                        pending = read_json(self.home / "state/state.json")["pending_cart_change"]
                        self.assertEqual(record["payload"]["intent_digest"], digest(pending))
                        self.assertEqual(pending["before"], {"29829": self.quantity} if self.quantity else {})
                        self.assertEqual(record["payload"]["operations"], pending["operations"])
                        self.assertEqual(record["payload"]["expected_quantities"], pending["expected"])
                        self.action_records.append(record)
                        self.seen.append("cart_change")
                        if self.action_result == "late_refusal":
                            continue
                        if self.action_result == "not_dispatched":
                            facts = {"dispatch": "not_dispatched"}
                        else:
                            consume_request(self.broker, record["request_id"], record["task_id"])
                            self.quantity += pending["operations"][0]["quantity"]
                            facts = {"dispatch": "dispatched"} if self.action_result == "dispatched" else {}
                        task_state = "waiting_for_information" if self.action_result == "waiting" else "completed"
                    else:
                        self.assertEqual(record["operation"], "provider_read")
                        tool = record["payload"]["tool"]
                        self.seen.append(tool)
                        facts = read_fixture.observation(tool, record["payload"]["arguments"])
                        if tool == "get_cart" and self.quantity:
                            facts.update(empty=False, items=[{"url": "https://oda.com/no/products/29829-synthetic-squash/",
                                "title": "Synthetic squash", "subtitle": "250 g", "quantity": self.quantity, "price": None}])
                        facts = self.transform(tool, facts)
                        task_state = "completed"
                    response = {"request_id": record["request_id"], "request_digest": digest(record),
                                "task_id": record["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
                                "task_state": task_state, "facts": facts}
                    respond_request(self.broker, record["request_id"], record["task_id"], response)
                except Exception as error:
                    self.failures.append(repr(error))

    def enable(self):
        policy = self.rpc({"operation": "native_cart_policy", "action": "show"})
        self.assertFalse(policy["enabled"])
        self.rpc({"operation": "native_cart_policy", "action": "set", "enabled": True,
                  "binding_digest": policy["binding_digest"]})

    def change(self, quantity, *, ok=True, cart_digest=None):
        cart = self.rpc({"operation": "cart", "action": "get"})
        return self.rpc({"operation": "cart", "action": "change", "cart_digest": cart_digest or cart["cart_digest"],
                         "operations": [{"product_id": "29829", "quantity": quantity}]}, ok=ok)

    def test_unit_add_remove_real_rpc_preserves_unknown_total_and_original_records(self):
        self.assertEqual(self.rpc({"operation": "health"})["client_guidance"], muse.NATIVE_CART_GUIDANCE)
        self.change(1, ok=False)
        self.assertFalse(self.action_records)
        self.enable()
        added = self.change(1)
        self.assertEqual(added["items"][0]["quantity"], 1)
        self.assertIsNone(added["total"])
        self.assertNotIn("pending_cart_change", self.store.read())
        removed = self.change(-1)
        self.assertEqual(removed["items"], [])
        self.assertIsNone(removed["total"])
        self.assertEqual(len(self.action_records), 2)
        self.rpc({"operation": "native_cart_policy", "action": "set", "enabled": False})
        self.assertFalse(self.store.read()["muse_native_cart_policy"]["enabled"])
        self.assertEqual((self.home / "muse-client.json").read_bytes(), self.marker)
        for path, original in self.history.items():
            self.assertEqual(path.read_bytes(), original)

    def test_scope_and_stale_cart_refusals_emit_no_action_or_pending_intent(self):
        self.enable()
        for quantity in (0, 2, -2, True, 1.5):
            self.change(quantity, ok=False)
        self.change(1, ok=False, cart_digest="a" * 64)
        self.change(-1, ok=False)
        for action in ("clear", "ensure", "sync", "apply"):
            self.rpc({"operation": "cart", "action": action}, ok=False)
        self.rpc({"operation": "products", "action": "apply"}, ok=False)
        self.assertFalse(self.action_records)
        self.assertNotIn("pending_cart_change", self.store.read())

    def test_preconsume_refusal_clears_only_matching_pending_intent(self):
        self.enable()
        self.action_result = "not_dispatched"
        result = self.change(1)
        self.assertIs(result["cart_change_dispatched"], False)
        self.assertEqual(self.quantity, 0)
        self.assertNotIn("pending_cart_change", self.store.read())
        record = self.action_records[0]
        self.assertFalse((self.broker / "consumed" / (record["request_id"] + ".json")).exists())

    def test_waiting_action_keeps_journal_disable_then_actual_end_readonly_reconcile(self):
        self.enable()
        self.action_result = "waiting"
        self.change(1, ok=False)
        pending = deepcopy(self.store.read()["pending_cart_change"])
        other = FakeOda()
        with self.assertRaisesRegex(HouseholdError, "bound native adapter"):
            Application(self.store, other, None).handle({"operation": "cart", "action": "reconcile_change"})
        self.assertFalse(other.calls)
        calls = list(self.seen)
        self.rpc({"operation": "native_cart_policy", "action": "set", "enabled": False})
        self.assertEqual(self.seen, calls)
        self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
        self.assertEqual(self.store.read()["pending_cart_change"], pending)
        record = self.action_records[0]
        end_request(self.broker, record["request_id"], record["task_id"], {
            "request_id": record["request_id"], "request_digest": digest(record), "task_id": record["task_id"],
            "observed_at": datetime.now(timezone.utc).isoformat(), "task_state": "completed",
            "facts": {"dispatch": "dispatched"}})
        result = self.rpc({"operation": "cart", "action": "reconcile_change"})
        self.assertTrue(result["reconciled"])
        self.assertNotIn("pending_cart_change", self.store.read())
        self.assertEqual(len(self.action_records), 1)
        self.assertEqual(self.quantity, 1)

    def test_timeout_late_unconsumed_refusal_reconciles_unique_intent_without_replay(self):
        self.enable()
        self.action_result = "late_refusal"
        with mock.patch("muse_browser.CART_ACTION_TIMEOUT", 1.0):
            self.change(1, ok=False)
        original = deepcopy(self.store.read()["pending_cart_change"])
        first = self.action_records[0]
        end_request(self.broker, first["request_id"], first["task_id"], {
            "request_id": first["request_id"], "request_digest": digest(first), "task_id": first["task_id"],
            "observed_at": datetime.now(timezone.utc).isoformat(), "task_state": "completed",
            "facts": {"dispatch": "not_dispatched"}})
        self.assertTrue(self.rpc({"operation": "cart", "action": "reconcile_change"})["reconciled"])
        self.assertNotIn("pending_cart_change", self.store.read())
        self.assertEqual(self.quantity, 0)
        self.assertEqual(len(self.action_records), 1)
        self.action_result = "waiting"
        self.change(1, ok=False)
        current = self.store.read()["pending_cart_change"]
        self.assertNotEqual(original["native_cart_binding"]["intent_id"], current["native_cart_binding"]["intent_id"])
        self.assertNotEqual(digest(original), digest(current))
        second = self.action_records[1]
        with self.assertRaisesRegex(HouseholdError, "consumed action"):
            end_request(self.broker, second["request_id"], second["task_id"], {
                "request_id": second["request_id"], "request_digest": digest(second), "task_id": second["task_id"],
                "observed_at": datetime.now(timezone.utc).isoformat(), "task_state": "completed",
                "facts": {"dispatch": "not_dispatched"}})
        self.assertEqual(self.store.read()["pending_cart_change"], current)


if __name__ == "__main__":
    unittest.main()
