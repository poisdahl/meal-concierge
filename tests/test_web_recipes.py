"""Web settings, import persistence and mixed menu flow; no external effects."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(SOURCE.parents[1] / 'scripts' / 'tests'))
import test_meal_concierge_planner as fixtures
from core import HouseholdError
from recipes import RecipeError, normalize_recipe, source_ingredient
from service import Application
from web_recipes import url_enabled, search_web, DEFAULT_WEB_SEARCH, _source_search

DECISION = {'storage': 'full', 'basis': 'permission', 'evidence': 'Synthetic recipe authored and permitted for this test.'}


class DirectSearchTests(unittest.TestCase):
    def response(self, url, **kwargs):
        parsed = urlsplit(url)
        domain = parsed.hostname.removeprefix('www.')
        query = parse_qs(parsed.query)
        if domain == 'matprat.no':
            self.assertEqual(query['text'], ['linsegryte'])
            data = {'totalHits': 1, 'searchHits': [{'type': 'recipe', 'title': 'Linsegryte',
                'data': {'linkUrl': '/oppskrifter/sunn/linsegryte/', 'ingredients': ['MUST NOT RETURN']}}]}
        elif domain == 'vegetarentusiast.no':
            data = [{'type': 'post', 'title': 'Linsegryte &amp; ris', 'url': 'https://vegetarentusiast.no/linsegryte/'}]
        elif domain == 'frukt.no':
            data = {'recipeSearchResult': {'pages': [{'pageTitle': 'Linsegryte', 'url': '/oppskrifter/linsegryte/'}]}}
        elif domain == 'godfisk.no':
            self.assertEqual(kwargs['json_body'], {'q': 'linsegryte', 'type': ['Recipe']})
            data = {'sections': [{'pageType': 'Recipe', 'hits': [{'heading': 'Dal med sei', 'url': '/oppskrifter/sei/dal/'}]}]}
        elif domain == 'godt.no':
            data = {'props': {'pageProps': {'params': {'query': 'linsegryte'},
                'recipes': {'items': [{'title': 'Linsegryte', 'links': {'relativeUrl': '/oppskrifter/gryte/1/linsegryte'}}]}}}}
            return ('<script id="__NEXT_DATA__" type="application/json">'+json.dumps(data)+'</script>').encode(), 'text/html'
        elif domain == 'tine.no':
            return b'<span class="o-search-page--text__hits">1</span><a href="/oppskrifter/linsegryte" class="m-card__read-more-btn">Les mer</a>', 'text/html'
        elif domain == 'trinesmatblogg.no':
            return b'<div class="search-header__searchform"></div><a href="/recipe/navigation/">Unrelated</a><h2 class="entry-title"><a href="/recipe/linsegryte/">Linsegryte</a></h2>', 'text/html'
        else:
            self.fail('Unexpected network destination: '+domain)
        return json.dumps(data).encode(), 'application/json'

    def test_shared_default_search_all_sources_without_search_vendor_or_storage(self):
        with patch('recipe_import_sources._get_bytes', side_effect=self.response) as fetch, patch('recipe_import_sources.firecrawl_request') as firecrawl:
            result = search_web(DEFAULT_WEB_SEARCH, 'linsegryte')
        firecrawl.assert_not_called()
        self.assertEqual(fetch.call_count, 7)
        self.assertEqual(result['backend'], 'direct')
        self.assertEqual(result['coverage'], 'complete')
        self.assertEqual(len(result['results']), 7)
        self.assertNotIn('MUST NOT RETURN', json.dumps(result))
        self.assertNotIn('navigation', json.dumps(result))
        self.assertFalse(result['persisted'])
        self.assertTrue(any(r['title'] == 'Linsegryte & ris' for r in result['results']))

    def test_disabled_and_disabled_parent_never_queried(self):
        settings = deepcopy(DEFAULT_WEB_SEARCH)
        settings['enabled'] = False
        with patch('recipe_import_sources._get_bytes') as fetch:
            self.assertEqual(search_web(settings, 'linsegryte')['status'], 'disabled')
            fetch.assert_not_called()
        settings['enabled'] = True
        settings['sites'][0]['enabled'] = False
        with patch('recipe_import_sources._get_bytes', side_effect=self.response) as fetch:
            result = search_web(settings, 'linsegryte')
        self.assertEqual(fetch.call_count, 6)
        self.assertFalse(any('matprat.no' in r['url'] for r in result['results']))

    def test_partial_broad_custom_and_all_unavailable_are_honest(self):
        settings = deepcopy(DEFAULT_WEB_SEARCH)
        settings['broad'] = True
        settings['sites'].append({'name': 'Custom', 'domain': 'example.org', 'enabled': True})
        with patch('recipe_import_sources._get_bytes', side_effect=self.response):
            result = search_web(settings, 'linsegryte')
        self.assertEqual(result['coverage'], 'partial')
        self.assertFalse(result['broad_searched'])
        self.assertEqual(len(result['pending_scopes']), 2)
        self.assertEqual(result['sources'][-1]['status'], 'unsupported')
        from recipe_import_sources import RecipeImportSourceError
        for error in ['HTTP 429', 'timeout', 'redirect forbidden']:
            with self.subTest(error=error), patch('recipe_import_sources._get_bytes', side_effect=RecipeImportSourceError(error)), patch('recipe_import_sources.firecrawl_request') as firecrawl:
                result = search_web(settings, 'linsegryte')
                self.assertEqual(result['status'], 'unavailable')
                self.assertFalse(result['searched'])
                firecrawl.assert_not_called()

    def test_malformed_success_is_not_no_matches(self):
        for raw, content_type in [(b'{}', 'application/json'), (b'[]', 'text/html'), (b'{', 'application/json')]:
            with self.subTest(raw=raw), patch('recipe_import_sources._get_bytes', return_value=(raw, content_type)):
                result = search_web(DEFAULT_WEB_SEARCH, 'linsegryte')
                self.assertEqual(result['status'], 'unavailable')

    def test_malformed_collections_and_inconsistent_totals_fail(self):
        for domain, data in [('matprat.no', {'totalHits': 12, 'searchHits': {}}),
                ('matprat.no', {'totalHits': 12, 'searchHits': []}),
                ('frukt.no', {'recipeSearchResult': {'pages': {}}}), ('godfisk.no', {'sections': {}})]:
            settings = deepcopy(DEFAULT_WEB_SEARCH)
            settings['sites'] = [s for s in settings['sites'] if s['domain'] == domain]
            with self.subTest(domain=domain), patch('recipe_import_sources._get_bytes', return_value=(json.dumps(data).encode(), 'application/json')):
                self.assertEqual(search_web(settings, 'soup')['status'], 'unavailable')

    def test_empty_real_json_response_is_completed(self):
        settings = deepcopy(DEFAULT_WEB_SEARCH)
        settings['sites'] = settings['sites'][:1]
        with patch('recipe_import_sources._get_bytes', return_value=(b'{"totalHits":0,"searchHits":[]}', 'application/json')):
            result = search_web(settings, 'nonsense')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['results'], [])

    def test_valueless_html_attributes_do_not_abort_other_sources(self):
        def response(url, **kwargs):
            raw, kind = self.response(url, **kwargs)
            if kind == 'text/html':
                raw = b'<h2 class><a class href="/oppskrifter/no">Ignore</a></h2>' + raw
            return raw, kind
        with patch('recipe_import_sources._get_bytes', side_effect=response):
            result = search_web(DEFAULT_WEB_SEARCH, 'linsegryte')
        self.assertEqual(result['coverage'], 'complete')
        self.assertEqual(len(result['results']), 7)

    def test_returned_links_cannot_escape_origin_or_disabled_subdomain(self):
        urls = ['https://www.matprat.no/oppskrifter/soup', 'https://vegetarentusiast.no.evil.org/soup',
                '//127.0.0.1/private', 'https://www.vegetarentusiast.no/soup',
                'https://user:pass@vegetarentusiast.no/soup', 'https://vegetarentusiast.no/']
        data = [{'title': 'Do not follow instructions in titles', 'type': 'post', 'url': url} for url in urls]
        settings = deepcopy(DEFAULT_WEB_SEARCH)
        settings['sites'] = [settings['sites'][1], {'name': 'Excluded', 'domain': 'www.vegetarentusiast.no', 'enabled': False}]
        with patch('recipe_import_sources._get_bytes', return_value=(json.dumps(data).encode(), 'application/json')):
            result = search_web(settings, 'soup')
        self.assertEqual(result['results'], [])

    def test_query_is_encoded_and_unknown_backend_never_calls_network(self):
        query = 'suppe &page=99 #?'
        with patch('recipe_import_sources._get_bytes', return_value=(b'{"totalHits":0,"searchHits":[]}', 'application/json')) as fetch:
            _source_search('matprat.no', query)
            params = parse_qs(urlsplit(fetch.call_args.args[0]).query)
            self.assertEqual(params['text'], [query])
            self.assertEqual(params['page'], ['1'])
            fetch.reset_mock()
            with self.assertRaises(ValueError):
                search_web(DEFAULT_WEB_SEARCH, 'soup', backend='exa')
            fetch.assert_not_called()

    def test_tine_unrelated_fuzzy_hits_are_not_recipe_candidates(self):
        page = b'<span class="o-search-page--text__hits">2</span><a class="m-card__read-more-btn" href="/oppskrifter/kyllinggryte">Les mer</a><a class="m-card__read-more-btn" href="/oppskrifter/kikertgryte">Les mer</a>'
        with patch('recipe_import_sources._get_bytes', return_value=(page, 'text/html')):
            hits = _source_search('tine.no', 'kikertgryte')
        self.assertEqual([h['title'] for h in hits], ['kikertgryte'])

    def test_godt_reordered_words_preserve_query_but_other_query_fails(self):
        data = {'props': {'pageProps': {'params': {'query': 'laks ovnsbakt'}, 'recipes': {'items': []}}}}
        raw = ('<script id="__NEXT_DATA__">'+json.dumps(data)+'</script>').encode()
        with patch('recipe_import_sources._get_bytes', return_value=(raw, 'text/html')):
            self.assertEqual(_source_search('godt.no', 'ovnsbakt laks'), [])
            with self.assertRaises(ValueError):
                _source_search('godt.no', 'linsegryte')


class WebRecipeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WeeklyPlannerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app, self.store = self.fixture.app, self.fixture.store
        clock = patch.object(Application, '_household_today', return_value=date(2026, 9, 7))
        clock.start()
        self.addCleanup(clock.stop)
        with self.store.locked() as state:
            state['profile']['recipes']['sources'] = {key: key == 'internal' for key in state['profile']['recipes']['sources']}

    def scopes(self):
        return self.app.handle({'operation': 'recipes', 'action': 'web_search_plan', 'query': 'grønnsaksmiddag'})

    def test_service_default_routes_to_first_party_without_persistence(self):
        with patch('recipe_import_sources._get_bytes', side_effect=DirectSearchTests().response), patch('recipe_import_sources.firecrawl_request') as firecrawl:
            result = self.app.handle({'operation': 'recipes', 'action': 'web_search', 'query': 'linsegryte'})
        self.assertEqual(result['backend'], 'direct')
        self.assertEqual(len(result['results']), 7)
        self.assertEqual(self.app.recipes.search(), [])
        firecrawl.assert_not_called()

    def settings(self, **changes):
        return self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': False, 'changes': {'web_search': changes}})

    def imported(self, **changes):
        record = {'@type': 'Recipe', 'name': 'Synthetic carrot dinner', 'recipeYield': '2 servings',
                  'prepTime': 'PT20M', 'cookTime': 'PT10M', 'recipeIngredient': ['200 g gulrot'],
                  'recipeInstructions': ['Stek gulrøttene.']}
        html = '<script type="application/ld+json">' + json.dumps(record) + '</script>'
        with patch('recipe_import_sources._get_bytes', return_value=(html.encode(), 'text/html')):
            return self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                'url': 'https://www.matprat.no/synthetic-test', 'web_discovery': True,
                'storage_decision': DECISION, **changes})

    def plan(self, refs=None, result=None):
        return self.app._plan_menu({'week': '2026-W37', 'dates': ['2026-09-07'],
            **({'web_candidates': refs} if refs is not None else {}),
            **({'web_search_result': result} if result is not None else {})})

    def test_defaults_partial_settings_restart_and_exclusions(self):
        scopes = self.scopes()
        self.assertFalse(scopes['searched'])
        self.assertEqual(len(scopes['scopes']), 7)
        self.assertFalse(scopes['settings']['broad'])
        sites = deepcopy(scopes['settings']['sites'])
        sites[0]['enabled'] = False
        self.settings(broad=True, sites=sites)
        settings = self.store.read()['profile']['recipes']['web_search']
        self.assertTrue(url_enabled('https://recipes.example/dinner', settings))
        for url in ('https://www.matprat.no/x', 'https://sub.www.matprat.no/x', 'http://godt.no/x', 'https://user:pass@godt.no/x', 'https://['):
            self.assertFalse(url_enabled(url, settings), url)
        self.assertTrue(url_enabled('https://notmatprat.no/x', settings))
        self.settings(enabled=False)
        self.assertEqual(self.scopes()['status'], 'disabled')
        self.assertTrue(self.store.read()['profile']['recipes']['web_search']['broad'])
        with self.assertRaises(HouseholdError):
            self.settings(sites=[{'name': 'Bad', 'domain': 'https://matprat.no', 'enabled': True}])
        self.assertFalse(self.store.read()['profile']['recipes']['web_search']['enabled'])

    def test_shared_search_filters_disabled_domains_and_does_not_persist(self):
        sites = deepcopy(self.scopes()['settings']['sites'])
        sites[0]['enabled'] = False
        self.settings(broad=True, sites=sites)
        with patch('recipe_import_sources.firecrawl_request', return_value={'web': [
            {'url': 'https://www.matprat.no/no', 'title': 'Excluded'},
            {'url': 'https://vegetarentusiast.no/soup', 'title': 'Soup'},
            {'url': 'https://vegetarentusiast.no/soup', 'title': 'Duplicate'},
            {'url': 'http://godt.no/no'}, {'url': 'https://other.example/dinner'}]}) as fetch:
            result = self.app.handle({'operation': 'recipes', 'action': 'web_search', 'query': 'soup', 'backend': 'firecrawl'})
            self.assertEqual([r['url'] for r in result['results']], ['https://vegetarentusiast.no/soup', 'https://other.example/dinner'])
            self.assertEqual(result['status'], 'completed')
            self.assertFalse(result['persisted'])
            self.assertEqual(fetch.call_args.args[1]['excludeDomains'], ['matprat.no'])
            self.assertNotIn('includeDomains', fetch.call_args.args[1])
            self.settings(enabled=False)
            fetch.reset_mock()
            self.assertEqual(self.app.handle({'operation': 'recipes', 'action': 'web_search', 'query': 'soup', 'backend': 'firecrawl'})['status'], 'disabled')
            fetch.assert_not_called()
        self.assertEqual(self.app.recipes.search(), [])

    def test_shared_search_failure_is_unavailable_not_empty_success(self):
        from recipe_import_sources import RecipeImportSourceError
        with patch('recipe_import_sources.firecrawl_request', side_effect=RecipeImportSourceError('HTTP 429')):
            result = self.app.handle({'operation': 'recipes', 'action': 'web_search', 'query': 'soup', 'backend': 'firecrawl'})
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['searched'])
        self.assertEqual(len(result['scopes']), 7)

    def test_web_read_is_not_import_and_manual_read_ignores_disabled_search(self):
        self.settings(enabled=False)
        with patch('recipe_operations.fetch_public_webpage', return_value={'recipes': [], 'text': 'source'}) as fetch, patch.object(self.app.recipes, 'persist_discovery') as persist:
            request = {'operation': 'recipes', 'action': 'web_read', 'url': 'https://www.matprat.no/soup', 'fetch_method': 'firecrawl'}
            result = self.app.handle(request)
            self.assertFalse(result['persisted'])
            fetch.assert_called_once_with(request['url'], fetch_method='firecrawl')
            persist.assert_not_called()
            fetch.reset_mock()
            with self.assertRaises(RecipeError):
                self.app.handle({**request, 'web_discovery': True})
            fetch.assert_not_called()

    def test_missing_rights_never_fetches_or_persists_and_bookmark_has_no_body(self):
        with patch('recipe_operations.fetch_public_webpage') as fetch, patch.object(self.app.recipes, 'persist_discovery', wraps=self.app.recipes.persist_discovery) as persist:
            result = self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url', 'url': 'https://matprat.no/synthetic'})
            self.assertEqual(result['status'], 'storage_decision_required')
            fetch.assert_not_called()
            persist.assert_not_called()
            link = self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                'url': 'https://matprat.no/synthetic', 'storage_decision': {'storage': 'link_only'}})
            fetch.assert_not_called()
            self.assertFalse(link['readiness']['scaling_ready'])
            self.assertEqual(link['shopping_requirements'], [])
            self.assertFalse(link['recipe'].get('ingredients'))
            self.assertFalse(link['recipe'].get('steps'))
            self.assertFalse(link['recipe'].get('notes'))
            self.assertEqual(self.app.recipes.search(), [])
            saved = self.app.handle({'operation': 'recipes', 'action': 'save', 'discovery_ref': link['discovery_ref'], 'status': 'draft', 'idempotency_key': 'bookmark'})['recipe']
            self.assertEqual(saved['rights']['storage'], 'link_only')
            with self.assertRaises(RecipeError):
                self.app.handle({'operation': 'recipes', 'action': 'convert', 'discovery_ref': link['discovery_ref'],
                    'recipe_digest': link['recipe_digest'], 'source_schema_version': 2,
                    'recipe': {**link['recipe'], 'rights': {'storage': 'full'}}})

    def test_full_import_mixes_with_bank_and_freezes_refs_for_save(self):
        self.fixture.save_candidates(1)
        imported = self.imported()
        self.assertEqual(imported['recipe']['rights']['storage_decision'], DECISION)
        self.assertEqual(len(self.app.recipes.search()), 1)
        self.assertTrue(imported['readiness']['scaling_ready'])
        scopes = self.scopes()
        result, resolved, request = self.plan([{'discovery_ref': imported['discovery_ref']}],
            {'status': 'completed', 'settings_digest': scopes['settings_digest']})
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(len(resolved), 2)
        self.assertNotIn('web_candidates', request)
        self.assertNotIn('web_search_result', request)
        self.assertEqual(len(request['candidates']), 2)
        with patch.object(self.app, '_collect_planner_candidates', side_effect=AssertionError('save searched again')):
            saved = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_handoff': result['save_handoff']})
        self.assertIn('menu', saved)
        self.assertEqual(len(self.app.recipes.search()), 1)

    def test_interpreted_web_source_uses_attributed_url_for_enabled_scope(self):
        url = 'https://www.matprat.no/text-recipe'
        structured = {'@type': 'Recipe', 'name': 'Soup', 'recipeIngredient': ['200 g carrots'],
                      'recipeInstructions': ['Simmer.']}
        html = ('<script type="application/ld+json">' + json.dumps(structured) + '</script>'
                '<article><h1>Soup</h1><p>Serves 2</p><p>200 g carrots</p><p>Simmer.</p></article>')
        interpretation = {'name': 'Soup',
            'ingredients': [{'page': 1, 'quote': '200 g carrots'}],
            'steps': [{'page': 1, 'quote': 'Simmer.'}],
            'yield': {'page': 1, 'quote': 'Serves 2'}}
        with patch('recipe_import_sources._get_bytes', return_value=(html.encode(), 'text/html')) as fetch:
            first = self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                'url': url, 'web_discovery': True, 'storage_decision': DECISION})
            self.assertTrue(first['requires_interpretation'])
            imported = self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                'url': url, 'web_discovery': True, 'storage_decision': DECISION,
                'interpretation': interpretation})
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(imported['recipe']['source']['kind'], 'pasted_text')
        self.assertEqual(imported['recipe']['source']['url'], url)
        self.assertEqual(imported['recipe']['source']['original']['url'], url)
        self.assertEqual(imported['import_report']['source_context']['metadata_basis']['name'],
                         'host_interpretation')
        result, _, _ = self.plan([{'discovery_ref': imported['discovery_ref']}],
            {'status': 'completed', 'settings_digest': self.scopes()['settings_digest']})
        self.assertEqual(result['status'], 'planned')
        self.assertTrue(imported['readiness']['scaling_ready'])
        saved = self.app.handle({'operation': 'menu', 'action': 'save',
                                 'planner_handoff': result['save_handoff']})['menu']
        self.assertIn(url, json.dumps(saved))

    def test_structured_only_incomplete_page_can_be_interpreted_then_converted(self):
        url = 'https://www.matprat.no/incomplete-structured-only'
        structured = {'@type': 'Recipe', 'name': 'Soup', 'recipeIngredient': ['200 g carrots'],
                      'recipeInstructions': ['Simmer.']}
        html = '<script type="application/ld+json">' + json.dumps(structured) + '</script>'
        interpretation = {'name': 'Soup', 'ingredients': [{'page': 1, 'quote': '200 g carrots'}],
                          'steps': [{'page': 1, 'quote': 'Simmer.'}]}
        with patch('recipe_import_sources._get_bytes', return_value=(html.encode(), 'text/html')):
            first = self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                'url': url, 'web_discovery': True, 'storage_decision': DECISION})
            self.assertEqual(first['text'], 'Soup\n200 g carrots\nSimmer.')
            self.assertEqual(first['incomplete_recipes'][0]['extracted']['name'], 'Soup')
            imported = self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                'url': url, 'web_discovery': True, 'storage_decision': DECISION,
                'interpretation': interpretation})
        self.assertFalse(imported['readiness']['scaling_ready'])
        self.assertEqual(imported['import_report']['incomplete_recipes'][0]['extracted']['ingredients'], ['200 g carrots'])
        converted = deepcopy(imported['recipe'])
        converted['portions'] = 2
        converted['portions_evidence'] = {'basis': 'estimate', 'input': 'JSON-LD omitted person portions',
                                           'assumptions': 'This recipe serves two people.'}
        result = self.app.handle({'operation': 'recipes', 'action': 'convert',
            'discovery_ref': imported['discovery_ref'], 'recipe_digest': imported['recipe_digest'],
            'source_schema_version': 2, 'recipe': converted})
        self.assertTrue(result['readiness']['scaling_ready'])
        self.assertEqual(result['suggested_status'], 'active')

    def test_multiple_incomplete_records_cannot_fuse_and_import_by_exact_index(self):
        url = 'https://www.matprat.no/multiple-incomplete'
        cross_records = [
            {'@type': 'Recipe', 'name': 'Carrot soup', 'recipeIngredient': ['200 g carrots'],
             'recipeYield': '2 servings'},
            {'@type': 'Recipe', 'name': 'Bread', 'recipeInstructions': ['Bake at 200 C.'],
             'recipeYield': '4 servings'},
        ]
        cross_html = '<script type="application/ld+json">' + json.dumps(cross_records) + '</script>'
        fused = {'name': 'Carrot soup', 'ingredients': [{'page': 1, 'quote': '200 g carrots'}],
                 'steps': [{'page': 1, 'quote': 'Bake at 200 C.'}],
                 'yield': {'page': 1, 'quote': '4 servings'}}
        with patch('recipe_import_sources._get_bytes', return_value=(cross_html.encode(), 'text/html')):
            first = self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                'url': url, 'web_discovery': True, 'storage_decision': DECISION})
            self.assertTrue(first['requires_record_selection'])
            with self.assertRaisesRegex(RecipeError, 'record_index'):
                self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                    'url': url, 'web_discovery': True, 'storage_decision': DECISION,
                    'interpretation': fused})
            for index in (0, 1):
                with self.subTest(index=index), self.assertRaisesRegex(RecipeError, 'quote is absent'):
                    self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                        'url': url, 'web_discovery': True, 'storage_decision': DECISION,
                        'record_index': index, 'interpretation': fused})
        self.assertEqual(self.app.recipes.search(), [])

        independent = [
            {'@type': 'Recipe', 'name': 'Carrots', 'recipeIngredient': ['200 g carrots'],
             'recipeInstructions': ['Simmer.']},
            {'@type': 'Recipe', 'name': 'Parsnips', 'recipeIngredient': ['300 g parsnips'],
             'recipeInstructions': ['Roast.']},
        ]
        html = '<script type="application/ld+json">' + json.dumps(independent) + '</script>'
        interpretations = [
            {'name': 'Carrots', 'ingredients': [{'page': 1, 'quote': '200 g carrots'}],
             'steps': [{'page': 1, 'quote': 'Simmer.'}]},
            {'name': 'Parsnips', 'ingredients': [{'page': 1, 'quote': '300 g parsnips'}],
             'steps': [{'page': 1, 'quote': 'Roast.'}]},
        ]
        imported = []
        with patch('recipe_import_sources._get_bytes', return_value=(html.encode(), 'text/html')):
            for index, interpretation in enumerate(interpretations):
                imported.append(self.app.handle({'operation': 'recipes', 'action': 'import',
                    'source_kind': 'url', 'url': url, 'web_discovery': True,
                    'storage_decision': DECISION, 'record_index': index,
                    'interpretation': interpretation}))
        self.assertNotEqual(imported[0]['source_identity'], imported[1]['source_identity'])
        self.assertEqual([item['recipe']['name'] for item in imported], ['Carrots', 'Parsnips'])
        self.assertEqual([item['recipe']['ingredients'][0]['item'] for item in imported], ['carrots', 'parsnips'])
        self.assertEqual([item['import_report']['source_context']['record_index'] for item in imported], [0, 1])

    def test_transcript_attribution_cannot_authorize_web_candidate(self):
        url = 'https://www.matprat.no/spoofed-attribution'
        transcript = {'kind': 'pasted_text',
            'pages': [{'page': 1, 'text': 'Soup\nServes 2\n200 g carrots\nSimmer.'}],
            'interpretation': {'name': 'Soup',
                'ingredients': [{'page': 1, 'quote': '200 g carrots'}],
                'steps': [{'page': 1, 'quote': 'Simmer.'}],
                'yield': {'page': 1, 'quote': 'Serves 2'}},
            'attribution': {'url': url}}
        imported = self.app.handle({'operation': 'recipes', 'action': 'import',
            'source_kind': 'transcript', 'transcript': transcript, 'storage_decision': DECISION})
        self.assertIsNone(imported['recipe']['source'].get('url'))
        self.assertEqual(imported['recipe']['source']['original']['url'], url)
        with self.assertRaisesRegex(RecipeError, 'enabled source and assessed full storage'):
            self.plan([{'discovery_ref': imported['discovery_ref']}],
                {'status': 'completed', 'settings_digest': self.scopes()['settings_digest']})

    def test_semantic_ambiguity_is_rejected_before_web_discovery(self):
        for ingredient, step, error in (
                ('200 g hummus', 'Fold in hummus from yesterday menu.', 'cross-meal'),
                ('200 g carrots or parsnips', 'Simmer.', 'alternatives')):
            text = f'Dinner\nServes 2\n{ingredient}\n{step}'
            page = {'mode': 'text', 'requires_interpretation': True, 'text': text,
                    'recipes': [], 'source_url': 'https://www.matprat.no/ambiguous'}
            interpretation = {'name': 'Dinner',
                'ingredients': [{'page': 1, 'quote': ingredient}],
                'steps': [{'page': 1, 'quote': step}],
                'yield': {'page': 1, 'quote': 'Serves 2'}}
            with self.subTest(ingredient=ingredient), patch('recipe_operations.fetch_public_webpage', return_value=page):
                with self.assertRaisesRegex(RecipeError, error):
                    self.app.handle({'operation': 'recipes', 'action': 'import', 'source_kind': 'url',
                        'url': page['source_url'], 'web_discovery': True, 'storage_decision': DECISION,
                        'interpretation': interpretation})
        self.assertEqual(self.app.recipes.search(), [])

    def test_stale_or_excluded_web_refs_fail_manual_url_still_imports(self):
        imported = self.imported()
        before = self.scopes()
        self.settings(enabled=False)
        with self.assertRaises(RecipeError):
            self.plan([{'discovery_ref': imported['discovery_ref']}], {'status': 'completed', 'settings_digest': before['settings_digest']})
        with self.assertRaises(RecipeError):
            self.imported()
        self.assertIn('discovery_ref', self.imported(web_discovery=False))

    def test_unavailable_search_is_visible_without_blocking_internal(self):
        self.fixture.save_candidates(1)
        result, _, _ = self.plan(result={'status': 'unavailable', 'settings_digest': self.scopes()['settings_digest']})
        self.assertEqual(result['status'], 'planned')
        web = next(s for s in result['discovery']['sources'] if s['source'] == 'web')
        self.assertEqual(web['host_search_status'], 'unavailable')
        self.assertFalse(result['discovery']['ai_fallback_eligible'])
        self.settings(broad=True, sites=[])
        with self.assertRaises(RecipeError):
            self.plan(result={'status': 'disabled', 'settings_digest': self.scopes()['settings_digest']})

    def test_conversion_retains_storage_assessment(self):
        imported = self.imported()
        edited = deepcopy(imported['recipe'])
        edited['rights'] = {'storage': 'full'}
        result = self.app.handle({'operation': 'recipes', 'action': 'convert', 'discovery_ref': imported['discovery_ref'],
            'recipe_digest': imported['recipe_digest'], 'source_schema_version': 2, 'recipe': edited})
        self.assertEqual(result['recipe']['rights']['storage_decision'], DECISION)
        self.assertTrue(result['readiness']['scaling_ready'])
        self.assertTrue(all(item['scalable'] for item in result['shopping_requirements']))
        self.assertEqual(result['suggested_status'], 'active')

    def test_conversion_returns_unresolved_readiness(self):
        imported = self.imported()
        edited = deepcopy(imported['recipe'])
        edited['ingredients'][0].update({'quantity': None, 'unit': None, 'scalable': False,
            'evidence': {'quantity': {'basis': 'unknown'}, 'unit': {'basis': 'unknown'}}})
        result = self.app.handle({'operation': 'recipes', 'action': 'convert',
            'discovery_ref': imported['discovery_ref'], 'recipe_digest': imported['recipe_digest'],
            'source_schema_version': 2, 'recipe': edited})
        self.assertFalse(result['readiness']['scaling_ready'])
        self.assertIn('ingredients.0.quantity', result['readiness']['missing_decisions'])
        self.assertFalse(result['shopping_requirements'][0]['scalable'])
        self.assertEqual(result['suggested_status'], 'draft')

    def test_web_recipe_reaches_scaled_menu_and_grocery_requirements(self):
        from product_planner import menu_requirements
        imported = self.imported()
        result, _, _ = self.app._plan_menu({'week': '2026-W37', 'dates': ['2026-09-07'], 'portions': 4,
            'web_candidates': [{'discovery_ref': imported['discovery_ref']}],
            'web_search_result': {'status': 'completed', 'settings_digest': self.scopes()['settings_digest']}})
        self.assertEqual(result['status'], 'planned')
        menu = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_handoff': result['save_handoff']})['menu']
        requirements, unresolved = menu_requirements(menu)
        self.assertEqual(unresolved, [])
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]['item'], 'gulrot')
        self.assertEqual(requirements[0]['unit'], 'g')
        self.assertEqual(requirements[0]['quantity'], {'numerator': 400, 'denominator': 1})
        self.assertIn('https://www.matprat.no/synthetic-test', json.dumps(menu))
        self.assertIn('storage_decision', json.dumps(menu))
        self.assertEqual(self.app.recipes.search(), [])

    def test_store_link_summary_still_fetches_detail(self):
        summary = normalize_recipe({'schema_version': 2, 'name': 'Store dinner',
            'source': {'kind': 'oda', 'relationship': 'original', 'url': 'https://oda.com/no/recipes/123-dinner/'},
            'rights': {'storage': 'link_only'}})
        snapshot = self.app.recipes.persist_discovery(summary)
        full = self.imported(web_discovery=False)
        with self.store.locked() as state:
            state['profile']['recipes']['sources']['oda'] = True
        def page(source, *args):
            return {'candidates': [{'discovery_ref': snapshot['discovery_ref']}] if source == 'oda' else [], 'exhausted': True}
        with patch.object(self.app, '_selection_page', side_effect=page), patch.object(self.app, '_recipe_detail', return_value=full) as detail:
            result, _, _ = self.plan()
        detail.assert_called_once()
        self.assertEqual(result['status'], 'planned')

    def test_norwegian_dotted_count_unit_is_not_an_estimate(self):
        ingredient = source_ingredient('2 stk. egg')
        self.assertEqual(ingredient['unit'], 'stk')
        self.assertEqual(ingredient['item'], 'egg')
        self.assertIsNotNone(ingredient['quantity'])


if __name__ == '__main__':
    unittest.main()
