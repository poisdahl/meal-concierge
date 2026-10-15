"""Store-free setup and real local recipe/menu paths, with commerce isolated."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
from core import StateStore, HouseholdError, first_store_state, _atomic_json
from service import Application, config, run, parser
from test_meal_concierge_planner import recipe


class NoStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {'household': 'Fixture', 'provider': 'none'}
        self.store = StateStore(self.root / 'state', self.settings)
        self.app = Application(self.store, None, None)
        self.app._now = lambda: datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
        self.app._household_today = lambda state=None: self.app._now().date()

    def save_menu(self):
        saved = self.app.handle({'operation': 'recipes', 'action': 'save',
                                 'recipe': recipe('Rice dish', 'rice', ingredient='ris'),
                                 'idempotency_key': 'fixture-rice'})['recipe']
        planned = self.app.handle({'operation': 'menu', 'action': 'plan',
            'planner_input': {'selection_mode': 'agent', 'days': [1], 'portions': 2,
                              'candidates': [{'recipe_ref': {'id': saved['id'], 'revision': saved['revision']}}]}})['plan']
        self.assertEqual(planned['status'], 'planned', planned)
        return self.app.handle({'operation': 'menu', 'action': 'save', 'planner_ref': planned['save_ref']})['menu']

    def test_retailer_favorites_cannot_block_first_connection(self):
        for operation in ('product_favorites', 'recurring'):
            with self.assertRaisesRegex(HouseholdError, 'Connect a store'):
                self.app.handle({'operation': operation, 'action': 'add', 'item': {'product_id':'10'}})
        for provider in ('oda', 'mathem', 'meny'):
            first_store_state(self.store.read(), provider)

    def test_setup_status_and_local_menu_need_no_account(self):
        setup = self.app.handle({'operation': 'setup'})
        self.assertEqual(setup['current']['provider'], 'none')
        self.assertNotIn('payment_choices', setup['current'])
        self.assertNotIn('checkout_payment', setup['current'])
        self.assertEqual(setup['store_readiness']['connection_check']['status'], 'not_configured')
        self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})
        menu = self.save_menu()
        result = self.app.handle({'operation': 'shopping_list'})
        self.assertEqual(result['items'][0]['item'], 'ris')
        self.assertEqual(result['menu_ref']['menu_id'], menu['menu_id'])
        self.assertEqual(self.app.handle({'operation': 'status'})['integration']['status'], 'not_configured')
        self.assertEqual(self.store.read()['checkout_payment'], {})
        self.assertTrue(all(not self.store.read()['profile']['recipes']['sources'][p] for p in ('oda','mathem','meny')))

    def test_commerce_fails_before_any_client_access(self):
        for operation in ('cart', 'catalog', 'products', 'checkout', 'orders', 'delivery'):
            with self.subTest(operation=operation), self.assertRaisesRegex(HouseholdError, 'No store'):
                self.app.handle({'operation': operation})
        with self.assertRaisesRegex(HouseholdError, 'connected store'):
            self.app.handle({'operation': 'schedule', 'action': 'update', 'changes': {'mode': 'cart_ready'}})

    def test_run_constructs_neither_store_nor_browser(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.settings))
        args = parser().parse_args(['--config', str(path), '--state', str(self.root/'runtime'), '--socket', str(self.root/'s.sock')])
        with mock.patch('service.RetailMcpClient', side_effect=AssertionError('store constructed')), \
             mock.patch('service.MenyClient', side_effect=AssertionError('store constructed')), \
             mock.patch('service.OdaBrowser', side_effect=AssertionError('browser constructed')), \
             mock.patch('service.MathemBrowser', side_effect=AssertionError('browser constructed')), \
             mock.patch('service.Server') as server:
            run(args, config(path))
            server.return_value.run.assert_called_once()
            app = server.call_args.args[-1]
            self.assertIsNone(app.provider_client)
            self.assertIsNone(app.browser)

    def test_explicit_first_connection_preserves_food_and_list(self):
        self.save_menu()
        listing = self.app.handle({'operation': 'shopping_list'})
        self.app.handle({'operation': 'shopping_list', 'action': 'update', 'list_ref': listing['list_ref'],
                         'extra_items': [{'id':'coffee','item':'Kaffe'}]})
        old = self.store.read()
        database = self.app.recipes.get(old['menu']['dishes'][0]['recipe_ref']['id'])
        for provider in ('oda', 'mathem', 'meny'):
            with self.subTest(provider=provider):
                new = first_store_state(old, provider)
                for key in ('menu', 'shopping_list', 'recipe_usage', 'planning_feedback', 'menu_planning'):
                    self.assertEqual(new[key], old[key])
                self.assertEqual(new['provider'], provider)
                self.assertTrue(new['profile']['recipes']['sources'][provider])
                self.assertEqual(new['setup']['status'], 'needs_review')
                with self.assertRaises(HouseholdError):
                    first_store_state(new, 'oda')
        self.assertEqual(self.app.recipes.get(database['id']), database)
        for key in ('pending_checkout', 'order_snapshots', 'cart_plan'):
            with self.subTest(key=key), self.assertRaises(HouseholdError):
                first_store_state({**old,key:{'fixture':True}}, 'oda')
        with self.assertRaisesRegex(HouseholdError, 'belongs to provider'):
            StateStore(self.store.directory, {**self.settings, 'provider':'oda'})

    def test_legacy_omitted_provider_still_means_oda(self):
        store = StateStore(self.root / 'legacy', {'household':'Legacy'})
        self.assertEqual(store.read()['provider'], 'oda')
        self.assertEqual(store.read()['checkout_payment']['method'], 'saved_card')


if __name__ == '__main__':
    unittest.main()
