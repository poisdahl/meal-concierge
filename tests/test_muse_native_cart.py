"""Synthetic real RPC/producer unit changes; never merchant capability evidence."""
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from core import HouseholdError, StateStore
from clients import muse
from muse_browser import claim_request, consume_request, digest, durable_publish, end_request, read_json, respond_request
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
        self.extra_quantities = {}
        self.action_hook = None
        self.action_result = "dispatched"
        self.action_records = []
        self.catalog_state = "completed"
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

    def quantities(self):
        return {**({"29829": self.quantity} if self.quantity else {}),
                **{key: value for key, value in self.extra_quantities.items() if value}}

    def observation(self, tool, arguments):
        return read_fixture.observation(tool, arguments)

    def produce(self):
        served = {path.name for path in self.history if path.parent.name == "requests"}
        while not self.stop.wait(0.01):
            for path in (self.broker / "requests").glob("*.json"):
                if path.name in served:
                    continue
                served.add(path.name)
                try:
                    record = read_json(path)
                    if record["operation"] == "cart_change" and self.action_result == "unclaimed":
                        self.action_records.append(record)
                        self.seen.append("cart_change")
                        continue
                    producer = None
                    if getattr(self, "finite_producer", False) and record["operation"] == "provider_read":
                        producer = [sys.executable, "-I", "-B", str(ROOT / "muse_browser_producer.py")]
                        args = ["--directory", str(self.broker), "--request-id", record["request_id"],
                                "--task-id", record["task_id"]]
                        claimed = subprocess.run([*producer, "claim", *args], capture_output=True, timeout=5)
                        self.assertEqual(claimed.returncode, 0, claimed.stderr)
                    else:
                        claim_request(self.broker, record["request_id"], record["task_id"])
                    if record["operation"] == "delivery_select":
                        facts, task_state = self.delivery_effect(record)
                    elif record["operation"] == "cart_change":
                        pending = read_json(self.home / "state/state.json")["pending_cart_change"]
                        self.assertEqual(record["payload"]["intent_digest"], digest(pending))
                        self.assertEqual(pending["before"], self.quantities())
                        self.assertEqual(record["payload"]["operations"], pending["operations"])
                        self.assertEqual(record["payload"]["expected_quantities"], pending["expected"])
                        self.action_records.append(record)
                        self.seen.append("cart_change")
                        outcome = (self.action_hook(len(self.action_records), pending)
                                   if self.action_hook else self.action_result)
                        if outcome == "late_refusal":
                            continue
                        if outcome == "not_dispatched":
                            facts = {"dispatch": "not_dispatched"}
                        else:
                            consume_request(self.broker, record["request_id"], record["task_id"])
                            unit = pending["operations"][0]
                            key = str(unit["productId"])
                            if key == "29829":
                                self.quantity += unit["quantity"]
                            else:
                                self.extra_quantities[key] = self.extra_quantities.get(key, 0) + unit["quantity"]
                            facts = {"dispatch": "dispatched"} if outcome in {"dispatched", "waiting"} else {}
                        task_state = "waiting_for_information" if outcome == "waiting" else "completed"
                    else:
                        self.assertEqual(record["operation"], "provider_read")
                        tool = record["payload"]["tool"]
                        self.seen.append(tool)
                        facts = self.observation(tool, record["payload"]["arguments"])
                        if tool == "get_cart" and self.quantities():
                            rows = []
                            for key, quantity in self.quantities().items():
                                name, package = {"29829": ("Synthetic squash", "250 g"),
                                                 "29830": ("Synthetic rice", "500 g")}.get(
                                                    key, ("Synthetic baseline", "1 pack"))
                                rows.append({"url": f"https://oda.com/no/products/{key}-synthetic/",
                                             "title": name, "subtitle": package,
                                             "quantity": quantity, "price": None})
                            facts.update(empty=False, items=rows)
                        facts = self.transform(tool, facts)
                        task_state = self.catalog_state if tool == "product_search" else "completed"
                    response = {"request_id": record["request_id"], "request_digest": digest(record),
                                "task_id": record["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
                                "task_state": task_state, "facts": facts}
                    if producer:
                        published = subprocess.run([*producer, "respond", *args, "--observed-at",
                            response["observed_at"], "--ending-state", task_state],
                            input=json.dumps(facts).encode(), capture_output=True, timeout=5)
                        self.assertEqual(published.returncode, 0, published.stderr)
                    else:
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

    def prepare_menu(self, *, two_ingredients=False, squash_grams=250):
        recipe = {"name": "Synthetic squash", "language": "nb-NO", "portions": 2,
            "ingredients": [{"raw": f"{squash_grams} g squash", "quantity": squash_grams, "unit": "g", "item": "squash", "scalable": True}],
            "steps": ["Cook the squash."], "source": {"kind": "user", "relationship": "user_supplied",
            "title": "Synthetic squash", "publisher": "Fixture", "external_id": "native-squash"},
            "rights": {"storage": "full", "credit": "Fixture"}, "times": {"active_minutes": 20}}
        if two_ingredients:
            recipe["ingredients"].append({"raw": "100 g rice", "quantity": 100,
                                          "unit": "g", "item": "rice", "scalable": True})
        entry = self.rpc({"operation": "recipes", "action": "save", "recipe": recipe,
                          "idempotency_key": "native-squash"})["recipe"]
        planned = self.rpc({"operation": "menu", "action": "plan", "planner_input": {
            "dates": [date.today().isoformat()],
            "candidates": [{"recipe_ref": {"id": entry["id"], "revision": entry["revision"]}}]}})
        menu = self.rpc({"operation": "menu", "action": "save",
                         "planner_ref": planned["plan"]["save_ref"]})["menu"]
        return {key: menu[key] for key in ("menu_id", "revision", "digest")}

    def test_native_product_prepare_selection_and_saved_read_require_enabled_cart_policy(self):
        reference = self.prepare_menu()
        prepared = self.rpc({"operation": "products", "action": "prepare", "menu_ref": reference,
                             "include_recurring": False, "price_mode": "estimate"})
        product = prepared["product_plan"]["requirements"][0]["observation"]["products"][0]
        selected = self.rpc({"operation": "products", "action": "prepare",
            "product_plan_ref": prepared["product_plan_ref"], "include_recurring": False,
            "candidate_approvals": [product["candidate_approval"]]})
        readback = self.rpc({"operation": "products", "action": "get",
                             "product_plan_ref": selected["product_plan_ref"]})
        self.assertEqual(readback["status"], "prepared")
        self.assertEqual(readback["requirements"][0]["selection"]["package_count"], 1)
        self.assertEqual(readback["totals"]["merchandise_ore"], 3990)
        self.assertIsNone(readback["totals"]["total_payable_ore"])
        for result in (prepared, selected, readback):
            self.assertNotIn("partial_apply_arguments", result)
            self.assertIn("original enabled cart policy", result["next"])
        self.assertTrue(selected["apply_arguments"])
        self.assertTrue(readback["apply_arguments"])
        self.rpc({"operation": "products", "action": "apply",
                  "product_plan_ref": selected["product_plan_ref"]}, ok=False)
        self.assertFalse(self.action_records)
        self.assertNotIn("pending_cart_change", self.store.read())
        self.assertEqual((self.home / "muse-client.json").read_bytes(), self.marker)
        for path, original in self.history.items():
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real native service process is Linux")
    def test_two_slow_catalog_reads_finish_one_prepare_without_cart_authority(self):
        clock = self.home / "fixture-monotonic-offset"
        clock.write_text("0")
        command = list(self.process.args)
        command[4] = self.runner.replace("sys.argv = sys.argv[1:]", """
import time
from pathlib import Path
clock = Path(sys.argv[sys.argv.index('--home') + 1]) / 'fixture-monotonic-offset'
real_monotonic = time.monotonic
time.monotonic = lambda: real_monotonic() + float(clock.read_text())
sys.argv = sys.argv[1:]
""")
        self.process.terminate()
        self.process.communicate(timeout=5)
        (self.home / "service.sock").unlink()
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        read_fixture.wait_for(lambda: (self.home / "service.sock").exists())
        self.rpc({"operation": "health"})
        self.finite_producer = True
        elapsed = 0

        def delayed_catalog(tool, facts):
            nonlocal elapsed
            if tool == "product_search":
                elapsed += 170
                temporary = clock.with_suffix(".tmp")
                temporary.write_text(str(elapsed))
                temporary.replace(clock)
                if any(word in facts["query"].lower() for word in ("rice", "ris")):
                    facts["products"] = [{"url": "https://oda.com/no/products/29830-synthetic-rice/",
                        "name": "Synthetic rice", "description": "500 g", "price": "5,00 kr",
                        "unitPrice": "10,00 kr", "unitName": "kg", "availability": True}]
            return facts

        self.transform = delayed_catalog
        reference = self.prepare_menu(two_ingredients=True, squash_grams=700)
        prepared = self.rpc({"operation": "products", "action": "prepare", "menu_ref": reference,
                             "include_recurring": False, "price_mode": "estimate"})
        self.assertEqual(elapsed, 340)
        requirements = prepared["product_plan"]["requirements"]
        self.assertEqual(len(requirements), 2)
        approvals = [row["observation"]["products"][0]["candidate_approval"] for row in requirements]
        selected = self.rpc({"operation": "products", "action": "prepare",
            "product_plan_ref": prepared["product_plan_ref"], "include_recurring": False,
            "candidate_approvals": approvals})
        saved = self.rpc({"operation": "products", "action": "get",
                          "product_plan_ref": selected["product_plan_ref"]})
        self.assertEqual(saved["status"], "prepared")
        self.assertEqual(sorted(row["selection"]["package_count"] for row in saved["requirements"]), [1, 3])
        self.assertEqual(saved["totals"]["merchandise_ore"], 12470)
        self.assertEqual(elapsed, 340)
        requests = [read_json(path) for path in (self.broker / "requests").glob("*.json")
                    if read_json(path)["payload"].get("tool") == "product_search"]
        self.assertEqual(len(requests), 2)
        for record in requests:
            seconds = (datetime.fromisoformat(record["expires_at"])
                       - datetime.fromisoformat(record["issued_at"])).total_seconds()
            self.assertGreater(seconds, 179)
            self.assertLessEqual(seconds, 180)
            self.assertTrue((self.broker / "closed" / (record["request_id"] + ".json")).exists())
        self.assertFalse(self.rpc({"operation": "native_cart_policy", "action": "show"})["enabled"])
        self.assertFalse(self.action_records)
        self.assertEqual(self.quantities(), {})
        self.assertFalse(self.store.read().get("pending_cart_change"))
        self.assertEqual((self.home / "muse-client.json").read_bytes(), self.marker)
        for path, original in self.history.items():
            self.assertEqual(path.read_bytes(), original)

    def managed_plan(self):
        self.extra_quantities = {"9999": 1}
        def transform(tool, facts):
            if tool == "product_search" and any(word in facts["query"].lower() for word in ("rice", "ris")):
                facts["products"] = [{"url": "https://oda.com/no/products/29830-synthetic-rice/",
                    "name": "Synthetic rice", "description": "500 g", "price": "5,00 kr",
                    "unitPrice": "10,00 kr", "unitName": "kg", "availability": True}]
            return facts
        self.transform = transform
        reference = self.prepare_menu(two_ingredients=True, squash_grams=700)
        prepared = self.rpc({"operation": "products", "action": "prepare", "menu_ref": reference,
                             "include_recurring": False, "price_mode": "estimate"})
        approvals = [row["observation"]["products"][0]["candidate_approval"]
                     for row in prepared["product_plan"]["requirements"]]
        selected = self.rpc({"operation": "products", "action": "prepare",
            "product_plan_ref": prepared["product_plan_ref"], "include_recurring": False,
            "candidate_approvals": approvals})
        self.assertEqual(selected["product_plan"]["status"], "prepared", selected)
        self.assertIsNone(selected["product_plan"]["totals"]["total_payable_ore"])
        return {"operation": "products", **selected["apply_arguments"], "cart_change_requested": True}

    def test_complete_pantry_plan_with_empty_cart_needs_no_payable_total(self):
        reference = self.prepare_menu()
        prepared = self.rpc({"operation": "products", "action": "prepare", "menu_ref": reference,
            "include_recurring": False, "price_mode": "estimate", "ingredient_decisions": [{
                "source": {"collection": "dishes", "recipe_index": 0, "ingredient_index": 0},
                "action": "have_all"}]})
        self.enable()
        result = self.rpc({"operation": "products", **prepared["apply_arguments"],
                           "cart_change_requested": True})
        self.assertTrue(result["applied"], result)
        self.assertTrue(result["nothing_to_buy"], result)
        self.assertFalse(self.action_records)
        state = self.store.read()
        self.assertNotIn("pending_cart_change", state)
        self.assertNotIn("managed_product_apply_fence", state)
        self.assertTrue(state["product_plan_completion"]["nothing_to_buy"])

    def test_managed_multi_product_apply_keeps_baseline_and_verifies_quantities_above_one(self):
        request = self.managed_plan()
        self.enable()
        result = self.rpc(request)
        self.assertTrue(result["applied"], result)
        self.assertEqual(self.quantities(), {"29829": 3, "29830": 1, "9999": 1})
        state = self.store.read()
        self.assertEqual(state["cart_plan"]["baseline_quantities"], {"9999": 1})
        self.assertEqual(state["cart_plan"]["added_quantities"], {"29829": 3, "29830": 1})
        self.assertNotIn("pending_cart_change", state)
        self.assertNotIn("managed_product_apply_fence", state)
        self.assertEqual(len(self.action_records), 4)
        self.assertEqual(len({row["payload"]["intent_digest"] for row in self.action_records}), 4)
        self.assertTrue(all((self.broker / "consumed" / (row["request_id"] + ".json")).exists()
                            for row in self.action_records))

    def test_managed_refusal_preserves_verified_prefix_and_reconciles_without_effect(self):
        request = self.managed_plan()
        self.enable()
        self.action_hook = lambda number, pending: "not_dispatched" if number == 2 else "dispatched"
        result = self.rpc(request)
        self.assertFalse(result["applied"], result)
        pending = self.store.read()["pending_cart_change"]
        self.assertEqual(pending["native_managed"]["verified"], {"29829": 1, "9999": 1})
        self.assertEqual(self.quantities(), {"29829": 1, "9999": 1})
        records = len(self.action_records)
        reconciled = self.rpc({"operation": "cart", "action": "reconcile_change"})
        self.assertTrue(reconciled["reconciled"], reconciled)
        state = self.store.read()
        self.assertEqual(state["cart_plan"]["added_quantities"], {"29829": 1})
        self.assertEqual(state["cart_plan"]["baseline_quantities"], {"9999": 1})
        self.assertIn("managed_product_apply_fence", state)
        self.assertNotIn("pending_cart_change", state)
        self.assertEqual(len(self.action_records), records)
        self.assertFalse((self.broker / "consumed" / (self.action_records[-1]["request_id"] + ".json")).exists())
        self.action_hook = None
        self.resume_managed_plan(expected_added=3)

    def resume_managed_plan(self, expected_added):
        cart = self.rpc({"operation": "cart", "action": "get"})
        reference = self.store.read()["cart_plan"]["menu_ref"]
        records = len(self.action_records)
        self.rpc({"operation": "cart", "action": "reconcile", "decision": "restore_missing",
                  "menu_ref": reference, "cart_digest": cart["cart_digest"]}, ok=False)
        decision = self.rpc({"operation": "cart", "action": "reconcile", "decision": "keep_current",
                             "menu_ref": reference, "cart_digest": cart["cart_digest"]})
        self.assertTrue(decision["reconciled"], decision)
        self.assertEqual(len(self.action_records), records)
        prepared = self.rpc({"operation": "products", "action": "prepare", "menu_ref": reference,
                             "include_recurring": False, "price_mode": "estimate"})
        approvals = [row["observation"]["products"][0]["candidate_approval"]
                     for row in prepared["product_plan"]["requirements"]]
        selected = self.rpc({"operation": "products", "action": "prepare",
            "product_plan_ref": prepared["product_plan_ref"], "include_recurring": False,
            "candidate_approvals": approvals})
        request = {"operation": "products", **selected["apply_arguments"], "cart_change_requested": True,
                   "restore_missing": True, "cart_digest": cart["cart_digest"]}
        resumed = self.rpc(request)
        self.assertTrue(resumed["applied"], resumed)
        self.assertEqual(self.quantities(), {"29829": 3, "29830": 1, "9999": 1})
        self.assertEqual(len(self.action_records) - records, expected_added)
        state = self.store.read()
        self.assertEqual(state["cart_plan"]["added_quantities"], {"29829": 3, "29830": 1})
        self.assertEqual(state["cart_plan"]["baseline_quantities"], {"9999": 1})
        self.assertNotIn("pending_cart_change", state)
        self.assertNotIn("managed_product_apply_fence", state)

    def test_managed_waiting_unit_blocks_next_effect_until_original_late_ending(self):
        request = self.managed_plan()
        self.enable()
        self.action_hook = lambda number, pending: "waiting" if number == 2 else "dispatched"
        result = self.rpc(request)
        self.assertNotEqual(result.get("applied"), True, result)
        pending = deepcopy(self.store.read()["pending_cart_change"])
        self.assertEqual(self.quantities(), {"29829": 2, "9999": 1})
        self.assertEqual(pending["native_managed"]["verified"], {"29829": 1, "9999": 1})
        self.assertEqual(len(self.action_records), 2)
        records = [read_json(path) for path in (self.broker / "requests").glob("*.json")]
        original = self.action_records[-1]
        self.assertFalse([record for record in records if record["issued_at"] > original["issued_at"]])
        self.rpc({"operation": "native_cart_policy", "action": "set", "enabled": False})
        self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
        self.assertEqual(self.store.read()["pending_cart_change"], pending)
        self.assertEqual(len(list((self.broker / "requests").glob("*.json"))), len(records))
        original = self.action_records[-1]
        end_request(self.broker, original["request_id"], original["task_id"], {
            "request_id": original["request_id"], "request_digest": digest(original),
            "task_id": original["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
            "task_state": "completed", "facts": {"dispatch": "dispatched"}})
        self.assertTrue(self.rpc({"operation": "cart", "action": "reconcile_change"})["reconciled"])
        state = self.store.read()
        self.assertEqual(state["cart_plan"]["added_quantities"], {"29829": 2})
        self.assertEqual(state["cart_plan"]["baseline_quantities"], {"9999": 1})
        self.assertIn("managed_product_apply_fence", state)
        self.assertNotIn("pending_cart_change", state)
        self.assertEqual(len(self.action_records), 2)
        self.action_hook = None
        self.enable()
        self.resume_managed_plan(expected_added=2)

    def test_native_product_prepare_requires_explicit_recurring_scope_before_catalog_read(self):
        reference = self.prepare_menu()
        calls = list(self.seen)
        for action in ([], {}):
            result = self.rpc({"operation": "products", "action": action}, ok=False)
            self.assertIn("Unsupported Muse browser read", result["error"])
        for value in (None, True, 0):
            result = self.rpc({"operation": "products", "action": "prepare", "menu_ref": reference,
                               "include_recurring": value}, ok=False)
            self.assertIn("include_recurring=false", result["error"])
        self.assertEqual(self.seen, calls)

    def test_native_continuation_cannot_narrow_original_recurring_enabled_scope(self):
        reference = self.prepare_menu()
        # An existing ordinary-core plan can precede this adapter. Its stored
        # scope stays authoritative when read or continued through Muse.
        prepared = Application(self.store, FakeOda(), None).handle({
            "operation": "products", "action": "prepare", "menu_ref": reference,
            "include_recurring": True, "price_mode": "estimate"})
        calls = list(self.seen)
        result = self.rpc({"operation": "products", "action": "prepare",
            "product_plan_ref": prepared["product_plan_ref"], "include_recurring": False}, ok=False)
        self.assertIn("original recurring-disabled", result["error"])
        self.assertEqual(self.seen, calls)
        readback = self.rpc({"operation": "products", "action": "get",
                             "product_plan_ref": prepared["product_plan_ref"]})
        self.assertNotIn("continue_arguments", readback)
        self.assertNotIn("apply_arguments", readback)
        self.assertFalse(self.action_records)

    def test_unresolved_first_catalog_read_prevents_second_ingredient_task(self):
        reference = self.prepare_menu(two_ingredients=True)
        self.catalog_state = "waiting_for_information"
        result = self.rpc({"operation": "products", "action": "prepare", "menu_ref": reference,
                           "include_recurring": False, "price_mode": "estimate"})
        records = [read_json(path) for path in (self.broker / "requests").glob("*.json")]
        catalog = [record for record in records if record["payload"].get("tool") == "product_search"]
        self.assertEqual(len(catalog), 1)
        self.assertEqual(self.seen.count("product_search"), 1)
        self.assertNotEqual(result["product_plan"]["status"], "prepared")
        self.assertFalse(self.action_records)

    def test_completed_incomplete_catalog_read_allows_next_ingredient_observation(self):
        reference = self.prepare_menu(two_ingredients=True)
        self.transform = lambda tool, facts: {**facts, "complete": False} if tool == "product_search" else facts
        result = self.rpc({"operation": "products", "action": "prepare", "menu_ref": reference,
                           "include_recurring": False, "price_mode": "estimate"})
        self.assertEqual(self.seen.count("product_search"), 2)
        self.assertNotEqual(result["product_plan"]["status"], "prepared")
        self.assertFalse(self.action_records)

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

    def unclaimed_change(self):
        self.enable()
        self.action_result = "unclaimed"
        with mock.patch("muse_browser.CART_ACTION_TIMEOUT", 1.0):
            self.change(1, ok=False)
        self.rpc({"operation": "native_cart_policy", "action": "set", "enabled": False})
        return deepcopy(self.store.read()["pending_cart_change"]), self.action_records[0]

    def test_closed_unclaimed_change_reconciles_without_ending_or_replay(self):
        pending, record = self.unclaimed_change()
        name = record["request_id"] + ".json"
        preserved = {path: path.read_bytes() for path in self.broker.glob("*/*.json")}
        with self.assertRaisesRegex(HouseholdError, "expired"):
            claim_request(self.broker, record["request_id"], record["task_id"])
        self.assertTrue(self.rpc({"operation": "cart", "action": "reconcile_change"})["reconciled"])
        self.assertNotIn("pending_cart_change", self.store.read())
        self.rpc({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(self.quantity, 0)
        self.assertEqual(len(self.action_records), 1)
        for path, original in preserved.items():
            self.assertEqual(path.read_bytes(), original)
        for kind in ("claims", "consumed", "responses", "endings", "publications"):
            self.assertFalse((self.broker / kind / name).exists())
        self.assertEqual(pending["before"], {})

    def test_unclaimed_reconciliation_refuses_each_admission_record_and_dangling_symlink(self):
        pending, record = self.unclaimed_change()
        for kind in ("claims", "consumed", "responses", "endings", "publications"):
            directory = self.broker / kind
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / (record["request_id"] + ".json")
            for symbolic in (False, True):
                with self.subTest(kind=kind, symbolic=symbolic):
                    if symbolic:
                        path.symlink_to(directory / "absent")
                    else:
                        path.write_text("{}")
                    self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
                    self.assertEqual(self.store.read()["pending_cart_change"], pending)
                    path.unlink()
        self.assertEqual(len(self.action_records), 1)

    def test_unclaimed_reconciliation_requires_original_payload_closure_binding_and_unique_request(self):
        pending, record = self.unclaimed_change()
        name = record["request_id"] + ".json"
        request_path = self.broker / "requests" / name
        closed_path = self.broker / "closed" / name
        request_bytes, closed_bytes = request_path.read_bytes(), closed_path.read_bytes()
        closed = read_json(closed_path)
        for replacement in ({**closed, "request_digest": "a" * 64},
                            {**closed, "task_id": "different-anchor"},
                            {**closed, "closed_at": record["issued_at"]},
                            {**closed, "closed_at": "2999-01-01T00:00:00+00:00"}):
            with self.subTest(closed=replacement):
                closed_path.write_text(json.dumps(replacement))
                self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
                self.assertEqual(self.store.read()["pending_cart_change"], pending)
        closed_path.write_bytes(closed_bytes)
        changed = deepcopy(record)
        changed["payload"]["expected_quantities"] = {}
        request_path.write_text(json.dumps(changed))
        closed_path.write_text(json.dumps({**closed, "request_digest": digest(changed)}))
        self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
        request_path.write_bytes(request_bytes)
        closed_path.write_bytes(closed_bytes)
        duplicate = {**record, "request_id": str(uuid.uuid4())}
        duplicate_path = self.broker / "requests" / (duplicate["request_id"] + ".json")
        durable_publish(duplicate_path, duplicate)
        refused = self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
        self.assertIn("ambiguous", refused["error"])
        duplicate_path.unlink()
        request_path.unlink()
        self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
        request_path.write_bytes(request_bytes)
        with self.store.locked() as state:
            state["pending_cart_change"]["native_cart_binding"]["binding_digest"] = "a" * 64
        altered = self.store.read()["pending_cart_change"]
        self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
        self.assertEqual(self.store.read()["pending_cart_change"], altered)
        # The producer may also observe the deliberately injected duplicate.
        self.assertEqual({row["request_id"] for row in self.action_records} - {duplicate["request_id"]},
                         {record["request_id"]})

    def test_unclaimed_reconciliation_preserves_changed_cart_and_journal_race(self):
        pending, _record = self.unclaimed_change()
        self.quantity = 1
        self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
        self.assertEqual(self.store.read()["pending_cart_change"], pending)
        self.quantity = 0

        def replace_journal(tool, facts):
            if tool == "get_cart":
                with self.store.locked() as state:
                    state["pending_cart_change"]["native_cart_binding"]["intent_id"] = str(uuid.uuid4())
            return facts

        self.transform = replace_journal
        self.rpc({"operation": "cart", "action": "reconcile_change"}, ok=False)
        self.assertNotEqual(self.store.read()["pending_cart_change"], pending)
        self.assertEqual(len(self.action_records), 1)


if __name__ == "__main__":
    unittest.main()
