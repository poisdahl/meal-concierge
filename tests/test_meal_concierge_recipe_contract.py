"""Version/evidence acceptance through the production recipe and shopping paths."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import StateStore
from recipes import (ESTIMATE_CONFIRMATION, RecipeError, RecipeStore, normalize_recipe,
                     prepare_recipe_input, recipe_digest, scale_recipe, source_ingredient, source_yield,
                     normalize_source_url, normalize_attribution_url)
from recipe_quantities import read_quantity, parse_measure
from product_planner import menu_requirements
from recipe_library_mealie import MealieAdapter
from recipe_library_recipesage import RecipeSageAdapter
from recipe_libraries import RecipeLibraryError
from recipe_sources import TheMealDBSource, provider_recipe_candidates
from service import Application
from service_common import menu_email_html
from test_meal_concierge_products import FakeProvider, product, option, observation
from test_meal_concierge_migration import Library as MigrationLibrary

# Generated once with the actual e9b113e5 implementation, never with the current
# normalizer. These hashes also cover the legacy discovery digest protocol.
LEGACY = '''{"ingredients":[{"amount":"700 g","item":"mel","notes":null,"optional":false,"pantry":false,"quantity":700.0,"raw":"700 g mel","scalable":true,"unit":"g"}],"language":"nb-NO","name":"Legacy literal","notes":null,"portions":12.0,"reheating":null,"rights":{"credit":null,"license":null,"license_url":null,"storage":"full"},"schema_version":1,"source":{"author":null,"external_id":null,"kind":"user","publisher":null,"relationship":"user_supplied","title":null,"url":"https://example.org/old"},"steps":["Bake at 180 C for 30 minutes."],"storage":null,"tags":[],"times":null}'''
LEGACY_DIGEST = "e5204a584d0741858cc8ca626f352e234e0449ebf0d7ed47dea05bbf354ffe69"
LEGACY_SNAPSHOT = ("d29be6a00b445b5bbd2579714668396e074ec9066df845e03908d114f33f0caf", "1694957c1720e83cbff663c7ffbeb69d509c5638e7b3060768b0f54429b6c287", "139f9785d96246d35067e231cb99bcffbaa3fc6da22c207ddf09414099279e56")


def authored_recipe():
    return {
        "schema_version": 2, "name": "Synthetic five needs", "portions": 12,
        "source": {"kind": "user", "relationship": "user_supplied"},
        "rights": {"storage": "full"},
        "ingredients": [{"item": item, "raw": f"Source wording: {amount} {unit} {item}", "quantity": amount, "unit": unit}
                        for item, amount, unit in [("salt", 3, "ts"), ("olje", 6, "ss"), ("mel", 700, "g"), ("gulrot", 1200, "g"), ("melk", 1200, "ml")]],
        "steps": ["Bake at 180 C for 30 minutes."],
    }


class QuantityProvider(FakeProvider):
    def call(self, name, arguments, **kwargs):
        if name == "product_search":
            self.calls.append((name, deepcopy(arguments)))
            query = arguments["queries"][0]
            unit = "g" if query in {"mel", "gulrot"} else "ml"
            return observation(query, [product(query, query, 500, unit, [option(1000)])])
        return super().call(name, arguments, **kwargs)


class RecipeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name), {"instance": "contract", "household": "Synthetic", "profile_overrides": {}})
        self.provider = QuantityProvider()
        self.app = Application(self.store, self.provider, object())
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})

    def tearDown(self):
        self.temp.cleanup()

    def save(self, recipe, key="save"):
        return self.app.handle({"operation": "recipes", "action": "save", "recipe": recipe, "idempotency_key": key})["recipe"]

    def save_menu(self, recipe):
        return self.app.handle({"operation": "menu", "action": "save", "menu": {"week": "2026-W40", "dishes": [recipe], "salads": []}})["menu"]

    def test_native_source_twelve_to_two_all_five_reach_application_products(self):
        fixture = json.loads((ROOT / "tests/fixtures/mealie/v3.24.0.json").read_text())["recipe_get"]
        fixture["recipeServings"] = 12
        fixture["recipeYieldQuantity"] = 12
        fixture["recipeYield"] = "servings"
        fixture["orgURL"] = "https://example.org/recipe?id=42&oldid=9&utm_source=fixture"
        fixture["recipeIngredient"] = [{"originalText": f"{amount} {unit} {item}"} for item, amount, unit in [("salt", 3, "ts"), ("olje", 6, "ss"), ("mel", 700, "g"), ("gulrot", 1200, "g"), ("melk", 1200, "ml")]]
        adapter = MealieAdapter({"library_id": "fixture-mealie", "provider": "mealie", "base_url": "https://recipes.example", "read_only": True}, {"token": "synthetic"})
        native = adapter._mapped_recipe(fixture)
        self.assertEqual(native["portions_evidence"]["basis"], "source")
        frozen = self.app.recipes.persist_discovery(native)
        saved = self.app.handle({"operation": "recipes", "action": "save", "discovery_ref": frozen["discovery_ref"], "idempotency_key": "source-save"})["recipe"]
        menu = self.save_menu({"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}, "portions": 2})
        scaled = menu["dishes"][0]
        self.assertEqual(scaled["ingredients"][2]["quantity"], {"numerator": 350, "denominator": 3})
        self.assertEqual(scaled["ingredients"][2]["original_text"], "700 g mel")
        self.assertEqual(scaled["source"]["url"], "https://example.org/recipe?id=42&oldid=9")
        requirements, unresolved = menu_requirements(menu)
        self.assertEqual(len(requirements), 5)
        self.assertEqual(unresolved, [])
        prepared = self.app.handle({"operation": "products", "action": "prepare", "menu_ref": self.app._cart_menu_ref(menu), "candidate_approvals": [{"requirement_id": r["requirement_id"], "candidate_refs": [r["item"]]} for r in requirements]})["product_plan"]
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(len(prepared["requirements"]), 5)
        self.assertTrue(all(name == "product_search" for name, _ in self.provider.calls))
        restarted = Application(self.store, self.provider, object())
        reread = restarted.handle({"operation": "recipes", "action": "get", "recipe_id": saved["id"]})["recipe"]
        self.assertEqual(normalize_recipe(reread), native)
        self.assertEqual(restarted.recipes.resolve_discovery(frozen["discovery_ref"])["recipe"], native)

    def test_literal_old_document_and_discovery_digests_survive_restart(self):
        old = json.loads(LEGACY)
        self.assertEqual(recipe_digest(old), LEGACY_DIGEST)
        self.assertEqual(RecipeStore._snapshot_parts(old)[1:], LEGACY_SNAPSHOT)
        saved = self.save(old)
        frozen = self.app.recipes.persist_discovery(old)
        self.save_menu({"recipe_ref": {"id": saved["id"], "revision": 1}, "portions": 2})
        restarted = Application(self.store, self.provider, object())
        self.assertEqual(normalize_recipe(restarted.recipes.get(saved["id"], 1)), old)
        self.assertEqual(restarted.recipes.resolve_discovery(frozen["discovery_ref"])["recipe"], old)
        with sqlite3.connect(self.app.recipes.path) as connection:
            self.assertEqual(connection.execute("SELECT document FROM revisions WHERE recipe_id=?", (saved["id"],)).fetchone()[0], LEGACY)
        historical = restarted.store.read()["menu"]
        self.assertEqual(menu_requirements(historical)[0][0]["quantity"], {"numerator": 350, "denominator": 3})

    def test_missing_servings_and_two_loaves_never_become_household_people(self):
        for wording in (None, "2 loaves"):
            recipe = authored_recipe()
            recipe.pop("portions")
            if wording:
                recipe["yield"] = {"original_text": wording, "quantity": 2, "unit": "loaves"}
            menu = self.save_menu(recipe)
            self.assertIsNone(menu["dishes"][0]["portions"])
            needs, unresolved = menu_requirements(menu)
            self.assertEqual(needs, [])
            self.assertEqual({r["reason"] for r in unresolved}, {"person_servings_unknown"})
            self.assertIn("personporsjoner er ukjent", menu_email_html(menu))
        self.assertEqual(self.save_menu(authored_recipe())["dishes"][0]["portions"], 12)

    def test_estimate_acceptance_is_exact_persisted_and_cannot_be_copied_or_reassigned(self):
        recipe = authored_recipe()
        recipe["portions_evidence"] = {"basis": "estimate", "input": "2 loaves", "assumptions": "Six slices per loaf, one slice per person."}
        saved = self.save(recipe)
        with self.assertRaisesRegex(RecipeError, "acceptance"):
            self.save_menu({"recipe_ref": {"id": saved["id"], "revision": 1}, "portions": 2})
        request = {"operation": "recipes", "action": "accept_estimates", "recipe_id": saved["id"], "expected_revision": 1, "recipe_digest": saved["recipe_digest"], "estimate_fields": ["portions"], "confirmation_statement": ESTIMATE_CONFIRMATION, "idempotency_key": "estimate"}
        with self.assertRaisesRegex(RecipeError, "digest"):
            self.app.handle({**request, "recipe_digest": "0" * 64})
        accepted = self.app.handle(request)["recipe"]
        self.assertEqual(self.app.handle(request)["recipe"]["revision"], 2)
        restarted = Application(self.store, self.provider, object())
        self.assertEqual(restarted.recipes.get(saved["id"])["portions_evidence"]["basis"], "estimate")
        menu = self.save_menu({"recipe_ref": {"id": saved["id"], "revision": 2}, "portions": 2})
        self.assertIn("godkjent anslag", menu_email_html(menu))
        self.assertEqual(menu["dishes"][0]["ingredients"][2]["evidence"]["quantity"]["calculation"]["factor"], {"numerator": 1, "denominator": 6})
        with self.assertRaisesRegex(RecipeError, "service-owned"):
            self.save(accepted, "copied-acceptance")
        changed = deepcopy(accepted)
        changed["portions"] = 99
        with self.assertRaisesRegex(RecipeError, "service-owned"):
            self.app.handle({"operation": "recipes", "action": "update", "recipe_id": saved["id"], "expected_revision": 2, "recipe": changed})

    def test_unknown_units_and_mass_volume_stay_unresolved_and_fractions_aggregate(self):
        recipe = authored_recipe()
        recipe["ingredients"] = [{"item": "mel", "quantity": 700, "unit": "g"}]
        scaled = scale_recipe(normalize_recipe(recipe), 2)
        needs, unresolved = menu_requirements({"dishes": [scaled] * 6, "salads": []})
        self.assertEqual(needs[0]["quantity"], {"numerator": 700, "denominator": 1})
        self.assertEqual(unresolved, [])
        recipe["ingredients"][0]["unit"] = "cup"
        needs, unresolved = menu_requirements({"dishes": [scale_recipe(normalize_recipe(recipe), 2)], "salads": []})
        self.assertEqual(needs, [])
        self.assertEqual(unresolved[0]["reason"], "quantity_or_unit_unresolved")
        recipe["ingredients"] = [{"item": "mel", "quantity": 1, "unit": "g"}, {"item": "mel", "quantity": 1, "unit": "ml"}]
        needs, _ = menu_requirements({"dishes": [scale_recipe(normalize_recipe(recipe), 2)], "salads": []})
        self.assertEqual({n["unit"] for n in needs}, {"ml", "g"})

    def test_missing_image_binding_and_source_claims_fail_without_mutation(self):
        recipe = authored_recipe()
        recipe["image"] = {"asset_id": "sha256:" + "a" * 64, "credit": "Independent cover credit"}
        normalized = normalize_recipe(recipe)
        self.assertEqual(normalize_recipe(normalized), normalized)
        self.assertEqual(menu_requirements({"dishes": [scale_recipe(normalized, 2)], "salads": []}), menu_requirements({"dishes": [scale_recipe(normalize_recipe(authored_recipe()), 2)], "salads": []}))
        with self.assertRaisesRegex(RecipeError, "available managed asset"):
            self.save(recipe)
        self.assertEqual(self.app.recipes.search(), [])
        recipe = authored_recipe()
        recipe["portions_evidence"] = {"basis": "source", "input": "claimed source says twelve"}
        with self.assertRaisesRegex(RecipeError, "trusted source"):
            self.save(recipe)
        recipe = authored_recipe()
        recipe["source_provider"] = "oda"
        saved = self.save(recipe)
        changed = deepcopy(saved)
        changed["source_provider"] = None
        with self.assertRaisesRegex(RecipeError, "preserve"):
            self.app.handle({"operation": "recipes", "action": "update", "recipe_id": saved["id"], "expected_revision": 1, "recipe": changed})

    def test_managed_cover_bank_menu_product_path_and_missing_cover_retries(self):
        from test_meal_concierge_recipe_assets import picture
        from recipe_assets import asset_filename
        recipe = authored_recipe()
        image_id = self.app.recipes.assets.import_bytes(picture())
        recipe["rights"]["credit"] = "Independent recipe text credit"
        recipe["image"] = {"asset_id": image_id, "credit": "Independent photo credit"}
        recipe["entry_origin"] = "bundled"
        saved = self.save(recipe)
        self.assertEqual(saved["entry_origin"], "user")
        reference = {"recipe_ref": {"id": saved["id"], "revision": 1}, "portions": 2}
        menu = self.save_menu(reference)
        self.assertEqual(menu["dishes"][0]["image"]["asset_id"], image_id)
        requirements, unresolved = menu_requirements(menu)
        self.assertEqual((len(requirements), unresolved), (5, []))
        prepared = self.app.handle({"operation": "products", "action": "prepare", "menu_ref": self.app._cart_menu_ref(menu), "candidate_approvals": [{"requirement_id": r["requirement_id"], "candidate_refs": [r["item"]]} for r in requirements]})["product_plan"]
        self.assertEqual(prepared["status"], "prepared")
        changed = deepcopy(saved)
        replacement = self.app.recipes.assets.import_bytes(picture(color="blue"))
        changed["image"]["asset_id"] = replacement
        update = {"operation": "recipes", "action": "update", "recipe_id": saved["id"], "expected_revision": 1, "recipe": changed, "idempotency_key": "replace-cover"}
        self.assertEqual(self.app.handle(update)["recipe"]["revision"], 2)
        for asset_id in (image_id, replacement):
            (self.app.recipes.assets.root / asset_filename(asset_id)).unlink()
        self.assertEqual(self.save(recipe)["id"], saved["id"])
        self.assertEqual(self.app.handle(update)["recipe"]["revision"], 2)
        historical_menu = self.save_menu(reference)
        self.assertEqual(historical_menu["dishes"][0]["image"]["asset_id"], image_id)
        html = menu_email_html(historical_menu)
        self.assertIn("Independent recipe text credit", html)
        self.assertIn("Independent photo credit", html)
        self.assertNotIn("<img", html)

    def test_inline_menu_attachment_validation_preserves_completed_missing_cover_replay(self):
        from test_meal_concierge_recipe_assets import picture
        from recipe_assets import asset_filename
        recipe = authored_recipe()
        image_id = self.app.recipes.assets.import_bytes(picture())
        recipe["image"] = {"asset_id": image_id, "credit": "Inline credit"}
        request = {"operation": "menu", "action": "save", "menu": {"week": "2026-W40", "dishes": [recipe], "salads": []}}
        first = self.app.handle(request)
        self.assertTrue(self.app.handle(request)["idempotent"])
        (self.app.recipes.assets.root / asset_filename(image_id)).unlink()
        repeated = self.app.handle(request)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["menu"], first["menu"])
        before = deepcopy(self.store.read())
        for fake_reference in (None, "not-a-bank-reference"):
            new = deepcopy(request)
            new["menu"]["week"] = "2026-W41"
            if fake_reference is not None:
                new["menu"]["dishes"][0]["recipe_ref"] = fake_reference
            with self.assertRaisesRegex(RecipeError, "available managed asset"):
                self.app.handle(new)
            self.assertEqual(self.store.read(), before)

    def test_application_origin_search_filters_before_limit_and_keeps_favorite_separate(self):
        bundled = self.app.recipes.import_pack_record(authored_recipe(), pack_id="synthetic", recipe_id="base", version="1")["recipe"]
        for index in range(3):
            recipe = authored_recipe()
            recipe["name"] = f"Personal {index}"
            self.save(recipe, f"user-{index}")
        query = {"operation": "recipes", "action": "search", "library_id": "builtin", "entry_origin": "bundled", "include_ineligible": True, "limit": 1}
        result = self.app.handle(query)["recipes"]
        self.assertEqual([row["id"] for row in result], [bundled["id"]])
        self.assertEqual(result[0]["entry_origin"], "bundled")
        self.assertEqual(result[0]["pack"], bundled["pack"])
        self.assertFalse(result[0]["locally_modified"])
        self.assertEqual(self.app.handle({**query, "favorites_only": True})["recipes"], [])
        self.app.recipes.set_favorite(bundled["library_recipe_ref"], True, idempotency_key="pack-favorite")
        self.assertEqual(len(self.app.handle({**query, "favorites_only": True})["recipes"]), 1)
        cross = {key: value for key, value in query.items() if key != "library_id"}
        cross["library_ids"] = ["builtin"]
        self.assertEqual(self.app.handle(cross)["recipes"][0]["entry_origin"], "bundled")

    def test_historical_menu_replay_does_not_change_when_current_pack_metadata_changes(self):
        for kind in ("recipe_ref", "library_recipe_ref"):
            with self.subTest(kind=kind):
                recipe = authored_recipe()
                bundled = self.app.recipes.import_pack_record(recipe, pack_id="synthetic", recipe_id=kind, version="1")["recipe"]
                reference = ({"recipe_ref": {"id": bundled["id"], "revision": 1}} if kind == "recipe_ref"
                             else {"library_recipe_ref": bundled["library_recipe_ref"]})
                request = {"operation": "menu", "action": "save", "menu": {"week": "2026-W40", "dishes": [{**reference, "portions": 2}], "salads": []}}
                first = self.app.handle(request)["menu"]
                self.assertTrue({"entry_origin", "pack", "locally_modified"}.isdisjoint(first["dishes"][0]))
                changed = deepcopy(bundled)
                changed["name"] = "Edited current revision"
                self.app.recipes.update(bundled["id"], 1, changed)
                self.assertTrue(self.app.recipes.get(bundled["id"], 1)["locally_modified"])
                repeated = self.app.handle(request)
                self.assertTrue(repeated["idempotent"])
                self.assertEqual(repeated["menu"], first)

    def test_inert_self_hosted_attribution_ports_keep_identity_and_legacy_rules(self):
        recipe = authored_recipe()
        recipe["source"]["url"] = "https://Recipes.Example:9000/soup?id=7&utm_source=test"
        recipe["source"]["original"] = {"url": "http://original.example:443/soup?id=7"}
        saved = self.save(recipe)
        self.assertEqual(saved["source"]["url"], "https://recipes.example:9000/soup?id=7")
        self.assertEqual(saved["source"]["original"]["url"], "http://original.example:443/soup?id=7")
        menu = self.save_menu({"recipe_ref": {"id": saved["id"], "revision": 1}, "portions": 2})
        self.assertIn('href="https://recipes.example:9000/soup?id=7"', menu_email_html(menu))
        self.assertIn('href="http://original.example:443/soup?id=7"', menu_email_html(menu))
        self.assertEqual(normalize_attribution_url("http://[::1]:9000/r"), "http://[::1]:9000/r")
        self.assertEqual(normalize_attribution_url("http://host.example:80/r"), "http://host.example/r")
        self.assertEqual(normalize_source_url("https://host.example:443/r"), "https://host.example/r")
        for bad in ("https://host.example:0/r", "https://host.example:65536/r", "https://host.example:abc/r", "https://user:password@host.example:9000/r", "javascript:bad()"):
            with self.assertRaises(RecipeError):
                normalize_attribution_url(bad)
        with self.assertRaisesRegex(RecipeError, "standard HTTPS port"):
            normalize_source_url("https://host.example:9000/r", version=1)
        with self.assertRaisesRegex(RecipeError, "HTTPS"):
            normalize_source_url("http://host.example:9000/r")

    def test_themealdb_preserves_upstream_original_measure_and_unknown_servings(self):
        meal = {"idMeal": "123", "strMeal": "Synthetic", "strInstructions": "Cook at 180 C for 30 minutes.", "strIngredient1": "salt", "strMeasure1": "3 ts", "strSource": "http://example.org/recipe?id=7&oldid=9&utm_medium=test"}
        source = TheMealDBSource(transport=lambda *a, **k: {"meals": [meal]})
        recipe = source.search("Synthetic", 1)[0]
        self.assertEqual(recipe["source"]["original"]["url"], "http://example.org/recipe?id=7&oldid=9")
        self.assertEqual(recipe["ingredients"][0]["original_text"], "3 ts salt")
        self.assertIsNone(recipe["portions"])
        rendered = menu_email_html({"week": "2026-W40", "dishes": [scale_recipe(recipe)], "salads": []})
        self.assertIn("Opprinnelig kilde", rendered)
        self.assertIn("Cook at 180 C for 30 minutes.", rendered)

    def test_generated_relabeling_and_legacy_downgrade_cannot_bypass_acceptance(self):
        recipe = authored_recipe()
        recipe["source"]["relationship"] = "generated"
        saved = self.save(recipe)
        for basis in ("unknown", "user", "source"):
            changed = deepcopy(saved)
            changed["portions_evidence"]["basis"] = basis
            with self.subTest(basis=basis), self.assertRaisesRegex(RecipeError, "relabeled"):
                self.app.handle({"operation": "recipes", "action": "update", "recipe_id": saved["id"], "expected_revision": 1, "recipe": changed})
        legacy = json.loads(LEGACY)
        with self.assertRaisesRegex(RecipeError, "downgrade"):
            self.app.handle({"operation": "recipes", "action": "update", "recipe_id": saved["id"], "expected_revision": 1, "recipe": legacy})
        self.assertEqual(self.app.recipes.get(saved["id"])["revision"], 1)
        unknown = normalize_recipe(authored_recipe())
        unknown["portions_evidence"]["basis"] = "unknown"
        with self.assertRaisesRegex(RecipeError, "evidence|acceptance"):
            scale_recipe(unknown, 2)
        self.assertEqual(menu_requirements({"dishes": [scale_recipe(unknown)], "salads": []})[0], [])
        prior = normalize_recipe(authored_recipe())
        prior["ingredients"][2]["evidence"]["quantity"]["basis"] = "estimate"
        changed = deepcopy(prior)
        changed["ingredients"][2]["item"] = "MEL"
        changed["ingredients"][2]["original_text"] = "700 g flour, translated display"
        changed["ingredients"][2]["evidence"]["quantity"] = {"basis": "user"}
        with self.assertRaisesRegex(RecipeError, "relabeled"):
            prepare_recipe_input(changed, prior=prior)
        changed = normalize_recipe(authored_recipe())
        changed["source"]["relationship"] = "user_supplied"
        for ingredient in changed["ingredients"]:
            ingredient["item"] += " renamed"
        with self.assertRaisesRegex(RecipeError, "relabeled"):
            prepare_recipe_input(changed, prior=saved)

    def test_unicode_mixed_fractions_and_locale_assumption_remain_honest(self):
        for text, expected in (("1½ tsp", Fraction(3, 2)), ("1¼ tbsp", Fraction(5, 4)), ("2¾ g", Fraction(11, 4))):
            quantity, _unit = parse_measure(text)
            self.assertEqual(read_quantity(quantity), expected)
        ingredient = source_ingredient("1½ tsp salt")
        self.assertEqual(ingredient["evidence"]["unit"]["basis"], "estimate")
        self.assertIn("locale", ingredient["evidence"]["unit"]["assumptions"])

    def test_new_generated_input_preserves_original_wording_before_version_upgrade(self):
        for relationship in ("generated", "GENERATED", "Generated"):
            recipe = authored_recipe()
            recipe.pop("schema_version")
            recipe["source"]["relationship"] = relationship
            recipe["ingredients"][2]["raw"] = "700 g flour, sifted before measuring"
            saved = self.save(recipe, "generated-" + relationship)
            with self.subTest(relationship=relationship):
                self.assertEqual(saved["schema_version"], 2)
                self.assertEqual(saved["ingredients"][2]["original_text"], "700 g flour, sifted before measuring")
                self.assertEqual(saved["ingredients"][2]["evidence"]["quantity"]["basis"], "estimate")

    def test_source_field_inheritance_allows_notes_reorder_and_explicit_quantity_correction(self):
        recipe = authored_recipe()
        recipe["source"]["url"] = "https://example.org/source-edit"
        recipe["ingredients"] = [source_ingredient("700 g mel"), source_ingredient("3 ts salt")]
        frozen = self.app.recipes.persist_discovery(recipe)
        saved = self.app.handle({"operation": "recipes", "action": "save", "discovery_ref": frozen["discovery_ref"], "idempotency_key": "source-edit"})["recipe"]
        changed = deepcopy(saved)
        changed["ingredients"].reverse()
        changed["ingredients"][1]["notes"] = "Sift before mixing."
        changed["ingredients"][0]["quantity"] = 4
        changed["ingredients"][0]["evidence"]["quantity"] = {"basis": "user", "input": "Use four teaspoons."}
        updated = self.app.handle({"operation": "recipes", "action": "update", "recipe_id": saved["id"], "expected_revision": 1, "recipe": changed})["recipe"]
        self.assertEqual(updated["ingredients"][1]["evidence"], saved["ingredients"][0]["evidence"])
        self.assertEqual(updated["ingredients"][0]["evidence"]["unit"]["basis"], "source")
        changed = deepcopy(updated)
        changed["ingredients"][1]["quantity"] = 999
        with self.assertRaisesRegex(RecipeError, "trusted source"):
            prepare_recipe_input(changed, prior=updated)
        prior = normalize_recipe(authored_recipe())
        prior["ingredients"] = [source_ingredient("700 g mel"), source_ingredient("50 g mel")]
        prior["ingredients"][1]["evidence"]["quantity"]["basis"] = "estimate"
        changed = deepcopy(prior)
        changed["ingredients"].reverse()
        changed["ingredients"][1]["notes"] = "Dough; dusting is the separate estimated amount."
        inherited = prepare_recipe_input(changed, prior=prior)
        self.assertEqual(inherited["ingredients"][1]["evidence"]["quantity"]["basis"], "source")
        self.assertEqual(inherited["ingredients"][0]["evidence"]["quantity"]["basis"], "estimate")

    def test_provider_candidates_keep_identifying_queries_in_new_version(self):
        for provider, host in (("oda", "oda.com"), ("meny", "meny.no"), ("mathem", "mathem.se")):
            url = f"https://{host}/recipes/?recipeId=42&revision=7&utm_source=test"
            recipe = provider_recipe_candidates(provider, {"recipes": [{"id": "42", "name": "Synthetic", "url": url}]}, 1)[0]
            with self.subTest(provider=provider):
                self.assertEqual(recipe["schema_version"], 2)
                self.assertEqual(recipe["source"]["url"], url.removesuffix("&utm_source=test"))
                self.assertEqual(recipe["rights"]["storage"], "link_only")

    def test_scaling_source_quantity_retains_accepted_serving_estimate_dependency(self):
        recipe = authored_recipe()
        recipe["source"]["url"] = "https://example.org/scaling-estimate"
        recipe["ingredients"] = [source_ingredient("700 g mel")]
        recipe["portions_evidence"] = {"basis": "estimate", "input": "2 loaves", "assumptions": "Six slices per loaf, one slice per person."}
        frozen = self.app.recipes.persist_discovery(recipe)
        accepted = self.app.handle({"operation": "recipes", "action": "accept_estimates", "discovery_ref": frozen["discovery_ref"], "recipe_digest": recipe_digest(recipe), "estimate_fields": ["portions"], "confirmation_statement": ESTIMATE_CONFIRMATION})["recipe"]
        scaled = normalize_recipe(scale_recipe(accepted, 2))
        dependency = scaled["ingredients"][0]["evidence"]["quantity"]["calculation"]
        self.assertEqual(dependency["input_portions"], {"numerator": 12, "denominator": 1})
        self.assertEqual(dependency["portions_evidence"], normalize_recipe(accepted)["portions_evidence"])
        self.assertEqual(scaled["portions_evidence"]["calculation"]["input_quantity"], {"numerator": 12, "denominator": 1})
        rendered = menu_email_html({"week": "2026-W40", "dishes": [scaled], "salads": []})
        self.assertIn("350/3 g mel (godkjent anslag)", rendered)
        rescaled = normalize_recipe(scale_recipe(scaled, 4))
        calculation = rescaled["ingredients"][0]["evidence"]["quantity"]["calculation"]
        self.assertEqual(calculation["input_quantity"], {"numerator": 700, "denominator": 1})
        self.assertEqual(calculation["factor"], {"numerator": 1, "denominator": 3})
        self.assertEqual(calculation["input_portions"], {"numerator": 12, "denominator": 1})
        with self.assertRaisesRegex(RecipeError, "service-owned"):
            prepare_recipe_input(scaled)
        invalid = deepcopy(scaled)
        invalid["ingredients"][0]["evidence"]["quantity"]["calculation"]["factor"] = {"numerator": 99, "denominator": 1}
        with self.assertRaisesRegex(RecipeError, "does not match"):
            normalize_recipe(invalid)
        for cls, library in ((MealieAdapter, "mealie"), (RecipeSageAdapter, "recipesage")):
            adapter = cls({"library_id": "fixture-" + library, "provider": library, "base_url": "https://recipes.example", "read_only": False}, {"token": "synthetic"})
            only_nested = deepcopy(scaled)
            only_nested["portions_evidence"] = {"basis": "user"}
            operation = {"operation_id": "libop:v1:abcdefghijklmnop", "library_id": "fixture-" + library, "snapshot_digest": "a" * 64, "source_identity": "fixture:source"}
            payload, _ = adapter._native_payload(only_nested, operation)
            with self.subTest(library=library), self.assertRaises(RecipeLibraryError):
                adapter._mapped_recipe({**payload, "id": "11111111-1111-4111-8111-111111111111"})

    def test_publisher_estimates_render_distinctly_and_external_sidecars_cannot_forge_them(self):
        recipe = authored_recipe()
        recipe["portions_evidence"] = {"basis": "estimate", "input": "two loaves",
            "assumptions": "Twelve person servings from two loaves.",
            "project_review": {"publisher": "Meal Concierge", "pack_id": "test", "pack_version": "1"}}
        saved = self.app.recipes.import_pack_record(recipe, pack_id="test", recipe_id="bread", version="1")["recipe"]
        scaled = scale_recipe(saved, 2)
        rendered = menu_email_html({"week": "2026-W40", "dishes": [scaled], "salads": []})
        self.assertIn("anslag fra Meal Concierge", rendered)
        self.assertNotIn("må avklares", rendered)
        self.assertNotIn("godkjent anslag", rendered)
        self.assertNotIn("acceptance", saved["portions_evidence"])
        for cls, library in ((MealieAdapter, "mealie"), (RecipeSageAdapter, "recipesage")):
            adapter = cls({"library_id": "fixture-" + library, "provider": library,
                "base_url": "https://recipes.example", "read_only": False}, {"token": "synthetic"})
            operation = {"operation_id": "libop:v1:abcdefghijklmnop", "library_id": "fixture-" + library,
                "snapshot_digest": "a" * 64, "source_identity": "fixture:source"}
            for value in (recipe, scaled):
                payload, _ = adapter._native_payload(value, operation)
                with self.subTest(library=library), self.assertRaises(RecipeLibraryError):
                    adapter._mapped_recipe({**payload, "id": "11111111-1111-4111-8111-111111111111"})

    def test_both_native_sidecars_cannot_forge_acceptance(self):
        for cls, library in ((MealieAdapter, "mealie"), (RecipeSageAdapter, "recipesage")):
            adapter = cls({"library_id": "fixture-" + library, "provider": library, "base_url": "https://recipes.example", "read_only": False}, {"token": "synthetic"})
            recipe = authored_recipe()
            recipe["portions_evidence"] = {"basis": "estimate", "input": "two loaves", "acceptance": {"recipe_digest": "a" * 64, "statement": ESTIMATE_CONFIRMATION}}
            operation = {"operation_id": "libop:v1:abcdefghijklmnop", "library_id": "fixture-" + library, "snapshot_digest": "a" * 64, "source_identity": "fixture:source"}
            payload, _ = adapter._native_payload(recipe, operation)
            with self.subTest(library=library), self.assertRaises(RecipeLibraryError):
                adapter._mapped_recipe({**payload, "id": "11111111-1111-4111-8111-111111111111"})
            relabeled = normalize_recipe(authored_recipe())
            relabeled["source"]["relationship"] = "generated"
            for basis in ("user", "source", "unknown"):
                relabeled["portions_evidence"]["basis"] = basis
                payload, _ = adapter._native_payload(relabeled, operation)
                with self.subTest(library=library, basis=basis), self.assertRaises(RecipeLibraryError):
                    adapter._mapped_recipe({**payload, "id": "11111111-1111-4111-8111-111111111111"})

    def test_trusted_discovery_planner_save_keeps_evidence_without_personal_entry(self):
        recipe = authored_recipe()
        recipe["source"]["url"] = "https://example.org/trusted"
        recipe["portions_evidence"] = {"basis": "source", "input": "Serves 12"}
        snapshot = self.app.recipes.persist_discovery(recipe)
        planned = self.app.handle({"operation": "menu", "action": "plan", "planner_input": {"week": "2026-W40", "dates": ["2026-09-28"], "portions": 2, "candidates": [{"discovery_ref": snapshot["discovery_ref"]}]}})["plan"]
        self.assertEqual(planned["status"], "planned")
        saved = self.app.handle({"operation": "menu", "action": "save", "planner_handoff": planned["save_handoff"]})["menu"]
        self.assertEqual(saved["dishes"][0]["portions_evidence"]["basis"], "source")
        self.assertEqual(len(menu_requirements(saved)[0]), 5)
        self.assertEqual(self.app.recipes.search(), [])

    def test_native_recipesage_loaves_and_upstream_attribution_need_no_sidecar(self):
        fixture = json.loads((ROOT / "tests/fixtures/recipesage/v4.0.6.json").read_text())["recipe_get"]
        if "recipe" in fixture:
            fixture = fixture["recipe"]
        fixture = {**fixture, "yield": "2 loaves", "source": "Original baker", "url": "https://example.org/recipe?id=7&rev=9"}
        adapter = RecipeSageAdapter({"library_id": "fixture-recipesage", "provider": "recipesage", "base_url": "https://recipes.example", "read_only": True}, {"token": "synthetic"})
        native = adapter._mapped_recipe(fixture)
        self.assertIsNone(native["portions"])
        self.assertEqual(native["yield"]["quantity"], {"numerator": 2, "denominator": 1})
        self.assertEqual(native["source"]["original"]["publisher"], "Original baker")

    def test_migration_rejects_unsupported_outbound_before_journal_and_preserves_inbound(self):
        recipe = authored_recipe()
        recipe["source"]["original"] = {"url": "https://example.org/upstream?id=7", "author": "Original author"}
        recipe["source_provider"] = "oda"
        source = MigrationLibrary("source", [recipe])
        destination = MigrationLibrary("destination")
        for library in (source, destination):
            self.app.recipe_libraries[library.library_id] = {"library_id": library.library_id, "provider": "mealie", "display_name": library.library_id, "read_only": False}
            self.app.recipe_library_adapters[library.library_id] = library
        request = {"operation": "migration", "action": "prepare", "source_library_id": "source", "destination_library_id": "destination", "metadata_options": {"favorites": "omit", "labels": "omit"}}
        plan = self.app.handle(request)
        self.assertEqual(plan["items"][0]["status"], "unsupported_rights")
        self.assertEqual(plan["items"][0]["reason"], "destination_storage_unavailable")
        result = self.app.handle({"operation": "migration", "action": "execute", "plan_id": plan["plan_id"], "confirmation": {"plan_digest": plan["plan_digest"], "statement": plan["confirmation_statement"]}})
        self.assertEqual(result["items"][0]["copy_status"], "skipped")
        self.assertEqual(destination.create_calls, 0)
        with sqlite3.connect(self.app.recipes.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM library_operations").fetchone()[0], 0)
        plan = self.app.handle({**request, "destination_library_id": "builtin"})
        result = self.app.handle({"operation": "migration", "action": "execute", "plan_id": plan["plan_id"], "confirmation": {"plan_digest": plan["plan_digest"], "statement": plan["confirmation_statement"]}})
        self.assertEqual(result["items"][0]["copy_status"], "confirmed")
        saved = self.app.recipes.search()[0]
        self.assertEqual(normalize_recipe(self.app.recipes.get(saved["id"])), normalize_recipe(recipe))


if __name__ == "__main__":
    unittest.main()
