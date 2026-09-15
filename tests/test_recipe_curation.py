"""Editorial estimates and verified publisher upgrades through real recipe paths."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from norwegian_grocery_curation import OrdinaryGroceryExcluded, OrdinaryGroceryPolicyError, apply
from recipe_curation import batch_mass, curate, recovered, serving_estimate
from recipe_portable import FORMAT, apply_archive, canonical_bytes, preflight_archive, write_archive
from recipe_quantities import read_quantity
from recipes import RecipeError, RecipeStore, normalize_recipe, prepare_recipe_input, scale_recipe, source_ingredient


def source_recipe():
    return normalize_recipe({'schema_version': 2, 'name': 'Bread',
        'ingredients': [source_ingredient('2 cups flour'), source_ingredient('160 ml water')],
        'steps': ['Knead, rise and bake.'], 'source': {'kind': 'wikibooks', 'external_id': '123',
        'relationship': 'adapted', 'url': 'https://en.wikibooks.org/wiki/Cookbook:Bread'},
        'rights': {'storage': 'full'}, 'external_snapshot': {'content_hash': 'a'*64, 'fetched_at':'2026-09-06T00:00:00+00:00', 'changes':'Structured source recipe'}})


class CurationTests(unittest.TestCase):
    def test_ordinary_grocery_adaptations_keep_source_wording_and_coherent_method(self):
        fish = source_recipe()
        fish['source'] = {**fish['source'], 'kind': 'themealdb', 'external_id': '52802'}
        fish['ingredients'] = [
            source_ingredient('200g Jerusalem Artichokes', item='Jerusalem Artichokes', measure='200g'),
            source_ingredient('25g grated Gruyère', item='Gruyère', measure='25g'),
        ]
        fish['steps'] = ['Grate the artichokes.', 'Sprinkle over the cheese.']
        recipe, _, classification = apply(fish, {}, pack_version='test.1', reviewed_source_hash='a' * 64)
        self.assertEqual(classification, 'adapt')
        self.assertEqual(recipe['ingredients'][0]['item'], 'Celeriac')
        self.assertEqual(recipe['ingredients'][0]['original_text'], '200g Jerusalem Artichokes')
        self.assertEqual(recipe['ingredients'][1]['item'], 'Jarlsberg')
        self.assertEqual(recipe['steps'], ['Grate the celeriac.', 'Sprinkle over the Jarlsberg.'])

        pot = source_recipe()
        pot['source'] = {**pot['source'], 'kind': 'themealdb', 'external_id': '53161'}
        pot['ingredients'] = [
            source_ingredient('150ml White Wine', item='White Wine', measure='150ml'),
            source_ingredient('800ml Chicken Stock', item='Chicken Stock', measure='800ml'),
        ]
        pot['steps'] = ['Cook with white wine and stock.']
        recipe, _, classification = apply(pot, {}, pack_version='test.1', reviewed_source_hash='a' * 64)
        self.assertEqual(classification, 'adapt')
        self.assertEqual(recipe['ingredients'][0]['item'], 'Cider vinegar')
        self.assertEqual(read_quantity(recipe['ingredients'][0]['quantity']), 10)
        self.assertEqual(read_quantity(recipe['ingredients'][1]['quantity']), 940)
        self.assertEqual(recipe['ingredients'][0]['original_text'], '150ml White Wine')
        self.assertEqual(recipe['steps'], ['Cook with stock and cider vinegar.'])
        _, _, classification = apply(source_recipe(), {}, pack_version='test.1')
        self.assertEqual(classification, 'keep')

        blocked = source_recipe()
        blocked['ingredients'] = [source_ingredient('2 tbsp ground crayfish', item='ground crayfish')]
        with self.assertRaisesRegex(OrdinaryGroceryExcluded, 'ground crayfish'):
            apply(blocked, {}, pack_version='test.1')

        for item in ('conchiglie pasta', 'striped bass', 'spirulina powder'):
            with self.subTest(item=item):
                retained = source_recipe()
                retained['ingredients'] = [source_ingredient('100 g '+item, item=item)]
                self.assertEqual(apply(retained, {}, pack_version='test.1')[2], 'keep')
        retained = source_recipe()
        retained['ingredients'] = [source_ingredient('red palm', item='red palm'),
                                   source_ingredient('oil', item='oil')]
        self.assertEqual(apply(retained, {}, pack_version='test.1')[2], 'keep')
        for item in ('yams', 'plantains', 'snails'):
            with self.subTest(blocked_plural=item):
                blocked = source_recipe()
                blocked['ingredients'] = [source_ingredient('2 '+item, item=item)]
                with self.assertRaisesRegex(OrdinaryGroceryExcluded, item):
                    apply(blocked, {}, pack_version='test.1')
        retained = source_recipe()
        retained['steps'] = ['No pressure cooker is needed.',
                             'Use a slow cooker if desired, or simmer in a pan.']
        self.assertEqual(apply(retained, {}, pack_version='test.1')[2], 'keep')

        with self.assertRaisesRegex(OrdinaryGroceryPolicyError, 'reviewed source hash'):
            apply(fish, {}, pack_version='test.1')
        drifted = deepcopy(fish)
        drifted['ingredients'][0] = source_ingredient(
            '300g Jerusalem Artichokes', item='Jerusalem Artichokes', measure='300g')
        with self.assertRaisesRegex(OrdinaryGroceryPolicyError, 'quantity no longer matches'):
            apply(drifted, {}, pack_version='test.1', reviewed_source_hash='a' * 64)

    def test_estimates_are_explicit_and_source_values_remain_exact(self):
        before = source_recipe()
        result, credit = curate(before, {}, pack_version='1')
        self.assertIsNone(before['portions'])
        self.assertEqual(result['ingredients'][1], before['ingredients'][1])
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
                         ('canned palm-nut extract',800,'ml'))
        self.assertIn('raw palm nuts', actual['original_text'])

    def test_source_bound_amendments_cannot_change_identity_or_ignore_source_issues(self):
        recipe = source_recipe()
        patch = {'source_hash': 'a'*64, 'note': 'Chosen batch', 'set': {}, 'resolved_issues': ['missing_steps']}
        for changes in [{'source_hash': 'b'*64}, {'set': {'source': {}}}, {'resolved_issues': []}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                curate(recipe, {'normalization_issues': ['missing_steps']}, pack_version='1',
                       amendments={'wikibooks:123': {**patch, **changes}})


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
