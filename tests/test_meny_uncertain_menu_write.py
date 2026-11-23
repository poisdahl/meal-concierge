"""An uncertain menu-driven MENY cart write can be settled by the household."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore, cart_summary
from meny import MenyCartStoppedError
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
        self.assertEqual(result["operation_id"], pending["operation_id"])
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
                again = app.handle({"operation": "cart", "action": "accept_current",
                                    "operation_id": shown["operation_id"], "cart_digest": shown["cart_digest"]})
                self.assertEqual(again["status"], "previously_accepted")
                self.assertIn("never resend", again["next"].lower())
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


class StoppedMenuMenyBatchTests(unittest.TestCase):
    """A menu batch that stopped before its next click and was read back has nothing to settle."""

    def household(self, stops, *, household_edit=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, provider, _browser, app, product = base.CartPlanTests.app(temp.name, "meny")
        original = provider.call
        self.writes = 0

        def call(tool, arguments, **kwargs):
            if tool == "manipulate_cart":
                self.writes += 1
                if self.writes in stops:
                    # The adapter clicked these units, then stopped before its next click.
                    landed = [dict(operation, quantity=1) for operation in arguments["operations"]
                              for _ in range(operation["quantity"])][:stops[self.writes]]
                    if landed:
                        provider._mutate_cart({"operations": landed})
                    if household_edit:
                        provider._mutate_cart({"operations": [{"productId": product, "quantity": 1}]})
                    raise MenyCartStoppedError("MENY operation deadline reached", landed)
            return original(tool, arguments, **kwargs)

        provider.call = call
        return store, provider, app, product

    @staticmethod
    def quantity(app, provider, product):
        return app._cart_lines(cart_summary(provider.cart))[0].get(product, 0)

    def assert_nothing_to_settle(self, result, store):
        self.assertEqual(result["reason"], "cart_write_stopped_before_click")
        self.assertTrue(result["cart_reconciliation_required"])
        for key in ("outcome_unknown", "cart_write_pending", "operation_id"):
            self.assertNotIn(key, result)
        state = store.read()
        self.assertFalse(state.get("pending_cart_change"))
        self.assertEqual({receipt["status"] for receipt in state["cart_change_receipts"].values()}, {"read_back"})
        self.assertEqual(state["cart_plan"]["status"], "needs_input")

    def test_a_stopped_sync_that_was_read_back_is_reviewed_without_accepting_anything(self):
        # Stop after the first click of a same-product two-click batch, or before
        # the first click of a later batch.
        for stop_at, clicks, confirmed in ((1, 1, 1), (2, 0, 2)):
            with self.subTest(stop_at=stop_at, clicks=clicks):
                store, provider, app, product = self.household({stop_at: clicks})
                start = self.quantity(app, provider, product)
                stopped = base.CartPlanTests.sync(app, product, start + 4)
                self.assertIs(stopped["synced"], False)
                self.assert_nothing_to_settle(stopped, store)
                self.assertEqual(self.quantity(app, provider, product), start + confirmed)
                self.assertEqual(store.read()["cart_plan"]["added_quantities"], {product: confirmed})
                self.assertNotIn("accept_current_available", app.handle({"operation": "cart", "action": "get"}))
                restored = app.handle({"operation": "cart", "action": "reconcile", "decision": "restore_missing",
                                       "menu_ref": app._cart_menu_ref(store.read()["menu"]),
                                       "cart_digest": stopped["cart_plan"]["cart_digest"]})
                self.assertTrue(restored["reconciled"], restored)
                self.assertEqual(self.quantity(app, provider, product), start + 4)

    def test_a_stopped_reconcile_that_was_read_back_is_reviewed_again(self):
        store, provider, app, product = self.household({1: 1, 2: 1})
        start = self.quantity(app, provider, product)
        menu_ref = app._cart_menu_ref(store.read()["menu"])
        stopped = base.CartPlanTests.sync(app, product, start + 4)
        self.assert_nothing_to_settle(stopped, store)
        again = app.handle({"operation": "cart", "action": "reconcile", "decision": "restore_missing",
                            "menu_ref": menu_ref, "cart_digest": stopped["cart_plan"]["cart_digest"]})
        self.assertIs(again["reconciled"], False)
        self.assert_nothing_to_settle(again, store)
        self.assertEqual(self.quantity(app, provider, product), start + 2)
        restored = app.handle({"operation": "cart", "action": "reconcile", "decision": "restore_missing",
                               "menu_ref": menu_ref, "cart_digest": again["cart_plan"]["cart_digest"]})
        self.assertTrue(restored["reconciled"], restored)
        self.assertEqual(self.quantity(app, provider, product), start + 4)
        self.assertEqual(self.writes, 3)

    def test_a_stop_whose_clicks_are_not_read_back_stays_uncertain_with_its_operation(self):
        # The household changes the cart before the stopped batch is read back.
        store, provider, app, product = self.household({1: 1}, household_edit=True)
        start = self.quantity(app, provider, product)
        result = base.CartPlanTests.sync(app, product, start + 2)
        pending = store.read()["pending_cart_change"]
        self.assertTrue(pending["stopped_before_next_click"])
        self.assertIsNone(result["synced"])
        self.assertEqual(result["reason"], "cart_write_result_uncertain")
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(result["operation_id"], pending["operation_id"])


class MenyRecoveryEdgeTests(unittest.TestCase):
    def app(self, provider_name="meny"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return base.CartPlanTests.app(temp.name, provider_name)

    @staticmethod
    def observed(provider):
        original = provider.call

        def call(tool, arguments, **kwargs):
            cart = original(tool, arguments, **kwargs)
            if tool == "get_cart" and arguments.get("observe_order_mode"):
                return {**cart, "ordinary_cart": True}
            return cart
        return original, call

    def test_a_proven_stop_settles_by_readback_after_the_household_changes_the_cart(self):
        from meny import MenyCartStoppedError
        store, provider, _browser, app, product = self.app()
        original, observed = self.observed(provider)
        stage = {"stopped": False, "fail_read": False}

        def call(tool, arguments, **kwargs):
            if tool == "get_cart" and stage["fail_read"]:
                stage["fail_read"] = False
                raise HouseholdError("MENY operation deadline reached")
            if tool == "manipulate_cart" and not stage["stopped"]:
                stage["stopped"] = True
                first = dict(arguments["operations"][0], quantity=1)
                provider._mutate_cart({"operations": [first]})
                stage["fail_read"] = True  # the deadline also cuts the post-stop read
                raise MenyCartStoppedError("MENY operation deadline reached", [first])
            return observed(tool, arguments, **kwargs)

        provider.call = call
        start = app._cart_lines(cart_summary(provider.cart))[0][product]
        try:
            base.CartPlanTests.sync(app, product, start + 2)
        except HouseholdError:
            pass
        pending = store.read()["pending_cart_change"]
        self.assertTrue(pending["stopped_before_next_click"])
        self.assertFalse(pending.get("dispatch_finished"))
        provider._mutate_cart({"operations": [{"productId": product, "quantity": 1}]})  # household edit
        self.assertNotIn("accept_current_available", app.handle({"operation": "cart", "action": "get"}))
        settled = app.handle({"operation": "cart", "action": "reconcile_change"})
        self.assertTrue(settled["reconciled"], settled)
        state = store.read()
        self.assertFalse(state.get("pending_cart_change"))
        self.assertEqual(state["cart_plan"]["status"], "needs_input")
        self.assertEqual(state["cart_plan"]["added_quantities"][product], 1)

    def test_acceptance_after_a_menu_change_keeps_confirmed_units_and_points_to_the_current_menu(self):
        store, provider, _browser, app, product = self.app()
        original, observed = self.observed(provider)
        writes = []

        def call(tool, arguments, **kwargs):
            if tool == "manipulate_cart":
                writes.append(arguments)
                if len(writes) == 2:
                    raise HouseholdError("transport response lost")
            return observed(tool, arguments, **kwargs)

        provider.call = call
        start = app._cart_lines(cart_summary(provider.cart))[0][product]
        uncertain = base.CartPlanTests.sync(app, product, start + 4)
        self.assertEqual(uncertain["reason"], "cart_write_result_uncertain")
        with store.locked() as state:
            state["menu"] = base.CartPlanTests.menu(revision=2, digest="b" * 64)
        shown = app.handle({"operation": "cart", "action": "get"})
        accepted = app.handle({"operation": "cart", "action": "accept_current",
                               "operation_id": shown["operation_id"], "cart_digest": shown["cart_digest"]})
        self.assertIn("current menu", accepted["next"])
        self.assertEqual(store.read()["cart_plan"]["added_quantities"], {product: 2})
        provider.call = original
        synced = base.CartPlanTests.sync(app, product, start + 4)
        if not synced.get("synced"):
            app.handle({"operation": "cart", "action": "reconcile", "decision": "restore_missing",
                        "menu_ref": app._cart_menu_ref(store.read()["menu"]),
                        "cart_digest": synced["cart_plan"]["cart_digest"]})
        plan = store.read()["cart_plan"]
        self.assertEqual(app._cart_lines(cart_summary(provider.cart))[0][product], start + 4)
        self.assertEqual(plan["baseline_quantities"].get(product, 0), start)

    def test_oda_managed_acceptance_keeps_its_approve_as_is_result(self):
        store, provider, _browser, app, product = self.app("oda")
        original = provider.call

        def call(tool, arguments, **kwargs):
            if tool == "manipulate_cart":
                raise HouseholdError("transport response lost")
            return original(tool, arguments, **kwargs)

        provider.call = call
        base.CartPlanTests.sync(app, product, 3)
        provider.call = original
        shown = app.handle({"operation": "cart", "action": "get"})
        accepted = app.handle({"operation": "cart", "action": "accept_current",
                               "operation_id": shown["operation_id"], "cart_digest": shown["cart_digest"]})
        self.assertEqual(accepted["status"], "owner_accepted_cart")
        self.assertNotIn("next", accepted)
        self.assertEqual(store.read()["cart_plan"]["status"], "active")

    def test_the_partial_continuation_reference_is_internal_only(self):
        _store, _provider, _browser, app, _product = self.app()
        with self.assertRaisesRegex(HouseholdError, "internal-only"):
            app.handle({"operation": "products", "action": "apply", "partial_apply": True,
                        "_partial_plan_ref": "productplan_forgedforgedforged"})


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
