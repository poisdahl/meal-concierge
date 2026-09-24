"""Same-payment switching through Application and durable on-disk journals."""
from copy import deepcopy
import unittest

import test_payment_recovery as recovery_fixtures
from test_payment_recovery import AMOUNTS
from core import HouseholdError, StateStore
from service import Application
from oda_browser import _oda_checkout_amounts_minor


class PaymentSwitchTests(unittest.TestCase):
    def setUp(self):
        self.flow = recovery_fixtures.OdaAdditionPaymentTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.app = self.flow.app
        self.browser = self.flow.browser
        self.merchant = self.flow.merchant
        self.source = self.flow.call('prepare')
        self.flow.call('confirm', confirmation_id=self.source['confirmation_id'])
        self.merchant.status = 'unpaid_order_change'
        self.cancel_clicks = 0
        self.target_reads = 0
        self.card_clicks = 0
        self.native_state = 'SUBMITTED'
        self.cancel_unknown = False
        self.lose_cancel = False
        self.lose_target_read = False
        self.paid_during_cancel = False
        self.target = None
        self.target_change_id = 'change-1'
        self.target_invalid = False
        self.target_unknown = False
        self.card_pending = False
        self.binding_checks = 0

        def close(context, before_cancel, *, prior, **kwargs):
            self.assertEqual(context['order_id'], 'order-1')
            self.assertEqual(context['expected_total'], 1670)
            if self.paid_during_cancel:
                self.flow.accept_addition()
                self.merchant.status = 'paid_and_modifiable'
                return {'status': 'paid', 'terminal_status': 'ACCEPTED', 'source_digest': 'b' * 64}
            if self.native_state == 'SUBMITTED' and not prior.get('cancel_attempted'):
                before_cancel({'source_digest': 'b' * 64, 'observed_status': 'SUBMITTED'})
                current = self.app.store.read()['pending_checkout']
                self.assertTrue((current.get('recovery') or current)['payment_switch']['cancel_attempted'])
                self.cancel_clicks += 1
                if not self.cancel_unknown:
                    self.native_state = 'REJECTED'
                if self.lose_cancel:
                    self.lose_cancel = False
                    raise HouseholdError('lost cancellation response')
            return {'status': 'closed' if self.native_state in {'REJECTED', 'FAILED'} else 'unknown',
                    'terminal_status': self.native_state, 'source_digest': 'b' * 64, 'payment_id': '123456'}

        def prepare_target(order_id, cart, before_order, binding, *, closure, retained_target, **kwargs):
            self.assertEqual(order_id, 'order-1')
            self.assertEqual(before_order, self.flow.original)
            self.assertEqual(cart, self.flow.cart)
            self.assertEqual(binding, self.flow.binding)
            self.assertEqual(closure['status'], 'closed')
            self.assertEqual(closure['payment_id'], '123456')
            self.target_reads += 1
            if self.lose_target_read:
                self.lose_target_read = False
                raise HouseholdError('lost payment target read')
            if self.target_unknown:
                return {'status': 'unknown'}
            self.target = {'order_id': order_id, 'order_change_id': self.target_change_id,
                           'payment_id': closure['payment_id'], 'goods_digest': 'c' * 64}
            if retained_target is not None:
                self.assertEqual(retained_target, self.target)
            return deepcopy(self.target)

        def verify(order_id, cart, before_order, binding, target, **kwargs):
            self.binding_checks += 1
            if self.target_invalid or target != self.target:
                raise HouseholdError('native addition target changed')
            self.assertEqual(order_id, target['order_id'])
            self.assertEqual(cart, self.flow.cart)
            self.assertEqual(before_order, self.flow.original)
            self.assertEqual(binding, self.flow.binding)

        def review(cart, order_id, *, payment, expected_binding, addition, **kwargs):
            self.assertEqual(addition['payment_switch_target'], self.target)
            self.assertEqual(addition['order_change_id'], self.target_change_id)
            self.assertEqual(payment['method'], 'saved_card')
            amounts = {key: None for key in AMOUNTS}
            amounts.update(product_subtotal=16.70, provider_total=16.70)
            return {'order_id': order_id, 'binding': deepcopy(expected_binding), 'payment_choice': deepcopy(payment),
                    'payment_display': '•••• 1234', 'amounts_minor': _oda_checkout_amounts_minor(amounts)}

        def pay(cart, review, before_click, *, addition, **kwargs):
            self.assertEqual(addition['payment_switch_target'], self.target)
            before_click()
            self.card_clicks += 1
            if self.card_pending:
                return {'authentication_context': {'tab_id': 'card-tab', 'payment_id': '123456'}}
            self.flow.accept_addition()
            self.merchant.status = 'paid_and_modifiable'
            return {}
        self.browser.close_vipps_request = close
        self.browser.prepare_oda_addition_retry = prepare_target
        self.browser.verify_oda_addition_retry = verify
        self.browser.review_payment_recovery = review
        self.browser.submit_payment_recovery = pay

    def call(self, action, **kwargs):
        return self.app.handle({'operation': 'checkout', 'action': action, **kwargs})

    def switch(self, **kwargs):
        return self.call('switch_payment', confirmation_id=self.source['confirmation_id'],
                         checkout_payment={'method': 'saved_card'}, **kwargs)

    def reopen(self):
        self.app = Application(StateStore(self.flow.temp.name, self.flow.settings), self.merchant, self.browser)
        self.app._now = self.flow.app._now

    def test_switch_closes_once_and_prepares_card_for_exact_same_addition(self):
        prepared = self.switch()
        self.assertTrue(prepared['recovery'])
        self.assertEqual(prepared['summary']['payment_method'], 'saved_card')
        self.assertEqual(prepared['summary']['total'], 16.70)
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 1, 0))
        pending = self.app.store.read()['pending_checkout']
        self.assertEqual(pending['vipps_request_context']['order_id'], 'order-1')
        self.assertEqual(pending['recovery']['payment_switch']['closure']['terminal_status'], 'REJECTED')
        self.assertEqual(self.switch()['confirmation_id'], prepared['confirmation_id'])
        self.assertTrue(self.call('confirm', confirmation_id=prepared['confirmation_id'])['confirmed'])
        self.assertTrue(self.switch()['confirmed'])
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 1, 1))
        self.assertEqual(self.app.store.read()['checkout_payment']['method'], 'vipps')
        self.assertEqual(self.app.store.read()['order_snapshots'], self.flow.snapshots)

    def test_lower_native_charge_survives_switch_review_and_restart(self):
        from core import StateStore
        self.target_change_id = '7654321'
        context = {'provider_charge_minor': 1200, 'payment_id': '123456',
                   'order_change_id': '7654321'}
        with self.app.store.locked() as state:
            state['pending_checkout']['vipps_request_context'].update(context)
        original_review = self.browser.review_payment_recovery
        def review(*args, **kwargs):
            self.assertEqual(kwargs['addition']['provider_charge_minor'], 1200)
            result = original_review(*args, **kwargs)
            result['provider_charge_minor'] = 1200
            result['amounts_minor'].update(provider_total=1671, bags=750)
            return result
        self.browser.review_payment_recovery = review
        prepared = self.switch()
        self.assertEqual(prepared['summary']['total'], 16.70)
        self.assertEqual(prepared['summary']['provider_charge_total'], 12.00)
        self.assertEqual(prepared['summary']['merchant_summary_total'], 16.71)
        self.app = Application(StateStore(self.flow.temp.name, self.flow.settings), self.merchant, self.browser)
        self.app._now = self.flow.app._now
        self.assertTrue(self.call('confirm', confirmation_id=prepared['confirmation_id'])['confirmed'])
        self.assertEqual((self.cancel_clicks, self.card_clicks), (1, 1))

    def test_changed_native_payment_target_blocks_switch_after_closure(self):
        self.target_change_id = '7654321'
        with self.app.store.locked() as state:
            state['pending_checkout']['vipps_request_context'].update(
                provider_charge_minor=1200, payment_id='123456', order_change_id='9999999')
        with self.assertRaisesRegex(HouseholdError, 'payment target changed'):
            self.switch()
        self.assertEqual((self.cancel_clicks, self.card_clicks), (1, 0))

    def test_child_context_requires_its_frozen_charge_and_change(self):
        from order_operations import OrderOperations
        self.target_change_id = '7654321'
        with self.app.store.locked() as state:
            state['pending_checkout']['vipps_request_context'].update(
                provider_charge_minor=1200, payment_id='123456', order_change_id='7654321')
        original_review = self.browser.review_payment_recovery
        def review(*args, **kwargs):
            result = original_review(*args, **kwargs)
            result['provider_charge_minor'] = 1200
            return result
        self.browser.review_payment_recovery = review
        self.switch()
        pending = self.app.store.read()['pending_checkout']
        context = {'tab_id': 'owned', 'order_id': 'order-1', 'expected_total': 1670,
                   'gateway_url_digest': 'b' * 64, 'provider_charge_minor': 1200,
                   'payment_id': '123457', 'order_change_id': '7654321'}
        self.assertTrue(OrderOperations._vipps_context_matches(context, pending, 'order-1', child=True))
        for change in ({'provider_charge_minor': 1199}, {'order_change_id': '7654322'},
                       {'payment_id': 'wrong'}):
            self.assertFalse(OrderOperations._vipps_context_matches(
                {**context, **change}, pending, 'order-1', child=True))

    def test_legacy_addition_adoption_persists_only_exact_native_context(self):
        self.target_change_id = '7654321'
        with self.app.store.locked() as state:
            state['pending_checkout'].pop('vipps_request_context', None)
        context = {'tab_id': 'owned', 'order_id': 'order-1', 'expected_total': 1670,
                   'gateway_url_digest': 'b' * 64, 'provider_charge_minor': 1200,
                   'payment_id': '123456', 'order_change_id': '7654321'}
        self.browser.read_order_binding = lambda *args, **kwargs: deepcopy(self.flow.binding)
        calls = []
        def adopt(cart, review, *, deadline, order_id, addition):
            calls.append((order_id, addition))
            return deepcopy(context)
        self.browser.adopt_vipps_request = adopt
        pending = self.app.store.read()['pending_checkout']
        adopted, observed = self.app._adopt_checkout_vipps_request(pending, 'order-1', None)
        self.assertEqual(observed, context)
        self.assertEqual(adopted['vipps_request_context'], context)
        self.assertEqual(adopted['vipps_request_status'], 'unknown')
        self.assertEqual(calls, [('order-1', True)])
        self.assertEqual((self.cancel_clicks, self.card_clicks), (0, 0))

    def test_failed_card_addition_can_switch_to_vipps_without_vipps_context(self):
        with self.app.store.locked() as state:
            pending = state['pending_checkout']
            pending['checkout_payment'] = {'method': 'saved_card', 'card_last4': None}
            pending['browser_review']['payment_choice'] = {'method': 'saved_card', 'card_last4': None}
            pending['browser_review']['payment_display'] = '•••• 1234'
            pending.pop('vipps_request_context', None)
            pending.pop('vipps_request_status', None)
            pending['authentication_context'] = {'tab_id': 'card-tab', 'payment_id': '123456'}
            pending['payment_failure'] = {'payment_failed': True, 'order_id': 'order-1',
                                          'order_change_id': 'change-1'}
        original_review = self.browser.review_payment_recovery
        def review(*args, **kwargs):
            result = original_review(*args, **{**kwargs, 'payment': {'method': 'saved_card'}})
            result['payment_choice'] = {'method': 'vipps'}
            result['payment_display'] = 'Vipps'
            return result
        self.browser.review_payment_recovery = review
        prepared = self.call('switch_payment', confirmation_id=self.source['confirmation_id'],
                             checkout_payment={'method': 'vipps'})
        self.assertTrue(prepared['recovery'])
        self.assertEqual(prepared['summary']['payment_method'], 'vipps')
        self.assertEqual((self.cancel_clicks, self.card_clicks), (0, 0))

    def test_lost_cancel_response_reopens_without_repeating_abort(self):
        self.lose_cancel = True
        with self.assertRaisesRegex(HouseholdError, 'lost cancellation'):
            self.switch()
        self.reopen()
        prepared = self.switch()
        self.assertTrue(prepared['recovery'])
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 1, 0))

    def test_unknown_cancel_never_retries_or_prepares_card(self):
        self.cancel_unknown = True
        for _ in range(2):
            result = self.switch()
            self.assertTrue(result['payment_switch_pending'])
            self.assertFalse(result['recovery_preparation_available'])
            self.reopen()
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 0, 0))
        self.assertNotIn('recovery', self.app.store.read()['pending_checkout'])

    def test_lost_target_read_reopens_and_safely_rereads_without_repeating_cancel(self):
        self.lose_target_read = True
        with self.assertRaisesRegex(HouseholdError, 'lost payment target read'):
            self.switch()
        self.reopen()
        prepared = self.switch()
        self.assertTrue(prepared['recovery'])
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 2, 0))

    def test_already_closed_request_needs_no_abort_and_paid_request_no_card(self):
        self.native_state = 'REJECTED'
        self.assertTrue(self.switch()['recovery'])
        self.assertEqual(self.cancel_clicks, 0)

    def test_paid_before_or_during_close_reconciles_without_retry(self):
        for before in (True, False):
            with self.subTest(before=before):
                if before:
                    self.flow.accept_addition()
                    self.merchant.status = 'paid_and_modifiable'
                else:
                    self.merchant.order = deepcopy(self.flow.original)
                    self.merchant.status = 'unpaid_order_change'
                    with self.app.store.locked() as state:
                        state['pending_checkout'] = deepcopy(self.saved_pending)
                        state['order_change'] = deepcopy(self.saved_pending['order_change'])
                        state['protected_results'].clear()
                    self.paid_during_cancel = True
                if before:
                    self.saved_pending = deepcopy(self.app.store.read()['pending_checkout'])
                self.assertTrue(self.switch()['confirmed'])
                self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (0, 0, 0))

    def test_reprepare_keeps_card_choice_but_cannot_restart_vipps_from_old_closure(self):
        self.switch()
        repeated = self.call('prepare', recovery=True)
        self.assertEqual(repeated['summary']['payment_method'], 'saved_card')
        with self.assertRaisesRegex(HouseholdError, 'only its requested method'):
            self.call('prepare', recovery=True, checkout_payment={'method': 'vipps'})
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 1, 0))

    def test_target_change_before_card_dispatch_blocks_payment(self):
        from unittest import mock

        prepared = self.switch()
        self.target_invalid = True
        submit = mock.Mock(side_effect=self.browser.submit_payment_recovery)
        self.browser.submit_payment_recovery = submit
        with self.assertRaisesRegex(HouseholdError, 'target changed'):
            self.call('confirm', confirmation_id=prepared['confirmation_id'])
        submit.assert_not_called()
        self.assertEqual(self.card_clicks, 0)

    def test_free_bags_on_retry_preserve_actual_review_and_complete_same_addition(self):
        original_review = self.browser.review_payment_recovery
        def review(*args, **kwargs):
            result = original_review(*args, **kwargs)
            result['amounts_minor']['bags'] = 0
            return result
        self.browser.review_payment_recovery = review
        prepared = self.switch()
        pending = self.app.store.read()['pending_checkout']
        self.assertIsNone(pending['browser_review']['amounts']['bags'])
        self.assertEqual(pending['recovery']['browser_review']['amounts_minor']['bags'], 0)
        self.assertTrue(self.call('confirm', confirmation_id=prepared['confirmation_id'])['confirmed'])
        self.assertEqual((self.cancel_clicks, self.card_clicks), (1, 1))

    def test_retry_fee_equivalence_rejects_nonzero_and_invalid_values(self):
        original_review = self.browser.review_payment_recovery
        for original, value in ((original, value) for original in (None, 0)
                                for value in (1, -1, '0', False, {}, [])):
            with self.subTest(original=original, value=value):
                with self.app.store.locked() as state:
                    state['pending_checkout']['browser_review']['amounts']['bags'] = original
                def review(*args, **kwargs):
                    result = original_review(*args, **kwargs)
                    result['amounts_minor']['bags'] = value
                    return result
                self.browser.review_payment_recovery = review
                with self.assertRaisesRegex(HouseholdError, 'Recovery fees differ'):
                    self.switch()
                self.assertNotIn('recovery', self.app.store.read()['pending_checkout'])
        self.assertEqual((self.cancel_clicks, self.card_clicks), (1, 0))

    def test_omitted_free_bags_on_retry_match_explicit_original_zero(self):
        with self.app.store.locked() as state:
            state['pending_checkout']['browser_review']['amounts']['bags'] = 0
        prepared = self.switch()
        self.assertIsNone(self.app.store.read()['pending_checkout']['recovery']['browser_review']['amounts_minor']['bags'])
        self.assertTrue(self.call('confirm', confirmation_id=prepared['confirmation_id'])['confirmed'])
        self.assertEqual(self.card_clicks, 1)

    def test_card_authentication_never_shows_parent_vipps_request(self):
        prepared = self.switch()
        self.card_pending = True
        self.browser.checkout_payment_authentication = lambda *a, **kw: {'active': True, 'challenge': True}
        result = self.call('confirm', confirmation_id=prepared['confirmation_id'])
        self.assertEqual(result['payment_method'], 'saved_card')
        self.assertTrue(result['authentication_required'])
        self.assertNotIn('payment_request_state', result)
        self.assertEqual(result['confirmation_id'], prepared['confirmation_id'])
        with self.assertRaisesRegex(HouseholdError, 'current payment attempt'):
            self.switch()
        self.assertEqual(self.card_clicks, 1)

    def test_dispatched_vipps_recovery_is_archived_before_its_card_replacement(self):
        with self.app.store.locked() as state:
            pending = state['pending_checkout']
            pending['checkout_payment'] = {'method': 'saved_card', 'card_last4': None}
            pending['payment_preference'] = deepcopy(state['checkout_payment'])
            pending['recovery'] = {
                'confirmation_id': 'vipps-recovery', 'order_id': 'order-1', 'status': 'awaiting_user_payment',
                'expires_at': pending['expires_at'], 'browser_review': deepcopy(pending['browser_review']),
                'dietary_assessment': deepcopy(pending['dietary_assessment']),
                'vipps_request_status': 'sent', 'vipps_request_context': deepcopy(pending['vipps_request_context'])}
        self.source = {'confirmation_id': 'vipps-recovery'}
        prepared = self.switch()
        self.assertTrue(prepared['recovery'])
        archived = self.app.store.read()['protected_results']['vipps-recovery']
        self.assertTrue(archived['result']['payment_closed'])
        self.assertEqual(archived['failed_attempt']['vipps_request_context']['order_id'], 'order-1')
        self.assertFalse(self.call('confirm', confirmation_id='vipps-recovery')['confirmed'])
        self.assertEqual(self.card_clicks, 0)
        self.assertTrue(self.call('confirm', confirmation_id=prepared['confirmation_id'])['confirmed'])
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 1, 1))

    def test_unavailable_target_is_read_again_without_repeating_cancel(self):
        self.target_unknown = True
        for _ in range(2):
            result = self.switch()
            self.assertTrue(result['payment_switch_pending'])
            self.assertFalse(result['recovery_preparation_available'])
            self.reopen()
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 2, 0))
        self.target_unknown = False
        self.assertTrue(self.switch()['recovery'])
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 3, 0))

    def test_wrong_confirmation_method_or_context_cannot_cancel(self):
        for request in [dict(confirmation_id='other', checkout_payment={'method': 'saved_card'}),
                        dict(confirmation_id=self.source['confirmation_id'], checkout_payment={'method': 'vipps'}),
                        dict(confirmation_id=self.source['confirmation_id'])]:
            with self.assertRaises(HouseholdError):
                self.call('switch_payment', **request)
        with self.app.store.locked() as state:
            state['pending_checkout']['vipps_request_context']['order_id'] = 'other'
        with self.assertRaisesRegex(HouseholdError, 'not retained'):
            self.switch()
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (0, 0, 0))


