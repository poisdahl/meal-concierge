"""Bounded discovery scans filter before counting and expose truthful local continuation."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from core import StateStore
from recipes import RecipeError
from service import Application
from test_recipe_source_preferences import recipe
from test_meal_concierge_products import FakeProvider


class DiscoveryBankContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(StateStore(Path(self.temp.name), {"household": "Synthetic", "provider": "oda"}), FakeProvider(), None)
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})
        with self.app.store.locked() as state:
            state["profile"]["recipes"]["sources"] = {key: key == "internal" for key in state["profile"]["recipes"]["sources"]}

    def add(self, name, *, pack=False):
        document = recipe(name)
        if pack:
            return self.app.recipes.import_pack_record(document, pack_id="synthetic-pack", recipe_id=name,
                                                       version="1", entry_origin="collection")["recipe"]
        return self.app.recipes.save(document)

    def discover(self, **kwargs):
        return self.app.handle({"operation": "recipes", "action": "discover", "query": "", "week": "2026-W47", **kwargs})

    def block(self, rows):
        with self.app.store.locked() as state:
            state["recipe_usage"]["synthetic-planned"] = {"week": "2026-W46", "status": "planned",
                "recipe_keys": [row["recipe_key"] for row in rows], "cooked_keys": [], "not_cooked_keys": []}

    def test_cooldown_rows_do_not_hide_an_eligible_older_recipe(self):
        older = self.add("Older eligible soup")
        self.block([self.add(f"Recent blocked {i}") for i in range(25)])
        result = self.discover(limit=3)
        self.assertEqual([row["id"] for row in result["recipes"]], [older["id"]])
        source = next(s for s in result["sources"] if s["source"] == "internal")
        self.assertEqual(source["status"], "ready")
        self.assertEqual(source["examined"], 26)
        self.assertTrue(result["bank_exhausted"])
        self.assertIsNone(result["next_cursor"])

    def test_a_bounded_filtered_page_is_search_limit_and_cursor_reaches_the_older_recipe(self):
        older = self.add("Older eligible soup")
        self.block([self.add(f"Recent blocked {i}") for i in range(25)])
        with mock.patch("recipe_operations.MAX_INTERNAL_DISCOVERY_ROWS", 20):
            first = self.discover(limit=3)
            self.assertEqual(first["recipes"], [])
            source = next(s for s in first["sources"] if s["source"] == "internal")
            self.assertEqual((source["status"], source["examined"]), ("search_limit", 20))
            self.assertFalse(first["bank_exhausted"])
            second = self.discover(limit=3, cursor=first["next_cursor"])
        self.assertEqual([row["id"] for row in second["recipes"]], [older["id"]])
        self.assertTrue(second["bank_exhausted"])

    def test_unused_source_slots_are_filled_and_continuation_does_not_repeat_external_search(self):
        for i in range(12):
            self.add(f"Bank soup {i}")
        empty = mock.Mock(return_value=[])
        self.app._provider_recipe_candidates = empty
        with self.app.store.locked() as state:
            state["profile"]["recipes"]["sources"]["oda"] = True
        first = self.discover(limit=8)
        self.assertEqual(len(first["recipes"]), 8)
        self.assertEqual(empty.call_count, 1)
        second = self.discover(limit=8, cursor=first["next_cursor"])
        self.assertEqual(len(second["recipes"]), 4)
        self.assertEqual(empty.call_count, 1)
        self.assertTrue(second["bank_exhausted"])
        self.assertFalse({row["id"] for row in first["recipes"]} & {row["id"] for row in second["recipes"]})
        self.assertEqual(next(s for s in second["sources"] if s["source"] == "oda")["status"], "not_requested")

    def test_source_preferences_and_unreturned_bank_candidates_survive_the_cursor(self):
        bank = self.add("Own soup")
        self.app._provider_recipe_candidates = mock.Mock(return_value=[
            recipe("Preferred discovery", kind="oda", url="https://oda.com/no/recipes/1-soup/")])
        with self.app.store.locked() as state:
            state["profile"]["recipes"]["sources"]["oda"] = True
            state["profile"]["recipes"]["preferred_sources"] = ["store"]
        first = self.discover(limit=1)
        self.assertEqual(first["recipes"][0]["discovery_source"], "oda")
        self.assertEqual(first["next_cursor"]["offset"], 0)
        second = self.discover(limit=1, cursor=first["next_cursor"])
        self.assertEqual(second["recipes"][0]["id"], bank["id"])

    def test_collection_preference_order_and_cursor_scope_are_preserved(self):
        preferred = [self.add(f"Collection soup {i}", pack=True) for i in range(2)]
        self.add("Newer own soup")
        with self.app.store.locked() as state:
            state["profile"]["recipes"]["preferred_sources"] = ["collection:synthetic-pack"]
        first = self.discover(limit=1)
        self.assertIn(first["recipes"][0]["id"], {r["id"] for r in preferred})
        second = self.discover(limit=1, cursor=first["next_cursor"])
        self.assertIn(second["recipes"][0]["id"], {r["id"] for r in preferred})
        self.assertNotEqual(first["recipes"][0]["id"], second["recipes"][0]["id"])
        for changes in ({"query": "different"}, {"week": "2026-W48"}, {"cursor": {"offset": True}}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RecipeError, "invalid full discovery bank cursor"):
                request = {"limit": 1, "cursor": first["next_cursor"], **changes}
                self.discover(**request)
        with self.app.store.locked() as state:
            state["profile"]["recipes"]["preferred_sources"] = ["own"]
        with self.assertRaisesRegex(RecipeError, "invalid full discovery bank cursor"):
            self.discover(cursor=first["next_cursor"])


if __name__ == "__main__":
    unittest.main()
