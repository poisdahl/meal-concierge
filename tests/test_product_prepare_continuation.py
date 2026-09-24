"""Whole-menu work survives bounded reads without partial cart writes."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import unittest
from unittest import mock

from core import HouseholdError, cart_summary
from service import Application
import test_meal_concierge_product_capacity as capacity
from test_meal_concierge_product_capacity import Retailer, recipe
from test_meal_concierge_products import observation, option, product


class BatchedRetailer(Retailer):
    def product_search_batch(self, queries, *, size, deadline):
        self.calls.append(("product_search_batch", {"queries": list(queries), "size": size}, {"deadline": deadline}))
        if self.on_call:
            self.on_call("product_search_batch", {"queries": queries})
        if self.fail_query in queries:
            raise HouseholdError("synthetic source timeout")
        return {query: deepcopy(self.entry(query)) for query in reversed(queries)}


class ProductContinuationTests(unittest.TestCase):
    setUp = capacity.ProductCapacityTests.setUp
    save_recipes = capacity.ProductCapacityTests.save_recipes
    save_week = capacity.ProductCapacityTests.save_week
    prepare = capacity.ProductCapacityTests.prepare
    approvals = capacity.ProductCapacityTests.approvals
    complete = capacity.ProductCapacityTests.complete
    apply = capacity.ProductCapacityTests.apply

    def request(self, **args):
        return self.app.handle({"operation": "products", **args})

    def save_large_week(self):
        with self.store.locked() as state:
            for key in ("minimum_fish_portions", "minimum_legume_dinners", "minimum_wholegrain_or_potato_dinners", "minimum_vegetable_types"):
                state["profile"]["diet"][key] = 0
        refs = self.save_recipes([recipe(f"Dinner {day}", [(f"food{day * 10 + i}", 100)
                                 for i in range(10 if day < 6 else 7)]) for day in range(7)])
        self.menu = self.app.handle({"operation": "menu", "action": "save", "menu": {
            "week": "2026-W37", "dishes": refs, "salads": []}})["menu"]

    def test_67_requirements_prepare_restart_delta_and_one_complete_apply(self):
        self.provider = BatchedRetailer()
        self.app = Application(self.store, self.provider, object())
        self.save_large_week()
        first = self.request(action="prepare", menu_ref=self.app._cart_menu_ref(self.menu))
        self.assertEqual(len(first["product_plan"]["requirements"]), 67)
        self.assertEqual(sum("observation" in row for row in first["product_plan"]["requirements"]), 64)
        self.assertEqual(len(self.provider.calls), 8)
        self.assertTrue(all(len(args["queries"]) <= 8 for _, args, _ in self.provider.calls))
        self.assertFalse(self.provider.quantities)
        # Restart/lost reply uses the durable ref; no observations are reread.
        self.app = Application(self.store, self.provider, object())
        recovered = self.request(action="get", menu_ref=self.app._cart_menu_ref(self.menu))
        self.assertEqual(recovered["product_plan_ref"], first["product_plan_ref"])
        self.provider.calls.clear()
        second = self.request(**first["continue_arguments"])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.provider.calls[0][1]["queries"]), 3)
        approvals = self.approvals(second["product_plan"])
        self.provider.calls.clear()
        third = self.request(action="prepare", product_plan_ref=second["product_plan_ref"], candidate_approvals=approvals[:64])
        ready = self.request(action="prepare", product_plan_ref=third["product_plan_ref"], candidate_approvals=approvals[64:])
        self.assertEqual(ready["product_plan"]["status"], "prepared")
        self.assertEqual(len(ready["product_plan"]["requirements"]), 67)
        self.assertEqual(self.provider.calls, [])
        page = self.request(action="get", product_plan_ref=ready["product_plan_ref"], response_view="agent")
        self.assertLess(len(json.dumps(page).encode()), 44000)
        partial = self.request(action="apply", product_plan_ref=ready["product_plan_ref"],
                               product_plan_digest=ready["product_plan"]["product_plan_digest"], cart_change_requested=True)
        self.assertEqual(partial["status"], "validating")
        self.assertFalse(partial["cart_changed"])
        self.assertFalse(self.provider.quantities)
        self.assertNotIn("get_cart", [tool for tool, _, _ in self.provider.calls])
        self.app = Application(self.store, self.provider, object())
        last = self.request(**partial["continue_arguments"])
        self.assertTrue(last["applied"])
        self.assertEqual(len(self.provider.quantities), 67)
        self.assertEqual(sum(self.provider.quantities.values()), 67)
        self.assertEqual(sum(tool == "manipulate_cart" for tool, _, _ in self.provider.calls), 1)
        self.assertEqual(sum(len(args["queries"]) for tool, args, _ in self.provider.calls if tool == "product_search_batch"), 67)

    def test_timeout_and_failed_prefix_preserve_explicit_pending_choice(self):
        self.save_week()
        clock = [1000.0]
        self.provider.fail_query = "salmon"
        def advance(tool, args):
            if tool == "product_search":
                clock[0] += 80
        self.provider.on_call = advance
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            first = self.request(action="prepare", menu_ref=self.app._cart_menu_ref(self.menu))
        tail = first["product_plan"]["requirements"][-1]
        approval = {"requirement_id": tail["requirement_id"], "candidate_refs": [self.provider.entry(tail["identity"])["products"][0]["product_ref"]]}
        clock[0] = 2000
        self.provider.calls.clear()
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            second = self.request(action="prepare", product_plan_ref=first["product_plan_ref"], candidate_approvals=[approval])
        attempted = [args["queries"][0] for tool, args, _ in self.provider.calls]
        self.assertNotIn("salmon", attempted)
        pending_row = next(row for row in second["product_plan"]["requirements"] if row["requirement_id"] == tail["requirement_id"])
        self.assertEqual(pending_row["candidate_approval"]["candidate_refs"], approval["candidate_refs"])
        self.provider.on_call = None
        self.provider.fail_query = None
        third = self.request(**second["continue_arguments"])
        row = next(row for row in third["product_plan"]["requirements"] if row["requirement_id"] == tail["requirement_id"])
        self.assertEqual(row["status"], "selected")

    def test_pending_validation_fences_previous_complete_cart(self):
        self.save_week()
        ready = self.complete()
        self.assertTrue(self.apply(ready)["applied"])
        self.provider.fail_query = "salmon"
        self.provider.calls.clear()
        pending = self.apply(ready)
        self.assertEqual(pending["status"], "validating")
        self.assertIn("managed_product_apply_fence", self.store.read())
        self.assertIsNotNone(self.app._cart_checkout_gate(cart_summary(self.provider.cart()), self.store.read()["menu"]))
        self.assertNotIn("manipulate_cart", [tool for tool, _, _ in self.provider.calls])

    def test_noop_current_profile_drift_cannot_record_apply_authority(self):
        self.save_week()
        ready = self.complete()
        self.assertTrue(self.apply(ready)["applied"])
        self.provider.calls.clear()
        def change(tool, args):
            if tool == "get_cart":
                with self.store.locked() as state:
                    state["profile"]["diet"]["avoid"] = ["salmon"]
        self.provider.on_call = change
        result = self.apply(ready)
        self.assertFalse(result["applied"])
        self.assertTrue(result["product_plan_stale"])
        self.assertIn("managed_product_apply_fence", self.store.read())
        self.assertNotIn("manipulate_cart", [tool for tool, _, _ in self.provider.calls])

    def test_shared_sku_different_queries_reads_detail_once_each_cycle(self):
        refs = self.save_recipes([recipe("Shared", [("spinach", 100), ("baby spinach", 100)])])
        self.menu = self.app.handle({"operation": "menu", "action": "save", "menu": {"week": "2026-W37", "dishes": refs, "salads": []}})["menu"]
        for query in ("spinach", "baby spinach"):
            self.provider.catalog[query] = observation(query, [product("1", "Spinach", 200, "g", [option(100)])])
        with self.store.locked() as state:
            state["profile"]["diet"]["avoid"] = ["peanuts"]
        reads = []
        self.provider.product_dietary_evidence = lambda ref, **kw: reads.append(ref) or {"unavailable": "exact_public_product_detail_unavailable"}
        initial = self.request(action="prepare", menu_ref=self.app._cart_menu_ref(self.menu))
        ids = [row["requirement_id"] for row in initial["product_plan"]["requirements"]]
        approvals = [{"requirement_id": row["requirement_id"], "candidate_refs": ["1"], "search_query": row["identity"],
                      "shared_package": {"requirement_ids": ids, "package_count": 1, "quantity_basis": "One 200g bag covers both 100g needs"}}
                     for row in initial["product_plan"]["requirements"]]
        prepared = self.request(action="prepare", product_plan_ref=initial["product_plan_ref"], candidate_approvals=approvals)
        self.assertEqual(prepared["product_plan"]["status"], "prepared")
        self.assertEqual(reads, ["1"])
        self.provider.calls.clear()
        result = self.request(action="apply", product_plan_ref=prepared["product_plan_ref"],
                              product_plan_digest=prepared["product_plan"]["product_plan_digest"], cart_change_requested=True)
        self.assertTrue(result["applied"])
        self.assertEqual(reads, ["1", "1"])
        self.assertEqual(sum(tool == "product_search" for tool, _, _ in self.provider.calls), 2)
        self.assertEqual(self.provider.quantities, {"1": 1})

    def test_detail_budget_pending_then_empty_completed_evidence_and_profile_reevaluation(self):
        with self.store.locked() as state:
            state["profile"]["diet"]["avoid"] = ["peanuts"]
        self.save_week()
        first = self.request(action="prepare", menu_ref=self.app._cart_menu_ref(self.menu))
        row = first["product_plan"]["requirements"][0]
        approval = {"requirement_id": row["requirement_id"], "candidate_refs": [self.provider.entry(row["identity"])["products"][0]["product_ref"]]}
        self.provider.product_dietary_evidence = lambda ref, **kw: {"unavailable": "detail_time_budget_exhausted"}
        pending = self.request(action="prepare", product_plan_ref=first["product_plan_ref"], candidate_approvals=[approval])
        self.assertIn("continue_arguments", pending)
        self.provider.product_dietary_evidence = lambda ref, **kw: {}
        complete = self.request(**pending["continue_arguments"])
        self.assertFalse("continue_arguments" in complete)
        self.assertEqual(complete["product_plan"]["requirements"][0]["status"], "selected")
        self.provider.product_dietary_evidence = lambda *a, **kw: self.fail("completed detail should be reused")
        with self.store.locked() as state:
            state["profile"]["diet"]["rules"] = [{"kind": "never_buy", "term": row["identity"]}]
        changed = self.request(action="prepare", product_plan_ref=complete["product_plan_ref"])
        self.assertEqual(changed["product_plan"]["requirements"][0]["status"], "needs_input")

    def test_changed_menu_during_validation_never_writes(self):
        self.save_week()
        ready = self.complete()
        def change(tool, args):
            if tool == "product_search":
                with self.store.locked() as state:
                    state["menu"]["revision"] += 1
                self.provider.on_call = None
        self.provider.on_call = change
        with self.assertRaisesRegex(HouseholdError, "changed|stale"):
            self.apply(ready)
        self.assertFalse(self.provider.quantities)

    def test_recovering_tail_is_retried_after_every_query_has_failed(self):
        self.save_week()
        counts = {}
        original = self.provider.call
        def flaky(tool, args, **kw):
            if tool == "product_search":
                query = args["queries"][0]
                counts[query] = counts.get(query, 0) + 1
                if query == "salmon" or counts[query] == 1:
                    raise HouseholdError("temporarily unavailable")
            return original(tool, args, **kw)
        self.provider.call = flaky
        with mock.patch("planning_operations.MAX_REQUIREMENTS", 1):
            result = self.request(action="prepare", menu_ref=self.app._cart_menu_ref(self.menu))
            for _ in range(74):
                result = self.request(**result["continue_arguments"])
        self.assertGreaterEqual(min(counts.values()), 2)
        self.assertEqual(sum("observation" in row for row in result["product_plan"]["requirements"]), 36)

    def test_expired_validation_restarts_only_facts_and_retains_review(self):
        self.save_week()
        ready = self.request(action="prepare", menu_ref=self.app._cart_menu_ref(self.menu))
        ready = self.request(action="prepare", product_plan_ref=ready["product_plan_ref"],
                             candidate_approvals=self.approvals(ready["product_plan"]))
        now = self.app._now()
        with mock.patch("planning_operations.MAX_REQUIREMENTS", 1), mock.patch.object(Application, "_now", return_value=now):
            partial = self.request(action="apply", product_plan_ref=ready["product_plan_ref"],
                                   product_plan_digest=ready["product_plan"]["product_plan_digest"], cart_change_requested=True)
        self.provider.calls.clear()
        with mock.patch("planning_operations.MAX_REQUIREMENTS", 1), mock.patch.object(Application, "_now", return_value=now + timedelta(hours=2)):
            resumed = self.request(**partial["continue_arguments"])
        self.assertEqual(resumed["product_plan_ref"], partial["product_plan_ref"])
        self.assertTrue(resumed["validation_progress"]["restarted"])
        self.assertEqual(resumed["validation_progress"]["pending_search_count"], 36)
        self.assertEqual(self.provider.calls[0][1]["queries"], [ready["product_plan"]["requirements"][0]["identity"]])
        self.assertFalse(self.provider.quantities)

    def test_malformed_detail_result_is_not_completed_unknown_evidence(self):
        self.save_week()
        with self.store.locked() as state:
            state["profile"]["diet"]["avoid"] = ["peanuts"]
        initial = self.request(action="prepare", menu_ref=self.app._cart_menu_ref(self.menu))
        approval = self.approvals(initial["product_plan"])[0]
        self.provider.product_dietary_evidence = lambda *args, **kwargs: []
        with self.assertRaisesRegex(HouseholdError, "invalid result"):
            self.request(action="prepare", product_plan_ref=initial["product_plan_ref"], candidate_approvals=[approval])
        self.assertFalse(self.provider.quantities)

    def test_validation_expiry_during_final_read_keeps_one_review_without_write(self):
        self.save_week()
        initial = self.request(action="prepare", menu_ref=self.app._cart_menu_ref(self.menu))
        ready = self.request(action="prepare", product_plan_ref=initial["product_plan_ref"],
                             candidate_approvals=self.approvals(initial["product_plan"]))
        now = self.app._now()
        with mock.patch("planning_operations.MAX_REQUIREMENTS", 36), mock.patch.object(Application, "_now", return_value=now):
            partial = self.request(action="apply", product_plan_ref=ready["product_plan_ref"],
                                   product_plan_digest=ready["product_plan"]["product_plan_digest"], cart_change_requested=True)
        clock = [now + timedelta(seconds=3590)]
        def advance(tool, args):
            if tool == "product_search":
                clock[0] += timedelta(seconds=20)
        self.provider.on_call = advance
        with mock.patch.object(Application, "_now", side_effect=lambda: clock[0]):
            result = self.request(**partial["continue_arguments"])
        self.assertEqual(result["status"], "validating")
        self.assertTrue(result["validation_progress"]["restarted"])
        self.assertEqual(result["product_plan_ref"], ready["product_plan_ref"])
        self.assertFalse(self.provider.quantities)
        self.provider.on_call = None
        self.assertTrue(self.request(**result["continue_arguments"])["applied"])
