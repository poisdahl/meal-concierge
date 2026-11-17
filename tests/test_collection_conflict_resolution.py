"""Exact archive/revision dispositions resolve local edits without an automatic merge."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recipes import RecipeError, RecipeStore, normalize_recipe, source_ingredient
from recipe_portable import (canonical_bytes, write_archive, apply_archive, resolve_archive_conflict,
                             export_private_archive, restore_private_archive)
import install


class CollectionResolutionTests(unittest.TestCase):
    pack_id = install.RECIPE_PACK['pack_id']

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.state = self.root / 'state'; self.state.mkdir()
        self.store = RecipeStore(self.state / 'recipes.sqlite3', 'synthetic')
        self.recipe = normalize_recipe({'schema_version': 2, 'name': 'Synthetic lentils', 'portions': 2,
            'portions_evidence': {'basis': 'source', 'input': '2 servings'},
            'ingredients': [source_ingredient('200 g lentils')], 'steps': ['Simmer.'],
            'source': {'kind': 'user', 'relationship': 'user_supplied'}, 'rights': {'storage': 'full'}})
        first = self.store.import_pack_record(self.recipe, pack_id=self.pack_id, recipe_id='one', version='1')['recipe']
        self.local = self.store.update(first['id'], first['revision'], {**self.recipe, 'notes': 'Keep my local cooking note.'})
        self.store.set_favorite(self.local['library_recipe_ref'], True, idempotency_key='favorite')

    def pack(self, version='2', status='ready'):
        recipe = {**self.recipe, 'name': 'Publisher lentils ' + version, 'steps': ['Simmer gently.']}
        manifest = {'format': 'meal-concierge-recipes', 'format_version': 1, 'kind': 'bundled',
                    'pack_id': self.pack_id, 'pack_version': version, 'normalizer_version': '3',
                    'recipe_schema_version': 2, 'records_count': 1}
        data = self.root / ('records-' + version)
        data.write_bytes(canonical_bytes({'recipe_id': 'one', 'status': status, 'recipe': recipe}) + b'\n')
        archive = self.root / ('pack-' + version + '.zip')
        write_archive(archive, manifest, {'records.jsonl': data})
        expected = {k: manifest[k] for k in ('format', 'format_version', 'kind', 'pack_id', 'pack_version', 'normalizer_version', 'recipe_schema_version')}
        expected.update(bytes=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        return archive, expected

    def resolve(self, archive, expected, disposition, revision=None):
        return resolve_archive_conflict(archive, self.state, 'synthetic', expected,
            pack_recipe_id='one', expected_revision=revision or self.local['revision'], disposition=disposition)

    def test_keep_local_completes_only_the_exact_reviewed_artifact(self):
        archive, expected = self.pack()
        self.assertEqual(apply_archive(archive, self.state, 'synthetic', expected)['conflicts'], 1)
        self.assertTrue(self.resolve(archive, expected, 'keep_local')['resolved'])
        for _ in range(2):
            report = apply_archive(archive, self.state, 'synthetic', expected)
            self.assertEqual((report['status'], report['conflicts']), ('complete', 0))
            self.assertEqual(report['results'][0]['resolution'], 'keep_local')
        saved = self.store.get(self.local['id'])
        self.assertEqual((saved['notes'], saved['revision']), (self.local['notes'], self.local['revision']))
        self.assertTrue(saved['is_favorite'])
        newer, descriptor = self.pack('3')
        self.assertEqual(apply_archive(newer, self.state, 'synthetic', descriptor)['conflicts'], 1)

    def test_deleted_collection_entry_survives_future_import_and_private_restore(self):
        before = self.root / "before.zip"
        export_private_archive(before, self.store)
        self.store.delete(self.local["id"], self.local["revision"])
        after = self.root / "after.zip"
        self.assertEqual(export_private_archive(after, self.store)["private_schema_version"], 4)
        with tempfile.TemporaryDirectory() as temp:
            restored = RecipeStore(Path(temp) / "recipes.sqlite3", "synthetic")
            self.assertEqual(restore_private_archive(after, restored)["deletions_created"], 1)
            self.assertEqual(restore_private_archive(after, restored)["deletions_unchanged"], 1)
            self.assertEqual(restore_private_archive(before, restored)["conflicts"], 1)
            restored.delete_absent_pack_records(pack_id=self.pack_id, present_recipe_ids=[])
            archive, expected = self.pack()
            result = apply_archive(archive, restored.path.parent, "synthetic", expected)
            self.assertEqual((result["status"], result["kept_deleted"]), ("complete", 1))
            identity = result["results"][0]["deletion_id"]
            with self.assertRaises(RecipeError):
                restored.allow_pack_reintroduction("wrong-pack", "one", identity)
            restored.allow_pack_reintroduction(self.pack_id, "one", identity)
            self.assertEqual(apply_archive(archive, restored.path.parent, "synthetic", expected)["created"], 1)
            self.assertEqual(restore_private_archive(after, restored)["conflicts"], 1)
            self.assertEqual(len(restored.search()), 1)

    def test_later_local_edit_invalidates_keep_local_and_stale_review_cannot_resolve(self):
        archive, expected = self.pack()
        self.resolve(archive, expected, 'keep_local')
        changed = self.store.update(self.local['id'], self.local['revision'], {**self.recipe, 'notes': 'New local edit.'})
        self.assertEqual(apply_archive(archive, self.state, 'synthetic', expected)['conflicts'], 1)
        with self.assertRaisesRegex(RecipeError, 'changed after review'):
            self.resolve(archive, expected, 'adopt_publisher')
        self.assertEqual(self.store.get(changed['id'])['notes'], 'New local edit.')

    def test_adopt_publisher_preserves_identity_history_favorite_and_local_archive(self):
        self.local = self.store.archive(self.local['id'], self.local['revision'])
        archive, expected = self.pack()
        result = self.resolve(archive, expected, 'adopt_publisher')
        saved = self.store.get(self.local['id'])
        self.assertEqual((saved['id'], saved['status'], saved['revision']),
                         (self.local['id'], 'archived', self.local['revision'] + 1))
        self.assertEqual(saved['steps'], ['Simmer gently.'])
        self.assertTrue(saved['is_favorite'])
        self.assertEqual(self.store.get(saved['id'], self.local['revision'])['notes'], self.local['notes'])
        self.assertEqual(apply_archive(archive, self.state, 'synthetic', expected)['status'], 'complete')

    def test_adopt_publisher_respects_a_new_draft_without_a_local_status_override(self):
        archive, expected = self.pack(status='draft')
        self.resolve(archive, expected, 'adopt_publisher')
        self.assertEqual(self.store.get(self.local['id'])['status'], 'draft')

    def test_changed_archive_and_absent_record_cannot_resolve(self):
        archive, expected = self.pack()
        wrong = {**expected, 'sha256': 'f' * 64}
        with self.assertRaises(RecipeError):
            self.resolve(archive, wrong, 'adopt_publisher')
        with self.assertRaisesRegex(RecipeError, 'absent'):
            resolve_archive_conflict(archive, self.state, 'synthetic', expected,
                pack_recipe_id='different', expected_revision=self.local['revision'], disposition='keep_local')
        self.assertEqual(self.store.get(self.local['id'])['notes'], self.local['notes'])

    def test_recipe_deletion_removes_only_its_resolution_workflow_receipt(self):
        archive, expected = self.pack()
        self.resolve(archive, expected, 'keep_local')
        self.store.delete_absent_pack_records(pack_id=self.pack_id, present_recipe_ids=[])
        with self.store._connection() as connection:
            self.assertFalse(connection.execute("SELECT value FROM metadata WHERE key LIKE 'pack-resolution:%'").fetchall())

    def test_private_recipe_restore_preserves_edit_but_requires_a_new_disposition(self):
        archive, expected = self.pack()
        self.resolve(archive, expected, 'keep_local')
        private = self.root / 'private.zip'
        export_private_archive(private, self.store)
        state = self.root / 'restored'; state.mkdir()
        restored = RecipeStore(state / 'recipes.sqlite3', 'synthetic'); restored.search()
        self.assertEqual(restore_private_archive(private, restored)['status'], 'complete')
        self.assertEqual(restored.get(self.local['id'])['notes'], self.local['notes'])
        self.assertEqual(apply_archive(archive, state, 'synthetic', expected)['conflicts'], 1)

    def test_installer_resolves_prepared_bytes_only_while_stopped(self):
        archive, expected = self.pack()
        expected['url'] = f'https://github.com/poisdahl/meal-concierge/releases/download/recipes-2/meal-concierge-recipes-2.zip'
        home = self.root / 'home'; home.mkdir()
        release = self.root / 'release'; (release / 'venv/bin').mkdir(parents=True)
        (release / 'venv/bin/python').symlink_to(sys.executable)
        for path in Path(install.__file__).parent.glob('*.py'):
            (release / path.name).symlink_to(path)
        settings = {'household': 'synthetic', 'provider': 'none'}
        install.write_json(home / 'config.json', settings)
        meta = {'home': str(home), 'release': str(release), 'code_root': str(self.root / 'code'),
                'name': 'synthetic-resolver', 'manager': 'external',
                'paths': {'config': str(home / 'config.json'), 'state': str(self.state), 'socket': str(home / 'service.sock')}}
        install.write_json(home / 'runtime.json', meta)
        directory = home / 'recipe-preparations' / expected['sha256']; directory.mkdir(parents=True)
        shutil.copyfile(archive, directory / 'archive.zip')
        install.write_json(directory / 'receipt.json', {'format': install.RECIPE_PREPARATION_VERSION, 'expected': expected,
            'release': str(release), 'state': str(self.state.resolve()), 'baseline': None})
        argv = ['install.py', 'resolve-recipe-conflict', '--home', str(home), '--prepared', expected['sha256'],
                '--pack-recipe-id', 'one', '--expected-revision', str(self.local['revision']), '--resolution', 'keep_local']
        with install.data_ownership(meta), patch.object(sys, 'argv', argv):
            with self.assertRaises(RuntimeError):
                install.main()
        with patch.object(sys, 'argv', argv), patch('sys.stdout', new_callable=io.StringIO) as output:
            install.main()
        self.assertEqual(json.loads(output.getvalue())['disposition'], 'keep_local')
        self.assertEqual(apply_archive(archive, self.state, 'synthetic', expected)['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
