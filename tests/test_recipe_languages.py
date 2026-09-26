"""Reviewed language variants through packs, service reads and frozen delivery."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recipes import normalize_recipe, scale_recipe, RecipeError, RecipeStore, adapt_recipe_changes
from recipe_languages import source_text_digest, recipe_presentation
from recipe_portable import FORMAT, canonical_bytes, write_archive, apply_archive
from build_recipe_pack import NORMALIZER_VERSION
from core import StateStore, HouseholdError
from service import Application
from menu_planning import menu_ref
from test_recipe_delivery import Provider, DEST, chat_cap
from recipe_delivery import render_menu, render_pdf
from agent_views import _recipe_view


def bilingual():
    recipe = normalize_recipe({'schema_version': 2, 'name': 'Gulrotsuppe', 'language': 'nb-NO', 'portions': 2,
        'ingredients': [{'item': 'gulrot', 'raw': '200 g gulrot', 'quantity': 200, 'unit': 'g', 'scalable': True, 'notes': 'i terninger'},
                        {'item': 'vann', 'raw': '1 l vann', 'quantity': 1, 'unit': 'l', 'scalable': True}],
        'steps': ['Kok gulroten i vannet.'], 'notes': 'Server varm.', 'storage': 'Avkjøl raskt.',
        'reheating': 'Varm til gjennomvarm.',
        'source': {'kind': 'user', 'relationship': 'user_supplied'}, 'rights': {'storage': 'full'}})
    recipe['translations'] = {'en': {'source_text_digest': source_text_digest(recipe),
        'name': 'Carrot soup', 'ingredients': [{'item': 'carrots', 'notes': 'diced'}, {'item': 'water'}],
        'steps': ['Simmer the carrots in the water.'], 'notes': 'Serve hot.',
        'storage': 'Cool promptly.', 'reheating': 'Reheat until hot throughout.'}}
    return normalize_recipe(recipe)


class RecipeLanguageTests(unittest.TestCase):
    def test_bound_text_and_quantity_ratios_preserve_scaling_and_reject_stale_variants(self):
        recipe = bilingual()
        scaled = scale_recipe(recipe, 6)
        self.assertEqual(source_text_digest(recipe), source_text_digest(scaled))
        self.assertEqual(recipe_presentation(scaled, 'en-US')['name'], 'Carrot soup')
        self.assertEqual(scaled['shopping_requirements'][0]['query'], 'gulrot')
        for mutate in (lambda r: r['ingredients'].reverse(),
                       lambda r: r['ingredients'][0].update(item='potet'),
                       lambda r: r['ingredients'][0].update(notes='finrevet'),
                       lambda r: r['ingredients'][0].update(quantity=1),
                       lambda r: r.update(steps=['Stek grønnsakene.'])):
            changed = deepcopy(recipe); mutate(changed)
            with self.assertRaisesRegex(RecipeError, 'stale'):
                normalize_recipe(changed)
        for mutate in (lambda v: v.update(quantity=5), lambda v: v.pop('storage'),
                       lambda v: v['ingredients'][0].pop('notes')):
            changed = deepcopy(recipe); mutate(changed['translations']['en'])
            with self.assertRaises(RecipeError): normalize_recipe(changed)
        adapted = adapt_recipe_changes({'steps': ['Bake the vegetables.']}, prior=recipe)
        self.assertNotIn('translations', adapted)
        legacy = deepcopy(recipe); legacy.pop('translations')
        self.assertNotIn('translations', normalize_recipe(legacy))
        presentation = recipe_presentation(legacy, 'en')
        self.assertTrue(presentation['fallback'])
        self.assertEqual(presentation['resolved_language'], 'nb-NO')

    def test_fallback_retains_unstructured_source_quantity(self):
        from recipe_languages import display_recipe
        from service_common import menu_email_html
        recipe = bilingual(); recipe.pop('translations')
        recipe['ingredients'][0].update(raw='en liten håndfull gulrot', amount=None, quantity=None, unit=None, scalable=False)
        shown = display_recipe(recipe, 'en')
        self.assertNotIn('display_item', shown['ingredients'][0])
        rendered = menu_email_html({'week': '2026-W37', 'output_language': 'en', 'dishes': [recipe]})
        self.assertIn('en liten håndfull gulrot', rendered)
        self.assertIn('Requested language unavailable', rendered)

    def test_pack_service_and_frozen_english_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recipe = bilingual()
            records = root/'records.jsonl'
            records.write_bytes(canonical_bytes({'recipe_id': 'synthetic:soup', 'status': 'ready', 'recipe': recipe}) + b'\n')
            manifest = {'format': FORMAT, 'format_version': 1, 'kind': 'bundled', 'pack_id': 'synthetic-language',
                        'pack_version': '1', 'normalizer_version': NORMALIZER_VERSION, 'recipe_schema_version': 2, 'records_count': 1}
            archive = root/'recipes.zip'
            write_archive(archive, manifest, {'records.jsonl': records})
            descriptor = {**manifest, 'bytes': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
            descriptor.pop('records_count')
            state = root/'state'; state.mkdir()
            report = apply_archive(archive, state, 'synthetic', descriptor)
            app = Application(StateStore(state, {'household': 'synthetic', 'provider': 'oda'}), Provider(), None)
            app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})
            reference = report['results'][0]['bank_recipe_ref']
            result = app.handle({'operation': 'recipes', 'action': 'get', 'recipe_id': reference['recipe_id'], 'language': 'en', 'portions': 6})
            self.assertEqual(result['recipe']['translations'], recipe['translations'])
            view = _recipe_view('get', result, 0, 10, 'summary')
            self.assertEqual(view['available_languages'], ['nb-NO', 'en'])
            self.assertEqual(view['presentation']['name'], 'Carrot soup')
            self.assertEqual(view['presentation']['ingredients']['items'][0]['notes'], 'diced')
            from datetime import datetime
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo('Europe/Oslo')).date()
            week = f'{today.isocalendar().year}-W{today.isocalendar().week:02d}'
            planned = app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {
                'week': week, 'dates': [today.isoformat()], 'as_of_date': today.isoformat(), 'portions': 6,
                'selection_mode': 'agent', 'alternatives': 1,
                'candidates': [{'recipe_ref': {'id': reference['recipe_id'], 'revision': 1}}]}})
            menu = app.handle({'operation': 'menu', 'action': 'save', 'language': 'en',
                               'planner_ref': planned['plan']['save_ref']})['menu']
            self.assertEqual(menu['dishes'][0]['name'], 'Gulrotsuppe')
            self.assertEqual(menu['output_language'], 'en')
            rendered = render_menu(menu, app.recipes.assets)
            self.assertIn('600 g carrots (diced)', rendered['text'])
            self.assertIn('Ingredients', rendered['text'])
            self.assertIn('Simmer the carrots', rendered['text'])
            self.assertNotIn('Fremgangsmåte', rendered['text'])
            pdf = render_pdf(rendered)
            self.assertTrue(pdf.startswith(b'%PDF'))
            self.assertIn(b'Weekly menu and recipes', pdf)
            request = {'operation': 'recipe_delivery', 'action': 'request', 'request_id': 'english', 'delivery_requested': True,
                       'menu_ref': menu_ref(menu), 'destinations': {'chat': DEST['chat']}, 'capabilities': {'chat': chat_cap()}}
            job = app.handle(request)
            self.assertEqual(job['language'], 'en')
            self.assertFalse(job['recipe_languages'][0]['fallback'])
            self.assertEqual(job, app.handle(request))
            with self.assertRaisesRegex(HouseholdError, 'another intent'):
                app.handle({**request, 'language': 'nb'})
            frozen = app.store.read()['recipe_delivery']['jobs']['english']
            self.assertEqual(frozen['menu_snapshot']['dishes'][0]['translations'], recipe['translations'])
            changed = deepcopy(recipe); changed.pop('translations'); changed['name'] = 'Changed'
            app.recipes.update(reference['recipe_id'], 1, changed)
            self.assertEqual(job, app.handle(request))
            self.assertTrue(any('Carrot soup' in part.get('text', '') for part in frozen['parts']))


if __name__ == '__main__': unittest.main()
