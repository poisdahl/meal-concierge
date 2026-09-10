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

    def review_payment_recovery(self, cart, order_id, *, payment, expected_binding, **kwargs):
        review = {"order_id": order_id, "binding": deepcopy(expected_binding), "payment_choice": deepcopy(payment),
                  "payment_display": "Vipps" if payment["method"] == "vipps" else "•••• 1234", "amounts_minor": _oda_checkout_amounts_minor(AMOUNTS)}
        if self.review_change:
            self.review_change(review)
        return review

    def submit_payment_recovery(self, cart, review, before_click, **kwargs):
        if self.precondition_failure:
            raise CheckoutPreconditionError("merchant review changed")
        before_click()
        self.clicks += 1
        if self.lost_response:
            raise HouseholdError("response lost after dispatch")
        self.merchant.status = "paid_and_modifiable"

    def read_order_binding(self, order_id, order, *, expected_binding, **kwargs):
        return expected_binding


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
            }
        self.original = self.app.store.read()["pending_checkout"]

    def call(self, action, **kwargs):
        return self.app.handle({"operation": "checkout", "action": action, **kwargs})

    def prepare(self):
        return self.call("prepare", recovery=True)

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
        self.assertTrue(result["recovery_payment_unconfirmed"])
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
            self.assertNotIn("recovery_preparation_available", result)
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
                    browser.checkout_provider = "oda"
                    browser._checkout_deadline = None
                    browser._checkout_operation = lambda *a, **k: nullcontext()
                    browser._cart_expectation = lambda cart: expected
                    browser.review_payment_recovery = lambda *a, **k: deepcopy(review)
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
            self.assertIsNone(browser._capture_addition_failure("order-1", "owned"))
        browser._eval = mock.Mock(return_value={"url": url, "failed": False})
        self.assertIsNone(browser._capture_addition_failure("order-1", "owned"))
        browser._invoke.return_value = {"tabs": [{"tabId": "different", "active": True}]}
        browser._eval.reset_mock()
        self.assertIsNone(browser._capture_addition_failure("order-1", "owned"))
        browser._eval.assert_not_called()
        self.assertIsNone(browser._capture_addition_failure("order-1", None))

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
