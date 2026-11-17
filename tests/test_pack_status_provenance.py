"""Publisher status changes are not mistaken for household decisions."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipe_portable import export_private_archive, restore_private_archive
from recipes import RecipeStore, normalize_recipe


class PackStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RecipeStore(self.root / 'source/recipes.sqlite3', 'synthetic')
        self.recipe = normalize_recipe({'schema_version': 2, 'name': 'Synthetic lentils', 'portions': 2,
            'ingredients': ['200 g lentils'], 'steps': ['Simmer.'],
            'source': {'kind': 'user', 'relationship': 'user_supplied'}, 'rights': {'storage': 'full'}})

    def apply(self, version, status='ready', recipe=None, store=None):
        return (store or self.store).import_pack_record(recipe or self.recipe,
            pack_id='synthetic-pack', recipe_id='lentils', version=str(version), status=status)['recipe']

    def test_repeated_publisher_status_changes_follow_the_publisher(self):
        first = self.apply(1)
        for version, status, expected in [(2, 'draft', 'draft'), (3, 'ready', 'active'), (4, 'draft', 'draft')]:
            saved = self.apply(version, status)
            self.assertEqual(saved['status'], expected)
            self.assertEqual(saved['id'], first['id'])
        changed = deepcopy(self.recipe)
        changed['steps'] = ['Simmer gently.']
        self.assertEqual(self.apply(5, recipe=changed)['status'], 'active')

    def test_a_local_status_choice_remains_across_publisher_updates(self):
        saved = self.apply(1)
        saved = self.apply(2, 'draft')
        saved = self.store.update(saved['id'], saved['revision'], self.recipe, status='active')
        self.assertEqual(self.apply(3, 'draft')['status'], 'active')
        changed = deepcopy(self.recipe)
        changed['steps'] = ['Simmer gently.']
        self.assertEqual(self.apply(4, 'draft', changed)['status'], 'active')

    def test_status_provenance_survives_private_roundtrip(self):
        saved = self.apply(1)
        saved = self.apply(2, 'draft')
        output = self.root / 'synthetic.zip'
        self.assertEqual(export_private_archive(output, self.store)['private_schema_version'], 3)
        target = RecipeStore(self.root / 'target/recipes.sqlite3', 'synthetic')
        target.search()
        self.assertEqual(restore_private_archive(output, target)['status'], 'complete')
        self.assertEqual(restore_private_archive(output, target)['unchanged'], 1)
        self.assertEqual(self.apply(3, store=target)['status'], 'active')
        self.assertEqual(target.get(saved['id'])['revision'], saved['revision'] + 1)

    def test_a_local_status_choice_with_a_content_edit_remains_when_reverted(self):
        saved = self.apply(1)
        changed = deepcopy(self.recipe)
        changed['notes'] = 'Local note.'
        saved = self.store.update(saved['id'], saved['revision'], changed, status='draft')
        self.store.update(saved['id'], saved['revision'], self.recipe)
        self.assertEqual(self.apply(2)['status'], 'draft')

    def test_legacy_status_history_remains_conservative(self):
        saved = self.apply(1)
        saved = self.apply(2, 'draft')
        with sqlite3.connect(self.store.path) as connection:
            connection.execute('DROP TABLE recipe_pack_status')
            connection.execute("UPDATE metadata SET value='6' WHERE key='schema_version'")
        before = self.store.get(saved['id'])
        self.assertEqual(before['status'], 'draft')
        self.assertEqual(self.apply(3)['status'], 'draft')
        self.assertEqual(self.apply(4)['status'], 'draft')

    def test_withdrawal_removes_provenance_for_only_that_recipe(self):
        saved = self.apply(1)
        self.store.delete_absent_pack_records(pack_id='synthetic-pack', present_recipe_ids=[])
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM recipe_pack_status').fetchone()[0], 0)
        replacement = self.apply(2)
        self.assertNotEqual(saved['id'], replacement['id'])


if __name__ == '__main__':
    unittest.main()
