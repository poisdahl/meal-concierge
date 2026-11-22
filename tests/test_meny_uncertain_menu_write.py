"""An uncertain menu-driven MENY cart write can be settled by the household."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore, cart_summary
from service import Application
import test_meal_concierge as base


class UncertainMenuMenyWriteTests(unittest.TestCase):
    def household(self, *, landed):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, provider, browser, app, product = base.CartPlanTests.app(temp.name, "meny")
        original_call = provider.call
        self.writes = []
        self.lost = True

        def call(tool, arguments, **kwargs):
            if tool == "manipulate_cart" and self.lost:
                self.writes.append(deepcopy(arguments))
                if landed:
                    # One click reached MENY before the response was lost.
                    provider._mutate_cart({"operations": [dict(arguments["operations"][0], quantity=1)]})
                raise HouseholdError("transport response lost")
            cart = original_call(tool, arguments, **kwargs)
            if tool == "get_cart" and arguments.get("observe_order_mode"):
                return {**cart, "ordinary_cart": True}
            return cart

        provider.call = call
        return store, provider, browser, app, product

    def quantities(self, app, provider):
        return app._cart_lines(cart_summary(provider.cart))[0]

    def uncertain_sync(self, app, product, quantity):
        result = base.CartPlanTests.sync(app, product, quantity)
        self.assertEqual(result["reason"], "cart_write_result_uncertain")
        self.assertTrue(result["outcome_unknown"])
        pending = app.store.read()["pending_cart_change"]
        self.assertTrue(pending["managed_menu"])
        self.assertFalse(pending.get("dispatch_finished"))
        self.assertFalse(pending.get("stopped_before_next_click"))
        return pending

    def test_household_can_accept_the_observed_cart_and_finish_the_menu_without_resending(self):
        for landed in (False, True):
            with self.subTest(landed=landed):
                store, provider, browser, app, product = self.household(landed=landed)
                start = self.quantities(app, provider)[product]
                journal = self.uncertain_sync(app, product, start + 2)
                # A restart keeps the original journal and offers the observed cart.
                app = Application(StateStore(store.directory, store.config), provider, browser)
                shown = app.handle({"operation": "cart", "action": "reconcile_change"})
                self.assertTrue(shown["accept_current_available"])
                self.assertTrue(shown["outcome_unknown"])
                self.assertEqual(shown["operation_id"], journal["operation_id"])
                self.assertTrue(app.handle({"operation": "cart", "action": "get"})["accept_current_available"])
                with self.assertRaisesRegex(HouseholdError, "reconcile_change"):
                    base.CartPlanTests.sync(app, product, start + 2)
                accepted = app.handle({"operation": "cart", "action": "accept_current",
                                       "operation_id": shown["operation_id"], "cart_digest": shown["cart_digest"]})
                self.assertEqual(accepted["status"], "owner_accepted_cart")
                self.assertIn("never resend", accepted["next"].lower())
                state = store.read()
                self.assertFalse(state.get("pending_cart_change"))
                receipt = state["cart_change_receipts"][journal["operation_id"]]
                self.assertEqual(receipt["original_operation"], journal)
                self.assertEqual(receipt["dispatch_outcome"], "unknown")
                # Acceptance settles the journal, not the menu: the plan asks for
                # a review of the observed cart, and the lost click is only adoptable.
                plan = state["cart_plan"]
                self.assertEqual(plan["status"], "needs_input")
                self.assertIsNone(plan["approved_cart_digest"])
                self.assertEqual(plan["pending_cart_digest"], accepted["cart_digest"])
                self.assertEqual(plan["added_quantities"].get(product, 0), 0)
                if landed:
                    self.assertEqual(plan["ownership_review"]["maximum_adoptable"], {product: 1})
                else:
                    self.assertNotIn("ownership_review", plan)
                self.assertEqual(len(self.writes), 1)
                self.lost = False
                question = base.CartPlanTests.sync(app, product, start + 2)
                self.assertFalse(question["synced"])
                self.assertEqual(question["cart_plan"]["cart_digest"], accepted["cart_digest"])
                self.assertEqual(len(self.writes), 1)
                restored = app.handle({"operation": "cart", "action": "reconcile", "decision": "restore_missing",
                                       "menu_ref": journal["managed_menu"], "cart_digest": accepted["cart_digest"]})
                self.assertTrue(restored["reconciled"])
                self.assertEqual(self.quantities(app, provider)[product], start + 2)

    def test_proven_endings_order_edits_and_native_bindings_keep_their_own_paths(self):
        store, provider, browser, app, product = self.household(landed=False)
        journal = self.uncertain_sync(app, product, 3)
        shown = app.handle({"operation": "cart", "action": "reconcile_change"})
        accept = {"operation": "cart", "action": "accept_current", "operation_id": shown["operation_id"],
                  "cart_digest": shown["cart_digest"]}
        for field, value in [("dispatch_finished", True), ("stopped_before_next_click", True),
                             ("order_change", {"order_id": "123"}), ("native_cart_binding", {"ref": "bound"})]:
            with self.subTest(journal_field=field):
                with store.locked() as state:
                    state["pending_cart_change"] = {**journal, field: value}
                self.assertNotIn("accept_current_available", app.handle({"operation": "cart", "action": "get"}))
                with self.assertRaises(HouseholdError):
                    app.handle(accept)
                self.assertEqual(store.read()["pending_cart_change"], {**journal, field: value})
        with store.locked() as state:
            state["pending_cart_change"] = deepcopy(journal)
        self.assertEqual(app.handle(accept)["status"], "owner_accepted_cart")


class MenyClickDeadlineTests(unittest.TestCase):
    def test_a_unit_that_cannot_be_read_back_before_the_deadline_is_never_clicked(self):
        import time
        from unittest import mock
        from meny import MENY_POST_CLICK_SECONDS, MenyCartStoppedError
        client = base.MenyClientTests().client()
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._sleep = mock.Mock()
        client._product_control = mock.Mock(return_value={"authenticated": True, "ready": True, "quantity": 0, "label": "add"})
        dispatched = []

        def click(_product, _label, before_dispatch, _same_control):
            before_dispatch()
            dispatched.append("mouse down")

        client._click_cart_control = mock.Mock(side_effect=click)
        # Enough time to open the product page, too little to confirm a click.
        client.deadline = time.monotonic() + MENY_POST_CLICK_SECONDS - 5
        with self.assertRaises(MenyCartStoppedError) as caught:
            client._change_cart({"operations": [{"productId": base.MENY_PRODUCT, "quantity": 1}]})
        self.assertEqual(caught.exception.applied_operations, [])
        self.assertEqual(dispatched, [])
        client.deadline = time.monotonic() + MENY_POST_CLICK_SECONDS + 60
        client._read_settled_cart = mock.Mock(return_value={"items": []})
        client._wait_for_quantity = mock.Mock(return_value=1)
        client._resolve_order_route = mock.Mock()
        client._change_cart({"operations": [{"productId": base.MENY_PRODUCT, "quantity": 1}]})
        self.assertEqual(dispatched, ["mouse down"])


if __name__ == "__main__":
    unittest.main()
