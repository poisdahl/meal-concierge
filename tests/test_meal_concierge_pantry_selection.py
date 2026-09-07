"""Request stock through real menu selection/save and shopping aggregation."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import HouseholdError, StateStore
from planner import canonical
from product_planner import menu_requirements, build_product_plan, normalize_available_ingredients
from service import Application
from test_meal_concierge_planner import CONFIG, NoProviderCalls, recipe


class PantrySelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name), CONFIG)
        self.provider = NoProviderCalls()
        self.app = Application(self.store, self.provider, object())
        self.app._now = lambda: datetime(2026, 9, 7, 8, tzinfo=timezone.utc)
        with self.store.locked() as state:
            state['setup']['status'] = 'complete'
            state['profile']['recipes']['sources'] = {k: k == 'internal' for k in state['profile']['recipes']['sources']}

    def save(self, name, identity, ingredient='ris', amount=400, unit='g'):
        value = recipe(name, identity, ingredient=ingredient, unit=unit)
        value['ingredients'][0].update(quantity=amount, raw=f'{amount} {unit} {ingredient}')
        saved = self.app.handle({'operation': 'recipes', 'action': 'save', 'recipe': value,
                                 'idempotency_key': 'save-'+identity})['recipe']
        return {'recipe_ref': {'id': saved['id'], 'revision': saved['revision']}}

    def plan(self, refs=None, days=1, **extra):
        request = {'week': '2026-W37', 'dates': [f'2026-09-{7+i:02d}' for i in range(days)],
                   'portions': 2, **extra}
        if refs is not None:
            request['candidates'] = refs
        return self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': request})['plan']

    def menu(self, plan):
        return self.app.handle({'operation': 'menu', 'action': 'save', 'planner_ref': plan['save_ref']})['menu']

    def test_explicit_stock_changes_selection_and_empty_input_preserves_result(self):
        a = self.save('Gulrotmiddag', 'carrot', 'gulrot')
        b = self.save('Brokkolimiddag', 'broccoli', 'brokkoli')
        baseline = self.plan([a, b])
        self.assertEqual(baseline, self.plan([a, b], available_ingredients=[]))
        chosen = self.plan([a, b], available_ingredients=[{'item': 'brokkoli', 'use_first': True}])
        self.assertEqual(chosen['selection']['slots'][0]['reference'], b)
        reasons = chosen['selection']['slots'][0]['reason_contributions']
        stock = next(x for x in reasons if x['code'] == 'pantry:explicit_request')
        self.assertEqual(stock['detail']['coverage'], 'not_established')
        menu = self.menu(chosen)
        requirements, unresolved = menu_requirements(menu)
        self.assertFalse(unresolved)
        self.assertEqual(requirements[0]['quantity'], {'numerator': 400, 'denominator': 1})
        self.assertFalse(self.provider.calls)

    def test_whole_menu_subtracts_500_once_from_two_400_meals_and_survives_restart(self):
        refs = [self.save('Rismiddag A', 'rice-a'), self.save('Rismiddag B', 'rice-b')]
        plan = self.plan(refs, days=2, available_ingredients=[{'item': 'RIS', 'quantity': 500, 'unit': 'g'}])
        menu = self.menu(plan)
        before = self.store.read()
        for _ in range(2):
            needs, missing = menu_requirements(menu)
            self.assertFalse(missing)
            self.assertEqual(needs[0]['quantity'], {'numerator': 300, 'denominator': 1})
            self.assertEqual(needs[0]['gross_quantity'], {'numerator': 800, 'denominator': 1})
            self.assertEqual(needs[0]['confirmed_pantry_quantity'], {'numerator': 500, 'denominator': 1})
        restarted = Application(StateStore(Path(self.temp.name), CONFIG), self.provider, object())
        self.assertEqual(menu_requirements(restarted.handle({'operation': 'menu', 'action': 'get'})['menu']), (needs, missing))
        self.assertEqual(before, self.store.read())
        self.assertFalse(self.provider.calls)

    def test_later_source_decisions_replace_request_stock_without_double_subtraction(self):
        refs = [self.save('Rismiddag A', 'rice-a'), self.save('Rismiddag B', 'rice-b')]
        menu = self.menu(self.plan(refs, days=2, available_ingredients=[{'item': 'ris', 'quantity': 500, 'unit': 'g'}]))
        decisions = [{'source': {'collection': 'dishes', 'recipe_index': i, 'ingredient_index': 0},
                      'action': 'have_quantity', 'quantity': amount, 'unit': 'g'} for i, amount in enumerate([400, 100])]
        needs, missing = menu_requirements(menu, ingredient_decisions=decisions)
        self.assertFalse(missing)
        self.assertEqual(needs[0]['quantity']['numerator'], 300)
        # A subsequent include is an explicit buy choice, not another stock grant.
        decisions = [{'source': decisions[0]['source'], 'action': 'include'}]
        self.assertEqual(menu_requirements(menu, ingredient_decisions=decisions)[0][0]['quantity']['numerator'], 800)

    def test_incompatible_unknown_units_and_distinct_names_never_reduce_purchase(self):
        ref = self.save('Rismiddag', 'rice')
        for item in [{'item': 'ris', 'quantity': 500}, {'item': 'ris', 'quantity': 500, 'unit': 'ml'},
                     {'item': 'ris', 'quantity': 1, 'unit': 'håndfull'}, {'item': 'rismel', 'quantity': 500, 'unit': 'g'}]:
            with self.subTest(item=item):
                result, resolved, request = self.app._plan_menu({'week': '2026-W37', 'dates': ['2026-09-07'], 'candidates': [ref], 'portions': 2, 'available_ingredients': [item]})
                menu = self.app._materialize_planner_menu(result['save_handoff'], resolved)
                self.assertEqual(menu_requirements(menu)[0][0]['quantity']['numerator'], 400)
        with self.assertRaises(HouseholdError):
            self.plan([ref], available_ingredients=[{'item': 'ris'}, {'item': ' RIS '}])
        for invalid in [False, {}, 'ris', [{'item': 'ris', 'quantity': True}], [{'item': 'ris', 'use_first': 'yes'}]]:
            with self.subTest(invalid=invalid), self.assertRaises(HouseholdError):
                self.plan([ref], available_ingredients=invalid)

    def test_automatic_source_retrieval_uses_stock_but_cannot_override_hard_constraints(self):
        self.save('Gulrotmiddag', 'carrot', 'gulrot')
        b = self.save('Brokkolimiddag', 'broccoli', 'brokkoli')
        plan = self.plan(available_ingredients=[{'item': 'brokkoli', 'use_first': True}])
        self.assertEqual(plan['selection']['slots'][0]['reference'], b)
        with self.store.locked() as state:
            state['profile']['diet']['avoid'] = ['brokkoli']
        blocked = self.plan([b], available_ingredients=[{'item': 'brokkoli', 'use_first': True}])
        self.assertNotEqual(blocked['status'], 'planned')
        self.assertFalse(self.provider.calls)

    def test_product_prepare_rounds_once_and_does_not_change_the_cart(self):
        from test_meal_concierge_products import observation, product, option
        class Provider(NoProviderCalls):
            def call(self, tool, arguments, **kwargs):
                self.calls.append(tool)
                if tool != 'product_search':
                    raise AssertionError('prepare must not mutate or read the cart')
                return observation(arguments['queries'][0], [product('10', 'Ris', 500, 'g', [option(2000)])])
        shop = Provider()
        self.app.provider_client = shop
        refs = [self.save('Rismiddag A', 'rice-a'), self.save('Rismiddag B', 'rice-b')]
        menu = self.menu(self.plan(refs, days=2, available_ingredients=[{'item':'ris', 'quantity':500, 'unit':'g'}]))
        ref = {k: menu[k] for k in ('menu_id', 'revision', 'digest')}
        request = {'operation':'products', 'action':'prepare', 'menu_ref':ref}
        unapproved = self.app.handle(request)['product_plan']
        self.assertEqual(unapproved['status'], 'needs_input')
        needs = menu_requirements(menu)[0]
        request['candidate_approvals'] = [{'requirement_id':needs[0]['requirement_id'], 'candidate_refs':['10']}]
        first = self.app.handle(request)['product_plan']
        self.assertEqual(first['status'], 'prepared')
        self.assertEqual(first['requirements'][0]['selection']['required']['numerator'], 300)
        self.assertEqual(first['requirements'][0]['selection']['package_count'], 1)
        self.assertEqual(first, self.app.handle(request)['product_plan'])
        self.assertEqual(shop.calls, ['product_search'] * 3)

    def test_fully_known_stock_prepares_zero_purchase_but_unknown_stock_cannot(self):
        ref = self.save('Rismiddag', 'rice')
        menu = self.menu(self.plan([ref], available_ingredients=[{'item':'ris', 'quantity':1, 'unit':'kg'}]))
        plan = self.app.handle({'operation':'products', 'action':'prepare',
                               'menu_ref':{k:menu[k] for k in ('menu_id','revision','digest')}})['product_plan']
        self.assertEqual(plan['status'], 'prepared')
        self.assertEqual(plan['requirements'], [])
        self.assertFalse(self.provider.calls)

    def test_replan_reallocates_the_current_assertion_and_does_not_add_old_stock(self):
        import menu_planning as mp
        refs = [self.save('Rismiddag '+str(i), 'rice-'+str(i)) for i in range(4)]
        menu = self.menu(self.plan(refs[:2], days=2, available_ingredients=[{'item':'ris', 'quantity':500, 'unit':'g'}]))
        request = {'operation':'menu', 'action':'replan_prepare', 'menu_ref':mp.menu_ref(menu),
                   'remaining_dates':['2026-09-07', '2026-09-08'],
                   'planner_input':{'candidates':refs[2:], 'available_ingredients':[{'item':'ris','quantity':250,'unit':'g'}]}}
        prepared = self.app.handle(request)['replan']
        self.assertEqual(prepared, self.app.handle(request)['replan'])
        next_menu = self.app.handle({'operation':'menu', 'action':'replan_apply', 'replan':prepared})['menu']
        self.assertEqual(menu_requirements(next_menu)[0][0]['quantity']['numerator'], 550)
        self.assertTrue(self.app.handle({'operation':'menu', 'action':'replan_apply', 'replan':prepared})['idempotent'])
        self.assertEqual(menu_requirements(next_menu)[0][0]['quantity']['numerator'], 550)
        self.assertFalse(self.provider.calls)

    def test_absent_stock_queries_cannot_hide_ordinary_bank_fallback(self):
        ref = self.save('Potetmiddag', 'potato', 'potet')
        plan = self.plan(available_ingredients=[{'item': item} for item in ('brokkoli', 'kylling', 'ris')])
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(plan['selection']['slots'][0]['reference'], ref)
        internal = next(row for row in plan['discovery']['sources'] if row['source'] == 'internal')
        self.assertIn('', internal['queries'])
        self.assertLessEqual(internal['pages'], 6)

    def test_long_stock_identity_has_bounded_queries_without_disabling_source(self):
        ref = self.save('Potetmiddag', 'potato', 'potet')
        long_name = 'x' * 250
        plan = self.plan(available_ingredients=[{'item': long_name}])
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(plan['selection']['slots'][0]['reference'], ref)
        internal = next(row for row in plan['discovery']['sources'] if row['source'] == 'internal')
        self.assertTrue(all(len(query) <= 200 for query in internal['queries']))
        menu = self.menu(plan)
        self.assertEqual(menu['available_ingredients'][0]['item'], long_name)

    def test_long_first_query_does_not_starve_ordinary_query(self):
        from recipe_selection import collect_candidates, compact_candidate
        from test_meal_concierge_recipe_selection import candidate, request
        from core import DEFAULT_PROFILE
        rows = [candidate(i) for i in range(1, 41)]
        values = {row['reference_key']: row for row in rows}
        calls = []
        def fetch(source, query, cursor, limit, deadline):
            calls.append((query, cursor))
            page = rows[:20] if query == 'stock' else rows[20:]
            return {'candidates':[compact_candidate(row['recipe'], row['reference']) for row in page],
                    'next_cursor': (cursor or 0)+1, 'exhausted':False}
        scope = request(1)
        scope['available_ingredients'] = normalize_available_ingredients([{'item':'stock'}])
        result = collect_candidates(source_queries={'internal':['stock','']}, fetch_page=fetch,
            resolve=lambda summary, deadline: values[canonical({k: summary[k] for k in ('recipe_ref','discovery_ref') if k in summary})],
            request=scope, profile=DEFAULT_PROFILE)
        self.assertEqual([query for query, _ in calls[:2]], ['stock',''])
        self.assertLessEqual(len(calls), 6)


if __name__ == '__main__':
    unittest.main()
