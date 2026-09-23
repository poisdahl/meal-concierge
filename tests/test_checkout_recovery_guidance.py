"""Workflow guidance for the active checkout payment attempt."""

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planning_assessment import workflow_status


class CheckoutRecoveryGuidanceTests(unittest.TestCase):
    def action(self, pending, provider="oda"):
        return workflow_status({"provider": provider, "pending_checkout": pending})["next_action"]

    def test_contextless_legacy_vipps_keeps_original_and_explains_exact_review(self):
        action = self.action({"status": "awaiting_user_payment", "confirmation_id": "original",
                              "checkout_payment": {"method": "vipps"}})
        self.assertEqual((action["action"], action["confirmation_id"]), ("reconcile", "original"))
        self.assertEqual(action["payment_method"], "vipps")
        self.assertNotIn("Approve the existing payment request", action["reason"])
        for required in ("no Vipps request or manual payment", "recovery=true", "order_id", "original confirmation_id",
                         "vipps_request_not_received=true", "saved_card or vipps", "checkout_payment",
                         "independently verify", "Do not erase the journal"):
            self.assertIn(required, action["reason"])

    def test_legacy_review_hint_excludes_conflicting_journal_evidence(self):
        base = {"status": "awaiting_user_payment", "confirmation_id": "original",
                "checkout_payment": {"method": "vipps"}}
        conflicts = ({"owner_vipps_approval_completed_at": "2026-09-23T12:00:00+00:00"},
                     {"payment_failure": {"payment_failed": True}},
                     {"payment_switch": {"status": "closed"}},
                     {"automatic_checkout": True}, {"authentication_unresolved": True},
                     {"unpaid_order_binding_source": "different_source"})
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                action = self.action({**base, **conflict})
                self.assertEqual(action["action"], "reconcile")
                self.assertNotIn("recovery=true", action["reason"])

    def test_sent_vipps_requires_positive_request_evidence(self):
        pending = {"status": "awaiting_user_payment", "confirmation_id": "original",
                   "checkout_payment": {"method": "vipps"}, "vipps_request_status": "sent"}
        for evidence in ({}, {"vipps_request_context": {}},
                         {"vipps_request_context": {"tab_id": "tab"}}):
            with self.subTest(evidence=evidence):
                self.assertNotIn("Approve that exact request", self.action({**pending, **evidence})["reason"])
        sent = {**pending, "vipps_request_context": {"tab_id": "tab"},
                "payment_requested_at": "2026-09-23T12:00:00+00:00"}
        action = self.action(sent)
        self.assertEqual((action["action"], action["confirmation_id"]), ("reconcile", "original"))
        self.assertIn("Approve that exact request", action["reason"])

    def test_saved_card_awaiting_payment_never_claims_vipps_request(self):
        action = self.action({"status": "awaiting_user_payment", "confirmation_id": "original",
                              "checkout_payment": {"method": "saved_card"}})
        self.assertEqual(action["action"], "reconcile")
        self.assertEqual(action["payment_method"], "saved_card")
        self.assertNotIn("Approve", action["reason"])
        self.assertNotIn("Vipps", action["reason"])

    def test_meny_acknowledged_request_retains_phone_approval_guidance(self):
        base = {"status": "awaiting_user_payment", "confirmation_id": "meny-original"}
        self.assertNotIn("Approve that exact request", self.action(base, provider="meny")["reason"])
        acknowledged = {**base, "payment_requested_at": "2026-09-23T12:00:00+00:00",
                        "payment_expires_at": "2026-09-23T12:10:00+00:00"}
        action = self.action(acknowledged, provider="meny")
        self.assertEqual((action["action"], action["confirmation_id"], action["payment_method"]),
                         ("reconcile", "meny-original", "vipps"))
        self.assertIn("MENY Vipps request was sent", action["reason"])

    def test_prepared_recovery_uses_child_confirmation_and_policy(self):
        action = self.action({"status": "awaiting_user_payment", "confirmation_id": "original",
                              "checkout_payment": {"method": "vipps"},
                              "recovery": {"status": "awaiting_confirmation", "confirmation_id": "child",
                                           "browser_review": {"payment_choice": {"method": "saved_card"}}}})
        self.assertEqual((action["action"], action["confirmation_id"]), ("confirm", "child"))
        self.assertEqual(action["payment_method"], "saved_card")
        self.assertIn("existing confirmation policy", action["reason"])
        self.assertNotIn("Vipps", action["reason"])
        self.assertEqual(workflow_status({"provider": "oda", "pending_checkout": {
            "status": "awaiting_user_payment", "confirmation_id": "original",
            "recovery": {"status": "awaiting_confirmation", "confirmation_id": "child", "browser_review": {
                "payment_choice": {"method": "saved_card"}}}}})["checkout_status"], "awaiting_confirmation")

    def test_expired_prepared_child_requests_fresh_review(self):
        action = self.action({"status": "awaiting_user_payment", "confirmation_id": "original",
                              "checkout_payment": {"method": "vipps"},
                              "recovery": {"status": "awaiting_confirmation", "confirmation_id": "expired-child",
                                           "expires_at": "2020-01-01T00:00:00+00:00",
                                           "browser_review": {"payment_choice": {"method": "saved_card"}}}})
        self.assertEqual((action["action"], action["recovery"]), ("prepare", True))
        self.assertNotIn("confirmation_id", action)
        self.assertEqual(action["checkout_payment"]["method"], "saved_card")
        self.assertIn("expired", action["reason"])

    def test_expired_legacy_saved_card_review_refreshes_same_order_without_dispatch(self):
        from test_payment_recovery import RecoveryTests

        flow = RecoveryTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        with flow.app.store.locked() as state:
            pending = state["pending_checkout"]
            pending.pop("vipps_request_status")
            pending.pop("unpaid_order_id")
            pending.pop("unpaid_order_binding_source")
            pending["status"] = "awaiting_user_payment"
        flow.browser.payment_state = "retry_available"
        first = flow.call("prepare", recovery=True, order_id="order-1",
                          checkout_payment={"method": "saved_card"})
        with flow.app.store.locked() as state:
            state["pending_checkout"]["recovery"]["expires_at"] = "2020-01-01T00:00:00+00:00"
        status = flow.app.handle({"operation": "status"})
        action = status["workflow"]["next_action"]
        self.assertEqual(status["workflow"]["checkout_status"], "awaiting_confirmation")
        self.assertEqual((action["action"], action["recovery"], action["order_id"], action["confirmation_id"]),
                         ("prepare", True, "order-1", "original"))
        self.assertEqual(action["checkout_payment"]["method"], "saved_card")
        self.assertNotIn("vipps_request_not_received", action)
        self.assertIn("currently confirms no Vipps request or manual payment", action["reason"])
        fresh = flow.app.handle({"operation": "checkout", "action": "prepare", "recovery": True,
                                 "order_id": action["order_id"], "confirmation_id": action["confirmation_id"],
                                 "vipps_request_not_received": True,
                                 "checkout_payment": action["checkout_payment"]})
        self.assertNotEqual(fresh["confirmation_id"], first["confirmation_id"])
        child = flow.app.store.read()["pending_checkout"]["recovery"]
        self.assertEqual(child["browser_review"]["payment_choice"], action["checkout_payment"])
        self.assertTrue(child["owner_reported_no_vipps_request"])
        self.assertEqual(child["original_confirmation_id"], "original")
        self.assertEqual(flow.browser.clicks, 0)

    def test_dispatched_recovery_reconciles_child_method(self):
        parent = {"status": "awaiting_user_payment", "confirmation_id": "original",
                  "checkout_payment": {"method": "vipps"}}
        saved_card = {**parent, "recovery": {"status": "uncertain", "confirmation_id": "card-child",
                                              "browser_review": {"payment_choice": {"method": "saved_card"}}}}
        action = self.action(saved_card)
        self.assertEqual((action["action"], action["confirmation_id"]), ("reconcile", "card-child"))
        self.assertEqual(action["payment_method"], "saved_card")
        self.assertNotIn("Vipps", action["reason"])
        vipps_child = {**parent, "recovery": {"status": "awaiting_user_payment", "confirmation_id": "vipps-child",
                                               "browser_review": {"payment_choice": {"method": "vipps"}},
                                               "vipps_request_status": "sent",
                                               "vipps_request_context": {"tab_id": "tab"},
                                               "payment_requested_at": "2026-09-23T12:00:00+00:00"}}
        action = self.action(vipps_child)
        self.assertEqual((action["action"], action["confirmation_id"]), ("reconcile", "vipps-child"))
        self.assertEqual(action["payment_method"], "vipps")
        self.assertIn("Approve that exact request", action["reason"])


if __name__ == "__main__":
    unittest.main()
