"""Reader diagnostics and text matching without fetching or interpreting source instructions."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from recipe_import_readers import RecipeImportReaderError, read_transcript, read_webpage, read_webpage_jsonld
from recipe_sources import WikibooksSource
from test_meal_concierge_recipes import full_recipe


class SourceQuoteNormalizationTests(unittest.TestCase):
    def source(self, text, ingredient, step="Simmer."):
        return {"kind": "pasted_text", "pages": [{"page": 1, "text": text}],
                "interpretation": {"name": "Soup", "language": "en",
                    "ingredients": [{"page": 1, "quote": ingredient}],
                    "steps": [{"page": 1, "quote": step}]}}

    def test_soft_hyphens_zero_width_spaces_and_nfd_do_not_hide_normal_quotes(self):
        for text, quote in (("Soup\n200 g gul\u00adrøtter\nSimmer.", "200 g gulrøtter"),
                            ("Soup\n200 g cre\u0300me\nSimmer.", "200 g crème"),
                            ("Soup\n200 g lent\u200bils\nSimmer.", "200 g lentils")):
            with self.subTest(text=text):
                result = read_transcript(self.source(text, quote))
                self.assertEqual(result["candidate"]["ingredients"][0]["evidence"]["quantity"]["input"], "Page 1: " + quote)
                exact = read_transcript(self.source(text, text.splitlines()[1]))
                self.assertEqual(result["source_context"]["content_sha256"], exact["source_context"]["content_sha256"])

    def test_normalization_cannot_turn_a_numeric_fragment_into_source_evidence(self):
        for text in ("Soup\n1\u200b500 g carrots\nSimmer.", "Soup\n1\u00ad500 g carrots\nSimmer."):
            with self.subTest(text=text), self.assertRaisesRegex(RecipeImportReaderError, "inside a number"):
                read_transcript(self.source(text, "500 g carrots"))

    def test_inaccessible_text_and_overlong_steps_have_field_specific_errors(self):
        with self.assertRaisesRegex(RecipeImportReaderError, "no readable recipe text"):
            read_webpage("<script>renderRecipe()</script>", source_url="https://recipes.example/soup")
        raw = {"@type": "Recipe", "name": "Soup", "recipeYield": "2 servings",
               "recipeIngredient": ["200 g lentils"], "recipeInstructions": ["x" * 4001]}
        with self.assertRaisesRegex(RecipeImportReaderError, "recipeInstructions must be bounded text"):
            read_webpage_jsonld('<script type="application/ld+json">' + json.dumps(raw) + '</script>',
                               source_url="https://recipes.example/soup")
        raw["recipeInstructions"] = ["x" * 4000]
        self.assertEqual(len(read_webpage_jsonld('<script type="application/ld+json">' + json.dumps(raw) + '</script>',
                                               source_url="https://recipes.example/soup")), 1)


class WikibooksQueryTests(unittest.TestCase):
    def test_a_category_lower_bound_does_not_make_unrelated_titles_search_hits(self):
        titles = ["Cookbook:Lamb Vindaloo", "Cookbook:Lasagne", "Cookbook:Laks soup"]
        source = WikibooksSource(transport=lambda *args, **kwargs: {"query": {"categorymembers": [{"title": t} for t in titles]}})
        with mock.patch.object(source, "_fetch_recipe", side_effect=lambda title: full_recipe(title)) as fetch:
            results = source.search("laks", 5)
        self.assertEqual([r["name"] for r in results], ["Cookbook:Laks soup"])
        fetch.assert_called_once_with("Cookbook:Laks soup")


if __name__ == "__main__":
    unittest.main()
