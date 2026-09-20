"""Editorial estimates and verified publisher upgrades through real recipe paths."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recipe_curation import apply_amendment, batch_mass, canonical_recipe_hash, curate, recovered, serving_estimate
from recipe_pack_sources import _ingredient
from recipe_portable import FORMAT, apply_archive, canonical_bytes, preflight_archive, write_archive
from recipe_quantities import read_quantity
from product_planner import menu_requirements
from recipe_delivery import render_email, render_menu, render_pdf
from recipes import RecipeError, RecipeStore, normalize_recipe, prepare_recipe_input, scale_recipe, source_ingredient


def source_recipe():
    return normalize_recipe({'schema_version': 2, 'name': 'Bread',
        'ingredients': [source_ingredient('2 cups flour'), source_ingredient('160 ml water')],
        'steps': ['Knead, rise and bake.'], 'source': {'kind': 'wikibooks', 'external_id': '123',
        'relationship': 'adapted', 'url': 'https://en.wikibooks.org/wiki/Cookbook:Bread'},
        'rights': {'storage': 'full'}, 'external_snapshot': {'content_hash': 'a'*64, 'fetched_at':'2026-09-06T00:00:00+00:00', 'changes':'Structured source recipe'}})


class CurationTests(unittest.TestCase):
    def test_estimates_are_explicit_and_source_values_remain_exact(self):
        before = source_recipe()
        result, credit = curate(before, {}, pack_version='1')
        self.assertIsNone(before['portions'])
        self.assertEqual(result['ingredients'][1]['item'], 'vann')
        self.assertEqual(result['ingredients'][1]['quantity'], before['ingredients'][1]['quantity'])
        self.assertEqual(result['ingredients'][1]['original_text'], before['ingredients'][1]['original_text'])
        self.assertEqual(read_quantity(result['ingredients'][0]['quantity']), 240)
        self.assertEqual(result['ingredients'][0]['unit'], 'g')
        self.assertEqual(result['ingredients'][0]['original_text'], '2 cups flour')
        self.assertEqual(result['portions_evidence']['basis'], 'estimate')
        self.assertTrue(result['portions_evidence']['assumptions'])
        self.assertNotIn('acceptance', result['portions_evidence'])
        self.assertEqual(credit['curation']['unresolved'], [])
        self.assertTrue(scale_recipe(result, 4)['readiness']['scaling_ready'])
        with self.assertRaisesRegex(RecipeError, 'project review'):
            prepare_recipe_input(result)
        self.assertEqual(prepare_recipe_input({**result, 'notes': 'My note'}, prior=result)['notes'], 'My note')
        changed = deepcopy(result)
        changed['portions'] += 1
        with self.assertRaisesRegex(RecipeError, 'project review'):
            prepare_recipe_input(changed, prior=result)

    def test_unknown_food_is_not_invented_or_reviewed(self):
        before = source_recipe()
        before['ingredients'] = [source_ingredient('Unspecified mystery filling')]
        result, credit = curate(before, {}, pack_version='1')
        self.assertFalse(result['ingredients'][0]['scalable'])
        self.assertTrue(credit['curation']['unresolved'])
        self.assertTrue(any(not row['scalable'] for row in scale_recipe(result)['shopping_requirements']))

        before['ingredients'] = [source_ingredient('100 g palm oil', language='en')]
        result, credit = curate(before, {}, pack_version='1')
        self.assertEqual(result['ingredients'][0]['item'], 'palm oil')
        self.assertEqual(result['ingredients'][0]['original_text'], '100 g palm oil')
        self.assertFalse(result['ingredients'][0]['scalable'])
        self.assertIn('ingredients.0.identity_review_required', credit['curation']['unresolved'])

    def test_pack_source_identity_mapping_is_deferred_until_after_curation(self):
        before = source_recipe()
        before['ingredients'] = [_ingredient(text) for text in (
            '2 cups flour', '1 cup olive oil', '160 ml water',
            '6 garlic cloves', '2 chicken breasts (400 g)', '3 cloves',
        )]
        self.assertEqual([row['item'] for row in before['ingredients']], [
            '2 cups flour', '1 cup olive oil', 'water', 'garlic cloves',
            '2 chicken breasts (400 g)', '3 cloves',
        ])
        result, credit = curate(before, {}, pack_version='1')
        self.assertEqual([row['item'] for row in result['ingredients']], [
            'hvetemel', 'olivenolje', 'vann', 'hvitløk', 'kyllingbryst', 'nellikspiker',
        ])
        self.assertTrue(all(row['scalable'] for row in result['ingredients']))
        self.assertEqual(credit['curation']['unresolved'], [])

    def test_recovery_preserves_metric_and_does_not_read_unicode_unit_as_grams(self):
        for text, quantity, unit in [('2 gō (300 g) sushi rice', 300, 'g'),
                                     ('scant ½ cup (100 ml) olive oil', 100, 'ml'),
                                     ('A little over ¾ cup (200 ml) milk', 200, 'ml'),
                                     ('2 cups coconut milk', 480, 'ml'),
                                     ('500g flour', 500, 'g')]:
            with self.subTest(text=text):
                row = recovered(source_ingredient(text))
                self.assertEqual(read_quantity(row['quantity']), quantity)
                self.assertEqual(row['unit'], unit)

    def test_yield_dimension_and_accompanying_sauce_do_not_multiply_servings(self):
        recipe = source_recipe()
        recipe['name'] = 'Rice with Tomato Sauce'
        recipe['ingredients'] = [source_ingredient('300 g rice'), source_ingredient('200 g tomatoes')]
        portions, _ = serving_estimate(recipe)
        self.assertEqual(portions, 3)
        recipe['name'] = 'Stollen'
        recipe['yield'] = {'original_text': '14 inch cake'}
        self.assertLess(serving_estimate(recipe)[0], 20)

    def test_small_food_counts_use_distinct_piece_weights(self):
        for text, expected in [('6 garlic cloves',30), ('3 cloves',0.6), ('12 cherry tomatoes',180), ('24 king prawns',480)]:
            with self.subTest(text=text):
                self.assertAlmostEqual(batch_mass(recovered(source_ingredient(text))), expected)

    def test_parenthesized_equivalent_keeps_food_before_preparation(self):
        for text, food, expected in [
                ('1½ sticks butter (170 g or ¾ cup), cut into small pieces', 'butter',170),
                ('2 chicken breasts (400 g / 14 oz), chopped', 'chicken breasts',400),
                ('4 boneless skinless chicken breasts (about 6 ounces each), grilled', 'chicken breasts',24)]:
            with self.subTest(text=text):
                result=recovered(source_ingredient(text))
                self.assertIn(food,result['item'])
                self.assertEqual(read_quantity(result['quantity']),expected)

    def test_additive_lower_bound_and_plural_yields(self):
        for text, amount, unit in [('20g + 20g Parmesan',40,'g'),('1 tablespoon plus 2 cups vegetable oil, divided',495,'ml'),('At least 50 wonton wrappers',50,'count'),('At least 1 quart (1 L) maple syrup',1,'l')]:
            with self.subTest(text=text):
                row=recovered(source_ingredient(text))
                self.assertEqual((read_quantity(row['quantity']),row['unit']),(amount,unit))
        recipe=source_recipe();recipe['yield']={'original_text':'2 pies'}
        self.assertEqual(serving_estimate(recipe)[0],16)
        recipe['yield']=None;recipe['name']='Rice';recipe['ingredients']=[source_ingredient('320 g uncooked rice')]
        self.assertEqual(serving_estimate(recipe)[0],4)
        recipe['name']='Slagroomtaart';recipe['tags']=['Dessert'];recipe['ingredients']=[source_ingredient('100 g flour'),source_ingredient('750 ml cream'),source_ingredient('200 g sugar')]
        self.assertGreaterEqual(serving_estimate(recipe)[0],10)

    def test_english_additive_recovery_uses_reviewed_norwegian_identity(self):
        row = recovered(source_ingredient('20 g + 20 g olive oil'))
        self.assertEqual(row['item'], 'olivenolje')
        self.assertEqual(read_quantity(row['quantity']), 40)
        self.assertEqual(row['unit'], 'g')
        self.assertEqual(row['original_text'], '20 g + 20 g olive oil')
        self.assertTrue(row['scalable'])
        for text in ('200 g unfamiliar ingredient', '2 cups exotic powder'):
            with self.subTest(text=text):
                unresolved = recovered(source_ingredient(text, language='en'))
                self.assertFalse(unresolved['scalable'])

    def test_editorial_choice_survives_preserved_original_alternatives(self):
        before = source_recipe()
        row = source_ingredient('4 cups raw palm nuts or 800 ml canned palm nut extract')
        row.update(item='canned palm-nut extract', quantity={'numerator':800,'denominator':1}, unit='ml', scalable=True,
            evidence={key:{'basis':'estimate','input':'Meal Concierge editorial adaptation',
                'assumptions':'Selected the explicit prepared extract alternative.'} for key in ('quantity','unit')})
        result, _ = curate(before, {}, pack_version='1', amendments={'wikibooks:123':{
            'source_hash':'a'*64,'note':'Prepared extract selected.','resolved_issues':[], 'set':{'ingredients':[row]}}})
        actual = result['ingredients'][0]
        self.assertEqual((actual['item'], read_quantity(actual['quantity']), actual['unit']),
                         ('hermetisk palmenøttekstrakt',800,'ml'))
        self.assertIn('raw palm nuts', actual['original_text'])

    def test_source_bound_amendments_cannot_change_identity_or_ignore_source_issues(self):
        recipe = source_recipe()
        patch = {'source_hash': 'a'*64, 'note': 'Chosen batch', 'set': {}, 'resolved_issues': ['missing_steps']}
        for changes in [{'source_hash': 'b'*64}, {'set': {'source': {}}}, {'resolved_issues': []}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                curate(recipe, {'normalization_issues': ['missing_steps']}, pack_version='1',
                       amendments={'wikibooks:123': {**patch, **changes}})

    def test_reviewed_translation_is_dual_bound_and_keeps_runtime_notes(self):
        recipe = source_recipe()
        recipe.update(language='en-GB', portions=4,
                      portions_evidence={'basis':'source','input':'Serves 4','conversion':None},
                      notes='Keep refrigerated for 2 days.',
                      storage='Keep refrigerated for 2 days.', reheating='Reheat for 5 minutes.',
                      steps=['Bake at 180C/350F/Gas 4 for 15-20 minutes; do not add water.'])
        recipe = normalize_recipe(recipe)
        before = deepcopy(recipe)
        payload_hash = 'b' * 64
        amendment = {
            'source_hash': 'a' * 64,
            'source_identity': 'wikibooks:123',
            'source_payload_hash': payload_hash,
            'curated_recipe_hash': canonical_recipe_hash(recipe),
            'note': 'Independently reviewed Norwegian active fields.',
            'resolved_issues': [],
            'set': {
                'name': 'Brød', 'language': 'nb-NO',
                'steps': ['Stek ved 180C/350F/Gas 4 i 15-20 minutter; ikke tilsett vann.'],
                'notes': 'Oppbevares kjølig i 2 dager.',
                'storage': 'Oppbevares kjølig i 2 dager.',
                'reheating': 'Varm opp i 5 minutter.',
            },
        }
        result, credit = apply_amendment(recipe, {}, {'wikibooks:123': amendment},
                                         source_payload_hash=payload_hash)
        self.assertEqual(result['notes'], 'Oppbevares kjølig i 2 dager.')
        self.assertEqual(result['language'], 'nb-NO')
        self.assertEqual(result['storage'], 'Oppbevares kjølig i 2 dager.')
        self.assertEqual(result['reheating'], 'Varm opp i 5 minutter.')
        self.assertEqual(result['steps'][0], 'Stek ved 180C/350F/Gas 4 i 15-20 minutter; ikke tilsett vann.')
        for field in ('ingredients', 'source', 'rights', 'external_snapshot', 'portions', 'yield'):
            self.assertEqual(result[field], before[field])
        self.assertEqual(credit['editorial_adaptation'], amendment['note'])
        baseline_scaled = scale_recipe(before, 4)
        translated_scaled = scale_recipe(result, 4)
        self.assertEqual(translated_scaled['shopping_requirements'], baseline_scaled['shopping_requirements'])
        menu = {'week': '2026-W39', 'dishes': [translated_scaled], 'salads': []}
        requirements, unresolved = menu_requirements(menu)
        self.assertEqual((requirements, unresolved), menu_requirements(
            {'week': '2026-W39', 'dishes': [baseline_scaled], 'salads': []}))
        rendered = render_menu(menu, None, images=False)
        self.assertIn('Brød', rendered['text'])
        self.assertIn('180C/350F/Gas 4', rendered['text'])
        self.assertIn('Oppbevares kjølig i 2 dager.', rendered['text'])
        pdf = render_pdf(rendered)
        self.assertTrue(pdf.startswith(b'%PDF'))
        message = render_email(rendered, recipient='owner@example.test', sender='sender@example.test',
                               subject='Ukesmeny 2026-W39', pdf=pdf)
        self.assertIn(b'application/pdf', message)

        for changed in (
                {'source_payload_hash': 'c' * 64},
                {'curated_recipe_hash': 'd' * 64},
                {'source_identity': 'wikibooks:999'},
                {'source_payload_hash': None}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                apply_amendment(deepcopy(before), {}, {'wikibooks:123': {**amendment, **changed}},
                                source_payload_hash=payload_hash)
        without_notes = deepcopy(amendment)
        without_notes['set'].pop('notes')
        without_notes['curated_recipe_hash'] = canonical_recipe_hash(before)
        preserved, _ = apply_amendment(deepcopy(before), {}, {'wikibooks:123': without_notes},
                                       source_payload_hash=payload_hash)
        self.assertEqual(preserved['notes'], before['notes'])
        protected = deepcopy(amendment)
        protected['set'] = {'ingredients': [{**before['ingredients'][0], 'quantity': 999, 'unit': 'kg'}]}
        for value in (protected, {**amendment, 'omit_cover': True}):
            with self.subTest(protected=value), self.assertRaisesRegex(ValueError, 'protected recipe fields'):
                apply_amendment(deepcopy(before), {}, {'wikibooks:123': value},
                                source_payload_hash=payload_hash)
        with self.assertRaisesRegex(ValueError, 'complete source and curated recipe binding'):
            apply_amendment(deepcopy(before), {}, {'wikibooks:123': {
                'source_hash': 'a' * 64, 'note': 'Unbound language change.',
                'resolved_issues': [], 'set': {'language': 'nb-NO'},
            }})

    def test_legacy_amendment_still_puts_review_explanation_in_notes(self):
        result, credit = apply_amendment(source_recipe(), {}, {'wikibooks:123': {
            'source_hash': 'a' * 64, 'note': 'Legacy explanation.',
            'resolved_issues': [], 'set': {'name': 'Legacy bread'},
        }})
        self.assertEqual(result['notes'], 'Meal Concierge editorial adaptation: Legacy explanation.')
        self.assertEqual(credit['editorial_adaptation'], 'Legacy explanation.')


class PublisherUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root/'state'
        self.state.mkdir()

    def package(self, version, *, marker_version=None):
        recipe, _ = curate(source_recipe(), {}, pack_version=marker_version or version)
        recipe['notes'] = 'Publisher wording '+version
        record = {'recipe_id': 'wikibooks:123', 'status': 'ready', 'recipe': recipe}
        records = self.root/('records-'+version+'.jsonl')
        records.write_bytes(canonical_bytes(record)+b'\n')
        manifest = {'format': FORMAT, 'format_version': 1, 'kind': 'bundled',
            'pack_id': 'wikibooks-themealdb-en', 'pack_version': version,
            'normalizer_version': '2', 'recipe_schema_version': 2, 'records_count': 1}
        path = self.root/('pack-'+version+'.zip')
        write_archive(path, manifest, {'records.jsonl': records})
        descriptor = {key: manifest[key] for key in ('format','format_version','kind','pack_id','pack_version','normalizer_version','recipe_schema_version')}
        descriptor.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return path, descriptor

    def test_verified_pack_marker_binding_and_repeated_upgrades_preserve_local_intent(self):
        bad, descriptor = self.package('bad', marker_version='wrong')
        with self.assertRaisesRegex(RecipeError, 'project review'):
            preflight_archive(bad, descriptor)
        self.assertEqual(list(self.state.iterdir()), [])
        for local_status in ('archived', 'draft', None):
            with self.subTest(local_status=local_status):
                state = self.root/str(local_status)
                state.mkdir()
                # Each scenario uses the same immutable archives.
                if local_status == 'archived':
                    packages = [self.package(str(n)) for n in (1,2,3)]
                path, descriptor = packages[0]
                report = apply_archive(path, state, 'synthetic', descriptor)
                store = RecipeStore(state/'recipes.sqlite3', 'synthetic')
                ref = report['results'][0]['bank_recipe_ref']
                first = store.get(ref['recipe_id'])
                store.set_favorite(ref, True, idempotency_key='favorite')
                if local_status == 'archived': store.archive(first['id'], first['revision'])
                elif local_status == 'draft': store.update(first['id'], first['revision'], normalize_recipe(first), status='draft')
                for path, descriptor in packages[1:]:
                    result = apply_archive(path, state, 'synthetic', descriptor)
                    self.assertEqual(result['updated'], 1)
                    saved = store.get(first['id'])
                    self.assertEqual(saved['status'], local_status or 'active')
                    self.assertTrue(saved['is_favorite'])
                    self.assertEqual(normalize_recipe(store.get(first['id'], 1)), normalize_recipe(first))
                    self.assertEqual(apply_archive(path, state, 'synthetic', descriptor)['unchanged'], 1)
                saved = store.get(first['id'])
                store.update(saved['id'], saved['revision'], {**normalize_recipe(saved), 'notes': 'Local cooking note'})
                before = store.get(saved['id'])
                result = apply_archive(path, state, 'synthetic', descriptor)
                self.assertEqual(result['conflicts'], 1)
                self.assertEqual(store.get(saved['id']), before)


if __name__ == '__main__':
    unittest.main()
