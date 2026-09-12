"""Recover one merchant order through Application and a reopened on-disk journal."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import StateStore, HouseholdError, CheckoutPreconditionError, cart_summary
from service import Application
from oda_browser import _oda_checkout_amounts_minor

CART = {"items": [{"product_id": "67626", "name": "Wholegrain pasta", "quantity": 1, "price": 16.70}],
        "count": 1, "totalGrossAmount": 246.40,
        "deliverySlot": {"name": "Lør 12. september 07:00 - 13:00"}, "deliveryAddress": "Example street 1"}
AMOUNTS = {"product_subtotal": 16.70, "delivery_price": 19.0, "discounts": None,
           "deposits": None, "bags": 11.70, "other_fees": {"Tillegg for mindre bestilling": 199.0}, "provider_total": 246.40}


class Merchant:
    provider = "oda"

    def __init__(self):
        self.status = "unpaid_order"
        self.order = {"orderNumber": "order-1", "currency": "NOK", "grossAmount": 246.4,
                      "products": deepcopy(CART["items"]), "deliveryDate": "2026-09-12",
                      "deliverySlotDisplay": "Lør 12. september 07:00 - 13:00"}
        self.calls = []

    def probe(self, **kwargs):
        return {"server": {"name": "Synthetic merchant"}, "tool_count": 3}

    def call(self, name, arguments, **kwargs):
        self.calls.append(name)
        if name == "get_orders":
            return {"orders": [deepcopy(self.order)]}
        if name == "get_order":
            assert arguments["order_number"] == self.order["orderNumber"]
            return deepcopy(self.order)
        if name == "order_tracking":
            return {"orderNumber": self.order["orderNumber"], "status": self.status}
        if name == "product_search":
            return {"provider": "oda", "products": []}
        raise AssertionError("Recovery must not access or recreate the cart: " + name)


class MerchantBrowser:
    def __init__(self, merchant):
        self.merchant = merchant
        self.clicks = 0
        self.lost_response = False
        self.precondition_failure = False
        self.review_change = None
        self.payment_state = "unknown"
        self.vipps_request_state = "unknown"
        self.recovery_surface = "unknown"
        self.binding_reads = 0

    def review_payment_recovery(self, cart, order_id, *, payment, expected_binding, **kwargs):
        self.recovery_surface = "retry"
        review = {"order_id": order_id, "binding": deepcopy(expected_binding), "payment_choice": deepcopy(payment),
                  "payment_display": "Vipps" if payment["method"] == "vipps" else "•••• 1234", "amounts_minor": _oda_checkout_amounts_minor(AMOUNTS)}
        if self.review_change:
            self.review_change(review)
        return review

    def submit_payment_recovery(self, cart, review, before_click, **kwargs):
        if self.precondition_failure:
            raise CheckoutPreconditionError("merchant review changed")
        self.recovery_surface = "retry"
        before_click()
        if self.recovery_surface != "retry":
            raise CheckoutPreconditionError("recovery navigation changed before click")
        self.clicks += 1
        if self.lost_response:
            raise HouseholdError("response lost after dispatch")
        self.merchant.status = "paid_and_modifiable"

    def read_order_binding(self, order_id, order, *, expected_binding, **kwargs):
        self.binding_reads += 1
        self.recovery_surface = "order"
        return expected_binding

    def order_payment_state(self, order_id, **kwargs):
        self.recovery_surface = "order"
        return {"status": self.payment_state}

    def checkout_vipps_request_state(self, context, **kwargs):
        self.recovery_surface = "vipps"
        return {"status": self.vipps_request_state}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.merchant = Merchant()
        self.browser = MerchantBrowser(self.merchant)
        self.settings = {"provider": "oda", "household": "Synthetic", "checkout_payment": {"method": "vipps"}}
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        self.now = datetime(2026, 9, 9, 20, tzinfo=timezone.utc)
        self.app._now = lambda: self.now
        with self.app.store.locked() as state:
            state["pending_checkout"] = {
                "confirmation_id": "original", "status": "uncertain",
                "expires_at": "2026-09-09T18:00:00+00:00", "cart": deepcopy(CART),
                "summary": {**cart_summary(CART), "payment_method": "vipps"}, "orders_before": {"orders": []},
                "browser_review": {"account_reference_digest": "a" * 64, "payment_display": "Vipps", "amounts": deepcopy(AMOUNTS)},
                "checkout_payment": deepcopy(state["checkout_payment"]), "order_change": None,
                "vipps_request_status": "expired",
                "unpaid_order_id": "order-1",
                "unpaid_order_binding_source": "oda_checkout_pay_response",
            }
            state["pending_checkout"]["summary"]["delivery"]["slot"] = {
                "slot_ref": "oda:2026-09-12:70", "provider_slot_id": 70,
                "start_at": "2026-09-12T05:00:00Z", "end_at": "2026-09-12T11:00:00Z",
                "price_kind": "exact", "price_ore": 1900, "selected": True,
            }
        self.original = self.app.store.read()["pending_checkout"]

    def call(self, action, **kwargs):
        if action == "prepare" and kwargs.get("recovery") and kwargs.get("order_id"):
            kwargs.setdefault("confirmation_id", "original")
            kwargs.setdefault("vipps_request_not_received", True)
        return self.app.handle({"operation": "checkout", "action": action, **kwargs})

    def prepare(self):
        return self.call("prepare", recovery=True)

    def prepare_saved_card_to_vipps_recovery(self):
        with self.app.store.locked() as state:
            state["checkout_payment"] = {"method": "saved_card"}
            pending = state["pending_checkout"]
            pending["checkout_payment"] = {"method": "saved_card"}
            pending["browser_review"]["payment_display"] = "•••• 1234"
            pending.pop("vipps_request_status", None)
            pending.pop("unpaid_order_binding_source", None)
        prepared = self.call("prepare", recovery=True, checkout_payment={"method": "vipps"})

        def submit(_cart, review, before_click, **kwargs):
            before_click()
            self.browser.clicks += 1
            context = {
                "tab_id": "vipps-tab", "expected_total": 24640,
                "gateway_url_digest": "a" * 64, "order_id": review["order_id"],
            }
            kwargs["before_vipps_request"](context)
            return {"vipps_request_sent": True, "vipps_request_context": context}

        self.browser.submit_payment_recovery = submit
        self.browser.checkout_vipps_request_state = lambda *args, **kwargs: {"status": "sent"}
        waiting = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(waiting["payment_request_sent"])
        self.assertEqual(
            self.app.store.read()["pending_checkout"]["recovery"]["vipps_request_context"]["order_id"],
            "order-1",
        )
        return prepared

    def test_saved_card_to_vipps_recovery_confirms_the_response_bound_order(self):
        prepared = self.prepare_saved_card_to_vipps_recovery()
        self.merchant.status = "paid_and_modifiable"
        result = self.call("reconcile", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["order_id"], "order-1")

    def test_saved_card_to_vipps_recovery_accepts_positive_expiry(self):
        prepared = self.prepare_saved_card_to_vipps_recovery()
        self.browser.checkout_vipps_request_state = lambda *args, **kwargs: {"status": "expired"}
        result = self.call("reconcile", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(result["payment_request_expired"])
        self.assertTrue(result["payment_failed"])
        self.assertTrue(result["recovery_preparation_available"])

    def test_same_order_recovery_and_both_confirmation_replays(self):
        prepared = self.prepare()
        self.assertEqual(prepared["order_id"], "order-1")
        self.assertNotEqual(prepared["confirmation_id"], "original")
        self.assertEqual(self.browser.clicks, 0)
        saved = self.app.store.read()["pending_checkout"]
        self.assertEqual({k: v for k, v in saved.items() if k != "recovery"}, self.original)
        self.assertFalse(self.call("confirm", confirmation_id="original")["confirmed"])
        self.assertEqual(self.browser.clicks, 0)
        result = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["order_id"], "order-1")
        for key in ("original", prepared["confirmation_id"]):
            self.assertTrue(self.call("confirm", confirmation_id=key)["idempotent"])
            self.assertTrue(self.call("reconcile", confirmation_id=key)["confirmed"])
        self.assertEqual(self.browser.clicks, 1)
        self.assertIsNone(self.app.store.read()["pending_checkout"])
        self.assertNotIn("get_cart", self.merchant.calls)

    def test_exact_retry_order_rejects_conflicting_paid_tracking(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending["status"] = "awaiting_user_payment"
            pending.pop("vipps_request_status")
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.merchant.status = "paid_and_not_modifiable"
        self.browser.payment_state = "retry_available"

        with self.assertRaisesRegex(HouseholdError, "no longer unpaid"):
            self.call("prepare", recovery=True, order_id="order-1")

        self.assertEqual(self.browser.clicks, 0)
        pending = self.app.store.read()["pending_checkout"]
        self.assertNotIn("unpaid_order_id", pending)
        self.assertNotIn("unpaid_order_binding_source", pending)

    def test_exact_retry_order_recovers_after_tracking_converges_to_unpaid(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("vipps_request_status")
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "retry_available"

        prepared = self.call("prepare", recovery=True, order_id="order-1")

        self.assertTrue(prepared["recovery"])
        self.assertEqual(prepared["order_id"], "order-1")
        self.assertEqual(self.browser.clicks, 0)

    def test_exact_retry_requires_owner_no_request_report_and_original_confirmation(self):
        request = {
            "operation": "checkout", "action": "prepare", "recovery": True,
            "order_id": "order-1",
        }
        for changes in ({}, {"vipps_request_not_received": True}, {
                "vipps_request_not_received": True, "confirmation_id": "other"}):
            with self.subTest(changes=changes), self.assertRaises(HouseholdError):
                self.app.handle({**request, **changes})

        self.assertEqual(self.browser.clicks, 0)

    def test_order_id_is_rejected_outside_exact_recovery_or_terminal_replay(self):
        requests = (
            {"action": "confirm", "confirmation_id": "original"},
            {"action": "reconcile", "confirmation_id": "original"},
            {"action": "submit", "idempotency_key": "new-intent"},
        )
        for request in requests:
            with self.subTest(action=request["action"]), self.assertRaisesRegex(
                    HouseholdError, "only for exact-order recovery"):
                self.call(order_id="order-1", **request)

    def test_exact_retry_records_owner_report_on_the_fresh_confirmation(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("vipps_request_status")
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "retry_available"

        prepared = self.call("prepare", recovery=True, order_id="order-1")

        child = self.app.store.read()["pending_checkout"]["recovery"]
        self.assertEqual(child["confirmation_id"], prepared["confirmation_id"])
        self.assertTrue(child["owner_reported_no_vipps_request"])
        self.assertEqual(child["original_confirmation_id"], "original")

    def test_exact_retry_dietary_drift_requires_fresh_owner_evidence(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("vipps_request_status")
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "retry_available"
        prepared = self.call("prepare", recovery=True, order_id="order-1")
        with self.app.store.locked() as state:
            state["profile"]["diet"]["allergies_or_sensitivities"] = ["mustard"]

        with self.assertRaisesRegex(HouseholdError, "prepare exact recovery again"):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])

        child = self.app.store.read()["pending_checkout"]["recovery"]
        self.assertEqual(child["confirmation_id"], prepared["confirmation_id"])
        self.assertTrue(child["owner_reported_no_vipps_request"])
        self.assertEqual(child["original_confirmation_id"], "original")
        self.assertEqual(self.browser.clicks, 0)

    def test_exact_retry_page_recovery_cannot_switch_away_from_vipps(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("vipps_request_status")
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "retry_available"

        with self.assertRaisesRegex(HouseholdError, "must preserve Vipps"):
            self.call(
                "prepare", recovery=True, order_id="order-1",
                checkout_payment={"method": "saved_card"},
            )

        self.assertEqual(self.browser.clicks, 0)

    def test_exact_order_recovery_requires_the_payment_started_page(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "unknown"

        with self.assertRaisesRegex(HouseholdError, "no longer unpaid"):
            self.call("prepare", recovery=True, order_id="order-1")

        self.assertEqual(self.browser.clicks, 0)
        self.assertNotIn("unpaid_order_id", self.app.store.read()["pending_checkout"])

    def test_exact_order_recovery_rejects_an_order_present_before_checkout(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending["orders_before"] = {"orders": [{"orderNumber": "order-1"}]}
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "retry_available"

        with self.assertRaisesRegex(HouseholdError, "exactly one new merchant order"):
            self.call("prepare", recovery=True, order_id="order-1")

        self.assertEqual(self.browser.clicks, 0)

    def test_exact_payment_started_recovery_rechecks_the_page_before_dispatch(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "retry_available"
        prepared = self.call("prepare", recovery=True, order_id="order-1")

        def changed_retry_surface(_cart, _review, before_click, **_kwargs):
            before_click()
            raise CheckoutPreconditionError("recovery retry surface changed")

        self.browser.submit_payment_recovery = changed_retry_surface

        with self.assertRaisesRegex(CheckoutPreconditionError, "retry surface changed"):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])

        self.assertEqual(self.browser.clicks, 0)

    def test_exact_payment_started_recovery_dispatches_one_bound_vipps_request(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "retry_available"
        prepared = self.call("prepare", recovery=True, order_id="order-1")

        def submit(_cart, review, before_click, **kwargs):
            before_click()
            self.browser.clicks += 1
            context = {
                "tab_id": "vipps-tab", "expected_total": 24640,
                "gateway_url_digest": "a" * 64, "order_id": review["order_id"],
            }
            kwargs["before_vipps_request"](context)
            return {"vipps_request_sent": True}

        self.browser.submit_payment_recovery = submit
        waiting = self.call("confirm", confirmation_id=prepared["confirmation_id"])

        self.assertTrue(waiting["payment_request_sent"])
        self.assertEqual(waiting["order_id"], "order-1")
        self.assertEqual(self.browser.clicks, 1)
        pending = self.app.store.read()["pending_checkout"]
        self.assertEqual(pending["recovery"]["vipps_request_status"], "sent")
        self.assertEqual(pending["recovery"]["vipps_request_context"]["order_id"], "order-1")
        self.merchant.status = "paid_and_modifiable"
        self.browser.vipps_request_state = "sent"
        self.browser.payment_state = "unknown"
        self.assertTrue(self.call(
            "reconcile", confirmation_id=prepared["confirmation_id"],
            vipps_approval_completed=True,
        )["confirmed"])
        self.assertEqual(self.browser.clicks, 1)

    def test_exact_retry_paid_tracking_stays_locked_without_owner_approval(self):
        for observed in ("sent", "expired", "unknown"):
            with self.subTest(observed=observed):
                with self.app.store.locked() as state:
                    state["pending_checkout"] = deepcopy(self.original)
                    pending = state["pending_checkout"]
                    pending.pop("vipps_request_status")
                    pending.pop("unpaid_order_id")
                    pending.pop("unpaid_order_binding_source")
                self.merchant.status = "unpaid_order"
                self.browser.payment_state = "retry_available"
                prepared = self.call("prepare", recovery=True, order_id="order-1")

                def submit(_cart, review, before_click, **kwargs):
                    before_click()
                    self.browser.clicks += 1
                    context = {
                        "tab_id": "vipps-tab", "expected_total": 24640,
                        "gateway_url_digest": "a" * 64, "order_id": review["order_id"],
                    }
                    kwargs["before_vipps_request"](context)
                    return {"vipps_request_sent": True}

                self.browser.submit_payment_recovery = submit
                self.call("confirm", confirmation_id=prepared["confirmation_id"])
                self.merchant.status = "paid_and_not_modifiable"
                self.browser.vipps_request_state = observed
                binding_reads = self.browser.binding_reads

                result = self.call("reconcile", confirmation_id=prepared["confirmation_id"])

                self.assertFalse(result["confirmed"])
                self.assertIsNotNone(self.app.store.read()["pending_checkout"])
                self.assertEqual(self.browser.binding_reads, binding_reads)
                self.assertEqual(self.browser.recovery_surface, "vipps")

    def test_exact_retry_expired_page_blocks_owner_approval_claim(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("vipps_request_status")
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "retry_available"
        prepared = self.call("prepare", recovery=True, order_id="order-1")

        def submit(_cart, review, before_click, **kwargs):
            before_click()
            context = {
                "tab_id": "vipps-tab", "expected_total": 24640,
                "gateway_url_digest": "a" * 64, "order_id": review["order_id"],
            }
            kwargs["before_vipps_request"](context)
            return {"vipps_request_sent": True}

        self.browser.submit_payment_recovery = submit
        self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.merchant.status = "paid_and_not_modifiable"
        self.browser.vipps_request_state = "expired"

        result = self.call(
            "reconcile", confirmation_id=prepared["confirmation_id"],
            vipps_approval_completed=True,
        )

        self.assertFalse(result["confirmed"])
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])

    def test_owner_vipps_approval_requires_the_exact_dispatched_recovery(self):
        with self.assertRaisesRegex(HouseholdError, "exact dispatched Oda recovery"):
            self.call(
                "reconcile", confirmation_id="original",
                vipps_approval_completed=True,
            )

    def test_exact_retry_preclick_provider_check_keeps_the_retry_surface_current(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("vipps_request_status")
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
        self.browser.payment_state = "retry_available"
        prepared = self.call("prepare", recovery=True, order_id="order-1")

        result = self.call("confirm", confirmation_id=prepared["confirmation_id"])

        self.assertFalse(result["confirmed"])
        self.assertEqual(self.browser.clicks, 1)

    def test_exact_retry_page_reports_the_tracking_conflict_without_rewriting_it(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending["unpaid_order_binding_source"] = "oda_retry_available_page"
            pending.pop("vipps_request_status")
            pending.pop("vipps_request_context", None)
        self.merchant.status = "paid_and_not_modifiable"
        self.browser.payment_state = "retry_available"

        result = self.call("reconcile", confirmation_id="original")

        self.assertFalse(result["confirmed"])
        self.assertEqual(result["payment"]["provider_status"], "paid_and_not_modifiable")
        self.assertEqual(result["tracking_conflict"], {
            "provider_tracking_status": "paid_and_not_modifiable",
            "order_page_status": "retry_available",
        })
        self.assertNotIn("recovery_preparation_available", result)
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])

    def test_unknown_exact_retry_page_never_confirms_a_prepared_recovery(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending["unpaid_order_binding_source"] = "oda_retry_available_page"
        self.merchant.status = "paid_and_not_modifiable"

        result = self.call("reconcile", confirmation_id="original")

        self.assertFalse(result["confirmed"])
        self.assertNotIn("tracking_conflict", result)
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])

    def test_active_original_vipps_request_blocks_an_exact_retry(self):
        for request_status, observed in (("sent", "sent"), ("dispatching", "unknown")):
            with self.subTest(request_status=request_status, observed=observed):
                with self.app.store.locked() as state:
                    state["pending_checkout"] = deepcopy(self.original)
                    pending = state["pending_checkout"]
                    pending.pop("unpaid_order_id")
                    pending.pop("unpaid_order_binding_source")
                    pending["vipps_request_status"] = request_status
                    pending["vipps_request_context"] = {
                        "tab_id": "original-tab", "expected_total": 24640,
                        "gateway_url_digest": "b" * 64, "order_id": "order-1",
                    }
                self.browser.vipps_request_state = observed
                self.browser.payment_state = "retry_available"

                result = self.call("prepare", recovery=True, order_id="order-1")

                self.assertFalse(result["confirmed"])
                self.assertFalse(result["retry_allowed"])
                self.assertNotIn("recovery", result)
                self.assertEqual(self.browser.clicks, 0)

    def test_retry_page_does_not_override_a_recorded_active_vipps_request(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending["unpaid_order_binding_source"] = "oda_retry_available_page"
            pending["vipps_request_status"] = "sent"
            pending["vipps_request_context"] = {
                "tab_id": "original-tab", "expected_total": 24640,
                "gateway_url_digest": "b" * 64, "order_id": "order-1",
            }
        self.merchant.status = "paid_and_not_modifiable"
        self.browser.payment_state = "retry_available"
        self.browser.vipps_request_state = "sent"

        result = self.call("reconcile", confirmation_id="original")

        self.assertFalse(result["confirmed"])
        self.assertTrue(result["awaiting_user_payment"])
        self.assertNotIn("recovery_preparation_available", result)
        self.assertEqual(self.browser.clicks, 0)
        self.assertEqual(self.browser.recovery_surface, "vipps")

    def test_original_vipps_request_becoming_active_after_review_blocks_dispatch(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
            pending.pop("vipps_request_status")
        self.browser.payment_state = "retry_available"
        prepared = self.call("prepare", recovery=True, order_id="order-1")
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending["vipps_request_status"] = "sent"
            pending["vipps_request_context"] = {
                "tab_id": "original-tab", "expected_total": 24640,
                "gateway_url_digest": "b" * 64, "order_id": "order-1",
            }

        with self.assertRaisesRegex(HouseholdError, "not positively closed"):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])

        self.assertEqual(self.browser.clicks, 0)

    def test_exact_retry_rejects_non_payment_tracking_states(self):
        for tracking_status in ("picking", "delivered", "cancelled", "nonsense", ""):
            with self.subTest(tracking_status=tracking_status):
                with self.app.store.locked() as state:
                    state["pending_checkout"] = deepcopy(self.original)
                    pending = state["pending_checkout"]
                    pending.pop("unpaid_order_id")
                    pending.pop("unpaid_order_binding_source")
                    pending.pop("vipps_request_status")
                self.merchant.status = tracking_status
                self.browser.payment_state = "retry_available"

                with self.assertRaisesRegex(HouseholdError, "no longer unpaid"):
                    self.call("prepare", recovery=True, order_id="order-1")

                self.assertEqual(self.browser.clicks, 0)

    def test_exact_retry_rejects_concurrent_post_checkout_orders(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
            pending.pop("vipps_request_status")
        original_call = self.merchant.call

        def call(name, arguments, **kwargs):
            if name == "get_orders":
                return {"orders": [deepcopy(self.merchant.order), {"orderNumber": "order-2"}]}
            return original_call(name, arguments, **kwargs)

        self.merchant.call = call
        self.browser.payment_state = "retry_available"

        with self.assertRaisesRegex(HouseholdError, "exactly one new merchant order"):
            self.call("prepare", recovery=True, order_id="order-1")

        self.assertEqual(self.browser.clicks, 0)

    def test_clicking_without_exact_vipps_request_never_confirms_stale_paid_tracking(self):
        for payment_state in ("retry_available", "unknown"):
            with self.subTest(payment_state=payment_state):
                with self.app.store.locked() as state:
                    state["pending_checkout"] = deepcopy(self.original)
                    pending = state["pending_checkout"]
                    pending.pop("vipps_request_status")
                    pending.pop("unpaid_order_id")
                    pending.pop("unpaid_order_binding_source")
                self.merchant.status = "unpaid_order"
                self.browser.payment_state = "retry_available"
                prepared = self.call("prepare", recovery=True, order_id="order-1")
                with self.app.store.locked() as state:
                    state["pending_checkout"]["recovery"]["status"] = "clicking"
                self.merchant.status = "paid_and_not_modifiable"
                self.browser.payment_state = payment_state

                result = self.call("reconcile", confirmation_id=prepared["confirmation_id"])

                self.assertFalse(result["confirmed"])
                self.assertIsNotNone(self.app.store.read()["pending_checkout"])
                self.assertEqual(self.browser.clicks, 0)

    def test_bank_challenge_recovery_survives_restart_and_lost_browser_without_repayment(self):
        prepared = self.call("prepare", recovery=True, checkout_payment={"method": "saved_card"})
        context = {"tab_id": "owned", "payment_id": "123456"}
        def submit(cart, review, before_click, **kwargs):
            before_click()
            self.browser.clicks += 1
            return {"authentication_context": context}
        self.browser.submit_payment_recovery = submit
        self.browser.checkout_payment_authentication = lambda actual, **kwargs: {"active": True, "challenge": True}
        result = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(result["authentication_required"])
        self.assertEqual(result["summary"], prepared["summary"])
        self.assertEqual(result["payment_method"], "saved_card")
        self.assertEqual(result["confirmation_id"], prepared["confirmation_id"])
        self.assertEqual(result["original_confirmation_id"], "original")
        self.assertEqual(self.app.store.read()["pending_checkout"]["recovery"]["authentication_context"], context)
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        self.app._now = lambda: self.now + timedelta(days=1)
        for observed, status in [({"active": True, "challenge": True}, "challenge"),
                                 ({"active": True, "challenge": False}, "awaiting_outcome"), (None, "unavailable")]:
            self.browser.checkout_payment_authentication = lambda actual, **kw: observed
            for action, args in [("reconcile", {"confirmation_id": prepared["confirmation_id"]}),
                                 ("confirm", {"confirmation_id": prepared["confirmation_id"]}),
                                 ("prepare", {"recovery": True})]:
                result = self.call(action, **args)
                self.assertEqual(result["authentication_status"], status)
                self.assertFalse(result["recovery_preparation_available"])
                self.assertFalse(result["retry_allowed"])
                self.assertNotIn("authentication_context", result)
        self.assertEqual(self.browser.clicks, 1)
        self.merchant.status = "paid_and_modifiable"
        self.assertTrue(self.call("reconcile", confirmation_id=prepared["confirmation_id"])["confirmed"])
        self.assertEqual(self.browser.clicks, 1)

    def test_recovery_keeps_unresolved_marker_after_unbound_capture_and_restart(self):
        prepared = self.call("prepare", recovery=True, checkout_payment={"method": "saved_card"})
        def submit(cart, review, before_click, **kwargs):
            before_click()
            self.browser.clicks += 1
            return {"payment_failed": True, "order_id": "order-1"}
        self.browser.submit_payment_recovery = submit
        result = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertEqual(result["authentication_status"], "unavailable")
        self.assertTrue(self.app.store.read()["pending_checkout"]["recovery"]["authentication_unresolved"])
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        self.app._now = lambda: self.now
        for action in ("reconcile", "confirm", "authenticate"):
            result = self.call(action, confirmation_id=prepared["confirmation_id"])
            self.assertFalse(result["retry_allowed"])
            self.assertTrue(result["payment_followup_required"])
        self.assertEqual(self.browser.clicks, 1)
        self.merchant.status = "paid_and_modifiable"
        self.assertTrue(self.call("reconcile", confirmation_id=prepared["confirmation_id"])["confirmed"])

    def test_original_bank_context_blocks_recovery_even_when_observation_is_unavailable(self):
        with self.app.store.locked() as state:
            state["pending_checkout"]["authentication_context"] = {"tab_id": "owned", "payment_id": "123456"}
        self.browser.checkout_payment_authentication = lambda *a, **kw: None
        self.browser.review_payment_recovery = lambda *a, **kw: self.fail("must preserve the original payment page")
        result = self.prepare()
        self.assertEqual(result["authentication_status"], "unavailable")
        self.assertEqual(result["confirmation_id"], "original")
        self.assertFalse(result["recovery_preparation_available"])
        self.assertEqual(self.browser.clicks, 0)

    def test_bank_app_selection_is_once_even_after_lost_response_and_restart(self):
        context = {"tab_id": "owned", "payment_id": "123456"}
        with self.app.store.locked() as state:
            state["pending_checkout"]["authentication_context"] = context
        self.browser.checkout_payment_authentication = lambda *a, **kw: {"active": True, "challenge": True}
        choices = []
        def choose(actual, before_choice, **kwargs):
            self.assertEqual(actual, context)
            self.assertFalse(self.app.store.read()["pending_checkout"].get("bank_app_choice_attempted"))
            before_choice()
            self.assertTrue(self.app.store.read()["pending_checkout"]["bank_app_choice_attempted"])
            choices.append(True)
            raise HouseholdError("lost method-selection response")
        self.browser.choose_checkout_bank_app = choose
        with self.assertRaisesRegex(HouseholdError, "lost method"):
            self.call("authenticate", confirmation_id="original")
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        for _ in range(2):
            result = self.call("authenticate", confirmation_id="original")
            self.assertTrue(result["bank_app_choice_attempted"])
            self.assertTrue(result["authentication_required"])
        self.assertEqual(choices, [True])
        self.assertEqual(self.browser.clicks, 0)
        self.merchant.status = "paid_and_modifiable"
        self.assertTrue(self.call("authenticate", confirmation_id="original")["confirmed"])
        self.assertEqual(choices, [True])

    def test_bank_app_missing_chooser_and_changed_journal_do_not_claim_selection(self):
        with self.app.store.locked() as state:
            state["pending_checkout"]["authentication_context"] = {"tab_id": "owned", "payment_id": "123456"}
        self.browser.checkout_payment_authentication = lambda *a, **kw: {"active": True, "challenge": True}
        self.browser.choose_checkout_bank_app = lambda *a, **kw: {"chosen": False}
        result = self.call("authenticate", confirmation_id="original")
        self.assertFalse(result["bank_app_method_chosen"])
        self.assertFalse(result["bank_app_choice_attempted"])
        def changed(actual, before_choice, **kwargs):
            with self.app.store.locked() as state:
                state["pending_checkout"]["authentication_context"]["payment_id"] = "987654"
            before_choice()
            self.fail("must stop before bank method selection")
        self.browser.choose_checkout_bank_app = changed
        with self.assertRaisesRegex(HouseholdError, "pending payment changed"):
            self.call("authenticate", confirmation_id="original")
        self.assertNotIn("bank_app_choice_attempted", self.app.store.read()["pending_checkout"])
        self.assertEqual(self.browser.clicks, 0)

    def test_bank_app_selection_uses_dispatched_recovery_context_and_confirmation(self):
        prepared = self.call("prepare", recovery=True, checkout_payment={"method": "saved_card"})
        context = {"tab_id": "recovery", "payment_id": "987654"}
        def submit(cart, review, before_click, **kwargs):
            before_click()
            self.browser.clicks += 1
            return {"authentication_context": context}
        self.browser.submit_payment_recovery = submit
        self.browser.checkout_payment_authentication = lambda *a, **kw: {"active": True, "challenge": True}
        self.call("confirm", confirmation_id=prepared["confirmation_id"])
        def choose(actual, before_choice, **kwargs):
            self.assertEqual(actual, context)
            before_choice()
            pending = self.app.store.read()["pending_checkout"]
            self.assertTrue(pending["recovery"]["bank_app_choice_attempted"])
            self.assertNotIn("bank_app_choice_attempted", pending)
            return {"chosen": True}
        self.browser.choose_checkout_bank_app = choose
        with self.assertRaisesRegex(HouseholdError, "current payment"):
            self.call("authenticate", confirmation_id="original")
        result = self.call("authenticate", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(result["bank_app_method_chosen"])
        self.assertTrue(result["bank_app_choice_attempted"])
        self.assertEqual(self.browser.clicks, 1)

    def test_late_mathem_failure_persists_and_recovers_only_the_original_order(self):
        from unittest import mock
        self.settings.update(provider="mathem", checkout_payment={"method": "saved_card"})
        root = str(Path(self.temp.name) / "mathem")
        self.app = Application(StateStore(root, self.settings), self.merchant, self.browser)
        self.merchant.order["currency"] = "SEK"
        amounts = deepcopy(AMOUNTS)
        amounts["other_fees"] = {"Avgift för liten varukorg": 199.0}
        breakdown = {"product_discount": None, "delivery_discount": None}
        original_review = self.browser.review_payment_recovery
        def review(*args, **kwargs):
            return {**original_review(*args, **kwargs), "amounts_minor": {
                **_oda_checkout_amounts_minor(amounts, provider="mathem"), "discount_breakdown": breakdown}}
        self.browser.review_payment_recovery = review
        with self.app.store.locked() as state:
            state["pending_checkout"] = deepcopy(self.original)
            pending = state["pending_checkout"]
            pending["authentication_context"] = {"tab_id": "owned", "payment_id": "123456"}
            pending["checkout_payment"] = deepcopy(state["checkout_payment"])
            pending["browser_review"]["payment_display"] = "•••• 1234"
            pending["browser_review"]["amounts"] = amounts
            pending["browser_review"]["discount_breakdown"] = breakdown
            pending["summary"]["payment_method"] = "saved_card"
            pending["summary"]["delivery"]["slot"]["slot_ref"] = "mathem:2026-09-12:70"
        before = self.app.store.read()["pending_checkout"]
        failure = {"payment_failed": True, "order_id": "order-1"}
        self.browser.checkout_payment_authentication = mock.Mock(return_value=None)
        self.browser.checkout_payment_failure = mock.Mock(return_value=failure)
        result = self.call("reconcile", confirmation_id="original")
        self.assertTrue(result["payment_failed"])
        self.assertTrue(result["recovery_preparation_available"])
        self.assertEqual(self.app.store.read()["pending_checkout"], {**before, "payment_failure": failure})
        self.app = Application(StateStore(root, self.settings), self.merchant, self.browser)
        self.browser.checkout_payment_failure.reset_mock()
        self.assertTrue(self.call("authenticate", confirmation_id="original")["payment_failed"])
        original_call = self.merchant.call
        unrelated = {**deepcopy(self.merchant.order), "orderNumber": "order-2"}
        def exact_failure_outside_order_page(name, arguments, **kwargs):
            if name == "get_orders":
                return {"orders": [deepcopy(unrelated)]}
            return original_call(name, arguments, **kwargs)
        self.merchant.call = exact_failure_outside_order_page
        prepared = self.prepare()
        self.assertEqual(prepared["order_id"], "order-1")
        self.browser.checkout_payment_failure.assert_not_called()
        self.assertEqual(self.browser.clicks, 0)
        context = {"tab_id": "recovery-tab", "payment_id": "234567"}
        self.browser.checkout_payment_authentication.return_value = {"active": True, "challenge": True}
        def challenged(cart, review, before_click, **kwargs):
            before_click()
            self.browser.clicks += 1
            return {"authentication_context": context}
        self.browser.submit_payment_recovery = challenged
        cid = prepared["confirmation_id"]
        self.assertTrue(self.call("confirm", confirmation_id=cid)["authentication_required"])
        pending = self.app.store.read()["pending_checkout"]
        self.browser.checkout_payment_authentication.return_value = None
        self.browser.checkout_payment_failure.return_value = None
        unknown = self.call("reconcile", confirmation_id=cid)
        self.assertEqual(unknown["authentication_status"], "unavailable")
        self.assertFalse(unknown["recovery_preparation_available"])
        self.assertEqual(self.app.store.read()["pending_checkout"], pending)
        self.browser.checkout_payment_failure.return_value = {**failure, "order_id": "another"}
        with self.assertRaisesRegex(HouseholdError, "another merchant change"):
            self.call("reconcile", confirmation_id=cid)
        self.assertEqual(self.app.store.read()["pending_checkout"], pending)
        self.browser.checkout_payment_failure.return_value = failure
        self.browser.checkout_payment_failure.reset_mock()
        failed = self.call("reconcile", confirmation_id=cid)
        self.assertTrue(failed["payment_failed"])
        self.assertTrue(failed["recovery_preparation_available"])
        self.assertNotIn("recovery_payment_unconfirmed", failed)
        self.browser.checkout_payment_failure.assert_called_once_with(context, deadline=mock.ANY)
        retained = self.app.store.read()["pending_checkout"]
        self.assertEqual({k: v for k, v in retained.items() if k != "recovery"}, {**before, "payment_failure": failure})
        self.assertEqual(retained["recovery"], {**pending["recovery"], "payment_failure": failure})
        self.app = Application(StateStore(root, self.settings), self.merchant, self.browser)
        fresh = self.prepare()
        self.assertNotEqual(fresh["confirmation_id"], cid)
        archived = deepcopy(self.app.store.read()["protected_results"][cid])
        self.assertEqual(archived["failed_attempt"], retained["recovery"])
        for action in ("confirm", "reconcile", "authenticate"):
            self.assertTrue(self.call(action, confirmation_id=cid)["payment_failed"])
        self.assertEqual(self.browser.clicks, 1)
        self.browser.submit_payment_recovery = MerchantBrowser.submit_payment_recovery.__get__(self.browser)
        self.assertTrue(self.call("confirm", confirmation_id=fresh["confirmation_id"])["confirmed"])
        self.assertTrue(self.call("reconcile", confirmation_id="original")["confirmed"])
        self.assertFalse(self.call("reconcile", confirmation_id=cid)["confirmed"])
        self.assertEqual(self.app.store.read()["protected_results"][cid], archived)
        self.assertIsNone(self.app.store.read()["pending_checkout"])
        self.assertEqual(self.browser.clicks, 2)

    def test_late_mathem_failure_rejects_other_order_and_concurrent_change(self):
        self.app.provider = "mathem"
        self.merchant.order["currency"] = "SEK"
        with self.app.store.locked() as state:
            state["pending_checkout"]["authentication_context"] = {"tab_id": "owned", "payment_id": "123456"}
            state["pending_checkout"]["summary"]["delivery"]["slot"]["slot_ref"] = "mathem:2026-09-12:70"
        self.browser.checkout_payment_authentication = lambda *a, **kw: None
        before = self.app.store.read()["pending_checkout"]
        self.browser.checkout_payment_failure = lambda *a, **kw: {"payment_failed": True, "order_id": "another"}
        with self.assertRaisesRegex(HouseholdError, "another merchant order"):
            self.call("reconcile", confirmation_id="original")
        self.assertEqual(self.app.store.read()["pending_checkout"], before)
        def changed(*a, **kw):
            with self.app.store.locked() as state:
                state["pending_checkout"]["authentication_context"]["payment_id"] = "987654"
            return {"payment_failed": True, "order_id": "order-1"}
        self.browser.checkout_payment_failure = changed
        with self.assertRaisesRegex(HouseholdError, "changed while resolving"):
            self.call("reconcile", confirmation_id="original")
        self.assertNotIn("payment_failure", self.app.store.read()["pending_checkout"])
        self.assertEqual(self.browser.clicks, 0)

    def test_mathem_retry_can_omit_only_the_cancelled_delivery_and_credit_rows(self):
        settings = {"provider": "mathem", "household": "Synthetic", "checkout_payment": {"method": "saved_card"}}
        self.app = Application(StateStore(Path(self.temp.name) / "mathem", settings), self.merchant, self.browser)
        self.merchant.order.update(currency="SEK", grossAmount=227.40)
        pending = deepcopy(self.original)
        pending["cart"]["totalGrossAmount"] = 227.40
        pending["summary"] = cart_summary(pending["cart"])
        pending["summary"]["delivery"]["slot"] = {
            "slot_ref": "mathem:2026-09-12:70", "provider_slot_id": 70,
            "start_at": "2026-09-12T05:00:00Z", "end_at": "2026-09-12T11:00:00Z",
            "price_kind": "exact", "price_ore": 0, "selected": True,
        }
        pending["checkout_payment"] = deepcopy(self.app.store.read()["checkout_payment"])
        amounts = {**AMOUNTS, "delivery_price": 79.0, "discounts": -79.0,
                   "other_fees": {"Avgift för liten varukorg": 199.0}, "provider_total": 227.40}
        pending["browser_review"].update(amounts=amounts, payment_display="•••• 1234",
            discount_breakdown={"product_discount": None, "delivery_discount": -79.0})
        current = {**_oda_checkout_amounts_minor(amounts, provider="mathem"), "delivery_price": None,
                   "discounts": None, "discount_breakdown": {"product_discount": None, "delivery_discount": None}}
        def review(cart, order_id, *, expected_binding, payment, **kwargs):
            return {"order_id": order_id, "binding": expected_binding, "payment_choice": payment,
                    "payment_display": "•••• 1234", "amounts_minor": deepcopy(current)}
        self.browser.review_payment_recovery = review
        with self.app.store.locked() as state:
            state["pending_checkout"] = deepcopy(pending)
        prepared = self.prepare()
        self.assertEqual(prepared["summary"]["total"], 227.40)
        self.assertIsNone(prepared["summary"]["amounts"]["delivery_price"])
        self.assertIsNone(prepared["summary"]["amounts"]["discounts"])
        self.assertEqual(prepared["summary"]["discount_breakdown"], current["discount_breakdown"])
        self.assertEqual(prepared["summary"]["delivery"]["slot"]["price_ore"], 0)
        self.assertEqual({k: v for k, v in self.app.store.read()["pending_checkout"].items() if k != "recovery"}, pending)
        omitted = deepcopy(current)
        current.update(delivery_price=7900, discounts=-7900,
                       discount_breakdown={"product_discount": None, "delivery_discount": -7900})
        with self.app.store.locked() as state:
            state["pending_checkout"] = deepcopy(pending)
        retained = self.prepare()
        self.assertEqual(retained["summary"]["discount_breakdown"]["delivery_discount"], -79.0)
        current = omitted
        for changed_slot, changed_breakdown, changed_current in [
                ({"price_kind": "exact", "price_ore": 1}, None, {}),
                ({"price_kind": "from", "price_ore": 0}, None, {}),
                ({}, None, {}),
                (None, {"product_discount": -1.0, "delivery_discount": -78.0}, {}),
                (None, None, {"delivery_price": 7900}),
                (None, None, {"discounts": -7900}),
                (None, None, {"delivery_price": 0, "discounts": 0}),
                (None, None, {"bags": 1171}),
                (None, None, {"provider_total": 22741})]:
            with self.subTest(slot=changed_slot, original_discount=changed_breakdown, retry=changed_current):
                bad = deepcopy(pending)
                if changed_slot is not None:
                    bad["summary"]["delivery"]["slot"] = changed_slot
                if changed_breakdown is not None:
                    bad["browser_review"]["discount_breakdown"] = changed_breakdown
                with self.app.store.locked() as state:
                    state["pending_checkout"] = bad
                saved_current = deepcopy(current)
                current.update(changed_current)
                with self.assertRaises(HouseholdError):
                    self.prepare()
                self.assertEqual(self.app.store.read()["pending_checkout"], bad)
                current = saved_current
        self.assertEqual(self.browser.clicks, 0)

    def test_oda_addition_challenge_reports_saved_card_with_global_vipps_preference(self):
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending["order_change"] = {"order_id": "order-1", "status": "editing",
                "before": {"order": deepcopy(self.merchant.order)},
                "binding": {"account_reference_digest": "a" * 64, "receipt_address": "Example street 1"}}
            pending["cart"] = {"items": deepcopy(CART["items"]), "totalGrossAmount": 16.70}
            pending["summary"] = {**cart_summary(pending["cart"]), "payment": "•••• 1234"}
            pending["authentication_context"] = {"tab_id": "owned", "payment_id": "123456"}
        before = self.app.store.read()["pending_checkout"]
        self.browser.checkout_payment_authentication = lambda *a, **kw: {"active": True, "challenge": True}
        result = self.prepare()
        self.assertTrue(result["authentication_required"])
        self.assertEqual(result["payment_method"], "saved_card")
        self.assertEqual(result["summary"], before["summary"])
        self.assertEqual(result["confirmation_id"], "original")
        self.assertFalse(result["recovery_preparation_available"])
        self.assertEqual(self.app.store.read()["pending_checkout"], before)
        self.assertEqual(self.app.store.read()["checkout_payment"]["method"], "vipps")
        self.assertEqual(self.merchant.calls, [])
        self.assertEqual(self.browser.clicks, 0)

    def test_lost_response_restart_and_expiry_never_repeat_dispatch(self):
        prepared = self.prepare()
        self.browser.lost_response = True
        with self.assertRaisesRegex(HouseholdError, "response lost"):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertEqual(self.app.store.read()["pending_checkout"]["recovery"]["status"], "clicking")
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        self.now += timedelta(days=1)
        self.app._now = lambda: self.now
        for key in ("original", prepared["confirmation_id"]):
            self.assertFalse(self.call("confirm", confirmation_id=key)["confirmed"])
        self.assertFalse(self.prepare()["confirmed"])
        self.assertEqual(self.browser.clicks, 1)
        self.merchant.status = "paid_and_modifiable"
        self.assertTrue(self.call("reconcile", confirmation_id=prepared["confirmation_id"])["confirmed"])
        self.assertTrue(self.call("confirm", confirmation_id="original")["confirmed"])
        self.assertEqual(self.browser.clicks, 1)

    def test_expired_review_and_preclick_drift_preserve_original(self):
        prepared = self.prepare()
        self.now += timedelta(minutes=21)
        with self.assertRaisesRegex(HouseholdError, "expired"):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])
        again = self.prepare()
        self.assertNotEqual(again["confirmation_id"], prepared["confirmation_id"])
        self.browser.precondition_failure = True
        with self.assertRaises(CheckoutPreconditionError):
            self.call("confirm", confirmation_id=again["confirmation_id"])
        self.assertEqual(self.app.store.read()["pending_checkout"], self.original)
        self.assertEqual(self.browser.clicks, 0)

    def test_wrong_goods_amount_delivery_or_cancelled_order_never_prepare(self):
        changes = [lambda: self.merchant.order.update(grossAmount=247),
                   lambda: self.merchant.order["products"][0].update(quantity=2),
                   lambda: self.merchant.order.update(deliverySlotDisplay="Lør 12. september 09:00 - 13:00"),
                   lambda: self.merchant.order.update(deliveryAddress="Another address"),
                   lambda: setattr(self.merchant, "status", "cancelled")]
        original_order = deepcopy(self.merchant.order)
        for change in changes:
            with self.subTest(change=change):
                self.merchant.order = deepcopy(original_order)
                self.merchant.status = "unpaid_order"
                change()
                with self.assertRaises(HouseholdError):
                    self.prepare()
                self.assertEqual(self.app.store.read()["pending_checkout"], self.original)
        self.assertEqual(self.browser.clicks, 0)

    def test_changed_fee_split_or_payment_requires_another_review(self):
        for mutation in (lambda r: r.update(payment_display="•••• 1234"),
                         lambda r: r["amounts_minor"].update(bags=1200)):
            self.browser.review_change = mutation
            with self.assertRaises(HouseholdError):
                self.prepare()
        self.assertEqual(self.browser.clicks, 0)

    def test_paid_race_before_click_preserves_original_without_payment(self):
        prepared = self.prepare()
        self.merchant.status = "paid_and_modifiable"
        with self.assertRaisesRegex(HouseholdError, "no longer unpaid"):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertEqual(self.browser.clicks, 0)
        self.assertTrue(self.call("reconcile", confirmation_id="original")["confirmed"])


    def test_explicit_saved_card_recovery_preserves_original_and_global_choice(self):
        prepared = self.call("prepare", recovery=True, checkout_payment={"method": "saved_card"})
        self.assertEqual(prepared["summary"]["payment_method"], "saved_card")
        pending = self.app.store.read()["pending_checkout"]
        self.assertEqual(pending["recovery"]["browser_review"]["payment_choice"], {"method": "saved_card", "card_last4": None})
        self.assertEqual({k: v for k, v in pending.items() if k != "recovery"}, self.original)
        self.assertEqual(self.app.store.read()["checkout_payment"], self.original["checkout_payment"])
        with self.app.store.locked() as state:
            state["profile"]["diet"]["allergies_or_sensitivities"] = ["mustard"]
        revised = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(revised["reprepared"])
        self.assertEqual(revised["summary"]["payment"], "•••• 1234")
        self.assertEqual(revised["summary"]["payment_method"], "saved_card")
        findings = revised["summary"]["dietary_assessment"]["findings"]
        self.assertTrue(findings)
        held = self.call("confirm", confirmation_id=revised["confirmation_id"])
        self.assertTrue(held["dietary_review_required"])
        self.assertEqual(held["summary"]["payment_method"], "saved_card")
        self.assertEqual(held["summary"]["payment"], "•••• 1234")
        result = self.call("confirm", confirmation_id=revised["confirmation_id"],
                           dietary_review=[row["finding_id"] for row in findings])
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.browser.clicks, 1)
        self.assertEqual(self.app.store.read()["checkout_payment"], self.original["checkout_payment"])
        self.assertTrue(self.call("confirm", confirmation_id="original")["confirmed"])
        self.assertEqual(self.browser.clicks, 1)

    def test_recovery_standing_notice_uses_effective_payment_summary(self):
        self.app.confirmation_policy = "standing"
        with self.app.store.locked() as state:
            state["profile"]["diet"]["allergies_or_sensitivities"] = ["mustard"]
        prepared = self.call("prepare", recovery=True, checkout_payment={"method": "saved_card"})
        findings = prepared["summary"]["dietary_assessment"]["findings"]
        with self.app.store.locked() as state:
            state["profile"]["diet"]["uncertainty_permissions"] = [
                {**{key: finding[key] for key in ("kind", "term", "product_ref", "condition")},
                 "accepted": True, "notify": True} for finding in findings]
        prepared = self.call("prepare", recovery=True, checkout_payment={"method": "saved_card"})
        held = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(held["notification_required"])
        summary = held["notice"]["payload"]["summary"]
        self.assertEqual(summary["payment_method"], "saved_card")
        self.assertEqual(summary["payment"], "•••• 1234")
        self.assertEqual(self.browser.clicks, 0)
        self.assertEqual(self.app.store.read()["pending_checkout"]["summary"]["payment_method"], "vipps")

    def test_waiting_second_confirm_reloads_dispatch_state_before_browser_access(self):
        from contextlib import contextmanager
        prepared = self.prepare()
        original_lock = self.app._browser_operation
        @contextmanager
        def another_confirm_won(deadline):
            with original_lock(deadline):
                with self.app.store.locked() as state:
                    state["pending_checkout"]["recovery"]["status"] = "clicking"
                yield
        self.app._browser_operation = another_confirm_won
        result = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["awaiting_user_payment"])
        self.assertEqual(result["payment_request_state"], "unknown")
        self.assertNotIn("recovery_preparation_available", result)
        self.assertEqual(self.browser.clicks, 0)
        self.assertEqual(self.app.store.read()["pending_checkout"]["recovery"]["status"], "clicking")

    def test_original_required_result_notice_survives_both_recovery_outcomes(self):
        for dispatch_recovery in (False, True):
            with self.subTest(dispatch_recovery=dispatch_recovery):
                with self.app.store.locked() as state:
                    state["pending_checkout"] = deepcopy(self.original)
                    state["protected_results"] = {}
                    state["checkout_notices"] = {"original-before": {
                        "confirmation_id": "original", "phase": "before_dispatch", "status": "sent",
                        "delivered": True, "notice_token": "original-before", "payload": {"findings": []}}}
                self.merchant.status = "unpaid_order"
                prepared = self.prepare()
                if dispatch_recovery:
                    result = self.call("confirm", confirmation_id=prepared["confirmation_id"])
                else:
                    self.merchant.status = "paid_and_modifiable"
                    result = self.call("reconcile", confirmation_id="original")
                self.assertTrue(result["confirmed"])
                self.assertIn("notice", result)
                self.assertTrue(result["notice"]["dispatch"])
                replay = self.call("confirm", confirmation_id=prepared["confirmation_id"])
                self.assertEqual(replay["notice"]["notice_token"], result["notice"]["notice_token"])
                self.assertFalse(replay["notice"]["dispatch"])


class MathemAdditionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.merchant = Merchant()
        self.merchant.provider = "mathem"
        self.merchant.status = "unpaid_order_change"
        self.merchant.order = {"orderNumber": "order-1", "currency": "SEK", "grossAmount": 124.50,
            "products": [{"product_id": "4904", "quantity": 1}], "deliveryDate": "2026-09-13",
            "deliverySlotDisplay": "Sön 13. sep 14:00 - 16:00"}
        self.before = deepcopy(self.merchant.order)
        self.browser = MerchantBrowser(self.merchant)
        self.browser.order_followup = lambda *a, **kw: {}
        self.amounts = {key: None for key in AMOUNTS}
        self.amounts.update(product_subtotal=18.50, provider_total=18.50)
        self.settings = {"provider": "mathem", "household": "Synthetic", "checkout_payment": {"method": "saved_card"}}
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        self.now = datetime(2026, 9, 10, 7, tzinfo=timezone.utc)
        self.app._now = lambda: self.now
        self.binding = {"account_reference_digest": "a" * 64, "receipt_address": "Example street 1"}
        cart = {"items": [{"product_id": "4904", "name": "Wholegrain pasta", "quantity": 1, "price": 18.50}],
                "count": 1, "totalGrossAmount": 18.50, "deliverySlot": {"name": "Sön 13. sep 14:00 - 16:00"},
                "deliveryAddress": "Example street 1"}
        change = {"order_id": "order-1", "binding": self.binding,
                  "before": {"order": deepcopy(self.before), "tracking": {"status": "paid_and_modifiable"}}}
        with self.app.store.locked() as state:
            state["order_change"] = deepcopy(change)
            state["pending_checkout"] = {"confirmation_id": "original", "status": "uncertain",
                "expires_at": "2026-09-09T22:00:00+00:00", "cart": cart, "summary": cart_summary(cart),
                "orders_before": {"orders": [deepcopy(self.before)]}, "order_change": change,
                "browser_review": {"binding": self.binding, "payment_display": "•••• 1234", "amounts": self.amounts},
                "checkout_payment": deepcopy(state["checkout_payment"]),
                "payment_failure": {"payment_failed": True, "order_id": "order-1", "order_change_id": "change-1"}}
        self.original = self.app.store.read()["pending_checkout"]
        self.review_change = None
        self.options_seen = []
        def review(cart, order_id, *, payment, expected_binding, addition, **kwargs):
            self.options_seen.append(deepcopy(addition))
            assert order_id == "order-1" and expected_binding == self.binding
            assert addition["order_change_id"] == "change-1" and addition["before"]["order"] == self.before
            amounts = _oda_checkout_amounts_minor(self.amounts, provider="mathem")
            amounts.update(provider_total=1851, discount_breakdown={"product_discount": None, "delivery_discount": None})
            result = {"order_id": order_id, "binding": deepcopy(expected_binding), "payment_choice": deepcopy(payment),
                      "payment_display": "•••• 1234", "amounts_minor": amounts}
            if self.review_change:
                self.review_change(result)
            return result
        self.browser.review_payment_recovery = review
        def submit(cart, review, before_click, *, addition, **kwargs):
            assert addition["order_change_id"] == "change-1"
            before_click()
            self.browser.clicks += 1
            if self.browser.lost_response:
                raise HouseholdError("lost response after recovery click")
            self.accept()
        self.browser.submit_payment_recovery = submit

    def accept(self):
        self.merchant.status = "paid_and_modifiable"
        self.merchant.order.update(grossAmount=143.00, products=[{"product_id": "4904", "quantity": 2}])

    def call(self, action, **kwargs):
        return self.app.handle({"operation": "checkout", "action": action, **kwargs})

    def prepare(self):
        return self.call("prepare", recovery=True)

    def test_delayed_previous_result_notice_preserves_current_payment_page(self):
        self.merchant.status = "paid_and_modifiable"
        self.app._checkout_notice("previous", "before_dispatch", {"findings": []})
        self.browser.order_followup = lambda *a, **kw: self.fail("must not navigate away from the pending payment")
        result = self.app._checkout_result_notice({"confirmed": True, "confirmation_id": "previous",
                                                  "order_id": "order-1", "tracking_status": "paid_and_modifiable"})
        self.assertEqual(result["notice"]["phase"], "after_reconciliation")
        self.assertEqual(self.app.store.read()["pending_checkout"], self.original)

    def test_late_original_addition_failure_preserves_history_then_recovers_once(self):
        from unittest import mock
        context = {"tab_id": "owned", "payment_id": "123456"}
        failure = deepcopy(self.original["payment_failure"])
        with self.app.store.locked() as state:
            state["pending_checkout"].pop("payment_failure")
            state["pending_checkout"].update(authentication_context=context, bank_app_choice_attempted=True)
        pending = self.app.store.read()["pending_checkout"]
        self.browser.checkout_payment_authentication = mock.Mock(return_value=None)
        self.browser.checkout_payment_failure = mock.Mock(return_value=failure)
        result = self.call("reconcile", confirmation_id="original")
        self.assertTrue(result["payment_failed"])
        self.assertTrue(result["recovery_preparation_available"])
        self.browser.checkout_payment_failure.assert_called_once_with(context, deadline=mock.ANY, expected_order_id="order-1")
        self.assertEqual(self.app.store.read()["pending_checkout"], {**pending, "payment_failure": failure})
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        self.app._now = lambda: self.now
        self.browser.checkout_payment_failure.reset_mock()
        prepared = self.prepare()
        self.assertEqual(prepared["summary"]["items"], pending["summary"]["items"])
        self.assertEqual(prepared["summary"]["total"], 18.50)
        result = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["changed_existing_order"])
        self.assertTrue(self.call("reconcile", confirmation_id="original")["confirmed"])
        self.assertEqual(self.browser.clicks, 1)
        self.browser.checkout_payment_failure.assert_not_called()

    def test_late_addition_failure_rejects_target_base_and_concurrent_changes(self):
        from unittest import mock
        failure = deepcopy(self.original["payment_failure"])
        with self.app.store.locked() as state:
            state["pending_checkout"].pop("payment_failure")
            state["pending_checkout"]["authentication_context"] = {"tab_id": "owned", "payment_id": "123456"}
        pending = self.app.store.read()["pending_checkout"]
        self.browser.checkout_payment_authentication = mock.Mock(return_value=None)
        self.browser.checkout_payment_failure = mock.Mock(return_value=failure)
        for result in ({**failure, "order_id": "another"}, {"payment_failed": True, "order_id": "order-1"}):
            self.browser.checkout_payment_failure.return_value = result
            with self.assertRaisesRegex(HouseholdError, "must be bound"):
                self.call("reconcile", confirmation_id="original")
            self.assertEqual(self.app.store.read()["pending_checkout"], pending)
        self.browser.checkout_payment_failure.return_value = failure
        for drift in ({"grossAmount": 125.00}, {"products": [{"product_id": "4904", "quantity": 2}]},
                      {"deliverySlotDisplay": "Sön 13. sep 16:00 - 18:00"}, {"currency": "NOK"}):
            self.merchant.order = {**deepcopy(self.before), **drift}
            with self.assertRaisesRegex(HouseholdError, "paid order changed"):
                self.call("reconcile", confirmation_id="original")
            self.assertEqual(self.app.store.read()["pending_checkout"], pending)
        self.merchant.order = deepcopy(self.before)
        self.merchant.status = "paid_and_modifiable"
        with self.assertRaisesRegex(HouseholdError, "no longer unpaid"):
            self.call("reconcile", confirmation_id="original")
        self.merchant.status = "unpaid_order_change"
        def concurrent(*args, **kwargs):
            with self.app.store.locked() as state:
                state["pending_checkout"]["authentication_context"]["payment_id"] = "987654"
            return failure
        self.browser.checkout_payment_failure.side_effect = concurrent
        with self.assertRaisesRegex(HouseholdError, "changed while resolving"):
            self.call("reconcile", confirmation_id="original")
        self.assertNotIn("payment_failure", self.app.store.read()["pending_checkout"])
        self.assertEqual(self.browser.clicks, 0)

    def test_late_addition_resolution_preserves_active_auth_and_excludes_delivery(self):
        from unittest import mock
        context = {"tab_id": "owned", "payment_id": "123456"}
        pending = {**self.original, "authentication_context": context}
        pending.pop("payment_failure")
        self.browser.checkout_payment_authentication = mock.Mock(return_value={"active": True, "challenge": True})
        self.browser.checkout_payment_failure = mock.Mock(side_effect=AssertionError("must not resolve this attempt"))
        self.assertTrue(self.app._checkout_authentication_wait(pending, None)["authentication_required"])
        self.browser.checkout_payment_authentication.return_value = None
        delivery = {**pending, "order_change": {**pending["order_change"], "requested_delivery": {"display": "unchanged"}}}
        self.assertFalse(self.app._checkout_authentication_wait(delivery, None)["recovery_preparation_available"])
        self.browser.checkout_payment_failure.assert_not_called()

    def challenged_recovery(self):
        from unittest import mock
        context = {"tab_id": "recovery-tab", "payment_id": "234567"}
        self.browser.checkout_payment_authentication = mock.Mock(return_value={"active": True, "challenge": True})
        self.browser.checkout_payment_failure = mock.Mock(return_value=None)
        def submit(cart, review, before_click, **kwargs):
            before_click()
            self.browser.clicks += 1
            return {"authentication_context": context}
        self.browser.submit_payment_recovery = submit
        prepared = self.prepare()
        self.assertTrue(self.call("confirm", confirmation_id=prepared["confirmation_id"])["authentication_required"])
        return prepared["confirmation_id"], context

    def test_failed_recovery_child_is_archived_before_fresh_review_and_old_ids_stay_failed(self):
        from unittest import mock
        cid, context = self.challenged_recovery()
        pending = self.app.store.read()["pending_checkout"]
        self.assertFalse(self.prepare()["recovery_preparation_available"])
        self.browser.checkout_payment_failure.assert_not_called()
        self.browser.checkout_payment_authentication.return_value = None
        failure = deepcopy(self.original["payment_failure"])
        self.browser.checkout_payment_failure.return_value = failure
        result = self.call("reconcile", confirmation_id=cid)
        self.assertTrue(result["payment_failed"])
        self.assertTrue(result["recovery_preparation_available"])
        self.browser.checkout_payment_failure.assert_called_once_with(context, deadline=mock.ANY, expected_order_id="order-1")
        failed = deepcopy(pending["recovery"])
        failed["payment_failure"] = failure
        self.assertEqual(self.app.store.read()["pending_checkout"], {**pending, "recovery": failed})
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        self.app._now = lambda: self.now
        fresh = self.prepare()
        self.assertNotEqual(cid, fresh["confirmation_id"])
        state = self.app.store.read()
        archived = deepcopy(state["protected_results"][cid])
        self.assertEqual(archived["failed_attempt"], failed)
        self.assertEqual({k: v for k, v in state["pending_checkout"].items() if k != "recovery"}, self.original)
        self.assertNotIn("payment_failure", state["pending_checkout"]["recovery"])
        self.assertEqual(self.browser.clicks, 1)
        self.browser.choose_checkout_bank_app = mock.Mock(side_effect=AssertionError("must not choose for an old confirmation"))
        for action in ("confirm", "reconcile", "authenticate"):
            result = self.call(action, confirmation_id=cid)
            self.assertFalse(result["confirmed"])
            self.assertTrue(result["payment_failed"])
            self.assertFalse(result["retry_allowed"])
            self.assertNotIn("failed_attempt", result)
        def lost_reply(cart, review, before_click, **kwargs):
            before_click()
            self.browser.clicks += 1
            raise HouseholdError("lost reply after fresh recovery dispatch")
        self.browser.submit_payment_recovery = lost_reply
        with self.assertRaisesRegex(HouseholdError, "lost reply"):
            self.call("confirm", confirmation_id=fresh["confirmation_id"])
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        self.app._now = lambda: self.now
        self.assertFalse(self.prepare()["recovery_preparation_available"])
        self.assertFalse(self.call("confirm", confirmation_id=fresh["confirmation_id"])["confirmed"])
        self.assertFalse(self.call("confirm", confirmation_id="original")["confirmed"])
        self.assertEqual(self.browser.clicks, 2)
        self.accept()
        self.assertTrue(self.call("reconcile", confirmation_id=fresh["confirmation_id"])["confirmed"])
        self.assertTrue(self.call("reconcile", confirmation_id="original")["confirmed"])
        self.assertFalse(self.call("reconcile", confirmation_id=cid)["confirmed"])
        self.assertEqual(self.app.store.read()["protected_results"][cid], archived)
        self.assertEqual(self.browser.clicks, 2)

    def test_recovery_child_failure_rejects_wrong_pair_changed_base_and_concurrent_state(self):
        cid, _ = self.challenged_recovery()
        pending = self.app.store.read()["pending_checkout"]
        self.browser.checkout_payment_authentication.return_value = None
        failure = deepcopy(self.original["payment_failure"])
        for change in ({"order_id": "other"}, {"order_change_id": "other"}, {"payment_failed": False}):
            self.browser.checkout_payment_failure.return_value = {**failure, **change}
            with self.assertRaisesRegex(HouseholdError, "another merchant change"):
                self.call("reconcile", confirmation_id=cid)
            self.assertEqual(self.app.store.read()["pending_checkout"], pending)
        self.browser.checkout_payment_failure.return_value = failure
        for change in ({"grossAmount": 125.00}, {"currency": "NOK"},
                       {"products": [{"product_id": "4904", "quantity": 2}]},
                       {"deliverySlotDisplay": "Sön 13. sep 16:00 - 18:00"}):
            self.merchant.order = {**deepcopy(self.before), **change}
            with self.assertRaisesRegex(HouseholdError, "paid order changed"):
                self.call("reconcile", confirmation_id=cid)
            self.assertEqual(self.app.store.read()["pending_checkout"], pending)
        self.merchant.order = deepcopy(self.before)
        def concurrent(*args, **kwargs):
            with self.app.store.locked() as state:
                state["pending_checkout"]["recovery"]["authentication_context"]["payment_id"] = "345678"
            return failure
        self.browser.checkout_payment_failure.side_effect = concurrent
        with self.assertRaisesRegex(HouseholdError, "changed while resolving"):
            self.call("reconcile", confirmation_id=cid)
        self.assertNotIn("payment_failure", self.app.store.read()["pending_checkout"]["recovery"])
        self.assertEqual(self.app.store.read()["protected_results"], {})
        self.assertEqual(self.browser.clicks, 1)

    def test_replacement_recovery_requires_new_notice_and_failed_review_keeps_old_child(self):
        cid, _ = self.challenged_recovery()
        self.browser.checkout_payment_authentication.return_value = None
        self.browser.checkout_payment_failure.return_value = deepcopy(self.original["payment_failure"])
        self.call("reconcile", confirmation_id=cid)
        failed = self.app.store.read()["pending_checkout"]
        self.review_change = lambda review: review["amounts_minor"].update(bags=100)
        with self.assertRaisesRegex(HouseholdError, "fees differ"):
            self.prepare()
        self.assertEqual(self.app.store.read()["pending_checkout"], failed)
        self.assertEqual(self.app.store.read()["protected_results"], {})
        self.review_change = None
        self.app.confirmation_policy = "standing"
        with self.app.store.locked() as state:
            state["profile"]["diet"]["allergies_or_sensitivities"] = ["mustard"]
        findings = self.app._checkout_dietary(failed["summary"])["findings"]
        with self.app.store.locked() as state:
            state["profile"]["diet"]["uncertainty_permissions"] = [
                {**{k: f[k] for k in ("kind", "term", "product_ref", "condition")}, "accepted": True, "notify": True}
                for f in findings]
        prior = self.app._checkout_notice(cid, "before_dispatch", {"findings": findings})
        self.call("notice_result", notice_token=prior["notice_token"], send_outcome="sent", sender_receipt="earlier synthetic notice")
        fresh = self.prepare()
        held = self.call("confirm", confirmation_id=fresh["confirmation_id"])
        self.assertTrue(held["notification_required"])
        self.assertNotEqual(prior["notice_token"], held["notice"]["notice_token"])
        self.assertEqual(held["notice"]["confirmation_id"], fresh["confirmation_id"])
        self.assertEqual(held["notice"]["payload"]["summary"]["total"], 18.50)
        self.assertEqual(held["notice"]["payload"]["summary"]["merchant_summary_total"], 18.51)
        self.assertEqual(self.browser.clicks, 1)

    def test_same_addition_review_preserves_goods_and_separate_overview_total(self):
        prepared = self.prepare()
        self.assertEqual(prepared["summary"]["total"], 18.50)
        self.assertEqual(prepared["summary"]["merchant_summary_total"], 18.51)
        self.assertEqual(prepared["summary"]["items"], self.original["summary"]["items"])
        self.assertEqual(self.browser.clicks, 0)
        self.assertFalse(self.call("confirm", confirmation_id="original")["confirmed"])
        result = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["changed_existing_order"])
        self.assertEqual(result["original_confirmation_id"], "original")
        self.assertEqual(self.browser.clicks, 1)
        for cid in ("original", prepared["confirmation_id"]):
            self.assertTrue(self.call("confirm", confirmation_id=cid)["idempotent"])
            self.assertTrue(self.call("reconcile", confirmation_id=cid)["confirmed"])
        self.assertIsNone(self.app.store.read()["pending_checkout"])
        self.assertIsNone(self.app.store.read()["order_change"])
        self.assertNotIn("get_cart", self.merchant.calls)

    def test_lost_response_restart_and_repeated_failure_never_pay_twice(self):
        prepared = self.prepare()
        self.browser.lost_response = True
        with self.assertRaises(HouseholdError):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.app = Application(StateStore(self.temp.name, self.settings), self.merchant, self.browser)
        self.now += timedelta(days=1)
        self.app._now = lambda: self.now
        for cid in ("original", prepared["confirmation_id"]):
            result = self.call("confirm", confirmation_id=cid)
            self.assertTrue(result["recovery_payment_unconfirmed"])
            self.assertFalse(result.get("recovery_preparation_available"))
        self.assertFalse(self.prepare()["confirmed"])
        self.assertEqual(self.browser.clicks, 1)
        self.accept()
        self.assertTrue(self.call("reconcile", confirmation_id=prepared["confirmation_id"])["confirmed"])
        self.assertTrue(self.call("confirm", confirmation_id="original")["confirmed"])
        self.assertEqual(self.browser.clicks, 1)

    def test_missing_original_capture_or_changed_base_cannot_prepare(self):
        with self.app.store.locked() as state:
            state["pending_checkout"].pop("payment_failure")
        with self.assertRaisesRegex(HouseholdError, "must be bound"):
            self.prepare()
        with self.app.store.locked() as state:
            state["pending_checkout"] = deepcopy(self.original)
        for change in ({"grossAmount": 125.00}, {"products": [{"product_id": "4904", "quantity": 2}]},
                       {"deliverySlotDisplay": "Sön 13. sep 16:00 - 18:00"}, {"currency": "NOK"}):
            self.merchant.order = {**deepcopy(self.before), **change}
            with self.assertRaisesRegex(HouseholdError, "paid order changed"):
                self.prepare()
        self.assertEqual(self.browser.clicks, 0)
        self.assertEqual(self.app.store.read()["pending_checkout"], self.original)

    def test_paid_owner_race_is_reconciled_without_recovery_dispatch(self):
        prepared = self.prepare()
        self.accept()
        with self.assertRaisesRegex(HouseholdError, "no longer unpaid"):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])
        result = self.call("reconcile", confirmation_id="original")
        self.assertTrue(result["confirmed"])
        self.assertNotIn("original_confirmation_id", result)
        self.assertTrue(self.call("reconcile", confirmation_id=prepared["confirmation_id"])["confirmed"])
        self.assertEqual(self.browser.clicks, 0)

    def test_recovery_notice_discloses_payable_and_overview_and_records_result_once(self):
        self.app.confirmation_policy = "standing"
        with self.app.store.locked() as state:
            state["profile"]["diet"]["allergies_or_sensitivities"] = ["mustard"]
        prepared = self.prepare()
        with self.app.store.locked() as state:
            state["profile"]["diet"]["uncertainty_permissions"] = [
                {**{key: f[key] for key in ("kind", "term", "product_ref", "condition")}, "accepted": True, "notify": True}
                for f in prepared["summary"]["dietary_assessment"]["findings"]]
        prepared = self.prepare()
        held = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(held["notification_required"])
        message = held["notice"]["payload"]["message"]
        self.assertIn("Payment due now: 18.50 SEK", message)
        self.assertIn("overview separately shows 18.51 SEK", message)
        self.call("notice_result", notice_token=held["notice"]["notice_token"], send_outcome="sent", sender_receipt="synthetic sender receipt")
        result = self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["notice"]["dispatch"])
        replay = self.call("confirm", confirmation_id="original")
        self.assertEqual(result["notice"]["notice_token"], replay["notice"]["notice_token"])
        self.assertFalse(replay["notice"]["dispatch"])
        self.assertEqual(self.browser.clicks, 1)

    def test_changed_payment_or_added_fee_never_prepares(self):
        for change in (lambda r: r.update(payment_display="•••• 4321"),
                       lambda r: r["amounts_minor"].update(bags=100)):
            self.review_change = change
            with self.assertRaises(HouseholdError):
                self.prepare()
        self.assertEqual(self.browser.clicks, 0)


class RetryAmountTests(unittest.TestCase):
    def test_persisted_recovery_review_clicks_once_and_blocks_actual_drift(self):
        import json
        import shutil
        import subprocess
        from contextlib import nullcontext
        from test_payment_setup import PAYMENT_DOM
        from oda_browser import OdaBrowser, _oda_checkout_surface_script, _oda_checkout_amount_script
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the actual browser script")
        expected = {"delivery_address": "Eksempelveien 1", "total_minor": 4550, "product_count": 1}
        url = "https://oda.com/no/checkout/retry/?orderNumber=order-1"
        rows = [["1 vare", "26,50 kr"], ["Levering", "19,00 kr"], ["Total inkl. MVA", "45,50 kr"]]
        for method, selected in [("vipps", 0), ("saved_card", 2)]:
            payment = {"method": method}
            def evaluate(script, **change):
                c = {"url": url, "rows": rows, "selected": selected, **change}
                result = subprocess.run([node, "-e", PAYMENT_DOM], input=json.dumps({"script": script, "c": c}), text=True, capture_output=True, check=True)
                return json.loads(result.stdout)
            surface = evaluate(_oda_checkout_surface_script(expected, payment))["result"]
            amounts = evaluate(_oda_checkout_amount_script(4550, expected_product_count=1, retry=True))["result"]["amounts"]
            # StateStore persists sorted object keys, including nested review fields.
            review = json.loads(json.dumps({"order_id": "order-1", "binding": {}, "payment_choice": payment,
                "surface": surface, "amounts_minor": amounts}, sort_keys=True))
            for change in [{}, {"selected": 2 if selected == 0 else 0}, {"quantity": 2},
                           {"payDisabled": True}, {"url": url.replace("order-1", "order-2")},
                           {"rows": [["1 vare", "25,50 kr"], ["Levering", "20,00 kr"], ["Total inkl. MVA", "45,50 kr"]]}]:
                with self.subTest(method=method, change=change):
                    browser = OdaBrowser.__new__(OdaBrowser)
                    browser.vipps_phone_number = "90000000"
                    browser._checkout_dispatch_tab = lambda: None
                    browser.checkout_provider = "oda"
                    browser._checkout_deadline = None
                    browser._checkout_operation = lambda *a, **k: nullcontext()
                    browser._cart_expectation = lambda cart: expected
                    browser.review_payment_recovery = lambda *a, **k: deepcopy(review)
                    browser._invoke = lambda *a, **k: {}
                    browser._capture_checkout_payment = lambda *a, **k: None
                    observed, callbacks = [], []
                    def final_eval(script):
                        self.assertEqual(callbacks, [True])
                        result = evaluate(script, **change)
                        observed.append(result)
                        return result["result"]
                    browser._eval = final_eval
                    def submit():
                        browser.submit_payment_recovery({}, review, lambda: callbacks.append(True))
                    if change:
                        with self.assertRaises(CheckoutPreconditionError):
                            submit()
                    else:
                        submit()
                    self.assertEqual(observed[0]["clicks"], [] if change else ["PAY"])

    def test_observed_retry_without_delsum_and_atomic_amount_binding(self):
        import json
        import shutil
        import subprocess
        from test_payment_setup import PAYMENT_DOM
        from oda_browser import _oda_checkout_amount_script
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the actual browser script")
        rows = [["1 vare", "26,50 kr"], ["Levering", "19,00 kr"], ["Total inkl. MVA", "45,50 kr"]]
        def execute(script, **config):
            result = subprocess.run([node, "-e", PAYMENT_DOM], input=json.dumps({"script": script, "c": {"rows": rows, **config}}), text=True, capture_output=True, check=True)
            return json.loads(result.stdout)
        normal = execute(_oda_checkout_amount_script(4550, expected_product_count=1))
        self.assertFalse(normal["result"]["amounts_valid"])
        script = _oda_checkout_amount_script(4550, expected_product_count=1, retry=True)
        review = execute(script)
        self.assertTrue(review["result"]["amounts_valid"])
        url = "https://oda.com/no/checkout/retry/?orderNumber=order-1"
        click = _oda_checkout_amount_script(4550, expected_product_count=1, retry=True, vipps=True,
                    expected_amounts=review["result"]["amounts"], expected_url=url)
        self.assertEqual(execute(click, url=url)["clicks"], ["PAY"])
        self.assertEqual(execute(click, url=url.replace("order-1", "order-2"))["clicks"], [])
        self.assertEqual(execute(click, url=url, button="Betal med 46,50 kr")["clicks"], [])


class AuthenticationBrowserTests(unittest.TestCase):
    def test_late_addition_native_failure_requires_exact_original_pair_and_numeric_change(self):
        import json
        import shutil
        import subprocess
        from unittest import mock
        from oda_browser import MathemBrowser
        node = shutil.which("node")
        if not node:
            self.skipTest("Node executes the native payment-resolution script")
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._checkout_deadline = None
        browser._invoke = mock.Mock(return_value={"tabs": [{"tabId": "owned", "active": True}]})
        context = {"tab_id": "owned", "payment_id": "123456"}
        url = "https://www.mathem.se/se/checkout/retry/?orderNumber=order-1&orderChangeId=7654321"
        browser._checkout_payment_observation = mock.Mock(return_value={"url": url, "failed": True})
        native = {"type": "checkout-payment-retry", "params": {"order_number": "order-1", "order_change_id": 7654321}}
        harness = r"""