class NativeRetryTabTests(unittest.TestCase):
    def fixture(self, *, changed_order=False, retry_order='order-1', retry_change=123,
                restarted=False):
        from contextlib import nullcontext
        from oda_browser import OdaBrowser

        browser = OdaBrowser.__new__(OdaBrowser)
        browser.checkout_provider = 'oda'
        browser._checkout_operation = lambda *args, **kwargs: nullcontext()
        cart = deepcopy(recovery_fixtures.CART)
        original = deepcopy(recovery_fixtures.Merchant().order)
        current_order = deepcopy(original)
        if changed_order:
            current_order['grossAmount'] += 1
        binding = {'account_reference_digest': 'a' * 64, 'receipt_address': 'Example street 1'}
        payment_tab = 'fresh' if restarted else 'vipps'
        state = {'tab': payment_tab, 'urls': {payment_tab: 'about:blank' if restarted else 'https://pay.vipps.no/?token=retained'},
                 'events': []}

        class Provider:
            def call(self, name, arguments, *, deadline):
                self_outer.assertEqual((name, arguments), ('get_order', {'order_number': 'order-1'}))
                return deepcopy(current_order)

        self_outer = self
        browser._binding_client = lambda: Provider()
        browser._checkout_dispatch_tab = lambda: state['tab']

        def invoke(*args):
            if args == ('tab', 'list'):
                return {'tabs': [{'tabId': tab, 'label': 'meal-concierge-order-inspection' if tab == 'inspect' else 'payment'}
                                 for tab in state['urls']]}
            if args[:2] == ('tab', 'new'):
                self.assertEqual(args[2:4], ('--label', 'meal-concierge-order-inspection'))
                state['tab'] = 'inspect'
                state['urls']['inspect'] = args[4]
                state['events'].append(('inspection_open', state['tab']))
                return {'tabId': 'inspect'}
            if args[0] == 'tab':
                self.assertIn(args[1], state['urls'])
                state['tab'] = args[1]
                state['events'].append(('tab_select', state['tab']))
                return {}
            if args == ('get', 'url'):
                return {'url': state['urls'][state['tab']]}
            self.fail(f'unexpected browser command: {args}')

        browser._invoke = invoke

        def read_binding(order_id, before_order, *, deadline, expected_binding):
            self.assertEqual(state['tab'], 'inspect')
            self.assertEqual((order_id, before_order, expected_binding), ('order-1', original, binding))
            state['events'].append(('binding_read', state['tab']))
            return binding

        browser._read_order_binding = read_binding

        def eval_retry(script):
            self.assertIn('/api/v1/checkout/payment/', script)
            state['events'].append(('retry_read', state['tab']))
            return {'retry': {'type': 'checkout-payment-retry', 'params': {
                'order_number': retry_order, 'order_change_id': retry_change}}}

        browser._eval = eval_retry

        def open_url(url):
            self.assertEqual(state['tab'], payment_tab)
            state['events'].append(('retry_open', state['tab']))
            state['urls'][state['tab']] = url

        browser._open = open_url
        closure = {'status': 'closed', 'terminal_status': 'TIMEOUT', 'payment_id': '123456'}
        return browser, cart, original, binding, closure, state

    def test_closed_native_timeout_inspects_then_restores_and_verifies_retry(self):
        from oda_payment_switch import prepare_oda_addition_retry
        browser, cart, original, binding, closure, state = self.fixture()
        target = prepare_oda_addition_retry(
            browser, 'order-1', cart, original, binding, deadline=None, closure=closure)
        self.assertEqual({key: target[key] for key in ('order_id', 'order_change_id', 'payment_id')},
                         {'order_id': 'order-1', 'order_change_id': '123', 'payment_id': '123456'})
        self.assertEqual(state['events'], [
            ('inspection_open', 'inspect'), ('binding_read', 'inspect'),
            ('retry_read', 'inspect'), ('tab_select', 'vipps'),
            ('retry_open', 'vipps'), ('retry_read', 'vipps')])
        self.assertEqual(state['urls']['vipps'],
                         'https://oda.com/no/checkout/retry/?orderNumber=order-1&orderChangeId=123')

    def test_changed_original_order_or_payment_target_never_opens_retry(self):
        from oda_payment_switch import prepare_oda_addition_retry
        browser, cart, original, binding, closure, _state = self.fixture()
        target = prepare_oda_addition_retry(browser, 'order-1', cart, original, binding,
                                            deadline=None, closure=closure)
        for changes, message in (({'changed_order': True}, 'original Oda order changed'),
                                 ({'retry_order': 'other'}, 'exact order addition'),
                                 ({'retry_change': 124}, 'retry target changed')):
            with self.subTest(changes=changes):
                browser, cart, original, binding, closure, state = self.fixture(**changes)
                with self.assertRaisesRegex(HouseholdError, message):
                    prepare_oda_addition_retry(browser, 'order-1', cart, original, binding,
                                               deadline=None, closure=closure,
                                               retained_target=target if 'retry_change' in changes else None)
                self.assertEqual(state['tab'], 'vipps')
                self.assertNotIn(('retry_open', 'vipps'), state['events'])

    def test_restarted_browser_reopens_only_retained_closed_retry(self):
        from oda_payment_switch import prepare_oda_addition_retry
        browser, cart, original, binding, closure, state = self.fixture(restarted=True)
        target = {'order_id': 'order-1', 'order_change_id': '123', 'payment_id': '123456'}
        from oda_payment_switch import _cart_digest
        target['cart_digest'] = _cart_digest(cart)
        observed = prepare_oda_addition_retry(browser, 'order-1', cart, original, binding,
            deadline=None, closure=closure, retained_target=target)
        self.assertEqual(observed, target)
        self.assertEqual(state['events'].count(('retry_open', 'fresh')), 1)
        self.assertEqual(state['urls']['fresh'],
            'https://oda.com/no/checkout/retry/?orderNumber=order-1&orderChangeId=123')


