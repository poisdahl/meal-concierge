"""Source culinary measures retain their dimensions through cooking and shopping."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError, StateStore
from product_observations import parse_package
from product_planner import build_product_plan, cart_requirements, menu_requirements
from recipe_curation import batch_mass
from recipe_languages import display_recipe, source_text_digest
from recipe_quantities import parse_measure, read_quantity
from recipes import normalize_recipe, scale_recipe, source_ingredient
from service import Application
from test_meal_concierge_products import FakeProvider, observation, option, product


ALIASES = {
    'drop': ('drop', 'drops', 'dråpe', 'dråper'),
    'pinch': ('pinch', 'pinches', 'klype', 'klyper'),
    'handful': ('handful', 'handfuls', 'håndfull', 'håndfuller', 'neve', 'never'),
    'slice': ('slice', 'slices', 'skive', 'skiver'),
    'bunch': ('bunch', 'bunches', 'bunt', 'bunter', 'knippe', 'knipper'),
    'pot': ('pot', 'pots', 'potte', 'potter'),
    'sheet': ('sheet', 'sheets', 'ark'),
    'thumb': ('thumb', 'thumbs', 'tommel', 'tomler'),
}


def fixture(*ingredients):
    return normalize_recipe({
        'schema_version': 2, 'name': 'Synthetic culinary measures', 'language': 'nb',
        'portions': 2, 'portions_evidence': {'basis': 'user', 'input': 'Two servings.'},
        'ingredients': list(ingredients), 'steps': ['Use the listed cooking measures.'],
        'source': {'kind': 'user', 'relationship': 'user_supplied'},
        'rights': {'storage': 'full'},
    })


class SourceUnitTests(unittest.TestCase):
    def test_source_aliases_scale_exact_ratios_and_preserve_evidence(self):
        for dimension, aliases in ALIASES.items():
            for alias in aliases:
                with self.subTest(unit=alias):
                    original = fixture(source_ingredient(f'1/2 {alias} salt', language='nb'))
                    self.assertTrue(original['ingredients'][0]['scalable'])
                    scaled = scale_recipe(original, 3)
                    row = scaled['ingredients'][0]
                    self.assertEqual(read_quantity(row['quantity']), Fraction(3, 4))
                    self.assertEqual(row['unit'], alias)
                    self.assertEqual(row['original_text'], f'1/2 {alias} salt')
                    self.assertEqual(row['evidence']['unit']['basis'], 'source')
                    calculation = row['evidence']['quantity']['calculation']
                    self.assertEqual(read_quantity(calculation['factor']), Fraction(3, 2))
                    requirements, unresolved = menu_requirements({'dishes': [scaled], 'salads': []})
                    self.assertEqual(unresolved, [])
                    self.assertEqual(requirements[0]['unit'], dimension)
                    self.assertEqual(read_quantity(requirements[0]['quantity']), Fraction(3, 4))
                    rescaled = normalize_recipe(scale_recipe(scaled, 8))
                    self.assertEqual(read_quantity(rescaled['ingredients'][0]['quantity']), 2)
                    self.assertEqual(rescaled['ingredients'][0]['original_text'], row['original_text'])

    def test_only_same_dimension_aliases_aggregate_and_pantry_requires_compatibility(self):
        recipe = fixture(*[
            source_ingredient(f'1 {unit} salt', language='nb')
            for unit in (*ALIASES, 'g', 'ml', 'stk', 'pk', 'håndfull')
        ])
        menu = {'dishes': [scale_recipe(recipe, 2)], 'salads': []}
        requirements, unresolved = menu_requirements(menu)
        self.assertEqual(unresolved, [])
        quantities = {row['unit']: read_quantity(row['quantity']) for row in requirements}
        self.assertEqual(set(quantities), {*ALIASES, 'g', 'ml', 'count', 'package'})
        self.assertEqual(quantities['handful'], 2)
        self.assertTrue(all(amount == 1 for unit, amount in quantities.items() if unit != 'handful'))
        with self.assertRaisesRegex(HouseholdError, 'exact compatible unit'):
            menu_requirements(menu, ingredient_decisions=[{
                'source': {'collection': 'dishes', 'recipe_index': 0, 'ingredient_index': 2},
                'action': 'have_quantity', 'quantity': 100, 'unit': 'g',
            }])

    def test_explicit_unscalable_and_missing_amounts_remain_unresolved(self):
        row = source_ingredient('2 handfuls salt', language='en')
        row['scalable'] = False
        recipe = fixture(row)
        scaled = scale_recipe(recipe, 6)
        self.assertFalse(scaled['readiness']['scaling_ready'])
        self.assertFalse(scaled['ingredients'][0]['scalable'])
        self.assertEqual(read_quantity(scaled['ingredients'][0]['quantity']), 2)
        self.assertTrue(menu_requirements({'dishes': [scaled], 'salads': []})[1])
        missing = fixture(source_ingredient('a generous handful salt', language='en'))
        requirements, unresolved = menu_requirements({'dishes': [scale_recipe(missing, 6)], 'salads': []})
        self.assertEqual(requirements, [])
        self.assertTrue(unresolved)
        self.assertEqual(parse_measure('a generous handful'), (None, None))
        self.assertEqual(parse_measure('1 cup'), (None, 'cup'))

    def test_ambiguous_plate_servings_do_not_become_sheets(self):
        for text, language in [('1 plate rice', 'en'), ('1 plate sjokolade', 'nb')]:
            with self.subTest(text=text):
                row = source_ingredient(text, language=language)
                self.assertFalse(row['scalable'])
                self.assertTrue(menu_requirements({'dishes': [scale_recipe(fixture(row), 2)], 'salads': []})[1])
        self.assertEqual(parse_measure('1 plate'), (None, 'plate'))

    def test_culinary_counts_never_supply_guessed_mass(self):
        for aliases in ALIASES.values():
            for alias in aliases:
                for item in ('basil', 'salt', 'bread', '100 g almonds'):
                    with self.subTest(unit=alias, item=item):
                        self.assertEqual(batch_mass({'item': item, 'quantity': 1, 'unit': alias}), 0)
        self.assertEqual(batch_mass({'item': 'garlic', 'quantity': 1, 'unit': 'clove'}), 5)
        self.assertEqual(batch_mass({'item': 'bread', 'quantity': 100, 'unit': 'g'}), 100)

    def test_english_display_keeps_canonical_source_units_and_quantities(self):
        units = [aliases[2] for aliases in ALIASES.values()] + [
            'fedd', 'clove', 'cloves', 'stilk', 'stilker', 'stalk', 'stalks',
            'pk', 'pakke', 'pakker', 'package', 'packages',
        ]
        english = ('drops', 'pinches', 'handfuls', 'slices', 'bunches', 'pots', 'sheets', 'thumbs',
                   *(['cloves'] * 3), *(['stalks'] * 4), *(['packages'] * 5))
        rows = [source_ingredient(f'2 {unit} salt', language='nb') for unit in units]
        recipe = fixture(*rows)
        recipe['translations'] = {'en': {
            'name': 'Synthetic culinary measures', 'steps': ['Use the listed cooking measures.'],
            'ingredients': [{'item': 'salt'} for _ in rows],
            'source_text_digest': source_text_digest(recipe),
        }}
        recipe = normalize_recipe(recipe)
        before = deepcopy(recipe)
        displayed = display_recipe(scale_recipe(recipe, 6), 'en')
        self.assertEqual([row['amount'] for row in displayed['ingredients']], [f'6 {unit}' for unit in english])
        self.assertEqual([row['unit'] for row in displayed['ingredients']], units)
        self.assertEqual(recipe, before)

    def test_retail_mass_volume_and_piece_counts_need_explicit_practical_packages(self):
        for dimension in ALIASES:
            value = {'dishes': [scale_recipe(fixture(source_ingredient(f'2 {dimension} salt')), 6)], 'salads': []}
            requirement = menu_requirements(value)[0][0]
            for retail_unit in ('g', 'ml', 'count'):
                with self.subTest(source_unit=dimension, retail_unit=retail_unit):
                    selected = product('10', 'Salt', 100, retail_unit, [option(1000)])
                    selected['package']['contained_count'] = 20
                    arguments = dict(provider='oda', binding={}, menu=value,
                        observations={requirement['requirement_id']: observation('salt', [selected])})
                    choice = {'requirement_id': requirement['requirement_id'], 'candidate_refs': ['10']}
                    unresolved = build_product_plan(**arguments, candidate_approvals=[choice])
                    self.assertEqual(unresolved['status'], 'needs_input')
                    problem = unresolved['unresolved_requirements'][0]
                    self.assertEqual(problem['reason'], 'candidate_package_incompatible')
                    self.assertEqual(problem['repair']['kind'], 'practical_package_estimate')
                    choice.update(package_count=1, quantity_basis='One observed retail package chosen as a practical cooking estimate.')
                    prepared = build_product_plan(**arguments, candidate_approvals=[choice])
                    selection = prepared['requirements'][0]['selection']
                    self.assertEqual(prepared['status'], 'prepared')
                    self.assertEqual(prepared['coverage_status'], 'practical_estimate')
                    self.assertIsNone(prepared['comparison_claim'])
                    self.assertEqual(selection['unit'], dimension)
                    self.assertIsNone(selection['coverage'])
                    self.assertIsNone(selection['surplus_quantity'])
                    self.assertEqual(cart_requirements(prepared)[0]['quantity'], 1)
            self.assertIsNone(parse_package(f'5 {dimension}'))

    def test_service_menu_practical_package_choice_and_cart_revalidation(self):
        class Provider(FakeProvider):
            price = 1000

            def call(self, tool_name, arguments, **kwargs):
                if tool_name == 'product_search':
                    self.calls.append((tool_name, deepcopy(arguments)))
                    return observation(arguments['queries'][0], [product('10', 'Salt', 100, 'g', [option(self.price)])])
                return super().call(tool_name, arguments, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            provider = Provider()
            store = StateStore(Path(root), {'instance': 'source-units', 'household': 'Synthetic', 'profile_overrides': {}})
            app = Application(store, provider, object())
            app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})
            row = source_ingredient('1/2 håndfull salt', language='nb')
            for evidence in row['evidence'].values():
                evidence['basis'] = 'user'
            saved = app.handle({'operation': 'recipes', 'action': 'save',
                'recipe': fixture(row), 'idempotency_key': 'source-units'})['recipe']
            menu = app.handle({'operation': 'menu', 'action': 'save', 'menu': {
                'week': '2026-W40', 'dishes': [{'recipe_ref': {'id': saved['id'], 'revision': saved['revision']}, 'portions': 6}],
                'salads': []}})['menu']
            self.assertTrue(menu['dishes'][0]['readiness']['scaling_ready'])
            self.assertEqual(read_quantity(menu['dishes'][0]['ingredients'][0]['quantity']), Fraction(3, 2))
            requirement = menu_requirements(menu)[0][0]
            request = {'operation': 'products', 'action': 'prepare', 'menu_ref': app._cart_menu_ref(menu)}
            choice = {'requirement_id': requirement['requirement_id'], 'candidate_refs': ['10']}
            blocked = app.handle({**request, 'candidate_approvals': [choice]})
            self.assertEqual(blocked['product_plan']['status'], 'needs_input')
            basis = 'One observed salt package chosen as a practical estimate for the source handful measure.'
            choice.update(package_count=1, quantity_basis=basis)
            prepared = app.handle({**request, 'candidate_approvals': [choice]})
            self.assertEqual(prepared['product_plan']['status'], 'prepared')
            self.assertEqual(prepared['product_plan']['coverage_status'], 'practical_estimate')
            provider.price = 1100
            apply = {'operation': 'products', 'action': 'apply', **prepared['apply_arguments'], 'cart_change_requested': True}
            self.assertFalse(app.handle(apply)['applied'])
            self.assertEqual(provider.cart['items'], [])
            provider.price = 1000
            self.assertTrue(app.handle(apply)['applied'])
            self.assertTrue(app.handle(apply)['applied'])
            self.assertEqual(provider.cart['count'], 1)
            summary = store.read()['cart_plan']['product_plan_summary']
            self.assertEqual(summary['coverage_status'], 'practical_estimate')
            self.assertEqual(summary['quantity_estimates'][0]['quantity_basis'], basis)
            reread = Application(store, provider, object()).handle({
                'operation': 'recipes', 'action': 'get', 'recipe_id': saved['id']})['recipe']
            self.assertEqual(reread['ingredients'][0]['unit'], 'håndfull')
            self.assertEqual(read_quantity(reread['ingredients'][0]['quantity']), Fraction(1, 2))


if __name__ == '__main__':
    unittest.main()
