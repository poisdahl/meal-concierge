"""Synthetic regressions for cart decisions, delivery restrictions and weight holds."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import test_meal_concierge as fixtures
from test_payment_setup import PAYMENT_DOM
from core import HouseholdError
from oda_browser import OdaBrowser, CHECKOUT_URL, _oda_checkout_amount_script, _oda_checkout_amounts_minor
from retail_mcp import normalize_retail_delivery_slots


class OrderFlowTests(unittest.TestCase):
    def test_manual_removal_can_be_accepted_from_cart_read_without_checkout(self):
        for provider_name in ('oda', 'meny'):
            with self.subTest(provider=provider_name), tempfile.TemporaryDirectory() as directory:
                store, provider, _, app, pid = fixtures.CartPlanTests.app(directory, provider_name)
                fixtures.CartPlanTests.sync(app, pid)
                provider._mutate_cart({'operations': [{'productId': pid, 'quantity': -1}]})
                read = app.handle({'operation': 'cart', 'action': 'get', 'response_view': 'agent'})
                writes = sum(t == 'manipulate_cart' for t, _ in provider.calls)
                result = app.handle({'operation': 'cart', 'action': 'reconcile',
                    'menu_ref': app._cart_menu_ref(store.read()['menu']),
                    'cart_digest': read['cart_digest'], 'decision': 'keep_current',
                    'accept_missing_product_ids': [pid]})
                self.assertTrue(result['reconciled'])
                self.assertEqual(sum(t == 'manipulate_cart' for t, _ in provider.calls), writes)
                self.assertEqual(store.read()['cart_plan']['approved_cart_digest'], read['cart_digest'])

    def test_read_decision_rejects_stale_digest_or_changed_menu(self):
        for drift in ('cart', 'menu', 'pending'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                store, provider, _, app, pid = fixtures.CartPlanTests.app(directory, 'oda')
                fixtures.CartPlanTests.sync(app, pid)
                provider._mutate_cart({'operations': [{'productId': pid, 'quantity': -1}]})
                read = app.handle({'operation': 'cart', 'action': 'get'})
                original = provider.call
                def changed(tool, args, **kwargs):
                    result = original(tool, args, **kwargs)
                    if tool == 'get_cart' and drift == 'menu':
                        with store.locked() as state:
                            state['menu'] = fixtures.CartPlanTests.menu(2, 'b' * 64)
                    return result
                provider.call = changed
                if drift == 'cart':
                    provider._mutate_cart({'operations': [{'productId': pid, 'quantity': -1}]})
                if drift == 'pending':
                    # A protected pending operation must never be adopted by this convenience path.
                    with store.locked() as state:
                        state['order_change'] = {'status': 'editing', 'provider': 'oda', 'order_id': '123', 'started_at': '2026-09-01T00:00:00+00:00'}
                writes = sum(t == 'manipulate_cart' for t, _ in provider.calls)
                with self.assertRaises(HouseholdError):
                    app.handle({'operation': 'cart', 'action': 'reconcile',
                        'menu_ref': {k: fixtures.CartPlanTests.menu()[k] for k in ('menu_id', 'revision', 'digest')},
                        'cart_digest': read['cart_digest'], 'decision': 'restore_missing'})
                self.assertEqual(sum(t == 'manipulate_cart' for t, _ in provider.calls), writes)

    def test_only_new_week_empty_cart_starts_fresh(self):
        for mode in ('new_week', 'same_menu', 'successor_same_week', 'nonempty'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                store, provider, _, app, pid = fixtures.CartPlanTests.app(directory, 'oda')
                fixtures.CartPlanTests.sync(app, pid)
                provider._mutate_cart({'operations': [{'productId': pid, 'quantity': -2}]})
                with store.locked() as state:
                    old = deepcopy(state['menu'])
                    state['recipe_usage'][old['menu_id']] = {'week': old['week'], 'status': 'planned', 'recipe_keys': [], 'cooked_keys': [], 'not_cooked_keys': []}
                    if mode != 'same_menu':
                        state['menu'] = {**old, 'menu_id': 'menu_next', 'digest': 'b' * 64,
                                         'week': old['week'] if mode == 'successor_same_week' else '2026-W37'}
                        if mode == 'successor_same_week':
                            state['menu']['supersedes'] = app._cart_menu_ref(old)
                if mode == 'nonempty':
                    provider._mutate_cart({'operations': [{'productId': 20, 'quantity': 1}]})
                result = fixtures.CartPlanTests.sync(app, pid)
                self.assertEqual(result['synced'], mode == 'new_week')
                if mode != 'new_week':
                    self.assertTrue(result['cart_reconciliation_required'])

    def test_delivery_preserves_unavailable_dates_and_reads_restriction_once(self):
        with tempfile.TemporaryDirectory() as directory:
            _, provider, browser, app, _ = fixtures.CartPlanTests.app(directory, 'oda')
            original = provider.call
            raw = json.loads((Path(__file__).parent / 'fixtures/delivery/oda_slots.json').read_text())['response']
            def slots(tool, args, **kwargs):
                if tool != 'get_delivery_slots':
                    return original(tool, args, **kwargs)
                data = deepcopy(raw)
                # Same fixture day across repeated reads suffices to prove aggregation.
                if args['delivery_date'] == '2026-09-08':
                    for row in data['slots']:
                        row['isUnavailable'] = True
                return normalize_retail_delivery_slots(data)
            provider.call = slots
            warning = 'Noen av varene i handlekurven, f.eks. Såpe, er ikke tilgjengelige før 9. september 2026.'
            browser.cart_delivery_restrictions = mock.Mock(return_value=[warning])
            result = app.handle({'operation': 'delivery', 'action': 'list', 'dates': ['2026-09-08', '2026-09-09']})
            self.assertTrue(result['slots'])
            self.assertEqual(result['availability'][0]['unavailable'], 20)
            self.assertEqual(result['cart_restrictions'], [warning])
            browser.cart_delivery_restrictions.assert_called_once()
            browser.cart_delivery_restrictions.side_effect = HouseholdError('browser unavailable')
            result = app.handle({'operation': 'delivery', 'action': 'list', 'dates': ['2026-09-08']})
            self.assertEqual(result['slots'], [])
            self.assertEqual(result['restriction_status'], 'unknown')


    def test_delivery_diagnosis_preserves_every_pending_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            store, provider, browser, app, _ = fixtures.CartPlanTests.app(directory, 'oda')
            provider.call = mock.Mock(return_value={'slots': []})
            browser.cart_delivery_restrictions = mock.Mock(return_value=['notice'])
            for key in ('pending_checkout', 'pending_cart_change', 'pending_cancellation', 'order_change'):
                with self.subTest(key=key), mock.patch.object(store, 'read', return_value={**store.read(), key: {'status': 'uncertain'}}):
                    result = app._delivery({'action': 'list', 'dates': ['2026-09-08']})
                    self.assertEqual(result['restriction_status'], 'unknown')
                    browser.cart_delivery_restrictions.assert_not_called()
            self.assertTrue(all(c.args[0] == 'get_delivery_slots' for c in provider.call.call_args_list))


class WeightReservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('Node is required for browser parser checks')

    def evaluate(self, script, **case):
        output = subprocess.run([self.node, '-e', PAYMENT_DOM], input=json.dumps({'script': script, 'c': case}),
                                text=True, capture_output=True, check=True, timeout=10)
        return json.loads(output.stdout)

    def test_visible_cart_restriction_read_is_bounded_and_has_no_clicks(self):
        warning = 'Noen av varene i handlekurven, f.eks. Såpe, er ikke tilgjengelige før 9. september 2026.'
        browser = object.__new__(OdaBrowser)
        browser._open = mock.Mock()
        def evaluate(script):
            result = self.evaluate(script, outsideRows=[[warning], ['Unrelated text']])
            self.assertEqual(result['clicks'], [])
            return result['result']
        browser._eval = evaluate
        self.assertEqual(browser.cart_delivery_restrictions(), [warning])
        browser._open.assert_called_once_with('https://oda.com/no/cart/')

    def test_weight_reservation_is_bound_in_read_and_final_click(self):
        rows = [['1 vare', '100,00 kr'], ['Delsum', '100,00 kr'],
                ['Leveringsemballasje', '10,00 kr'], ['Vektreservasjon', '5,00 kr'],
                ['Levering', '0,00 kr'], ['Total inkl. MVA', '115,00 kr']]
        read = _oda_checkout_amount_script(11500, expected_product_count=1)
        options = {'selected': 2, 'button': 'Bekreft og betal 115,00 kr'}
        observed = self.evaluate(read, rows=rows, **options)['result']
        self.assertTrue(observed['amounts_valid'])
        self.assertEqual(observed['amounts']['other_fees'], {'Vektreservasjon': 500})
        click = _oda_checkout_amount_script(11500, expected_product_count=1,
            expected_amounts=observed['amounts'], expected_url=CHECKOUT_URL)
        self.assertEqual(self.evaluate(click, rows=rows, **options)['clicks'], ['PAY'])
        for changed in ([*rows, rows[3]], [*rows[:3], ['Vektreservasjon', '-5,00 kr'], *rows[4:]],
                        [*rows[:3], ['Unknown fee', '5,00 kr'], *rows[4:]],
                        [*rows[:2], ['Leveringsemballasje', '9,00 kr'], ['Vektreservasjon', '6,00 kr'], *rows[4:]]):
            with self.subTest(rows=changed):
                self.assertEqual(self.evaluate(click, rows=changed, **options)['clicks'], [])
        for options2 in ({'hiddenRows': [3]}, {'outsideRows': [rows[3]]}):
            self.assertFalse(self.evaluate(read, rows=rows, **options, **options2)['result']['amounts_valid'])
        both = [*rows[:-1], ['Tillegg for mindre bestilling', '20,00 kr'], ['Total inkl. MVA', '135,00 kr']]
        self.assertTrue(self.evaluate(_oda_checkout_amount_script(13500, expected_product_count=1), rows=both, **options)['result']['amounts_valid'])
        for fee in (float('nan'), float('inf'), -1, True):
            amounts = {key: None for key in observed['amounts']}
            amounts['other_fees'] = {'Vektreservasjon': fee}
            with self.assertRaises(HouseholdError):
                _oda_checkout_amounts_minor(amounts)
