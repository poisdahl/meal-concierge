from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import HouseholdError
from meny import MenyClient
from retailer_recipes import MAX_DETAIL_BYTES, meny_recipe_input, meny_recipe_jsonld, meny_recipe_path


RECIPE_PATH = "/oppskrifter/test/synthetic-rice"
RECIPE_URL = "https://meny.no" + RECIPE_PATH


def page():
    # Invented culinary text in the field types observed on MENY 2026-09-06.
    # No copied private recipe, account, cart or product association.
    recipe = {
        "@context": "https://schema.org/", "@type": "Recipe",
        "url": RECIPE_URL, "name": "Synthetic rice",
        "recipeYield": "Antall personer: 4", "inLanguage": "no",
        "recipeIngredient": ["200 g ris", "1 stk testgrønnsak", "salt etter smak"],
        "recipeInstructions": ["Kok risen.", "Server testretten."],
        "author": {"@type": "Organization", "name": "MENY"},
        "dateModified": "2026-09-01T10:00:00Z",
    }
    return {"ready": True, "url": RECIPE_URL, "canonical_urls": [RECIPE_URL],
            "scripts": [json.dumps(recipe)]}


def browser_evaluate(script, observation):
    # Execute the production extractor against synthetic DOM observations.
    harness = r'''
const fs = require('node:fs');
const {script, page} = JSON.parse(fs.readFileSync(0, 'utf8'));
global.location = new URL(page.url);
global.document = {querySelectorAll(selector) {
  if (selector === 'script[type="application/ld+json"]') return page.scripts.map(textContent => ({textContent}));
  if (selector === 'link[rel="canonical"]') return page.canonical_urls.map(href => ({href}));
  throw new Error('Unexpected DOM interaction');
}};
process.stdout.write(eval(script));
'''
    completed = subprocess.run([shutil.which("node"), "-e", harness],
                               input=json.dumps({"script": script, "page": observation}),
                               text=True, capture_output=True, check=True, timeout=10)
    return json.loads(completed.stdout)


class MenyRecipeObservationTests(unittest.TestCase):
    def test_exact_observed_structure_retains_text_and_servings_label(self):
        result = meny_recipe_jsonld(page(), RECIPE_PATH)
        self.assertEqual(result["recipeYield"], "Antall personer: 4")
        self.assertEqual(result["recipeIngredient"][0], "200 g ris")
        self.assertEqual(result["recipeInstructions"], ["Kok risen.", "Server testretten."])

    def test_unrelated_jsonld_does_not_replace_the_exact_recipe(self):
        observation = page()
        observation["scripts"].append('{"@type":"BreadcrumbList"}')
        self.assertEqual(meny_recipe_jsonld(observation, RECIPE_PATH)["name"], "Synthetic rice")

    def test_path_rejects_other_routes_encoded_traversal_and_external_targets(self):
        for value in (None, "https://meny.no" + RECIPE_PATH, "/varer/test", "/oppskrifter/../varer",
                      "/oppskrifter/%2e%2e/varer", "/oppskrifter/%252e%252e/varer", "/oppskrifter//test",
                      "/oppskrifter/test?portions=8", "/oppskrifter/%3fquery", "/oppskrifter/%00test",
                      "/oppskrifter/%zz", "/oppskrifter/%5ctest"):
            with self.subTest(value=value), self.assertRaises(HouseholdError):
                meny_recipe_path(value)

    def test_page_canonical_and_structured_identity_must_all_match(self):
        for change in ({"ready": False}, {"url": RECIPE_URL + "?portions=8"},
                       {"url": "https://oda.com" + RECIPE_PATH}, {"canonical_urls": []},
                       {"canonical_urls": [RECIPE_URL, RECIPE_URL]}):
            with self.subTest(change=change), self.assertRaises(HouseholdError):
                meny_recipe_jsonld({**page(), **change}, RECIPE_PATH)
        observation = page()
        document = json.loads(observation["scripts"][0])
        document["url"] = "https://meny.no/oppskrifter/other"
        observation["scripts"] = [json.dumps(document)]
        with self.assertRaises(HouseholdError):
            meny_recipe_jsonld(observation, RECIPE_PATH)

    def test_duplicate_missing_malformed_and_oversized_data_fail_closed(self):
        valid = page()["scripts"][0]
        invalid_scripts = [[], [valid, valid], ["{}"], ["{"], ['{"x":NaN}'],
                           [valid, '{"@graph":[{"@type":"Recipe"}]}'],
                           [valid, '{"@type":["Thing","Recipe"]}'],
                           [valid.replace('"@type": "Recipe"', '"@type":"Recipe","@type":"Recipe"')],
                           [" " * (MAX_DETAIL_BYTES + 1)], [None], ["{}"] * 9]
        for scripts in invalid_scripts:
            with self.subTest(shape=[type(value).__name__ for value in scripts]), self.assertRaises(HouseholdError):
                meny_recipe_jsonld({**page(), "scripts": scripts}, RECIPE_PATH)

    def test_required_source_fields_are_bounded_without_truncation(self):
        for field, value in (("recipeIngredient", []), ("recipeIngredient", ["x"] * 201),
                             ("recipeIngredient", ["x" * 501]), ("recipeIngredient", [None]),
                             ("recipeInstructions", [{"text": "unobserved variant"}]),
                             ("recipeInstructions", ["x"] * 101), ("recipeYield", 4),
                             ("name", ""), ("name", "\ud800"),
                             ("author", {"@type": ["Organization"], "name": "MENY"}),
                             ("author", {"@type": {}, "name": "MENY"}),
                             ("author", {"@type": "Person", "name": "x" * 301})):
            observation = page()
            document = json.loads(observation["scripts"][0])
            document[field] = value
            observation["scripts"] = [json.dumps(document)]
            with self.subTest(field=field), self.assertRaises(HouseholdError):
                meny_recipe_jsonld(observation, RECIPE_PATH)


