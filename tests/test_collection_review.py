"""Exact withdrawal review and private derivative export."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from recipe_export_remediation import review, export_reviewed
from recipe_portable import open_private_archive, withdrawal_preview, export_private_archive
from recipes import RecipeError, RecipeStore


class CollectionReviewTests(unittest.TestCase):
    def test_withdrawal_preview_changes_when_a_favorite_changes(self):
        from test_recipe_preparation import PreparationTests
        fixture = PreparationTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        path, expected = fixture.pack()
        store = RecipeStore(fixture.home / "state/recipes.sqlite3", fixture.config["household"])
        entry = store.import_pack_record({"name": "Synthetic local plate", "portions": 2, "steps": ["Chop."],
            "ingredients": ["2 carrots"], "source": {"kind": "user", "relationship": "user_supplied"}, "rights": {"storage": "full"}},
            pack_id=expected["pack_id"], recipe_id="old", version="0")["recipe"]
        before = withdrawal_preview(path, store.path.parent, store.household, expected)
        store.set_favorite(entry["library_recipe_ref"], True, idempotency_key="star")
        after = withdrawal_preview(path, store.path.parent, store.household, expected)
        self.assertNotEqual(before["review"], after["review"])
        self.assertEqual((after["count"], after["favorites"], after["items"][0]["name"]), (1, 1, entry["name"]))

    def test_reviewed_redacted_copy_preserves_history_and_its_private_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RecipeStore(root / "bank/recipes.sqlite3", "synthetic")
            recipe = {"schema_version": 2, "name": "Synthetic", "portions": 2, "ingredients": ["2 carrots"],
                      "steps": ["Chop."], "notes": "Source https://example.org/page?keep=1&token=synthetic-secret",
                      "source": {"kind": "user", "relationship": "user_supplied"}, "rights": {"storage": "full"}}
            entry = store.save(recipe)
            store.update(entry["id"], 1, {**recipe, "notes": "Current local notes."})
            before = store.path.read_bytes()
            with self.assertRaises(RecipeError):
                export_private_archive(root / "unsafe.zip", store)
            receipt = review(store)
            self.assertEqual(receipt["affected_revisions"], 1)
            self.assertNotIn("secret", json.dumps(receipt))
            with self.assertRaises(RecipeError):
                export_reviewed(store, root / "safe.zip", "stale", root / "before.db")
            self.assertFalse((root / "before.db").exists())
            export_reviewed(store, root / "safe.zip", receipt["review"], root / "before.db")
            self.assertEqual((root / "before.db").read_bytes(), before)
            self.assertEqual(store.path.read_bytes(), before)
            with open_private_archive(root / "safe.zip") as archive:
                revisions = list(archive.recipe_entries())[0]["revisions"]
                self.assertEqual([r["document"]["notes"] for r in revisions], ["Source https://example.org/page?keep=1", "Current local notes."])
