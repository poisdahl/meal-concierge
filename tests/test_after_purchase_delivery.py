"""Confirmed purchase snapshots through the real managed send/receipt path."""
from copy import deepcopy
from email import policy
from email.parser import BytesParser
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError
from email_sender import EmailSender
from menu_planning import menu_ref
from menu_planning import digest
from test_email_sender import Mailbox
from test_recipe_delivery import fixture, chat_cap, DEST


class OrderProvider:
    def __init__(self):
        self.status = 'paid_and_modifiable'

    def call(self, tool, arguments, **kwargs):
        order = arguments['order_number']
        if tool == 'order_tracking':
            return {'order_id': order, 'status': self.status}
        if tool == 'get_order':
            return {'order_number': order, 'delivery_date': '2099-10-02'}
        raise AssertionError('Delivery must not purchase or modify groceries')


class AfterPurchaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app, self.menu, _ = fixture(self.root)
        self.provider = OrderProvider()
        self.app.email_provider_clients['oda'] = self.provider
        self.mailbox = Mailbox()
        self.config = {'runtime_id': 'test-host', 'receipt_dir': str(self.root / 'receipts'),
                       'connections': [{'id': 'gmail', 'type': 'command', 'unattended': True}]}
        self.runner = EmailSender(self.rpc, self.config, transport_factory=lambda c: self.mailbox)
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'after_purchase'})
        # Existing explicit email PDF preference is preserved; new default is owned by core.
        self.rpc('recipe_delivery', action='configure', changes={'email': {'pdf': False}})

    def rpc(self, operation, **request):
        return self.app.handle({'operation': operation, **request})

    def purchase(self, order='order-1', menu_bound=True):
        pending = {'menu': deepcopy(self.menu), 'cart_plan': {}, 'summary': {
            'menu_attribution': 'menu_bound' if menu_bound else 'cart_only',
            'delivery': {'date': '2099-10-02'}, 'items': []}}
        with self.app.store.locked() as state:
            self.app._record_order_snapshot(state, pending, order)
            return self.app._purchase_delivery(state, order)

    def send(self, action='send_order'):
        return self.runner.execute({'action': action, 'provider': 'oda', 'order_id': 'order-1'})

    def test_configure_defaults_new_binding_to_purchase_and_preserves_existing_timing(self):
        with self.app.store.locked() as state:
            state['recipe_delivery'].pop('sender_binding', None)
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test'})
        self.assertEqual('after_purchase', self.rpc('recipe_delivery', action='sender')['binding']['timing'])
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'on_request'})
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test'})
        self.assertEqual('on_request', self.rpc('recipe_delivery', action='sender')['binding']['timing'])

    def test_interactive_setup_defaults_to_request_and_accepts_explicit_purchase_timing(self):
        self.config['connections'][0]['unattended'] = False
        with self.app.store.locked() as state:
            state['recipe_delivery'].pop('sender_binding', None)
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test'})
        self.assertEqual('on_request', self.rpc('recipe_delivery', action='sender')['binding']['timing'])
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'after_purchase'})
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test'})
        self.assertEqual('after_purchase', self.rpc('recipe_delivery', action='sender')['binding']['timing'])
        for timing in ('delivery_day', 'both'):
            with self.assertRaisesRegex(HouseholdError, 'unattended'):
                self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': timing})
        self.assertEqual([], self.mailbox.messages)

    def test_interactive_purchase_waits_for_transport_then_sends_original_once(self):
        self.config['connections'][0]['unattended'] = False
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'after_purchase'})
        offered = self.purchase()
        self.assertEqual('send_order', offered['email']['action'])
        original = deepcopy(self.app.store.read()['email_jobs'])
        with mock.patch.object(self.mailbox, 'inspect', side_effect=ValueError('existing connection needs user action')):
            with self.assertRaisesRegex(ValueError, 'needs user action'):
                self.send()
        self.assertEqual(original, self.app.store.read()['email_jobs'])
        self.assertEqual([], self.mailbox.messages)
        self.mailbox.failure = 'after'
        self.assertEqual('unknown', self.send()['outcome'])
        self.assertTrue(self.send(action='reconcile_order')['sent'])
        self.assertTrue(self.send()['sent'])
        self.assertEqual(1, len(self.mailbox.messages))
        self.assertEqual(1, len(self.app.store.read()['email_jobs']))

    def test_delivery_day_still_requires_original_and_current_unattended_support(self):
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'both'})
        self.purchase()
        self.rpc('email', action='schedule', provider='oda', order_id='order-1', delivery_date='2099-10-02')
        self.config['connections'][0]['unattended'] = False
        with self.assertRaisesRegex(ValueError, 'unattended'):
            self.send()
        self.assertEqual([], self.mailbox.messages)

    def test_future_delivery_sends_now_once_without_scheduler_or_duplicate_pdf(self):
        offered = self.purchase()
        self.assertEqual('send_order', offered['email']['action'])
        self.assertEqual([], self.mailbox.messages)
        self.purchase()  # reconciliation replay cannot add another occurrence
        self.assertEqual(1, len(self.app.store.read()['email_jobs']))
        self.assertEqual([], self.rpc('email', action='automation_plan')['updates'])
        self.assertTrue(self.send()['sent'])
        self.assertTrue(self.send()['sent'])
        self.assertEqual(1, len(self.mailbox.messages))
        message = BytesParser(policy=policy.default).parsebytes(self.mailbox.messages[0])
        self.assertIn('levering 2. oktober 2099', str(message['Subject']))
        self.assertNotIn('Ukesmeny', str(message['Subject']))
        self.assertFalse(any(p.get_content_type() == 'application/pdf' for p in message.walk()))
        self.assertIn(self.menu['dishes'][0]['name'], str(message.get_body(('html',)).get_content()))

    def test_enabling_purchase_delivery_does_not_retroactively_queue_reconciled_order(self):
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'on_request'})
        self.purchase()
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'after_purchase'})
        self.purchase()
        self.assertEqual([], self.app.store.read()['email_jobs'])

    def test_legacy_manual_request_keeps_original_intent_identity(self):
        self.rpc('recipe_delivery', action='disable', channel='email')
        intent = {'menu_ref': menu_ref(self.menu), 'destinations': {'chat': DEST['chat']},
                  'capabilities': {'chat': chat_cap()}}
        result = self.rpc('recipe_delivery', action='request', request_id='legacy-manual',
                          delivery_requested=True, **intent)
        with self.app.store.locked() as state:
            state['recipe_delivery']['jobs']['legacy-manual']['intent_digest'] = digest(intent)
        replay = self.rpc('recipe_delivery', action='request', request_id='legacy-manual',
                          delivery_requested=True, provider=None, order_id=None, **intent)
        self.assertEqual(result, replay)

    def test_grocery_only_and_unrequested_timing_do_not_enqueue(self):
        self.assertIsNone(self.purchase(menu_bound=False))
        self.assertEqual([], self.app.store.read()['email_jobs'])
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'on_request'})
        self.purchase()
        self.assertEqual([], self.app.store.read()['email_jobs'])

    def test_order_chat_reads_frozen_purchase_after_current_menu_changes(self):
        offered = self.purchase()
        with self.app.store.locked() as state:
            state['menu'] = None
        self.assertTrue(self.send()['sent'])
        result = self.rpc('recipe_delivery', action='request', request_id=offered['request_id'],
                          delivery_requested=True, provider='oda', order_id='order-1', channel='chat',
                          destinations={'chat': DEST['chat']}, capabilities={'chat': chat_cap()})
        self.assertEqual(menu_ref(self.menu), result['menu_ref'])
        self.assertEqual('purchased_menu', result['purpose'])
        self.assertTrue(any(p['kind'] == 'pdf' for p in result['parts']))
        with self.assertRaises(HouseholdError):
            self.rpc('recipe_delivery', action='request', request_id='wrong-provider', delivery_requested=True,
                     provider='meny', order_id='order-1', channel='chat',
                     destinations={'chat': DEST['chat']}, capabilities={'chat': chat_cap()})

    def test_unconfirmed_or_paused_order_does_not_dispatch(self):
        self.purchase()
        self.provider.status = 'unpaid_order'
        self.assertFalse(self.send()['send'])
        self.assertEqual([], self.mailbox.messages)
        self.provider.status = 'paid_and_modifiable'
        self.rpc('recipe_delivery', action='pause')
        self.assertFalse(self.send()['send'])
        self.assertEqual([], self.mailbox.messages)

    def test_lost_sender_response_reconciles_without_resend(self):
        self.purchase()
        self.mailbox.failure = 'after'
        first = self.send()
        self.assertEqual('unknown', first['outcome'])
        self.assertEqual(1, len(self.mailbox.messages))
        self.assertTrue(self.send(action='reconcile_order')['sent'])
        self.assertEqual(1, len(self.mailbox.messages))

    def test_migration_requires_exact_removed_scheduler_and_keeps_original_job(self):
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'both'})
        self.purchase()
        self.rpc('email', action='schedule', provider='oda', order_id='order-1', delivery_date='2099-10-02')
        binding = {'platform': 'synthetic-timer', 'scope': 'private-fixture', 'job_id': 'job-1'}
        planned = self.rpc('email', action='scheduler_plan', provider='oda', order_id='order-1', scheduler={
            'binding': binding, 'inventory': {'platform': 'synthetic-timer', 'scope': 'private-fixture',
                                             'verified': True, 'matching_jobs': 0}})
        scheduler = planned['scheduler']
        removal = {'binding': binding, 'generation': scheduler['generation'], 'previous_binding': None,
                   'state': 'removed', 'verified': True}
        with self.assertRaises(HouseholdError):
            self.rpc('email', action='migrate_after_purchase', provider='oda', order_id='order-1',
                     delivery_requested=True, scheduler={**removal, 'verified': False})
        self.rpc('email', action='migrate_after_purchase', provider='oda', order_id='order-1',
                 delivery_requested=True, scheduler=removal)
        self.assertEqual(1, len(self.app.store.read()['email_jobs']))
        self.assertTrue(self.send()['sent'])
        self.assertEqual(1, len(self.mailbox.messages))

    def test_existing_day_job_is_not_retimed_or_duplicated(self):
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'both'})
        self.purchase()
        self.rpc('email', action='schedule', provider='oda', order_id='order-1', delivery_date='2099-10-02')
        original = deepcopy(self.app.store.read()['email_jobs'])
        self.runner.execute({'action': 'configure', 'recipient': 'recipient@example.test', 'timing': 'after_purchase'})
        self.purchase()
        self.assertEqual(original, self.app.store.read()['email_jobs'])

    def test_cancelled_purchase_does_not_send_recipes(self):
        offered = self.purchase()
        self.provider.status = 'cancelled'
        self.assertFalse(self.send()['send'])
        self.assertEqual([], self.mailbox.messages)
        with self.assertRaises(HouseholdError):
            self.rpc('recipe_delivery', action='request', request_id=offered['request_id'],
                     delivery_requested=True, provider='oda', order_id='order-1', channel='chat',
                     destinations={'chat': DEST['chat']}, capabilities={'chat': chat_cap()})

    def test_elapsed_recurring_replay_and_cancellation_recompute_confirmed_history(self):
        state = self.app.store.read()
        schedule = {'unit': 'days', 'every': 21, 'anchor': '2026-09-01'}
        state['recurring_items'] = [{'product_id': '123', 'schedule': deepcopy(schedule)}]
        pending = {'cart_plan': {'recurring_items': [{'product_id': '456', 'original_product_id': '123',
                    'quantity': 1, 'fulfillment_key': 'due-1', 'schedule': deepcopy(schedule)}]},
                   'summary': {'menu_attribution': 'cart_only', 'items': [{'product_id': '456', 'name': 'Replacement', 'quantity': 1}]}}
        self.app._record_order_snapshot(state, pending, 'order-first')
        fulfilled = deepcopy(state['recurring_fulfilled']['due-1'])
        self.app._record_order_snapshot(state, pending, 'order-first')
        self.assertEqual(fulfilled, state['recurring_fulfilled']['due-1'])
        self.assertEqual(fulfilled['fulfilled_on'], state['recurring_items'][0]['last_fulfilled_on'])
        pending['cart_plan']['recurring_items'][0]['fulfillment_key'] = 'due-2'
        self.app._record_order_snapshot(state, pending, 'order-second')
        self.app._mark_order_cancelled(state, 'order-first', provider='oda', active_provider='oda')
        self.assertEqual(fulfilled['fulfilled_on'], state['recurring_items'][0]['last_fulfilled_on'])
        self.app._mark_order_cancelled(state, 'order-second', provider='oda', active_provider='oda')
        self.assertNotIn('last_fulfilled_on', state['recurring_items'][0])

    def test_after_purchase_cannot_be_given_duplicate_day_schedule(self):
        self.purchase()
        with self.assertRaises(HouseholdError):
            self.rpc('email', action='schedule', provider='oda', order_id='order-1', delivery_date='2099-10-02')


if __name__ == '__main__':
    unittest.main()
