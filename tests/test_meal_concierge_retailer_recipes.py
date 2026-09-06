from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import HouseholdError
from meny import MenyClient
from retailer_recipes import MAX_DETAIL_BYTES, meny_recipe_input, meny_recipe_jsonld, meny_recipe_path


RECIPE_PATH = "/oppskrifter/test/synthetic-rice"
RECIPE_URL = "https://meny.no" + RECIPE_PATH


def page():
    # Invented culinary text in the field types observed on MENY 2026-09-06.
    # No copied private recipe, account, cart or product association.
    recipe = {
        "@context": "https://schema.org/", "@type": "Recipe",
        "url": RECIPE_URL, "name": "Synthetic rice",
        "recipeYield": "Antall personer: 4", "inLanguage": "no",
        "recipeIngredient": ["200 g ris", "1 stk testgrønnsak", "salt etter smak"],
        "recipeInstructions": ["Kok risen.", "Server testretten."],
        "author": {"@type": "Organization", "name": "MENY"},
        "dateModified": "2026-09-01T10:00:00Z",
    }
    return {"ready": True, "url": RECIPE_URL, "canonical_urls": [RECIPE_URL],
            "scripts": [json.dumps(recipe)]}


def browser_evaluate(script, observation):
    # Execute the production extractor against synthetic DOM observations.
    harness = r'''
const fs = require('node:fs');
const {script, page} = JSON.parse(fs.readFileSync(0, 'utf8'));
global.location = new URL(page.url);
global.document = {querySelectorAll(selector) {
  if (selector === 'script[type="application/ld+json"]') return page.scripts.map(textContent => ({textContent}));
  if (selector === 'link[rel="canonical"]') return page.canonical_urls.map(href => ({href}));
  throw new Error('Unexpected DOM interaction');
}};
process.stdout.write(eval(script));
'''
    completed = subprocess.run([shutil.which("node"), "-e", harness],
                               input=json.dumps({"script": script, "page": observation}),
                               text=True, capture_output=True, check=True, timeout=10)
    return json.loads(completed.stdout)


class MenyRecipeObservationTests(unittest.TestCase):
    def test_exact_observed_structure_retains_text_and_servings_label(self):
        result = meny_recipe_jsonld(page(), RECIPE_PATH)
        self.assertEqual(result["recipeYield"], "Antall personer: 4")
        self.assertEqual(result["recipeIngredient"][0], "200 g ris")
        self.assertEqual(result["recipeInstructions"], ["Kok risen.", "Server testretten."])

    def test_unrelated_jsonld_does_not_replace_the_exact_recipe(self):
        observation = page()
        observation["scripts"].append('{"@type":"BreadcrumbList"}')
        self.assertEqual(meny_recipe_jsonld(observation, RECIPE_PATH)["name"], "Synthetic rice")

    def test_path_rejects_other_routes_encoded_traversal_and_external_targets(self):
        for value in (None, "https://meny.no" + RECIPE_PATH, "/varer/test", "/oppskrifter/../varer",
                      "/oppskrifter/%2e%2e/varer", "/oppskrifter/%252e%252e/varer", "/oppskrifter//test",
                      "/oppskrifter/test?portions=8", "/oppskrifter/%3fquery", "/oppskrifter/%00test",
                      "/oppskrifter/%zz", "/oppskrifter/%5ctest"):
            with self.subTest(value=value), self.assertRaises(HouseholdError):
                meny_recipe_path(value)

    def test_page_canonical_and_structured_identity_must_all_match(self):
        for change in ({"ready": False}, {"url": RECIPE_URL + "?portions=8"},
                       {"url": "https://oda.com" + RECIPE_PATH}, {"canonical_urls": []},
                       {"canonical_urls": [RECIPE_URL, RECIPE_URL]}):
            with self.subTest(change=change), self.assertRaises(HouseholdError):
                meny_recipe_jsonld({**page(), **change}, RECIPE_PATH)
        observation = page()
        document = json.loads(observation["scripts"][0])
        document["url"] = "https://meny.no/oppskrifter/other"
        observation["scripts"] = [json.dumps(document)]
        with self.assertRaises(HouseholdError):
            meny_recipe_jsonld(observation, RECIPE_PATH)

    def test_duplicate_missing_malformed_and_oversized_data_fail_closed(self):
        valid = page()["scripts"][0]
        invalid_scripts = [[], [valid, valid], ["{}"], ["{"], ['{"x":NaN}'],
                           [valid, '{"@graph":[{"@type":"Recipe"}]}'],
                           [valid, '{"@type":["Thing","Recipe"]}'],
                           [valid.replace('"@type": "Recipe"', '"@type":"Recipe","@type":"Recipe"')],
                           [" " * (MAX_DETAIL_BYTES + 1)], [None], ["{}"] * 9]
        for scripts in invalid_scripts:
            with self.subTest(shape=[type(value).__name__ for value in scripts]), self.assertRaises(HouseholdError):
                meny_recipe_jsonld({**page(), "scripts": scripts}, RECIPE_PATH)

    def test_required_source_fields_are_bounded_without_truncation(self):
        for field, value in (("recipeIngredient", []), ("recipeIngredient", ["x"] * 201),
                             ("recipeIngredient", ["x" * 501]), ("recipeIngredient", [None]),
                             ("recipeInstructions", [{"text": "unobserved variant"}]),
                             ("recipeInstructions", ["x"] * 101), ("recipeYield", 4),
                             ("name", ""), ("name", "\ud800"),
                             ("author", {"@type": ["Organization"], "name": "MENY"}),
                             ("author", {"@type": {}, "name": "MENY"}),
                             ("author", {"@type": "Person", "name": "x" * 301})):
            observation = page()
            document = json.loads(observation["scripts"][0])
            document[field] = value
            observation["scripts"] = [json.dumps(document)]
            with self.subTest(field=field), self.assertRaises(HouseholdError):
                meny_recipe_jsonld(observation, RECIPE_PATH)


