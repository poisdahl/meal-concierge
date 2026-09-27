import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unittest
from copy import deepcopy
from planning_assessment import workflow_status, capacity_warnings
from core import DEFAULT_PROFILE


class WorkflowContinuationTests(unittest.TestCase):
    def action(self, **state):
        return workflow_status({'provider':'oda', **state})['next_action']

    def test_expired_original_keeps_manual_method_and_never_confirms(self):
        action = self.action(pending_checkout={'status':'awaiting_confirmation', 'expires_at':'2000-01-01T00:00:00+00:00', 'confirmation_id':'old', 'payment_preference':{'method':'vipps'}, 'checkout_payment':{'method':'saved_card'}})
        self.assertEqual(action['arguments'], {'action':'prepare', 'checkout_payment':{'method':'saved_card'}})
        self.assertNotIn('confirmation_id', action)

    def test_cancellation_exact_identity_expiry_and_uncertain_dispatch(self):
        cancellation = {'status':'awaiting_confirmation', 'confirmation_id':'cancel', 'order_id':'order', 'expires_at':'2000-01-01T00:00:00+00:00'}
        for pending in (None, {'status':'uncertain','confirmation_id':'payment'}):
            action = self.action(pending_cancellation=cancellation, pending_checkout=pending)
            self.assertEqual(action['arguments'], {'action':'cancel_prepare','order_id':'order'})
        action = self.action(pending_cancellation={**cancellation,'status':'uncertain'})
        self.assertEqual(action['action'], 'cancel_reconcile')
        self.assertEqual(action['arguments']['confirmation_id'], 'cancel')

    def test_feedback_and_email_require_real_input(self):
        menu = {'menu_id':'m','revision':1,'digest':'d','phase':'ordered','order_id':'o','dishes':[]}
        state = {'menu':menu,'profile':deepcopy(DEFAULT_PROFILE)}
        action = self.action(**state)
        self.assertFalse(action['ready_to_call'])
        self.assertIn('reported_cooking_experience',action['needs_input'])
        action = self.action(**state,email_recipient='synthetic@example.test')
        self.assertEqual(action['arguments']['order_id'],'o')
        self.assertIn('delivery_date',action['needs_input'])
        for timing in ('on_request','after_purchase'):
            action = self.action(**state,email_recipient='synthetic@example.test',recipe_delivery={'sender_binding':{'timing':timing}})
            self.assertNotEqual(action['action'],'schedule')

    def test_capacity_is_read_only_and_respects_existing_bound(self):
        state = {'menu_planning': {'history':dict.fromkeys(map(str,range(1800)))}, 'recipe_usage':{}}
        before = deepcopy(state)
        self.assertEqual(capacity_warnings(state)[0]['status'],'approaching_limit')
        self.assertEqual(state,before)

    def test_cart_reconciliation_precedes_expired_cancellation(self):
        action = self.action(pending_cart_change={'status':'uncertain'}, pending_cancellation={
            'status':'awaiting_confirmation','expires_at':'2000-01-01T00:00:00+00:00','order_id':'o'})
        self.assertEqual((action['operation'],action['action']),('cart','reconcile_change'))

    def test_product_arguments_execute_with_exact_current_menu(self):
        import test_meal_concierge_products as fixtures
        from planning_assessment import _continuation
        fixture = fixtures.ProductRuntimeTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        action = {'operation':'products','action':'prepare'}
        _continuation(action, fixture.store.read(), fixture.menu, {})
        self.assertEqual(action['arguments']['menu_ref'],fixture.menu_ref)
        result = fixture.app.handle({'operation':action['operation'],**action['arguments']})
        self.assertIn('product_plan',result)
        self.assertEqual(fixture.provider.cart['items'],[])
