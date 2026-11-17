"""Menu clear invalidates selection authority without forgetting cart ownership."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_meal_concierge as fixtures
from core import StateStore
from service import Application


class MenuClearCartOwnershipTests(unittest.TestCase):
    def test_clear_preserves_ledger_across_restart_and_next_menu(self):
        for provider_name in ('oda', 'mathem', 'meny'):
            with self.subTest(provider=provider_name), tempfile.TemporaryDirectory() as directory:
                store, shop, browser, app, product = fixtures.CartPlanTests.app(directory, provider_name)
                result = fixtures.CartPlanTests.sync(app, product, 3)
                self.assertTrue(result['synced'], result)
                before = store.read()['cart_plan']
                cart = deepcopy(shop.cart)
                with store.locked() as state:
                    state['cart_plan']['product_plan_digest'] = 'a' * 64
                app.handle({'operation': 'menu', 'action': 'clear',
                            'menu_ref': app._cart_menu_ref(store.read()['menu'])})
                after = store.read()['cart_plan']
                self.assertEqual(shop.cart, cart)
                self.assertIsNone(store.read()['menu'])
                for key in ('added_quantities', 'supplemental_quantities', 'baseline_quantities'):
                    self.assertEqual(after.get(key), before.get(key))
                self.assertNotIn('product_plan_digest', after)
                settings = {**fixtures.CONFIG, 'provider': provider_name}
                app = Application(StateStore(Path(directory), settings), shop, browser)
                with app.store.locked() as state:
                    state['menu'] = {**fixtures.CartPlanTests.menu(), 'menu_id': 'next-menu'}
                # A new plan inherits only attributable additions; existing
                # household stock remains baseline and is never auto-removed.
                live = before['last_synced_quantities']
                plan = app._new_cart_plan(app._cart_menu_ref(app.store.read()['menu']),
                    live, before['product_names'], {}, {}, set(), previous=app.store.read()['cart_plan'])
                self.assertEqual(plan['added_quantities'], before['added_quantities'])
                self.assertEqual(plan['baseline_quantities'], before['baseline_quantities'])
                self.assertEqual(plan['status'], 'needs_input')


if __name__ == '__main__':
    unittest.main()
