"""Oda payment replacement through the service and reopened durable journals."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore
from service import Application
import test_payment_recovery as recovery_fixtures
import test_meal_concierge as checkout_fixtures


class OdaPaymentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.flow = recovery_fixtures.RecoveryTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.app = self.flow.app
        self.browser = self.flow.browser
        self.merchant = self.flow.merchant
        self.cancel_count = 0

    def call(self, action, **kwargs):
        return self.app.handle({"operation": "checkout", "action": action, **kwargs})

    def reopen(self):
        self.app = Application(StateStore(self.flow.temp.name, self.flow.settings), self.merchant, self.browser)
        self.app._now = lambda: self.flow.now

    @staticmethod
    def context(order_id="order-1"):
        return {"tab_id": "owned", "expected_total": 24640,
                "gateway_url_digest": "b" * 64, "order_id": order_id}

    def close(self, context, before_cancel, *, prior, **kwargs):
        self.assertEqual(context["expected_total"], 24640)
        if not prior.get("cancel_attempted"):
            before_cancel({"payment_id": "123456", "observed_status": "PENDING"})
            self.cancel_count += 1
        return {"status": "closed", "terminal_status": "REJECTED", "payment_id": "123456"}

    def prepare_gateway_failure(self, *, dispatch=False, changed_context=False):
        prepared = self.call("prepare", recovery=True)

        def submit(cart, review, before_click, *, on_vipps_gateway, before_vipps_request, **kwargs):
            before_click()
            self.browser.clicks += 1
            context = self.context()
            on_vipps_gateway(context)
            saved = self.app.store.read()["pending_checkout"]["recovery"]
            self.assertEqual(saved["vipps_request_status"], "prepared")
            self.assertNotIn("vipps_request_attempted_at", saved)
            if dispatch or changed_context:
                before_vipps_request({**context, **({"gateway_url_digest": "c" * 64} if changed_context else {})})
            raise HouseholdError("observed gateway interrupted")

        self.browser.submit_payment_recovery = submit
        with self.assertRaises(HouseholdError):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])
        return prepared

    def test_pre_next_gateway_survives_restart_and_closes_before_card_replacement(self):
        prepared = self.prepare_gateway_failure()
        self.reopen()
        self.browser.vipps_request_state = "prepared"
        observed = self.call("reconcile", confirmation_id=prepared["confirmation_id"])
        self.assertEqual(observed["payment_request_state"], "prepared")
        self.assertFalse(observed["awaiting_user_payment"])
        self.assertNotIn("recovery_preparation_available", observed)
        self.browser.close_vipps_request = self.close
        card = self.call("switch_payment", confirmation_id=prepared["confirmation_id"],
                         checkout_payment={"method": "saved_card"})
        self.assertEqual(card["summary"]["payment_method"], "saved_card")
        self.assertEqual((self.cancel_count, self.browser.clicks), (1, 1))
        archived = self.app.store.read()["protected_results"][prepared["confirmation_id"]]
        self.assertEqual(archived["failed_attempt"]["vipps_request_status"], "prepared")
        self.assertTrue(self.call("confirm", confirmation_id=prepared["confirmation_id"])["payment_closed"])
        self.browser.submit_payment_recovery = recovery_fixtures.MerchantBrowser.submit_payment_recovery.__get__(self.browser)
        self.assertTrue(self.call("confirm", confirmation_id=card["confirmation_id"])["confirmed"])
        self.assertEqual((self.cancel_count, self.browser.clicks), (1, 2))

    def test_pre_next_gateway_does_not_confirm_from_coarse_paid_tracking(self):
        prepared = self.prepare_gateway_failure()
        self.browser.vipps_request_state = "prepared"
        self.merchant.status = "paid_and_modifiable"
        result = self.call("reconcile", confirmation_id=prepared["confirmation_id"])
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["payment_request_state"], "prepared")
        self.assertIsNotNone(self.app.store.read()["pending_checkout"])

    def test_lost_post_next_response_preserves_fence_and_unknown_cancel_never_replaces(self):
        prepared = self.prepare_gateway_failure(dispatch=True)
        self.reopen()
        attempt = self.app.store.read()["pending_checkout"]["recovery"]
        self.assertEqual(attempt["vipps_request_status"], "dispatching")
        self.assertIn("vipps_request_attempted_at", attempt)

        def unresolved(context, before_cancel, *, prior, **kwargs):
            if not prior.get("cancel_attempted"):
                before_cancel({"payment_id": "123456"})
                self.cancel_count += 1
            return {"status": "unknown"}

        self.browser.close_vipps_request = unresolved
        for _ in range(2):
            result = self.call("switch_payment", confirmation_id=prepared["confirmation_id"],
                               checkout_payment={"method": "saved_card"})
            self.assertTrue(result["payment_switch_pending"])
            self.assertFalse(result["recovery_preparation_available"])
            self.reopen()
        self.assertEqual((self.cancel_count, self.browser.clicks), (1, 1))
        self.assertEqual(self.app.store.read()["pending_checkout"]["recovery"]["confirmation_id"], prepared["confirmation_id"])

    def test_next_cannot_replace_the_journalled_gateway_context(self):
        prepared = self.prepare_gateway_failure(changed_context=True)
        saved = self.app.store.read()["pending_checkout"]["recovery"]
        self.assertEqual(saved["confirmation_id"], prepared["confirmation_id"])
        self.assertEqual(saved["vipps_request_context"], self.context())
        self.assertEqual(saved["vipps_request_status"], "prepared")
        self.assertNotIn("vipps_request_attempted_at", saved)

    def set_card_attempt(self):
        with self.app.store.locked() as state:
            state["checkout_payment"] = {"method": "saved_card", "card_last4": None}
            pending = state["pending_checkout"]
            pending["checkout_payment"] = deepcopy(state["checkout_payment"])
            pending["browser_review"].update(payment_display="•••• 1234", payment_choice=deepcopy(state["checkout_payment"]))
            pending["summary"]["payment_method"] = "saved_card"
            pending.pop("vipps_request_status", None)
            pending.pop("unpaid_order_binding_source", None)
            pending["authentication_unresolved"] = True
            pending["authentication_context"] = {"tab_id": "bank", "payment_id": "123456"}
        self.browser.checkout_payment_authentication = lambda *args, **kwargs: None

    def test_card_to_vipps_requires_native_failure_then_dispatches_one_replacement(self):
        self.set_card_attempt()
        failure = None
        self.browser.checkout_payment_failure = lambda *args, **kwargs: deepcopy(failure)
        waiting = self.call("switch_payment", confirmation_id="original", checkout_payment={"method": "vipps"})
        self.assertTrue(waiting["payment_switch_pending"])
        self.assertNotIn("recovery", self.app.store.read()["pending_checkout"])
        self.assertEqual(self.browser.clicks, 0)
        failure = {"payment_failed": True, "order_id": "order-1"}
        self.reopen()
        vipps = self.call("switch_payment", confirmation_id="original", checkout_payment={"method": "vipps"})
        self.assertEqual(vipps["summary"]["payment_method"], "vipps")
        pending = self.app.store.read()["pending_checkout"]
        self.assertEqual(pending["payment_failure"], failure)
        self.assertNotIn("authentication_unresolved", pending)
        self.assertEqual(pending["recovery"]["payment_switch"]["closure"]["source"], "native_payment_failure")

        def submit(cart, review, before_click, *, on_vipps_gateway, before_vipps_request, **kwargs):
            before_click()
            self.browser.clicks += 1
            on_vipps_gateway(self.context())
            before_vipps_request(self.context())
            return {"vipps_request_sent": True}

        self.browser.submit_payment_recovery = submit
        self.browser.vipps_request_state = "sent"
        self.assertTrue(self.call("confirm", confirmation_id=vipps["confirmation_id"])["payment_request_sent"])
        self.reopen()
        self.call("confirm", confirmation_id=vipps["confirmation_id"])
        self.assertEqual(self.browser.clicks, 1)
        self.merchant.status = "paid_and_modifiable"
        self.assertTrue(self.call("reconcile", confirmation_id=vipps["confirmation_id"])["confirmed"])
        self.assertTrue(self.call("confirm", confirmation_id="original")["confirmed"])
        self.assertEqual(self.app.store.read()["checkout_payment"]["method"], "saved_card")

    def test_card_failure_for_another_order_cannot_unlock_replacement(self):
        self.set_card_attempt()
        self.browser.checkout_payment_failure = lambda *args, **kwargs: {"payment_failed": True, "order_id": "other-order"}
        with self.assertRaises(HouseholdError):
            self.call("switch_payment", confirmation_id="original", checkout_payment={"method": "vipps"})
        pending = self.app.store.read()["pending_checkout"]
        self.assertNotIn("payment_failure", pending)
        self.assertNotIn("recovery", pending)
        self.assertEqual(self.browser.clicks, 0)

    def test_prepared_replacement_can_switch_back_without_repeating_closure(self):
        original = self.prepare_gateway_failure()
        self.browser.close_vipps_request = self.close
        card = self.call("switch_payment", confirmation_id=original["confirmation_id"], checkout_payment={"method": "saved_card"})
        vipps = self.call("switch_payment", confirmation_id=card["confirmation_id"], checkout_payment={"method": "vipps"})
        self.assertNotEqual(vipps["confirmation_id"], card["confirmation_id"])
        self.assertEqual(vipps["summary"]["payment_method"], "vipps")
        self.assertEqual((self.cancel_count, self.browser.clicks), (1, 1))
        with self.assertRaises(HouseholdError):
            self.call("confirm", confirmation_id=card["confirmation_id"])
        self.assertEqual(self.app.store.read()["pending_checkout"]["recovery"]["confirmation_id"], vipps["confirmation_id"])

    def test_legacy_adoption_is_exact_and_does_not_establish_terminal_state(self):
        with self.app.store.locked() as state:
            state["pending_checkout"].pop("vipps_request_status", None)
            state["pending_checkout"].pop("vipps_request_context", None)
            state["pending_checkout"].pop("vipps_expiry_gateway_digest", None)
            state["pending_checkout"].pop("unpaid_order_binding_source", None)
        self.browser.payment_state = "retry_available"
        adopted = []

        def adopt(cart, review, *, order_id, **kwargs):
            self.assertEqual(order_id, "order-1")
            self.assertEqual(cart, recovery_fixtures.CART)
            adopted.append(True)
            return self.context(None)

        self.browser.adopt_vipps_request = adopt
        self.browser.close_vipps_request = lambda *args, **kwargs: {"status": "unknown"}
        result = self.call("switch_payment", confirmation_id="original", checkout_payment={"method": "saved_card"})
        self.assertTrue(result["payment_switch_pending"])
        pending = self.app.store.read()["pending_checkout"]
        self.assertEqual(pending["vipps_request_status"], "unknown")
        self.assertEqual(pending["vipps_request_context"], self.context(None))
        self.assertNotIn("vipps_request_attempted_at", pending)
        self.assertNotIn("recovery", pending)
        self.reopen()
        self.call("switch_payment", confirmation_id="original", checkout_payment={"method": "saved_card"})
        self.assertEqual(adopted, [True])
        self.assertEqual(self.browser.clicks, 0)

    def test_missing_or_wrong_legacy_evidence_never_adopts_or_replaces(self):
        with self.app.store.locked() as state:
            state["pending_checkout"].pop("vipps_request_status", None)
            state["pending_checkout"].pop("vipps_request_context", None)
            state["pending_checkout"].pop("vipps_expiry_gateway_digest", None)
            state["pending_checkout"].pop("unpaid_order_binding_source", None)
        self.browser.payment_state = "retry_available"
        for context in (None, self.context("another-order"), {**self.context(None), "expected_total": 1}):
            with self.subTest(context=context):
                self.browser.adopt_vipps_request = lambda *args, **kwargs: context
                with self.assertRaises(HouseholdError):
                    self.call("switch_payment", confirmation_id="original", checkout_payment={"method": "saved_card"})
                pending = self.app.store.read()["pending_checkout"]
                self.assertNotIn("vipps_request_context", pending)
                self.assertNotIn("recovery", pending)
        self.assertEqual(self.browser.clicks, 0)


class OdaAdditionReplacementLifecycleTests(unittest.TestCase):
    def test_pre_next_addition_does_not_confirm_from_goods_and_coarse_paid_tracking(self):
        flow = recovery_fixtures.OdaAdditionPaymentTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        prepared = flow.call("prepare")

        def submit(cart, order_id, order, review, before_click, *, on_vipps_gateway, **kwargs):
            before_click()
            on_vipps_gateway({"tab_id": "owned", "expected_total": 1670,
                              "gateway_url_digest": "d" * 64, "order_id": order_id})
            raise HouseholdError("gateway stopped before Next")

        flow.browser.submit_order_change = submit
        with self.assertRaises(HouseholdError):
            flow.call("confirm", confirmation_id=prepared["confirmation_id"])
        flow.accept_addition()
        flow.browser.vipps_request_state = "prepared"
        result = flow.call("reconcile", confirmation_id=prepared["confirmation_id"])
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["payment_request_state"], "prepared")
        self.assertIsNotNone(flow.app.store.read()["pending_checkout"])

    def test_vipps_to_card_then_failed_card_to_vipps_preserves_one_addition(self):
        import test_payment_switch as switch_fixtures
        flow = switch_fixtures.PaymentSwitchTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        card = flow.switch()
        flow.card_pending = True
        flow.browser.checkout_payment_authentication = lambda *args, **kwargs: None
        failure = None
        flow.browser.checkout_payment_failure = lambda *args, **kwargs: deepcopy(failure)
        waiting = flow.call("confirm", confirmation_id=card["confirmation_id"])
        self.assertEqual(waiting["payment_method"], "saved_card")
        self.assertEqual(waiting["authentication_status"], "unavailable")
        unknown = flow.call("switch_payment", confirmation_id=card["confirmation_id"], checkout_payment={"method": "vipps"})
        self.assertTrue(unknown["payment_switch_pending"])
        self.assertEqual(flow.card_clicks, 1)
        failure = {"payment_failed": True, "order_id": "order-1", "order_change_id": "change-1"}
        card_review = flow.browser.review_payment_recovery

        def review(cart, order_id, *, payment, **kwargs):
            result = card_review(cart, order_id, payment={"method": "saved_card"}, **kwargs)
            result.update(payment_choice=deepcopy(payment), payment_display="Vipps")
            return result

        flow.browser.review_payment_recovery = review
        flow.reopen()
        vipps = flow.call("switch_payment", confirmation_id=card["confirmation_id"], checkout_payment={"method": "vipps"})
        self.assertEqual(vipps["summary"]["payment_method"], "vipps")
        self.assertEqual(vipps["order_id"], "order-1")
        self.assertEqual((flow.cancel_clicks, flow.card_clicks), (1, 1))
        self.assertTrue(flow.call("confirm", confirmation_id=card["confirmation_id"])["payment_failed"])
        self.assertEqual(flow.app.store.read()["order_snapshots"], flow.flow.snapshots)
        current = flow.app.store.read()["pending_checkout"]["recovery"]
        self.assertEqual(current["confirmation_id"], vipps["confirmation_id"])
        self.assertEqual(current["payment_switch"]["target"]["order_change_id"], "change-1")


class OdaInitialPaymentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.flow = checkout_fixtures.FlowTests()
        self.flow.setUp()
        self.addCleanup(self.flow.tearDown)
        self.app = self.flow.app
        self.app._now = lambda: checkout_fixtures.ODA_FIXTURE_NOW
        self.browser = self.flow.browser

    def call(self, action, **kwargs):
        return self.app.handle({"operation": "checkout", "action": action, **kwargs})

    def test_initial_review_switches_both_directions_without_global_change_or_dispatch(self):
        card = self.call("prepare")
        original_preference = deepcopy(self.app.store.read()["checkout_payment"])
        vipps = self.call("switch_payment", confirmation_id=card["confirmation_id"], checkout_payment={"method": "vipps"})
        self.assertEqual(vipps["summary"]["payment_method"], "vipps")
        self.assertNotEqual(vipps["confirmation_id"], card["confirmation_id"])
        card_again = self.call("switch_payment", confirmation_id=vipps["confirmation_id"], checkout_payment={"method": "saved_card"})
        self.assertEqual(card_again["summary"]["payment_method"], "saved_card")
        self.assertEqual(self.app.store.read()["checkout_payment"], original_preference)
        self.assertEqual(self.browser.checkout_clicks, 0)
        with self.assertRaises(HouseholdError):
            self.call("confirm", confirmation_id=card["confirmation_id"])

    def test_initial_gateway_is_durable_before_next_and_survives_browser_failure(self):
        prepared = self.call("prepare", checkout_payment={"method": "vipps"})
        context = {"tab_id": "owned", "expected_total": 3500, "gateway_url_digest": "c" * 64, "order_id": None}

        def submit(cart, review, before_click, *, on_vipps_gateway, **kwargs):
            before_click()
            self.browser.checkout_clicks += 1
            on_vipps_gateway(context)
            self.assertEqual(self.app.store.read()["pending_checkout"]["vipps_request_status"], "prepared")
            raise HouseholdError("gateway interrupted before phone dispatch")

        self.browser.submit_checkout = submit
        with self.assertRaises(HouseholdError):
            self.call("confirm", confirmation_id=prepared["confirmation_id"])
        saved = StateStore(self.flow.temp.name, checkout_fixtures.CONFIG).read()["pending_checkout"]
        self.assertEqual(saved["status"], "uncertain")
        self.assertEqual(saved["vipps_request_context"], context)
        self.assertEqual(saved["vipps_request_status"], "prepared")
        self.assertNotIn("vipps_request_attempted_at", saved)
        self.assertEqual(self.browser.checkout_clicks, 1)

    def test_identity_review_reaches_browser_and_only_the_current_confirmation(self):
        proof = {"digest": "a" * 64, "decisions": [
            {"expected_index": 0, "actual_index": 0, "reason": "The same brand is displayed in the subtitle."}
        ]}
        original_review = self.browser.review_checkout
        original_submit = self.browser.submit_checkout
        seen = []

        def review(cart, *, identity_review, **kwargs):
            self.assertEqual(identity_review, proof)
            seen.append("review")
            return {**original_review(cart, **kwargs), "identity_review": deepcopy(identity_review)}

        def submit(cart, prepared_review, before_click, **kwargs):
            self.assertEqual(prepared_review["identity_review"], proof)
            seen.append("submit")
            return original_submit(cart, prepared_review, before_click, **kwargs)

        self.browser.review_checkout = review
        self.browser.submit_checkout = submit
        before = self.app.store.read()
        prepared = self.call("prepare", identity_review=proof)
        state = self.app.store.read()
        self.assertEqual(state["pending_checkout"]["browser_review"]["identity_review"], proof)
        self.assertNotIn("identity_review", state)
        self.assertEqual(state["checkout_payment"], before["checkout_payment"])
        self.assertEqual(state["profile"], before["profile"])
        self.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertEqual(seen, ["review", "submit"])
        self.assertEqual(self.browser.checkout_clicks, 1)

    def test_rejected_identity_review_preserves_prior_review_without_dispatch(self):
        from oda_browser import OdaCheckoutMismatchError
        original = self.call("prepare")
        saved = deepcopy(self.app.store.read()["pending_checkout"])

        def stale(cart, *, identity_review, **kwargs):
            raise OdaCheckoutMismatchError("Checkout identity digest changed; review the same current cart")

        self.browser.review_checkout = stale
        with self.assertRaisesRegex(OdaCheckoutMismatchError, "digest changed"):
            self.call("prepare", identity_review={"digest": "b" * 64, "decisions": []})
        self.assertEqual(self.app.store.read()["pending_checkout"], saved)
        self.assertEqual(saved["confirmation_id"], original["confirmation_id"])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_identity_review_rejects_other_actions_recovery_and_scheduled_scopes(self):
        proof = {"digest": "a" * 64, "decisions": []}
        before = self.app.store.read()
        cases = [
            {"action": "confirm"}, {"action": "submit"}, {"action": "reconcile"},
            {"action": "switch_payment"}, {"action": "auto"},
            {"action": "prepare", "recovery": True},
            {"action": "prepare", "occurrence": "2026-W37"},
            {"action": "prepare", "scheduler": {"manual": True}},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaisesRegex(HouseholdError, "identity_review"):
                self.app.handle({"operation": "checkout", "identity_review": proof, **case})
        with self.assertRaisesRegex(HouseholdError, "identity_review"):
            self.call("prepare", identity_review=[])
        self.assertEqual(self.app.store.read(), before)
        self.assertEqual(self.browser.review_deadlines, [])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_identity_review_rejects_existing_order_and_inherited_scheduled_work(self):
        proof = {"digest": "a" * 64, "decisions": []}
        for key, value in (
            ("order_change", {"status": "editing"}),
            ("pending_checkout", {"status": "awaiting_confirmation", "occurrence": "2026-W37"}),
        ):
            with self.subTest(key=key):
                with self.app.store.locked() as state:
                    state[key] = value
                before = self.app.store.read()
                with self.assertRaisesRegex(HouseholdError, "addition or scheduled"):
                    self.call("prepare", identity_review=proof)
                self.assertEqual(self.app.store.read(), before)
                with self.app.store.locked() as state:
                    state[key] = None
        self.assertEqual(self.browser.review_deadlines, [])


class CheckoutIdentityMcpRoutingTests(unittest.TestCase):
    def test_checkout_tool_forwards_scoped_identity_proof_and_omits_absence(self):
        import importlib.util
        import types
        from unittest import mock

        class FakeMCPServer:
            def __init__(self, *args, **kwargs):
                self.tools = {}

            def tool(self, **metadata):
                def register(function):
                    self.tools[function.__name__] = metadata
                    return function
                return register

        api = types.ModuleType("mcp.server.mcpserver")
        api.MCPServer = FakeMCPServer
        errors = types.ModuleType("mcp.server.mcpserver.exceptions")
        errors.ToolError = RuntimeError
        spec = importlib.util.spec_from_file_location("lifecycle_mcp_test", Path(__file__).resolve().parents[1] / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
            "mcp.server.mcpserver": api, "mcp.server.mcpserver.exceptions": errors,
        }):
            spec.loader.exec_module(module)
        module.rpc = mock.Mock(return_value={})
        proof = {"digest": "a" * 64, "decisions": [
            {"expected_index": 0, "actual_index": 0, "reason": "Same product; brand moved to subtitle."}
        ]}
        module.meal_concierge_checkout(action="prepare", identity_review=proof)
        self.assertEqual(module.rpc.call_args.kwargs["identity_review"], proof)
        module.meal_concierge_checkout(action="prepare")
        self.assertNotIn("identity_review", module.rpc.call_args.kwargs)


class OdaNativeCardDeclineTests(unittest.TestCase):
    """Native pay-response evidence, including declines before any 3DS redirect."""
    def setUp(self):
        import json
        from contextlib import nullcontext
        from oda_browser import OdaBrowser
        self.json = json
        self.browser = OdaBrowser.__new__(OdaBrowser)
        self.browser._checkout_operation = lambda *a, **kw: nullcontext()
        self.browser._checkout_deadline = None
        self.browser._settle = lambda *a: None
        self.tab = "owned"
        self.urls = {"owned": "https://oda.com/no/checkout/confirm/"}
        self.records = {}
        self.events = []

        def invoke(*args):
            self.events.append(args)
            if args == ("tab", "list"):
                return {"tabs": [{"tabId": tab, "active": self.tab == tab} for tab in self.urls]}
            if len(args) == 2 and args[0] == "tab":
                self.assertIn(args[1], self.urls)
                self.tab = args[1]
                return {}
            if args[:2] == ("network", "requests"):
                return {"requests": [deepcopy(r) for r in self.records.values()]}
            if args[:2] == ("network", "request"):
                return deepcopy(self.records.get(args[2], {}))
            if args == ("get", "url"):
                return {"url": self.urls[self.tab]}
            self.fail("Unexpected native operation")

        self.browser._invoke = invoke
        self.browser._checkout_payment_observation = lambda tab: (
            {"url": self.urls[self.tab]} if self.tab == tab else None)

    def native(self, request_id="new", *, mode=None, order="order-1", change=None):
        # The public storefront posts camelCase with X-Requested-Case: camel.
        return {"requestId": request_id, "timestamp": 123456789, "method": "POST", "status": 200,
                "url": "https://oda.com/api/v1/checkout/pay/",
                "postData": self.json.dumps({"mode": mode or {"type": "confirm"}, "primaryPayment": {
                    "methodId": 12, "paymentMethodData": {"provider": "adyen", "storedPaymentMethodId": "synthetic-card"}}}),
                "responseBody": self.json.dumps({"type": "checkout-payment-retry",
                    "params": {"orderNumber": order, "orderChangeId": change}})}

    def decline(self, *, mode=None, change=None, old=False):
        source = ("https://oda.com/no/checkout/retry/?orderNumber=order-1"
                  + ("&orderChangeId=" + str(change) if change is not None else "")) if mode and mode["type"].startswith("retry") else (
                  "https://oda.com/no/checkout/confirm/" + ("?orderNumber=order-1" if change is not None else ""))
        self.urls["owned"] = source
        if old:
            self.records["old"] = self.native("old", mode=mode, change=change)
        fence = self.browser._oda_card_request_fence("owned", source)
        self.records["new"] = self.native(mode=mode, change=change)
        self.urls["owned"] = "https://oda.com/no/checkout/retry/?orderNumber=order-1" + (
            "&orderChangeId=" + str(change) if change is not None else "")
        result = self.browser._capture_checkout_payment("owned", card_request_fence=fence,
            **({"order_id": "order-1"} if change is not None and not mode["type"].startswith("retry") else {}),
            capture_failure=not old)
        return result, fence

    def test_direct_decline_all_modes_retains_exact_response_for_later_resolution(self):
        cases = [({"type": "confirm"}, None), ({"type": "retry", "orderNumber": "order-1"}, None),
                 ({"type": "confirm_modification", "orderNumber": "order-1"}, 17),
                 ({"type": "retry_modification", "orderNumber": "order-1", "orderChangeId": 17}, 17)]
        for mode, change in cases:
            with self.subTest(mode=mode):
                self.records.clear()
                result, _ = self.decline(mode=mode, change=change, old=mode["type"].startswith("retry"))
                context = result["authentication_context"]
                self.assertEqual(set(context), {"tab_id", "checkout_request_id", "checkout_request_digest"})
                self.assertEqual(context["checkout_request_id"], "new")
                self.assertIsNone(self.browser.checkout_payment_authentication(context))
                expected = {"payment_failed": True, "order_id": "order-1", **({"order_change_id": "17"} if change else {})}
                self.assertEqual(self.browser.checkout_payment_failure(context,
                    **({"expected_order_id": "order-1"} if change else {})), expected)
                self.assertTrue(all(event[0] in {"tab", "get", "network"} for event in self.events))

    def test_native_optional_change_is_accepted_only_for_new_order_modes(self):
        for mode, change in [({"type": "confirm"}, None), ({"type": "retry", "orderNumber": "order-1"}, None),
                             ({"type": "confirm_modification", "orderNumber": "order-1"}, 17),
                             ({"type": "retry_modification", "orderNumber": "order-1", "orderChangeId": 17}, 17)]:
            with self.subTest(mode=mode):
                self.records.clear()
                result, fence = self.decline(mode=mode, change=change)
                response = self.json.loads(self.records["new"]["responseBody"])
                response["params"].pop("orderChangeId")
                self.records["new"]["responseBody"] = self.json.dumps(response)
                captured = self.browser._capture_checkout_payment("owned", card_request_fence=fence, capture_failure=False)
                if change is None:
                    self.assertEqual(captured, result)
                    self.assertEqual(self.browser.checkout_payment_failure(result["authentication_context"]),
                                     {"payment_failed": True, "order_id": "order-1"})
                else:
                    self.assertEqual(captured, {"authentication_unresolved": True})
                    self.assertIsNone(self.browser.checkout_payment_failure(result["authentication_context"], expected_order_id="order-1"))

    def test_old_failure_missing_fence_ambiguous_or_changed_mode_remain_unresolved(self):
        mode = {"type": "retry", "orderNumber": "order-1"}
        result, fence = self.decline(mode=mode, old=True)
        baseline = deepcopy(self.records)
        for scenario in ("old_only", "missing_fence", "ambiguous", "mode", "foreign", "missing_body", "pending"):
            with self.subTest(scenario=scenario):
                self.records = deepcopy(baseline)
                selected_fence = fence
                if scenario == "old_only":
                    self.records.pop("new")
                elif scenario == "missing_fence":
                    selected_fence = None
                elif scenario == "ambiguous":
                    self.records["duplicate"] = self.native("duplicate", mode=mode)
                elif scenario == "mode":
                    self.records["new"] = self.native(mode={"type": "confirm"})
                elif scenario == "foreign":
                    self.records["new"]["url"] = "https://example.com/api/v1/checkout/pay/"
                elif scenario == "missing_body":
                    self.records["new"].pop("responseBody")
                else:
                    self.records["new"].pop("status")
                self.assertEqual(self.browser._capture_checkout_payment("owned", card_request_fence=selected_fence,
                    capture_failure=False), {"authentication_unresolved": True})

    def test_retained_decline_rejects_changed_request_outcome_route_and_target(self):
        result, _ = self.decline(mode={"type": "retry_modification", "orderNumber": "order-1", "orderChangeId": 17}, change=17)
        context = result["authentication_context"]
        original = deepcopy(self.records["new"])
        url = self.urls["owned"]
        for scenario in ("timestamp", "post", "status", "accepted", "order", "change", "params", "route", "duplicate_query", "missing_tab", "wrong_expected_order"):
            with self.subTest(scenario=scenario):
                self.records["new"] = deepcopy(original)
                self.urls = {"owned": url}
                self.tab = "owned"
                expected = "order-1"
                response = self.json.loads(original["responseBody"])
                if scenario == "timestamp":
                    self.records["new"]["timestamp"] += 1
                elif scenario == "post":
                    self.records["new"]["postData"] += " "
                elif scenario == "status":
                    self.records["new"]["status"] = 400
                elif scenario == "accepted":
                    response["type"] = "checkout-payment-success"
                elif scenario == "order":
                    response["params"]["orderNumber"] = "other"
                elif scenario == "change":
                    response["params"]["orderChangeId"] = 18
                elif scenario == "params":
                    response["params"]["orderChangeId"] = "17"
                elif scenario == "route":
                    self.urls["owned"] = url.replace("oda.com", "example.com")
                elif scenario == "duplicate_query":
                    self.urls["owned"] += "&orderNumber=order-1"
                elif scenario == "missing_tab":
                    self.urls = {"other": url}
                    self.tab = "other"
                else:
                    expected = "other"
                self.records["new"]["responseBody"] = self.json.dumps(response)
                self.assertIsNone(self.browser.checkout_payment_failure(context, expected_order_id=expected))

    def test_new_authentication_still_retains_3ds_context_instead_of_old_decline(self):
        self.urls["owned"] = "https://oda.com/no/checkout/retry/?orderNumber=order-1"
        self.records["old"] = self.native("old", mode={"type": "retry", "orderNumber": "order-1"})
        fence = self.browser._oda_card_request_fence("owned", self.urls["owned"])
        context = {"tab_id": "owned", "payment_id": "123456"}
        observations = iter([{"url": self.urls["owned"]}, {"url": "https://oda.com/no/checkout/threeDS/?paymentId=123456",
                             "authentication_context": context}] + [None])
        self.browser._checkout_payment_observation = lambda tab: next(observations)
        self.assertEqual(self.browser._capture_checkout_payment("owned", card_request_fence=fence,
            capture_failure=False), {"authentication_context": context})

    def test_final_click_takes_fence_before_callback_and_retains_only_new_response(self):
        from oda_browser import CHECKOUT_URL
        self.records["old"] = self.native("old")
        trace = []
        original_fence = self.browser._oda_card_request_fence
        def fence(*args):
            trace.append("fence")
            return original_fence(*args)
        self.browser._oda_card_request_fence = fence
        def click(script):
            trace.append("click")
            self.records["new"] = self.native()
            self.urls["owned"] = "https://oda.com/no/checkout/retry/?orderNumber=order-1"
            return {"clicked": True}
        self.browser._eval = click
        result = self.browser._click_checkout_submit(4550, CHECKOUT_URL, lambda: trace.append("journal"),
            expected_product_count=1, expected_amounts={"product_subtotal": 26.5, "delivery_price": 19,
                "discounts": None, "deposits": None, "bags": None, "other_fees": None, "provider_total": 45.5})
        self.assertEqual(trace, ["fence", "journal", "click"])
        self.assertEqual(result["authentication_context"]["checkout_request_id"], "new")


    def test_recovery_submit_captures_fresh_decline_on_unchanged_retry_page(self):
        mode = {"type": "retry", "orderNumber": "order-1"}
        url = "https://oda.com/no/checkout/retry/?orderNumber=order-1"
        self.urls["owned"] = url
        self.records["old"] = self.native("old", mode=mode)
        review = {"order_id": "order-1", "binding": {}, "payment_choice": {"method": "saved_card"},
                  "surface": {"url": url}, "amounts_minor": {"provider_total": 4550}}
        self.browser.review_payment_recovery = lambda *args, **kwargs: deepcopy(review)
        self.browser._cart_expectation = lambda cart: {"delivery_address": "Example street 1", "total_minor": 4550, "product_count": 1}
        self.browser._require_checkout_time = lambda seconds: None
        callbacks = []
        def dispatch():
            self.assertTrue(any(event[:2] == ("network", "requests") for event in self.events))
            callbacks.append(True)
        def click(script):
            self.assertEqual(callbacks, [True])
            self.records["new"] = self.native(mode=mode)
            return {"clicked": True}
        self.browser._eval = click
        result = self.browser.submit_payment_recovery({}, review, dispatch)
        self.assertEqual(result["authentication_context"]["checkout_request_id"], "new")
        self.assertEqual(self.browser.checkout_payment_failure(result["authentication_context"]),
                         {"payment_failed": True, "order_id": "order-1"})


class OdaDirectCardPersistenceTests(unittest.TestCase):
    def test_native_decline_context_reopens_and_unlocks_same_order_switch(self):
        flow = OdaPaymentLifecycleTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        native = OdaNativeCardDeclineTests()
        native.setUp()
        result, _ = native.decline(mode={"type": "confirm"})
        flow.set_card_attempt()
        with flow.app.store.locked() as state:
            state["pending_checkout"]["authentication_context"] = result["authentication_context"]
        flow.browser.checkout_payment_authentication = native.browser.checkout_payment_authentication
        flow.browser.checkout_payment_failure = native.browser.checkout_payment_failure
        flow.reopen()
        replacement = flow.call("switch_payment", confirmation_id="original", checkout_payment={"method": "vipps"})
        self.assertEqual(replacement["summary"]["payment_method"], "vipps")
        state = flow.app.store.read()["pending_checkout"]
        self.assertEqual(state["payment_failure"], {"payment_failed": True, "order_id": "order-1"})
        self.assertEqual(state["recovery"]["payment_switch"]["closure"]["card_failure_context"], result["authentication_context"])
        self.assertEqual(flow.browser.clicks, 0)

    def test_recovery_vipps_ack_without_dispatch_callback_does_not_claim_sent(self):
        flow = OdaPaymentLifecycleTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        prepared = flow.call("prepare", recovery=True)
        def submit(cart, review, before_click, **kwargs):
            before_click()
            flow.browser.clicks += 1
            return {"vipps_request_sent": True}
        flow.browser.submit_payment_recovery = submit
        with self.assertRaisesRegex(HouseholdError, "no durable dispatch"):
            flow.call("confirm", confirmation_id=prepared["confirmation_id"])
        flow.reopen()
        child = flow.app.store.read()["pending_checkout"]["recovery"]
        self.assertNotEqual(child.get("vipps_request_status"), "sent")
        self.assertNotIn("payment_requested_at", child)
        flow.call("confirm", confirmation_id=prepared["confirmation_id"])
        self.assertEqual(flow.browser.clicks, 1)

    def test_direct_addition_target_rechecks_native_failure_without_reloading_source(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from oda_payment_switch import prepare_oda_addition_retry, verify_oda_addition_retry
        native = OdaNativeCardDeclineTests()
        native.setUp()
        result, _ = native.decline(mode={"type": "retry_modification", "orderNumber": "order-1", "orderChangeId": 17}, change=17)
        context = result["authentication_context"]
        browser = native.browser
        original = deepcopy(recovery_fixtures.Merchant().order)
        cart = deepcopy(recovery_fixtures.CART)
        binding = {"account_reference_digest": "a" * 64, "receipt_address": "Example street 1"}
        browser._binding_client = lambda: SimpleNamespace(call=lambda *args, **kwargs: deepcopy(original))
        browser._read_order_binding = lambda *args, **kwargs: deepcopy(binding)
        browser._open = lambda *args: self.fail("A retained native failure must not be reloaded")
        @contextmanager
        def inspection():
            previous = native.tab
            native.urls["inspection"] = "https://oda.com/no/"
            native.tab = "inspection"
            try:
                yield
            finally:
                native.tab = previous
        browser._inspection_tab = inspection
        closure = {"status": "closed", "terminal_status": "FAILED", "source": "native_payment_failure",
                   "card_failure_context": context, "order_id": "order-1", "order_change_id": "17"}
        target = prepare_oda_addition_retry(browser, "order-1", cart, original, binding, deadline=None, closure=closure)
        self.assertEqual(target["card_failure_context"], context)
        self.assertNotIn("payment_id", target)
        native.urls["replacement"] = native.urls["owned"]
        native.tab = "replacement"
        verify_oda_addition_retry(browser, "order-1", cart, original, binding, target, deadline=None)
        self.assertEqual(native.tab, "replacement")
        native.records["new"]["timestamp"] += 1
        with self.assertRaisesRegex(HouseholdError, "native card failure"):
            verify_oda_addition_retry(browser, "order-1", cart, original, binding, target, deadline=None)
        self.assertEqual(native.tab, "replacement")

    def test_addition_recovery_uses_distinct_stable_tab_before_account_navigation(self):
        from contextlib import nullcontext
        from oda_browser import OdaBrowser
        browser = OdaBrowser.__new__(OdaBrowser)
        browser._checkout_operation = lambda *args, **kwargs: nullcontext()
        browser.verify_oda_addition_retry = lambda *args, **kwargs: None
        binding = {"account_reference_digest": "a" * 64, "receipt_address": "Example street 1"}
        browser._order_cart = lambda cart, *args: cart
        browser._cart_expectation = lambda cart: {"delivery_address": binding["receipt_address"],
                                                  "total_minor": 2040, "product_count": 1}
        digest = "d" * 64
        label = "meal-concierge-payment-recovery-" + digest
        tabs = [{"tabId": "failed", "label": "meal-concierge-payment-recovery"}]
        active, navigated = ["failed"], []
        def invoke(*args):
            if args == ("tab", "list"):
                return {"tabs": deepcopy(tabs)}
            if args[:2] == ("tab", "new"):
                self.assertEqual(args[2:4], ("--label", label))
                tabs.append({"tabId": "replacement", "label": label})
                active[0] = "replacement"
                return {}
            if args == ("tab", "replacement"):
                active[0] = "replacement"
                return {}
            self.fail("The retained failed payment tab must not be selected for navigation")
        browser._invoke = invoke
        def verify(address):
            navigated.append(active[0])
            raise HouseholdError("stop after verifying navigation target")
        browser._verify_checkout_account = verify
        addition = {"order_change_id": "17", "before": {"order": {}}, "payment_switch_target": {
            "order_change_id": "17", "card_failure_context": {"tab_id": "failed", "checkout_request_id": "native", "checkout_request_digest": digest}}}
        for _ in range(2):
            with self.assertRaisesRegex(HouseholdError, "stop after"):
                browser.review_payment_recovery({}, "order-1", payment={"method": "vipps"}, expected_binding=binding, addition=addition)
        self.assertEqual(navigated, ["replacement", "replacement"])
        self.assertEqual(len(tabs), 2)