class NativeObservedAdoptionTests(unittest.TestCase):
    def test_addition_adopts_exact_lower_native_charge_and_rejects_unbound_charge(self):
        from contextlib import nullcontext
        import json
        from oda_payment_switch import adopt_vipps_request, _native_vipps_terminal

        browser, validation, poll = recovery_fixtures.VippsNativeTerminalTests().fixture()
        source = 'https://pay.vipps.no/?token=source'
        claims = json.loads(validation['responseBody'])
        claims['amount'] = 1640
        validation['responseBody'] = json.dumps(claims)
        validation['timestamp'] = 3
        poll['timestamp'] = 4
        pay = {'url': 'https://oda.com/api/v1/checkout/pay/', 'method': 'POST', 'status': 200,
               'timestamp': 2, 'requestId': 'pay', 'postData': json.dumps({
                   'mode': {'type': 'confirm_modification', 'orderNumber': 'order-1'},
                   'primaryPayment': {'methodId': 25, 'paymentMethodData': {}}})}
        browser.rows.append(pay)
        browser.checkout_provider = 'oda'
        browser.vipps_phone_number = '90000000'
        browser._cart_expectation = lambda cart: {'total_minor': 2040}
        browser._checkout_operation = lambda *args, **kwargs: nullcontext()
        browser._inspection_tab = lambda: nullcontext()
        browser._order_url = lambda order_id: 'https://oda.com/no/orders/' + order_id + '/'
        browser._open = lambda url: None
        browser._select_payment_tab = lambda tab_id: tab_id == 'owned'
        original_invoke = browser._invoke
        browser._invoke = lambda *args: (
            {'tabs': [{'tabId': 'owned', 'url': source}]} if args == ('tab', 'list') else
            {'url': source} if args == ('get', 'url') else original_invoke(*args))
        target = [7654321]
        browser._eval = lambda script: (
            {'retry': {'type': 'checkout-payment-retry', 'params': {
                'order_number': 'order-1', 'order_change_id': target[0]}}}
            if '/api/v1/checkout/payment/' in script else {'identity': True, 'sent': True})
        context = adopt_vipps_request(browser, {}, {'order_id': 'order-1'}, deadline=None,
                                      order_id='order-1', addition=True)
        self.assertEqual(context['expected_total'], 2040)
        self.assertEqual(context['provider_charge_minor'], 1640)
        self.assertEqual(context['payment_id'], '456')
        self.assertEqual(context['order_change_id'], '7654321')
        self.assertEqual(_native_vipps_terminal(browser, source, context)['terminal_status'], 'TIMEOUT')
        for mutate in (lambda: pay.update(timestamp=5),
                       lambda: target.__setitem__(0, None),
                       lambda: claims.update(amount=2041),
                       lambda: claims.update(currency='SEK')):
            with self.subTest(mutate=mutate):
                old_pay, old_target, old_claims = pay['timestamp'], target[0], dict(claims)
                mutate()
                validation['responseBody'] = json.dumps(claims)
                self.assertIsNone(adopt_vipps_request(browser, {}, {'order_id': 'order-1'},
                    deadline=None, order_id='order-1', addition=True))
                pay['timestamp'], target[0] = old_pay, old_target
                claims.clear(); claims.update(old_claims)
                validation['responseBody'] = json.dumps(claims)
        pay['postData'] = json.dumps({'mode': {'type': 'confirm_modification', 'orderNumber': 'order-1'},
                                      'primaryPayment': {'methodId': 99, 'paymentMethodData': {}}})
        self.assertIsNone(adopt_vipps_request(browser, {}, {'order_id': 'order-1'},
                          deadline=None, order_id='order-1', addition=True))
        pay['postData'] = json.dumps({'mode': {'type': 'confirm_modification', 'orderNumber': 'order-1'},
                                      'primaryPayment': {'methodId': 25, 'paymentMethodData': {}}})
        for invalid_url in ('https://pay.vipps.no/?token=other',
                            'https://pay.vipps.no/?token=source&other=1'):
            with self.subTest(invalid_url=invalid_url):
                browser._invoke = lambda *args: (
                    {'tabs': [{'tabId': 'owned', 'url': invalid_url}]} if args == ('tab', 'list') else
                    {'url': invalid_url} if args == ('get', 'url') else original_invoke(*args))
                self.assertIsNone(adopt_vipps_request(browser, {}, {'order_id': 'order-1'},
                                  deadline=None, order_id='order-1', addition=True))

    def test_native_validation_amount_fallback_and_retry_order_bind_adoption(self):
        from contextlib import nullcontext
        import json
        from oda_payment_switch import adopt_vipps_request

        browser, validation, _poll = recovery_fixtures.VippsNativeTerminalTests().fixture()
        source = 'https://pay.vipps.no/?token=source'
        browser.checkout_provider = 'oda'
        browser.vipps_phone_number = '90000000'
        browser._cart_expectation = lambda cart: {'total_minor': 4370}
        browser._checkout_operation = lambda *args, **kwargs: nullcontext()
        browser._inspection_tab = lambda: nullcontext()
        browser._order_url = lambda order_id: 'https://oda.com/no/orders/' + order_id + '/'
        browser._open = lambda url: None
        browser._select_payment_tab = lambda tab_id: tab_id == 'owned'
        native_invoke = browser._invoke
        browser._invoke = lambda *args: (
            {'tabs': [{'tabId': 'owned', 'url': source}]} if args == ('tab', 'list') else
            {'url': source} if args == ('get', 'url') else native_invoke(*args))
        retry_order = ['order-1']
        browser._eval = lambda script: (
            {'retry': {'type': 'checkout-payment-retry', 'params': {
                'order_number': retry_order[0], 'order_change_id': None}}}
            if '/api/v1/checkout/payment/' in script else
            {'identity': True, 'sent': True})
        adopted = adopt_vipps_request(browser, {}, {}, deadline=None, order_id='order-1')
        self.assertEqual(adopted['tab_id'], 'owned')
        self.assertEqual(adopted['expected_total'], 4370)
        self.assertEqual(adopted['order_id'], None)

        claims = json.loads(validation['responseBody'])
        for changed in ({**claims, 'amount': 4371},
                        {**claims, 'fallback': 'https://example.org/no/checkout/456/redirect-return/'}):
            with self.subTest(changed=changed):
                validation['responseBody'] = json.dumps(changed)
                self.assertIsNone(adopt_vipps_request(browser, {}, {}, deadline=None, order_id='order-1'))
        validation['responseBody'] = json.dumps(claims)
        retry_order[0] = 'other'
        self.assertIsNone(adopt_vipps_request(browser, {}, {}, deadline=None, order_id='order-1'))


