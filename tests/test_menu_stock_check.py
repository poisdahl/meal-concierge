"""Whole-menu stock answers are explicit, exact, local and successor-scoped."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from core import HouseholdError, StateStore
import menu_planning as mp
from product_planner import menu_requirements
from recipe_quantities import read_quantity
from service import Application
from shopping_lists import effective_ingredient_decisions
import test_meal_concierge_replanning as replanning


class MenuStockCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(self.temp.name, {'household': 'Synthetic stock check', 'provider': 'none'})
        self.app = Application(self.store, None, None, external_recipe_sources={})
        self.menu = {'menu_id': 'menu_stock', 'revision': 1, 'digest': 'a' * 64, 'phase': 'draft',
            'dishes': [{'shopping_requirements': [
                {'item': 'olivenolje', 'quantity': 50, 'unit': 'ml', 'scalable': True, 'pantry': True},
                {'item': 'salt', 'quantity': 1, 'unit': 'g', 'scalable': True, 'pantry': True},
                {'item': 'pepper', 'unit': 'etter smak', 'scalable': False, 'optional': True}]},
                {'shopping_requirements': [{'item': 'olivenolje', 'quantity': 25, 'unit': 'ml', 'scalable': True}]}],
            'salads': []}
        with self.store.locked() as state:
            state['menu'] = deepcopy(self.menu)

    def get(self, **page):
        return self.app.handle({'operation': 'shopping_list', **page})

    def answer(self, listing, answers):
        return self.app.handle({'operation': 'shopping_list', 'action': 'record_stock',
            'stock_ref': listing['stock_check']['stock_ref'], 'stock_answers': answers})

    def row(self, listing, name):
        return next(row for row in listing['stock_check']['items'] if row['item'] == name)

    def test_one_bounded_stock_set_keeps_all_sources_flags_and_unknown_optionals(self):
        before = self.store.path.read_bytes()
        listing = self.get()
        check = listing['stock_check']
        self.assertEqual((check['total_items'], check['pending_count']), (3, 3))
        oil = self.row(listing, 'olivenolje')
        self.assertEqual(read_quantity(oil['quantity']), 75)
        self.assertEqual(len(oil['sources']), 2)
        self.assertTrue(oil['pantry'])
        self.assertFalse(oil['accepted_pantry_assumption'])
        pepper = self.row(listing, 'pepper')
        self.assertTrue(pepper['optional'])
        self.assertEqual(pepper['status'], 'unanswered')
        self.assertIn('unresolved_reason', pepper)
        first = self.get(limit=1)
        tail = self.get(limit=1, offset=first['stock_check']['next_offset'])
        self.assertEqual(first['stock_check']['stock_ref'], tail['stock_check']['stock_ref'])
        self.assertEqual(len(first['stock_check']['items']), 1)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_group_partial_stock_is_allocated_once_and_preserves_other_answers(self):
        listing = self.get()
        salt = self.row(listing, 'salt')
        listing = self.answer(listing, [{'requirement_id': salt['requirement_id'], 'action': 'have_all'}])
        oil = self.row(listing, 'olivenolje')
        current = self.answer(listing, [{'requirement_id': oil['requirement_id'], 'action': 'have_quantity', 'quantity': 60, 'unit': 'ml'}])
        oil_need = next(row for row in current['items'] if row['item'] == 'olivenolje')
        self.assertEqual(read_quantity(oil_need['quantity']), 15)
        self.assertEqual(read_quantity(oil_need['confirmed_pantry_quantity']), 60)
        self.assertEqual(self.row(current, 'olivenolje')['status'], 'answered')
        self.assertEqual(self.row(current, 'salt')['status'], 'answered')
        state = self.store.read()
        saved = state['menu_ingredient_decisions']['decisions']
        self.assertEqual(len(saved), 3)
        supplied = [{'source': {'collection': 'dishes', 'recipe_index': 0, 'ingredient_index': 0}, 'action': 'include'}]
        effective, _ = effective_ingredient_decisions(state, state['menu'], supplied)
        self.assertEqual(next(d for d in effective if d['source'] == supplied[0]['source'])['action'], 'have_all')
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(HouseholdError, 'changed'):
            self.answer(listing, [{'requirement_id': oil['requirement_id'], 'action': 'include'}])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_optional_unknown_needs_explicit_omit_and_required_omission_is_rejected(self):
        listing = self.get()
        pepper = self.row(listing, 'pepper')
        included = self.answer(listing, [{'requirement_id': pepper['requirement_id'], 'action': 'include'}])
        self.assertTrue(any(row['kind'] == 'unresolved' and row['item'] == 'pepper' for row in included['items']))
        omitted = self.answer(included, [{'requirement_id': pepper['requirement_id'], 'action': 'omit'}])
        self.assertFalse(any(row['item'] == 'pepper' for row in omitted['items']))
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(HouseholdError, 'optional'):
            self.answer(omitted, [{'requirement_id': self.row(omitted, 'salt')['requirement_id'], 'action': 'omit'}])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_mismatched_units_or_unknown_partial_quantity_cannot_write(self):
        listing = self.get()
        before = self.store.path.read_bytes()
        for name, unit in (('olivenolje', 'g'), ('pepper', 'g')):
            with self.subTest(name=name), self.assertRaises(HouseholdError):
                self.answer(listing, [{'requirement_id': self.row(listing, name)['requirement_id'],
                                       'action': 'have_quantity', 'quantity': 1, 'unit': unit}])
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_accepted_pantry_instruction_is_visible_without_inventing_a_stock_quantity(self):
        with self.store.locked() as state:
            state['profile']['pantry'].update(assume=['olivenolje'], assumptions_accepted=True)
        listing = self.get()
        oil = self.row(listing, 'olivenolje')
        self.assertTrue(oil['accepted_pantry_assumption'])
        self.assertEqual(oil['answered_sources'], 0)
        self.assertEqual(read_quantity(oil['remaining_quantity']), 75)


class StockSuccessorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = replanning.ReplanningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app, self.store, self.menu = self.fixture.app, self.fixture.store, self.fixture.menu
        listing = self.app.handle({'operation': 'shopping_list'})
        carrot = next(row for row in listing['stock_check']['items'] if row['item'] == 'gulrot')
        self.app.handle({'operation': 'shopping_list', 'action': 'record_stock',
            'stock_ref': listing['stock_check']['stock_ref'],
            'stock_answers': [{'requirement_id': carrot['requirement_id'], 'action': 'have_all'}]})

    def test_adding_an_independent_slot_carries_only_existing_exact_source_answers(self):
        result = self.app.handle({'operation': 'menu', 'action': 'add_slot', 'menu_ref': mp.menu_ref(self.menu),
            'idempotency_key': 'stock-new-slot', 'slot_input': {'date': '2026-09-10', 'meal_type': 'dinner',
                'portions': 2, 'reference': self.fixture.candidates[3]}})
        self.assertEqual((result['stock_answers_carried'], result['stock_answers_dropped']), (3, 0))
        state = self.store.read()
        self.assertEqual(state['menu_ingredient_decisions']['menu_ref'], mp.menu_ref(result['menu']))
        needs, unknown = menu_requirements(mp.shopping_menu(state['menu']), ingredient_decisions=state['menu_ingredient_decisions']['decisions'])
        self.assertEqual(unknown, [])
        self.assertEqual(read_quantity(needs[0]['quantity']), 200)
        listing = self.app.handle({'operation': 'shopping_list'})
        self.assertEqual(listing['stock_check']['pending_count'], 1)

    def test_replacing_or_rescaling_a_source_drops_its_answer_but_keeps_others(self):
        slot = self.menu['slots'][1]
        result = self.app.handle({'operation': 'menu', 'action': 'edit_slots', 'menu_ref': mp.menu_ref(self.menu),
            'idempotency_key': 'stock-quantity-change', 'edits': [
                {'action': 'replace', 'slot_id': slot['slot_id'], 'reference': slot['reference'], 'portions': 4}]})
        self.assertEqual((result['stock_answers_carried'], result['stock_answers_dropped']), (2, 1))
        state = self.store.read()
        needs, _ = menu_requirements(mp.shopping_menu(state['menu']), ingredient_decisions=state['menu_ingredient_decisions']['decisions'])
        self.assertEqual(read_quantity(needs[0]['quantity']), 400)
        self.assertEqual(len(result['stock_answers_dropped_sources']), 1)

    def test_unrelated_menu_identity_does_not_inherit_stock(self):
        with self.store.locked() as state:
            state['menu']['menu_id'] = 'menu_unrelated'
        listing = self.app.handle({'operation': 'shopping_list'})
        self.assertEqual(listing['stock_check']['pending_count'], 1)
        self.assertEqual(listing['stock_check']['items'][0]['answered_sources'], 0)

    def test_historical_preparation_stock_is_dropped_instead_of_reused(self):
        self.fixture.cook(self.menu['slots'][0])
        result = self.app.handle({'operation': 'menu', 'action': 'add_slot', 'menu_ref': mp.menu_ref(self.menu),
            'idempotency_key': 'stock-after-cooking', 'slot_input': {'date': '2026-09-10', 'meal_type': 'dinner',
                'portions': 2, 'reference': self.fixture.candidates[3]}})
        self.assertEqual((result['stock_answers_carried'], result['stock_answers_dropped']), (2, 1))


if __name__ == '__main__':
    unittest.main()
