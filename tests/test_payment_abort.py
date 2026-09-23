"""Exact Oda payment abort and same-order cancellation journal behavior."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import CheckoutPreconditionError, HouseholdError
from planning_assessment import workflow_status
import test_payment_recovery as recovery_fixtures


class PaymentAbortTests(unittest.TestCase):
    def setUp(self):
        self.flow = recovery_fixtures.RecoveryTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.app = self.flow.app
        self.browser = self.flow.browser
        self.merchant = self.flow.merchant
        self.prepared = self.flow.call("prepare", recovery=True,
                                       checkout_payment={"method": "saved_card"})
        self.confirmation = self.prepared["confirmation_id"]
        self.context = {"tab_id": "card-tab", "payment_id": "payment-1"}
        with self.app.store.locked() as state:
            child = state["pending_checkout"]["recovery"]
            child["status"] = "awaiting_user_payment"
            child["authentication_context"] = deepcopy(self.context)
        self.abort_clicks = 0
        self.browser.abort_card_payment = self._abort_closed
        self.browser.review_cancellation = self._review_cancellation
        self.browser.submit_cancellation = self._submit_cancellation

    def _abort_closed(self, context, before_abort, *, order_id, prior, **kwargs):
        self.assertEqual(context, self.context)
        self.assertEqual(order_id, "order-1")
        if not prior.get("cancel_attempted"):
            before_abort({"payment_id": "payment-1"})
            self.abort_clicks += 1
        return {"status": "closed", "order_id": order_id, "payment_id": "payment-1",
                "terminal_status": "checkout-payment-retry", "source": "oda_three_ds",
                "cancel_attempted": bool(self.abort_clicks)}

    def _review_cancellation(self, order_id, order, **kwargs):
        self.assertEqual(order_id, "order-1")
        return {"available": True, "binding": {
            "account_reference_digest": "a" * 64, "receipt_address": "Example street 1"},
            "consequence": None}

    def _submit_cancellation(self, order_id, order, review, before_click, **kwargs):
        before_click()
        self.merchant.status = "cancelled"

    def abort(self, confirmation=None):
        return self.flow.call("abort_payment", confirmation_id=confirmation or self.confirmation)

    def test_unknown_abort_is_fenced_and_observed_without_second_click(self):
        def uncertain(context, before_abort, *, prior, **kwargs):
            if not prior.get("cancel_attempted"):
                before_abort({"payment_id": "payment-1"})
                self.abort_clicks += 1
            return {"status": "unknown", "cancel_attempted": True}
        self.browser.abort_card_payment = uncertain
        first = self.abort()
        second = self.abort()
        self.assertEqual(self.abort_clicks, 1)
        self.assertEqual((first["payment_abort_status"], second["payment_abort_status"]),
                         ("unknown", "unknown"))
        blocked = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.assertFalse(blocked["available"])
        self.assertEqual(blocked["next_action"]["action"], "abort_payment")
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])

    def test_wrong_confirmation_order_or_account_never_aborts(self):
        with self.assertRaisesRegex(HouseholdError, "exact current"):
            self.abort("original")
        with self.app.store.locked() as state:
            state["pending_checkout"]["recovery"]["order_id"] = "other-order"
        with self.assertRaisesRegex(HouseholdError, "exact pending Oda order"):
            self.abort()
        with self.app.store.locked() as state:
            state["pending_checkout"]["recovery"]["order_id"] = "order-1"
        self.browser.read_order_binding = lambda *a, **kw: {
            "account_reference_digest": "b" * 64, "receipt_address": "Example street 1"}
        with self.assertRaisesRegex(HouseholdError, "account differs"):
            self.abort()
        self.assertEqual(self.abort_clicks, 0)

    def test_paid_race_reconciles_purchase_without_cancel(self):
        def paid(context, before_abort, *, order_id, **kwargs):
            self.merchant.status = "paid_and_modifiable"
            return {"status": "paid", "order_id": order_id, "payment_id": "payment-1",
                    "terminal_status": "checkout-payment-success", "source": "oda_three_ds"}
        self.browser.abort_card_payment = paid
        self.browser.checkout_payment_authentication = lambda *a, **kw: None
        self.browser.checkout_payment_failure = lambda *a, **kw: None
        result = self.abort()
        self.assertEqual(result["payment_abort_status"], "paid")
        self.assertTrue(result["confirmed"])
        self.assertIsNone(self.app.store.read()["pending_checkout"])
        self.assertEqual(self.abort_clicks, 0)

    def test_native_paid_abort_reconciles_legacy_retry_page_bound_purchase(self):
        with self.app.store.locked() as state:
            state["pending_checkout"]["unpaid_order_binding_source"] = "oda_retry_available_page"
        def paid(context, before_abort, *, order_id, **kwargs):
            self.merchant.status = "paid_and_modifiable"
            return {"status": "paid", "order_id": order_id, "payment_id": "payment-1",
                    "terminal_status": "checkout-payment-success", "source": "oda_three_ds"}
        self.browser.abort_card_payment = paid
        self.browser.checkout_payment_authentication = lambda *a, **kw: None
        self.browser.checkout_payment_failure = lambda *a, **kw: None
        result = self.abort()
        self.assertTrue(result["confirmed"])
        self.assertIsNone(self.app.store.read()["pending_checkout"])

    def test_unbound_native_terminal_result_does_not_unlock_cancellation(self):
        def wrong_payment(context, before_abort, *, order_id, **kwargs):
            return {"status": "closed", "order_id": order_id, "payment_id": "another-payment",
                    "terminal_status": "checkout-payment-retry", "source": "oda_three_ds"}
        self.browser.abort_card_payment = wrong_payment
        result = self.abort()
        self.assertEqual(result["payment_abort_status"], "unknown")
        blocked = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.assertFalse(blocked["available"])
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])

    def test_closed_card_abort_allows_same_order_switch_without_second_abort_or_payment(self):
        self.abort()
        self.assertIn("exact payment is closed", workflow_status(self.app.store.read())["next_action"]["reason"])
        self.browser.checkout_payment_authentication = lambda *a, **kw: {
            "active": True, "challenge": True}
        switched = self.flow.call("switch_payment", confirmation_id=self.confirmation,
                                  checkout_payment={"method": "vipps"})
        self.assertTrue(switched["recovery"])
        self.assertEqual(switched["order_id"], "order-1")
        self.assertNotEqual(switched["confirmation_id"], self.confirmation)
        self.assertEqual(self.abort_clicks, 1)
        self.assertEqual(self.browser.clicks, 0)

    def test_prepared_replacement_payment_does_not_block_same_order_cancellation(self):
        self.abort()
        switched = self.flow.call("switch_payment", confirmation_id=self.confirmation,
                                  checkout_payment={"method": "vipps"})
        self.assertTrue(switched["recovery"])
        self.assertEqual(self.app.store.read()["pending_checkout"]["recovery"]["status"], "awaiting_confirmation")
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.assertTrue(prepared["available"])
        self.assertEqual(self.abort_clicks, 1)
        with self.assertRaisesRegex(HouseholdError, "pending order cancellation"):
            self.flow.call("switch_payment", confirmation_id=switched["confirmation_id"],
                           checkout_payment={"method": "saved_card"})

    def test_closed_vipps_abort_is_reused_by_switch_without_second_cancel(self):
        vipps = {"tab_id": "vipps-tab", "order_id": "order-1", "expected_total": 24640,
                 "gateway_url_digest": "a" * 64}
        with self.app.store.locked() as state:
            child = state["pending_checkout"]["recovery"]
            child["browser_review"]["payment_choice"] = {"method": "vipps"}
            child.pop("authentication_context", None)
            child["vipps_request_status"] = "sent"
            child["vipps_request_context"] = deepcopy(vipps)
            child["payment_requested_at"] = "2026-09-09T20:00:00+00:00"
        cancellations = 0
        def close_vipps(context, before_cancel, *, prior, **kwargs):
            nonlocal cancellations
            self.assertEqual(context, vipps)
            self.assertFalse(prior.get("cancel_attempted"))
            before_cancel({"payment_id": "456", "gateway_url_digest": vipps["gateway_url_digest"]})
            cancellations += 1
            return {"status": "closed", "terminal_status": "REJECTED", "source": "native_http200",
                    "gateway_url_digest": vipps["gateway_url_digest"], "payment_id": "456"}
        self.browser.close_vipps_request = close_vipps
        closed = self.abort()
        self.assertEqual(closed["payment_abort_status"], "closed")
        self.assertIn("exact payment is closed", workflow_status(self.app.store.read())["next_action"]["reason"])
        switched = self.flow.call("switch_payment", confirmation_id=self.confirmation,
                                  checkout_payment={"method": "saved_card"})
        self.assertTrue(switched["recovery"])
        self.assertEqual(switched["order_id"], "order-1")
        self.assertEqual(cancellations, 1)
        self.assertEqual(self.browser.clicks, 0)

    def test_native_paid_vipps_abort_reconciles_legacy_retry_page_purchase(self):
        vipps = {"tab_id": "vipps-tab", "order_id": "order-1", "expected_total": 24640,
                 "gateway_url_digest": "a" * 64}
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending["unpaid_order_binding_source"] = "oda_retry_available_page"
            child = pending["recovery"]
            child["browser_review"]["payment_choice"] = {"method": "vipps"}
            child.pop("authentication_context", None)
            child["vipps_request_status"] = "sent"
            child["vipps_request_context"] = deepcopy(vipps)
            child["payment_requested_at"] = "2026-09-09T20:00:00+00:00"
        def paid(context, before_cancel, **kwargs):
            self.merchant.status = "paid_and_modifiable"
            return {"status": "paid", "terminal_status": "ACCEPTED", "source": "native_http200",
                    "gateway_url_digest": vipps["gateway_url_digest"], "payment_id": "456"}
        self.browser.close_vipps_request = paid
        result = self.abort()
        self.assertTrue(result["confirmed"])
        self.assertIsNone(self.app.store.read()["pending_checkout"])

    def test_unknown_switch_cancel_fence_is_reused_by_later_abort(self):
        vipps = {"tab_id": "vipps-tab", "order_id": "order-1", "expected_total": 24640,
                 "gateway_url_digest": "a" * 64}
        with self.app.store.locked() as state:
            child = state["pending_checkout"]["recovery"]
            child["browser_review"]["payment_choice"] = {"method": "vipps"}
            child.pop("authentication_context", None)
            child["vipps_request_status"] = "sent"
            child["vipps_request_context"] = deepcopy(vipps)
            child["payment_requested_at"] = "2026-09-09T20:00:00+00:00"
        cancellations = 0
        def close_vipps(context, before_cancel, *, prior, **kwargs):
            nonlocal cancellations
            if not prior.get("cancel_attempted"):
                before_cancel({"payment_id": "456", "gateway_url_digest": vipps["gateway_url_digest"]})
                cancellations += 1
            return {"status": "unknown"}
        self.browser.close_vipps_request = close_vipps
        switched = self.flow.call("switch_payment", confirmation_id=self.confirmation,
                                  checkout_payment={"method": "saved_card"})
        self.assertTrue(switched["payment_switch_pending"])
        aborted = self.abort()
        self.assertEqual(aborted["payment_abort_status"], "unknown")
        self.assertEqual(cancellations, 1)

    def test_native_card_failure_is_journaled_without_retry_page_readiness(self):
        self.browser.payment_state = "unknown"
        with self.app.store.locked() as state:
            state["pending_checkout"]["unpaid_order_binding_source"] = "oda_retry_available_page"
        self.browser.checkout_payment_authentication = lambda *a, **kw: None
        self.browser.checkout_payment_failure = lambda context, **kw: {
            "payment_failed": True, "order_id": "order-1"}
        result = self.flow.call("reconcile", confirmation_id=self.confirmation)
        self.assertFalse(result["confirmed"])
        child = self.app.store.read()["pending_checkout"]["recovery"]
        self.assertEqual(child["payment_failure"], {"payment_failed": True, "order_id": "order-1"})
        self.browser.checkout_payment_failure = lambda *a, **kw: self.fail("retained tab must not be needed")
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.assertTrue(prepared["available"])

    def test_prepared_switch_after_journaled_native_failure_can_cancel_order(self):
        with self.app.store.locked() as state:
            state["pending_checkout"]["recovery"]["payment_failure"] = {
                "payment_failed": True, "order_id": "order-1"}
        switched = self.flow.call("switch_payment", confirmation_id=self.confirmation,
                                  checkout_payment={"method": "vipps"})
        self.assertTrue(switched["recovery"])
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.assertTrue(prepared["available"])

    def test_ordinary_reprepare_after_native_failure_can_cancel_order(self):
        with self.app.store.locked() as state:
            state["pending_checkout"]["recovery"]["payment_failure"] = {
                "payment_failed": True, "order_id": "order-1"}
        reviewed = self.flow.call("prepare", recovery=True,
                                  checkout_payment={"method": "vipps"})
        self.assertTrue(reviewed["recovery"])
        self.assertEqual(self.app.store.read()["pending_checkout"]["recovery"]["status"], "awaiting_confirmation")
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.assertTrue(prepared["available"])

    def test_cancelled_merchant_switch_replays_terminal_without_crash(self):
        self.abort()
        self.merchant.status = "cancelled"
        result = self.flow.call("switch_payment", confirmation_id=self.confirmation,
                                checkout_payment={"method": "vipps"})
        self.assertTrue(result["cancelled"])
        self.assertIsNone(self.app.store.read()["pending_checkout"])

    def test_same_order_cancellation_requires_closed_payment_and_keeps_policy(self):
        blocked = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.assertFalse(blocked["available"])
        self.assertEqual(blocked["next_action"], {"operation": "checkout", "action": "abort_payment",
                                                  "confirmation_id": self.confirmation})
        with self.assertRaisesRegex(HouseholdError, "pending checkout"):
            self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "different"})
        self.abort()
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.assertTrue(prepared["available"])
        self.assertTrue(prepared["confirmation_required"])
        self.assertEqual(prepared["confirmation_policy"], "fresh")
        self.app.confirmation_policy = "standing"
        standing = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.assertFalse(standing["confirmation_required"])
        self.assertEqual(standing["confirmation_policy"], "standing")

    def test_pending_cancellation_blocks_recovery_payment_claim(self):
        self.abort()
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        workflow = workflow_status(self.app.store.read())
        self.assertEqual(workflow["next_action"]["action"], "cancel_confirm")
        self.assertEqual(workflow["next_action"]["confirmation_id"], prepared["confirmation_id"])
        with self.app.store.locked() as state:
            pending = deepcopy(state["pending_checkout"])
            child = deepcopy(pending["recovery"])
        with self.assertRaisesRegex(CheckoutPreconditionError, "pending cancellation"):
            self.app._recovery_dispatch_state_guard(self.app.store.read(), pending, child,
                                                    {}, "unused", None, claim=True)
        self.assertEqual(self.abort_clicks, 1)

    def test_checkout_change_after_cancellation_review_blocks_final_click(self):
        self.abort()
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        cancellation_clicks = 0
        def count_cancellation(*args, **kwargs):
            nonlocal cancellation_clicks
            cancellation_clicks += 1
        self.browser.submit_cancellation = count_cancellation
        with self.app.store.locked() as state:
            state["pending_checkout"]["recovery"]["payment_abort"]["closure"]["terminal_status"] = "changed"
        with self.assertRaisesRegex(HouseholdError, "bound payment changed"):
            self.app.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "order-1",
                             "confirmation_id": prepared["confirmation_id"]})
        self.assertEqual(cancellation_clicks, 0)
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])

    def test_unverified_merchant_cancellation_retains_checkout(self):
        self.abort()
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        self.browser.submit_cancellation = lambda order_id, order, review, before_click, **kw: before_click()
        result = self.app.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "order-1",
                                  "confirmation_id": prepared["confirmation_id"]})
        self.assertFalse(result["cancelled"])
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])
        self.assertEqual(self.app.store.read()["pending_cancellation"]["status"], "uncertain")
        with self.assertRaisesRegex(HouseholdError, "pending order cancellation"):
            self.flow.call("reconcile", confirmation_id=self.confirmation)
        self.merchant.status = "cancelled"
        resolved = self.app.handle({"operation": "orders", "action": "cancel_reconcile",
                                    "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(resolved["cancelled"])
        self.assertIsNone(self.app.store.read()["pending_checkout"])

    def test_cancelled_merchant_reconcile_archives_both_confirmations(self):
        self.abort()
        self.merchant.status = "cancelled"
        result = self.flow.call("reconcile", confirmation_id=self.confirmation)
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["confirmed"])
        self.assertIsNone(self.app.store.read()["pending_checkout"])
        for confirmation in ("original", self.confirmation):
            replay = self.flow.call("reconcile", confirmation_id=confirmation)
            self.assertTrue(replay["cancelled"])
            self.assertFalse(replay["confirmed"])
            self.assertFalse(replay["retry_allowed"])
            self.assertEqual(replay["payment_resolution"], {
                "authorization_release": "unknown", "refund": "unknown"})
            stale = self.flow.call("confirm", confirmation_id=confirmation)
            self.assertTrue(stale["cancelled"])

    def test_cancellation_click_archives_checkout_only_on_positive_merchant_status(self):
        self.abort()
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-1"})
        result = self.app.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "order-1",
                                  "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["cancelled"])
        self.assertIsNone(self.app.store.read()["pending_checkout"])
        self.assertTrue(self.flow.call("reconcile", confirmation_id=self.confirmation)["cancelled"])


if __name__ == "__main__":
    unittest.main()
