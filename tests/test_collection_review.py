"""Exact withdrawal review and private derivative export."""
import json
import sqlite3
from unittest.mock import patch
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
        import install
        with self.assertRaisesRegex(RuntimeError, "withdrawals require"):
            install.apply_recipe_pack(path, fixture.meta, expected, preparation={
                "withdrawal_consent": before["review"], "withdrawal_review": before["review"], "baseline": None})
        self.assertTrue(store.get(entry["id"])["is_favorite"])
        self.assertNotEqual(before["review"], after["review"])
        self.assertEqual((after["count"], after["favorites"], after["items"][0]["name"]), (1, 1, entry["name"]))

    def test_interrupted_bank_upgrade_preserves_exact_prior_bank_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipes.sqlite3"
            store = RecipeStore(path, "synthetic")
            store.search()
            with sqlite3.connect(path) as db:
                db.execute("DROP TABLE pack_deletions")
                db.execute("UPDATE metadata SET value='7' WHERE key='schema_version'")
            with patch.object(RecipeStore, "_schema", side_effect=RuntimeError("synthetic migration stop")):
                with self.assertRaisesRegex(RuntimeError, "synthetic migration stop"):
                    store.search()
            backup = path.with_name("recipes-v7.backup.sqlite3")
            original = backup.read_bytes()
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone(), ("7",))
            store.search()
            self.assertEqual(backup.read_bytes(), original)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone(), ("8",))

    def test_bilingual_userinfo_and_query_review_restores_each_revision(self):
        from test_recipe_languages import bilingual
        from recipe_languages import source_text_digest
        from recipe_portable import restore_private_archive
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = RecipeStore(root / 'before/recipes.sqlite3', 'synthetic')
            recipe = bilingual()
            recipe["notes"] += " https://reader:synthetic-pass@example.org:443/plate?portion=2"
            recipe["translations"]["en"]["notes"] += " https://example.org/plate?token=synthetic-token&portion=2"
            old_binding = source_text_digest(recipe)
            recipe["translations"]["en"]["source_text_digest"] = old_binding
            # Historical text in another language has no source-custody binding.
            from copy import deepcopy
            recipe["translations"]["en-GB"] = deepcopy(recipe["translations"]["en"])
            old_legacy = source_text_digest(recipe, legacy=True)
            recipe["translations"]["en-GB"]["source_text_digest"] = old_legacy
            recipe["translations"]["en-GB"]["name"] += "\nfor two"
            import recipes
            with patch.object(recipes, "_single_line", recipes._bounded_text):
                entry = original.save(recipe)
            original.update(entry["id"], 1, bilingual())
            receipt = review(original)
            self.assertEqual(receipt["affected_revisions"], 1)
            bindings = receipt["items"][0]["translation_bindings"]["en"]
            self.assertEqual(bindings["original"], old_binding)
            self.assertNotEqual(bindings["redacted"], old_binding)
            export_reviewed(original, root / 'copy.zip', receipt['review'], root / 'prior.db')
            restored = RecipeStore(root / 'after/recipes.sqlite3', 'synthetic')
            restore_private_archive(root / 'copy.zip', restored)
            with sqlite3.connect(restored.path) as db:
                rows = [json.loads(row[0]) for row in db.execute(
                    'SELECT document FROM revisions WHERE recipe_id=? ORDER BY revision',(entry['id'],))]
            from recipe_languages import recipe_presentation
            self.assertEqual(rows[0]["translations"]["en-GB"]["name"], recipe["translations"]["en-GB"]["name"])
            self.assertEqual(receipt["items"][0]["translation_bindings"]["en-GB"]["original"], old_legacy)
            self.assertEqual(rows[0]["translations"]["en-GB"]["source_text_digest"], source_text_digest(rows[0], legacy=True))
            self.assertNotEqual(recipe_presentation(rows[0], "en-GB")["resolved_language"], "en-GB")
            self.assertEqual(len(rows), 2)
            self.assertIn('https://example.org:443/plate?portion=2', rows[0]['notes'])
            self.assertIn('https://example.org/plate?portion=2', rows[0]['translations']['en']['notes'])
            self.assertEqual(rows[0]['translations']['en']['source_text_digest'], bindings['redacted'])
            self.assertEqual(rows[1], bilingual())
            for secret in ('synthetic-pass','synthetic-token'):
                self.assertNotIn(secret, json.dumps(rows) + json.dumps(receipt) + (root/'copy.zip.audit.json').read_text())

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
            audit = json.loads((root / "safe.zip.audit.json").read_bytes())
            self.assertEqual(audit["review"], receipt["review"])
            self.assertEqual(audit["revisions"], receipt["items"])
            self.assertNotIn("synthetic-secret", json.dumps(audit))
            self.assertEqual((root / "before.db").read_bytes(), before)
            self.assertEqual(store.path.read_bytes(), before)
            with open_private_archive(root / "safe.zip") as archive:
                revisions = list(archive.recipe_entries())[0]["revisions"]
                self.assertEqual([r["document"]["notes"] for r in revisions], ["Source https://example.org/page?keep=1", "Current local notes."])