class NewOrderPaymentSwitchTests(unittest.TestCase):
    def test_dispatched_new_order_recovery_switch_binds_its_known_order(self):
        flow = recovery_fixtures.RecoveryTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        original_submit = flow.browser.submit_payment_recovery
        source = flow.prepare_saved_card_to_vipps_recovery()
        cancels = []
        def close(context, before_cancel, **kwargs):
            self.assertEqual(context['order_id'], 'order-1')
            self.assertEqual(context['expected_total'], 24640)
            before_cancel({'source_digest': 'a' * 64})
            cancels.append(True)
            return {'status': 'closed', 'terminal_status': 'REJECTED', 'source_digest': 'a' * 64}
        flow.browser.close_vipps_request = close
        prepared = flow.call('switch_payment', confirmation_id=source['confirmation_id'],
                             checkout_payment={'method': 'saved_card'})
        self.assertEqual(prepared['order_id'], 'order-1')
        self.assertNotEqual(prepared['confirmation_id'], source['confirmation_id'])
        self.assertEqual(flow.browser.clicks, 1)
        flow.browser.submit_payment_recovery = original_submit
        self.assertTrue(flow.call('confirm', confirmation_id=prepared['confirmation_id'])['confirmed'])
        self.assertEqual(flow.browser.clicks, 2)
        self.assertEqual(cancels, [True])

    def test_same_new_order_switch_preserves_context_and_never_enters_addition_retry(self):
        flow = recovery_fixtures.RecoveryTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        cancels = []
        with flow.app.store.locked() as state:
            pending = state['pending_checkout']
            pending['vipps_request_status'] = 'sent'
            pending['vipps_request_context'] = {'tab_id': 'owned', 'expected_total': 24640,
                                               'gateway_url_digest': 'b' * 64, 'order_id': None}
        flow.browser.vipps_request_state = 'sent'
        def close(context, before_cancel, **kwargs):
            self.assertIsNone(context['order_id'])
            before_cancel({'source_digest': 'b' * 64})
            cancels.append(True)
            return {'status': 'closed', 'terminal_status': 'REJECTED', 'source_digest': 'b' * 64}
        flow.browser.close_vipps_request = close
        prepared = flow.call('switch_payment', confirmation_id='original', checkout_payment={'method': 'saved_card'})
        self.assertTrue(prepared['recovery'])
        self.assertEqual(prepared['order_id'], 'order-1')
        self.assertEqual(flow.browser.clicks, 0)
        result = flow.call('confirm', confirmation_id=prepared['confirmation_id'])
        self.assertTrue(result['confirmed'])
        self.assertEqual(flow.browser.clicks, 1)
        self.assertEqual(cancels, [True])


