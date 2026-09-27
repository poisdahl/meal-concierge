"""Owner-local retirement preserves sent payment evidence without provider writes."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_payment_recovery as fixtures
from core import HouseholdError, StateStore
from service import Application


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.flow = fixtures.RecoveryTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.app, self.merchant, self.browser = self.flow.app, self.flow.merchant, self.flow.browser
        self.merchant.status = "delivered"
        self.merchant.order["grossAmount"] = 239.65
        self.authorization = {"source": "explicit_owner_instruction", "reference": "owner-message-1",
                              "instruction": "Retire this stale local payment attempt; preserve its history."}
        with self.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.update(status="awaiting_user_payment", vipps_request_status="sent",
                           vipps_request_attempted_at=self.flow.now.isoformat())
            pending.pop("vipps_expiry_gateway_digest")
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
            state["menu"] = {"menu_id": "current", "phase": "draft"}
            state["cart_plan"] = {"provider": "oda", "status": "active", "menu_ref": {"menu_id": "current"},
                                  "product_names": {}, "baseline_quantities": {}, "required_quantities": {},
                                  "added_quantities": {}, "last_synced_quantities": {}, "start_as_extra_product_ids": []}
            state["recipe_usage"] = {"current": {"status": "planned"}}
            self.app._bind_protected_request(state, "checkout", "original-submit", "original")
        self.freeze()
        # Any unexpected provider operation fails, including cart reads or writes.
        self.original_call = self.merchant.call
        self.merchant.call = mock.Mock(side_effect=self.original_call)
        self.browser.read_order_binding = mock.Mock(wraps=self.browser.read_order_binding)

    def freeze(self):
        self.before = self.app.store.read()
        self.pending = deepcopy(self.before["pending_checkout"])
        self.digest = self.app._checkout_cancellation_digest(self.pending)

    def retire(self, **changes):
        return self.app.handle({"operation": "checkout", "action": "retire_attempt",
                               "confirmation_id": (self.pending.get("recovery") or self.pending)["confirmation_id"],
                               "order_id": "order-1", "expected_checkout_digest": self.digest,
                               "owner_authorization": deepcopy(self.authorization), **changes})

    def test_archives_sent_journal_and_delivered_discrepancy_without_purchase_effects(self):
        result = self.retire()
        self.assertTrue(result["retired_locally"])
        self.assertFalse(result["confirmed"])
        self.assertNotIn("cancelled", result)
        self.assertNotIn("payment_dispatched", result)
        self.assertEqual(result["tracking_status"], "delivered")
        self.assertEqual(result["difference_ore"], -675)
        self.assertEqual(result["payment"]["charge"], "unknown")
        self.assertEqual(result["payment"]["authorization"], "unknown")
        self.assertEqual(result["payment_resolution"], {"authorization_release": "unknown", "refund": "unknown"})
        after = self.app.store.read()
        record = after["protected_results"]["original"]
        self.assertEqual(record["retired_attempt"], self.pending)
        self.assertEqual(record["retirement_evidence"]["order"], self.merchant.order)
        self.assertEqual(record["result"]["owner_authorization"], self.authorization)
        self.assertIsNone(after["pending_checkout"])
        changed = {key for key in after if after[key] != self.before.get(key)}
        self.assertEqual(changed, {"pending_checkout", "protected_results", "protected_requests", "last_checkout_confirmation_id"})
        self.assertEqual([call.args[0] for call in self.merchant.call.call_args_list],
                         ["get_orders", "get_order", "order_tracking"])
        self.assertEqual(self.browser.clicks, 0)
        self.assertEqual(self.browser.read_order_binding.call_args.kwargs["expected_binding"],
                         {"account_reference_digest": "a" * 64, "receipt_address": "Example street 1"})

    def test_replays_original_child_and_submit_after_restart_without_reads(self):
        with self.app.store.locked() as state:
            state["pending_checkout"]["recovery"] = {
                "confirmation_id": "child", "order_id": "order-1", "status": "uncertain",
                "vipps_request_context": {"order_id": "order-1", "gateway_url_digest": "b" * 64},
            }
            self.app._bind_protected_request(state, "checkout", "child-submit", "child")
        self.freeze()
        result = self.retire()
        self.app = Application(StateStore(self.flow.temp.name, self.flow.settings), self.merchant, self.browser)
        self.merchant.call.side_effect = AssertionError("replay must be local")
        self.browser.read_order_binding.side_effect = AssertionError("replay must be local")
        with self.app.store.locked() as state:
            state["pending_checkout"] = {**deepcopy(self.pending), "confirmation_id": "newer", "recovery": None}
        before_replays = self.app.store.read()
        for cid in ("original", "child"):
            self.assertEqual(self.retire(confirmation_id=cid), {**result, "idempotent": True})
            for action in ("confirm", "reconcile"):
                self.assertEqual(self.app.handle({"operation": "checkout", "action": action,
                                                 "confirmation_id": cid}), {**result, "idempotent": True})
        for key in ("original-submit", "child-submit"):
            self.assertEqual(self.app.handle({"operation": "checkout", "action": "submit", "idempotency_key": key}),
                             {**result, "idempotent": True})
        self.assertEqual(self.app.store.read(), before_replays)
        self.assertEqual(self.app.store.read()["protected_results"]["original"]["retired_attempt"], self.pending)
        with self.assertRaises(HouseholdError):
            self.retire(owner_authorization={**self.authorization, "reference": "another-message"})
        with self.assertRaises(HouseholdError):
            self.retire(order_id="other-order")

    def test_retired_order_cannot_be_rebound_after_restart(self):
        self.retire()
        self.app = Application(StateStore(self.flow.temp.name, self.flow.settings), self.merchant, self.browser)
        with self.app.store.locked() as state:
            state["pending_checkout"] = {**deepcopy(self.pending), "confirmation_id": "later"}
        before = self.app.store.read()
        with self.assertRaisesRegex(HouseholdError, "explicitly abandoned"):
            self.app._checkout_recovery_target(before["pending_checkout"], None, "order-1")
        self.assertEqual(self.app.store.read(), before)
        self.assertEqual(self.app._abandoned_order_ids(before), {"order-1"})

    def test_request_and_current_state_guards_do_not_mutate_or_read_provider(self):
        for changes in ({"owner_authorization": None}, {"owner_authorization": {}},
                        {"owner_authorization": {**self.authorization, "source": "automatic"}},
                        {"expected_checkout_digest": "f" * 64}, {"confirmation_id": "old"}):
            with self.subTest(changes=changes), self.assertRaises(HouseholdError):
                self.retire(**changes)
            self.assertEqual(self.app.store.read(), self.before)
        for key, value in (("occurrence", "weekly"), ("scheduler_context", {"manual": True}),
                           ("automatic_checkout", True), ("order_change", {"order_id": "order-1"}),
                           ("unpaid_order_id", "other-order"), ("status", "awaiting_confirmation")):
            with self.app.store.locked() as state:
                state["pending_checkout"] = {**deepcopy(self.pending), key: value}
            before = self.app.store.read()
            with self.subTest(key=key), self.assertRaises(HouseholdError):
                self.retire(expected_checkout_digest=self.app._checkout_cancellation_digest(before["pending_checkout"]))
            self.assertEqual(self.app.store.read(), before)
        self.merchant.call.assert_not_called()

    def test_provider_identity_and_binding_failures_preserve_journal(self):
        cases = [
            ("get_orders", {"orders": [self.merchant.order, {**self.merchant.order, "orderNumber": "other"}]}),
            ("get_order", {**self.merchant.order, "orderNumber": "other"}),
            ("order_tracking", {"orderNumber": "other", "status": "delivered"}),
            ("order_tracking", {"orderNumber": "order-1", "status": "unpaid_order"}),
            ("get_order", {**self.merchant.order, "currency": "SEK"}),
            ("get_order", {**self.merchant.order, "deliveryDate": "2026-09-13"}),
            ("get_order", {**self.merchant.order, "deliverySlotDisplay": "Lør 12. september 08:00 - 13:00"}),
            ("get_order", {**self.merchant.order, "deliveryAddress": "Other address"}),
            ("get_order", {**self.merchant.order, "products": [{"product_id": "other", "quantity": 1, "price": 16.7}]}),
            ("get_order", {**self.merchant.order, "products": [{**self.merchant.order["products"][0], "quantity": 2}]}),
        ]
        for tool, response in cases:
            self.merchant.call.side_effect = lambda name, arguments, **kw: deepcopy(response) if name == tool else self.original_call(name, arguments, **kw)
            with self.subTest(tool=tool, response=response), self.assertRaises(HouseholdError):
                self.retire()
            self.assertEqual(self.app.store.read(), self.before)
        self.merchant.call.side_effect = self.original_call
        self.browser.read_order_binding.return_value = {"account_reference_digest": "b" * 64,
                                                       "receipt_address": "Example street 1"}
        with self.assertRaisesRegex(HouseholdError, "account differs"):
            self.retire()
        self.assertEqual(self.app.store.read(), self.before)
        self.browser.read_order_binding.side_effect = HouseholdError("inspection unavailable")
        with self.assertRaisesRegex(HouseholdError, "inspection unavailable"):
            self.retire()
        self.assertEqual(self.app.store.read(), self.before)

    def test_concurrent_change_during_inspection_is_preserved(self):
        def changed(*args, expected_binding, **kwargs):
            with self.app.store.locked() as state:
                state["pending_checkout"]["payment_requested_at"] = "concurrent observation"
            return expected_binding
        self.browser.read_order_binding.side_effect = changed
        with self.assertRaisesRegex(HouseholdError, "changed during"):
            self.retire()
        after = self.app.store.read()
        self.assertEqual(after["pending_checkout"]["payment_requested_at"], "concurrent observation")
        after["pending_checkout"].pop("payment_requested_at")
        self.assertEqual(after, self.before)

    def test_other_active_operations_and_retained_request_identity_block_retirement(self):
        for key in ("pending_cancellation", "order_change", "pending_cart_change"):
            with self.app.store.locked() as state:
                state[key] = {"status": "uncertain"}
            before = self.app.store.read()
            with self.subTest(key=key), self.assertRaises(HouseholdError):
                self.retire()
            self.assertEqual(self.app.store.read(), before)
            with self.app.store.locked() as state:
                state[key] = None
        with self.app.store.locked() as state:
            state["pending_checkout"]["vipps_request_context"]["order_id"] = "other-order"
        self.freeze()
        with self.assertRaisesRegex(HouseholdError, "retained payment identity"):
            self.retire()
        self.assertEqual(self.app.store.read(), self.before)
        self.merchant.call.assert_not_called()

    def test_releases_only_detached_usage_and_keeps_prior_history(self):
        with self.app.store.locked() as state:
            state["pending_checkout"]["menu"] = {"menu_id": "detached"}
            state["pending_checkout"]["summary"]["menu_attribution"] = "menu_bound"
            state["recipe_usage"]["detached"] = {"status": "planned"}
            state["protected_results"]["old"] = {"kind": "checkout", "failed_attempt": {"status": "uncertain"}}
        self.freeze()
        self.retire()
        after = self.app.store.read()
        self.assertEqual(after["recipe_usage"]["current"], self.before["recipe_usage"]["current"])
        self.assertEqual(after["recipe_usage"]["detached"]["status"], "cancelled")
        self.assertEqual(after["protected_results"]["old"], self.before["protected_results"]["old"])
        self.assertEqual(after["menu"], self.before["menu"])
        self.assertEqual(after["cart_plan"], self.before["cart_plan"])


if __name__ == "__main__":
    unittest.main()
