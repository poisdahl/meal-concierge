"""Malformed structured pages expose explicit untrusted text, never an automatic recipe."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import StateStore
from recipe_import_readers import RecipeImportReaderError, read_webpage, read_webpage_jsonld
from service import Application

URL = "https://recipes.example/lentils"
VISIBLE = "<article><h1>Lentil soup</h1><p>Serves 2</p><p>200 g lentils</p><p>Simmer.</p></article>"
DECISION = {"storage": "full", "basis": "private_use", "evidence": "The owner requested a private recipe copy."}


def script(value):
    text = json.dumps(value) if not isinstance(value, str) else value
    return '<script type="application/ld+json">' + text.replace('</', '<\\/') + '</script>'


def recipe():
    return {"@type": "Recipe", "name": "Lentil soup", "recipeYield": "2 servings",
            "recipeIngredient": ["200 g lentils"], "recipeInstructions": ["Simmer."]}


class StructuredFailureTests(unittest.TestCase):
    def test_malformed_empty_and_ambiguous_scripts_require_explicit_text_interpretation(self):
        for markup in (script("{broken}"), script(""), script(recipe()) + script("{broken}"),
                       script({**recipe(), "recipeInstructions": [{"@type": "HowToStep", "name": "Simmer"}]})):
            with self.subTest(markup=markup):
                raw = (markup + VISIBLE).encode()
                result = read_webpage(raw, source_url=URL)
                self.assertEqual(result["mode"], "text")
                self.assertEqual(result["recipes"], [])
                self.assertTrue(result["requires_interpretation"])
                self.assertIn("structured_error", result)
                self.assertEqual(result["source_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertIn("200 g lentils", result["text"])
                self.assertNotIn("@type", result["text"])
        with self.assertRaises(RecipeImportReaderError):
            read_webpage_jsonld(script("{broken}") + VISIBLE, source_url=URL)

    def test_inert_optional_metadata_is_omitted_and_reported_without_guessing(self):
        raw = {**recipe(), "identifier": 42, "totalTime": 30,
               "image": ["/images/soup.jpg", "https://images.example/soup?token=DO_NOT_RETAIN"],
               "comment": {"@type": "Comment", "name": "Author Notes", "text": "Serve warm."},
               "recipeIngredient": ["", "200 g lentils", " \t "]}
        result = read_webpage(script(raw), source_url=URL)
        self.assertEqual(result["mode"], "structured")
        extracted = result["recipes"][0]
        self.assertEqual(extracted["extracted"]["ingredients"], ["200 g lentils"])
        self.assertEqual(extracted["extracted"]["source"]["external_id"], "42")
        self.assertEqual(extracted["extracted"]["notes"], "Serve warm.")
        self.assertEqual(extracted["extracted"]["times"], {})
        self.assertEqual(extracted["image_candidates"], [])
        self.assertTrue({"image", "totalTime", "recipeIngredient.empty"} <= set(extracted["unsupported_fields"]))
        self.assertNotIn("DO_NOT_RETAIN", json.dumps(result))

    def test_untrusted_source_content_cannot_become_source_authority_or_send_instructions(self):
        raw = script('{"name":"https://u:DO_NOT_RETAIN@recipes.example/","@type":"Recipe"}')
        result = read_webpage(raw + VISIBLE + '<script>send()</script>', source_url=URL)
        self.assertIn("structured_error", result)
        self.assertNotIn("DO_NOT_RETAIN", json.dumps(result))
        self.assertNotIn("send()", result["text"])

    def test_an_empty_incomplete_recipe_does_not_hide_visible_page_text(self):
        result = read_webpage(script({"@type": "Recipe"}) + VISIBLE, source_url=URL)
        self.assertTrue(result["requires_interpretation"])
        self.assertEqual(result["recipes"], [])
        self.assertIn("200 g lentils", result["text"])
        ambiguous = read_webpage(script([{"@type": "Recipe"}, {"@type": "Recipe", "name": "Another"}]) + VISIBLE,
                                 source_url=URL)
        self.assertEqual(ambiguous["structured_error"]["code"], "incomplete_recipe_selection")
        self.assertEqual(ambiguous["recipes"], [])
        self.assertIn("200 g lentils", ambiguous["text"])

    def test_verified_second_read_retains_exact_original_hash_and_rights_without_saving(self):
        raw = (script("{broken}") + VISIBLE).encode()
        with tempfile.TemporaryDirectory() as directory:
            app = Application(StateStore(Path(directory), {"household": "Synthetic", "provider": "none"}), None, None)
            request = {"operation": "recipes", "action": "import", "source_kind": "url", "url": URL,
                       "storage_decision": DECISION}
            with mock.patch("recipe_import_sources._get_bytes", return_value=(raw, "text/html")) as fetch:
                preview = app.handle(request)
                self.assertFalse(preview["personal_entry_created"])
                self.assertNotIn("discovery_ref", preview)
                self.assertIn("structured_error", preview)
                interpret_request = {**request, "interpretation": {
                    "name": "Lentil soup", "language": "en", "yield": {"page": 1, "quote": "Serves 2"},
                    "ingredients": [{"page": 1, "quote": "200 g lentils"}],
                    "steps": [{"page": 1, "quote": "Simmer."}]}}
                interpreted = app.handle(interpret_request)
                repeated = app.handle(interpret_request)
                self.assertEqual(repeated["discovery_ref"], interpreted["discovery_ref"])
                self.assertEqual(repeated["recipe_digest"], interpreted["recipe_digest"])
                self.assertEqual(fetch.call_count, 3)
            frozen = app.recipes.resolve_discovery(interpreted["discovery_ref"])["recipe"]
            self.assertEqual(frozen["external_snapshot"]["content_hash"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(frozen["rights"]["storage_decision"]["evidence"], DECISION["evidence"])
            self.assertEqual(interpreted["import_report"]["structured_error"], preview["structured_error"])
            self.assertEqual(app.recipes.search(), [])
            changed_raw = raw + b"<!-- layout-only change -->"
            with mock.patch("recipe_import_sources._get_bytes", return_value=(changed_raw, "text/html")):
                changed = app.handle(interpret_request)
            self.assertNotEqual(changed["discovery_ref"], interpreted["discovery_ref"])
            self.assertEqual(changed["recipe"]["external_snapshot"]["content_hash"], hashlib.sha256(changed_raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
