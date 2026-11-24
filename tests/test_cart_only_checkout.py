"""An explicit cart-only purchase leaves an unrelated saved menu and its plan alone."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError, StateStore
from service import Application
import test_meal_concierge as base
from test_meal_concierge import CONFIG, ODA_FIXTURE_NOW, FakeBrowser, MutableFakeOda

MENU_KEYS = ("menu", "cart_plan", "recipe_usage", "order_snapshots")


def menu_state(store):
    state = store.read()
    return {key: deepcopy(state.get(key)) for key in MENU_KEYS}


@mock.patch("service.now", new=lambda: ODA_FIXTURE_NOW)
class CartOnlyCheckoutTests(unittest.TestCase):
    def household(self, directory, *, policy=None):
        settings = {**CONFIG, "provider": "oda", **({"confirmation_policy": policy} if policy else {})}
        store = StateStore(Path(directory), settings)
        provider = MutableFakeOda()
        browser = FakeBrowser()
        browser.oda = provider
        application = Application(store, provider, browser)
        with store.locked() as state:
            state["menu"] = base.CartPlanTests.menu()
        # The saved menu needs the pasta already in the cart; a later manual
        # extra was bought or removed elsewhere and is no longer in the cart.
        base.CartPlanTests.sync(application, "10", quantity=1)
        application.handle({"operation": "cart", "action": "change",
                            "operations": [{"product_id": "20", "quantity": 1}]})
        provider._mutate_cart({"operations": [{"productId": 20, "quantity": -1}]})
        return store, provider, browser, application

    def test_menu_gate_still_applies_without_an_explicit_cart_only_request(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory)
            stopped = application.handle({"operation": "checkout", "action": "prepare"})
            self.assertTrue(stopped["cart_reconciliation_required"])
            self.assertEqual(stopped["reason"], "cart_requires_owner_decision")
            self.assertIn("cart_only=true", stopped["next"])
            self.assertIsNone(store.read()["pending_checkout"])
            self.assertEqual(browser.checkout_clicks, 0)

    def test_cart_only_purchase_and_cancellation_leave_menu_and_plan_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store, provider, browser, application = self.household(directory)
            before = menu_state(store)
            self.assertEqual(before["cart_plan"]["required_quantities"], {"10": 1})

            prepared = application.handle({"operation": "checkout", "action": "prepare", "cart_only": True})

            self.assertNotIn("cart_reconciliation_required", prepared)
            self.assertEqual(prepared["summary"]["menu_attribution"], "cart_only")
            self.assertEqual(prepared["summary"]["menu_shortfall"], [])
            pending = store.read()["pending_checkout"]
            self.assertTrue(pending["cart_only"])
            self.assertIsNone(pending["menu"])
            self.assertIsNone(pending["menu_ref"])
            self.assertEqual(menu_state(store), before)

            result = application.handle({"operation": "checkout", "action": "confirm",
                                         "confirmation_id": prepared["confirmation_id"]})

            self.assertTrue(result["confirmed"], result)
            self.assertEqual(result["menu_attribution"], "cart_only")
            self.assertEqual(browser.checkout_clicks, 1)
            self.assertEqual(menu_state(store), before)
            self.assertEqual(store.read()["menu"]["phase"], "draft")
            # A retried confirm after a lost reply returns the same order.
            retried = application.handle({"operation": "checkout", "action": "confirm", "cart_only": True,
                                          "confirmation_id": prepared["confirmation_id"]})
            self.assertTrue(retried["confirmed"], retried)
            self.assertEqual(browser.checkout_clicks, 1)

            review = application.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "new-order"})
            cancelled = application.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "new-order",
                                            "confirmation_id": review["confirmation_id"]})
            self.assertTrue(cancelled["cancelled"], cancelled)
            self.assertEqual(provider.tracking, "cancelled")
            self.assertEqual(menu_state(store), before)

    def test_explicit_menu_purchase_with_the_same_products_still_binds_the_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory)
            with self.assertRaisesRegex(HouseholdError, "Choose one"):
                application.handle({"operation": "checkout", "action": "prepare", "cart_only": True, "weekly": True})
            stopped = application.handle({"operation": "checkout", "action": "prepare"})
            application.handle({"operation": "cart", "action": "reconcile", "decision": "keep_current",
                                "menu_ref": application._cart_menu_ref(store.read()["menu"]),
                                "cart_digest": stopped["cart_plan"]["cart_digest"],
                                "accept_missing_product_ids": ["20"]})
            prepared = application.handle({"operation": "checkout", "action": "prepare"})
            self.assertEqual(prepared["summary"]["menu_attribution"], "menu_bound")

            result = application.handle({"operation": "checkout", "action": "confirm",
                                         "confirmation_id": prepared["confirmation_id"]})

            self.assertTrue(result["confirmed"], result)
            self.assertEqual(result["menu_attribution"], "menu_bound")
            self.assertEqual(browser.checkout_clicks, 1)
            state = store.read()
            self.assertEqual(state["menu"]["phase"], "ordered")
            self.assertEqual(state["menu"]["order_id"], "new-order")
            self.assertIn("new-order", state["order_snapshots"])

    def test_staging_and_buying_a_standalone_cart_does_not_create_stale_menu_extras(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory), {**CONFIG, "provider": "oda"})
            provider = MutableFakeOda()
            provider._mutate_cart({"operations": [{"productId": 10, "quantity": -1}]})
            browser = FakeBrowser()
            browser.oda = provider
            app = Application(store, provider, browser)
            with store.locked() as state:
                state["menu"] = base.CartPlanTests.menu()
            before = menu_state(store)
            delta = {"operation": "cart", "action": "change", "cart_only": True,
                     "operations": [{"product_id": "10", "quantity": 1}]}
            app.handle(delta)
            self.assertEqual(menu_state(store), before)
            review = app.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            bought = app.handle({"operation": "checkout", "action": "confirm",
                                 "confirmation_id": review["confirmation_id"]})
            self.assertTrue(bought["confirmed"], bought)
            # The fake leaves items in the cart after payment; model their removal.
            provider._mutate_cart({"operations": [{"productId": 10, "quantity": -1}]})
            cancellation = app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "new-order"})
            cancelled = app.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "new-order",
                                     "confirmation_id": cancellation["confirmation_id"]})
            self.assertTrue(cancelled["cancelled"])
            self.assertEqual(menu_state(store), before)
            synced = base.CartPlanTests.sync(app, "20", quantity=1)
            self.assertTrue(synced["synced"], synced)
            self.assertEqual(store.read()["cart_plan"]["supplemental_quantities"], {})
            self.assertEqual([str(row["product_id"]) for row in provider.cart["items"]], ["20"])

    def test_lost_standalone_add_response_keeps_its_scope_through_recovery(self):
        for action in ("reconcile_change", "accept_current"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                store, provider, _browser, app = self.household(directory)
                before = menu_state(store)
                original = provider.call
                dispatched = False

                def lose_cart_write(tool, arguments, **kwargs):
                    nonlocal dispatched
                    if dispatched and tool == "get_cart":
                        raise HouseholdError("synthetic readback unavailable")
                    result = original(tool, arguments, **kwargs)
                    if tool == "manipulate_cart":
                        dispatched = True
                        raise HouseholdError("synthetic lost response")
                    return result

                ensure = {"operation": "cart", "action": "ensure", "cart_only": True,
                          "requirements": [{"product_id": "30", "product_name": "Test item", "quantity": 1}]}
                with mock.patch.object(provider, "call", side_effect=lose_cart_write):
                    with self.assertRaises(HouseholdError):
                        app.handle(ensure)
                self.assertIs(store.read()["pending_cart_change"]["cart_only"], True)
                observed = app.handle({"operation": "cart", "action": "get"})
                recovered = app.handle({"operation": "cart", "action": action,
                                        "operation_id": observed["operation_id"], "cart_digest": observed["cart_digest"]})
                self.assertTrue(recovered["reconciled"], recovered)
                self.assertIsNone(store.read().get("pending_cart_change"))
                app.handle(ensure)
                self.assertEqual(len([item for item in provider.cart["items"] if str(item["product_id"]) == "30"]), 1)
                self.assertEqual(menu_state(store), before)

    def test_cart_only_staging_rejects_removal_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            store, provider, _browser, app = self.household(directory)
            before = deepcopy(provider.cart)
            with self.assertRaisesRegex(HouseholdError, "cart_only stages additions"):
                app.handle({"operation": "cart", "action": "change", "cart_only": True,
                            "operations": [{"product_id": "30", "quantity": 1}, {"product_id": "10", "quantity": -1}]})
            self.assertEqual(provider.cart, before)
            self.assertIsNone(store.read().get("pending_cart_change"))

    def test_lost_cart_only_submit_reconciles_once_without_binding_the_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            store, provider, browser, application = self.household(directory, policy="standing")
            before = menu_state(store)
            original = browser.submit_checkout

            def lost(*args, **kwargs):
                original(*args, **kwargs)
                raise HouseholdError("synthetic accepted order response lost")

            browser.submit_checkout = lost
            request = {"operation": "checkout", "action": "submit", "cart_only": True,
                       "idempotency_key": "one-off-groceries"}
            with self.assertRaisesRegex(HouseholdError, "response lost"):
                application.handle(request)
            self.assertEqual(store.read()["pending_checkout"]["status"], "uncertain")
            browser.submit_checkout = original

            restarted = Application(StateStore(Path(directory), {**CONFIG, "provider": "oda",
                                                                 "confirmation_policy": "standing"}), provider, browser)
            result = restarted.handle(request)

            self.assertTrue(result["confirmed"], result)
            self.assertEqual(result["menu_attribution"], "cart_only")
            self.assertTrue(restarted.handle(request)["idempotent"])
            self.assertEqual(browser.checkout_clicks, 1)
            self.assertEqual(len(provider.orders), 1)
            self.assertEqual(menu_state(restarted.store), before)

    def test_cart_only_submit_does_not_reuse_a_prepared_menu_review(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory, policy="standing")
            stopped = application.handle({"operation": "checkout", "action": "prepare"})
            application.handle({"operation": "cart", "action": "reconcile", "decision": "keep_current",
                                "menu_ref": application._cart_menu_ref(store.read()["menu"]),
                                "cart_digest": stopped["cart_plan"]["cart_digest"],
                                "accept_missing_product_ids": ["20"]})
            menu_review = application.handle({"operation": "checkout", "action": "prepare"})
            self.assertEqual(menu_review["summary"]["menu_attribution"], "menu_bound")
            before = menu_state(store)

            result = application.handle({"operation": "checkout", "action": "submit", "cart_only": True,
                                         "idempotency_key": "only-the-cart"})

            self.assertTrue(result["confirmed"], result)
            self.assertEqual(result["menu_attribution"], "cart_only")
            self.assertNotEqual(result["confirmation_id"], menu_review["confirmation_id"])
            self.assertEqual(browser.checkout_clicks, 1)
            self.assertEqual(menu_state(store), before)
            self.assertEqual(store.read()["menu"]["phase"], "draft")

    def test_renewed_and_payment_switched_reviews_keep_cart_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory)
            first = application.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            renewed = application.handle({"operation": "checkout", "action": "renew_review",
                                          "confirmation_id": first["confirmation_id"]})
            self.assertEqual(renewed["summary"]["menu_attribution"], "cart_only")
            self.assertTrue(store.read()["pending_checkout"]["cart_only"])
            switched = application.handle({"operation": "checkout", "action": "switch_payment",
                                           "confirmation_id": renewed["confirmation_id"],
                                           "checkout_payment": {"method": "saved_card"}})
            self.assertEqual(switched["summary"]["menu_attribution"], "cart_only")
            self.assertTrue(store.read()["pending_checkout"]["cart_only"])
            self.assertIsNone(store.read()["pending_checkout"]["menu"])
            self.assertEqual(browser.checkout_clicks, 0)

    def menu_review(self, store, application):
        stopped = application.handle({"operation": "checkout", "action": "prepare"})
        application.handle({"operation": "cart", "action": "reconcile", "decision": "keep_current",
                            "menu_ref": application._cart_menu_ref(store.read()["menu"]),
                            "cart_digest": stopped["cart_plan"]["cart_digest"],
                            "accept_missing_product_ids": ["20"]})
        review = application.handle({"operation": "checkout", "action": "prepare"})
        self.assertEqual(review["summary"]["menu_attribution"], "menu_bound")
        return review

    def test_dispatch_rejects_a_purchase_intent_that_differs_from_the_review(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory, policy="standing")
            review = self.menu_review(store, application)
            with self.assertRaisesRegex(HouseholdError, "linked to the saved menu, not cart_only"):
                application.handle({"operation": "checkout", "action": "confirm", "cart_only": True,
                                    "confirmation_id": review["confirmation_id"]})
            for action in ("renew_review", "switch_payment", "reconcile", "discard_review"):
                with self.assertRaisesRegex(HouseholdError, "purchase choice"):
                    application.handle({"operation": "checkout", "action": action, "cart_only": True,
                                        "confirmation_id": review["confirmation_id"]})
            with self.assertRaisesRegex(HouseholdError, "true or false"):
                application.handle({"operation": "checkout", "action": "prepare", "cart_only": 1})
            with store.locked() as state:
                application._bind_protected_request(state, "checkout", "bound-to-menu", review["confirmation_id"])
            with self.assertRaisesRegex(HouseholdError, "bound to a menu-linked review"):
                application.handle({"operation": "checkout", "action": "submit", "cart_only": True,
                                    "idempotency_key": "bound-to-menu"})
            self.assertEqual(browser.checkout_clicks, 0)
            self.assertEqual(store.read()["menu"]["phase"], "draft")

    def test_submit_without_a_flag_pays_the_shown_cart_only_review(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory, policy="standing")
            self.menu_review(store, application)
            before = menu_state(store)
            shown = application.handle({"operation": "checkout", "action": "prepare", "cart_only": True})

            result = application.handle({"operation": "checkout", "action": "submit", "idempotency_key": "yes-buy-it"})

            self.assertTrue(result["confirmed"], result)
            self.assertEqual(result["confirmation_id"], shown["confirmation_id"])
            self.assertEqual(result["menu_attribution"], "cart_only")
            self.assertEqual(browser.checkout_clicks, 1)
            self.assertEqual(menu_state(store), before)

    def test_unrelated_menu_contents_and_delivery_reselection_keep_a_cart_only_purchase(self):
        with tempfile.TemporaryDirectory() as directory:
            store, provider, browser, application = self.household(directory, policy="standing")
            with store.locked() as state:
                state["menu"]["dishes"] = [{"title": "Recipe from another store", "source_provider": "meny"}]
            before = menu_state(store)
            with self.assertRaises(Exception):
                application.handle({"operation": "checkout", "action": "prepare"})
            application.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            provider.delivery_slots["slots"][0]["price_ore"] = 5900
            reprepared = application.handle({"operation": "checkout", "action": "submit", "cart_only": True,
                                             "idempotency_key": "price-changed"})
            self.assertTrue(reprepared.get("reprepared"), reprepared)
            self.assertTrue(store.read()["pending_checkout"]["cart_only"])

            result = application.handle({"operation": "checkout", "action": "submit", "idempotency_key": "price-changed"})

            self.assertTrue(result["confirmed"], result)
            self.assertEqual(result["menu_attribution"], "cart_only")
            self.assertEqual(browser.checkout_clicks, 1)
            self.assertEqual(menu_state(store), before)

    def test_weekly_request_never_pays_a_cart_only_review_and_expiry_keeps_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory, policy="standing")
            self.menu_review(store, application)
            before = menu_state(store)
            shown = application.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            with self.assertRaisesRegex(HouseholdError, "cart_only, not for the saved weekly menu"):
                application.handle({"operation": "checkout", "action": "confirm", "weekly": True,
                                    "confirmation_id": shown["confirmation_id"]})
            with store.locked() as state:
                application._bind_protected_request(state, "checkout", "bound-to-cart", shown["confirmation_id"])
            with self.assertRaisesRegex(HouseholdError, "bound to a cart_only review"):
                application.handle({"operation": "checkout", "action": "submit", "weekly": True,
                                    "idempotency_key": "bound-to-cart"})
            self.assertEqual(browser.checkout_clicks, 0)
            with store.locked() as state:
                state["pending_checkout"]["expires_at"] = "2000-01-01T00:00:00+00:00"

            result = application.handle({"operation": "checkout", "action": "submit", "idempotency_key": "after-expiry"})

            self.assertTrue(result["confirmed"], result)
            self.assertEqual(result["menu_attribution"], "cart_only")
            self.assertEqual(browser.checkout_clicks, 1)
            self.assertEqual(menu_state(store), before)

    def test_payment_switch_stays_with_review_but_does_not_follow_new_purchase(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, browser, app = self.household(directory, policy="standing")
            app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                        "changes": {"checkout_payment": {"method": "vipps"}}})
            menu = self.menu_review(store, app)
            switched = app.handle({"operation": "checkout", "action": "switch_payment",
                                   "confirmation_id": menu["confirmation_id"],
                                   "checkout_payment": {"method": "saved_card"}})
            renewed = app.handle({"operation": "checkout", "action": "renew_review",
                                  "confirmation_id": switched["confirmation_id"]})
            self.assertEqual(renewed["summary"]["payment_method"], "saved_card")
            self.assertEqual(browser.checkout_clicks, 0)
            result = app.handle({"operation": "checkout", "action": "submit", "cart_only": True,
                                 "idempotency_key": "separate-shop"})
            self.assertEqual(result["authorized_summary"]["payment_method"], "vipps")
            self.assertEqual(result["authorized_summary"]["menu_attribution"], "cart_only")
            self.assertEqual(browser.checkout_clicks, 1)
            self.assertEqual(store.read()["menu"]["phase"], "draft")

    def test_fresh_standalone_prepare_resets_card_override_when_basket_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            _, provider, browser, app = self.household(directory)
            app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                        "changes": {"checkout_payment": {"method": "vipps"}}})
            request = {"operation": "checkout", "action": "prepare", "cart_only": True}
            app.handle({**request, "checkout_payment": {"method": "saved_card"}})
            same = app.handle(request)
            self.assertEqual(same["summary"]["payment_method"], "saved_card")
            provider._mutate_cart({"operations": [{"productId": 10, "quantity": -1},
                                                   {"productId": 20, "quantity": 1}]})
            separate = app.handle(request)
            self.assertEqual(separate["summary"]["payment_method"], "vipps")
            self.assertEqual(browser.checkout_clicks, 0)

    def test_weekly_confirm_requires_frozen_menu_coverage_including_older_reviews(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                store, provider, browser, app = self.household(directory, policy="standing")
                with store.locked() as state:
                    state["cart_plan"] = None
                provider._mutate_cart({"operations": [{"productId": 10, "quantity": -1}]})
                app.handle({"operation": "cart", "action": "ensure", "requirements": [
                    {"product_id": "10", "product_name": "Pasta", "quantity": 1}]})
                review = app.handle({"operation": "checkout", "action": "prepare"})
                self.assertEqual(review["summary"]["menu_attribution"], "cart_only")
                with store.locked() as state:
                    pending = state["pending_checkout"]
                    self.assertNotIn("cart_only", pending)
                    if legacy:
                        del pending["summary"]["menu_attribution"]
                    app._bind_protected_request(state, "checkout", "old-review", pending["confirmation_id"])
                with self.assertRaisesRegex(HouseholdError, "not for the saved weekly menu"):
                    app.handle({"operation": "checkout", "action": "confirm", "weekly": True,
                                "confirmation_id": review["confirmation_id"]})
                with self.assertRaisesRegex(HouseholdError, "bound to a cart_only review"):
                    app.handle({"operation": "checkout", "action": "submit", "weekly": True,
                                "idempotency_key": "old-review"})
                self.assertEqual(browser.checkout_clicks, 0)
                self.assertEqual(store.read()["menu"]["phase"], "draft")

    def test_weekly_confirm_rechecks_the_weekly_product_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, browser, app = self.household(directory)
            menu = self.menu_review(store, app)
            self.assertEqual(menu["summary"]["menu_attribution"], "menu_bound")
            stopped = app.handle({"operation": "checkout", "action": "confirm", "weekly": True,
                                  "confirmation_id": menu["confirmation_id"]})
            self.assertEqual(stopped["reason"], "weekly_menu_products_incomplete")
            self.assertEqual(browser.checkout_clicks, 0)

    def test_occurrence_submit_prepares_its_own_review_even_after_expiry(self):
        for expired in (False, True):
            with self.subTest(expired=expired), tempfile.TemporaryDirectory() as directory:
                store, _, browser, app = self.household(directory, policy="standing")
                app.handle({"operation": "schedule", "action": "update", "changes": {
                    "enabled": True, "mode": "cart_ready", "auto_checkout": False,
                    "delivery": {"weekday": "Saturday", "strategy": "keep_selected"}}})
                app.handle({"operation": "schedule", "action": "set_cron_job", "cron_job_id": "test-cron"})
                ready = app.handle({"operation": "checkout", "action": "auto", "occurrence": "2026-W36"})
                occurrence = ready["occurrence"]
                app.handle({"operation": "delivery", "action": "select", "slot_ref": "oda:2026-09-05:70"})
                prior = app.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
                with store.locked() as state:
                    app._bind_protected_request(state, "checkout", "interactive-key", prior["confirmation_id"])
                    if expired:
                        state["pending_checkout"]["expires_at"] = "2000-01-01T00:00:00+00:00"
                if not expired:
                    with self.assertRaisesRegex(HouseholdError, "different purchase occurrence"):
                        app.handle({"operation": "checkout", "action": "submit", "occurrence": occurrence,
                                    "idempotency_key": "interactive-key"})
                stopped = app.handle({"operation": "checkout", "action": "submit", "occurrence": occurrence,
                                      "idempotency_key": "occurrence-key"})
                self.assertEqual(stopped["reason"], "cart_requires_owner_decision")
                self.assertEqual(browser.checkout_clicks, 0)
                self.assertEqual(store.read()["occurrences"][occurrence]["status"], "cart_ready")
                self.assertIsNone(store.read()["pending_checkout"])
                if not expired:
                    self.assertEqual(stopped["discarded_confirmation_id"], prior["confirmation_id"])
                    receipt = app.handle({"operation": "checkout", "action": "confirm",
                                          "confirmation_id": prior["confirmation_id"]})
                    self.assertEqual(receipt["status"], "review_discarded")
                app.handle({"operation": "cart", "action": "reconcile", "decision": "keep_current",
                            "menu_ref": app._cart_menu_ref(store.read()["menu"]),
                            "cart_digest": stopped["cart_plan"]["cart_digest"],
                            "accept_missing_product_ids": ["20"]})
                result = app.handle({"operation": "checkout", "action": "submit", "occurrence": occurrence,
                                     "idempotency_key": "occurrence-key"})
                self.assertTrue(result["confirmed"], result)
                self.assertNotEqual(result["confirmation_id"], prior["confirmation_id"])
                self.assertEqual(store.read()["occurrences"][occurrence]["status"], "completed")
                self.assertEqual(browser.checkout_clicks, 1)

    def test_retired_standalone_review_gives_scoped_retry_and_raw_false_can_renew(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, browser, app = self.household(directory)
            review = app.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            renewed = app.handle({"operation": "checkout", "action": "renew_review", "cart_only": False,
                                  "confirmation_id": review["confirmation_id"]})
            self.assertEqual(renewed["summary"]["menu_attribution"], "cart_only")
            with store.locked() as state:
                state["pending_checkout"]["expires_at"] = "2000-01-01T00:00:00+00:00"
            from planning_assessment import workflow_status
            self.assertTrue(workflow_status(store.read())["next_action"]["arguments"]["cart_only"])
            with self.assertRaisesRegex(HouseholdError, "prepare a fresh review with cart_only=true"):
                app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": renewed["confirmation_id"]})
            self.assertIsNone(store.read()["pending_checkout"])
            self.assertEqual(browser.checkout_clicks, 0)
            review = app.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            self.assertEqual(review["summary"]["menu_attribution"], "cart_only")

    def test_cart_only_still_waits_for_an_interrupted_menu_product_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory)
            with store.locked() as state:
                state["managed_product_apply_fence"] = {"reason": "synthetic interrupted apply"}
            result = application.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            self.assertEqual(result["reason"], "managed_product_apply_incomplete")
            self.assertIsNone(store.read()["pending_checkout"])
            self.assertEqual(browser.checkout_clicks, 0)

    def test_cart_only_does_not_buy_a_partially_applied_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory)
            with store.locked() as state:
                state["cart_plan"]["partial_product_plan_digest"] = "synthetic-partial-apply"
            result = application.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            self.assertEqual(result["reason"], "managed_product_apply_incomplete")
            self.assertIsNone(store.read()["pending_checkout"])
            self.assertEqual(browser.checkout_clicks, 0)

    def test_a_new_product_apply_fence_blocks_an_existing_cart_only_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory)
            prepared = application.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            original_check = application._checkout_dietary

            def start_apply(*args, **kwargs):
                result = original_check(*args, **kwargs)
                application._start_managed_product_apply_fence()
                return result

            with mock.patch.object(application, "_checkout_dietary", side_effect=start_apply):
                stopped = application.handle({"operation": "checkout", "action": "confirm",
                                              "confirmation_id": prepared["confirmation_id"]})
            self.assertEqual(stopped["reason"], "managed_product_apply_incomplete")
            self.assertEqual(store.read()["pending_checkout"]["status"], "awaiting_confirmation")
            self.assertEqual(browser.checkout_clicks, 0)

    def test_cart_only_is_rejected_for_scheduled_and_recovery_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory)
            before = deepcopy(store.read())
            with self.assertRaisesRegex(HouseholdError, "not payment recovery"):
                application.handle({"operation": "checkout", "action": "prepare", "cart_only": True, "recovery": True})
            with self.assertRaisesRegex(HouseholdError, "true or false"):
                application.handle({"operation": "checkout", "action": "prepare", "cart_only": "yes"})
            with self.assertRaisesRegex(HouseholdError, "interactive new order"):
                application._checkout_prepare(None, occurrence="2026-W36", automatic_checkout=True,
                                              cart_only=True)
            self.assertEqual(store.read(), before)
            self.assertEqual(browser.checkout_clicks, 0)


if __name__ == "__main__":
    unittest.main()
