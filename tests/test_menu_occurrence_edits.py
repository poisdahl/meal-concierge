"""Exact menu occurrence edits through the real application boundary."""
from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from core import HouseholdError
import batch_planning as bp
import menu_planning as mp
from planner import saved_menu_minimum_evaluation
from product_planner import menu_requirements
from recipe_quantities import read_quantity
from service import Application
from tests import test_meal_concierge_planner as fixtures
from tests import test_meal_concierge_product_capacity as product_fixtures


class MenuOccurrenceEditsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WeeklyPlannerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app, self.store = self.fixture.app, self.fixture.store
        clock = patch.object(Application, "_household_today", return_value=date(2026, 9, 7))
        clock.start()
        self.addCleanup(clock.stop)
        with self.store.locked() as state:
            state["profile"]["meals"].update({"dinner_days": 3, "dishes": 3})
            state["profile"]["diet"]["leafy_green_days"] = [3, 3]
        candidates = self.fixture.save_candidates(3)
        planned = self.fixture.plan(self.fixture.request(candidates,
            dates=["2026-09-07", "2026-09-08", "2026-09-09"], selection_mode="agent"))
        self.menu = self.app.handle({"operation": "menu", "action": "save",
                                     "planner_ref": planned["save_ref"]})["menu"]
        self.salad = self.save_recipe("Spinach salad", "spinach-side", "spinat")

    def save_recipe(self, name, identity, ingredient):
        saved = self.app.handle({"operation": "recipes", "action": "save",
                                 "recipe": fixtures.recipe(name, identity, ingredient=ingredient),
                                 "idempotency_key": identity})["recipe"]
        return {"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}}

    def edit(self, menu, edits, key):
        request = {"operation": "menu", "action": "edit_slots", "menu_ref": mp.menu_ref(menu),
                   "idempotency_key": key, "edits": edits}
        return self.app.handle(request), request

    def amounts(self, menu):
        requirements, unresolved = menu_requirements(mp.shopping_menu(menu))
        return {row["item"]: read_quantity(row["quantity"], legacy_float=True)
                for row in requirements}, unresolved

    def test_one_preparation_links_three_dinners_and_counts_each_date_once(self):
        fact = {"source": "explicit", "assessment": "substantial", "ingredient_indices": [0],
                "basis": "The spinach is the leafy side, not a garnish."}
        result, request = self.edit(self.menu, [
            {"action": "add", "date": "2026-09-07", "meal_type": "side", "portions": 2,
             "reference": self.salad, "leafy_green": fact, "served_with": "dinner"},
            {"action": "add", "date": "2026-09-08", "portions": 2, "source_edit_index": 0, "served_with": "dinner"},
            {"action": "add", "date": "2026-09-09", "portions": 2, "source_edit_index": 0, "served_with": "dinner"},
        ], "salad-three-days")
        menu = result["menu"]
        self.assertEqual([s["date"] for s in menu["slots"] if s["meal_type"] == "side"],
                         ["2026-09-07", "2026-09-08", "2026-09-09"])
        self.assertEqual(len(menu["batches"]), 1)
        self.assertEqual(menu["batches"][0]["prepared_portions"], {"numerator": 6, "denominator": 1})
        self.assertEqual(self.amounts(menu)[0]["spinat"], 600)
        leafy = next(row["detail"] for row in saved_menu_minimum_evaluation(menu, self.store.read()["profile"])["results"]
                     if row["target"] == "leafy_green_days")
        self.assertEqual((leafy["counted_dinners"], leafy["unknown_dinners"]), (3, 0))
        self.assertTrue(self.app.handle(request)["idempotent"])
        with self.assertRaises(HouseholdError):
            self.edit(self.menu, [{"action": "remove", "slot_id": menu["slots"][-1]["slot_id"]}], "stale")
        side = next(s for s in menu["slots"] if s["meal_type"] == "side" and s["date"] == "2026-09-07")
        with self.assertRaisesRegex(HouseholdError, "linked servings"):
            self.edit(menu, [{"action": "remove", "slot_id": side["slot_id"]}], "source-only-remove")

    def test_independent_repeats_and_exact_side_replacement_after_successors(self):
        first, _ = self.edit(self.menu, [
            {"action": "add", "date": day, "meal_type": "side", "portions": 1, "reference": self.salad}
            for day in ("2026-09-07", "2026-09-08", "2026-09-09")], "three-preps")
        menu = first["menu"]
        self.assertEqual(self.amounts(menu)[0]["spinat"], 300)
        unrelated = self.save_recipe("Berry dessert", "berry-dessert", "berries")
        second, _ = self.edit(menu, [{"action": "add", "date": "2026-09-08", "meal_type": "dessert",
                                      "portions": 1, "reference": unrelated}], "dessert")
        menu = second["menu"]
        replacement = self.save_recipe("New side", "new-side", "agurk")
        target = next(s for s in menu["slots"] if s["meal_type"] == "side" and s["date"] == "2026-09-08")
        third, _ = self.edit(menu, [{"action": "replace", "slot_id": target["slot_id"],
                                     "reference": replacement}], "replace-middle")
        edited = third["menu"]
        amounts, unresolved = self.amounts(edited)
        self.assertEqual(unresolved, [])
        self.assertEqual(amounts["spinat"], 200)
        self.assertEqual(amounts["agurk"], 100)
        self.assertEqual(amounts["berries"], 100)
        self.assertEqual(len([s for s in edited["slots"] if s["meal_type"] == "dinner"]), 3)
        self.assertEqual(len(self.store.read()["menu_planning"]["history"]), 3)
        self.assertEqual(self.store.read()["menu_planning"]["history"][mp.lock_key(menu)], menu)
        provider = product_fixtures.Retailer()
        self.app.provider_client = provider
        prepared = self.app.handle({"operation": "products", "action": "prepare",
                                    "menu_ref": mp.menu_ref(edited)})
        self.assertIn(prepared["product_plan"]["status"], {"needs_input", "prepared"})
        self.assertEqual({row["item"] for row in prepared["product_plan"]["requirements"]},
                         set(amounts))
        self.assertEqual(provider.quantities, {})
        with self.assertRaisesRegex(HouseholdError, "idempotency"):
            self.edit(edited, [{"action": "remove", "slot_id": target["slot_id"]}], "replace-middle")

    def test_historical_repeat_keeps_future_shopping_and_move_remove_are_exact(self):
        result, _ = self.edit(self.menu, [
            {"action": "add", "date": day, "meal_type": "side", "portions": 1, "reference": self.salad}
            for day in ("2026-09-07", "2026-09-08", "2026-09-09")], "repeat-historical")
        menu = result["menu"]
        first = next(s for s in menu["slots"] if s["meal_type"] == "side" and s["date"] == "2026-09-07")
        self.app.handle({"operation": "recipes", "action": "mark_cooked", "menu_id": menu["menu_id"],
                         "expected_revision": menu["revision"], "slot_id": first["slot_id"]})
        middle = next(s for s in menu["slots"] if s["meal_type"] == "side" and s["date"] == "2026-09-08")
        moved, _ = self.edit(menu, [{"action": "move", "slot_id": middle["slot_id"],
                                     "date": "2026-09-10"}], "move-middle")
        menu = moved["menu"]
        self.assertEqual(self.amounts(menu)[0]["spinat"], 200)
        self.assertIn(first["slot_id"], menu["historical_slot_ids"])
        last = next(s for s in menu["slots"] if s["meal_type"] == "side" and s["date"] == "2026-09-09")
        removed, _ = self.edit(menu, [{"action": "remove", "slot_id": last["slot_id"]}], "remove-last")
        self.assertEqual(self.amounts(removed["menu"])[0]["spinat"], 100)
        self.assertEqual(next(s for s in removed["menu"]["slots"] if s["slot_id"] == first["slot_id"]), first)

    def test_two_sides_count_one_dinner_and_lunch_side_is_not_dinner(self):
        fact = {"source": "explicit", "assessment": "substantial", "ingredient_indices": [0],
                "basis": "Spinach leaves are eaten as a side."}
        result, _ = self.edit(self.menu, [
            {"action": "add", "date": "2026-09-07", "meal_type": "side", "portions": 2,
             "reference": self.salad, "leafy_green": fact, "served_with": "dinner"},
            {"action": "add", "date": "2026-09-07", "meal_type": "side", "portions": 2,
             "reference": self.salad, "leafy_green": fact, "served_with": "dinner"},
            {"action": "add", "date": "2026-09-08", "meal_type": "side", "portions": 2,
             "reference": self.salad, "leafy_green": fact, "served_with": "lunch"},
        ], "two-sides-lunch")
        detail = next(row["detail"] for row in saved_menu_minimum_evaluation(result["menu"], self.store.read()["profile"])["results"]
                      if row["target"] == "leafy_green_days")
        self.assertEqual(detail["counted_dinners"], 1)
        self.assertEqual(len(detail["dinner_assessments"]), 3)

    def test_partial_dinner_side_is_unknown_and_second_dinner_rejected(self):
        fact = {"source": "explicit", "assessment": "substantial", "ingredient_indices": [0],
                "basis": "The spinach is served as a side."}
        result, _ = self.edit(self.menu, [{"action": "add", "date": "2026-09-07", "meal_type": "side",
                                          "portions": 1, "reference": self.salad, "leafy_green": fact,
                                          "served_with": "dinner"}], "partial-side")
        detail = next(row["detail"] for row in saved_menu_minimum_evaluation(result["menu"], self.store.read()["profile"])["results"]
                      if row["target"] == "leafy_green_days")
        self.assertEqual(detail["counted_dinners"], 0)
        self.assertTrue(detail["dinner_assessments"][0]["partial_side_coverage"])
        with self.assertRaisesRegex(HouseholdError, "already has a dinner"):
            self.edit(result["menu"], [{"action": "add", "date": "2026-09-07", "meal_type": "dinner",
                                        "portions": 2, "reference": self.salad}], "second-dinner")

    def test_invalid_action_and_pending_cart_leave_state_unchanged(self):
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(HouseholdError, "edit action"):
            self.edit(self.menu, [{"action": []}], "bad-action")
        self.assertEqual(self.store.path.read_bytes(), before)
        with self.store.locked() as state:
            state["pending_cart_change"] = {"uncertain": True}
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(HouseholdError, "pending cart change"):
            self.edit(self.menu, [{"action": "add", "date": "2026-09-07", "meal_type": "side",
                                   "portions": 1, "reference": self.salad}], "pending-cart")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_shopping_projection_keeps_salad_source_positions(self):
        menu = mp.canonical(self.menu)
        import json
        menu = json.loads(menu)
        mp.bind_preparations(menu)
        recipe = menu["dishes"].pop(0)
        recipe["shopping_requirements"][0]["pantry"] = True
        menu["salads"].append(recipe)
        projected = mp.shopping_menu(menu)
        self.assertEqual(len(projected["salads"]), 1)
        self.assertEqual(projected["salads"][0]["preparation_slot_id"], recipe["preparation_slot_id"])
        source = {"collection": "salads", "recipe_index": 0, "ingredient_index": 0}
        requirements, unresolved = menu_requirements(projected, ingredient_decisions=[{"source": source, "action": "include"}])
        self.assertEqual(unresolved, [])
        self.assertTrue(any(row["item"] == recipe["shopping_requirements"][0]["item"] for row in requirements))

    def test_removing_dinner_updates_planning_scope_and_completeness(self):
        target = next(slot for slot in self.menu["slots"] if slot["date"] == "2026-09-09")
        result, _ = self.edit(self.menu, [{"action": "remove", "slot_id": target["slot_id"]}], "remove-dinner")
        self.assertEqual(result["menu"]["planning_scope"]["dates"], ["2026-09-07", "2026-09-08"])
        self.assertFalse(result["menu"]["weekly_plan_complete"])

    def test_confirmed_unrecorded_side_batch_can_be_changed(self):
        added, _ = self.edit(self.menu, [
            {"action": "add", "date": day, "meal_type": "side", "portions": 2,
             "reference": self.salad, "served_with": "dinner"}
            for day in ("2026-09-07", "2026-09-08")], "two-confirmed-sides")
        menu = added["menu"]
        source, target = [s for s in menu["slots"] if s["meal_type"] == "side"]
        spec = {"source_slot_id": source["slot_id"], "source_snapshot_digest": source["snapshot_digest"],
                "prepared_portions": "4", "consumed_at_source": "2",
                "suitability": {"source": "current_user", "value": "suitable"},
                "storage": {"source": "current_user", "method": "refrigerated", "max_interval_days": 2},
                "leftovers": [{"slot_id": target["slot_id"], "portions": "2"}]}
        prepared = self.app.handle({"operation": "menu", "action": "batch_prepare",
                                    "menu_ref": mp.menu_ref(menu), "batch_spec": spec})["batch_plan"]
        self.assertEqual(prepared["status"], "prepared", prepared)
        batched = self.app.handle({"operation": "menu", "action": "batch_apply", "batch_plan": prepared,
                                   "batch_confirmation": {"batch_digest": prepared["batch_digest"],
                                                          "statement": bp.CONFIRMATION_STATEMENT}})["menu"]
        self.assertEqual(len(batched["batches"]), 1)
        leftover = next(s for s in batched["slots"] if s.get("kind") == "leftover")
        self.assertEqual(leftover["served_with"], "dinner")
        expanded, _ = self.edit(batched, [{"action": "add", "date": "2026-09-09", "portions": 2,
                                          "source_slot_id": source["slot_id"], "served_with": "dinner"}], "expand-batch")
        menu = expanded["menu"]
        self.assertEqual(menu["batches"][0]["prepared_portions"], {"numerator": 6, "denominator": 1})
        self.assertEqual(menu["batches"][0]["storage"], {"basis": "unknown"})
        self.assertNotIn("confirmation", menu["batches"][0])
        self.app.handle({"operation": "recipes", "action": "mark_cooked", "menu_id": menu["menu_id"],
                         "expected_revision": menu["revision"], "slot_id": source["slot_id"],
                         "actual_batch": {"prepared_portions": "6", "consumed_at_source": "2"}})
        edited, _ = self.edit(menu, [{"action": "remove", "slot_id": leftover["slot_id"]}], "remove-future-serving")
        self.assertEqual(edited["menu"]["batches"][0]["unallocated_portions"], {"numerator": 2, "denominator": 1})
        self.assertNotIn("spinat", self.amounts(edited["menu"])[0])

    def test_one_not_cooked_repeat_does_not_cancel_another_planned_occurrence(self):
        added, _ = self.edit(self.menu, [
            {"action": "add", "date": day, "meal_type": "side", "portions": 1,
             "reference": self.salad} for day in ("2026-09-07", "2026-09-08")], "two-usage-sides")
        menu = added["menu"]
        sides = [s for s in menu["slots"] if s["meal_type"] == "side"]
        self.app.handle({"operation": "recipes", "action": "mark_not_cooked", "menu_id": menu["menu_id"],
                         "expected_revision": menu["revision"], "slot_id": sides[0]["slot_id"]})
        usage = self.app._usage_summary(self.store.read(), sides[0]["recipe_key"], menu["week"])
        self.assertFalse(usage["eligible"])
        self.app.handle({"operation": "recipes", "action": "mark_not_cooked", "menu_id": menu["menu_id"],
                         "expected_revision": menu["revision"], "slot_id": sides[1]["slot_id"]})
        usage = self.app._usage_summary(self.store.read(), sides[0]["recipe_key"], menu["week"])
        self.assertTrue(usage["eligible"])

    def test_saved_dietary_facts_follow_exact_reference_and_date(self):
        recipes = [dict(self.menu["dishes"][0], recipe_key="same-family",
                        preparation_slot_id=f"slot-{index}") for index in (1, 2)]
        slots = [{"slot_id": f"slot-{index}", "date": f"2026-09-0{index + 6}",
                  "meal_type": "dinner", "portions": 2, "recipe_key": "same-family",
                  "reference": {"recipe_ref": {"id": "same", "revision": index}},
                  "snapshot_digest": mp.recipe_snapshot_digest(recipe)}
                 for index, recipe in zip((1, 2), recipes)]
        facts = [{"values": ["fish"], "vegetable_types": [], "complete": True},
                 {"values": [], "vegetable_types": [], "complete": True}]
        menu = {"dishes": recipes, "salads": [], "slots": slots,
                "planner_selection": {"selection": {"slots": [
                    {"date": slot["date"], "recipe_key": slot["recipe_key"],
                     "reference": slot["reference"], "dietary_facets": facet}
                    for slot, facet in zip(slots, facts)]}},
                "planning_scope": {"selection_mode": "agent", "strict_targets": ["minimum_fish_portions"]}}
        profile = {"meals": {"dinner_days": 2}, "diet": {"minimum_fish_portions": 1}}
        assessment = saved_menu_minimum_evaluation(menu, profile)
        self.assertEqual(assessment["status"], "pass")
        self.assertEqual(assessment["results"][0]["detail"]["observed"], 1)

    def test_two_same_recipe_preparations_scale_independently(self):
        result, _ = self.edit(self.menu, [
            {"action": "add", "date": "2026-09-07", "meal_type": "side", "portions": 1,
             "reference": self.salad},
            {"action": "add", "date": "2026-09-08", "portions": 1, "source_edit_index": 0},
            {"action": "add", "date": "2026-09-09", "meal_type": "side", "portions": 1,
             "reference": self.salad},
            {"action": "add", "date": "2026-09-10", "portions": 1, "source_edit_index": 2},
        ], "two-same-batches")
        menu = result["menu"]
        self.assertEqual(len(menu["batches"]), 2)
        self.assertEqual(self.amounts(menu)[0]["spinat"], 400)
        source_key = mp.slot_by_id(menu, menu["batches"][0]["source_slot_id"])["recipe_key"]
        self.assertEqual(len({r["preparation_slot_id"] for r in menu["dishes"]
                              if r["recipe_key"] == source_key}), 2)

    def test_exact_edit_rejects_current_dietary_ban_without_state_change(self):
        with self.store.locked() as state:
            state["profile"]["diet"]["rules"] = [{"kind": "never_buy", "term": "spinat"}]
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(HouseholdError, "ingredient rules"):
            self.edit(self.menu, [{"action": "add", "date": "2026-09-07", "meal_type": "side",
                                   "portions": 1, "reference": self.salad}], "banned-side")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_agent_selection_can_intentionally_repeat_historical_recipe(self):
        reference = self.menu["slots"][0]["reference"]
        planned = self.fixture.plan(self.fixture.request([reference], dates=["2026-09-10"],
                                                       selection_mode="agent"))
        self.assertEqual(planned["status"], "planned")
        reasons = planned["candidate_evaluations"][0]["hard_constraints"]["reasons"]
        self.assertIn("advisory", [row["status"] for row in reasons if row["code"] == "cooldown"])

    def test_ordered_menu_cannot_be_edited(self):
        with self.store.locked() as state:
            state["menu"]["phase"] = "ordered"
            state["recipe_usage"][self.menu["menu_id"]]["status"] = "ordered"
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(HouseholdError, "ordered menu slots"):
            self.edit(self.menu, [{"action": "add", "date": "2026-09-09", "meal_type": "side",
                                   "portions": 1, "reference": self.salad}], "ordered-edit")
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
