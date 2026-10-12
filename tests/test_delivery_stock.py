"""Delivery-page stock explanation, distinct from catalog purchasability."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_meal_concierge as fixtures
from test_payment_setup import PAYMENT_DOM
from core import HouseholdError
from oda_browser import OdaBrowser, CART_URL, DELIVERY_URL, _oda_delivery_restrictions_script

WARNING = 'Noen av varene i handlekurven, f.eks. Håndsåpe refill, er ikke tilgjengelige før 18. oktober 2026.'
SNAPSHOT = {'items': [('10', 1)], 'address': {'id': 1, 'address': 'Eksempelveien 1'}}


class DeliveryStockTests(unittest.TestCase):
    def test_real_browser_reader_waits_on_delivery_page_and_restores_original_tab(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node executes the real browser scripts')
        browser = object.__new__(OdaBrowser)
        browser._verify_checkout_account = mock.Mock(return_value=hashlib.sha256(b'1').hexdigest())
        browser._settle = mock.Mock()
        browser._cart_surface = mock.Mock(return_value={"action": "continue"})
        events = []
        @contextmanager
        def inspection():
            events.append('inspection')
            try:
                yield
            finally:
                events.append('original restored')
        browser._inspection_tab = inspection
        current = None
        reads = 0
        def opened(url):
            nonlocal current
            current = url
            events.append(url)
        browser._open = opened
        def evaluate(script):
            nonlocal reads
            if current == DELIVERY_URL:
                reads += 1
            # Cart has no banner; the correct page hydrates after one empty read.
            case = {'url': current, 'nativeRow': True, 'outsideRows': [[WARNING]] if current == DELIVERY_URL and reads > 1 else []}
            run = subprocess.run([node, '-e', PAYMENT_DOM], input=json.dumps({'script': script, 'c': case}), text=True, capture_output=True, check=True, timeout=10)
            value = json.loads(run.stdout)
            self.assertEqual(value['clicks'], [])
            return value['result']
        browser._eval = evaluate
        self.assertEqual(browser.cart_delivery_restrictions(SNAPSHOT), {"cart_restrictions": [WARNING]})
        self.assertEqual(events, ['inspection', CART_URL, DELIVERY_URL, 'original restored'])
        self.assertEqual(reads, 2)
        browser._settle.assert_called_once_with(0.25)

    def test_unready_or_wrong_cart_never_becomes_no_restrictions(self):
        for case in ('loading', 'wrong_cart', 'wrong_account'):
            with self.subTest(case=case):
                browser = object.__new__(OdaBrowser)
                browser._verify_checkout_account = mock.Mock(return_value=hashlib.sha256(b'2' if case == 'wrong_account' else b'1').hexdigest())
                browser._settle = mock.Mock()
                browser._cart_surface = mock.Mock(return_value={"action": "continue"})
                restored = []
                @contextmanager
                def tab():
                    try:
                        yield
                    finally:
                        restored.append(True)
                browser._inspection_tab = tab
                browser._open = mock.Mock()
                browser._eval = mock.Mock(side_effect=lambda script: {'url': CART_URL, 'authenticated': True, 'items': [{'product_id': '11' if case == 'wrong_cart' else '10', 'quantity': 1}]} if 'ROW_IDENTITY' not in script and 'total_matches' in script else {'ready': False, 'warnings': []})
                with self.assertRaises(HouseholdError):
                    browser.cart_delivery_restrictions(SNAPSHOT)
                self.assertEqual(restored, [True])
                self.assertLessEqual(browser._eval.call_count, 21)
                if case == 'wrong_account':
                    browser._open.assert_not_called()

    def test_banner_requires_exact_page_and_visible_current_evidence(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required')
        for page, text, expected in [(DELIVERY_URL, WARNING, True), (CART_URL, WARNING, False),
                                     (DELIVERY_URL, '', False), (DELIVERY_URL, 'Tidligere historikk: '+WARNING, False)]:
            run = subprocess.run([node, '-e', PAYMENT_DOM], input=json.dumps({'script': _oda_delivery_restrictions_script(), 'c': {'url': page, 'outsideRows': [[text]]}}), text=True, capture_output=True, check=True, timeout=10)
            result = json.loads(run.stdout)
            self.assertEqual(result['result']['ready'], expected)
            self.assertEqual(result['clicks'], [])

    def test_large_delivery_page_does_not_measure_unrelated_card_visibility(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required')
        instrumented = PAYMENT_DOM.replace(
            "global.getComputedStyle=e=>({display:e.hidden?'none':'block',visibility:'visible'});",
            """if(c.hideNotice)outside.at(-1).hidden=true;
