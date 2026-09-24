"""A new menu must not rewrite the frozen purchase or its recipe reservation."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError, StateStore
import planner
from service_common import menu_digest
from service import Application
from test_meal_concierge_recipes import CONFIG, FakeBrowser, FakeOda, full_recipe
import test_payment_recovery as recovery_fixtures


class MenuDuringPaymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = StateStore(Path(temporary.name), CONFIG)
        self.provider = FakeOda()
        browser = FakeBrowser()
        browser.oda = self.provider
        self.app = Application(self.store, self.provider, browser)
        self.original = self.app.handle({"operation": "menu", "action": "save", "menu": {
            "week": "2026-W40", "dishes": [full_recipe("Original fish")], "salads": [],
        }})["menu"]
        self.original_id = self.original["menu_id"]
        self.pending = {
            "status": "uncertain", "confirmation_id": "original-payment",
            "menu": deepcopy(self.original), "cart_plan": {
                "menu_ref": self.app._cart_menu_ref(self.original),
                "product_plan_digest": "frozen-product-plan",
            },
            "summary": {"menu_attribution": "menu_bound", "items": [
                {"product_id": "10", "name": "Fish", "quantity": 1}],
                "total": 10.0, "delivery": {"display": "Synthetic window", "address": "Test street"}},
        }
        with self.store.locked() as state:
            state["pending_checkout"] = deepcopy(self.pending)
            state["cart_plan"] = deepcopy(self.pending["cart_plan"])

    def save_new(self, name="Next fish"):
        return self.app.handle({"operation": "menu", "action": "save",
                                "menu_ref": self.app._cart_menu_ref(self.store.read()["menu"]),
                                "menu": {"week": "2026-W40", "dishes": [full_recipe(name)], "salads": []}})["menu"]

    def agent_input(self, name):
        recipe = full_recipe(name, external_id=name.lower().replace(" ", "-"))
        recipe["ingredients"] = recipe["ingredients"][:1]
        recipe["times"] = {"active_minutes": 30}
        saved = self.app.handle({"operation": "recipes", "action": "save",
                                 "recipe": recipe, "idempotency_key": "save-" + name})["recipe"]
        return {"week": "2026-W40", "dates": ["2026-09-28"],
                "selection_mode": "agent",
                "candidates": [{"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}}]}

    def allow_single_dinner_plan(self):
        with self.store.locked() as state:
            state["profile"]["meals"]["dinner_days"] = 1
            for target in planner.SAVED_MINIMUM_TARGETS:
                state["profile"]["diet"][target] = 0

    def planned_menu(self, name, *, menu_ref=None):
        planned = self.app.handle({"operation": "menu", "action": "plan",
                                   "planner_input": self.agent_input(name)})["plan"]
        self.assertEqual(planned["status"], "planned")
        return self.app.handle({"operation": "menu", "action": "save",
                                "planner_ref": planned["save_ref"],
                                "menu_ref": menu_ref})["menu"]

    def independent_addition(self):
        self.allow_single_dinner_plan()
        with self.store.locked() as state:
            state["pending_checkout"] = None
            state["cart_plan"] = None
        ordered = self.planned_menu("Ordered fish", menu_ref=self.app._cart_menu_ref(self.original))
        order_id = "synthetic-order"
        with self.store.locked() as state:
            pending = deepcopy(self.pending)
            pending["menu"] = deepcopy(ordered)
            pending["cart_plan"]["menu_ref"] = self.app._cart_menu_ref(ordered)
            state["pending_checkout"] = pending
            state["cart_plan"] = deepcopy(pending["cart_plan"])
        newer = self.planned_menu("Independent fish", menu_ref=self.app._cart_menu_ref(ordered))
        with self.store.locked() as state:
            snapshot = deepcopy(ordered)
            snapshot.update(phase="ordered", order_id=order_id)
            state["order_snapshots"][order_id] = snapshot
            state["recipe_usage"][ordered["menu_id"]].update(status="ordered", order_id=order_id)
            change = {"provider": "oda", "status": "editing", "order_id": order_id,
                      "before": {"order": {"orderNumber": order_id}}}
            state["order_change"] = deepcopy(change)
            pending = deepcopy(self.pending)
            pending.update(menu=deepcopy(newer), order_change=deepcopy(change),
                           cart_plan={"product_plan_digest": "frozen-addition-plan",
                                      "approved_cart_digest": "frozen-cart",
                                      "menu_ref": self.app._cart_menu_ref(newer)})
            pending["summary"]["menu_attribution"] = "cart_only"
            state["pending_checkout"] = pending
            state["cart_plan"] = deepcopy(pending["cart_plan"])
        return ordered, newer

    @mock.patch("service.now", new=lambda: datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    def test_planner_draft_needs_current_reference_and_preserves_pending_purchase(self):
        self.allow_single_dinner_plan()
        planned = self.app.handle({"operation": "menu", "action": "plan",
                                   "planner_input": self.agent_input("Independent fish")})["plan"]
        self.assertEqual(planned["status"], "planned")
        before = self.store.read()
        with self.assertRaises(HouseholdError):
            self.app.handle({"operation": "menu", "action": "save",
                             "planner_ref": planned["save_ref"]})
        self.assertEqual(self.store.read(), before)
        current = self.app.handle({"operation": "menu", "action": "get"})["menu"]
        saved = self.app.handle({"operation": "menu", "action": "save",
                                 "planner_ref": planned["save_ref"],
                                 "menu_ref": self.app._cart_menu_ref(current)})["menu"]
        after = self.store.read()
        self.assertNotEqual(saved["menu_id"], current["menu_id"])
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(after["pending_checkout"], before["pending_checkout"])
        self.assertEqual(after["cart_plan"], before["cart_plan"])
        self.assertEqual(after["recipe_usage"][current["menu_id"]],
                         before["recipe_usage"][current["menu_id"]])

    @mock.patch("service.now", new=lambda: datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    def test_targeted_replan_remains_blocked_without_mutating_frozen_checkout(self):
        self.allow_single_dinner_plan()
        with self.store.locked() as state:
            state["pending_checkout"] = None
            state["cart_plan"] = None
        first_input = self.agent_input("Planner first")
        first_plan = self.app.handle({"operation": "menu", "action": "plan",
                                      "planner_input": first_input})["plan"]
        self.assertEqual(first_plan["status"], "planned")
        source = self.app.handle({"operation": "menu", "action": "save",
                                  "planner_ref": first_plan["save_ref"],
                                  "menu_ref": self.app._cart_menu_ref(self.original)})["menu"]
        with self.store.locked() as state:
            pending = deepcopy(self.pending)
            pending["menu"] = deepcopy(source)
            pending["cart_plan"]["menu_ref"] = self.app._cart_menu_ref(source)
            state["pending_checkout"] = pending
            state["cart_plan"] = deepcopy(pending["cart_plan"])
        replacement = self.agent_input("Planner second")
        prepared = self.app.handle({"operation": "menu", "action": "replan_prepare",
                                    "menu_ref": self.app._cart_menu_ref(source),
                                    "remaining_dates": ["2026-09-28"],
                                    "planner_input": replacement})
        self.assertEqual(prepared["replan"]["status"], "prepared")
        before = self.store.read()
        with self.assertRaises(HouseholdError) as waiting_error:
            self.app.handle({"operation": "menu", **prepared["apply_arguments"]})
        self.assertIn("linked", str(waiting_error.exception))
        self.assertEqual(self.store.read(), before)

        def rejected_replan_during_blocked_save():
            fresh = self.app.handle({"operation": "menu", "action": "replan_prepare",
                                     "menu_ref": self.app._cart_menu_ref(source),
                                     "remaining_dates": ["2026-09-28"],
                                     "planner_input": replacement})
            blocked_before = self.store.read()
            with self.assertRaises(HouseholdError) as replan_error:
                self.app.handle({"operation": "menu", **fresh["apply_arguments"]})
            self.assertNotIn("menu plan", str(replan_error.exception))
            self.assertEqual(self.store.read(), blocked_before)

        with self.store.locked() as state:
            state["pending_checkout"]["recovery"] = {"status": "awaiting_confirmation"}
        rejected_replan_during_blocked_save()
        with self.store.locked() as state:
            state["pending_checkout"]["recovery"] = {"status": "clicking"}
        rejected_replan_during_blocked_save()
        with self.store.locked() as state:
            state["pending_checkout"].pop("recovery")
        fresh = self.app.handle({"operation": "menu", "action": "replan_prepare",
                                 "menu_ref": self.app._cart_menu_ref(source),
                                 "remaining_dates": ["2026-09-28"],
                                 "planner_input": replacement})
        handler_before = self.store.read()
        self.app.browser_lock.acquire()
        try:
            with self.assertRaises(HouseholdError) as handler_error:
                self.app.handle({"operation": "menu", **fresh["apply_arguments"]})
            self.assertNotIn("menu plan", str(handler_error.exception))
        finally:
            self.app.browser_lock.release()
        self.assertEqual(self.store.read(), handler_before)

        with self.store.locked() as state:
            state["pending_checkout"] = None
            state["cart_plan"] = None
        ordinary = self.app.handle({"operation": "menu", "action": "replan_prepare",
                                    "menu_ref": self.app._cart_menu_ref(source),
                                    "remaining_dates": ["2026-09-28"],
                                    "planner_input": replacement})
        applied = self.app.handle({"operation": "menu", **ordinary["apply_arguments"]})
        self.assertNotEqual(applied["menu"]["menu_id"], source["menu_id"])

    def test_post_dispatch_save_preserves_frozen_checkout_and_settles_original_usage(self):
        newer = self.save_new()
        state = self.store.read()
        self.assertNotEqual(newer["menu_id"], self.original_id)
        self.assertEqual(newer["revision"], 1)
        self.assertEqual(state["pending_checkout"], self.pending)
        self.assertEqual(state["cart_plan"], self.pending["cart_plan"])
        self.assertEqual(state["recipe_usage"][self.original_id]["status"], "planned")
        self.assertNotIn(self.original_id, state["menu_planning"]["retired"])
        self.assertEqual(state["recipe_usage"][newer["menu_id"]]["status"], "planned")
        with self.assertRaisesRegex(HouseholdError, "cooldown"):
            self.save_new("Original fish")
        with self.store.locked() as locked:
            locked["pending_checkout"] = None
            self.app._record_order_snapshot(locked, self.pending, "order-1")
        settled = self.store.read()
        self.assertEqual(settled["order_snapshots"]["order-1"]["menu_id"], self.original_id)
        self.assertEqual(settled["recipe_usage"][self.original_id]["status"], "ordered")
        self.assertEqual(settled["recipe_usage"][self.original_id]["order_id"], "order-1")
        self.assertEqual(settled["menu"], newer)

    def test_verified_cancel_releases_only_detached_reservation(self):
        newer = self.save_new()
        with self.store.locked() as state:
            self.app._archive_cancelled_checkout(state, self.pending, "order-1")
        settled = self.store.read()
        self.assertIsNone(settled["pending_checkout"])
        self.assertEqual(settled["recipe_usage"][self.original_id]["status"], "cancelled")
        self.assertEqual(settled["recipe_usage"][newer["menu_id"]]["status"], "planned")
        with self.store.locked() as state:
            state["recipe_usage"][self.original_id]["status"] = "ordered"
            state["recipe_usage"][self.original_id]["order_id"] = "order-1"
            state["order_snapshots"]["order-1"] = deepcopy(self.original)
            self.app._mark_order_cancelled(state, "order-1", provider="oda", active_provider="oda")
        self.assertEqual(self.store.read()["recipe_usage"][self.original_id]["status"], "cancelled")

    def test_carried_slot_owner_is_not_released_on_later_order_cancel(self):
        newer = self.save_new()
        with self.store.locked() as state:
            state["menu"]["slots"] = [{"slot_id": "carried", "recipe_key": self.original["dishes"][0]["recipe_key"]}]
            state["menu"]["slot_owners"] = {"carried": self.original_id}
            self.app._release_detached_checkout_usage(state, self.pending)
            self.assertEqual(state["recipe_usage"][self.original_id]["status"], "planned")
            state["recipe_usage"][self.original_id].update(status="ordered", order_id="order-1")
            state["order_snapshots"]["order-1"] = deepcopy(self.original)
            self.app._mark_order_cancelled(state, "order-1", provider="oda", active_provider="oda")
        state = self.store.read()
        self.assertEqual(state["recipe_usage"][self.original_id]["status"], "planned")
        self.assertEqual(state["menu"]["menu_id"], newer["menu_id"])

    def test_detached_inherited_owner_is_released_after_terminal_cancel(self):
        inherited_id = "menu_inherited_source"
        with self.store.locked() as state:
            state["recipe_usage"][inherited_id] = {
                "week": "2026-W40", "status": "planned", "recipe_keys": ["inherited"],
                "cooked_keys": [], "not_cooked_keys": [], "order_id": None,
            }
            state["pending_checkout"]["menu"]["slot_owners"] = {"carried": inherited_id}
        pending = deepcopy(self.store.read()["pending_checkout"])
        self.save_new()
        with self.store.locked() as state:
            self.app._archive_cancelled_checkout(state, pending, "order-1")
        self.assertEqual(self.store.read()["recipe_usage"][inherited_id]["status"], "cancelled")

    def test_cancel_releases_unmade_inherited_slots_but_keeps_cooked_history(self):
        inherited_id = "menu_inherited_cooking"
        cooked_key = self.original["dishes"][0]["recipe_key"]
        future_key = "bank:uncooked-future-recipe"
        with self.store.locked() as state:
            state["recipe_usage"][inherited_id] = {
                "week": "2026-W40", "status": "planned",
                "recipe_keys": [cooked_key, future_key],
                "cooked_keys": [cooked_key], "not_cooked_keys": [], "order_id": None,
            }
            state["pending_checkout"]["menu"]["slot_owners"] = {"future": inherited_id}
        pending = deepcopy(self.store.read()["pending_checkout"])
        self.save_new()
        with self.store.locked() as state:
            self.app._archive_cancelled_checkout(state, pending, "order-1")
            cooked = self.app._usage_summary(state, cooked_key, "2026-W41")
            future = self.app._usage_summary(state, future_key, "2026-W41")
        self.assertEqual(self.store.read()["recipe_usage"][inherited_id]["status"], "cancelled")
        self.assertFalse(cooked["eligible"])
        self.assertTrue(future["eligible"])

    def test_later_cancel_of_paid_frozen_order_releases_inherited_source_owner(self):
        inherited_id = "menu_inherited_paid"
        with self.store.locked() as state:
            state["recipe_usage"][inherited_id] = {
                "week": "2026-W40", "status": "planned", "recipe_keys": ["bank:inherited"],
                "cooked_keys": [], "not_cooked_keys": [], "order_id": None,
            }
            state["pending_checkout"]["menu"]["slot_owners"] = {"carried": inherited_id}
        pending = deepcopy(self.store.read()["pending_checkout"])
        self.save_new()
        with self.store.locked() as state:
            self.app._record_order_snapshot(state, pending, "order-1")
        self.assertEqual(self.store.read()["recipe_usage"][inherited_id]["status"], "planned")
        with self.store.locked() as state:
            self.app._mark_order_cancelled(state, "order-1", provider="oda", active_provider="oda")
        state = self.store.read()
        self.assertEqual(state["recipe_usage"][inherited_id]["status"], "cancelled")
        self.assertEqual(state["order_snapshots"]["order-1"]["slot_owners"], {"carried": inherited_id})

    def test_running_click_and_prepared_recovery_still_block_menu_save(self):
        with self.store.locked() as state:
            state["pending_checkout"]["status"] = "clicking"
        frozen = deepcopy(self.store.read()["pending_checkout"])
        self.save_new()
        self.assertEqual(self.store.read()["pending_checkout"], frozen)
        with self.store.locked() as state:
            state["pending_checkout"]["status"] = "uncertain"
            state["pending_checkout"]["recovery"] = {"status": "awaiting_confirmation"}
        frozen = deepcopy(self.store.read()["pending_checkout"])
        self.save_new("Another independent fish")
        self.assertEqual(self.store.read()["pending_checkout"], frozen)
        with self.store.locked() as state:
            state["pending_checkout"] = None
            state["pending_cancellation"] = {"confirmation_id": "cancellation-review"}
        with self.assertRaisesRegex(HouseholdError, "pending order cancellation"):
            self.save_new()
        self.assertNotEqual(self.store.read()["menu"]["menu_id"], self.original_id)

    def test_retained_post_submit_card_child_can_save_after_browser_handler_finishes(self):
        with self.store.locked() as state:
            state["pending_checkout"]["recovery"] = {
                "status": "clicking", "authentication_context": {"tab_id": "retained-3ds"}}
        self.assertNotEqual(self.save_new()["menu_id"], self.original_id)

    def test_browser_handler_lock_allows_proven_independent_menu_only(self):
        frozen = deepcopy(self.store.read()["pending_checkout"])
        self.app.browser_lock.acquire()
        try:
            newer = self.save_new()
        finally:
            self.app.browser_lock.release()
        self.assertNotEqual(newer["menu_id"], self.original_id)
        self.assertEqual(self.store.read()["pending_checkout"], frozen)

    @mock.patch("service.now", new=lambda: datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    def test_addition_pending_menu_can_be_newer_menu_and_replanned_twice(self):
        ordered, newer = self.independent_addition()
        frozen = self.store.read()
        self.assertEqual(frozen["pending_checkout"]["menu"]["menu_id"], newer["menu_id"])
        for name in ("First replacement", "Second replacement"):
            source = self.store.read()["menu"]
            prepared = self.app.handle({"operation": "menu", "action": "replan_prepare",
                "menu_ref": self.app._cart_menu_ref(source), "remaining_dates": ["2026-09-28"],
                "planner_input": self.agent_input(name)})
            self.assertEqual(prepared["replan"]["status"], "prepared")
            self.app.browser_lock.acquire()
            try:
                applied = self.app.handle({"operation": "menu", **prepared["apply_arguments"]})["menu"]
            finally:
                self.app.browser_lock.release()
            self.assertNotEqual(applied["menu_id"], source["menu_id"])
            current = self.store.read()
            for key in ("pending_checkout", "order_change", "cart_plan"):
                self.assertEqual(current[key], frozen[key])
            self.assertEqual(current["order_snapshots"], frozen["order_snapshots"])
            self.assertEqual(current["recipe_usage"][ordered["menu_id"]],
                             frozen["recipe_usage"][ordered["menu_id"]])
            self.assertNotIn(ordered["menu_id"], current["menu_planning"]["retired"])
        with self.store.locked() as state:
            state["pending_checkout"] = None
            state["order_change"] = None
        self.assertEqual(self.store.read()["menu"]["menu_id"], applied["menu_id"])

    @mock.patch("service.now", new=lambda: datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    def test_independent_save_preserves_prepared_addition_recovery_and_old_order(self):
        ordered, newer = self.independent_addition()
        with self.store.locked() as state:
            state["pending_checkout"]["recovery"] = {
                "status": "awaiting_confirmation", "confirmation_id": "synthetic-recovery"}
        before = self.store.read()
        self.app.browser_lock.acquire()
        try:
            saved = self.save_new("Fresh independent draft")
        finally:
            self.app.browser_lock.release()
        after = self.store.read()
        self.assertNotEqual(saved["menu_id"], newer["menu_id"])
        self.assertEqual(saved["revision"], 1)
        for key in ("pending_checkout", "order_change", "cart_plan", "order_snapshots"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["recipe_usage"][ordered["menu_id"]],
                         before["recipe_usage"][ordered["menu_id"]])
        self.assertNotIn(ordered["menu_id"], after["menu_planning"]["retired"])
        with self.store.locked() as state:
            state["pending_checkout"] = None
            state["order_change"] = None
            self.app._mark_order_cancelled(state, "synthetic-order",
                                           provider="oda", active_provider="oda")
        settled = self.store.read()
        self.assertEqual(settled["menu"], saved)
        self.assertEqual(settled["recipe_usage"][saved["menu_id"]]["status"], "planned")

    @mock.patch("service.now", new=lambda: datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    def test_addition_replan_preserves_own_predecessor_owner_and_rejects_stale_plan(self):
        ordered, newer = self.independent_addition()
        with self.store.locked() as state:
            state["pending_checkout"] = None
            state["order_change"] = None
            state["cart_plan"] = None
        recipe_ref = self.agent_input("Extra independent dish")["candidates"][0]["recipe_ref"]
        successor = self.app.handle({"operation": "menu", "action": "add_slot",
            "menu_ref": self.app._cart_menu_ref(newer),
            "slot_input": {"date": "2026-09-29", "meal_type": "dinner", "portions": 2,
                           "reference": {"recipe_ref": recipe_ref}},
            "idempotency_key": "synthetic-extra-meal"})["menu"]
        self.assertIn(newer["menu_id"], successor["slot_owners"].values())
        with self.store.locked() as state:
            change = deepcopy(self.pending["order_change"]) if "order_change" in self.pending else {
                "provider": "oda", "status": "editing", "order_id": "synthetic-order"}
            state["order_change"] = change
            pending = deepcopy(self.pending)
            pending.update(menu=deepcopy(successor), order_change=deepcopy(change),
                           cart_plan={"product_plan_digest": "still-frozen"})
            state["pending_checkout"] = pending
            state["cart_plan"] = deepcopy(pending["cart_plan"])
        replacement = self.agent_input("Another extra dish")
        replacement["dates"] = ["2026-09-29"]
        prepared = self.app.handle({"operation": "menu", "action": "replan_prepare",
            "menu_ref": self.app._cart_menu_ref(successor), "remaining_dates": ["2026-09-29"],
            "planner_input": replacement})
        self.assertEqual(prepared["replan"]["status"], "prepared")
        frozen = self.store.read()
        with self.store.locked() as state:
            state["profile"]["meals"]["portions"] = 3
        with self.assertRaisesRegex(HouseholdError, "state changed|stale"):
            self.app.handle({"operation": "menu", **prepared["apply_arguments"]})
        with self.store.locked() as state:
            state["profile"]["meals"]["portions"] = frozen["profile"]["meals"]["portions"]
        fresh = self.app.handle({"operation": "menu", "action": "replan_prepare",
            "menu_ref": self.app._cart_menu_ref(successor), "remaining_dates": ["2026-09-29"],
            "planner_input": replacement})
        applied = self.app.handle({"operation": "menu", **fresh["apply_arguments"]})["menu"]
        self.assertEqual(applied["slot_owners"][newer["slots"][0]["slot_id"]], newer["menu_id"])
        self.assertEqual(self.store.read()["recipe_usage"][ordered["menu_id"]],
                         frozen["recipe_usage"][ordered["menu_id"]])

    @mock.patch("service.now", new=lambda: datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    def test_addition_linked_or_ambiguous_menu_cannot_replan(self):
        ordered, newer = self.independent_addition()
        snapshot = deepcopy(self.store.read()["order_snapshots"]["synthetic-order"])
        for defect in ("owner", "slot", "missing_snapshot", "missing_usage", "changed_change"):
            with self.subTest(defect=defect):
                with self.store.locked() as state:
                    state["menu"] = deepcopy(newer)
                    state["order_snapshots"]["synthetic-order"] = deepcopy(snapshot)
                    state["recipe_usage"][ordered["menu_id"]]["order_id"] = "synthetic-order"
                    state["pending_checkout"]["order_change"] = deepcopy(state["order_change"])
                    if defect == "owner":
                        state["menu"]["slot_owners"] = {newer["slots"][0]["slot_id"]: ordered["menu_id"]}
                    elif defect == "slot":
                        state["menu"]["slots"][0]["slot_id"] = ordered["slots"][0]["slot_id"]
                    elif defect == "missing_snapshot":
                        state["order_snapshots"].pop("synthetic-order")
                    elif defect == "missing_usage":
                        state["recipe_usage"][ordered["menu_id"]]["order_id"] = None
                    else:
                        state["pending_checkout"]["order_change"]["order_id"] = "other-order"
                    state["menu"]["digest"] = menu_digest(state["menu"])
                before = self.store.read()
                prepared = self.app.handle({"operation": "menu", "action": "replan_prepare",
                    "menu_ref": self.app._cart_menu_ref(before["menu"]),
                    "remaining_dates": ["2026-09-28"], "planner_input": self.agent_input("Blocked replacement " + defect)})
                self.assertEqual(prepared["replan"]["status"], "prepared")
                with self.assertRaisesRegex(HouseholdError, "linked|unidentified"):
                    self.app.handle({"operation": "menu", **prepared["apply_arguments"]})
                self.assertEqual(self.store.read()["pending_checkout"], before["pending_checkout"])
                self.assertEqual(self.store.read()["cart_plan"], before["cart_plan"])
                if defect not in {"owner", "slot"}:
                    with self.assertRaisesRegex(HouseholdError, "linked|unidentified"):
                        self.save_new("Blocked new draft " + defect)
                    self.assertEqual(self.store.read()["pending_checkout"], before["pending_checkout"])

    @mock.patch("service.now", new=lambda: datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    def test_whole_save_cannot_retire_inherited_owner_shared_with_frozen_order(self):
        ordered, newer = self.independent_addition()
        inherited = "menu_shared_planned_owner"
        ordered_slot = ordered["slots"][0]
        newer_slot = newer["slots"][0]
        with self.store.locked() as state:
            state["order_snapshots"]["synthetic-order"]["slot_owners"] = {
                ordered_slot["slot_id"]: inherited}
            state["recipe_usage"][inherited] = {
                "week": ordered["week"], "status": "planned", "order_id": None,
                "recipe_keys": [ordered_slot["recipe_key"], newer_slot["recipe_key"]],
                "slots": [deepcopy(ordered_slot), deepcopy(newer_slot)],
                "cooked_keys": [], "not_cooked_keys": [],
            }
            state["menu"]["slot_owners"] = {newer_slot["slot_id"]: inherited}
            state["menu"]["digest"] = menu_digest(state["menu"])
        before = self.store.read()
        with self.assertRaisesRegex(HouseholdError, "shares protected order slots"):
            self.save_new("Another distinct whole menu")
        after = self.store.read()
        self.assertEqual(after, before)
        self.assertEqual(after["recipe_usage"][inherited], before["recipe_usage"][inherited])
        self.assertEqual(after["menu_planning"]["retired"], before["menu_planning"]["retired"])

    def test_real_recovery_reconcile_orders_frozen_menu_after_new_menu_save(self):
        fixture = recovery_fixtures.RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        with app.store.locked() as state:
            original_pending = deepcopy(state["pending_checkout"])
            state["pending_checkout"] = None
        original = app.handle({"operation": "menu", "action": "save", "menu": {
            "week": "2026-W40", "dishes": [full_recipe("Recovery fish")], "salads": [],
        }})["menu"]
        with app.store.locked() as state:
            original_pending["menu"] = deepcopy(original)
            original_pending["cart_plan"] = {"menu_ref": app._cart_menu_ref(original)}
            original_pending["summary"]["menu_attribution"] = "menu_bound"
            state["pending_checkout"] = original_pending
            state["cart_plan"] = deepcopy(original_pending["cart_plan"])
        prepared = fixture.prepare_saved_card_to_vipps_recovery()
        pending_before = deepcopy(app.store.read()["pending_checkout"])
        newer = app.handle({"operation": "menu", "action": "save",
                            "menu_ref": app._cart_menu_ref(original),
                            "menu": {"week": "2026-W40", "dishes": [full_recipe("Next recovery fish")],
                                     "salads": []}})["menu"]
        self.assertEqual(app.store.read()["pending_checkout"], pending_before)
        fixture.merchant.status = "paid_and_modifiable"
        result = fixture.call("reconcile", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(result["confirmed"])
        state = app.store.read()
        self.assertEqual(state["menu"], newer)
        self.assertEqual(state["order_snapshots"]["order-1"]["menu_id"], original["menu_id"])
        self.assertEqual(state["recipe_usage"][original["menu_id"]]["status"], "ordered")
        self.assertEqual(state["recipe_usage"][newer["menu_id"]]["status"], "planned")


if __name__ == "__main__":
    unittest.main()
