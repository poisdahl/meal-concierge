"""Cancel one Oda order while its exact addition payment remains uncertain."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import CancellationPreconditionError, HouseholdError
from service_common import canonical
import test_payment_recovery as recovery_fixtures


class PendingAdditionCancellationTests(unittest.TestCase):
    def setUp(self):
        self.flow = recovery_fixtures.RecoveryTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.app = self.flow.app
        self.browser = self.flow.browser
        self.merchant = self.flow.merchant
        self.merchant.status = "paid_and_modifiable"
        self.addition = {"product_id": "888", "name": "Oats", "quantity": 1, "price": 16.70}
        self.binding = {"account_reference_digest": "a" * 64,
                        "receipt_address": "Example street 1"}
        self.change = {"provider": "oda", "order_id": "order-1",
                       "status": "editing", "before": {"order": deepcopy(self.merchant.order),
                                                    "tracking": {"orderNumber": "order-1", "status": "paid_and_modifiable"}},
                       "binding": deepcopy(self.binding)}
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending["order_change"] = deepcopy(self.change)
            pending["summary"]["items"] = [deepcopy(self.addition)]
            pending["summary"]["total"] = 16.70
            pending["cart"]["items"] = [deepcopy(self.addition)]
            pending["browser_review"]["binding"] = deepcopy(self.binding)
            pending["vipps_request_context"] = {
                "tab_id": "vipps-addition", "order_id": "order-1",
                "expected_total": 1670, "gateway_url_digest": "b" * 64,
            }
            pending["vipps_request_status"] = "unknown"
            pending.pop("vipps_expiry_gateway_digest", None)
            state["order_change"] = deepcopy(self.change)
            state["menu"] = {"menu_id": "menu-B", "phase": "draft"}
            state["recipe_usage"]["menu-B"] = {"status": "planned"}
        self.clicks = 0
        self.browser.review_cancellation = lambda order_id, order, **kw: {
            "available": True, "binding": deepcopy(self.binding), "consequence": None}

        def submit(order_id, order, review, before_click, **kw):
            before_click()
            self.clicks += 1
            self.merchant.status = "cancelled"
        self.browser.submit_cancellation = submit

    def prepare(self):
        return self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})

    def confirm(self, prepared):
        return self.app.handle({"operation": "orders", "action": "cancel_confirm",
                                "order_id": "order-1", "confirmation_id": prepared["confirmation_id"]})

    def test_uncertain_addition_can_cancel_exact_order_without_payment_closure(self):
        checkout = deepcopy(self.app.store.read()["pending_checkout"])
        prepared = self.prepare()
        self.assertTrue(prepared["available"])
        self.assertEqual(canonical(self.app.store.read()["pending_checkout"]), canonical(checkout))
        result = self.confirm(prepared)
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.clicks, 1)
        state = self.app.store.read()
        self.assertIsNone(state["pending_checkout"])
        self.assertIsNone(state["order_change"])
        self.assertEqual(state["menu"], {"menu_id": "menu-B", "phase": "draft"})
        self.assertEqual(state["recipe_usage"]["menu-B"]["status"], "planned")
        terminal = state["protected_results"]["original"]
        self.assertEqual(terminal["cancelled_attempt"], checkout)
        self.assertFalse(terminal["result"]["retry_allowed"])
        self.assertEqual(result["payment_resolution"],
                         {"authorization_release": "unknown", "refund": "unknown"})

    def test_unknown_cancel_keeps_both_journals_and_reconciles_without_second_click(self):
        def unknown(order_id, order, review, before_click, **kw):
            before_click()
            self.clicks += 1
        self.browser.submit_cancellation = unknown
        prepared = self.prepare()
        result = self.confirm(prepared)
        self.assertFalse(result["cancelled"])
        state = self.app.store.read()
        self.assertEqual(state["pending_cancellation"]["status"], "uncertain")
        self.assertIsNotNone(state["pending_checkout"])
        self.assertEqual(state["order_change"], self.change)
        again = self.app.handle({"operation": "orders", "action": "cancel_reconcile",
                                 "confirmation_id": prepared["confirmation_id"]})
        self.assertFalse(again["cancelled"])
        self.assertEqual(self.clicks, 1)
        self.merchant.status = "cancelled"
        final = self.app.handle({"operation": "orders", "action": "cancel_reconcile",
                                 "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(final["cancelled"])
        self.assertIsNone(self.app.store.read()["order_change"])

    def test_verified_cancel_archives_original_and_recovery_payment_evidence(self):
        with self.app.store.locked() as state:
            state["pending_checkout"]["recovery"] = {
                "confirmation_id": "recovery", "order_id": "order-1", "status": "uncertain",
                "vipps_request_context": {"tab_id": "recovery-vipps", "payment_id": "42",
                                          "order_id": "order-1", "gateway_url_digest": "c" * 64},
            }
        frozen = deepcopy(self.app.store.read()["pending_checkout"])
        prepared = self.prepare()
        self.assertTrue(self.confirm(prepared)["cancelled"])
        archived = self.app.store.read()["protected_results"]
        self.assertEqual(archived["original"]["cancelled_attempt"], frozen)
        self.assertEqual(archived["recovery"]["cancelled_attempt"], frozen["recovery"])
        self.assertFalse(archived["recovery"]["result"]["retry_allowed"])

    def test_order_change_mutation_at_final_click_blocks_dispatch(self):
        prepared = self.prepare()
        def mutate(order_id, order, review, before_click, **kw):
            with self.app.store.locked() as state:
                state["order_change"]["status"] = "changed"
            before_click()
            self.clicks += 1
        self.browser.submit_cancellation = mutate
        with self.assertRaisesRegex(CancellationPreconditionError, "bound payment changed"):
            self.confirm(prepared)
        self.assertEqual(self.clicks, 0)
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])

    def test_merchant_receipt_mutation_at_final_click_blocks_dispatch(self):
        prepared = self.prepare()
        def mutate(order_id, order, review, before_click, **kw):
            self.merchant.order["grossAmount"] = 247.40
            before_click()
            self.clicks += 1
        self.browser.submit_cancellation = mutate
        with self.assertRaisesRegex(CancellationPreconditionError, "merchant order changed"):
            self.confirm(prepared)
        self.assertEqual(self.clicks, 0)
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])

    def test_accepted_addition_requires_fresh_review_of_changed_receipt(self):
        prepared = self.prepare()
        self.merchant.order["products"].append(deepcopy(self.addition))
        self.merchant.order["grossAmount"] = 263.10
        with self.assertRaisesRegex(HouseholdError, "order changed"):
            self.confirm(prepared)
        renewed = self.prepare()
        self.assertNotEqual(renewed["confirmation_id"], prepared["confirmation_id"])
        self.assertTrue(self.confirm(renewed)["cancelled"])

    def test_independent_merchant_cancellation_archives_uncertain_addition(self):
        self.merchant.status = "cancelled"
        result = self.flow.call("reconcile", confirmation_id="original")
        self.assertTrue(result["cancelled"])
        self.assertIsNone(self.app.store.read()["order_change"])
        self.assertEqual(self.app.store.read()["recipe_usage"]["menu-B"]["status"], "planned")
        self.assertEqual(self.clicks, 0)


if __name__ == "__main__":
    unittest.main()