let styleReads=0;
global.getComputedStyle=e=>{
 if(e.innerText.startsWith('Unrelated card'))throw Error('unrelated card layout read');
 styleReads++;return {display:e.hidden?'none':'block',visibility:'visible'};
};""").replace('clicks,selected:', 'clicks,styleReads,selected:')
        cards = [[f'Unrelated card {i}', 'Normal product information'] for i in range(1000)]
        for present, hidden in ((True, False), (True, True), (False, False)):
            with self.subTest(present=present, hidden=hidden):
                # Split markup remains matched from rendered innerText, while
                # nested duplicate containers and hidden ancestors stay excluded.
                rows = cards + ([['Noen av varene i handlekurven, f.eks.',
                                  'Håndsåpe refill, er ikke tilgjengelige før 18. oktober 2026.']] if present else [])
                run = subprocess.run([node, '-e', instrumented],
                    input=json.dumps({'script': _oda_delivery_restrictions_script(),
                        'c': {'url': DELIVERY_URL, 'outsideRows': rows, 'hideNotice': hidden}}),
                    text=True, capture_output=True, check=True, timeout=10)
                value = json.loads(run.stdout)
                self.assertEqual(value['result'], {'ready': present and not hidden,
                    'warnings': [WARNING] if present and not hidden else []})
                self.assertLess(value['styleReads'], 20)
                self.assertEqual(value['clicks'], [])

    def app(self, directory, *, changed=None, selectable=False):
        store, provider, browser, app, _ = fixtures.CartPlanTests.app(directory, 'oda')
        original = provider.call
        count = 0
        def call(tool, args, **kwargs):
            nonlocal count
            if tool == 'get_delivery_addresses':
                return {'result': [{'id': 1, 'address': 'Eksempelveien 1', 'isSelected': True}]}
            if tool == 'get_cart':
                count += 1
                result = original(tool, args, **kwargs)
                if changed == 'cart' and count >= 2:
                    result['items'][0]['quantity'] += 1
                return result
            if tool == 'get_delivery_slots':
                if selectable:
                    return original(tool, args, **kwargs)
                provider.calls.append((tool, args))
                return {'slots': [], 'availability': {'date': '2026-10-13', 'total': 20, 'selectable': 0, 'unavailable': 20, 'full': 0}}
            return original(tool, args, **kwargs)
        provider.call = call
        browser.cart_delivery_restrictions = mock.Mock(return_value={"cart_restrictions": [WARNING]})
        return store, provider, browser, app

    def test_selectable_windows_do_not_hide_current_cart_stock_notice(self):
        with tempfile.TemporaryDirectory() as d:
            store, provider, browser, app = self.app(d, selectable=True)
            before = store.read()
            notice = 'Laksefilet uten skinn er dessverre utsolgt'
            browser.cart_delivery_restrictions.return_value = {'cart_stock_notices': [notice]}
            result = app.handle({'operation': 'delivery', 'action': 'list'})
            self.assertTrue(result['slots'])
            self.assertEqual(result['cart_stock_status'], 'observed')
            self.assertEqual(result['cart_stock_notices'], [notice])
            self.assertEqual(result['cart_stock_source'], 'current_cart_page')
            self.assertEqual(result['restriction_status'], 'unknown')
            self.assertNotIn('cart_restrictions', result)
            self.assertIn('before selecting delivery', result['next'])
            self.assertIn('Do not claim', result['next'])
            browser.cart_delivery_restrictions.assert_called_once()
            self.assertEqual(store.read(), before)
            self.assertFalse(any(t in ('manipulate_cart', 'select_delivery_slot') for t, _ in provider.calls))

    def test_current_stock_notice_is_discarded_if_cart_changes_after_inspection(self):
        with tempfile.TemporaryDirectory() as d:
            _, provider, browser, app = self.app(d, selectable=True)
            def inspect(*args, **kwargs):
                provider._mutate_cart({'operations': [{'productId': '10', 'quantity': 1}]})
                return {'cart_stock_notices': ['Current product er dessverre utsolgt']}
            browser.cart_delivery_restrictions.side_effect = inspect
            result = app.handle({'operation': 'delivery', 'action': 'list'})
            self.assertEqual(result['cart_stock_status'], 'unknown')
            self.assertNotIn('cart_stock_notices', result)
            self.assertNotIn('cart_stock_source', result)
            self.assertEqual(result['slots'], [])
            self.assertTrue(result['availability_stale'])

    def test_bound_cart_stock_notice_returns_before_delivery_navigation(self):
        browser = object.__new__(OdaBrowser)
        browser._verify_checkout_account = mock.Mock(return_value=hashlib.sha256(b'1').hexdigest())
        browser._settle = mock.Mock()
        restored = []
        @contextmanager
        def tab():
            try:
                yield
            finally:
                restored.append(True)
        browser._inspection_tab = tab
        browser._open = mock.Mock()
        browser._eval = mock.Mock(return_value={'url': CART_URL, 'authenticated': True,
            'items': [{'product_id': '10', 'quantity': 1}]})
        notice = 'Laksefilet uten skinn er dessverre utsolgt'
        browser._cart_surface = mock.Mock(return_value={'action': 'blocked', 'unavailable_message': notice})
        self.assertEqual(browser.cart_delivery_restrictions(SNAPSHOT), {'cart_stock_notices': [notice]})
        browser._open.assert_called_once_with(CART_URL)
        self.assertEqual(restored, [True])

    def test_current_restriction_is_reported_before_selection_without_writes(self):
        with tempfile.TemporaryDirectory() as d:
            store, provider, browser, app = self.app(d)
            before = store.read()
            result = app.handle({'operation': 'delivery', 'action': 'list', 'dates': ['2026-10-13']})
            self.assertEqual(result['restriction_status'], 'observed')
            self.assertEqual(result['cart_restrictions'], [WARNING])
            self.assertEqual(result['availability'][0]['full'], 0)
            self.assertEqual(store.read(), before)
            self.assertFalse(any(t in ('manipulate_cart', 'select_delivery_slot') for t, _ in provider.calls))

    def test_different_address_or_changed_cart_cannot_borrow_warning(self):
        for case in ('address', 'cart'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                _, _, browser, app = self.app(d, changed=case)
                result = app.handle({'operation': 'delivery', 'action': 'list', 'dates': ['2026-10-13'], **({'address_id': 2} if case == 'address' else {})})
                self.assertEqual(result['restriction_status'], 'unknown')
                self.assertNotIn('cart_restrictions', result)
                browser.cart_delivery_restrictions.assert_not_called()
                if case == 'cart':
                    self.assertTrue(result['availability_stale'])

    def test_compact_product_review_keeps_observed_stock_reason(self):
        from planning_operations import PlanningOperations
        result = PlanningOperations._product_review_snapshot({'requirements': [{'item': 'soap', 'observation': {'products': [{'product_ref': 10, 'name': 'Soap', 'availability': 'unavailable', 'display': {'availability': 'Utsolgt fra leverandør'}}]}}]})
        self.assertEqual(result['requirements'][0]['candidates'][0]['availability_description'], 'Utsolgt fra leverandør')

    def test_optional_snapshot_has_own_budget_and_cannot_spend_slot_deadline(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            _, provider, browser, app = self.app(d)
            original = provider.call
            limit = time.monotonic() + 60
            seen = []
            def call(tool, args, **kwargs):
                seen.append((tool, kwargs.get('deadline')))
                if tool == 'get_cart':
                    self.assertLessEqual(kwargs['deadline'], time.monotonic() + 15)
                    self.assertGreaterEqual(limit-kwargs['deadline'], 10)
                    raise HouseholdError('optional snapshot timed out')
                return original(tool, args, **kwargs)
            provider.call = call
            result = app._delivery({'action': 'list', 'dates': ['2026-10-13'], '_deadline': limit})
            self.assertEqual(result['restriction_status'], 'unknown')
            self.assertIn(('get_delivery_slots', limit), seen)
            browser.cart_delivery_restrictions.assert_not_called()

    def test_failed_inspection_still_invalidates_changed_cart_or_address(self):
        for case in ('cart', 'address'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                _, provider, browser, app = self.app(d)
                original = provider.call
                changed = False
                def call(tool, args, **kwargs):
                    result = original(tool, args, **kwargs)
                    if tool == 'get_delivery_slots':
                        result['slots'] = deepcopy(provider.delivery_slots['slots'])
                    if changed and tool == 'get_cart' and case == 'cart':
                        result['items'][0]['quantity'] += 1
                    if changed and tool == 'get_delivery_addresses' and case == 'address':
                        result['result'][0]['id'] = 2
                    return result
                provider.call = call
                def inspect(*args, **kwargs):
                    nonlocal changed
                    changed = True
                    raise HouseholdError('browser no longer matches')
                browser.cart_delivery_restrictions.side_effect = inspect
                result = app._delivery({'action': 'list', 'dates': ['2026-10-13'], 'address_id': 1})
                self.assertEqual(result['restriction_status'], 'unknown')
                self.assertEqual(result['slots'], [])
                self.assertTrue(result['availability_stale'])
                self.assertNotIn('cart_restrictions', result)

    def test_realistic_provider_latency_preserves_warning_and_final_check_budget(self):
        for limit in (160.0, 135.0):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as d:
                _, provider, browser, app = self.app(d)
                clock = [100.0]
                original = provider.call
                reads = []
                def call(tool, args, **kwargs):
                    if tool in ('get_cart', 'get_delivery_addresses'):
                        clock[0] += 2.5
                        if clock[0] > kwargs['deadline']:
                            raise HouseholdError('provider read deadline')
                        reads.append(tool)
                    return original(tool, args, **kwargs)
                provider.call = call
                def inspect(*args, **kwargs):
                    self.assertGreaterEqual(limit - kwargs['deadline'], 15)
                    # Use the entire admitted browser window. The final two
                    # provider reads must still have their own reserved time.
                    clock[0] = kwargs['deadline']
                    return {"cart_restrictions": [WARNING]}
                browser.cart_delivery_restrictions.side_effect = inspect
                with mock.patch('order_operations.time.monotonic', side_effect=lambda: clock[0]):
                    result = app._delivery({'action': 'list', 'dates': ['2026-10-13'], '_deadline': limit})
                self.assertEqual(result['restriction_status'], 'observed')
                self.assertEqual(result['cart_restrictions'], [WARNING])
                self.assertEqual(reads, ['get_cart', 'get_delivery_addresses'] * 3)
                self.assertLess(clock[0], limit)
