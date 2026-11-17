"""A rejected local discovery save can be repaired without replaying external effects."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from core import StateStore
from recipes import RecipeError
from service import Application
from test_meal_concierge_recipes import full_recipe


class BuiltinDiscoveryRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(StateStore(Path(self.temp.name), {"household": "Synthetic", "provider": "none"}), None, None)
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})
        self.preview = self.app.recipes.persist_discovery(full_recipe("Synthetic", external_id="retry"))

    def save(self, key):
        return self.app.handle({"operation": "recipes", "action": "save", "discovery_ref": self.preview["discovery_ref"], "idempotency_key": key})

    def test_a_definite_bank_rejection_retains_its_diagnostic_and_same_key_can_retry(self):
        with mock.patch.object(self.app.recipes, "save_discovery", side_effect=RecipeError("managed cover asset is missing")):
            with self.assertRaisesRegex(RecipeError, "managed cover asset is missing"):
                self.save("retry-exact")
        result = self.save("retry-exact")
        self.assertTrue(result["saved"])
        self.assertEqual(self.save("retry-exact")["library_recipe_ref"], result["library_recipe_ref"])
        self.assertEqual(len(self.app.recipes.search()), 1)

    def test_reused_unrelated_key_can_be_replaced_after_a_rejection(self):
        self.app.recipes.save(full_recipe("Unrelated"), idempotency_key="already-used")
        with self.assertRaisesRegex(RecipeError, "idempotency key was already used"):
            self.save("already-used")
        result = self.save("corrected-key")
        self.assertTrue(result["saved"])
        self.assertEqual(len(self.app.recipes.search()), 2)

    def test_external_failed_operations_are_never_redispatched(self):
        operation = self.app.recipes.begin_library_create(self.preview["discovery_ref"], "external", idempotency_key="external-failed")
        self.app.recipes.finish_library_create(operation["operation_id"], "failed", error_code="builtin_rejected", error="synthetic")
        replay = self.app.recipes.begin_library_create(self.preview["discovery_ref"], "external", idempotency_key="external-failed")
        self.assertEqual(replay["status"], "failed")
        self.assertEqual(replay["operation_id"], operation["operation_id"])


class SourceChangedUpdateTests(unittest.TestCase):
    save = BuiltinDiscoveryRetryTests.save

    def setUp(self):
        BuiltinDiscoveryRetryTests.setUp(self)
        from recipes import source_ingredient, normalize_recipe
        self.original = normalize_recipe({
            "schema_version": 2, "name": "Synthetic source soup", "portions": 2,
            "portions_evidence": {"basis": "source", "input": "Serves 2"},
            "source": {"kind": "web", "url": "https://recipes.example/soup", "relationship": "original"},
            "rights": {"storage": "full"}, "steps": ["Simmer."],
            "ingredients": [source_ingredient("200 g carrots", language="en")]})
        self.identity = "import:v1:" + "a" * 64
        self.preview = self.app.recipes.persist_discovery(self.original, source_identity=self.identity)
        self.saved = self.save("original-save")["recipe"]

    def test_source_conflict_can_be_updated_from_the_same_service_resolved_identity(self):
        from copy import deepcopy
        from recipes import source_ingredient
        corrected = deepcopy(self.original)
        corrected["ingredients"] = [source_ingredient("400 g carrots", language="en")]
        snapshot = self.app.recipes.persist_discovery(corrected, source_identity=self.identity)
        conflict = self.app.handle({"operation": "recipes", "action": "save", "discovery_ref": snapshot["discovery_ref"], "idempotency_key": "source-conflict"})
        self.assertEqual(conflict["conflict"]["kind"], "source_changed")
        request = {"operation": "recipes", "action": "update", "recipe_id": self.saved["id"],
                   "expected_revision": self.saved["revision"], "discovery_ref": snapshot["discovery_ref"],
                   "idempotency_key": "update-source"}
        result = self.app.handle(request)["recipe"]
        self.assertEqual(result["revision"], self.saved["revision"] + 1)
        self.assertEqual(result["ingredients"][0]["quantity"], {"numerator": 400, "denominator": 1})
        self.assertEqual(result["ingredients"][0]["evidence"]["quantity"]["basis"], "source")
        self.assertTrue(self.app.handle(request)["recipe"]["idempotent"])
        self.assertEqual(self.app.recipes.get(self.saved["id"], 1)["ingredients"][0]["quantity"]["numerator"], 200)
        retried = self.app.handle({"operation": "recipes", "action": "save", "discovery_ref": snapshot["discovery_ref"], "idempotency_key": "save-corrected"})
        self.assertTrue(retried["saved"])
        self.assertEqual(retried["library_recipe_ref"], result["library_recipe_ref"])

    def test_a_different_source_cannot_relabel_its_evidence_as_the_saved_source(self):
        other = self.app.recipes.persist_discovery(self.original, source_identity="import:v1:" + "b" * 64)
        with self.assertRaisesRegex(RecipeError, "exact saved source identity"):
            self.app.handle({"operation": "recipes", "action": "update", "recipe_id": self.saved["id"],
                             "expected_revision": 1, "discovery_ref": other["discovery_ref"]})
        with self.assertRaisesRegex(RecipeError, "not both"):
            self.app.handle({"operation": "recipes", "action": "update", "recipe_id": self.saved["id"],
                             "expected_revision": 1, "discovery_ref": self.preview["discovery_ref"], "recipe": self.original})


if __name__ == "__main__":
    unittest.main()
