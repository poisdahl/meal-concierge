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
        self.assertIn("menu plan", str(waiting_error.exception))
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
            with self.assertRaises(HouseholdError):
                self.save_new()
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
            with self.assertRaises(HouseholdError):
                self.save_new()
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
        with self.assertRaisesRegex(HouseholdError, "active payment submission"):
            self.save_new()
        with self.store.locked() as state:
            state["pending_checkout"]["status"] = "uncertain"
            state["pending_checkout"]["recovery"] = {"status": "awaiting_confirmation"}
        with self.assertRaisesRegex(HouseholdError, "prepared recovery review"):
            self.save_new()
        with self.store.locked() as state:
            state["pending_checkout"] = None
            state["pending_cancellation"] = {"confirmation_id": "cancellation-review"}
        with self.assertRaisesRegex(HouseholdError, "pending order cancellation"):
            self.save_new()
        self.assertEqual(self.store.read()["menu"], self.original)

    def test_retained_post_submit_card_child_can_save_after_browser_handler_finishes(self):
        with self.store.locked() as state:
            state["pending_checkout"]["recovery"] = {
                "status": "clicking", "authentication_context": {"tab_id": "retained-3ds"}}
        self.assertNotEqual(self.save_new()["menu_id"], self.original_id)

    def test_browser_handler_lock_is_nonblocking_and_does_not_mutate_menu(self):
        self.app.browser_lock.acquire()
        try:
            with self.assertRaisesRegex(HouseholdError, "active provider operation"):
                self.save_new()
        finally:
            self.app.browser_lock.release()
        self.assertEqual(self.store.read()["menu"], self.original)

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
