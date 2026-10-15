"""Shopping lists share exact requirements while preserving the existing cart path."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
from core import StateStore, HouseholdError
from service import Application
from shopping_lists import build_list
from product_planner import menu_requirements
from test_meal_concierge_products import FakeProvider, MenyFixtureProvider


class ShoppingListTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore(self.root, {'household':'List fixture', 'provider':'oda'})
        self.provider = FakeProvider()
        self.app = Application(self.store, self.provider, object())
        self.menu = {'menu_id':'menu_list_fixture', 'revision':1, 'digest':'a'*64, 'phase':'draft',
            'dishes':[{'shopping_requirements':[{'item':'Fixture Mel', 'quantity':500, 'unit':'g', 'scalable':True}]}],
            'salads':[]}
        with self.store.locked() as state:
            state['menu'] = deepcopy(self.menu)
        self.ref = {key:self.menu[key] for key in ('menu_id','revision','digest')}

    def get(self):
        return self.app.handle({'operation':'shopping_list'})

    def update(self, listing, **values):
        return self.app.handle({'operation':'shopping_list','action':'update','list_ref':listing['list_ref'],**values})

    def test_generic_list_has_no_provider_calls_and_persists_extras_checks(self):
        first = self.get()
        row = first['items'][0]
        updated = self.update(first, extra_items=[{'id':'coffee','item':'Kaffe'}], checked_items={row['id']:True})
        self.assertTrue(updated['items'][0]['checked'])
        self.assertEqual(updated['items'][1]['item'], 'Kaffe')
        restarted = Application(StateStore(self.root, {'household':'List fixture', 'provider':'oda'}), self.provider, object())
        self.assertEqual(restarted.handle({'operation':'shopping_list'})['items'], updated['items'])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.provider.cart['items'], [])

    def test_replan_preserves_extras_and_reopens_changed_quantity(self):
        first = self.get()
        checked = self.update(first, extra_items=[{'id':'coffee','item':'Kaffe'}], checked_items={first['items'][0]['id']:True})
        with self.store.locked() as state:
            state['menu']['menu_id'] = 'menu_successor'
            state['menu']['digest'] = 'b'*64
            state['menu']['dishes'][0]['shopping_requirements'][0]['quantity'] = 800
        changed = self.get()
        self.assertFalse(changed['items'][0]['checked'])
        self.assertTrue(changed['items'][0]['check_needs_review'])
        self.assertEqual(changed['items'][1]['item'], 'Kaffe')
        with self.assertRaisesRegex(HouseholdError,'changed'):
            self.update(checked, checked_items={changed['items'][0]['id']:True})
        refreshed = self.app.handle({'operation':'shopping_list','action':'refresh','list_ref':changed['list_ref']})
        self.assertEqual(refreshed['items'][1]['item'],'Kaffe')

    def test_new_week_reopens_checks_but_explicit_replans_preserve_them(self):
        first = self.get()
        checked = self.update(first, checked_items={first['items'][0]['id']: True})
        with self.store.locked() as state:
            state['menu']['supersedes'] = deepcopy(self.ref)
            state['menu']['menu_id'] = 'replan'
        self.assertTrue(self.get()['items'][0]['checked'])
        with self.store.locked() as state:
            state['menu'].pop('supersedes')
            state['menu']['menu_id'] = 'next_week'
        self.assertFalse(self.get()['items'][0]['checked'])

    def test_extra_edit_roundtrip_preserves_identity_and_check(self):
        listing = self.update(self.get(), extra_items=[{'id':'coffee', 'item':'Coffee'}])
        extra = next(row for row in listing['items'] if row['kind'] == 'extra')
        listing = self.update(listing, checked_items={extra['id']: True})
        result = self.update(listing, extra_items=[
            {'id':extra['extra_id'], 'item':extra['item']}, {'id':'paper', 'item':'Paper'}])
        same = next(row for row in result['items'] if row['id'] == extra['id'])
        self.assertTrue(same['checked'])

    def test_recorded_stock_changes_list_reference_and_matches_product_need(self):
        first = self.get()
        self.app.handle({'operation':'products','action':'record_ingredients','menu_ref':self.ref,
                         'ingredient_decisions':[{'source':{'collection':'dishes','recipe_index':0,'ingredient_index':0},
                                                  'action':'have_quantity','quantity':200,'unit':'g'}]})
        current = self.get()
        self.assertNotEqual(current['list_ref'], first['list_ref'])
        self.assertEqual(current['items'][0]['quantity']['numerator'],300)
        with self.assertRaises(HouseholdError):
            self.update(first, extra_items=[])
        plan = self.app.handle({'operation':'products','action':'prepare','menu_ref':self.ref})['product_plan']
        self.assertEqual(plan['requirements'][0]['quantity'],current['items'][0]['quantity'])

    def test_unresolved_ingredients_and_extra_only_list_remain_usable(self):
        with self.store.locked() as state:
            state['menu']['dishes'][0]['shopping_requirements'].append({'item':'Pepper','unit':'etter smak','scalable':False})
        listing = self.get()
        self.assertTrue(any(row['kind']=='unresolved' and row['item']=='Pepper' for row in listing['items']))
        self.update(listing,extra_items=[{'id':'paper','item':'Papir'}])
        with self.store.locked() as state:
            state['menu']=None
        self.assertEqual(self.get()['items'][0]['item'],'Papir')

    def test_explicit_matching_then_existing_apply_remains_usable(self):
        listing = self.get()
        listing = self.update(listing,extra_items=[{'id':'coffee','item':'Kaffe'}])
        result = self.app.handle({'operation':'shopping_list','action':'match','list_ref':listing['list_ref'],
            'candidate_approvals':[{'requirement_id':listing['items'][0]['id'],'candidate_refs':['10']}]})
        self.assertEqual(result['matching_status'],'prepared', result)
        self.assertEqual(result['unmatched_extra_items'],1)
        self.assertEqual(self.provider.cart['items'], [])
        product = result['product_result']
        reference = product['product_plan_ref']
        # Ordinary list reads do not rotate an existing product review.
        self.get(); self.get()
        review = self.app.handle({'operation':'products','action':'get','product_plan_ref':reference})
        self.assertEqual(review['product_plan_ref'], reference)
        record = self.app._product_plan_record(self.store.read(), reference)
        applied = self.app.handle({'operation':'products', **record['apply_arguments'], 'cart_change_requested':True})
        self.assertTrue(applied['applied'], applied)
        self.assertEqual(self.provider.cart['items'][0]['quantity'],1)

    def test_match_to_existing_cart_path_for_mathem_and_meny(self):
        for name in ('mathem', 'meny'):
            with self.subTest(provider=name):
                store = StateStore(self.root / name, {'household':'Synthetic', 'provider':name})
                provider = MenyFixtureProvider() if name == 'meny' else FakeProvider()
                if name == 'mathem':
                    original = provider.call
                    def call(tool, args, **kwargs):
                        result = original(tool, args, **kwargs)
                        if tool == 'product_search':
                            result['provider'] = 'mathem'
                            for product in result['products']:
                                product['provider'] = 'mathem'
                        return result
                    provider.call = call
                app = Application(store, provider, object())
                menu = deepcopy(self.menu)
                identity = '10'
                if name == 'meny':
                    identity = provider.milk['product_ref']
                    menu['dishes'][0]['shopping_requirements'] = [
                        {'item':'Lettmelk', 'quantity':1, 'unit':'l', 'scalable':True}]
                with store.locked() as state:
                    state['menu'] = menu
                listing = app.handle({'operation':'shopping_list'})
                result = app.handle({'operation':'shopping_list', 'action':'match', 'list_ref':listing['list_ref'],
                    'candidate_approvals':[{'requirement_id':listing['items'][0]['id'], 'candidate_refs':[identity]}]})
                self.assertEqual(result['matching_status'], 'prepared', result)
                self.assertEqual(provider.cart['items'], [])
                record = app._product_plan_record(store.read(), result['product_result']['product_plan_ref'])
                applied = app.handle({'operation':'products', **record['apply_arguments'], 'cart_change_requested':True})
                self.assertTrue(applied['applied'], applied)
                self.assertEqual(provider.cart['items'][0]['quantity'], 1)

    def test_matching_continuation_and_catalog_failure(self):
        listing=self.get()
        result=self.app.handle({'operation':'shopping_list','action':'match','list_ref':listing['list_ref']})
        reference=result['product_result']['product_plan_ref']
        next_result=self.app.handle({'operation':'shopping_list','action':'match','list_ref':listing['list_ref'],
            'product_plan_ref':reference,'candidate_approvals':[{'requirement_id':listing['items'][0]['id'],'candidate_refs':['10']}]})
        self.assertEqual(next_result['matching_status'],'prepared', next_result)
        self.provider.call=lambda *a,**kw: (_ for _ in ()).throw(HouseholdError('fixture offline'))
        failed=self.app.handle({'operation':'shopping_list','action':'match','list_ref':listing['list_ref']})
        self.assertEqual(self.get()['items'][0]['item'],'Fixture Mel')
        self.assertEqual(failed['matching_status'],'needs_input')
        self.assertEqual(failed['product_result']['requirements'][0]['issues'][0]['reason'], 'provider_search_unavailable_or_scope_changed')

    def test_meny_match_uses_existing_browser_guard(self):
        self.app.provider='meny'
        calls=[]
        @contextmanager
        def guard(*args,**kwargs):
            calls.append('guard')
            raise HouseholdError('fixture browser busy')
            yield
        self.app._browser_operation=guard
        listing=self.get()
        result=self.app.handle({'operation':'shopping_list','action':'match','list_ref':listing['list_ref']})
        self.assertEqual(calls,['guard'])
        self.assertEqual(result['matching_status'],'unavailable')
        self.assertEqual(self.provider.calls,[])

    def test_no_store_match_is_optional(self):
        self.app.provider='none'
        listing=self.get()
        result=self.app.handle({'operation':'shopping_list','action':'match','list_ref':listing['list_ref']})
        self.assertEqual(result['matching_status'],'not_configured')
        self.assertEqual(result['items'],listing['items'])
        self.assertEqual(self.provider.calls,[])


if __name__ == '__main__':
    unittest.main()