class MenyRecipeAdapterGateTests(unittest.TestCase):
    def client(self):
        return MenyClient(instance="synthetic", binary="agent-browser", executable="/unavailable",
                          profile="/unavailable", home="/unavailable", socket_directory="/unavailable", uid=1000, gid=1000)

    def test_login_failure_never_opens_recipe_or_changes_cart(self):
        client = self.client()
        client._require_login = mock.Mock(side_effect=HouseholdError("login required"))
        client._open = mock.Mock()
        client._change_cart = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "login required"):
            client.call("recipe_detail", {"recipe_id": RECIPE_PATH})
        client._open.assert_not_called()
        client._change_cart.assert_not_called()

    def test_deadline_expiry_prevents_browser_work(self):
        client = self.client()
        client._require_login = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "deadline"):
            client.call("recipe_detail", {"recipe_id": RECIPE_PATH}, deadline=0)
        client._require_login.assert_not_called()

    def test_native_scaling_arguments_are_not_silently_ignored(self):
        client = self.client()
        client._require_login = mock.Mock()
        client._open = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "only the exact"):
            client.call("recipe_detail", {"recipe_id": RECIPE_PATH, "portions": 8})
        client._open.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the browser extractor")
    def test_actual_extractor_preserves_page_data_but_lost_login_stops_normalization(self):
        client = self.client()
        client._require_login = mock.Mock()
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock(side_effect=[None, HouseholdError("login lost")])
        extracted = []
        def evaluate(script):
            result = browser_evaluate(script, page())
            extracted.append(result)
            return result
        client._eval = evaluate
        with self.assertRaisesRegex(HouseholdError, "login lost"):
            client.call("recipe_detail", {"recipe_id": RECIPE_PATH})
        self.assertEqual(extracted, [page()])
        client._open.assert_called_once_with(RECIPE_URL)


class MenyRecipeInputTests(unittest.TestCase):
    def test_base_source_quantities_and_person_servings_remain_exact(self):
        value = meny_recipe_input(page(), RECIPE_PATH, fetched_at="2026-09-06T00:00:00Z")
        self.assertEqual(value["portions"], 4)
        self.assertEqual(value["portions_evidence"]["basis"], "source")
        self.assertEqual(value["yield"]["original_text"], "Antall personer: 4")
        self.assertEqual(value["ingredients"][0]["quantity"], {"numerator": 200, "denominator": 1})
        self.assertEqual(value["ingredients"][0]["original_text"], "200 g ris")
        self.assertIsNone(value["ingredients"][2]["quantity"])
        self.assertNotIn("calculation", value["ingredients"][0]["evidence"]["quantity"])

    def test_source_cannot_grant_other_provider_pantry_diet_or_estimate_authority(self):
        observation = page()
        document = json.loads(observation["scripts"][0])
        document.update({"source_provider": "oda", "accepted_estimates": True,
                         "pantry": True, "suitableForDiet": "GlutenFreeDiet",
                         "product_associations": [{"product_id": "unsafe"}]})
        observation["scripts"] = [json.dumps(document)]
        value = meny_recipe_input(observation, RECIPE_PATH)
        self.assertEqual(value["source_provider"], "meny")
        self.assertEqual(value["source"]["relationship"], "original")
        self.assertEqual(value["source"]["external_id"], RECIPE_PATH)
        self.assertNotIn("accepted_estimates", value)
        self.assertNotIn("product_associations", value)
        self.assertNotIn("suitableForDiet", value)
        self.assertFalse(any(item.get("pantry") for item in value["ingredients"]))

    def test_unobserved_yield_keeps_original_without_guessing_persons(self):
        for text in ("4", "2 loaves", "4 porsjoner", "Antall personer: mange"):
            observation = page()
            document = json.loads(observation["scripts"][0])
            document["recipeYield"] = text
            observation["scripts"] = [json.dumps(document)]
            with self.subTest(text=text):
                value = meny_recipe_input(observation, RECIPE_PATH)
                self.assertIsNone(value["portions"])
                self.assertEqual(value["portions_evidence"]["basis"], "unknown")
                self.assertEqual(value["yield"]["original_text"], text)


