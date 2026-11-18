"""A missing reset scope must not clear a household's explicit exclusions."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore
from service import Application


class ProfileResetScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name), {"household": "Synthetic reset scope", "provider": "none"})
        self.app = Application(self.store, None, None)
        self.store.update_profile({"meals": {"portions": 4}, "diet": {
            "rules": [{"kind": "allergy", "term": "egg"}], "avoid": ["cream"]}})

    def test_missing_empty_or_ambiguous_scope_rejects_without_writing(self):
        before = self.store.path.read_bytes()
        for paths in (None, [], ["*", "meals.portions"], [""], ["meals.portions", "meals.portions"]):
            with self.subTest(paths=paths), self.assertRaises(HouseholdError):
                self.app.handle({"operation": "profile", "action": "reset", "paths": paths})
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_targeted_reset_returns_exact_previous_values_and_keeps_other_rules(self):
        before = self.store.read()["profile"]
        result = self.app.handle({"operation": "profile", "action": "reset", "paths": ["meals.portions"]})
        self.assertEqual(result["previous_values"], {"meals.portions": 4})
        self.assertEqual(result["profile"]["meals"]["portions"], 2)
        self.assertEqual(result["profile"]["diet"], before["diet"])

    def test_whole_profile_reset_requires_the_explicit_single_wildcard(self):
        before = self.store.read()["profile"]
        result = self.app.handle({"operation": "profile", "action": "reset", "paths": ["*"]})
        self.assertEqual(result["previous_values"], {"*": before})
        self.assertEqual(result["profile"]["diet"]["rules"], [])
        self.assertEqual(result["profile"]["diet"]["avoid"], [])
        self.assertEqual(result["profile"]["meals"]["portions"], 2)

    def test_invalid_path_leaves_the_complete_profile_untouched(self):
        before = self.store.path.read_bytes()
        with self.assertRaises(HouseholdError):
            self.store.reset_profile(["meals.portions", "does.not.exist"])
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
