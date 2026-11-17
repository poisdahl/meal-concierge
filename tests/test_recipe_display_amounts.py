"""Kitchen-facing quantities remain exact and cannot rewrite recipe authority."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_views import _recipe_view
from core import StateStore
from recipe_quantities import display_amount, quantity_json
from recipe_languages import display_recipe, source_text_digest
from recipes import normalize_recipe, scale_recipe
from service import Application
from service_common import menu_email_html
from test_meal_concierge_products import FakeProvider


class ExactDisplayAmountTests(unittest.TestCase):
    def test_exact_counts_mixed_fractions_local_units_and_decimal_separators(self):
        for quantity, unit, language, expected in (
                (Fraction(15, 8), 'pot', 'nb', '1 7/8 potter'),
                (Fraction(3, 4), 'count', 'nb-NO', '3/4 stk'),
                (Fraction(3, 8), 'pot', 'en', '3/8 pot'),
                (Fraction(495, 4), 'ml', 'nb', '123,75 ml'),
                (Fraction(735, 8), 'milliliters', 'nb', '91,875 ml'),
                (Fraction(735, 8), 'ml', 'en', '91.875 ml'),
                (Fraction(1, 3), 'g', 'nb', '1/3 g'),
                (Fraction(1, 2500), 'kilogram', 'nb', '0,0004 kg'),
                (Fraction(3, 2), 'tbsp', 'nb', '1,5 ss'),
                (Fraction(3, 2), 'ss', 'en', '1.5 tbsp'),
                (Fraction(1), 'us cup', 'nb', '1 amerikansk kopp'),
                (Fraction(1), 'metrisk kopp', 'en', '1 metric cup')):
            with self.subTest(quantity=quantity, unit=unit, language=language):
                self.assertEqual(display_amount(quantity_json(quantity), unit, language), expected)
        for unknown in (None, True, 0, -1, {'numerator': 1, 'denominator': 0}, float('nan')):
            self.assertIsNone(display_amount(unknown, 'g'))

    def recipe(self):
        return normalize_recipe({'schema_version': 2, 'name': 'Synthetic herb oil', 'language': 'nb', 'portions': 8,
            'ingredients': [{'item': 'olivenolje', 'quantity': 245, 'unit': 'ml'},
                            {'item': 'basilikum', 'quantity': 1, 'unit': 'pot'},
                            {'item': 'løk', 'quantity': 2, 'unit': 'count'}],
            'steps': ['Bland.'], 'source': {'kind': 'user', 'relationship': 'user_supplied'}, 'rights': {'storage': 'full'}})

    def test_recipe_views_and_email_preserve_all_authoritative_amount_fields(self):
        scaled = scale_recipe(self.recipe(), 3)
        before = deepcopy(scaled)
        digest = source_text_digest(scaled)
        shown = display_recipe(scaled, 'nb')
        view = _recipe_view('get', {'recipe': scaled}, 0, 10, 'ingredients')
        # View paging is independent of normalization/storage and uses the same exact formatter.
        rows = view['ingredients']['items']
        self.assertEqual(rows[0]['display_amount'], '91,875 ml')
        self.assertEqual(rows[1]['display_amount'], '3/8 potte')
        for original, displayed in zip(scaled['ingredients'], shown['ingredients']):
            for field in ('quantity', 'unit', 'amount', 'raw', 'quantity_text'):
                self.assertEqual(displayed.get(field), original.get(field))
        rendered = menu_email_html({'week': '2026-W47', 'dishes': [scaled]})
        self.assertIn('91,875 ml olivenolje', rendered)
        self.assertIn('3/8 potte basilikum', rendered)
        self.assertEqual(scaled, before)
        self.assertEqual(source_text_digest(scaled), digest)
        self.assertEqual(normalize_recipe(scaled), normalize_recipe(before))

    def test_shopping_and_product_review_language_changes_no_reference_or_amount(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(directory, {'household': 'Synthetic amounts', 'provider': 'oda'})
            app = Application(store, FakeProvider(), object())
            menu = {'menu_id': 'menu_display', 'revision': 1, 'digest': 'a' * 64,
                    'dishes': [scale_recipe(self.recipe(), 3)], 'salads': []}
            with store.locked() as state:
                state['setup']['status'] = 'complete'
                state['menu'] = menu
            before = store.path.read_bytes()
            nb = app.handle({'operation': 'shopping_list', 'language': 'nb'})
            en = app.handle({'operation': 'shopping_list', 'language': 'en'})
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(nb['list_ref'], en['list_ref'])
            by_item = {row['item']: row for row in nb['items']}
            self.assertEqual(by_item['olivenolje']['display_amount'], '91,875 ml')
            self.assertIn('91.875 ml', en['text'])
            reference = {key: menu[key] for key in ('menu_id', 'revision', 'digest')}
            prepared = app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': reference})
            product_ref = prepared['product_plan_ref']
            original_page = app.handle({'operation': 'products', 'action': 'get', 'product_plan_ref': product_ref, 'language': 'nb'})
            page = app.handle({'operation': 'products', 'action': 'get', 'product_plan_ref': product_ref, 'language': 'en'})
            self.assertEqual(original_page['product_selection_digest'], page['product_selection_digest'])
            for original, displayed in zip(original_page['requirements'], page['requirements']):
                self.assertEqual(original['quantity'], displayed['quantity'])
                self.assertEqual(original['unit'], displayed['unit'])
            self.assertTrue(any(row.get('display_amount') == '91.875 ml' for row in page['requirements']))


if __name__ == '__main__':
    unittest.main()
