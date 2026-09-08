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
        self.store = StateStore(Path(self.temp.name), CONFIG)
        self.provider = Retailer()
        self.browser = Browser(); self.browser.oda = self.provider
        self.app = Application(self.store, self.provider, self.browser)
        patch = mock.patch.object(Application, '_now', return_value=ODA_FIXTURE_NOW)
        patch.start(); self.addCleanup(patch.stop)
        self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})

    def profile(self, **changes):
        return self.app.handle({'operation': 'profile', 'action': 'update', 'changes': changes})

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
        self.profile(meals={'meal_mode': 'batch', 'dishes': 2, 'batch_dishes': 2,
            'cook_days': ['Monday', 'Thursday'], 'prepared_portion_range': [4, high], 'recurring_batch_accepted': True})
        candidates = []
        for index in range(4):
            saved = self.app.handle({'operation': 'recipes', 'action': 'save',
                'recipe': recipes_fixture.recipe(f'Gulrotgryte {index}', f'batch-{index}'), 'idempotency_key': f'recipe-{index}'})['recipe']
            candidates.append({'recipe_ref': {'id': saved['id'], 'revision': saved['revision']}})
        self.candidates = candidates
        self.profile(recipes={'sources': {'oda': False, 'meny': False, 'mathem': False, 'themealdb': False, 'wikibooks': False}})
        return self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {'week': '2026-W37'}})['plan']

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
        self.profile(diet={'rules': [{'kind': 'preference', 'term': 'sugar'}], 'uncertainty_permissions': [
            {'kind': 'preference', 'term': 'sugar', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
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
        self.assertEqual(paid['notice']['payload']['correction_options']['edit_availability'], 'additions_only')
        self.assertIn('Current editing: additions currently supported', paid['notice']['payload']['message'])
        self.assertEqual(paid['notice']['payload']['correction_options']['deadline_status'], 'unknown')
        self.deliver(paid['notice'])
        recovered = self.call('submit', idempotency_key='weekly-37')
        self.assertTrue(recovered['confirmed']); self.assertTrue(recovered['notice']['delivered'])
        self.assertFalse(recovered['notice']['dispatch']); self.assertEqual(self.browser.checkout_clicks, 1)
        next_plan = self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {'week': '2026-W38', 'candidates': self.candidates}})['plan']
        self.assertEqual(next_plan['status'], 'planned')
        self.assertEqual(len(next_plan['selection']['slots']), 7)

    def test_shortage_is_concrete_and_settings_unchanged(self):
        plan = self.batch(4)
        self.assertEqual(plan['status'], 'needs_input')
        issue = plan['issues'][0]
        self.assertEqual(issue['required_portions'], 14); self.assertEqual(issue['available_portions'], 8)
        self.assertEqual([s['proposed_prepared_portions'] for s in issue['shortages']], [6, 8])
        self.assertEqual(self.store.read()['profile']['meals']['prepared_portion_range'], [4, 4])
        self.assertIsNone(self.store.read()['menu'])

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
        self.profile(meals={'meal_mode': 'mixed', 'dinner_days': 5, 'eat_days': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
            'cook_days': ['Monday', 'Tuesday', 'Friday'], 'dishes': 3, 'batch_dishes': 1})
        mixed = self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {'week': '2026-W37'}})['plan']
        self.assertEqual(mixed['status'], 'planned')
        self.assertEqual(mixed['selection']['batches'][0]['source_date'], '2026-09-08')
        self.assertEqual(len(mixed['selection']['slots']), 5)

    def test_scheduled_notice_resume_and_completed_recovery(self):
        menu, _ = self.shop(self.batch())
        self.profile(diet={'rules': [{'kind': 'preference', 'term': 'sugar'}], 'uncertainty_permissions': [
            {'kind': 'preference', 'term': 'sugar', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
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

    def test_manual_affected_item_review_under_standing_policy(self):
        self.profile(diet={'rules': [{'kind': 'allergy', 'term': 'milk'}]})
        self.app.confirmation_policy = 'standing'
        prepared = self.call('prepare')
        finding = prepared['summary']['dietary_assessment']['findings'][0]
        result = self.call('confirm', confirmation_id=prepared['confirmation_id'], dietary_review=[finding['finding_id']])
        self.assertTrue(result['confirmed']); self.assertEqual(self.browser.checkout_clicks, 1)

    def test_scheduled_changed_assessment_preserves_amount_scope(self):
        self.shop(self.batch())
        self.profile(diet={'rules': [{'kind': 'preference', 'term': 'sugar'}], 'uncertainty_permissions': [
            {'kind': 'preference', 'term': 'sugar', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
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
        self.profile(diet={'rules': [{'kind': 'preference', 'term': 'sugar'}], 'uncertainty_permissions': [
            {'kind': 'preference', 'term': 'sugar', 'product_ref': '10', 'condition': 'unknown', 'accepted': True, 'notify': True}]})
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