const {script,c}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.location={href:c.url};const calls=[];
global.fetch=async(url,options)=>{calls.push({url,...options});return {ok:c.ok,json:async()=>{if(c.changed)location.href+='&changed=1';return c.response}}};
Promise.resolve(eval(script)).then(result=>process.stdout.write(JSON.stringify({result:JSON.parse(result),calls})));
"""
        config = {"url": url, "ok": True, "response": native}
        calls = []
        def evaluate(script):
            run = subprocess.run([node, "-e", harness], input=json.dumps({"script": script, "c": config}),
                                 text=True, capture_output=True, check=True, timeout=10)
            result = json.loads(run.stdout)
            calls.extend(result["calls"])
            return result["result"]
        browser._eval = evaluate
        expected = {"payment_failed": True, "order_id": "order-1", "order_change_id": "7654321"}
        self.assertEqual(browser.checkout_payment_failure(context, expected_order_id="order-1"), expected)
        self.assertEqual(calls, [{"url": "/api/v1/payments/adyen/three-ds/123456/", "method": "GET",
                                  "credentials": "same-origin", "redirect": "error"}])
        for change in [{"ok": False}, {"changed": True},
                       {"response": {**native, "type": "payments-providers-adyen-three-ds"}},
                       *({"response": {**native, "params": params}} for params in [
                           {"order_number": "other", "order_change_id": 7654321},
                           {"order_number": ["order-1"], "order_change_id": 7654321},
                           {"order_number": "order-1"}, {**native["params"], "extra": True},
                           *({"order_number": "order-1", "order_change_id": value}
                             for value in [None, 7654322, "7654321", True, 7654321.5, 9007199254740992])])]:
            config = {"url": url, "ok": True, "response": native, **change}
            self.assertIsNone(browser.checkout_payment_failure(context, expected_order_id="order-1"))
        config = {"url": url, "ok": True, "response": native}
        browser._invoke.return_value = {"tabs": [{"tabId": "other", "active": True}]}
        self.assertIsNone(browser.checkout_payment_failure(context, expected_order_id="order-1"))
        browser._invoke.return_value = {"tabs": [{"tabId": "owned", "active": True}]}
        browser._eval = mock.Mock(side_effect=AssertionError("must not query an unrelated payment route"))
        for page in [{"url": url, "failed": False}, None,
                     *({"url": value, "failed": True} for value in [
                         url.replace("order-1", "other"), url + "&extra=1", url + "&orderChangeId=7654321",
                         url + "#other", url.replace("www.mathem.se", "example.com"),
                         url.replace("7654321", "change-1"), url.replace("&orderChangeId=7654321", "")])]:
            browser._checkout_payment_observation.return_value = page
            self.assertIsNone(browser.checkout_payment_failure(context, expected_order_id="order-1"))
        browser._eval.assert_not_called()

    def test_late_native_failure_requires_original_terminal_response_and_route(self):
        import json
        import shutil
        import subprocess
        from unittest import mock
        from oda_browser import MathemBrowser
        node = shutil.which("node")
        if not node:
            self.skipTest("Node executes the native payment-resolution script")
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._checkout_deadline = None
        browser._invoke = mock.Mock(return_value={"tabs": [{"tabId": "owned", "active": True}]})
        context = {"tab_id": "owned", "payment_id": "123456"}
        url = "https://www.mathem.se/se/checkout/retry/?orderNumber=order-1"
        browser._checkout_payment_observation = mock.Mock(return_value={"url": url, "failed": True})
        native = {"type": "checkout-payment-retry", "params": {"order_number": "order-1", "order_change_id": None}}
        harness = r"""
