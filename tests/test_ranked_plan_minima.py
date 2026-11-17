"""Every ranked save reference follows the same minimum policy as save."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import StateStore
import planner
from planner import SAVED_MINIMUM_TARGETS, saved_menu_minimum_evaluation
from service import Application
from test_meal_concierge_planner import CONFIG, NoProviderCalls, recipe, explicit_facts


class RankedMinimumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name), CONFIG)
        self.app = Application(self.store, NoProviderCalls(), object())
        with self.store.locked() as state:
            state["setup"]["status"] = "complete"
            for target in SAVED_MINIMUM_TARGETS:
                state["profile"]["diet"][target] = 0

    def candidates(self, count, *, ingredient="gulrot"):
        results = []
        for index in range(count):
            row = self.app.handle({"operation": "recipes", "action": "save",
                "recipe": recipe(f"Dinner {index}", f"minimum-dinner-{index}", ingredient=ingredient),
                "idempotency_key": f"minimum-dinner-{index}"})["recipe"]
            results.append({"recipe_ref": {"id": row["id"], "revision": row["revision"]}})
        return results

    def plan(self, candidates, **scope):
        return self.app.handle({"operation": "menu", "action": "plan", "planner_input": {
            "candidates": candidates, "portions": 2, **scope}})["plan"]

    def test_full_week_leafy_range_is_checked_before_a_save_reference_is_returned(self):
        candidates = self.candidates(1, ingredient="spinat")
        with self.store.locked() as state:
            state["profile"]["meals"].update(dinner_days=1, dishes=1)
            state["profile"]["diet"]["leafy_green_days"] = [1, 1]
        candidates[0]["facts"] = {"leafy_green": {"source": "explicit", "assessment": "does_not_count",
            "ingredient_indices": [], "basis": "Synthetic reviewed dinner has no substantial leafy component."}}
        rejected = self.plan(candidates, week="2026-W47", dates=["2026-11-16"])
        self.assertEqual(rejected["status"], "no_plan")
        self.assertNotIn("save_ref", rejected)
        self.assertIn("leafy_green_days", rejected["request"]["strict_targets"])
        candidates[0]["facts"]["leafy_green"].update(
            assessment="substantial", ingredient_indices=[0], basis="Ingredient 0 is the leafy vegetable component.")
        accepted = self.plan(candidates, week="2026-W47", dates=["2026-11-16"])
        self.assertEqual(accepted["status"], "planned")
        saved = self.app.handle({"operation": "menu", "action": "save", "planner_ref": accepted["save_ref"]})["menu"]
        self.assertEqual(saved_menu_minimum_evaluation(saved, self.store.read()["profile"])["enforced_status"], "pass")

    def test_short_relative_plan_does_not_promise_to_meet_a_full_week_quota(self):
        candidates = self.candidates(1)
        with self.store.locked() as state:
            state["profile"]["diet"].update(minimum_fish_portions=2, leafy_green_days=[4, 5])
        planned = self.plan(candidates, planning_mode="relative", days=[1])
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned["request"]["strict_targets"], [])
        saved = self.app.handle({"operation": "menu", "action": "save", "planner_ref": planned["save_ref"]})["menu"]
        evaluation = saved_menu_minimum_evaluation(saved, self.store.read()["profile"])
        self.assertTrue(evaluation["complete_menu"])
        self.assertEqual(evaluation["enforced_status"], "pass")

    def test_explicit_strict_target_still_blocks_a_short_relative_plan(self):
        candidates = self.candidates(1)
        with self.store.locked() as state:
            state["profile"]["diet"]["minimum_fish_portions"] = 2
        planned = self.plan(candidates, planning_mode="relative", days=[1], strict_targets=["minimum_fish_portions"])
        self.assertEqual(planned["status"], "no_plan")
        self.assertNotIn("save_ref", planned)

    def test_seven_date_period_checks_the_same_minima_even_with_fewer_normal_dinners(self):
        candidates = self.candidates(7)
        for candidate in candidates:
            candidate["facts"] = explicit_facts(dietary=[], complete=True)
        with self.store.locked() as state:
            state["profile"]["meals"].update(dinner_days=4, dishes=4)
            state["profile"]["diet"]["minimum_fish_portions"] = 2
        planned = self.plan(candidates, planning_mode="ad_hoc", dates=[f"2026-11-{day}" for day in range(16, 23)])
        self.assertEqual(planned["status"], "no_plan")
        self.assertNotIn("save_ref", planned)
        self.assertIn("minimum_fish_portions", planned["request"]["strict_targets"])

    def test_permutation_search_reuses_candidate_facts_and_keeps_winner_diagnostics_in_order(self):
        candidates = self.candidates(4)
        for index, candidate in enumerate(candidates):
            candidate["facts"] = explicit_facts(active_minutes=15 + index)
        with self.store.locked() as state:
            state["profile"]["meals"].update(dinner_days=3, dishes=3)
            state["profile"]["diet"]["leafy_green_days"] = [0, 3]
        with patch("planner._ingredient_identities", wraps=planner._ingredient_identities) as identities, \
                patch("planner._leafy_dinner", wraps=planner._leafy_dinner) as leafy:
            result = self.plan(candidates, week="2026-W47", dates=["2026-11-16", "2026-11-17", "2026-11-18"],
                               alternatives=3, strict_targets=["active_minutes"])
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["explored_states"], 24)
        self.assertLessEqual(identities.call_count, 12)
        self.assertLessEqual(leafy.call_count, 40)
        by_key = {row["reference_key"]: row["facts"]["active_minutes"]["value"]
                  for row in result["candidate_evaluations"]}
        for selection in result["selections"]:
            active = next(row for row in selection["strict_targets"]["results"] if row["target"] == "active_minutes")
            self.assertEqual(active["detail"]["values"], [by_key[slot["reference_key"]] for slot in selection["slots"]])


if __name__ == "__main__":
    unittest.main()