class AdditionRelativeDeliveryTests(unittest.TestCase):
    def test_observed_relative_oda_cart_delivery_binds_full_original_date_window_and_address(self):
        from datetime import datetime, timezone
        for display, date_value, address, accepted in [
                ('Hjemlevering mellom kl 13 og 18, i morgen', '2026-09-12', 'Example street 1', True),
                ('Hjemlevering mellom kl 13 og 18, i dag', '2026-09-12', 'Example street 1', False),
                ('Hjemlevering mellom kl 14 og 18, i morgen', '2026-09-12', 'Example street 1', False),
                ('Hjemlevering mellom kl 13 og 18, i morgen', '2027-09-12', 'Example street 1', False),
                ('Hjemlevering mellom kl 13 og 18, i morgen', '2026-09-12', 'Other street', False)]:
            with self.subTest(display=display, date=date_value, address=address):
                flow = recovery_fixtures.OdaAdditionPaymentTests()
                flow.setUp()
                try:
                    flow.app._now = lambda: datetime(2026, 9, 11, 20, tzinfo=timezone.utc)
                    flow.cart.update(deliverySlot={'id': 123, 'name': display}, deliveryAddress=address)
                    flow.original['deliverySlotDisplay'] = 'Lør 12. sep 13:00 - 18:00'
                    flow.merchant.order['deliverySlotDisplay'] = flow.original['deliverySlotDisplay']
                    flow.original['deliveryDate'] = date_value
                    flow.merchant.order['deliveryDate'] = date_value
                    with flow.app.store.locked() as state:
                        state['order_change']['before']['order'] = deepcopy(flow.original)
                    if accepted:
                        self.assertIn('confirmation_id', flow.call('prepare'))
                    else:
                        with self.assertRaises(HouseholdError):
                            flow.call('prepare')
                    self.assertEqual(flow.browser.clicks, 0)
                finally:
                    flow.doCleanups()


