"""Household preferences between recipe sources: saved, shown and applied."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError, StateStore, recipe_source_preference
from planner import _preference_reasons
from recipe_selection import shortlist, source_group
from recipes import RecipeError, normalize_recipe
from service import Application
from test_meal_concierge_products import FakeProvider
from test_meal_concierge_recipes import external_recipe


def recipe(name, **source):
    return {"schema_version": 2, "name": name, "language": "nb", "portions": 2,
            "portions_evidence": {"basis": "user", "input": "Two servings."},
            "ingredients": [{"item": "linser", "quantity": 200, "unit": "g"}], "steps": ["Kok linsene."],
            "tags": ["Middag"], "categories": ["dinner"],
            "source": {"kind": "user", "relationship": "user_supplied", **source}, "rights": {"storage": "full"}}


class StaticSource:
    def __init__(self, kind, names):
        self.kind, self.names, self.limits = kind, names, []

    def search(self, query, limit):
        self.limits.append(limit)
        return [external_recipe(self.kind, name, f"{self.kind}-{index}", content_hash=f"{index:064x}")
                for index, name in enumerate(self.names[:limit])]


class SourcePreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "state"
        self.config = {"instance": "source-preferences", "household": "Synthetic household",
                       "provider": "oda", "profile_overrides": {}}
        self.store = StateStore(self.directory, self.config)
        self.app = Application(self.store, FakeProvider(), object(), external_recipe_sources={})
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})

    def apply(self, preferences):
        return self.app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                                "changes": {"recipe_source_preferences": preferences}})["current"]

    def saved_levels(self):
        recipes = self.store.read()["profile"]["recipes"]
        return recipes["preferred_sources"], recipes["occasional_sources"]

    def fill_bank(self):
        """Own recipes and two collections, saved so that the cookbook entries are the oldest."""
        ids = {}
        for index in range(2):
            ids[f"Cookbook {index}"] = self.app.recipes.import_pack_record(
                normalize_recipe(recipe(f"Cookbook {index}")), pack_id="family-cookbooks",
                recipe_id=f"cookbook-{index}", version="1", entry_origin="collection")["recipe"]["id"]
        for index in range(2):
            ids[f"Published {index}"] = self.app.recipes.import_pack_record(
                normalize_recipe(recipe(f"Published {index}")), pack_id="wikibooks-themealdb-en",
                recipe_id=f"published-{index}", version="1", entry_origin="bundled")["recipe"]["id"]
        for index in range(2):
            ids[f"Own {index}"] = self.app.recipes.save(recipe(f"Own {index}"), idempotency_key=f"own-{index}")["id"]
        return ids

    def bank_page(self):
        result = self.app.handle({"operation": "recipes", "action": "discover", "projection": "summary",
                                  "source": "internal", "limit": 20})
        return result, [row["name"] for row in result["recipes"]]

    def test_new_and_existing_households_start_without_preferences(self):
        self.assertEqual(self.saved_levels(), ([], []))
        # Showing the sources of an empty household does not create its recipe bank.
        rows = self.app.handle({"operation": "setup", "action": "show"})["current"]["recipe_source_overview"]
        self.assertEqual([(row["id"], row.get("recipes")) for row in rows if row["kind"] == "bank"], [("own", 0)])
        self.assertFalse(self.app.recipes.path.exists())
        # A household saved before the setting existed loads, and can still be updated.
        path = self.directory / "state.json"
        state = json.loads(path.read_text())
        for field in ("preferred_sources", "occasional_sources"):
            del state["profile"]["recipes"][field]
        path.write_text(json.dumps(state))
        reopened = StateStore(self.directory, self.config)
        self.assertEqual(reopened.read()["profile"]["recipes"]["preferred_sources"], [])
        updated = reopened.update_profile({"recipes": {"repeat_cooldown_weeks": 4}})
        self.assertEqual((updated["recipes"]["repeat_cooldown_weeks"], updated["recipes"]["occasional_sources"]), (4, []))

    def test_setup_saves_replaces_and_moves_preferences(self):
        self.apply({"preferred": ["own", "collection:family-cookbooks"], "occasional": ["themealdb", "wikibooks"]})
        self.assertEqual(self.saved_levels(), (["own", "collection:family-cookbooks"], ["themealdb", "wikibooks"]))
        # A supplied list replaces the saved one, and takes its sources out of the other level.
        self.apply({"occasional": ["own", "store"]})
        self.assertEqual(self.saved_levels(), (["collection:family-cookbooks"], ["own", "store"]))
        self.apply({"preferred": []})
        self.assertEqual(self.saved_levels(), ([], ["own", "store"]))
        profile = self.app.handle({"operation": "profile", "action": "update",
                                   "changes": {"recipes": {"occasional_sources": [], "preferred_sources": ["web"]}}})["profile"]
        self.assertEqual(recipe_source_preference(profile["recipes"], "web"), "preferred")
        self.assertEqual(recipe_source_preference(profile["recipes"], "own"), "normal")

    def test_invalid_preferences_are_rejected_and_nothing_is_saved(self):
        self.apply({"preferred": ["own"]})
        for preferences in ({"preferred": ["own"], "occasional": ["own"]}, {"preferred": ["internal"]},
                            {"preferred": ["collection:"]}, {"preferred": ["own", "own"]},
                            {"preferred": "own"}, {"preferred": [1]}, {}, {"normal": ["own"]},
                            {"preferred": ["collection:line\nbreak"]},
                            {"preferred": [f"collection:a-{index}" for index in range(9)],
                             "occasional": [f"collection:b-{index}" for index in range(8)]}):
            with self.subTest(preferences=preferences):
                with self.assertRaises(HouseholdError):
                    self.apply(preferences)
        with self.assertRaises(HouseholdError):
            self.app.handle({"operation": "profile", "action": "update",
                             "changes": {"recipes": {"occasional_sources": ["own"]}}})
        self.assertEqual(self.saved_levels(), (["own"], []))
        # A collection id is whatever pack import accepts, including a space.
        self.apply({"preferred": ["collection:My Cookbook"], "occasional": [f"collection:b-{index}" for index in range(15)]})
        self.assertEqual(self.saved_levels()[0], ["collection:My Cookbook"])

    def test_overview_lists_each_source_once_with_switch_preference_and_bank_counts(self):
        self.fill_bank()
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                         "changes": {"recipe_sources": {"wikibooks": False}}})
        current = self.apply({"preferred": ["collection:family-cookbooks"],
                              "occasional": ["themealdb", "collection:not-installed"]})
        rows = {row["id"]: row for row in current["recipe_source_overview"]}
        self.assertEqual(list(rows), ["collection:family-cookbooks", "own", "collection:wikibooks-themealdb-en",
                                      "store", "web", "collection:not-installed", "themealdb", "wikibooks"])
        self.assertEqual({key: rows["collection:family-cookbooks"][key] for key in ("kind", "enabled", "recipes", "installed", "preference")},
                         {"kind": "collection", "enabled": True, "recipes": 2, "installed": True, "preference": "preferred"})
        self.assertEqual((rows["own"]["recipes"], rows["own"]["preference"], rows["own"]["switch"]), (2, "normal", "internal"))
        self.assertEqual(rows["collection:family-cookbooks"]["switch"], "internal")
        self.assertEqual((rows["collection:not-installed"]["installed"], rows["collection:not-installed"]["recipes"]), (False, 0))
        # Only the selected store is a source; other stores are not listed.
        self.assertEqual((rows["store"]["label"], rows["store"]["switch"], rows["store"]["enabled"]), ("ODA recipes", "oda", True))
        self.assertEqual((rows["wikibooks"]["enabled"], rows["themealdb"]["preference"]), (False, "occasional"))
        self.assertEqual(rows["web"]["sites"][0], "MatPrat")
        self.assertEqual(rows["web"]["other_websites"], "only on request")
        overview = self.app.handle({"operation": "profile", "action": "overview"})["recipe_source_overview"]
        self.assertEqual(overview, current["recipe_source_overview"])
        self.assertEqual(self.app.handle({"operation": "setup", "action": "show"})["current"]["recipe_source_overview"], overview)

    def test_settings_stay_readable_when_the_recipe_bank_is_not(self):
        self.fill_bank()
        self.apply({"preferred": ["collection:family-cookbooks"]})
        with patch.object(self.app.recipes, "entry_group_counts", side_effect=RecipeError("recipe bank is unavailable")):
            shown = self.app.handle({"operation": "setup", "action": "show"})["current"]["recipe_source_overview"]
            changed = self.app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                                       "changes": {"people": 3}})["current"]
            self.app.handle({"operation": "profile", "action": "overview"})
        rows = {row["id"]: row for row in shown}
        self.assertEqual((rows["own"]["recipes"], rows["collection:family-cookbooks"]["recipes"],
                          rows["collection:family-cookbooks"]["installed"], rows["collection:family-cookbooks"]["preference"]),
                         (None, None, None, "preferred"))
        self.assertEqual(changed["people"], 3)

    def test_bank_pages_list_preferred_groups_first_and_occasional_last(self):
        self.fill_bank()
        result, names = self.bank_page()
        # No preference: most recently updated first, exactly as before.
        self.assertEqual(names, ["Own 1", "Own 0", "Published 1", "Published 0", "Cookbook 1", "Cookbook 0"])
        self.assertNotIn("source_preferences", result)
        self.assertEqual({row["source_group"] for row in result["recipes"]},
                         {"own", "collection:family-cookbooks", "collection:wikibooks-themealdb-en"})
        self.assertTrue(all("source_preference" not in row for row in result["recipes"]))
        self.apply({"preferred": ["collection:family-cookbooks"], "occasional": ["collection:wikibooks-themealdb-en", "store"]})
        result, names = self.bank_page()
        self.assertEqual(names, ["Cookbook 1", "Cookbook 0", "Own 1", "Own 0", "Published 1", "Published 0"])
        self.assertEqual(result["source_preferences"], {"preferred": ["collection:family-cookbooks"],
                                                        "occasional": ["collection:wikibooks-themealdb-en", "store"]})
        self.assertEqual([row.get("source_preference") for row in result["recipes"]],
                         ["preferred", "preferred", None, None, "occasional", "occasional"])
        # Paging keeps that order.
        first = self.app.handle({"operation": "recipes", "action": "discover", "projection": "summary",
                                 "source": "internal", "limit": 4})
        second = self.app.handle({"operation": "recipes", "action": "discover", "projection": "summary",
                                  "source": "internal", "limit": 4, "cursor": first["next_cursor"]})
        self.assertEqual([row["name"] for row in first["recipes"] + second["recipes"]], names)
        self.apply({"preferred": [], "occasional": ["own"]})
        self.assertEqual(self.bank_page()[1], ["Published 1", "Published 0", "Cookbook 1", "Cookbook 0", "Own 1", "Own 0"])

    def test_candidates_are_grouped_by_where_the_household_gets_them(self):
        self.assertEqual(source_group(recipe("Own"), entry_origin="user", pack=None), "own")
        self.assertEqual(source_group(recipe("Legacy"), entry_origin="unknown"), "own")
        self.assertEqual(source_group(recipe("Pack"), entry_origin="collection", pack={"pack_id": "family"}), "collection:family")
        # A store recipe the household saved is its own; an unsaved one is the store's.
        store = recipe("Store", kind="oda", publisher="ODA", url="https://oda.com/no/recipes/1-soup/")
        self.assertEqual(source_group(store, entry_origin="user"), "own")
        self.assertEqual(source_group(store), "store")
        for kind, group in (("themealdb", "themealdb"), ("wikibooks", "wikibooks"), ("web", "web"),
                            ("pasted_text", "own"), ("user", "own")):
            with self.subTest(kind=kind):
                self.assertEqual(source_group(recipe("Other", kind=kind)), group)

    def test_ranked_scoring_adds_a_bounded_source_reason(self):
        profile = deepcopy(self.store.read()["profile"])
        candidate = {"recipe": normalize_recipe(recipe("Soup")), "entry_origin": "collection",
                     "pack": {"pack_id": "family-cookbooks"}, "facts": {"dietary_facets": {"values": []}}}

        def source_reasons(candidate):
            return [(reason["code"], reason["weight"], reason["detail"])
                    for reason in _preference_reasons(candidate, "2026-11-16", profile) if reason["code"].startswith("source:")]

        self.assertEqual(source_reasons(candidate), [])
        profile["recipes"]["preferred_sources"] = ["collection:family-cookbooks"]
        profile["recipes"]["occasional_sources"] = ["store", "own"]
        self.assertEqual(source_reasons(candidate), [("source:preferred", 4, {"source_group": "collection:family-cookbooks"})])
        own = {**candidate, "entry_origin": "user", "pack": None}
        self.assertEqual(source_reasons(own), [("source:occasional", -4, {"source_group": "own"})])
        discovery = {"recipe": normalize_recipe(recipe("Web", kind="web", url="https://recipes.example/soup")),
                     "facts": {"dietary_facets": {"values": []}}}
        self.assertEqual(source_reasons(discovery), [])

    def test_an_automatic_plan_follows_the_saved_preference_between_bank_groups(self):
        self.fill_bank()
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                         "changes": {"recipe_sources": {"oda": False}, "web_search": {"enabled": False}}})

        def planned():
            plan = self.app.handle({"operation": "menu", "action": "plan", "planner_input": {
                "week": "2026-W47", "dates": ["2026-11-16"], "portions": 2}})["plan"]
            self.assertEqual(plan["status"], "planned")
            slot = plan["selection"]["slots"][0]
            return slot["name"], [reason["code"] for reason in slot["reason_contributions"] if reason["code"].startswith("source:")]

        for preferences, prefix, reasons in (
                ({"preferred": ["own"]}, "Own", ["source:preferred"]),
                ({"preferred": ["collection:wikibooks-themealdb-en"], "occasional": ["own"]}, "Published", ["source:preferred"]),
                ({"preferred": [], "occasional": ["own", "collection:family-cookbooks"]}, "Published", [])):
            with self.subTest(preferences=preferences):
                self.apply(preferences)
                name, source_reasons = planned()
                self.assertTrue(name.startswith(prefix), name)
                self.assertEqual(source_reasons, reasons)

    def test_equal_scores_keep_saved_recipes_ahead_of_discoveries(self):
        ids = self.fill_bank()
        state = self.store.read()
        request = {"dates": ["2026-11-16"], "portions": 2, "cooldown_overrides": {}, "as_of_date": "2026-11-16"}
        references = [{"recipe_ref": {"id": ids[name], "revision": 1}} for name in ("Own 0", "Own 1", "Cookbook 0", "Cookbook 1")]
        estimate = {"basis": "estimate", "input": "200 g linser", "assumptions": "Synthetic fixture amount."}
        for index in range(8):
            discovered = recipe(f"Discovery {index}", kind="web", url=f"https://recipes.example/{index}",
                                relationship="original")
            discovered["ingredients"][0]["evidence"] = {"quantity": dict(estimate), "unit": dict(estimate)}
            discovered["portions_evidence"] = {"basis": "source", "input": "Two servings."}
            snapshot = self.app.recipes.persist_discovery(normalize_recipe(discovered))
            references.append({"discovery_ref": snapshot["discovery_ref"]})
        candidates = self.app._resolve_planner_candidates({**request, "candidates": references}, state)
        chosen = shortlist(candidates, request, state["profile"])
        self.assertEqual(chosen["suitable_count"], 12)
        kept = [candidate["recipe"]["name"] for candidate in chosen["candidates"]]
        self.assertEqual(len(kept), chosen["capacity"])
        self.assertLessEqual({"Own 0", "Own 1", "Cookbook 0", "Cookbook 1"}, set(kept))

    def test_cross_source_discovery_lists_preferred_sources_first_and_occasional_last(self):
        self.fill_bank()
        meal_db, wikibooks = StaticSource("themealdb", [f"MealDB {i}" for i in range(6)]), StaticSource("wikibooks", [f"Wiki {i}" for i in range(6)])
        self.app.external_recipe_sources = {"themealdb": meal_db, "wikibooks": wikibooks}
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                         "changes": {"recipe_sources": {"oda": False}}})
        self.apply({"preferred": ["wikibooks"], "occasional": ["themealdb", "store"]})
        result = self.app.handle({"operation": "recipes", "action": "discover", "limit": 9})
        # Every source is asked for the same share; only the order follows the preference.
        self.assertEqual(result["balanced_limit_per_source"], 3)
        self.assertEqual((wikibooks.limits, meal_db.limits), ([3], [3]))
        names = [row["name"] for row in result["recipes"]]
        self.assertEqual(names[:3], ["Wiki 0", "Own 1", "MealDB 0"])
        self.assertEqual(len(names), 9)
        statuses = {row["source"]: row.get("preference") for row in result["sources"]}
        # A switched-off or non-selected store carries no preference label.
        self.assertEqual(statuses, {"internal": None, "oda": None, "meny": None, "mathem": None,
                                    "themealdb": "occasional", "wikibooks": "preferred"})
        self.assertEqual(result["source_preferences"], {"preferred": ["wikibooks"], "occasional": ["themealdb", "store"]})
        # The overall limit cuts the occasional source first.
        short = self.app.handle({"operation": "recipes", "action": "discover", "limit": 2})
        self.assertEqual([row["name"] for row in short["recipes"]], ["Wiki 0", "Own 1"])
        # An occasional source that is the only one switched on still fills the result.
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                         "changes": {"recipe_sources": {"internal": False, "wikibooks": False}}})
        alone = self.app.handle({"operation": "recipes", "action": "discover", "limit": 5})
        self.assertEqual([row["name"] for row in alone["recipes"]], [f"MealDB {i}" for i in range(5)])


if __name__ == "__main__":
    unittest.main()
