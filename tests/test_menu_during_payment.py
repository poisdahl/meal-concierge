"""A new menu must not rewrite the frozen purchase or its recipe reservation."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError, StateStore
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
