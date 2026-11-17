"""Source component headings survive without becoming required shopping goods."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from recipe_import_readers import RecipeImportReaderError, read_mealie_json, read_recipesage_export, source_candidate
from recipe_import_sources import _recipesage_record
from recipe_library_recipesage import RecipeSageAdapter
from recipes import normalize_recipe, scale_recipe
from test_recipe_import import recipesage_fixture

LINES = ["[Empty garnish]", "[Dough]", "350 g flour", "200 ml water", "[Sauce]", "100 g tomatoes", "[Serving extras]"]


class RecipeSageIngredientSectionTests(unittest.TestCase):
    def assert_components(self, recipe):
        normalized = normalize_recipe(recipe)
        self.assertEqual(len(normalized["ingredients"]), 3)
        self.assertEqual([i["notes"] for i in normalized["ingredients"]], ["[Dough]", "[Dough]", "[Sauce]"])
        self.assertEqual([i["original_text"] for i in normalized["ingredients"]], [LINES[2], LINES[3], LINES[5]])
        for row in normalized["ingredients"]:
            self.assertEqual(row["evidence"]["quantity"]["input"], row["original_text"])
            self.assertEqual(row["evidence"]["quantity"]["basis"], "source")
        self.assertIn("[Empty garnish]", normalized["notes"])
        self.assertIn("[Serving extras]", normalized["notes"])
        requirements = scale_recipe(normalized)["shopping_requirements"]
        self.assertEqual(len(requirements), 3)
        self.assertFalse(any("[" in row["item"] for row in requirements))
        return normalized

    def test_offline_export_preserves_component_association_and_original_lines(self):
        raw = recipesage_fixture()
        raw["recipeIngredient"] = LINES
        raw["recipeYield"] = "2 servings"
        before = deepcopy(raw)
        record = read_recipesage_export(json.dumps({"recipes": [raw]}))[0]
        self.assertEqual(record["extracted"]["ingredients"], LINES)
        result = self.assert_components(source_candidate(record))
        self.assertEqual(result["source"]["original"]["publisher"], "Synthetic cookbook")
        self.assertEqual(raw, before)

    def test_native_import_and_adapter_get_both_map_the_same_component_notes(self):
        raw = json.loads((ROOT / "tests/fixtures/recipesage/v4.0.6.json").read_text())["recipe_get"]
        raw["ingredients"] = "\n".join(LINES)
        raw["yield"] = "2 servings"
        raw["notes"] = "Original note."
        before = deepcopy(raw)
        native = self.assert_components(source_candidate(_recipesage_record(raw)))
        adapter = RecipeSageAdapter({"library_id": "synthetic-sage", "provider": "recipesage",
                                     "base_url": "https://recipes.example", "read_only": True}, {"token": "SYNTHETIC"})
        direct = self.assert_components(adapter._mapped_recipe(raw))
        self.assertEqual(native["ingredients"], direct["ingredients"])
        self.assertIn("Original note.", native["notes"])
        self.assertIn("Original note.", direct["notes"])
        self.assertEqual(raw, before)

    def test_a_headerless_recipe_and_a_literal_bracket_in_an_ingredient_are_unchanged(self):
        raw = recipesage_fixture()
        raw["recipeIngredient"] = ["100 g tomatoes [drained]", "2 dl water"]
        recipe = source_candidate(read_recipesage_export(json.dumps({"recipes": [raw]}))[0])
        self.assertEqual(len(recipe["ingredients"]), 2)
        self.assertNotIn("notes", recipe["ingredients"][0])
        self.assertEqual(recipe["ingredients"][0]["original_text"], raw["recipeIngredient"][0])

    def test_native_imports_report_unusable_optional_urls_and_mealie_empty_rows(self):
        for provider, version, url_field in (("mealie", "v3.24.0.json", "orgURL"),
                                             ("recipesage", "v4.0.6.json", "url")):
            for url in ("Family recipe card", "https://u:DO_NOT_RETAIN@recipes.example/recipe"):
                with self.subTest(provider=provider, url=url):
                    raw = json.loads((ROOT / "tests/fixtures" / provider / version).read_text())["recipe_get"]
                    raw[url_field] = url
                    if "DO_NOT_RETAIN" in url:
                        with self.assertRaises(RecipeImportReaderError) as caught:
                            if provider == "mealie":
                                read_mealie_json(json.dumps(raw))
                            else:
                                _recipesage_record(raw)
                        self.assertNotIn("DO_NOT_RETAIN", str(caught.exception))
                        continue
                    if provider == "mealie":
                        raw["recipeIngredient"].append({"originalText": ""})
                        record = read_mealie_json(json.dumps(raw))
                        self.assertIn("recipeIngredient.empty", record["unsupported_fields"])
                    else:
                        record = _recipesage_record(raw)
                    candidate = normalize_recipe(source_candidate(record))
                    self.assertTrue(candidate["ingredients"])
                    self.assertIsNone(candidate["source"]["original"]["url"])
                    self.assertIn(url_field, record["unsupported_fields"])
                    self.assertNotIn("DO_NOT_RETAIN", json.dumps(record))


if __name__ == "__main__":
    unittest.main()
