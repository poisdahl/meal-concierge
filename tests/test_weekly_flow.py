"""Ordinary Application regressions from the interrupted weekly shop; synthetic only."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_meal_concierge_recurring_dietary as fixture
from core import HouseholdError
from planner import equipment_conflicts
from product_planner import ingredient_search, nonfood_candidate
from recipes import _ingredient_v1
from service import Application
import menu_planning as mp


class WeeklyFlowTests(unittest.TestCase):
    setUp = fixture.RecurringDietaryTests.setUp
    profile = fixture.RecurringDietaryTests.profile
    call = fixture.RecurringDietaryTests.call
    batch = fixture.RecurringDietaryTests.batch
    shop = fixture.RecurringDietaryTests.shop

    def recurring(self, product='10'):
        return self.app.handle({'operation': 'recurring', 'action': 'add', 'item': {
            'product_id': product, 'product_name': 'Fast vare', 'quantity': 1,
            'schedule': {'every': 1, 'unit': 'weeks', 'anchor': '2026-W37'}}})

    def test_digest_replaces_105_ids_and_replays_once(self):
        self.profile(diet={'rules': [{'kind': 'allergy', 'term': 'synthetic' + str(i)} for i in range(5)]})
        self.provider.cart['items'] = [{'product_id': i, 'name': 'Vare '+str(i), 'quantity': 1, 'price': 35.0} for i in range(10, 31)]
        self.provider.cart.update(count=21, subtotal=735.0)
        self.app.confirmation_policy = 'standing'
        prepared = self.call('submit', idempotency_key='digest-order')
        assessment = prepared['summary']['dietary_assessment']
        self.assertEqual(len(assessment['findings']), 105)
        with self.assertRaisesRegex(HouseholdError, 'dietary review changed'):
            self.call('submit', idempotency_key='digest-order', dietary_review_digest='wrong')
        result = self.call('submit', idempotency_key='digest-order', dietary_review_digest=assessment['assessment_digest'])
        self.assertTrue(result['confirmed'])
        self.assertTrue(self.call('submit', idempotency_key='digest-order', dietary_review_digest=assessment['assessment_digest'])['confirmed'])
        self.assertEqual(self.browser.checkout_clicks, 1)

    def test_unknown_preferences_do_not_block_but_known_allergy_does(self):
        self.profile(diet={'avoid': ['smoked food', 'red meat'], 'rules': [{'kind': 'preference', 'term': 'sugar'}]})
        prepared = self.call('prepare')
        self.assertEqual(prepared['summary']['dietary_assessment']['findings'], [])
        self.profile(diet={'rules': [{'kind': 'allergy', 'term': 'milk'}]})
        self.provider.detail_html = '<div>Ingredienser</div><div>milk</div><div>Allergener</div><div>milk</div><div>Produksjonsland</div><div>Norway</div>'
        prepared = self.call('prepare')
        result = self.call('confirm', confirmation_id=prepared['confirmation_id'], dietary_review_digest=prepared['summary']['dietary_assessment']['assessment_digest'])
        self.assertTrue(result['dietary_review_required'])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_unknown_culinary_exclusion_is_advisory_but_known_dry_lentils_block(self):
        from dietary_assessment import assess
        self.profile(diet={'rules': [{'kind': 'never_buy', 'term': 'dry whole legumes'}]})
        self.assertEqual(self.call('prepare')['summary']['dietary_assessment']['findings'], [])
        profile = self.store.read()['profile']
        findings = assess(profile, {'name': 'Tørkede linser', 'dietary_evidence': {'ingredients': 'Grønne linser'}})
        self.assertTrue(any(f['blocked'] for f in findings))
        findings = assess(profile, {'name': 'Hermetiske linser', 'dietary_evidence': {'ingredients': 'Linser, vann'}})
        self.assertFalse(any(f['blocked'] for f in findings))

    def test_recurring_shared_sku_is_additive_idempotent_and_fulfilled(self):
        self.recurring()
        menu, products = self.shop(self.batch())
        plan = self.store.read()['cart_plan']
        self.assertEqual(plan['required_quantities']['10'], plan['menu_required_quantities']['10'] + 1)
        first_cart = deepcopy(self.provider.cart)
        sync = self.app.handle({'operation': 'cart', 'action': 'weekly', 'menu_ref': mp.menu_ref(menu)})
        self.assertTrue(sync['synced']); self.assertEqual(first_cart, self.provider.cart)
        self.recurring('20')
        sync = self.app.handle({'operation': 'cart', 'action': 'weekly', 'menu_ref': mp.menu_ref(menu)})
        self.assertTrue(sync['synced'])
        self.assertEqual(self.store.read()['cart_plan']['product_plan_digest'], products['product_plan_digest'])
        self.app.confirmation_policy = 'standing'
        result = self.call('submit', weekly=True, idempotency_key='weekly-order')
        self.assertTrue(result['confirmed'], result)
        self.assertEqual(self.app.handle({'operation': 'recurring', 'action': 'due', 'date': '2026-09-07'})['due'], [])
        self.assertTrue(self.call('submit', weekly=True, idempotency_key='weekly-order')['confirmed'])
        self.assertEqual(self.browser.checkout_clicks, 1)
        with self.store.locked() as state:
            self.app._mark_order_cancelled(state, result['order_id'], provider='oda', active_provider='oda')
        self.assertEqual(len(self.app.handle({'operation': 'recurring', 'action': 'due', 'date': '2026-09-07'})['due']), 2)

    def test_ten_staples_with_all_menu_stock_and_addon_survive(self):
        self.batch()
        for product in range(20, 30):
            self.recurring(str(product))
        plan = self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {
            'week': '2026-W37', 'candidates': self.candidates,
            'available_ingredients': [{'item': 'gulrot', 'quantity': 1400, 'unit': 'g'}]}})['plan']
        menu, products = self.shop(plan)
        self.assertEqual(products['requirements'], [])
        self.assertEqual(len(self.store.read()['cart_plan']['required_quantities']), 10)
        self.assertEqual(sum(i['quantity'] for i in self.provider.cart['items']), 10)
        extra = self.app.handle({'operation': 'cart', 'action': 'ensure', 'requirements': [
            {'product_id': '50', 'product_name': 'Spirer', 'quantity': 1}]})
        self.assertEqual(len(self.provider.cart['items']), 11)
        self.assertEqual(self.store.read()['cart_plan']['product_plan_digest'], products['product_plan_digest'])
        self.assertTrue(self.app.handle({'operation': 'cart', 'action': 'weekly', 'menu_ref': mp.menu_ref(menu)})['synced'])
        self.assertEqual(sum(i['quantity'] for i in self.provider.cart['items']), 11)

    def test_missing_recurring_product_is_not_marked_bought(self):
        self.recurring('20')
        self.shop(self.batch())
        prepared = self.call('prepare')
        pending = self.store.read()['pending_checkout']
        pending['summary']['items'] = [row for row in pending['summary']['items'] if str(row['product_id']) != '20']
        with self.store.locked() as state:
            self.app._record_order_snapshot(state, pending, '1234567')
        self.assertEqual(len(self.app.handle({'operation': 'recurring', 'action': 'due', 'date': '2026-09-07'})['due']), 1)

    def test_reconcile_existing_manual_menu_goods_does_not_readd_them_as_extras(self):
        plan = self.batch()
        menu = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_handoff': plan['save_handoff']})['menu']
        self.provider.cart.update(items=[], count=0, subtotal=0)
        self.app.handle({'operation': 'cart', 'action': 'ensure', 'requirements': [
            {'product_id': '10', 'product_name': 'Gulrot', 'quantity': 2},
            {'product_id': '50', 'product_name': 'Spirer', 'quantity': 1}]})
        preview = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu)})['product_plan']
        approved = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu),
            'candidate_approvals': [{'requirement_id': r['requirement_id'], 'candidate_refs': ['10']} for r in preview['requirements']]})
        pending = self.app.handle({'operation': 'products', **approved['apply_arguments'], 'cart_change_requested': True})
        self.assertTrue(pending['cart_reconciliation_required'])
        reconciled = self.app.handle({'operation': 'cart', 'action': 'reconcile', 'menu_ref': mp.menu_ref(menu),
            'cart_digest': pending['cart_plan']['cart_digest'], 'decision': 'restore_missing', 'exclude_product_ids': ['10']})
        self.assertTrue(reconciled['reconciled'])
        self.assertFalse(any(i['missing_quantity'] for i in reconciled['cart_plan']['items']))
        self.assertTrue(self.app.handle({'operation': 'products', **approved['apply_arguments'], 'cart_change_requested': True})['applied'])
        self.recurring('20')  # Changed requirements must not revive withdrawn extras.
        self.assertTrue(self.app.handle({'operation': 'cart', 'action': 'weekly', 'menu_ref': mp.menu_ref(menu)})['synced'])
        self.assertEqual({str(i['product_id']): i['quantity'] for i in self.provider.cart['items']}, {'10': 2, '50': 1, '20': 1})

    def test_recorded_home_stock_survives_restart_and_removes_menu_goods_only(self):
        menu, products = self.shop(self.batch())
        self.app.handle({'operation': 'cart', 'action': 'ensure', 'requirements': [
            {'product_id': '50', 'product_name': 'Oregano extra', 'quantity': 1}]})
        decisions = [{'source': source, 'action': 'have_all'}
                     for row in products['requirements'] for source in row['sources']]
        # Sources include amount detail in the plan; the decision uses its exact position.
        decisions = [{'source': {k: d['source'][k] for k in ('collection', 'recipe_index', 'ingredient_index')},
                      'action': 'have_all'} for d in decisions]
        before = deepcopy(self.provider.cart)
        recorded = self.app.handle({'operation': 'products', 'action': 'record_ingredients',
            'menu_ref': mp.menu_ref(menu), 'ingredient_decisions': decisions})
        self.assertTrue(recorded['recorded'])
        self.assertEqual(before, self.provider.cart)
        self.assertEqual(self.call('prepare', weekly=True)['reason'], 'weekly_menu_products_incomplete')
        self.app = Application(self.store, self.provider, self.browser)
        prepared = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu)})
        self.assertEqual(prepared['product_plan']['requirements'], [])
        compared = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu),
            'previous_product_plan': prepared['product_plan']})
        self.assertEqual(compared['observation_drift']['status'], 'unchanged')
        handoff_prepared = self.app.handle({'operation': 'products', 'action': 'prepare',
            'planner_handoff': menu['planner_selection']})
        self.assertEqual(handoff_prepared['product_plan']['requirements'], [])
        applied = self.app.handle({'operation': 'products', **prepared['apply_arguments'], 'cart_change_requested': True})
        self.assertTrue(applied['applied'], applied)
        self.assertEqual({str(i['product_id']): i['quantity'] for i in self.provider.cart['items']}, {'50': 1})
        self.assertTrue(self.app.handle({'operation': 'products', **prepared['apply_arguments'], 'cart_change_requested': True})['applied'])
        self.assertEqual({str(i['product_id']): i['quantity'] for i in self.provider.cart['items']}, {'50': 1})
        # Reversing the assertion buys the actual menu quantity again.
        included = [{**d, 'action': 'include'} for d in decisions]
        self.app.handle({'operation': 'products', 'action': 'record_ingredients', 'menu_ref': mp.menu_ref(menu), 'ingredient_decisions': included})
        self.assertTrue(self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu)})['product_plan']['requirements'])
        stale = self.app.handle({'operation': 'products', **prepared['apply_arguments'], 'cart_change_requested': True})
        self.assertFalse(stale['applied'])
        self.assertTrue(stale['fresh_product_plan']['requirements'])
        self.assertTrue(all(d['action'] == 'include' for d in stale['fresh_product_plan']['ingredient_decisions']))
        with self.assertRaises(HouseholdError):
            self.app.handle({'operation': 'products', 'action': 'record_ingredients',
                'menu_ref': {**mp.menu_ref(menu), 'revision': 999}, 'ingredient_decisions': included})

    def test_replacing_menu_products_removes_old_sku_and_preserves_shared_extra(self):
        menu, _products = self.shop(self.batch())
        old_quantity = self.store.read()['cart_plan']['required_quantities']['10']
        self.app.handle({'operation': 'cart', 'action': 'change', 'operations': [{'product_id': '10', 'quantity': 1}]})
        side = self.app.handle({'operation': 'recipes', 'action': 'save',
            'recipe': fixture.recipes_fixture.recipe('Extra side', 'new-side'), 'idempotency_key': 'new-side'})['recipe']
        with mock.patch.object(Application, '_household_today', return_value=date(2026, 9, 3)):
            updated = self.app.handle({'operation': 'menu', 'action': 'add_slot', 'menu_ref': mp.menu_ref(menu),
            'slot_input': {'date': '2026-09-07', 'meal_type': 'side', 'portions': 2, 'reference': {'recipe_ref': {'id': side['id'], 'revision': side['revision']}}},
            'idempotency_key': 'menu-revision-with-side'})['menu']
        self.assertNotEqual(mp.menu_ref(updated), mp.menu_ref(menu))
        menu = updated
        self.provider.product_id = 20
        prepared = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu)})
        approved = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu),
            'candidate_approvals': [{'requirement_id': r['requirement_id'], 'candidate_refs': ['20']}
                                    for r in prepared['product_plan']['requirements']]})
        result = self.app.handle({'operation': 'products', **approved['apply_arguments'], 'cart_change_requested': True})
        self.assertTrue(result['applied'], result)
        self.assertEqual({str(i['product_id']): i['quantity'] for i in self.provider.cart['items']}, {'10': 1, '20': old_quantity})

    def test_uncertain_menu_removal_preserves_the_remaining_extra_allocation(self):
        menu, products = self.shop(self.batch())
        self.app.handle({'operation': 'cart', 'action': 'change', 'operations': [{'product_id': '10', 'quantity': 1}]})
        decisions = [{'source': {k: source[k] for k in ('collection', 'recipe_index', 'ingredient_index')},
                      'action': 'have_all'} for row in products['requirements'] for source in row['sources']]
        prepared = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu),
            'ingredient_decisions': decisions})
        original = self.provider.call
        def lost_response(tool, arguments, **kwargs):
            result = original(tool, arguments, **kwargs)
            if tool == 'manipulate_cart':
                raise HouseholdError('synthetic lost response after deletion')
            return result
        with mock.patch.object(self.provider, 'call', side_effect=lost_response):
            result = self.app.handle({'operation': 'products', **prepared['apply_arguments'], 'cart_change_requested': True})
        self.assertFalse(result['applied'])
        plan = self.store.read()['cart_plan']
        self.assertEqual(plan['supplemental_quantities']['10'], 1)
        self.assertNotIn('10', plan['added_quantities'])
        carried = self.app._new_cart_plan({**mp.menu_ref(menu), 'revision': menu['revision']+1},
            {'10': 1}, {'10': 'Gulrot extra'}, {}, {}, set(), previous=plan)
        self.assertEqual(carried['supplemental_quantities'], {'10': 1})
        self.assertEqual(carried['added_quantities'], {})

    def test_recurring_substitution_replaces_one_occurrence_without_double_purchase(self):
        self.recurring('20')
        menu, _products = self.shop(self.batch())
        original = deepcopy(self.store.read()['recurring_items'])
        replacement = {'product_id': '21', 'product_name': 'Økologiske pærer 500g', 'quantity': 1}
        request = {'operation': 'recurring', 'action': 'substitute', 'product_id': '20',
                   'date': '2026-09-07', 'replacement': replacement}
        self.assertTrue(self.app.handle(request)['substituted'])
        self.assertTrue(self.app.handle(request)['substituted'])
        self.assertEqual(self.call('prepare', weekly=True)['reason'], 'weekly_goods_changed')
        self.assertTrue(self.app.handle({'operation': 'cart', 'action': 'weekly', 'menu_ref': mp.menu_ref(menu)})['synced'])
        quantities = {str(i['product_id']): i['quantity'] for i in self.provider.cart['items']}
        self.assertNotIn('20', quantities); self.assertEqual(quantities['21'], 1)
        self.assertEqual(self.store.read()['recurring_items'], original)
        next_due = self.app.handle({'operation': 'recurring', 'action': 'due', 'date': '2026-09-14'})['due']
        self.assertEqual(next_due[0]['product_id'], '20')
        self.app.confirmation_policy = 'standing'
        paid = self.call('submit', weekly=True, idempotency_key='substitute-order')
        self.assertTrue(paid['confirmed'], paid)
        self.assertEqual(self.app.handle({'operation': 'recurring', 'action': 'due', 'date': '2026-09-07'})['due'], [])

    def test_saved_menu_equipment_is_checked_at_confirmation(self):
        self.profile(meals={'equipment': ['pot','pan','oven','blender']})
        self.shop(self.batch())
        # Old/imported frozen menus must be checked too, including after preparation.
        with self.store.locked() as state:
            state['menu']['dishes'][0]['steps'] = ['Use a blender.']
        prepared = self.call('prepare')
        self.profile(meals={'equipment': ['pot','pan','oven']})
        self.assertIn('equipment_unavailable', [i['code'] for i in self.app.handle({'operation':'menu','action':'assess'})['assessment']['issues']])
        result = self.call('confirm', confirmation_id=prepared['confirmation_id'])
        self.assertEqual(result['reason'], 'equipment_unavailable')
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_weekly_raw_cart_cannot_claim_menu_coverage(self):
        self.assertEqual(self.call('prepare', weekly=True)['reason'], 'weekly_menu_products_incomplete')
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_batch_menu_renders_recipe_sources_and_pdf_without_changing_snapshot(self):
        from recipe_delivery import render_menu, render_pdf
        import pypdfium2
        plan = self.batch()
        menu = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_handoff': plan['save_handoff']})['menu']
        original = deepcopy(menu)
        rendered = render_menu(menu, self.app.recipes.assets, images=False)
        document = pypdfium2.PdfDocument(render_pdf(rendered))
        self.addCleanup(document.close)
        text = '\n'.join(page.get_textpage().get_text_range() for page in document)
        self.assertIn('Tilberedning:', text)
        self.assertIn('Ingredienser', text)
        self.assertIn('Fremgangsmåte', text)
        self.assertIn('600 g gulrot', text)
        self.assertIn('800 g gulrot', text)
        self.assertIn('Tilbered 6 porsjoner', text)
        self.assertIn('Tilbered 8 porsjoner', text)
        self.assertNotIn('200 g gulrot', text)
        self.assertNotIn("'basis':", text)
        self.assertNotIn('Ukeplan', text)
        self.assertIn('(rester)', text)
        for recipe in menu['dishes']:
            self.assertIn(recipe['name'], text)
        self.assertEqual(menu, original)
        self.assertEqual(self.store.read()['menu'], original)
        partial = deepcopy(menu)
        partial['dishes'][0]['ingredients'][0]['scalable'] = False
        fallback = render_menu(partial, self.app.recipes.assets, images=False)['text']
        self.assertIn('kan ikke skaleres automatisk', fallback)
        self.assertIn('200 g gulrot', fallback)

    def test_explicit_dates_and_one_plan_portions_preserve_profile(self):
        self.batch(4)
        plan = self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {
            'week': '2026-W37', 'dates': ['2026-09-'+str(i).zfill(2) for i in range(7,14)],
            'prepared_portion_range': [6,8], 'candidates': self.candidates}})['plan']
        self.assertEqual(plan['status'], 'planned', plan)
        menu = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_handoff': plan['save_handoff']})['menu']
        self.assertEqual(len(menu['batches']), 2)
        self.assertEqual(self.store.read()['profile']['meals']['prepared_portion_range'], [4,4])

    @mock.patch.object(Application, '_household_today', return_value=date(2026,9,7))
    def test_partial_batch_replan_keeps_complete_component(self, _today):
        original, _ = self.shop(self.batch())
        self.call('prepare')
        self.profile(meals={'portions':3})  # Existing batch consumption remains two.
        prepared = self.app.handle({'operation': 'menu', 'action': 'replan_prepare',
            'menu_ref': mp.menu_ref(original), 'remaining_dates': ['2026-09-10','2026-09-11','2026-09-12','2026-09-13'],
            'planner_input': {'candidates': self.candidates}})['replan']
        self.assertEqual(prepared['status'], 'prepared', prepared)
        successor = self.app.handle({'operation': 'menu', 'action': 'replan_apply', 'replan': prepared})['menu']
        self.assertIsNone(self.store.read()['pending_checkout'])
        self.assertEqual(self.browser.checkout_clicks, 0)
        self.assertEqual(len(successor['batches']), 2)
        self.assertEqual(len(successor['slots']), 7)
        self.assertTrue(all(fixture.bp.fraction(b['consumed_at_source']) == 2 for b in successor['batches']))
        self.assertTrue(all(r['portions'] == 2 for r in successor['dishes']))
        ids = {slot['slot_id'] for slot in successor['slots']}
        for batch in successor['batches']:
            self.assertIn(batch['source_slot_id'], ids)
            self.assertTrue({s['slot_id'] for s in batch['leftovers']} <= ids)
        self.assertEqual(sum(i.get('kind')=='leftover' for i in successor['slots']), 5)

    def test_reversed_week_dates_canonicalize(self):
        self.batch()
        request = {'week':'2026-W37', 'candidates':self.candidates,
                   'dates':['2026-09-'+str(i).zfill(2) for i in range(7,14)]}
        first = self.app.handle({'operation':'menu','action':'plan','planner_input':request})['plan']
        request['dates'].reverse()
        second = self.app.handle({'operation':'menu','action':'plan','planner_input':request})['plan']
        self.assertEqual(first['save_ref'], second['save_ref'])

    def test_special_equipment_and_real_alternatives(self):
        profile = self.store.read()['profile']
        for step, expected in [('Pressure cook for 35 minutes.', 'pressure cooker'), ('Use your pressure-cooker.', 'pressure cooker'),
                               ('Blend in a blender.', 'blender'), ('Use a food processor.', 'food processor'),
                               ('Kjør deigen i kjøkkenmaskin.', 'stand mixer'), ('Cook in an airfryer.', 'air fryer'),
                               ('Cook in a slow cooker.', 'slow cooker'), ('Use a waffle iron.', 'waffle iron'), ('Kok i trykkokeren.', 'pressure cooker'), ('Kjør i blenderen.', 'blender'), ('Bruk kjøkkenmaskinen.', 'stand mixer'), ('Microwave on high for 5 minutes.', 'microwave'), ('Preheat the grill.', 'grill'), ('Cook in a pressure cooker and serve over rice or oven-roasted potatoes.', 'pressure cooker')]:
            with self.subTest(step=step):
                self.assertIn(expected, equipment_conflicts(profile, {'steps':[step]}))
        for step in ['You do not need a pressure cooker.', 'Use a pressure cooker or simmer in a covered pot for 50 minutes.', 'Mix by hand.', 'Uten trykkoker.', 'Fold in the barbecue sauce.']:
            self.assertEqual(equipment_conflicts(profile, {'steps':[step]}), [], step)
        profile['meals']['equipment'].append('blender')
        self.assertEqual(equipment_conflicts(profile, {'steps':['Use a food processor or blender.']}), [])

    def test_optional_and_local_product_search_semantics(self):
        self.assertTrue(_ingredient_v1('chili (optional)',0)['optional'])
        self.assertTrue(_ingredient_v1({'item':'chili', 'raw':'1 hot pepper (optional, for heat), chopped'},0)['optional'])
        self.assertEqual(ingredient_search('black beans','oda'), 'sorte bønner')
        self.assertTrue(nonfood_candidate({'name':'Whiskas våtfôr med torsk'}))


if __name__ == '__main__':
    unittest.main()
