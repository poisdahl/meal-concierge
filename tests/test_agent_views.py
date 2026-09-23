"""Concise ordinary views preserve authority and real outcomes."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_views import project_agent_result


class AgentViewTests(unittest.TestCase):
    def test_status_keeps_next_action_and_incomplete_assessment(self):
        result = {"household": "Synthetic", "menu_phase": "draft", "pending_checkout_status": "uncertain",
                  "schedule": {"large_internal_state": "x" * 100_000},
                  "workflow": {"next_action": {"operation": "checkout", "action": "reconcile"},
                               "menu": {"ready": False, "status": "needs_input", "issue_count": 1,
                                        "issues": [{"code": "known_preference_deviation"}],
                                        "dietary_assessments": [{"kind": "preference", "term": "dairy",
                                                                 "condition": "preference_deviation", "blocked": False,
                                                                 "evidence": {"internal": "x" * 100_000}}]}}}
        view = project_agent_result("status", None, result)
        self.assertEqual(view["workflow"]["next_action"]["action"], "reconcile")
        self.assertFalse(view["workflow"]["menu"]["ready"])
        self.assertEqual(view["workflow"]["menu"]["dietary_deviations_or_unknown_count"], 1)
        self.assertNotIn("schedule", view)
        self.assertLess(len(json.dumps(view)), 45_000)

    def test_menu_exact_reference_slots_and_assessment_page(self):
        reference = {"menu_id": "menu_synthetic", "revision": 2, "digest": "d" * 64}
        slots = [{"slot_id": f"slot_{index}", "date": f"2026-09-{index + 1:02d}",
                  "meal_type": "dinner", "portions": 2, "recipe_key": f"recipe:{index}",
                  "reference": {"recipe_ref": {"id": f"recipe_{index}", "revision": 1}}}
                 for index in range(15)]
        menu = {**reference, "week": "2026-W40", "phase": "draft", "slots": slots,
                "dishes": [{"recipe_key": f"recipe:{index}", "name": f"Meal {index}"}
                           for index in range(15)],
                "planner_selection": {"selection": {"slots": [{"date": "2026-09-01",
                    "reason_contributions": [{"code": "dietary_preference", "detail": {
                        "kind": "preference", "term": "dairy", "condition": "preference_deviation"}}]}]}}}
        assessment = {"menu_ref": reference, "ready": False, "status": "needs_input", "issue_count": 1,
                      "issues": [{"code": "portion_mismatch"}], "dietary_assessments": []}
        view = project_agent_result("menu", "get", {"menu": menu, "assessment": assessment}, offset=10, limit=5)
        self.assertEqual(view["menu_ref"], reference)
        self.assertEqual(view["menu"]["menu_id"], reference["menu_id"])
        self.assertEqual(view["slots"]["total"], 15)
        self.assertEqual(view["slots"]["items"][0]["slot_id"], "slot_10")
        self.assertFalse(view["assessment"]["ready"])
        self.assertEqual(view["preference_deviations"]["total"], 1)
        applied = project_agent_result("menu", "add_slot", {"menu": menu, "menu_ref": reference,
                          "added_slot": slots[-1], "idempotent": True})
        self.assertTrue(applied["idempotent"])
        self.assertEqual(applied["added_slot"], slots[-1])

    def test_planner_handoff_is_compacted_before_service_transport(self):
        result = {"plan": {"status": "planned", "save_ref": "save_exact",
                           "selection": {"slots": [{"date": "2026-09-28", "name": "Soup",
                                                   "reference": {"recipe_ref": {"id": "soup", "revision": 1}}}]},
                           "save_handoff": {"large_internal_evidence": "x" * 100_000}}}
        view = project_agent_result("menu", "plan", result)
        self.assertEqual(view["projection"], "agent")
        self.assertEqual(view["plan"]["save_ref"], "save_exact")
        self.assertNotIn("save_handoff", view["plan"])
        self.assertLess(len(json.dumps(view)), 45_000)

    def test_cart_lines_are_normalized_and_write_outcome_survives_missing_total(self):
        cart = {"items": [{"product_id": 10, "name": "Synthetic beans", "quantity": 2, "price": 4.5}],
                "subtotal": 9.0, "provider_session": "not-for-agent"}
        digest = "a" * 64
        view = project_agent_result("cart", "get", {**cart, "cart_digest": digest})
        self.assertEqual(view["cart_digest"], digest)
        self.assertEqual(view["lines"]["items"][0]["product_id"], "10")
        self.assertEqual(view["total"], 9.0)
        self.assertNotIn("provider_session", view)
        mutation = project_agent_result("cart", "clear", {"cleared": True, "idempotent": False,
                                        "cart": {"items": []}})
        self.assertTrue(mutation["cleared"])
        self.assertFalse(mutation["idempotent"])
        self.assertEqual(mutation["cart_normalization"], "unavailable")
        self.assertNotIn("cart_digest", mutation)
        uncertain = project_agent_result("cart", "sync", {
            "synced": None, "outcome_unknown": True, "cart_write_pending": True,
            "cart_reconciliation_required": True, "reason": "verification_unavailable",
            "cart_plan": {"provider": "oda", "status": "needs_input", "cart_digest": "b" * 64,
                          "approved": False, "items": [{"product_id": "10", "name": "Beans",
                                                         "extra_quantity": 1, "missing_quantity": 2}]}})
        self.assertTrue(uncertain["outcome_unknown"])
        self.assertEqual(uncertain["cart_plan"]["cart_digest"], "b" * 64)
        self.assertEqual(uncertain["cart_plan"]["items"]["items"][0]["missing_quantity"], 2)

    def test_configuration_gate_is_not_rewritten_as_success(self):
        result = {"status": "configuration_required", "current": {"provider": "oda"},
                  "question": "Choose the configured store."}
        self.assertEqual(project_agent_result("menu", "save", result), {"projection": "agent", **result})

    def test_provider_rejection_keeps_business_outcome(self):
        rejected = {"ok": False, "status": "rejected", "error": "provider unavailable",
                    "retryable": False}
        view = project_agent_result("cart", "get", rejected)
        self.assertEqual(view, {"projection": "agent", **rejected})
        self.assertNotIn("cart_normalization", view)

    def test_orders_do_not_turn_list_membership_into_active_or_paid(self):
        rows = [{"orderNumber": "cancelled-1", "status": "cancelled", "paymentStatus": "paid",
                 "deliveryDate": "2026-09-30"},
                {"orderNumber": "unknown-2", "status": "confirmed"}]
        listed = project_agent_result("orders", "list", {"orders": rows})["orders"]["items"]
        self.assertEqual(project_agent_result("orders", None, {"orders": rows})["action"], "list")
        self.assertTrue(listed[0]["cancelled"])
        self.assertEqual(listed[0]["payment_status"], "unknown")
        self.assertEqual(listed[1]["tracking_status"], "not_read")
        self.assertTrue(listed[1]["exact_read_required"])
        not_cancelled = project_agent_result("orders", "get", {
            "order": {"id": "still-open", "status": "not_cancelled"},
            "tracking": {"status": "not_cancelled"}})["order"]
        self.assertFalse(not_cancelled["cancelled"])
        self.assertEqual(not_cancelled["normalized_order_id"], "still-open")
        exact = project_agent_result("orders", "get", {
            "order": rows[0], "tracking": {"order_id": "cancelled-1", "status": "cancelled"}})
        self.assertTrue(exact["order"]["cancelled"])
        self.assertEqual(exact["order"]["tracking_status"], "cancelled")

    def test_exact_order_goods_are_reviewable_in_pages(self):
        order = {"orderNumber": "synthetic-123", "status": "confirmed", "currency": "NOK",
                 "grossAmount": "47.85", "products": [
                     {"product": {"id": 4694, "name": "Synthetic pasta"},
                      "quantity": 2, "totalGrossAmount": "31.90"},
                     {"product": {"id": 8750, "name": "Synthetic beans"},
                      "quantity": 1, "totalGrossAmount": "15.95"}]}
        first = project_agent_result("orders", "get", {"order": order}, limit=1)
        self.assertEqual(first["order"]["normalized_order_id"], "synthetic-123")
        self.assertEqual(first["order"]["grossAmount"], "47.85")
        self.assertEqual(first["order_items"]["total"], 2)
        self.assertEqual(first["order_items"]["next_offset"], 1)
        self.assertEqual(first["order_items"]["items"][0], {
            "index": 0, "product_id": 4694, "name": "Synthetic pasta",
            "quantity": 2, "price": "31.90"})
        second = project_agent_result("orders", "get", {"order": order}, offset=1, limit=1)
        self.assertEqual(second["order_items"]["items"][0]["name"], "Synthetic beans")
        self.assertNotIn("next_offset", second["order_items"])

    def test_recipe_pages_retain_original_identity_indices_and_estimates(self):
        ingredients = [{"item": f"ingredient {index}", "quantity": {"numerator": index + 1, "denominator": 1},
                        "unit": "g", "original_text": f"source {index}",
                        "evidence": {"quantity": {"basis": "estimate", "assumptions": "measured"}}}
                       for index in range(22)]
        recipe = {"name": "Synthetic soup", "schema_version": 2, "portions": 2,
                  "scaled_from_portions": 1, "id": "synthetic", "revision": 3,
                  "recipe_digest": "d" * 64, "source_provider": "oda",
                  "source": {"kind": "oda", "url": "https://oda.com/no/recipes/synthetic/",
                             "relationship": "original"}, "rights": {"storage": "full"},
                  "ingredients": ingredients, "steps": ["Simmer.", "Serve."]}
        result = {"recipe": recipe, "discovery_ref": {"id": "frozen"}}
        view = project_agent_result("recipes", "get", result, offset=10, limit=10, section="ingredients")
        self.assertEqual(view["original_portions"], 1)
        self.assertEqual(view["display_portions"], 2)
        self.assertEqual(view["recipe_digest"], "d" * 64)
        self.assertEqual(view["recipe_ref"], {"id": "synthetic", "revision": 3})
        self.assertEqual(view["source_schema_version"], 2)
        self.assertEqual(view["ingredients"]["items"][0]["index"], 10)
        self.assertEqual(view["ingredients"]["next_offset"], 20)
        self.assertTrue(view["ingredients"]["items"][0]["estimate_or_unknown"])
        self.assertNotIn("steps", view)
        self.assertLess(len(json.dumps(view)), 45_000)

    def test_recipe_summary_pages_large_normalized_fields_before_transport(self):
        recipe = {"name": "Long synthetic recipe", "schema_version": 2, "portions": 2,
                  "recipe_digest": "c" * 64, "notes": "n" * 4_000,
                  "ingredients": [{"item": f"item {index}", "quantity": 1, "unit": "g",
                                   "evidence": {"quantity": {"basis": "estimate", "assumptions": "a" * 900},
                                                "unit": {"basis": "estimate", "assumptions": "b" * 900}}}
                                  for index in range(20)],
                  "steps": ["Cook " + "s" * 4_000 for _ in range(20)]}
        view = project_agent_result("recipes", "get", {"recipe": recipe}, limit=20)
        self.assertIn("next_offset", view["ingredients"])
        self.assertIn("next_offset", view["steps"])
        self.assertEqual(view["notes"], "n" * 4_000)
        self.assertLess(len(json.dumps(view, ensure_ascii=False)), 45_000)

    def test_recipe_times_excludes_unbounded_provider_metadata(self):
        recipe = {"name": "Synthetic soup", "schema_version": 2, "portions": 2,
                  "times": {"active_minutes": 20, "provider_metadata": "x" * 60_000},
                  "ingredients": [{"item": "synthetic beans", "quantity": 1, "unit": "can"}],
                  "steps": ["Warm the beans."]}
        view = project_agent_result("recipes", "get", {"recipe": recipe}, limit=1)
        self.assertEqual(view["times"], {"active_minutes": 20, "other_fields_omitted": 1})
        self.assertLess(len(json.dumps(view, ensure_ascii=False)), 45_000)


if __name__ == "__main__":
    unittest.main()
