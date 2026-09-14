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
            self.target = {'order_id': order_id, 'order_change_id': 'change-1',
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
            self.assertEqual(addition['order_change_id'], 'change-1')
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
        with self.assertRaisesRegex(HouseholdError, 'only an existing saved card'):
            self.call('prepare', recovery=True, checkout_payment={'method': 'vipps'})
        self.assertEqual((self.cancel_clicks, self.target_reads, self.card_clicks), (1, 1, 0))

    def test_target_change_before_card_dispatch_blocks_payment(self):
        prepared = self.switch()
        self.target_invalid = True
        with self.assertRaisesRegex(HouseholdError, 'target changed'):
            self.call('confirm', confirmation_id=prepared['confirmation_id'])
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
        with self.assertRaisesRegex(HouseholdError, 'current Vipps'):
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


if __name__ == '__main__':
    unittest.main()
