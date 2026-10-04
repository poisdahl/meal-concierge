"""Explicit comparable-unit yields follow serving arithmetic, never source prose."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_views import project_agent_result
from core import StateStore
from recipe_delivery import render_menu
from recipes import RecipeError, normalize_recipe, prepare_recipe_input, scale_recipe
from service import Application
from service_common import menu_email_html


class OfflineProvider:
    def probe(self):
        return {"server": {"name": "synthetic"}, "tool_count": 0}


def fixture():
    return normalize_recipe({
        "schema_version": 2, "name": "Synthetic vegetable rounds", "language": "en",
        "portions": 3, "portions_evidence": {"basis": "source", "input": "three servings"},
        "ingredients": [{"quantity": 300, "unit": "g", "item": "carrots", "scalable": True}],
        "steps": ["Shape equal-sized rounds and cook."],
        "source": {"kind": "user", "relationship": "user_supplied"},
        "rights": {"storage": "full"},
        "yield": {"original_text": "six rounds", "quantity": 6, "unit": "rounds", "scalable": True,
                  "evidence": {"quantity": {"basis": "source", "input": "six rounds"},
                               "unit": {"basis": "source", "input": "rounds"}}},
    })


class YieldScalingTests(unittest.TestCase):
    def test_service_menu_and_delivery_use_current_yield_preserving_source(self):
        with tempfile.TemporaryDirectory() as root:
            app = Application(StateStore(Path(root) / "state", {"household": "Synthetic", "provider": "oda"}), OfflineProvider(), None)
            app.handle({"operation": "setup", "action": "apply", "keep_current": True})
            authored = fixture()
            authored["portions_evidence"]["basis"] = "user"
            for evidence in authored["yield"]["evidence"].values():
                evidence["basis"] = "user"
            saved = app.handle({"operation": "recipes", "action": "save", "recipe": authored, "idempotency_key": "yield"})["recipe"]
            current = None
            for target in (1, 6, 8):
                request = {"operation": "menu", "action": "save", "language": "en", "menu": {
                    "week": "2026-W40", "dishes": [{
                        "recipe_ref": {"id": saved["id"], "revision": saved["revision"]}, "portions": target}], "salads": []}}
                if current:
                    request["menu_ref"] = app._cart_menu_ref(current)
                current = app.handle(request)["menu"]
                recipe = current["dishes"][0]
                self.assertEqual(recipe["yield"]["quantity"], {"numerator": target * 2, "denominator": 1})
                rendered = render_menu(current, None, images=False)
                self.assertIn(f"Yield for these servings: {target * 2} rounds", rendered["text"])
                self.assertIn("Source yield: six rounds", rendered["text"])
                view = project_agent_result("recipes", "get", {"recipe": recipe})
                self.assertEqual(view["yield"]["quantity"], recipe["yield"]["quantity"])
            restarted = Application(app.store, OfflineProvider(), None)
            reread = restarted.handle({"operation": "recipes", "action": "get", "recipe_id": saved["id"]})["recipe"]
            self.assertTrue(reread["yield"]["scalable"])
            self.assertEqual(reread["yield"]["quantity"], {"numerator": 6, "denominator": 1})

    def test_rescaling_roundtrip_and_tamper_rejection(self):
        original = fixture()
        scaled = scale_recipe(scale_recipe(original, 1), 8)
        normalized = normalize_recipe(json.loads(json.dumps(scaled)))
        calculation = normalized["yield"]["evidence"]["quantity"]["calculation"]
        self.assertEqual(calculation["input_quantity"], {"numerator": 6, "denominator": 1})
        self.assertEqual(calculation["input_portions"], {"numerator": 3, "denominator": 1})
        self.assertEqual(calculation["factor"], {"numerator": 8, "denominator": 3})
        self.assertEqual(normalized["yield"]["original_text"], original["yield"]["original_text"])
        for field in ("input_quantity", "input_portions"):
            bad = deepcopy(normalized)
            bad["yield"]["evidence"]["quantity"]["calculation"][field]["numerator"] += 1
            with self.assertRaisesRegex(RecipeError, "calculation"):
                normalize_recipe(bad)

    def test_omitted_false_null_and_noop_preserve_existing_yields(self):
        original = fixture()
        self.assertEqual(scale_recipe(original)["yield"], original["yield"])
        for flag in (None, False):
            recipe = fixture()
            if flag is None:
                recipe["yield"].pop("scalable")
            else:
                recipe["yield"]["scalable"] = flag
            normalized = normalize_recipe(recipe)
            self.assertEqual(normalized, recipe)
            self.assertEqual(scale_recipe(scale_recipe(recipe, 6), 8)["yield"], recipe["yield"])
            self.assertNotIn("Yield for these servings", menu_email_html({"dishes": [scale_recipe(recipe, 6)], "output_language": "en"}))
        original["yield"] = None
        self.assertIsNone(scale_recipe(original, 8)["yield"])
        legacy = fixture()
        legacy["yield"].pop("scalable")
        legacy["yield"]["evidence"]["quantity"]["calculation"] = {
            "operation": "portion_scale", "input_quantity": {"numerator": 3, "denominator": 1},
            "factor": {"numerator": 2, "denominator": 1}}
        legacy = normalize_recipe(legacy)
        self.assertEqual(scale_recipe(legacy, 8)["yield"], legacy["yield"])

    def test_invalid_optins_and_disabling_calculated_yield_require_correction(self):
        for field, value in (("scalable", "true"), ("scalable", 1), ("quantity", None), ("unit", "")):
            recipe = fixture()
            recipe["yield"][field] = value
            with self.assertRaises(RecipeError):
                normalize_recipe(recipe)
        for field in ("portions", "yield"):
            recipe = fixture()
            evidence = recipe["portions_evidence"] if field == "portions" else recipe["yield"]["evidence"]["quantity"]
            evidence["basis"] = "unknown"
            with self.assertRaisesRegex(RecipeError, "scalable yield"):
                normalize_recipe(recipe)
        scaled = scale_recipe(fixture(), 6)
        scaled["yield"]["scalable"] = False
        with self.assertRaisesRegex(RecipeError, "yield.scalable"):
            normalize_recipe(scaled)
        prior = scale_recipe(fixture(), 6)
        scaled["yield"].pop("scalable")
        with self.assertRaisesRegex(RecipeError, "removing yield.scalable"):
            prepare_recipe_input(scaled, prior=prior)
        with self.assertRaisesRegex(RecipeError, "yield scaling was disabled"):
            scale_recipe(scaled, 8)

    def test_estimates_and_fractional_yield_preserve_meaning(self):
        recipe = fixture()
        recipe["portions_evidence"] = {"basis": "estimate", "input": "three servings", "assumptions": "Estimated serving size."}
        recipe = normalize_recipe(recipe)
        scaled = scale_recipe(recipe, 0.25)
        self.assertEqual(scaled["yield"]["quantity"], {"numerator": 1, "denominator": 2})
        self.assertEqual(scaled["steps"], recipe["steps"])
        menu = {"dishes": [scaled], "output_language": "en"}
        self.assertIn("Yield for these servings: 1/2 rounds (estimate)", menu_email_html(menu))
        self.assertIn("Yield for these servings: 1/2 rounds</p>", menu_email_html(menu, show_estimate_labels=False))
        self.assertEqual(scaled["yield"]["evidence"]["quantity"]["calculation"]["portions_evidence"]["basis"], "estimate")


if __name__ == "__main__":
    unittest.main()