class MenyRecipeAdapterGateTests(unittest.TestCase):
    def client(self):
        return MenyClient(instance="synthetic", binary="agent-browser", executable="/unavailable",
                          profile="/unavailable", home="/unavailable", socket_directory="/unavailable", uid=1000, gid=1000)

    def test_login_failure_never_opens_recipe_or_changes_cart(self):
        client = self.client()
        client._require_login = mock.Mock(side_effect=HouseholdError("login required"))
        client._open = mock.Mock()
        client._change_cart = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "login required"):
            client.call("recipe_detail", {"recipe_id": RECIPE_PATH})
        client._open.assert_not_called()
        client._change_cart.assert_not_called()

    def test_deadline_expiry_prevents_browser_work(self):
        client = self.client()
        client._require_login = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "deadline"):
            client.call("recipe_detail", {"recipe_id": RECIPE_PATH}, deadline=0)
        client._require_login.assert_not_called()

    def test_native_scaling_arguments_are_not_silently_ignored(self):
        client = self.client()
        client._require_login = mock.Mock()
        client._open = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "only the exact"):
            client.call("recipe_detail", {"recipe_id": RECIPE_PATH, "portions": 8})
        client._open.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the browser extractor")
    def test_actual_extractor_preserves_page_data_but_lost_login_stops_normalization(self):
        client = self.client()
        client._require_login = mock.Mock()
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock(side_effect=[None, HouseholdError("login lost")])
        extracted = []
        def evaluate(script):
            result = browser_evaluate(script, page())
            extracted.append(result)
            return result
        client._eval = evaluate
        with self.assertRaisesRegex(HouseholdError, "login lost"):
            client.call("recipe_detail", {"recipe_id": RECIPE_PATH})
        self.assertEqual(extracted, [page()])
        client._open.assert_called_once_with(RECIPE_URL)


class MenyRecipeInputTests(unittest.TestCase):
    def test_base_source_quantities_and_person_servings_remain_exact(self):
        value = meny_recipe_input(page(), RECIPE_PATH, fetched_at="2026-09-06T00:00:00Z")
        self.assertEqual(value["portions"], 4)
        self.assertEqual(value["portions_evidence"]["basis"], "source")
        self.assertEqual(value["yield"]["original_text"], "Antall personer: 4")
        self.assertEqual(value["ingredients"][0]["quantity"], {"numerator": 200, "denominator": 1})
        self.assertEqual(value["ingredients"][0]["original_text"], "200 g ris")
        self.assertIsNone(value["ingredients"][2]["quantity"])
        self.assertNotIn("calculation", value["ingredients"][0]["evidence"]["quantity"])

    def test_source_cannot_grant_other_provider_pantry_diet_or_estimate_authority(self):
        observation = page()
        document = json.loads(observation["scripts"][0])
        document.update({"source_provider": "oda", "accepted_estimates": True,
                         "pantry": True, "suitableForDiet": "GlutenFreeDiet",
                         "product_associations": [{"product_id": "unsafe"}]})
        observation["scripts"] = [json.dumps(document)]
        value = meny_recipe_input(observation, RECIPE_PATH)
        self.assertEqual(value["source_provider"], "meny")
        self.assertEqual(value["source"]["relationship"], "original")
        self.assertEqual(value["source"]["external_id"], RECIPE_PATH)
        self.assertNotIn("accepted_estimates", value)
        self.assertNotIn("product_associations", value)
        self.assertNotIn("suitableForDiet", value)
        self.assertFalse(any(item.get("pantry") for item in value["ingredients"]))

    def test_unobserved_yield_keeps_original_without_guessing_persons(self):
        for text in ("4", "2 loaves", "4 porsjoner", "Antall personer: mange"):
            observation = page()
            document = json.loads(observation["scripts"][0])
            document["recipeYield"] = text
            observation["scripts"] = [json.dumps(document)]
            with self.subTest(text=text):
                value = meny_recipe_input(observation, RECIPE_PATH)
                self.assertIsNone(value["portions"])
                self.assertEqual(value["portions_evidence"]["basis"], "unknown")
                self.assertEqual(value["yield"]["original_text"], text)


if __name__ == "__main__":
    unittest.main()