class VippsSeededObserverTests(unittest.TestCase):
    def observe(self, *, status='PENDING', read_http=200, mode='cancel'):
        import json
        import shutil
        import subprocess
        from oda_payment_switch import _VIPPS_OBSERVER_SCRIPT
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for native payment observation')
        fixture = r'''
import vm from 'node:vm';
const test=TEST;
let reads=0,clicks=0,socket;
const source='https://pay.vipps.no/?token=synthetic';
const pollUrl='https://api.vipps.no/vipps-epayment-legacy-mobile-api/landing-page';
const document={querySelectorAll:()=>[],elementFromPoint:()=>null};
const fetch=async(url,options)=>{reads++;
 if(url!==pollUrl||options.headers.Authorization!=='Bearer synthetic')throw Error('Wrong native request');
 return {status:test.read_http,headers:{get:()=> 'application/json'},json:async()=>({status:test.status})};
};
globalThis.WebSocket=class {
 constructor(){socket=this;this.listeners={};queueMicrotask(()=>this.fire('open',{}))}
 addEventListener(k,f){(this.listeners[k]??=[]).push(f)}
 fire(k,v){for(const f of this.listeners[k]||[])f(v)}
 async send(raw){const m=JSON.parse(raw);let result={};
  if(m.method==='Target.getTargets')result={targetInfos:[{type:'page',url:source,targetId:'owned'}]};
  if(m.method==='Target.createTarget')result={targetId:'reader'};
  if(m.method==='Target.attachToTarget')result={sessionId:m.params.targetId==='reader'?'reader-session':'session'};
  if(m.method==='Runtime.evaluate')result={result:{value:await vm.runInNewContext(m.params.expression,
   {document,location:m.sessionId==='reader-session'?{origin:'https://pay.vipps.no'}:
    {origin:'https://pay.vipps.no',href:source},URL,AbortSignal,fetch,getComputedStyle:()=>({})})}};
  queueMicrotask(()=>this.fire('message',{data:JSON.stringify({id:m.id,result})}));
 }
 close(){}
};
process.on('exit',()=>process.stderr.write(JSON.stringify({reads,clicks})));
'''.replace('TEST', json.dumps({'status': status, 'read_http': read_http}))
        cfg = {'endpoint': 'ws://localhost/synthetic', 'url': 'https://pay.vipps.no/?token=synthetic',
               'mode': mode, 'poll_path': '/vipps-epayment-legacy-mobile-api/landing-page',
               'observe_ms': 25, 'prior_attempted': False,
               'poll_seed': {'url': 'https://api.vipps.no/vipps-epayment-legacy-mobile-api/landing-page',
                             'payment_id': '456', 'headers': {'Authorization': 'Bearer synthetic'}}}
        process = subprocess.run([node, '--input-type=module', '-e', fixture + _VIPPS_OBSERVER_SCRIPT],
                                 input=json.dumps(cfg) + '\n', text=True, capture_output=True, timeout=10)
        self.assertNotIn('Bearer synthetic', process.stdout)
        self.assertNotIn('token=synthetic', process.stdout)
        return json.loads(process.stdout.splitlines()[-1]), json.loads(process.stderr)

    def test_expired_surface_requires_fresh_native_terminal_not_new_poll_or_cancel(self):
        terminal, metrics = self.observe(status='TIMEOUT')
        self.assertEqual((terminal['status'], terminal['terminal_status']), ('closed', 'TIMEOUT'))
        self.assertEqual(metrics, {'reads': 1, 'clicks': 0})
        for status, read_http in [('PENDING', 200), ('SUBMITTED', 200), ('TIMEOUT', 401)]:
            with self.subTest(status=status, http=read_http):
                unresolved, metrics = self.observe(status=status, read_http=read_http)
                self.assertEqual(unresolved['status'], 'unknown')
                self.assertEqual(metrics, {'reads': 1, 'clicks': 0})

    def test_fresh_native_acceptance_prevents_cancel_even_on_expired_surface(self):
        accepted, metrics = self.observe(status='ACCEPTED')
        self.assertEqual((accepted['status'], accepted['terminal_status']), ('paid', 'ACCEPTED'))
        self.assertEqual(metrics, {'reads': 1, 'clicks': 0})
        observed, metrics = self.observe(status='TIMEOUT', mode='state')
        self.assertEqual((observed['status'], observed['terminal_status']), ('closed', 'TIMEOUT'))
        self.assertEqual(metrics, {'reads': 1, 'clicks': 0})

    def test_gateway_expiry_needs_native_terminal_proof(self):
        import hashlib
        import time
        from contextlib import nullcontext
        from unittest import mock
        from oda_browser import OdaBrowser
        source = 'https://pay.vipps.no/?token=synthetic'
        context = {'tab_id': 'owned', 'expected_total': 234567,
                   'gateway_url_digest': hashlib.sha256(source.encode()).hexdigest(),
                   'order_id': 'order-1'}
        class Browser:
            vipps_phone_number = '12345678'
            _checkout_deadline = time.monotonic() + 30
            _checkout_operation = lambda self, *args, **kwargs: nullcontext()
            _select_payment_tab = lambda self, tab_id: tab_id == 'owned'
            _invoke = lambda self, *args: {'url': source}
            _eval = lambda self, script: {'identity': True, 'expired': True}
        browser = Browser()
        with mock.patch('oda_payment_switch.native_vipps_request_state', return_value={'status': 'unknown'}):
            self.assertEqual(OdaBrowser.checkout_vipps_request_state(browser, context), {'status': 'unknown'})
        with mock.patch('oda_payment_switch.native_vipps_request_state',
                        return_value={'status': 'expired', 'terminal_status': 'TIMEOUT'}):
            self.assertEqual(OdaBrowser.checkout_vipps_request_state(browser, context)['status'], 'expired')
        with mock.patch('oda_payment_switch.native_vipps_request_state',
                        return_value={'status': 'sent', 'terminal_status': 'ACCEPTED'}):
            self.assertEqual(OdaBrowser.checkout_vipps_request_state(browser, context)['status'], 'sent')


if __name__ == '__main__':
    unittest.main()
