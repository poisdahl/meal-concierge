"""Synthetic ordinary Application tool flow; no merchant or recipient effects."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import StateStore, HouseholdError
from service import Application
import menu_planning as mp
import batch_planning as bp
import test_meal_concierge_planner as recipes_fixture
import test_meal_concierge_products as products_fixture
from test_meal_concierge import MutableFakeOda, FakeBrowser, CONFIG, ODA_FIXTURE_NOW


class Retailer(MutableFakeOda):
    def __init__(self):
        super().__init__()
        self.evidence = {}
        self.product_id = 10
        self.detail_html = None

    def product_dietary_evidence(self, reference, *, deadline=None):
        if self.detail_html is None:
            return {}
        from dietary_assessment import parse_oda_product_page
        return parse_oda_product_page(self.detail_html, f'https://oda.com/no/products/{reference}-synthetic/')

    def call(self, tool, arguments, **kwargs):
        if tool == 'product_search':
            product = products_fixture.product(str(self.product_id), 'Gulrot', 1000, 'g', [products_fixture.option(3500)])
            product['dietary_evidence'] = deepcopy(self.evidence)
            return products_fixture.observation(arguments['queries'][0], [product])
        return super().call(tool, arguments, **kwargs)


class Browser(FakeBrowser):
    def submit_checkout(self, cart, review, before_click=None, **kwargs):
        super().submit_checkout(cart, review, before_click, **kwargs)
        order = self.oda.orders[-1]
        order['grossAmount'] = cart['subtotal']
        order['products'] = [{'product': {'id': i['product_id'], 'name': i['name']}, 'quantity': i['quantity'], 'totalGrossAmount': str(i['price'] * i['quantity'])} for i in cart['items']]


class RecurringDietaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mc55-')
        self.addCleanup(self.temp.cleanup)
        config = deepcopy(CONFIG)
        config['profile_overrides'] = {'diet': {
            'minimum_fish_portions': 0,
            'minimum_legume_dinners': 0,
            'minimum_vegetable_types': 0,
            'minimum_wholegrain_or_potato_dinners': 0,
        }}
        self.store = StateStore(Path(self.temp.name), config)
        self.provider = Retailer()
        self.browser = Browser(); self.browser.oda = self.provider
        self.app = Application(self.store, self.provider, self.browser)
        patch = mock.patch.object(Application, '_now', return_value=ODA_FIXTURE_NOW)
        patch.start(); self.addCleanup(patch.stop)
        self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})

    def profile(self, **changes):
        return self.app.handle({'operation': 'profile', 'action': 'update', 'changes': changes})

    def accepted_meals(self, **changes):
        meals = deepcopy(self.store.read()['profile']['meals'])
        meals.update(changes, recurring_batch_accepted=True)
        return {key: meals[key] for key in (
            'meal_mode', 'dinner_days', 'dishes', 'batch_dishes', 'portions',
            'prepared_portion_range', 'cook_days', 'eat_days', 'recurring_batch_accepted',
        )}

    def call(self, action, **values):
        return self.app.handle({'operation': 'checkout', 'action': action, **values})

    def deliver(self, notice):
        self.assertTrue(notice['dispatch'])
        import json, hashlib
        payload = notice['payload']['message'].encode()
        inbox = Path(self.temp.name) / (notice['phase'] + '.json')
        inbox.write_bytes(payload)
        self.assertEqual(inbox.read_bytes(), payload)
        receipt = 'synthetic-local-inbox:' + hashlib.sha256(inbox.read_bytes()).hexdigest()
        return self.call('notice_result', notice_token=notice['notice_token'], send_outcome='sent', sender_receipt=receipt)

    def batch(self, high=8):
        meals = deepcopy(self.store.read()['profile']['meals'])
        meals.update(meal_mode='batch', dishes=2, batch_dishes=2,
            cook_days=['Monday', 'Thursday'], prepared_portion_range=[4, high],
            recurring_batch_accepted=True)
        self.profile(meals={key: meals[key] for key in (
            'meal_mode', 'dinner_days', 'dishes', 'batch_dishes', 'portions',
            'prepared_portion_range', 'cook_days', 'eat_days', 'recurring_batch_accepted',
        )})
        candidates = []
        for index in range(4):
            saved = self.app.handle({'operation': 'recipes', 'action': 'save',
                'recipe': recipes_fixture.recipe(f'Gulrotgryte {index}', f'batch-{index}'), 'idempotency_key': f'recipe-{index}'})['recipe']
            candidates.append({'recipe_ref': {'id': saved['id'], 'revision': saved['revision']}})
        self.candidates = candidates
        self.profile(recipes={'sources': {'oda': False, 'meny': False, 'mathem': False, 'themealdb': False, 'wikibooks': False}})
        return self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {'week': '2026-W37'}})['plan']

    def test_recurring_writes_are_atomic_visible_and_repair_legacy_state(self):
        self.profile(meals=self.accepted_meals(meal_mode='batch', dishes=2, batch_dishes=2,
            cook_days=['Monday', 'Thursday']))
        shown = self.app.handle({'operation': 'setup', 'action': 'show'})['current']['weekly_menu']
        self.assertEqual(shown['meal_mode'], 'batch')
        self.assertTrue(shown['recurring_batch_accepted'])

        before = self.store.read()
        with self.assertRaisesRegex(HouseholdError, 'one changes.meals object containing meal_mode, dinner_days'):
            self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': False,
                'changes': {'people': 3, 'weekly_menu': {'dinner_days': 7, 'dishes': 7, 'batch_dishes': 0}}})
        self.assertEqual(self.store.read(), before)

        invalid = {'meal_mode': 'batch', 'dinner_days': 7, 'dishes': 7, 'batch_dishes': 0,
            'portions': 2, 'prepared_portion_range': [4, 8],
            'cook_days': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
            'eat_days': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
            'recurring_batch_accepted': True}
        with self.assertRaisesRegex(HouseholdError, 'batch count must fit cooking days'):
            self.profile(meals=invalid)
        self.assertEqual(self.store.read(), before)

        repaired = self.profile(meals={'meal_mode': 'fresh', 'dinner_days': 7, 'dishes': 7,
            'batch_dishes': 0, 'cook_days': invalid['cook_days'], 'eat_days': invalid['eat_days']})['profile']
        self.assertFalse(repaired['meals']['recurring_batch_accepted'])
        self.assertIsNone(bp.recurring_layout(repaired, ['2026-09-07', '2026-09-08', '2026-09-09',
            '2026-09-10', '2026-09-11', '2026-09-12', '2026-09-13']))

    def test_recurring_changes_clear_acceptance_but_unrelated_legacy_writes_remain_possible(self):
        self.profile(meals=self.accepted_meals(meal_mode='batch', dishes=2, batch_dishes=2,
            cook_days=['Monday', 'Thursday']))
        before = self.store.read()
        with self.assertRaisesRegex(HouseholdError, 'complete recurring definition'):
            self.profile(meals={'prepared_portion_range': [3, 4], 'recurring_batch_accepted': True})
        self.assertEqual(self.store.read(), before)
        changed = self.profile(meals={'prepared_portion_range': [3, 4]})['profile']
        self.assertFalse(changed['meals']['recurring_batch_accepted'])

        with self.store.locked() as state:
            state['profile']['meals'].update(meal_mode='batch', batch_dishes=0,
                recurring_batch_accepted=True)
        reopened = Application(StateStore(Path(self.temp.name), CONFIG), self.provider, self.browser)
        updated = reopened.handle({'operation': 'profile', 'action': 'update',
            'changes': {'cuisine': {'base_style': 'Legacy still editable'}}})['profile']
        self.assertEqual(updated['cuisine']['base_style'], 'Legacy still editable')
        self.assertTrue(updated['meals']['recurring_batch_accepted'])

    def test_incoherent_profile_overrides_remain_loadable_and_repairable(self):
        with tempfile.TemporaryDirectory(prefix='mc129-overrides-') as directory:
            config = deepcopy(CONFIG)
            config['profile_overrides'] = {'meals': {
                'meal_mode': 'batch', 'batch_dishes': 0, 'recurring_batch_accepted': True,
            }}
            store = StateStore(Path(directory), config)
            self.assertEqual(store.read()['profile']['meals']['meal_mode'], 'batch')
            unrelated = store.update_profile({'cuisine': {'base_style': 'Legacy override'}})
            self.assertEqual(unrelated['cuisine']['base_style'], 'Legacy override')
            before = store.read()
            with self.assertRaisesRegex(HouseholdError, 'complete recurring definition'):
                store.update_profile({'meals': {'recurring_batch_accepted': True}})
            self.assertEqual(store.read(), before)
            repaired = store.update_profile({'meals': {
                'meal_mode': 'fresh', 'dinner_days': 7, 'dishes': 7, 'batch_dishes': 0,
            }})
            self.assertFalse(repaired['meals']['recurring_batch_accepted'])

    def shop(self, plan):
        menu = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_handoff': plan['save_handoff']})['menu']
        self.provider.cart.update(items=[], count=0, subtotal=0)
        preview = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu)})['product_plan']
        approvals = [{'requirement_id': r['requirement_id'], 'candidate_refs': ['10']} for r in preview['requirements']]
        product_plan = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu), 'candidate_approvals': approvals})['product_plan']
        self.assertEqual(product_plan['status'], 'prepared')
        self.app.handle({'operation': 'products', 'action': 'apply', 'product_plan': product_plan,
            'product_plan_digest': product_plan['product_plan_digest'], 'cart_change_requested': True})
        return menu, product_plan

    def test_complete_recurring_plan_shopping_and_verified_automatic_checkout(self):
        self.profile(diet={'rules': [{'kind': 'sensitivity', 'term': 'sugar'}], 'uncertainty_permissions': [
            {'kind': 'sensitivity', 'term': 'sugar', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
        plan = self.batch()
        self.assertEqual(plan['status'], 'planned')
        menu, products = self.shop(plan)
        self.assertEqual(len(menu['slots']), 7); self.assertEqual(len(menu['batches']), 2)
        self.assertEqual([bp.fraction(b['prepared_portions']) for b in menu['batches']], [6, 8])
        self.assertEqual(products['requirements'][0]['quantity'], {'numerator': 1400, 'denominator': 1})
        self.assertTrue(self.app.handle({'operation': 'menu', 'action': 'assess'})['assessment']['ready'])
        self.assertTrue(all(s['status'] == 'planned_not_confirmed' for s in bp.dependency_status(self.store.read(), menu)))
        self.assertEqual(self.store.read()['batch_outcomes'], {'sources': {}, 'leftovers': {}})
        self.app.confirmation_policy = 'standing'
        first = self.call('submit', idempotency_key='weekly-37')
        self.assertTrue(first['notification_required']); self.assertTrue(first['notice']['dispatch'])
        self.assertEqual(self.browser.checkout_clicks, 0)
        self.assertFalse(self.call('submit', idempotency_key='weekly-37')['notice']['dispatch'])
        self.deliver(first['notice'])
        paid = self.call('submit', idempotency_key='weekly-37')
        self.assertTrue(paid['confirmed']); self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(paid['notice']['phase'], 'after_reconciliation')
        self.assertEqual(paid['notice']['payload']['correction_options']['edit_availability'], 'additions_and_reductions')
        self.assertIn('Current editing: additions and reductions currently supported', paid['notice']['payload']['message'])
        self.assertEqual(paid['notice']['payload']['correction_options']['deadline_status'], 'unknown')
        self.deliver(paid['notice'])
        recovered = self.call('submit', idempotency_key='weekly-37')
        self.assertTrue(recovered['confirmed']); self.assertTrue(recovered['notice']['delivered'])
        self.assertFalse(recovered['notice']['dispatch']); self.assertEqual(self.browser.checkout_clicks, 1)
        next_plan = self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {'week': '2026-W38', 'candidates': self.candidates}})['plan']
        self.assertEqual(next_plan['status'], 'planned')
        self.assertEqual(len(next_plan['selection']['slots']), 7)

    def test_preferred_batch_size_never_truncates_accepted_meal_coverage(self):
        self.batch(4)
        self.profile(meals=self.accepted_meals(prepared_portion_range=[3, 4]))
        plan = self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {'week': '2026-W37'}})['plan']
        self.assertEqual(plan['status'], 'planned')
        menu, _products = self.shop(plan)
        self.assertEqual([bp.fraction(b['prepared_portions']) for b in menu['batches']], [6, 8])
        self.assertEqual(len(menu['slots']), 7)
        self.assertEqual(self.store.read()['profile']['meals']['portions'], 2)
        self.assertEqual(self.store.read()['profile']['meals']['prepared_portion_range'], [3, 4])
        assessment = self.app.handle({'operation': 'menu', 'action': 'assess'})['assessment']
        self.assertTrue(assessment['ready'])
        self.assertEqual(assessment['issues'], [])

    def test_manual_finding_review_substitution_and_known_conflict(self):
        self.profile(diet={'rules': [{'kind': 'allergy', 'term': 'milk'}, {'kind': 'sensitivity', 'term': 'onion'}, {'kind': 'preference', 'term': 'sugar'}]})
        self.provider.evidence = {'ingredients': 'carrot sugar', 'allergens': []}
        prepared = self.call('prepare')
        findings = prepared['summary']['dietary_assessment']['findings']
        self.assertEqual([f['condition'] for f in findings], ['unknown', 'unknown', 'preference_deviation'])
        self.assertTrue(self.call('confirm', confirmation_id=prepared['confirmation_id'])['dietary_review_required'])
        self.provider.evidence = {'ingredients': 'milk sugar', 'allergens': ['milk']}
        changed = self.call('confirm', confirmation_id=prepared['confirmation_id'], dietary_review=[findings[0]['finding_id']])
        self.assertTrue(changed['reprepared'])
        blocked = self.call('confirm', confirmation_id=changed['confirmation_id'], dietary_review=[f['finding_id'] for f in changed['summary']['dietary_assessment']['findings']])
        self.assertTrue(blocked['dietary_review_required']); self.assertEqual(self.browser.checkout_clicks, 0)
        self.provider.product_id = 20
        self.provider.cart['items'][0]['product_id'] = 20
        self.provider.evidence = {'ingredients': 'carrot', 'allergen_free_from': ['milk']}
        new = self.call('prepare')
        new_findings = new['summary']['dietary_assessment']['findings']
        self.assertEqual(new_findings[0]['product_ref'], '20'); self.assertEqual(new_findings[0]['condition'], 'compatible_label')
        self.assertTrue(self.call('confirm', confirmation_id=new['confirmation_id'])['confirmed'])

    def test_uncovered_and_failed_or_uncertain_notice_never_dispatch(self):
        self.profile(diet={'rules': [{'kind': 'allergy', 'term': 'milk'}]})
        self.app.confirmation_policy = 'standing'
        uncovered = self.call('submit', idempotency_key='uncovered')
        self.assertTrue(uncovered['dietary_review_required']); self.assertEqual(self.browser.checkout_clicks, 0)
        self.profile(diet={'uncertainty_permissions': [{'kind': 'allergy', 'term': 'milk', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
        refreshed = self.call('submit', idempotency_key='uncovered')
        self.assertTrue(refreshed['reprepared'])
        notice = self.call('submit', idempotency_key='uncovered')['notice']
        for outcome in ('unknown', 'not_sent'):
            self.call('notice_result', notice_token=notice['notice_token'], send_outcome=outcome, sender_receipt='synthetic-native:' + outcome)
            stopped = self.call('submit', idempotency_key='uncovered')
            self.assertFalse(stopped['notice']['delivered']); self.assertFalse(stopped['notice']['dispatch'])
            self.assertEqual(self.browser.checkout_clicks, 0)

    def test_actual_batches_have_separate_outcomes_and_no_leftover_usage(self):
        menu, _ = self.shop(self.batch())
        with mock.patch.object(Application, '_household_today', return_value=date(2026, 9, 7)):
            source = menu['slots'][0]
            self.app.handle({'operation': 'recipes', 'action': 'mark_cooked', 'menu_id': menu['menu_id'], 'expected_revision': menu['revision'],
                'slot_id': source['slot_id'], 'actual_batch': {'prepared_portions': 6, 'consumed_at_source': 2}})
            self.assertEqual(len(self.store.read()['batch_outcomes']['sources']), 1)
            self.assertEqual(bp.dependency_status(self.store.read(), menu)[-1]['status'], 'planned_not_confirmed')

    def test_legacy_pending_and_manual_permission_never_bypass_disclosure(self):
        self.profile(diet={'rules': [{'kind': 'allergy', 'term': 'milk'}], 'uncertainty_permissions': [
            {'kind': 'allergy', 'term': 'milk', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
        prepared = self.call('prepare')
        self.assertTrue(self.call('confirm', confirmation_id=prepared['confirmation_id'])['dietary_review_required'])
        with self.store.locked() as state:
            state['pending_checkout'].pop('dietary_assessment')
            state['pending_checkout']['summary'].pop('dietary_assessment')
        self.provider.detail_html = '<div>Ingredienser</div><div>carrot cream</div><div>Allergener</div><div>milk</div><div>Produksjonsland</div><div>Norway</div>'
        revised = self.call('confirm', confirmation_id=prepared['confirmation_id'])
        self.assertTrue(revised['reprepared'])
        self.assertTrue(revised['summary']['dietary_assessment']['findings'][0]['blocked'])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_mixed_schedule_and_override_accounting(self):
        plan = self.batch()
        with self.assertRaisesRegex(HouseholdError, 'consumption must match'):
            self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {'week': '2026-W37', 'portions': 3}})
        self.profile(meals=self.accepted_meals(meal_mode='mixed', dinner_days=5,
            eat_days=['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
            cook_days=['Monday', 'Tuesday', 'Friday'], dishes=3, batch_dishes=1))
        mixed = self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {'week': '2026-W37'}})['plan']
        self.assertEqual(mixed['status'], 'planned')
        self.assertEqual(mixed['selection']['batches'][0]['source_date'], '2026-09-08')
        self.assertEqual(len(mixed['selection']['slots']), 5)

    def test_scheduled_notice_resume_and_completed_recovery(self):
        menu, _ = self.shop(self.batch())
        self.profile(diet={'rules': [{'kind': 'sensitivity', 'term': 'sugar'}], 'uncertainty_permissions': [
            {'kind': 'sensitivity', 'term': 'sugar', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
        self.app.confirmation_policy = 'standing'
        self.app.handle({'operation': 'schedule', 'action': 'update', 'changes': {'enabled': True, 'maximum_total': 200,
            'auto_checkout': True, 'delivery': {'weekday': 'Saturday', 'strategy': 'keep_selected'}}})
        self.app.handle({'operation': 'schedule', 'action': 'set_cron_job', 'cron_job_id': 'synthetic-weekly'})
        before = self.call('auto', occurrence='2026-W36')
        self.assertTrue(before['notification_required'])
        token = before['notice']['notice_token']
        self.deliver(before['notice'])
        paid = self.call('auto', occurrence='2026-W36')
        self.assertTrue(paid['confirmed']); self.assertEqual(self.browser.checkout_clicks, 1)
        replay = self.call('auto', occurrence='2026-W36')
        self.assertEqual(replay['notice']['notice_token'], paid['notice']['notice_token'])
        self.assertEqual(replay['payment'], paid['payment'])
        self.assertEqual(replay['notice']['payload']['payment'], paid['payment'])
        self.assertEqual(paid['payment']['authorization'], 'unknown')
        self.assertEqual(paid['payment']['charge'], 'unknown')
        self.assertFalse(replay['notice']['dispatch']); self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(len(self.store.read()['checkout_notices']), 2)

    def test_negated_text_and_explicit_never_buy_conflict(self):
        from dietary_assessment import assess
        profile = {'diet': {'rules': [{'kind': 'allergy', 'term': 'milk'}]}}
        for field, value in [('ingredients', 'milk-free bread'), ('allergens', 'Does not contain milk'), ('ingredients', 'coconut cream'), ('ingredients', 'oat milk')]:
            result = assess(profile, {'product_ref': 10, 'name': 'Bread', 'dietary_evidence': {field: value}})
            self.assertEqual(result[0]['condition'], 'unknown')
        self.profile(diet={'rules': [{'kind': 'never_buy', 'term': 'sugar'}]})
        self.provider.detail_html = '<div>Ingredienser</div><div>carrot sugar</div><div>Allergener</div><div>none</div><div>Produksjonsland</div><div>Norway</div>'
        prepared = self.call('prepare')
        finding = prepared['summary']['dietary_assessment']['findings'][0]
        self.assertTrue(finding['blocked']); self.assertIn('source_url', finding['evidence'])
        self.assertTrue(self.call('confirm', confirmation_id=prepared['confirmation_id'], dietary_review=[finding['finding_id']])['dietary_review_required'])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_explicit_plant_cream_names_preserve_other_dairy_evidence(self):
        from dietary_assessment import assess
        cases = [('fløte', 'Plantebasert fløte'), ('rømme', 'Vegansk rømme'),
                 ('cream', 'Plant-based cream'), ('cream', 'Vegan sour cream'),
                 ('sour cream', 'Vegan sour cream'), ('grädde', 'Växtbaserad grädde')]
        for term, name in cases:
            for kind in ('preference', 'never_buy', 'allergy'):
                with self.subTest(term=term, kind=kind):
                    profile = {'diet': {'rules': [{'kind': kind, 'term': term}]}}
                    item = {'product_ref': '10', 'name': name,
                            'dietary_evidence': {'ingredients': 'vann, havre, rapsolje'}}
                    finding = assess(profile, item)[0]
                    self.assertEqual(finding['condition'], 'unknown')
                    self.assertEqual(finding['evidence']['product_name'], name)
                    for field in ('ingredients', 'allergens', 'may_contain'):
                        dairy = deepcopy(item)
                        dairy['dietary_evidence'][field] = term
                        actual = assess(profile, dairy)[0]
                        self.assertEqual(actual['condition'], 'preference_deviation' if kind == 'preference' else 'conflict')
                    for dairy_name in (term, f'Laktosefri {term}', f'{name} og {term}'):
                        actual = assess(profile, {**item, 'name': dairy_name})[0]
                        self.assertEqual(actual['condition'], 'preference_deviation' if kind == 'preference' else 'conflict')
                    for negation in ('not ', 'non-', 'ikke helt ', 'inte ', 'not a ',
                                     'not completely ', 'not 100% ', 'ikke en ', 'ej '):
                        actual = assess(profile, {**item, 'name': negation + name})[0]
                        self.assertEqual(actual['condition'], 'preference_deviation' if kind == 'preference' else 'conflict')
        milk = {'diet': {'rules': [{'kind': 'allergy', 'term': 'melk'}]}}
        self.assertTrue(assess(milk, {'name': 'Plantebasert fløte',
            'dietary_evidence': {'allergens': ['melk']}})[0]['blocked'])
        # Coconut ingredients never confer a nutritional or allergen-free label.
        self.assertEqual(assess({'diet': {'rules': [{'kind': 'never_buy', 'term': 'fløte'}]}},
            {'name': 'Plantebasert fløte', 'dietary_evidence': {'ingredients': 'kokosfett'}})[0]['condition'], 'unknown')

    def test_product_title_allergen_compounds_are_bounded_positive_evidence(self):
        from dietary_assessment import assess
        positives = (
            ('melk', 'TINE Melkesjokolade'),
            ('peanøtter', 'Peanøttsmør'),
            ('egg', 'Eggnudler'),
            ('milk', 'Milk chocolate'),
            ('peanuts', 'Peanut butter'),
            ('peanut', 'Peanut butter'),
            ('egg', 'Egg noodles'),
            ('mjölk', 'Mjölkchoklad'),
            ('jordnötter', 'Jordnötssmör'),
            ('ägg', 'Äggnudlar'),
            ('ägg', 'Ägg nudlar'),
            ('peanøtter', 'Jordnötssmör'),
            ('jordnötter', 'Peanøttsmør'),
            ('peanuts', 'Jordnötssmör'),
            ('peanøtter', 'Peanøttsaus'),
            ('jordnötter', 'Jordnötssås'),
            ('peanuts', 'Peanøttkake'),
            ('egg', 'Eggerøre'),
            ('ägg', 'Äggsallad'),
            ('milk', 'Melkedrikk'),
            ('melk', 'Mjölkglass'),
        )
        for term, name in positives:
            finding = assess(
                {'diet': {'rules': [{'kind': 'allergy', 'term': term}]}},
                {'product_ref': 10, 'name': name},
            )[0]
            self.assertEqual(finding['condition'], 'conflict')
            self.assertTrue(finding['blocked'])
        negatives = (
            ('melk', 'Melkesyre'),
            ('melk', 'Melkefri sjokolade'),
            ('peanøtter', 'Peanøttfri pålegg'),
            ('egg', 'Eggefri nudler'),
            ('mjölk', 'Mjölksyra'),
            ('mjölk', 'Mjölkfri choklad'),
            ('jordnötter', 'Jordnötsfri pålägg'),
            ('ägg', 'Äggfri pasta'),
            ('milk', 'Oat milk chocolate'),
            ('melk', 'Melkesjokolade uten melk'),
            ('peanøtter', 'Uten peanøttsmør'),
            ('egg', 'Eggnudler uten egg'),
            ('mjölk', 'Utan mjölk'),
            ('milk', 'Fri från mjölk'),
            ('peanøtter', 'Utan jordnötter'),
            ('melk', 'Uten tilsatt melk'),
            ('milk', 'Utan spår av mjölk'),
            ('milk', 'Oat-based milk chocolate'),
            ('melk', 'Havrebasert melk'),
            ('mjölk', 'Havrebaserad mjölk'),
            ('milk', 'Milk chocolate without milk'),
            ('egg', 'Egg noodles without egg'),
            ('ägg', 'Ägg nudlar utan ägg'),
            ('peanut', 'Peanut butter without peanut'),
        )
        for term, name in negatives:
            finding = assess(
                {'diet': {'rules': [{'kind': 'allergy', 'term': term}]}},
                {'product_ref': 10, 'name': name},
            )[0]
            self.assertEqual(finding['condition'], 'unknown')
            self.assertFalse(finding['blocked'])

    def test_manual_affected_item_review_under_standing_policy(self):
        self.profile(diet={'rules': [{'kind': 'allergy', 'term': 'milk'}]})
        self.app.confirmation_policy = 'standing'
        prepared = self.call('prepare')
        finding = prepared['summary']['dietary_assessment']['findings'][0]
        result = self.call('confirm', confirmation_id=prepared['confirmation_id'], dietary_review=[finding['finding_id']])
        self.assertTrue(result['confirmed']); self.assertEqual(self.browser.checkout_clicks, 1)

    def test_scheduled_changed_assessment_preserves_amount_scope(self):
        self.shop(self.batch())
        self.profile(diet={'rules': [{'kind': 'sensitivity', 'term': 'sugar'}], 'uncertainty_permissions': [
            {'kind': 'sensitivity', 'term': 'sugar', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
        self.app.confirmation_policy = 'standing'
        self.app.handle({'operation': 'schedule', 'action': 'update', 'changes': {'enabled': True, 'maximum_total': 200,
            'auto_checkout': True, 'delivery': {'weekday': 'Saturday', 'strategy': 'keep_selected'}}})
        self.app.handle({'operation': 'schedule', 'action': 'set_cron_job', 'cron_job_id': 'synthetic-weekly'})
        first = self.call('auto', occurrence='2026-W36')
        original = self.store.read()['pending_checkout']
        self.provider.evidence = {'ingredients': 'sugar', 'allergens': []}
        changed = self.call('auto', occurrence='2026-W36')
        self.assertTrue(changed['reprepared'])
        pending = self.store.read()['pending_checkout']
        self.assertTrue(pending['automatic_checkout'])
        self.assertEqual(pending['occurrence'], original['occurrence'])
        self.assertEqual(pending['scheduler_context'], original['scheduler_context'])
        self.assertTrue(self.call('auto', occurrence='2026-W36')['dietary_review_required'])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_lost_payment_result_keeps_journal_and_never_dispatches_again(self):
        self.profile(diet={'rules': [{'kind': 'sensitivity', 'term': 'sugar'}], 'uncertainty_permissions': [
            {'kind': 'sensitivity', 'term': 'sugar', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
        self.app.confirmation_policy = 'standing'
        notice = self.call('submit', idempotency_key='lost-payment')['notice']
        self.deliver(notice)
        submit = self.browser.submit_checkout
        def lost_response(*args, **kwargs):
            submit(*args, **kwargs)
            raise HouseholdError('synthetic response lost after merchant accepted payment')
        self.browser.submit_checkout = lost_response
        with self.assertRaisesRegex(HouseholdError, 'response lost'):
            self.call('submit', idempotency_key='lost-payment')
        self.assertEqual(self.store.read()['pending_checkout']['status'], 'uncertain')
        recovered = self.call('submit', idempotency_key='lost-payment')
        self.assertTrue(recovered['confirmed']); self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(recovered['notice']['phase'], 'after_reconciliation')
        self.call('notice_result', notice_token=recovered['notice']['notice_token'], send_outcome='unknown', sender_receipt='synthetic-result-timeout')
        repeated = self.call('submit', idempotency_key='lost-payment')
        self.assertTrue(repeated['confirmed']); self.assertFalse(repeated['notice']['delivered'])
        self.assertFalse(repeated['notice']['dispatch']); self.assertEqual(self.browser.checkout_clicks, 1)

    def test_source_reader_binding_and_product_estimate_disclosure(self):
        from dietary_assessment import parse_oda_product_page, read_oda_product_evidence
        html = b'<div>Ingredienser</div><div>carrot sugar</div><div>Allergener</div><div>milk</div><div>Produksjonsland</div><div>Norway</div>'
        response = mock.Mock(status=301)
        response.getheader.return_value = 'https://oda.com/no/products/20-other/'
        connection = mock.Mock(); connection.getresponse.return_value = response
        with mock.patch('recipe_import_sources._PinnedConnection', return_value=connection), mock.patch('recipe_import_sources._get_bytes') as fetch:
            self.assertEqual(read_oda_product_evidence('10')['unavailable'], 'exact_public_product_detail_unavailable')
            fetch.assert_not_called()
        self.assertEqual(parse_oda_product_page(html, 'https://oda.com/no/products/10-synthetic/')['allergens'], 'milk')
        self.profile(diet={'rules': [{'kind': 'never_buy', 'term': 'sugar'}]})
        plan = self.batch()
        self.provider.detail_html = html
        menu = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_handoff': plan['save_handoff']})['menu']
        preview = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu)})['product_plan']
        approvals = [{'requirement_id': r['requirement_id'], 'candidate_refs': ['10']} for r in preview['requirements']]
        blocked = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': mp.menu_ref(menu), 'candidate_approvals': approvals})['product_plan']
        self.assertEqual(blocked['status'], 'needs_input')
        self.assertEqual(blocked['unresolved_requirements'][0]['reason'], 'dietary_conflict_no_compatible_candidate')
        self.assertEqual(blocked['requirements'][0]['quantity'], {'numerator': 1400, 'denominator': 1})

    def test_invalid_review_ids_cannot_switch_standing_checkout_to_manual(self):
        self.profile(diet={'rules': [{'kind': 'preference', 'term': 'sugar'}]})
        self.app.confirmation_policy = 'standing'
        prepared = self.call('prepare')
        with self.assertRaisesRegex(HouseholdError, 'current-summary finding IDs'):
            self.call('confirm', confirmation_id=prepared['confirmation_id'], dietary_review=['nonexistent'])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_cancellation_result_and_replay_do_not_claim_payment_release(self):
        for lost in (False, True):
            with self.subTest(lost=lost):
                self.provider.tracking = 'paid_and_modifiable'
                prepared = self.app.handle({'operation': 'orders', 'action': 'cancel_prepare', 'order_id': 'old'})
                dispatch = self.browser.submit_cancellation
                def submit(*args, **kwargs):
                    dispatch(*args, **kwargs)
                    if lost:
                        raise HouseholdError('synthetic lost cancellation reply')
                self.browser.submit_cancellation = submit
                request = {'operation': 'orders', 'action': 'cancel_confirm', 'order_id': 'old', 'confirmation_id': prepared['confirmation_id']}
                try:
                    if lost:
                        with self.assertRaisesRegex(HouseholdError, 'lost cancellation'):
                            self.app.handle(request)
                        result = self.app.handle({'operation': 'orders', 'action': 'cancel_reconcile', 'confirmation_id': prepared['confirmation_id']})
                    else:
                        result = self.app.handle(request)
                finally:
                    self.browser.submit_cancellation = dispatch
                replay = self.app.handle(request)
                self.assertEqual(result['confirmation_id'], prepared['confirmation_id'])
                self.assertEqual(result['payment_resolution'], {'authorization_release': 'unknown', 'refund': 'unknown'})
                self.assertEqual(replay['payment_resolution'], result['payment_resolution'])
        self.assertEqual(self.browser.cancel_clicks, 2)

    def test_legacy_result_readback_adds_unknown_without_rewriting_journal(self):
        for kind, flag in [('checkout', 'confirmed'), ('cancellation', 'cancelled')]:
            with self.subTest(kind=kind), self.store.locked() as state:
                self.app._bind_protected_request(state, kind, 'legacy', 'legacy-' + kind, target_id='old' if kind == 'cancellation' else None)
                self.app._store_protected_result(state, 'legacy-' + kind, kind, {flag: True, 'order_id': 'old', 'confirmation_id': 'legacy-' + kind})
        original = deepcopy(self.store.read())
        for kind, operation, action, field in [('checkout', 'checkout', 'confirm', 'payment'), ('cancellation', 'orders', 'cancel_confirm', 'payment_resolution')]:
            result = self.app.handle({'operation': operation, 'action': action, 'confirmation_id': 'legacy-' + kind, 'order_id': 'old'})
            self.assertTrue(all(v == 'unknown' for k, v in result[field].items() if k not in {'provider_status', 'source'}))
            self.app.confirmation_policy = 'standing'
            replay = self.app.handle({'operation': operation, 'action': 'submit' if kind == 'checkout' else 'cancel_submit', 'idempotency_key': 'legacy', 'order_id': 'old'})
            self.assertEqual(replay[field], result[field])
        self.assertEqual(self.store.read(), original)
        self.assertEqual(self.browser.cancel_clicks, 0); self.assertEqual(self.browser.checkout_clicks, 0)

    def test_malformed_batch_leftovers_is_an_input_error(self):
        plan = self.batch()
        menu = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_handoff': plan['save_handoff']})['menu']
        source = menu['slots'][0]
        result = self.app.handle({'operation': 'menu', 'action': 'batch_prepare', 'menu_ref': mp.menu_ref(menu), 'batch_spec': {
            'source_slot_id': source['slot_id'], 'source_snapshot_digest': source['snapshot_digest'], 'prepared_portions': 6,
            'consumed_at_source': 2, 'suitability': {'source': 'current_user', 'value': 'suitable'},
            'storage': {'source': 'current_user', 'method': 'frozen', 'max_interval_days': 7}, 'leftovers': None}})
        self.assertEqual(result['batch_plan']['status'], 'needs_input')
