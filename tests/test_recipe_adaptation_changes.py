"""Bounded recipe changes preserve the exact source and unaffected evidence."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import StateStore
from recipes import RecipeError, normalize_recipe, recipe_digest
from service import Application
from test_meal_concierge_products import FakeProvider


def source_recipe(provider="oda"):
    host = {"oda": "oda.com", "mathem": "mathem.se", "meny": "meny.no"}[provider]
    estimate = {"basis": "estimate", "input": "100 g beans", "assumptions": "Use a measured drained portion.",
                "acceptance": {"recipe_digest": "a" * 64, "statement": "Accepted source estimate."}}
    cream_estimate = {"basis": "estimate", "input": "100 ml cream", "assumptions": "Measured cream volume.",
                      "project_review": {"publisher": "Fixture", "pack_id": "test", "pack_version": "1"}}
    recipe = {
        "schema_version": 2, "name": "Synthetic bean soup", "portions": 1,
        "source": {"kind": provider, "publisher": provider.upper(), "relationship": "original",
                   "external_id": "soup-1", "url": f"https://{host}/recipes/soup-1/",
                   "original": {"url": f"https://{host}/recipes/soup-1/",
                                "publisher": provider.upper(), "title": "Source bean soup"}},
        "source_provider": provider, "rights": {"storage": "full", "credit": "Synthetic fixture"},
        "portions_evidence": {"basis": "source", "input": "One portion"},
        "ingredients": [
            {"item": "beans", "original_text": "100 g beans", "quantity": 100, "unit": "g",
             "evidence": {"quantity": deepcopy(estimate), "unit": deepcopy(estimate)}},
            {"item": "cream", "original_text": "100 ml cream", "quantity": 100, "unit": "ml",
             "evidence": {"quantity": deepcopy(cream_estimate), "unit": deepcopy(cream_estimate)}},
        ],
        "steps": ["Simmer the beans and stir in the cream."],
    }
    if provider == "oda":
        recipe["ingredients"][1]["_store_product_hint"] = {
            "provider": "oda", "product_ref": 123, "name": "Synthetic cream",
            "url": "https://oda.com/no/products/123-synthetic-cream/",
            "relationship": "source_recipe_association",
        }
    return recipe


class AdaptationChangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def application(self, provider="oda"):
        store = StateStore(Path(self.temp.name) / provider, {
            "instance": "adapt-" + provider, "household": "Synthetic " + provider,
            "provider": provider, "profile_overrides": {},
        })
        client = FakeProvider()
        app = Application(store, client, object())
        app.handle({"operation": "setup", "action": "apply", "keep_current": True})
        return app, store, client

    @staticmethod
    def request(reference, digest, *, portions=2):
        return {"operation": "recipes", "action": "adapt", "recipe_ref": reference,
                "recipe_digest": digest, "source_schema_version": 2, "portions": portions,
                "changes": {"ingredients": [{"index": 1, "item": "unsweetened soy yogurt",
                                             "assumptions": "Use the same volume as cream."}],
                            "steps": ["Simmer the beans, then fold in the soy yogurt off heat."]}}

    def test_scaled_short_get_adapts_without_echo_and_preserves_unaffected_estimate(self):
        app, store, client = self.application()
        original = app.recipes.persist_discovery(source_recipe(), trusted_store_product_hints=True)
        saved = app.handle({"operation": "recipes", "action": "save",
                            "discovery_ref": original["discovery_ref"], "idempotency_key": "save-source"})["recipe"]
        reference = {"id": saved["id"], "revision": saved["revision"]}
        short = app.handle({"operation": "recipes", "action": "get", "recipe_id": saved["id"],
                            "portions": 2})["recipe"]
        self.assertEqual(short["ingredients"][0]["quantity"], {"numerator": 200, "denominator": 1})
        self.assertEqual(short["recipe_digest"], recipe_digest(saved))
        result = app.handle(self.request(reference, short["recipe_digest"]))
        adapted = result["recipe"]
        self.assertEqual(adapted["portions"], 2)
        self.assertEqual(adapted["ingredients"][0], short["ingredients"][0])
        self.assertEqual(adapted["ingredients"][0]["evidence"]["quantity"]["basis"], "estimate")
        self.assertIn("calculation", adapted["ingredients"][0]["evidence"]["quantity"])
        changed = adapted["ingredients"][1]
        self.assertEqual(changed["quantity"], {"numerator": 200, "denominator": 1})
        self.assertEqual(changed["original_text"], "100 ml cream")
        self.assertEqual(changed["evidence"]["quantity"]["basis"], "estimate")
        for evidence in changed["evidence"].values():
            self.assertEqual(evidence["assumptions"], "Use the same volume as cream.")
            self.assertNotIn("calculation", evidence)
            self.assertNotIn("acceptance", evidence)
            self.assertNotIn("project_review", evidence)
        self.assertNotIn("_store_product_hint", changed)
        self.assertEqual(adapted["source_provider"], "oda")
        self.assertEqual(adapted["rights"], saved["rights"])
        self.assertEqual(adapted["source"]["url"], saved["source"]["url"])
        self.assertEqual(adapted["source"]["relationship"], "adapted")
        self.assertEqual(client.calls, [])

        restarted = Application(store, client, object())
        self.assertEqual(normalize_recipe(restarted.recipes.get(saved["id"], saved["revision"]),
                                          trusted_store_product_hints=True),
                         normalize_recipe(saved, trusted_store_product_hints=True))
        self.assertEqual(restarted.recipes.resolve_discovery(result["discovery_ref"])["recipe"], adapted)
        archived = restarted.handle({"operation": "recipes", "action": "save",
                                     "discovery_ref": result["discovery_ref"],
                                     "idempotency_key": "save-adaptation"})["recipe"]
        self.assertNotEqual(archived["id"], saved["id"])
        self.assertEqual(normalize_recipe(restarted.recipes.get(saved["id"], saved["revision"]),
                                          trusted_store_product_hints=True),
                         normalize_recipe(saved, trusted_store_product_hints=True))

    def test_source_binding_survives_each_retailer(self):
        for provider in ("oda", "mathem", "meny"):
            with self.subTest(provider=provider):
                app, _, client = self.application(provider)
                source = app.recipes.persist_discovery(source_recipe(provider), trusted_store_product_hints=True)
                original = source["recipe"]
                # A discovery reference remains the exact frozen source authority.
                request = self.request(None, source["recipe_digest"])
                request.pop("recipe_ref")
                request["discovery_ref"] = source["discovery_ref"]
                adapted = app.handle(request)["recipe"]
                self.assertEqual(adapted["source_provider"], provider)
                self.assertEqual(adapted["source"]["url"], original["source"]["url"])
                self.assertEqual(adapted["source"]["original"], original["source"].get("original"))
                self.assertEqual(client.calls, [])

    def test_explicit_quantity_and_unit_are_new_estimates(self):
        app, _, _ = self.application()
        source = app.recipes.persist_discovery(source_recipe(), trusted_store_product_hints=True)
        request = self.request(None, source["recipe_digest"], portions=1)
        request.pop("recipe_ref")
        request["discovery_ref"] = source["discovery_ref"]
        request["changes"]["ingredients"][0].update(quantity={"numerator": 250, "denominator": 1}, unit="g")
        changed = app.handle(request)["recipe"]["ingredients"][1]
        self.assertEqual(changed["quantity"], {"numerator": 250, "denominator": 1})
        self.assertEqual(changed["unit"], "g")
        self.assertEqual(changed["evidence"]["quantity"]["basis"], "estimate")
        self.assertEqual(changed["evidence"]["unit"]["basis"], "estimate")
        self.assertNotIn("_store_product_hint", changed)

    def test_unresolved_changed_amount_remains_unknown(self):
        app, _, _ = self.application()
        original = source_recipe()
        original["ingredients"][1].update(quantity=None, unit=None, scalable=False, evidence={
            "quantity": {"basis": "unknown", "input": "cream to taste"},
            "unit": {"basis": "unknown", "input": "cream to taste"},
        })
        frozen = app.recipes.persist_discovery(original, trusted_store_product_hints=True)
        request = self.request(None, frozen["recipe_digest"], portions=1)
        request.pop("recipe_ref")
        request["discovery_ref"] = frozen["discovery_ref"]
        result = app.handle(request)
        changed = result["recipe"]["ingredients"][1]
        self.assertIsNone(changed["quantity"])
        self.assertIsNone(changed["unit"])
        self.assertEqual(changed["evidence"]["quantity"]["basis"], "unknown")
        self.assertEqual(changed["evidence"]["unit"]["basis"], "unknown")
        self.assertFalse(result["readiness"]["scaling_ready"])

    def test_rejects_forged_fields_stale_digest_and_invalid_edits(self):
        app, _, client = self.application()
        source = app.recipes.persist_discovery(source_recipe(), trusted_store_product_hints=True)
        base = self.request(None, source["recipe_digest"])
        base.pop("recipe_ref")
        base["discovery_ref"] = source["discovery_ref"]
        variants = [
            ({**base, "recipe_digest": "0" * 64}, "digest"),
            ({**base, "source_schema_version": 1}, "schema"),
            ({**base, "recipe": source["recipe"]}, "exactly one"),
            ({**{key: value for key, value in base.items() if key != "changes"},
              "recipe": source["recipe"]}, "portions apply only"),
            ({**base, "changes": {"source_provider": "mathem"}}, "support only"),
            ({**base, "changes": {"rights": {"storage": "full"}}}, "support only"),
            ({**base, "changes": {"ingredients": [{"index": 9, "item": "soy yogurt",
                                                   "assumptions": "Same volume."}], "steps": ["Mix."]}}, "index"),
            ({**base, "changes": {"ingredients": [{"index": True, "item": "soy yogurt",
                                                   "assumptions": "Same volume."}], "steps": ["Mix."]}}, "index"),
            ({**base, "changes": {"ingredients": [{"index": 1, "item": "soy yogurt",
                                                   "assumptions": "Same volume."}]}}, "steps"),
            ({**base, "changes": {"ingredients": [{"index": 1, "item": "soy yogurt",
                                                   "assumptions": "Same volume.", "evidence": {"basis": "source"}}],
                                   "steps": ["Mix."]}}, "requires index"),
        ]
        for request, message in variants:
            with self.subTest(message=message), self.assertRaisesRegex(RecipeError, message):
                app.handle(request)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
