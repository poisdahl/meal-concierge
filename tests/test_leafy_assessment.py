"""Leafy dinner assessments through exact planning and saved menu readback."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core import StateStore
from agent_views import project_agent_result
import menu_planning as mp
import planner
from service import Application
from tests.test_meal_concierge_planner import CONFIG, NoProviderCalls, recipe


def leafy_fact(assessment, indices=(), basis="Culinary assessment of this dinner"):
    return {"source": "explicit", "assessment": assessment,
            "ingredient_indices": list(indices), "basis": basis}


class LeafyAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name), CONFIG)
        self.provider = NoProviderCalls()
        self.app = Application(self.store, self.provider, object())
        with self.store.locked() as state:
            state["setup"]["status"] = "complete"
            state["profile"]["meals"].update({"dinner_days": 1, "dishes": 1})
            state["profile"]["diet"]["leafy_green_days"] = [1, 1]
            for target in planner.SAVED_MINIMUM_TARGETS:
                state["profile"]["diet"][target] = 0

    def tearDown(self):
        self.temp.cleanup()

    def save_recipe(self, name, identity, ingredient, unit="g"):
        saved = self.app.handle({"operation": "recipes", "action": "save",
                                 "recipe": recipe(name, identity, ingredient=ingredient, unit=unit),
                                 "idempotency_key": identity})["recipe"]
        return {"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}}

    def plan(self, candidates, dates=None, strict=()):
        return self.app.handle({"operation": "menu", "action": "plan", "planner_input": {
            "week": "2026-W37", "dates": dates or ["2026-09-07"], "portions": 4,
            "candidates": candidates, "selection_mode": "agent", "strict_targets": list(strict),
        }})["plan"]

    def test_tatsoi_plan_save_readback_and_changed_snapshot(self):
        candidate = self.save_recipe("Tatsoi dinner", "tatsoi-dinner", "tatsoi")
        candidate["facts"] = {"leafy_green": leafy_fact("substantial", [0],
            "Tatsoi is a vegetable component of this dinner, not a garnish")}
        plan = self.plan([candidate], strict=["leafy_green_days"])
        self.assertEqual(plan["status"], "planned")
        evidence = plan["selection"]["strict_targets"]["results"][0]["detail"]["dinner_assessments"][0]
        self.assertEqual(evidence["source"], "agent_assessment")
        self.assertEqual(evidence["quantity_evidence"], "calculated_from_listed_recipe")
        self.assertEqual(evidence["listed_grams_per_serving"], 100)
        visible = project_agent_result("menu", "plan", {"plan": plan}, offset=0, limit=10, section="summary")
        reasons = visible["plan"]["selection"]["slots"][0]["reason_contributions"]
        leafy = next(reason for reason in reasons if reason["code"] == "diet:leafy_green_dinner")
        self.assertEqual(leafy["detail"]["source"], "agent_assessment")
        self.assertEqual(leafy["detail"]["listed_grams_per_serving"], 100)
        saved = self.app.handle({"operation": "menu", "action": "save",
                                 "planner_ref": plan["save_ref"]})["menu"]
        self.assertEqual(saved["dishes"][0]["portions"], 4)
        self.assertEqual(saved["slots"][0]["snapshot_digest"], mp.digest(saved["dishes"][0]))
        observed = planner.saved_menu_minimum_evaluation(saved, self.store.read()["profile"])
        self.assertEqual(observed["enforced_status"], "pass")
        self.assertEqual(observed["results"][0]["detail"]["dinner_assessments"][0]["listed_grams_per_serving"], 100)
        for action in ("get", "assess"):
            readback = self.app.handle({"operation": "menu", "action": action})
            visible = project_agent_result("menu", action, readback,
                                           offset=0, limit=10, section="summary")
            leafy = visible["minimum_evaluation"]["leafy_green_days"]
            self.assertEqual(leafy["status"], "pass")
            self.assertEqual(leafy["detail"]["dinner_assessments"][0]["source"], "agent_assessment")
            self.assertEqual(leafy["detail"]["dinner_assessments"][0]["listed_grams_per_serving"], 100)
            self.assertEqual(leafy["detail"]["dinner_assessments"][0]["quantity_evidence"],
                             "calculated_from_listed_recipe")

        changed = deepcopy(saved)
        changed["dishes"][0]["ingredients"][0]["quantity"] = 1
        stale = planner.saved_menu_minimum_evaluation(changed, self.store.read()["profile"])
        self.assertEqual(stale["results"][0]["status"], "unknown")
        self.assertEqual(stale["results"][0]["detail"]["dinner_assessments"][0]["detail"],
                         "saved_assessment_missing_or_stale")
        with self.store.locked() as state:
            state["menu"]["dishes"][0]["ingredients"][0]["quantity"] = 1
        for action in ("get", "assess"):
            readback = self.app.handle({"operation": "menu", "action": action})
            visible = project_agent_result("menu", action, readback,
                                           offset=0, limit=10, section="summary")
            leafy = visible["minimum_evaluation"]["leafy_green_days"]
            self.assertEqual(leafy["status"], "unknown")
            self.assertEqual(leafy["detail"]["dinner_assessments"][0]["detail"],
                             "saved_assessment_missing_or_stale")

    def test_no_fact_and_unsupported_mass_cannot_satisfy_strict_target(self):
        mixed = self.save_recipe("Spinach pasta", "spinach-pasta", "spinach pasta")
        unknown = self.plan([mixed], strict=["leafy_green_days"])
        self.assertEqual(unknown["status"], "needs_input")
        self.assertEqual(unknown["issues"][0]["target"], "leafy_green_days")

        tatsoi = self.save_recipe("Tatsoi heads", "tatsoi-heads", "tatsoi", unit="stk")
        tatsoi["facts"] = {"leafy_green": leafy_fact("substantial", [0])}
        unsupported = self.plan([tatsoi], strict=["leafy_green_days"])
        self.assertEqual(unsupported["status"], "needs_input")
        self.assertEqual(unsupported["issues"][0]["detail"]["dinner_assessments"][0]["quantity_evidence"],
                         "mass_unit_unavailable")
        visible = project_agent_result("menu", "plan", {"plan": unsupported},
                                       offset=0, limit=10, section="summary")
        repair = visible["plan"]["issues"][0]["detail"]["dinner_assessments"][0]
        self.assertEqual(repair["source"], "agent_assessment")
        self.assertEqual(repair["ingredient_indices"], [0])
        self.assertEqual(repair["quantity_evidence"], "mass_unit_unavailable")
        self.assertIsNone(repair["counts"])

    def test_intact_old_slot_uses_labeled_legacy_hint_but_missing_new_fact_does_not(self):
        spinach = self.save_recipe("Spinach dinner", "old-spinach", "spinat")
        spinach["facts"] = {"leafy_green": leafy_fact("substantial", [0])}
        plan = self.plan([spinach])
        saved = self.app.handle({"operation": "menu", "action": "save",
                                 "planner_ref": plan["save_ref"]})["menu"]
        missing_new = deepcopy(saved)
        missing_new["slots"][0].pop("leafy_green")
        self.assertEqual(planner.saved_menu_minimum_evaluation(
            missing_new, self.store.read()["profile"])["status"], "unknown")
        old = deepcopy(missing_new)
        old["planner_selection"]["planner_version"] = "weekly-menu-v5"
        old_assessment = planner.saved_menu_minimum_evaluation(old, self.store.read()["profile"])
        self.assertEqual(old_assessment["status"], "pass")
        self.assertEqual(old_assessment["results"][0]["detail"]["dinner_assessments"][0]["source"],
                         "legacy_heuristic")

    def test_recurring_agent_menu_retains_advisory_policy_and_leafy_evidence(self):
        with self.store.locked() as state:
            state["profile"]["meals"].update({
                "dinner_days": 2, "dishes": 1, "batch_dishes": 1, "meal_mode": "batch",
                "cook_days": ["Monday"], "eat_days": ["Monday", "Tuesday"],
                "prepared_portion_range": [8, 8], "recurring_batch_accepted": True,
                "portions": 4,
            })
        tatsoi = self.save_recipe("Batch tatsoi", "batch-tatsoi", "tatsoi")
        tatsoi["facts"] = {"leafy_green": leafy_fact("substantial", [0])}
        plan = self.plan([tatsoi], dates=["2026-09-07", "2026-09-08"])
        self.assertEqual(plan["status"], "planned")
        saved = self.app.handle({"operation": "menu", "action": "save",
                                 "planner_ref": plan["save_ref"]})["menu"]
        self.assertEqual(saved["planning_scope"]["selection_mode"], "agent")
        self.assertEqual(saved["planning_scope"]["strict_targets"], [])
        self.assertEqual(len(saved["slots"]), 2)
        self.assertEqual(saved["slots"][1]["leafy_green"], saved["slots"][0]["leafy_green"])
        observed = planner.saved_menu_minimum_evaluation(saved, self.store.read()["profile"])
        self.assertEqual(observed["results"][0]["detail"]["counted_dinners"], 2)
        self.assertEqual(observed["status"], "fail")
        self.assertEqual(observed["enforced_status"], "pass")

    def test_replan_retains_carried_fact_and_does_not_reuse_replaced_fact(self):
        with self.store.locked() as state:
            state["profile"]["meals"].update({"dinner_days": 2, "dishes": 2})
        tatsoi = self.save_recipe("Tatsoi", "tatsoi-source", "tatsoi")
        tatsoi["facts"] = {"leafy_green": leafy_fact("substantial", [0])}
        carrot = self.save_recipe("Carrot", "carrot-source", "gulrot")
        carrot["facts"] = {"leafy_green": leafy_fact("does_not_count", [], "Only carrot")}
        frozen_time = datetime(2026, 9, 7, 8, tzinfo=timezone.utc)
        with mock.patch("service.now", return_value=frozen_time):
            plan = self.plan([tatsoi, carrot], dates=["2026-09-07", "2026-09-08"])
            self.assertEqual(plan["status"], "planned")
            current = self.app.handle({"operation": "menu", "action": "save",
                                       "planner_ref": plan["save_ref"]})["menu"]
            self.assertEqual(planner.saved_menu_minimum_evaluation(current, self.store.read()["profile"])["status"], "pass")
            replacement = self.save_recipe("New carrot", "new-carrot", "gulrot")
            prepared = self.app.handle({"operation": "menu", "action": "replan_prepare",
                                        "menu_ref": mp.menu_ref(current), "remaining_dates": ["2026-09-08"],
                                        "planner_input": {"selection_mode": "agent", "candidates": [replacement]}})
            self.assertEqual(prepared["replan"]["status"], "prepared", prepared)
            successor = self.app.handle({"operation": "menu", **prepared["apply_arguments"]})["menu"]
        self.assertEqual(successor["slots"][0]["leafy_green"], current["slots"][0]["leafy_green"])
        self.assertEqual(successor["slots"][0]["snapshot_digest"], current["slots"][0]["snapshot_digest"])
        self.assertEqual(successor["slots"][1]["leafy_green"]["source"], "heuristic")
        assessed = planner.saved_menu_minimum_evaluation(successor, self.store.read()["profile"])
        self.assertEqual(assessed["status"], "unknown")
        self.assertEqual(assessed["results"][0]["detail"]["counted_dinners"], 1)
        self.assertEqual(assessed["results"][0]["detail"]["unknown_dinners"], 1)


if __name__ == "__main__":
    unittest.main()
