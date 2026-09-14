"""Native removal lifecycle against synthetic storefront observations, no live writes."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[3] / 'scripts/tests')]
from core import HouseholdError, StateStore
from service import Application
from order_reductions import _intent, _ui_script
from test_meal_concierge import FakeOda, FakeBrowser, CONFIG


def source():
    return {
        'ok': True,
        'eligibility': {'order_number': 'test123', 'currency': 'NOK', 'total_uncredited_quantity': 4,
            'cutoff_text': 'Before cutoff', 'bonus_info': {'threshold_reached': False},
            'items_groups': [{'name': 'Groceries', 'items': [
                {'product_id': 27667, 'description': 'Nektariner Spania/ Italia, 1 kg', 'quantity': 2,
                 'uncredited_quantity': 2, 'gross_amount': 89.6, 'currency': 'NOK',
                 'eligible_for_removal': True, 'entire_quantity_removal_only': False},
                {'product_id': 99, 'description': 'Pærer', 'quantity': 2, 'uncredited_quantity': 2,
                 'gross_amount': 60, 'currency': 'NOK', 'eligible_for_removal': True,
                 'entire_quantity_removal_only': False}]}]},
        'details': {'summary': {'order_number': 'test123', 'currency': 'NOK', 'gross_amount': 198.6,
                    'status': {'can_remove_from_order': True, 'payment_status_state': 'payment_authorized'},
                    'delivery': {'delivery_address': 'Synthetic only', 'delivery_time': 'Friday'}},
                    'options': {'can_remove_from_order': True}, 'items': {'product_count': 4}}}


class Browser(FakeBrowser):
    def __init__(self):
        super().__init__()
        self.source = source()
        self.final_clicks = 0
        self.stage = None
        self.fail_after_click = False
        self.apply_click = True
        self.local_selection = {}
        self.last_items = []
        self.read_hook = None
        self.session_closes = 0

    def _checkout_operation(self, *args, **kwargs):
        if not kwargs.get('preserve_session'):
            self.session_closes += 1
        return nullcontext()

    def _open(self, url):
        self.url = url
        self.local_selection = {}

    def _settle(self, seconds):
        pass

    def _eval(self, script):
        if 'order-removal-source' in script:
            if self.read_hook:
                self.read_hook()
            return deepcopy(self.source)
        self.stage = json.loads(re.search(r', stage = (.*?), productId =', script).group(1))
        self.last_items = json.loads(re.search(r', items = (.*?), stage =', script).group(1))
        return {'ok': True}

    def _invoke(self, *args):
        if args[0] != 'click' or self.stage != 'confirm':
            return {}
        self.final_clicks += 1
        if self.apply_click:
            rows = self.source['eligibility']['items_groups'][0]['items']
            for intent in self.last_items:
                if intent.get('requested') is False:
                    continue
                row = next(r for r in rows if str(r['product_id']) == intent['product_id'])
                row['uncredited_quantity'] = intent['quantity']
                self.source['eligibility']['total_uncredited_quantity'] -= intent['removed_quantity']
                self.source['details']['summary']['gross_amount'] = round(
                    self.source['details']['summary']['gross_amount'] - intent['credit_ore']/100, 2)
        if self.fail_after_click:
            raise HouseholdError('synthetic transport lost after click')
        return {}


class OrderRemovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='meal-removal-')
        self.addCleanup(temp.cleanup)
        self.store = StateStore(Path(temp.name), CONFIG)
        self.provider = FakeOda()
        self.provider.orders = [{'orderNumber': 'test123', 'currency': 'NOK'}]
        self.browser = Browser()
        self.app = Application(self.store, self.provider, self.browser)
        self.cart_before = deepcopy(self.provider.cart)
        with self.store.locked() as state:
            state['menu'] = None
            state['order_snapshots']['test123'] = {'unchanged': 'recipe snapshot'}

    def call(self, action, **kwargs):
        return self.app.handle({'operation': 'orders', 'action': action, **kwargs})

    def prepare(self, quantity=0, **kwargs):
        return self.call('remove_prepare', order_id='test123', idempotency_key='remove-test',
                         items=[{'product_id': '27667', 'quantity': quantity}], **kwargs)

    def test_prepare_confirm_replay_preserves_cart_and_recipe_snapshot(self):
        prepared = self.prepare(1)
        self.assertEqual(prepared['expected_credit_ore'], 4480)
        self.assertEqual(self.browser.final_clicks, 0)
        self.assertEqual(self.prepare(1), prepared)
        result = self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])
        self.assertTrue(result['removed'])
        self.assertEqual(result['new_total_ore'], 15380)
        self.assertEqual(result['payment_resolution']['refund'], 'unknown')
        self.assertTrue(self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])['idempotent'])
        self.assertTrue(self.prepare(1)['idempotent'])
        self.assertEqual(self.browser.final_clicks, 1)
        self.assertEqual(self.provider.cart, self.cart_before)
        self.assertEqual(self.store.read()['order_snapshots']['test123'], {'unchanged': 'recipe snapshot'})
        self.assertIsNone(self.store.read()['order_change'])

    def test_zero_means_remove_this_product_not_cancel_order(self):
        prepared = self.prepare(0)
        result = self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])
        self.assertEqual(result['credit_ore'], 8960)
        self.assertEqual(self.browser.source['eligibility']['total_uncredited_quantity'], 2)

    def test_uncertain_click_is_reconciled_without_dispatching_again(self):
        prepared = self.prepare(1)
        self.browser.fail_after_click = True
        with self.assertRaisesRegex(HouseholdError, 'uncertain'):
            self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])
        self.assertEqual(self.store.read()['order_change']['status'], 'uncertain')
        result = self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])
        self.assertTrue(result['removed'])
        self.assertEqual(self.browser.final_clicks, 1)

    def test_unapplied_uncertain_click_remains_protected(self):
        prepared = self.prepare(1)
        self.browser.apply_click = False
        result = self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])
        self.assertTrue(result['reconciliation_required'])
        self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])
        self.call('remove_reconcile', confirmation_id=prepared['confirmation_id'])
        self.assertEqual(self.browser.final_clicks, 1)
        with self.assertRaisesRegex(HouseholdError, 'outcome may be uncertain'):
            self.call('change_abort', order_id='test123')

    def test_prepared_or_interrupted_preparing_abort_preserves_nonempty_cart(self):
        for status in ('prepared', 'preparing'):
            with self.subTest(status=status):
                self.prepare(1)
                with self.store.locked() as state:
                    state['order_change']['status'] = status
                result = self.call('change_abort', order_id='test123')
                self.assertTrue(result['aborted'])
                self.assertTrue(result['cart_retained'])
                self.assertEqual(self.provider.cart, self.cart_before)
                self.assertEqual(self.browser.final_clicks, 0)
                self.assertIsNone(self.store.read()['order_change'])

    def test_active_removal_blocks_cart_writes_and_checkout(self):
        self.prepare(1)
        for request in ({'operation': 'cart', 'action': 'ensure', 'requirements': [
                            {'product_id': '10', 'quantity': 2}]},
                        {'operation': 'checkout', 'action': 'prepare'}):
            with self.subTest(request=request), self.assertRaises(HouseholdError):
                self.app.handle(request)
        self.assertFalse(any(name == 'manipulate_cart' for name, _ in self.provider.calls))
        self.assertEqual(self.browser.final_clicks, 0)
        self.assertEqual(self.provider.cart, self.cart_before)

    def test_stale_order_cart_or_eligibility_never_clicks(self):
        for change in ('cart', 'price', 'quantity', 'delivery', 'eligibility'):
            with self.subTest(change=change):
                self.setUp()
                prepared = self.prepare(1)
                if change == 'cart':
                    self.provider.cart['subtotal'] += 1
                elif change == 'price':
                    self.browser.source['details']['summary']['gross_amount'] += 1
                elif change == 'quantity':
                    self.browser.source['eligibility']['items_groups'][0]['items'][0]['uncredited_quantity'] -= 1
                    self.browser.source['eligibility']['total_uncredited_quantity'] -= 1
                elif change == 'delivery':
                    self.browser.source['details']['summary']['delivery']['delivery_time'] = 'Saturday'
                else:
                    self.browser.source['eligibility']['items_groups'][0]['items'][0]['eligible_for_removal'] = False
                with self.assertRaisesRegex(HouseholdError, 'changed'):
                    self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])
                self.assertEqual(self.browser.final_clicks, 0)

    def test_foreign_order_duplicate_increase_and_remove_all_rejected(self):
        eligibility = source()['eligibility']
        for items in ([{'product_id': '27667', 'quantity': q}] for q in (-1, 3, True)):
            with self.assertRaises(HouseholdError):
                _intent(items, eligibility)
        for items in ([{'product_id': 'unknown', 'quantity': 0}],
                      [{'product_id': '27667', 'quantity': 0}]*2,
                      [{'product_id': '27667', 'quantity': 0}, {'product_id': '99', 'quantity': 0}]):
            with self.assertRaises(HouseholdError):
                _intent(items, eligibility)
        self.browser.source['eligibility']['order_number'] = 'other'
        with self.assertRaisesRegex(HouseholdError, 'identity'):
            self.prepare()
        self.assertIsNone(self.store.read()['order_change'])

    def test_equal_quantity_is_already_done_and_replay_binds_items_and_order(self):
        result = self.prepare(2)
        self.assertTrue(result['already_done'])
        self.assertFalse(result['confirmation_required'])
        self.assertEqual(self.browser.final_clicks, 0)
        with self.assertRaisesRegex(HouseholdError, 'different removal items'):
            self.prepare(0)
        with self.assertRaisesRegex(HouseholdError, 'order_id changed'):
            self.call('remove_confirm', confirmation_id=result['confirmation_id'], order_id='other')

    def test_completed_removal_reconciles_despite_later_cart_change(self):
        prepared = self.prepare(1)
        self.browser.fail_after_click = True
        with self.assertRaises(HouseholdError):
            self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])
        self.provider.cart['subtotal'] += 1
        result = self.call('remove_reconcile', confirmation_id=prepared['confirmation_id'])
        self.assertTrue(result['removed'])
        self.assertFalse(result['cart_unchanged'])
        self.assertEqual(self.browser.final_clicks, 1)

    def test_pending_checkout_and_full_quantity_only(self):
        with self.store.locked() as state:
            state['pending_checkout'] = {'status': 'uncertain'}
        with self.assertRaisesRegex(HouseholdError, 'pending checkout'):
            self.prepare()
        self.assertEqual(self.browser.session_closes, 0)
        with self.store.locked() as state:
            state['pending_checkout'] = None
        self.browser.source['eligibility']['items_groups'][0]['items'][0]['entire_quantity_removal_only'] = True
        with self.assertRaisesRegex(HouseholdError, 'entire quantity'):
            self.prepare(1)
        self.assertEqual(self.browser.final_clicks, 0)

    def test_wrong_credit_is_not_reported_as_completed(self):
        prepared = self.prepare(1)
        original = self.browser._invoke
        def altered(*args):
            result = original(*args)
            if self.browser.stage == 'confirm':
                self.browser.source['details']['summary']['gross_amount'] += 1
            return result
        self.browser._invoke = altered
        result = self.call('remove_confirm', confirmation_id=prepared['confirmation_id'])
        self.assertTrue(result['reconciliation_required'])
        self.assertFalse(result['removed'])

    @unittest.skipUnless(shutil.which('node'), 'Node is required for native DOM control regression')
    def test_dom_final_requires_visible_exact_dialog_and_selected_quantity(self):
        items = _intent([{'product_id': '27667', 'quantity': 1}], source()['eligibility'])
        script = _ui_script('test123', items, 'confirm')
        harness = r'''
const vm=require('vm');
function run({visibleDialog=true,quantity='1',wrongText=false,duplicate=false,wrongUrl=false,disabled=false,extra=false,wrongTotal=false,wrongPrice=false}={}) {
 const button={textContent:'Fjern varer',disabled,getClientRects:()=>[{}],getAttribute:()=>null,setAttribute(){this.tagged=true},removeAttribute(){}};
 const box={...button,textContent:quantity};
 const heading={...button,textContent:'Nektariner Spania/ Italia, 1 kg'};
 const article={...button,innerText:'Nektariner Spania/ Italia, 1 kg '+(wrongPrice?'43,80':'44,80')+' kr',querySelectorAll:s=>s==='h1'?[heading]:[box]};
 const dialog={...button,innerText:wrongText?'Cancel order': 'Er du sikker på at du vil fjerne disse varene fra bestillingen din? Prisen på de fjernede varene blir trukket fra totalen nederst på bestillingen.',getClientRects:()=>visibleDialog?[{}]:[],querySelectorAll:()=>[button]};
 const main={...button,innerText:'Totalt fjernet, inkl. MVA -'+(wrongTotal?'43,80':'44,80')+' kr'};
 const document={querySelectorAll:s=>s==='article'?[article]:s==='main'?[main]:s==='[role="combobox"][id="quantity-to-credit"]'?(extra?[box,box]:[box]):s==='[role="dialog"]'?(duplicate?[dialog,dialog]:[dialog]):[]};
 const location={origin:'https://oda.com',pathname:wrongUrl?'/wrong/':'/no/account/orders/remove-items/test123/'};
 return JSON.parse(vm.runInNewContext(SCRIPT,{document,location,getComputedStyle:()=>({visibility:'visible'})})).ok;
}
process.stdout.write(JSON.stringify([run(),run({visibleDialog:false}),run({quantity:'2'}),run({wrongText:true}),run({duplicate:true}),run({wrongUrl:true}),run({disabled:true}),run({extra:true}),run({wrongTotal:true}),run({wrongPrice:true})]));
'''.replace('SCRIPT', json.dumps(script))
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [True, False, False, False, False, False, False, False, False, False])


if __name__ == '__main__':
    unittest.main()