const {script,c}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.location={href:c.url};const calls=[];
global.fetch=async(url,options)=>{calls.push({url,...options});return {ok:c.ok,json:async()=>{if(c.changed)location.href+='&changed=1';return c.response}}};
Promise.resolve(eval(script)).then(result=>process.stdout.write(JSON.stringify({result:JSON.parse(result),calls})));
"""
        config = {"url": url, "ok": True, "response": native}
        calls = []
        def evaluate(script):
            run = subprocess.run([node, "-e", harness], input=json.dumps({"script": script, "c": config}),
                                 text=True, capture_output=True, check=True, timeout=10)
            result = json.loads(run.stdout)
            calls.extend(result["calls"])
            return result["result"]
        browser._eval = evaluate
        self.assertEqual(browser.checkout_payment_failure(context), {"payment_failed": True, "order_id": "order-1"})
        self.assertEqual(calls, [{"url": "/api/v1/payments/adyen/three-ds/123456/", "method": "GET",
                                  "credentials": "same-origin", "redirect": "error"}])
        for change in [{"ok": False}, {"changed": True},
                       {"response": {**native, "type": "payments-providers-adyen-three-ds"}},
                       *({"response": {**native, "params": params}} for params in [
                           {"order_number": "other", "order_change_id": None},
                           {"order_number": ["order-1"], "order_change_id": None},
                           {"order_number": "order-1", "order_change_id": "change-1"},
                           {"order_number": "order-1"},
                           {**native["params"], "extra": True}])]:
            config = {"url": url, "ok": True, "response": native, **change}
            self.assertIsNone(browser.checkout_payment_failure(context))
        browser._eval = mock.Mock(side_effect=AssertionError("must not query an unrelated payment route"))
        for page in [{"url": url, "failed": False}, {"url": url + "&orderChangeId=change-1", "failed": True},
                     {"url": url.replace("www.mathem.se", "example.com"), "failed": True}, None]:
            browser._checkout_payment_observation.return_value = page
            self.assertIsNone(browser.checkout_payment_failure(context))
        browser._eval.assert_not_called()

    def test_native_challenge_visibility_and_exact_retained_route_without_navigation(self):
        import json
        import shutil
        import subprocess
        from unittest import mock
        from oda_browser import OdaBrowser, MathemBrowser
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the native DOM observation")
        harness = r"""
