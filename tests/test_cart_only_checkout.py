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

    def test_cart_only_still_waits_for_an_interrupted_menu_product_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _provider, browser, application = self.household(directory)
            with store.locked() as state:
                state["managed_product_apply_fence"] = {"reason": "synthetic interrupted apply"}
            result = application.handle({"operation": "checkout", "action": "prepare", "cart_only": True})
            self.assertEqual(result["reason"], "managed_product_apply_incomplete")
            self.assertIsNone(store.read()["pending_checkout"])
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
