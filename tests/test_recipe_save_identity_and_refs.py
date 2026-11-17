"""An adaptation with a cover keeps its identity; malformed references are rejected, not crashes."""
import base64
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from core import HouseholdError, StateStore
from service import Application
from test_meal_concierge_products import FakeProvider


def recipe(name, external_id):
    return {"schema_version": 2, "name": name, "language": "nb-NO", "portions": 2,
            "ingredients": [{"item": "gulrot", "quantity": 200, "unit": "g", "raw": "200 g gulrot"}],
            "steps": ["Kok."], "tags": [], "categories": ["dinner"],
            "source": {"kind": "user", "publisher": "Fixture", "external_id": external_id,
                       "relationship": "user_supplied"},
            "rights": {"storage": "full"}}


class RecipeSaveTests(unittest.TestCase):
    def test_source_attribution_labels_are_single_line(self):
        for key in ("publisher", "author", "title", "external_id"):
            for original in (True, False):
                value = recipe("Brød", "bread")
                source = value["source"].setdefault("original", {}) if original else value["source"]
                source[key] = "Bakery\u2028Kitchen"
                with self.subTest(key=key, original=original), self.assertRaisesRegex(HouseholdError, "single-line"):
                    self.call(action="save", recipe=value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        store = StateStore(Path(self.temp.name) / "state", {
            "instance": "save-identity", "household": "Synthetic household",
            "provider": "oda", "profile_overrides": {}})
        self.app = Application(store, FakeProvider(), object(), external_recipe_sources={})
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})

    def call(self, **request):
        return self.app.handle({"operation": "recipes", **request})

    def test_file_import_preserves_deleted_intent_and_continues(self):
        store = self.app.recipes
        soup, rice = recipe("Soup", "soup"), recipe("Rice", "rice")
        self.assertEqual(store.import_records([soup])["created"], 1)
        with store._connection() as con:
            saved = con.execute("SELECT id,revision FROM recipes").fetchone()
        store.delete(saved["id"], saved["revision"])
        result = store.import_records([soup, rice])
        self.assertEqual([r["record"] for r in result["conflicts"]], [1])
        self.assertEqual(result["created"], 1)
        self.assertEqual(store.import_records([soup], import_intent="restore-reviewed")["created"], 1)
        self.assertEqual(store.import_records([soup], import_intent="restore-reviewed")["created"], 0)

    def test_labels_reject_controls_and_errors_name_only_the_field(self):
        from recipes import normalize_recipe, RecipeError
        value = recipe("Søt suppe", "soup")
        value["notes"] = "Line one.\nLine two."
        self.assertEqual(normalize_recipe(value)["notes"], value["notes"])
        for name in ("Soup\nsecond", "Soup\u202eabc"):
            with self.assertRaisesRegex(RecipeError, "single-line"):
                self.call(action="save", recipe={**value, "name": name}, idempotency_key="control-test")
        value["rights"]["license_url"] = "https://user:synthetic@example.test"
        with self.assertRaisesRegex(RecipeError, "rights.license_url") as error:
            normalize_recipe(value)
        self.assertNotIn("synthetic", str(error.exception))

    def test_an_adaptation_with_a_cover_is_saved_beside_its_original(self):
        original = self.call(action="save", recipe=recipe("Gulrotsuppe", "soup"), idempotency_key="original")["recipe"]
        adapted = self.call(action="adapt", recipe_ref={"id": original["id"], "revision": original["revision"]},
                            recipe_digest=original["recipe_digest"], source_schema_version=2,
                            changes={"name": "Gulrotsuppe med chili"})
        self.assertTrue(adapted["source_identity"].startswith("adapt:v1:"))
        buffer = io.BytesIO()
        Image.new("RGB", (64, 48), (200, 80, 40)).save(buffer, "PNG")
        covered = self.call(action="cover_import", discovery_ref=adapted["discovery_ref"],
                            recipe_digest=adapted["recipe_digest"], image={"alt": "Soup", "credit": "Own photo"},
                            image_base64=base64.b64encode(buffer.getvalue()).decode())
        # The cover used to drop the adaptation identity, so the save collided with the original.
        self.assertTrue(covered["source_identity"].startswith("adapt:v1:"))
        self.assertIsNotNone(covered["recipe"]["image"]["asset_id"])
        saved = self.call(action="save", discovery_ref=covered["discovery_ref"], idempotency_key="adapted-cover")
        self.assertEqual((saved["saved"], saved["status"]), (True, "confirmed"))
        self.assertNotEqual(saved["recipe"]["id"], original["id"])
        self.assertEqual(sorted(row["name"] for row in self.call(action="search")["recipes"]),
                         ["Gulrotsuppe", "Gulrotsuppe med chili"])

    def test_malformed_references_are_rejected_as_household_errors(self):
        original = self.call(action="save", recipe=recipe("Gulrotsuppe", "soup"), idempotency_key="original")["recipe"]
        incomplete = {"library_id": "builtin", "recipe_id": original["id"]}
        requests = [
            {"action": "save", "recipe": {**recipe("Kopi", "copy"), "library_recipe_ref": incomplete}},
            {"action": "update", "recipe_id": original["id"], "expected_revision": 1,
             "recipe": {**recipe("Gulrotsuppe", "soup"), "library_recipe_ref": incomplete}},
            {"action": "save", "discovery_ref": ["discovery:v1:x"], "library_id": "builtin"},
            {"action": "save", "discovery_ref": {"ref": "x"}},
            {"action": "save", "discovery_ref": 7, "idempotency_key": "numeric"},
            {"action": "save", "discovery_ref": "", "library_id": "builtin"},
        ]
        for request in requests:
            with self.subTest(request=request):
                # Anything else would end the connection without telling the caller why.
                with self.assertRaises(HouseholdError):
                    self.call(**request)
        self.assertEqual([row["name"] for row in self.call(action="search")["recipes"]], ["Gulrotsuppe"])


if __name__ == "__main__":
    unittest.main()