const {script,c}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.location={href:c.url};
global.getComputedStyle=e=>({display:e.hidden?'none':'block',visibility:'visible',opacity:'1'});
const parent={hidden:c.hiddenAncestor};
const frame={parentElement:parent,getBoundingClientRect:()=>({width:c.zero?0:400,height:400})};
const container={parentElement:parent,getBoundingClientRect:()=>({width:400,height:400}),querySelectorAll:s=>s==='iframe[name="threeDSIframe"]'?(c.duplicate?[frame,frame]:[frame]):[]};
global.document={querySelectorAll:s=>s==='.adyen-checkout__threeds2__challenge'?(c.fingerprint?[]:[container]):[]};
process.stdout.write(eval(script));
"""
        for cls, base in [(OdaBrowser, "https://oda.com/no/"), (MathemBrowser, "https://www.mathem.se/se/")]:
            browser = cls.__new__(cls)
            browser._checkout_deadline = None
            browser._invoke = mock.Mock(return_value={"tabs": [{"tabId": "owned", "active": True}]})
            context = {"tab_id": "owned", "payment_id": "123456"}
            url = base + "checkout/threeDS/?paymentId=123456"
            config = {"url": url}
            def evaluate(script):
                result = subprocess.run([node, "-e", harness], input=json.dumps({"script": script, "c": config}),
                                        capture_output=True, text=True, check=True, timeout=10)
                return json.loads(result.stdout)
            browser._eval = evaluate
            for change in ({}, {"fingerprint": True}, {"hiddenAncestor": True}, {"zero": True}, {"duplicate": True}):
                config = {"url": url, **change}
                self.assertEqual(browser.checkout_payment_authentication(context), {"active": True, "challenge": not change})
            for other in [url.replace("123456", "98765"), url + "&other=1", url + "#x",
                          url.replace(base, "https://example.com/no/"), base + "checkout/confirm/"]:
                config = {"url": other}
                self.assertIsNone(browser.checkout_payment_authentication(context))
            browser._invoke.return_value = {"tabs": [{"tabId": "different", "active": True}]}
            browser._eval = mock.Mock(side_effect=AssertionError("must not inspect another tab"))
            self.assertIsNone(browser.checkout_payment_authentication(context))
            self.assertTrue(all(call.args == ("tab", "list") for call in browser._invoke.call_args_list))

    def test_lost_first_observation_stays_unresolved_without_inventing_payment_identity(self):
        from unittest import mock
        from oda_browser import MathemBrowser
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._invoke = mock.Mock(return_value={"tabs": [{"tabId": "owned", "active": True}]})
        browser._settle = mock.Mock()
        browser._eval = mock.Mock(side_effect=HouseholdError("lost first observation"))
        self.assertEqual(browser._capture_checkout_payment("owned"), {"authentication_unresolved": True})
        self.assertEqual(browser._capture_checkout_payment(None), {"authentication_unresolved": True})
        self.assertIsNone(browser._capture_checkout_payment(None, authentication_expected=False))
        browser._eval = mock.Mock(return_value={"url": "https://www.mathem.se/se/checkout/retry/?orderNumber=order-1", "failed": True})
        self.assertEqual(browser._capture_checkout_payment("owned"), {"payment_failed": True, "order_id": "order-1"})

    def test_recovery_capture_ignores_old_failure_until_new_payment_redirect(self):
        from unittest import mock
        from oda_browser import MathemBrowser
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._invoke = mock.Mock(return_value={"tabs": [{"tabId": "owned", "active": True}]})
        browser._settle = mock.Mock()
        old = {"url": "https://www.mathem.se/se/checkout/retry/?orderNumber=order-1", "failed": True}
        auth = {"url": "https://www.mathem.se/se/checkout/threeDS/?paymentId=123456", "challenge": True}
        browser._eval = mock.Mock(side_effect=[deepcopy(old)] * 2 + [deepcopy(auth)] * 58)
        self.assertEqual(browser._capture_checkout_payment("owned", capture_failure=False),
                         {"authentication_context": {"tab_id": "owned", "payment_id": "123456"}})
        browser._eval = mock.Mock(return_value=old)
        self.assertEqual(browser._capture_checkout_payment("owned", capture_failure=False),
                         {"authentication_unresolved": True})
        self.assertEqual(browser._eval.call_count, 60)

    def test_original_capture_retains_context_but_terminal_success_or_failure_resolves_it(self):
        from unittest import mock
        from oda_browser import MathemBrowser
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._invoke = mock.Mock(return_value={"tabs": [{"tabId": "owned", "active": True}]})
        browser._settle = mock.Mock()
        auth = {"url": "https://www.mathem.se/se/checkout/threeDS/?paymentId=123456", "challenge": True}
        context = {"tab_id": "owned", "payment_id": "123456"}
        for end, expected in [(HouseholdError("lost observation"), {"authentication_context": context}),
                              ({"url": "https://www.mathem.se/se/checkout/success/"}, None),
                              ({"url": "https://www.mathem.se/se/checkout/retry/?orderNumber=order-1&orderChangeId=change-1", "failed": True},
                               {"payment_failed": True, "order_id": "order-1", "order_change_id": "change-1"})]:
            browser._eval = mock.Mock(side_effect=[deepcopy(auth), end, end])
            self.assertEqual(browser._capture_checkout_payment("owned", order_id="order-1"), expected)


class MathemRetryBrowserTests(unittest.TestCase):
    def test_exact_dispatch_page_capture_and_missing_or_foreign_results(self):
        from unittest import mock
        from oda_browser import MathemBrowser
        url = "https://www.mathem.se/se/checkout/retry/?orderNumber=order-1&orderChangeId=change-1"
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._settle = mock.Mock()
        browser._invoke = mock.Mock(return_value={"tabs": [{"tabId": "owned", "active": True}]})
        browser._eval = mock.Mock(side_effect=[HouseholdError("navigation context destroyed"), {"url": url, "failed": True}])
        result = browser._capture_addition_failure("order-1", "owned")
        self.assertEqual(result, {"payment_failed": True, "order_id": "order-1", "order_change_id": "change-1"})
        self.assertEqual(browser._eval.call_count, 2)
        self.assertFalse(any(args.args[0] in {"open", "click"} for args in browser._invoke.call_args_list))
        for wrong in (url.replace("order-1", "order-2"), url.replace("www.mathem.se", "example.com"),
                      url + "&orderChangeId=change-2", url + "#other", url.replace("&orderChangeId=change-1", "")):
            browser._eval = mock.Mock(return_value={"url": wrong, "failed": True})
            self.assertEqual(browser._capture_addition_failure("order-1", "owned"), {"authentication_unresolved": True})
        browser._eval = mock.Mock(return_value={"url": url, "failed": False})
        self.assertEqual(browser._capture_addition_failure("order-1", "owned"), {"authentication_unresolved": True})
        browser._invoke.return_value = {"tabs": [{"tabId": "different", "active": True}]}
        browser._eval.reset_mock()
        self.assertEqual(browser._capture_addition_failure("order-1", "owned"), {"authentication_unresolved": True})
        browser._eval.assert_not_called()
        self.assertEqual(browser._capture_addition_failure("order-1", None), {"authentication_unresolved": True})

    def test_actual_addition_retry_scripts_bind_payable_overview_and_final_review(self):
        import json
        import shutil
        import subprocess
        from contextlib import nullcontext
        from test_payment_setup import PAYMENT_DOM
        from oda_browser import MathemBrowser, _oda_checkout_surface_script, _oda_checkout_amount_script
        node = shutil.which("node")
        if not node:
            self.skipTest("Node executes the actual final browser script")
        harness = PAYMENT_DOM.replace("Vi leverer varene dine", "Vi levererar din beställning")
        expected = {"delivery_address": "Eksempelveien 1", "total_minor": 1850, "product_count": 1}
        url = "https://www.mathem.se/se/checkout/retry/?orderNumber=order-1&orderChangeId=change-1"
        rows = [["1 vara", "18,50 kr"], ["Totalt inkl. moms", "18,51 kr"]]
        payment = {"method": "saved_card"}
        def evaluate(script, **change):
            config = {"url": url, "rows": rows, "selected": 2, "button": "Bekräfta och betala 18,50 kr", **change}
            result = subprocess.run([node, "-e", harness], input=json.dumps({"script": script, "c": config}), text=True, capture_output=True, check=True)
            return json.loads(result.stdout)
        read = _oda_checkout_amount_script(1850, expected_product_count=1, provider="mathem", retry=True, addition_retry=True)
        amounts = evaluate(read)["result"]
        self.assertTrue(amounts["amounts_valid"])
        self.assertEqual(amounts["amounts"]["provider_total"], 1851)
        self.assertEqual(amounts["amounts"]["product_subtotal"], 1850)
        self.assertFalse(evaluate(_oda_checkout_amount_script(1850, expected_product_count=1, provider="mathem", retry=True))["result"]["amounts_valid"])
        surface = evaluate(_oda_checkout_surface_script(expected, payment, provider="mathem"))["result"]
        review = json.loads(json.dumps({"order_id": "order-1", "binding": {}, "payment_choice": payment,
                                       "surface": surface, "amounts_minor": amounts["amounts"]}, sort_keys=True))
        changes = [{}, {"button": "Bekräfta och betala 18,51 kr"}, {"payDisabled": True}, {"selected": 0},
                   {"url": url.replace("change-1", "change-2")},
                   {"rows": [["1 vara", "18,50 kr"], ["Totalt inkl. moms", "18,52 kr"]]},
                   {"rows": [["1 vara", "18,49 kr"], ["Totalt inkl. moms", "18,51 kr"]]},
                   {"rows": [["2 varor", "18,50 kr"], ["Totalt inkl. moms", "18,51 kr"]]},
                   {"rows": rows + [["Extra avgift", "1,00 kr"]]},
                   {"rows": rows + [["Leverans", "1,00 kr"]]}]
        for change in changes:
            with self.subTest(change=change):
                browser = MathemBrowser.__new__(MathemBrowser)
                browser._checkout_dispatch_tab = lambda: None
                browser._checkout_deadline = None
                browser._checkout_operation = lambda *a, **k: nullcontext()
                browser._cart_expectation = lambda cart: expected
                browser._order_cart = lambda cart, *a: cart
                browser.review_payment_recovery = lambda *a, **k: deepcopy(review)
                observed, callbacks = [], []
                def final_eval(script):
                    self.assertEqual(callbacks, [True])
                    result = evaluate(script, **change)
                    observed.append(result)
                    return result["result"]
                browser._eval = final_eval
                def submit():
                    browser.submit_payment_recovery({}, review, lambda: callbacks.append(True),
                        addition={"before": {"order": {}}, "order_change_id": "change-1"})
                if change:
                    with self.assertRaises(CheckoutPreconditionError): submit()
                else:
                    submit()
                self.assertEqual(observed[0]["clicks"], [] if change else ["PAY"])


if __name__ == "__main__":
    unittest.main()
