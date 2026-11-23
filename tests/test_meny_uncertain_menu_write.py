"""An uncertain menu-driven MENY cart write can be settled by the household."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore, cart_summary
from meny import MenyCartStoppedError
from retail_mcp import RetailPreDispatchError
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

    def test_an_unconfirmed_click_keeps_its_closed_reason_through_acceptance(self):
        from agent_views import project_agent_result
        from meny import MenyBrowserError
        store, provider, _browser, app, product = self.app()
        _original, observed = self.observed(provider)
        failure = MenyBrowserError("synthetic-secret https://private.invalid/?token=hidden",
                                   failure_class="quantity_not_settled", source="cart_readback", elapsed_ms=15125)

        def call(tool, arguments, **kwargs):
            if tool == "manipulate_cart":
                # The batch's first click landed; its second was not confirmed.
                provider._mutate_cart({"operations": [dict(arguments["operations"][0], quantity=1)]})
                raise failure
            return observed(tool, arguments, **kwargs)

        provider.call = call
        start = app._cart_lines(cart_summary(provider.cart))[0][product]
        result = base.CartPlanTests.sync(app, product, start + 2)
        self.assertEqual(result["reason"], "cart_write_result_uncertain")
        self.assertEqual(result["provider_failure"], failure.provider_failure)
        # Both agent views of the uncertain write keep the closed reason.
        views = [project_agent_result("cart", "sync", result),
                 app._product_apply_view({"applied": False, "reason": "cart reconciliation required", **result})]
        for view in views:
            self.assertEqual(view["provider_failure"], failure.provider_failure)
        self.assertEqual(views[1]["status"], "outcome_unknown")
        pending = store.read()["pending_cart_change"]
        self.assertEqual(pending["provider_failure"], failure.provider_failure)
        shown = app.handle({"operation": "cart", "action": "get"})
        app.handle({"operation": "cart", "action": "accept_current",
                    "operation_id": shown["operation_id"], "cart_digest": shown["cart_digest"]})
        state = store.read()
        receipt = state["cart_change_receipts"][pending["operation_id"]]
        self.assertEqual(receipt["original_operation"]["provider_failure"], failure.provider_failure)
        self.assertNotIn("synthetic-secret", json.dumps(state) + json.dumps(result) + json.dumps(views))

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


class MenyReviewedCartQuantityTests(unittest.TestCase):
    PRODUCT = base.MENY_PRODUCT
    LABEL = "Legg til 1 stk Brokkoli i handlevognen"

    def client(self):
        client = base.MenyClientTests().client()
        client._open = mock.Mock()
        client._require_login = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._resolve_order_route = mock.Mock()
        client.verify_order_change = mock.Mock()
        client._sleep = mock.Mock()
        return client

    def arguments(self, delta, before=1):
        return {"operations": [{"productId": self.PRODUCT, "quantity": delta}],
                "expected_before_quantities": {self.PRODUCT: before}}

    def control(self, quantity, *, exact=True):
        return {"ready": True, "authenticated": True, "quantity": quantity,
                "exact_quantity": exact, "label": self.LABEL}

    def test_a_reviewed_increment_stops_at_stationary_hover_when_the_quantity_changes(self):
        for observed in (1, 2):
            with self.subTest(observed=observed):
                client = self.client()
                client._product_control = mock.Mock(return_value=self.control(1))
                hit = {"quantity_changed": True} if observed == 2 else {"clear": True}
                client._eval = mock.Mock(side_effect=[{"ready": True}, hit])
                client._invoke = mock.Mock(return_value={"box": {"x": 1, "y": 2, "width": 20, "height": 10}})
                client._wait_for_quantity = mock.Mock(return_value=2)
                client._read_settled_cart = mock.Mock(return_value={"items": []})
                if observed == 2:
                    with self.assertRaisesRegex(MenyCartStoppedError, "quantity changed before the click") as caught:
                        client._change_cart(self.arguments(1))
                    self.assertEqual(caught.exception.applied_operations, [])
                    client._wait_for_quantity.assert_not_called()
                else:
                    client._change_cart(self.arguments(1))
                    client._wait_for_quantity.assert_called_once_with(2, 1, self.PRODUCT)
                client._product_control.assert_called_once_with("mark", 1, self.PRODUCT)
                self.assertEqual(client._eval.call_count, 2)
                self.assertEqual(client._invoke.call_args_list.count(mock.call("mouse", "down")), int(observed == 1))

    def test_an_unproven_zero_quantity_cannot_satisfy_an_empty_review(self):
        client = self.client()
        client._product_control = mock.Mock(return_value=self.control(0, exact=False))
        client._click_cart_control = mock.Mock()
        with self.assertRaisesRegex(MenyCartStoppedError, "quantity changed or cannot be verified"):
            client._change_cart(self.arguments(1, before=0))
        client._click_cart_control.assert_not_called()

    def test_cart_drawer_removal_requires_the_reviewed_quantity(self):
        for observed in (1, 2):
            with self.subTest(observed=observed):
                client = self.client()
                client._product_control = mock.Mock(return_value={"ready": False, "authenticated": True})
                client._read_cart = mock.Mock(return_value={"items": [{"product_id": self.PRODUCT, "quantity": observed}]})
                client._click_cart_remove_control = mock.Mock(side_effect=lambda _product, _quantity, _code, dispatch: dispatch())
                client._wait_for_cart_quantity = mock.Mock(return_value=0)
                if observed == 2:
                    with self.assertRaisesRegex(MenyCartStoppedError, "quantity changed in the cart drawer") as caught:
                        client._change_cart(self.arguments(-1))
                    self.assertEqual(caught.exception.applied_operations, [])
                    client._click_cart_remove_control.assert_not_called()
                else:
                    client._change_cart(self.arguments(-1))
                    client._click_cart_remove_control.assert_called_once_with(self.PRODUCT, 1, None, mock.ANY)

    def household(self, directory, *, drift_after):
        store, fake, _browser, _app, product = base.CartPlanTests.app(directory, "meny")
        client = self.client()
        app = Application(store, client, None)
        clicks = []
        drifted = False

        def product_control(action, _delta, _product):
            nonlocal drifted
            if action == "mark" and not drifted and len(clicks) == drift_after:
                fake._mutate_cart({"operations": [{"productId": product, "quantity": 1}]})
                drifted = True
            return self.control(fake.cart["items"][0]["quantity"])

        def click(product, _label, dispatch, _verify, *, reviewed_control):
            self.assertEqual(reviewed_control, (1, fake.cart["items"][0]["quantity"]))
            dispatch()
            clicks.append(product)
            fake._mutate_cart({"operations": [{"productId": product, "quantity": 1}]})

        client._read_cart = mock.Mock(side_effect=lambda **_: deepcopy(fake.cart))
        client._product_control = product_control
        client._click_cart_control = click
        client.call = mock.Mock(wraps=client.call)
        return app, client, fake, clicks

    def test_raw_and_managed_writes_stop_at_the_visible_drift_and_keep_the_exact_prefix(self):
        for managed in (False, True):
            for prefix in (0, 1):
                with self.subTest(managed=managed, prefix=prefix), tempfile.TemporaryDirectory() as directory:
                    app, client, fake, clicks = self.household(directory, drift_after=prefix)
                    if managed and prefix == 0:
                        with self.assertRaisesRegex(RetailPreDispatchError, "stopped before its first managed click"):
                            base.CartPlanTests.sync(app, self.PRODUCT, 3)
                    elif managed:
                        result = base.CartPlanTests.sync(app, self.PRODUCT, 3)
                        self.assertTrue(result["outcome_unknown"])
                    elif prefix:
                        with self.assertRaisesRegex(HouseholdError, "stopped cart batch is still uncertain"):
                            app.handle({"operation": "cart", "action": "change",
                                        "operations": self.arguments(2)["operations"]})
                    else:
                        result = app.handle({"operation": "cart", "action": "change",
                                             "operations": self.arguments(2)["operations"]})
                        self.assertEqual(result["status"], "not_dispatched")
                    self.assertEqual(clicks, [self.PRODUCT] * prefix)
                    self.assertEqual(fake.cart["items"][0]["quantity"], 2 + prefix)
                    writes = [call.args[1] for call in client.call.call_args_list if call.args[0] == "manipulate_cart"]
                    self.assertEqual(writes, [self.arguments(2)])
                    state = app.store.read()
                    if prefix:
                        pending = state["pending_cart_change"]
                        self.assertTrue(pending["stopped_before_next_click"])
                        self.assertEqual(pending["expected"], {self.PRODUCT: 2})
                    else:
                        self.assertNotIn("pending_cart_change", state)
                        self.assertEqual([entry["status"] for entry in state["cart_change_receipts"].values()],
                                         ["not_dispatched"])

    def test_managed_later_batches_use_the_current_reviewed_quantities(self):
        with tempfile.TemporaryDirectory() as directory:
            app, client, _fake, clicks = self.household(directory, drift_after=None)
            result = base.CartPlanTests.sync(app, self.PRODUCT, 4)
            self.assertTrue(result["synced"])
            self.assertEqual(clicks, [self.PRODUCT] * 3)
            writes = [call.args[1] for call in client.call.call_args_list if call.args[0] == "manipulate_cart"]
            self.assertEqual(writes, [self.arguments(2), self.arguments(1, before=3)])


class MenyClickSettleTests(unittest.TestCase):
    """A dispatched unit gets a bounded settle window that its click guard reserves (#422)."""

    LABEL = "Legg til 1 stk Brokkoli i handlevognen"

    def setUp(self):
        self.now = 1000.0
        clock = mock.patch("meny.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def page(self, delays, *, budget=240.0):
        """A product page that shows each dispatched add only after that click's delay."""
        client = base.MenyClientTests().client()
        client.deadline = self.now + budget
        self.clicks = []
        shown_at = []

        def shown():
            return sum(at <= self.now for at in shown_at)

        def sleep(seconds):
            client._require_time(seconds)
            self.now += seconds

        def product_control(_action, _delta, _product):
            self.now += 0.05  # each browser read takes a little time
            return {"authenticated": True, "ready": True, "page_ready": True,
                    "quantity": shown(), "label": self.LABEL}

        def click(_product, _label, before_dispatch, _same_control):
            before_dispatch()
            self.clicks.append(self.now)
            shown_at.append(self.now + delays[len(self.clicks) - 1])

        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._resolve_order_route = mock.Mock()
        client._sleep = sleep
        client._product_control = product_control
        client._click_cart_control = click
        client._read_cart = lambda: {"provider": "meny", "items": [
            {"product_id": base.MENY_PRODUCT, "quantity": shown()}]}
        return client

    @staticmethod
    def change(client):
        return client._change_cart({"operations": [{"productId": base.MENY_PRODUCT, "quantity": 2}]})

    def test_a_second_add_shown_after_several_seconds_is_confirmed(self):
        for delay in (4, 6, 8):
            with self.subTest(delay=delay):
                cart = self.change(self.page([0.5, delay]))
                self.assertEqual(cart["items"], [{"product_id": base.MENY_PRODUCT, "quantity": 2}])
                self.assertEqual(len(self.clicks), 2)

    def test_a_quantity_that_never_settles_ends_uncertain_with_closed_evidence(self):
        from meny import MENY_POST_CLICK_SECONDS, MENY_QUANTITY_SETTLE_SECONDS, MenyBrowserError, MenyCartStoppedError
        # Just enough time for the guard to allow the second click.
        client = self.page([0.5, float("inf")], budget=MENY_POST_CLICK_SECONDS + 5)
        with self.assertRaisesRegex(MenyBrowserError, "changed partially.*do not retry") as caught:
            self.change(client)
        self.assertNotIsInstance(caught.exception, MenyCartStoppedError)
        self.assertEqual(len(self.clicks), 2)
        self.assertLess(self.now, client.deadline)
        waited = self.now - self.clicks[1]
        self.assertGreaterEqual(waited, MENY_QUANTITY_SETTLE_SECONDS - 0.25)
        self.assertLessEqual(waited, MENY_QUANTITY_SETTLE_SECONDS)
        failure = dict(caught.exception.provider_failure)
        self.assertEqual(failure.pop("elapsed_ms"), round(waited * 1000))
        self.assertEqual(failure, {"provider": "meny", "class": "quantity_not_settled", "source": "cart_readback",
                                   "phase": "post_click_readback", "daemon_request_ending": "unknown"})

    def test_a_stalled_browser_read_is_bounded_by_the_quantity_window(self):
        from meny import MENY_QUANTITY_SETTLE_SECONDS, MenyBrowserError
        client = self.page([float("inf")])
        overall_deadline = client.deadline
        control = client._product_control

        def stalled_read(*args):
            if not self.clicks:
                return control(*args)
            self.assertEqual(client.deadline, self.clicks[0] + MENY_QUANTITY_SETTLE_SECONDS)
            self.now = client.deadline
            raise MenyBrowserError("synthetic browser timeout", failure_class="local_timeout", source="local_timeout")

        client._product_control = stalled_read
        with self.assertRaisesRegex(MenyBrowserError, "uncertain.*do not retry") as caught:
            client._change_one(base.MENY_PRODUCT, 1)
        self.assertEqual(caught.exception.provider_failure["class"], "local_timeout")
        self.assertEqual(client.deadline, overall_deadline)
        self.assertEqual(len(self.clicks), 1)

    def test_the_last_reading_distinguishes_no_change_from_another_quantity(self):
        for observed, failure_class in ((7, "quantity_not_settled"), (9, "quantity_unexpected")):
            with self.subTest(observed=observed):
                client = self.page([float("inf")])
                client._product_control = lambda *_: {"authenticated": True, "ready": True,
                                                      "quantity": 7, "label": self.LABEL}
                client._wait_for_quantity = mock.Mock(return_value=observed)
                with self.assertRaisesRegex(HouseholdError, "uncertain.*do not retry") as caught:
                    client._change_one(base.MENY_PRODUCT, 1)
                self.assertEqual(caught.exception.provider_failure["class"], failure_class)

    def test_the_click_guard_still_stops_a_unit_without_time_to_settle(self):
        from meny import MENY_POST_CLICK_SECONDS, MenyCartStoppedError
        # The first add shows after 12 s, so the second unit lacks its reserve.
        client = self.page([12, 0.5], budget=MENY_POST_CLICK_SECONDS + 10)
        with self.assertRaises(MenyCartStoppedError) as caught:
            self.change(client)
        self.assertEqual(caught.exception.applied_operations, [{"productId": base.MENY_PRODUCT, "quantity": 1}])
        self.assertEqual(len(self.clicks), 1)

    def test_a_removal_uses_the_same_settle_window(self):
        from meny import MENY_QUANTITY_SETTLE_SECONDS
        for delay in (6, float("inf")):
            with self.subTest(delay=delay):
                client = self.page([])
                client._read_cart = lambda: {"items": [{"product_id": base.MENY_PRODUCT, "quantity": 2}]}
                removed_at = []

                def remove(_product, _quantity, _code, before_dispatch):
                    before_dispatch()
                    removed_at.append(self.now + delay)

                def drawer(_script):
                    self.now += 0.05
                    return {"ready": True, "authenticated": True,
                            "quantity": 2 - sum(at <= self.now for at in removed_at)}

                client._click_cart_remove_control = remove
                client._eval = drawer
                if delay == 6:
                    client._remove_one_from_cart(base.MENY_PRODUCT, None)
                    continue
                with self.assertRaisesRegex(HouseholdError, "uncertain.*do not retry") as caught:
                    client._remove_one_from_cart(base.MENY_PRODUCT, None)
                failure = caught.exception.provider_failure
                self.assertEqual(failure["class"], "quantity_not_settled")
                self.assertGreaterEqual(failure["elapsed_ms"], (MENY_QUANTITY_SETTLE_SECONDS - 0.25) * 1000)


if __name__ == "__main__":
    unittest.main()