if __name__ == "__main__":
    unittest.main()


class RetailerPaginationTests(unittest.TestCase):
    def test_authenticated_mcp_page_shape_refills_with_bound_cursor(self):
        import tempfile
        from core import StateStore
        from service import Application
        class Provider:
            def __init__(self, provider):
                self.provider, self.pages, self.more = provider, [], True
            def probe(self):
                return {'protocol_version':'fixture', 'server':{'name':'fixture'}, 'tool_count':1}
            def call(self, name, args, **kwargs):
                self.pages.append(args['page'])
                assert name == 'recipe_search'
                return {'hasMore':self.more, 'recipes':[{'id':str(args['page']), 'title':'Synthetic recipe',
                    'url':f'https://www.{self.provider}.{"se" if self.provider == "mathem" else "com"}/recipes/{args["page"]}-synthetic/'}]}
        for provider in ('oda', 'mathem'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                client=Provider(provider)
                app=Application(StateStore(Path(temp), {'instance':'paging','household':'Synthetic','provider':provider}),client,None)
                request={'operation':'recipes','action':'discover','projection':'summary','source':provider,'query':'rice','limit':1}
                first=app.handle(request)
                self.assertFalse(first['exhausted'])
                self.assertEqual(first['next_cursor'], {'provider':provider,'query':'rice','page':2,'size':1})
                for changed in ({'query':'pasta'}, {'limit':2}, {'cursor':{**first['next_cursor'],'provider':'meny'}},
                                {'cursor':{**first['next_cursor'],'page':True}}):
                    with self.assertRaises(HouseholdError):
                        app.handle({**request,'cursor':first['next_cursor'],**changed})
                self.assertEqual(client.pages,[1])
                client.more=False
                second=app.handle({**request,'cursor':first['next_cursor']})
                self.assertTrue(second['exhausted'])
                self.assertIsNone(second['next_cursor'])
                self.assertEqual(client.pages,[1,2])
                self.assertNotEqual(first['recipes'][0]['discovery_ref'],second['recipes'][0]['discovery_ref'])
                client.more='false'
                self.assertFalse(app.handle(request)['exhausted'])
                client.more=True
                last=app.handle({**request,'cursor':{**first['next_cursor'],'page':50}})
                self.assertIsNone(last['next_cursor'])
                self.assertFalse(last['exhausted'])

    def test_unrepresentable_provider_rows_cannot_authorize_ai_fallback(self):
        import tempfile
        from core import StateStore
        from service import Application
        class Provider:
            def probe(self):
                return {'protocol_version':'fixture','server':{'name':'fixture'},'tool_count':1}
            def call(self, name, args, **kwargs):
                assert name=='recipe_search'
                return {'recipes':self.rows,'hasMore':False}
        for provider in ('oda','mathem'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                client=Provider()
                store=StateStore(Path(temp),{'instance':'rows','household':'Synthetic','provider':provider})
                app=Application(store,client,None)
                with store.locked() as state:
                    state['setup']['status']='complete'
                    state['profile']['recipes']['sources']={key:key==provider for key in state['profile']['recipes']['sources']}
                request={'operation':'menu','action':'plan','planner_input':{'week':'2026-W37','dates':['2026-09-07'],'portions':2}}
                for row in ({'id':1,'title':'Existing recipe','url':None}, {}, None,
                            {'id':2,'title':'Existing recipe','url':'https://unrelated.example/recipe'}):
                    client.rows=[row]
                    discovery=app.handle(request)['plan']['discovery']
                    self.assertFalse(discovery['ai_fallback_eligible'])
                    self.assertEqual(next(x for x in discovery['sources'] if x['source']==provider)['status'],'search_limit')
                client.rows=[]
                self.assertTrue(app.handle(request)['plan']['discovery']['ai_fallback_eligible'])


class RetailerPublicDetailTests(unittest.TestCase):
    def test_native_numeric_search_ids_are_retained_without_coercing_invalid_ids(self):
        from recipe_sources import provider_recipe_candidates
        for provider in ("oda", "mathem"):
            source = self.source(provider)
            for value in (123, "123", True, False, 0, -1, 1.5, {}, 10 ** 300):
                with self.subTest(provider=provider, value=value):
                    rows = provider_recipe_candidates(provider, {"recipes": [
                        {"id": value, "title": source["name"], "url": source["source"]["url"]}
                    ]}, limit=1)
                    self.assertEqual(len(rows), 1 if value in (123, "123") else 0)
                    if rows:
                        self.assertEqual(rows[0]["source"]["external_id"], "123")

    def source(self, provider="mathem"):
        host, locale = ("www.mathem.se", "se") if provider == "mathem" else ("oda.com", "no")
        return {"name": "Synthetic dinner", "source": {"url": f"https://{host}/{locale}/recipes/123-fixture/", "external_id": "123"}}

    def response(self, source, *, yield_text="4", ingredients=None):
        from recipe_import_sources import _source_result
        from recipe_import_readers import read_webpage
        raw = {"@context": "https://schema.org/", "@type": "Recipe", "name": source["name"],
            "recipeYield": yield_text, "recipeIngredient": ingredients or ["200 g ris", "1 msk olja", "2 st tomater"],
            "recipeInstructions": ["Prepare this synthetic dinner."], "author": {"@type": "Person", "name": "Synthetic Author"}}
        html = '<script type="application/ld+json">' + json.dumps(raw) + '</script>'
        page_result = read_webpage(html, source_url=source["source"]["url"])
        page_result["recipes"] = [_source_result(row, {"kind": "web", "url": source["source"]["url"]}) for row in page_result["recipes"]]
        return page_result

    def test_verified_portions_and_swedish_measures_keep_wording_and_exact_scale(self):
        from retailer_recipes import retail_web_recipe_input
        from recipes import normalize_recipe, bind_recipe_source, scale_recipe
        source = self.source()
        raw = ["200 g ris", "0,5 msk olja", "1 tsk salt", "2 krm peppar", "2 st tomater", "1 klyfta vitlök"]
        with mock.patch("recipe_import_sources.fetch_public_webpage", return_value=self.response(source, ingredients=raw)) as fetch:
            candidate = retail_web_recipe_input(source, "mathem")
        fetch.assert_called_once_with(source["source"]["url"])
        normalized = normalize_recipe(bind_recipe_source(candidate, provider="mathem"))
        self.assertEqual(normalized["portions"], 4)
        self.assertEqual(normalized["portions_evidence"]["input"], "4")
        self.assertEqual([row["original_text"] for row in normalized["ingredients"]], raw)
        self.assertIsNone(normalized["ingredients"][-1]["quantity"])
        self.assertEqual(normalized["source"]["author"], "Synthetic Author")
        scaled = scale_recipe(normalized, 2)
        quantities = [item["quantity"] for item in scaled["shopping_requirements"]]
        self.assertEqual((scaled["shopping_requirements"][1]["unit"], quantities[1]), ("ss", {"numerator": 1, "denominator": 4}))
        self.assertEqual(normalized["source_provider"], "mathem")

    def test_wrong_origin_identity_query_or_budget_never_fetches(self):
        from copy import deepcopy
        import time
        from retailer_recipes import retail_web_recipe_input
        source = self.source()
        with mock.patch("recipe_import_sources.fetch_public_webpage") as fetch:
            for changes in ({"url": "https://oda.com/no/recipes/123-fixture/"}, {"external_id": "999"},
                            {"url": source["source"]["url"] + "?portions=8"}, {"url": "https://www.mathem.se@evil.example/se/recipes/123-fixture/"}):
                altered = deepcopy(source); altered["source"].update(changes)
                with self.subTest(changes=changes), self.assertRaises(HouseholdError):
                    retail_web_recipe_input(altered, "mathem")
            with self.assertRaisesRegex(HouseholdError, "budget"):
                retail_web_recipe_input(source, "mathem", deadline=time.monotonic())
        fetch.assert_not_called()

    def test_ambiguous_changed_and_unknown_portions_are_not_materialized(self):
        from copy import deepcopy
        from retailer_recipes import retail_web_recipe_input
        source = self.source()
        valid = self.response(source)
        wrong_title = deepcopy(valid); wrong_title["recipes"][0]["candidate"]["name"] = "Another recipe"
        cases = [None, {"recipes": [], "requires_interpretation": True}, {**valid, "recipes": valid["recipes"] * 2},
                 wrong_title, self.response(source, yield_text="2 loaves"), self.response(source, yield_text="0")]
        for response in cases:
            with self.subTest(response=response), mock.patch("recipe_import_sources.fetch_public_webpage", return_value=response), self.assertRaises(HouseholdError):
                retail_web_recipe_input(source, "mathem")

    def test_application_detail_is_bound_cached_and_does_not_save_a_personal_recipe(self):
        import tempfile
        from core import StateStore
        from service import Application
        from recipe_sources import provider_recipe_candidates
        for provider in ("oda", "mathem"):
            source = self.source(provider)
            class Provider:
                def probe(self, **kwargs):return {"protocol_version":"fixture","server":{"name":"synthetic"},"tool_count":0}
                def call(self, *args, **kwargs):raise AssertionError("public details must not use provider credentials")
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                app = Application(StateStore(Path(directory), {"instance":"detail","household":"Detail test","provider":provider,"profile_overrides":{}}), Provider(), None)
                with app.store.locked() as state:state["profile"]["recipes"]["sources"][provider] = True
                rows = provider_recipe_candidates(provider, {"recipes":[{"id":123,"url":source["source"]["url"],"title":source["name"]}]}, limit=1)
                old = app.recipes.persist_discovery(rows[0])
                with mock.patch("recipe_import_sources.fetch_public_webpage", return_value=self.response(source, ingredients=["200 g ris"])) as fetch:
                    detailed = app.handle({"operation":"recipes","action":"detail","discovery_ref":old["discovery_ref"]})
                    replay = app.handle({"operation":"recipes","action":"detail","discovery_ref":old["discovery_ref"]})
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(detailed["discovery_ref"], replay["discovery_ref"])
                self.assertNotEqual(old["discovery_ref"], detailed["discovery_ref"])
                self.assertEqual(detailed["recipe"]["source"]["external_id"], "123")
                self.assertEqual(detailed["recipe"]["source_provider"], provider)
                self.assertEqual(detailed["recipe"]["portions"], 4)
                self.assertEqual(app.recipes.search(), [])
                self.assertEqual(app.recipes.resolve_discovery(old["discovery_ref"])["recipe"]["rights"]["storage"], "link_only")

    def test_automatic_menu_resolves_native_search_hit_and_scales_once(self):
        import tempfile
        from core import StateStore
        from service import Application
        from product_planner import menu_requirements
        for provider in ("oda", "mathem"):
            source = self.source(provider)
            class Provider:
                def probe(self, **kwargs):return {"protocol_version":"fixture","server":{"name":"synthetic"},"tool_count":1}
                def call(self, name, args, **kwargs):
                    if name != "recipe_search":raise AssertionError(name)
                    return {"recipes":[{"id":123,"title":source["name"],"url":source["source"]["url"]}],"hasMore":False}
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                app = Application(StateStore(Path(directory), {"instance":"menu-detail","household":"Synthetic","provider":provider}), Provider(), None)
                with app.store.locked() as state:
                    state["setup"]["status"] = "complete"
                    state["profile"]["recipes"]["sources"] = {key:key==provider for key in state["profile"]["recipes"]["sources"]}
                with mock.patch("recipe_import_sources.fetch_public_webpage", return_value=self.response(source, ingredients=["200 g ris"])) as fetch:
                    plan = app.handle({"operation":"menu","action":"plan","planner_input":{
                        "week":"2026-W37","dates":["2026-09-07"],"portions":2}})["plan"]
                    self.assertEqual(plan["status"], "planned")
                    menu = app.handle({"operation":"menu","action":"save","planner_ref":plan["save_ref"]})["menu"]
                self.assertEqual(fetch.call_count, 1)
                needs, unresolved = menu_requirements(menu)
                self.assertFalse(unresolved)
                self.assertEqual(len(needs), 1)
                self.assertEqual(needs[0]["quantity"], {"numerator":100,"denominator":1})
                self.assertEqual(app.recipes.search(), [])
