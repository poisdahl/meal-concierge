"""Menu clear invalidates selection authority without forgetting cart ownership."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_meal_concierge as fixtures
from core import HouseholdError, StateStore
from service import Application


class MenuClearCartOwnershipTests(unittest.TestCase):
    def test_partial_acknowledgement_needs_exact_owner_adoption_above_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            store, shop, browser, app, pid = fixtures.CartPlanTests.app(directory, "oda")
            original = shop.call
            def concurrent_cart(tool, args, **kwargs):
                if tool == "manipulate_cart":
                    # The successful reply covers a subset, and the account
                    # owner independently adds one further unit of the same SKU.
                    original(tool, {"operations": [op for op in args["operations"] if op["productId"] == 10]}, **kwargs)
                    return original(tool, {"operations": [{"productId": 10, "quantity": 1}]}, **kwargs)
                return original(tool, args, **kwargs)
            shop.call = concurrent_cart
            ref = app._cart_menu_ref(store.read()["menu"])
            result = app.handle({"operation":"cart", "action":"sync", "menu_ref":ref,
                "requirements":[{"product_id":pid, "product_name":"Pasta", "quantity":3},
                                {"product_id":"20", "product_name":"Peas", "quantity":1}]})
            review = result["cart_plan"]["ownership_review"]
            self.assertEqual(review["maximum_adoptable"], {"10":2})
            self.assertEqual(store.read()["cart_plan"]["added_quantities"], {})
            shop.call = original
            restarted = Application(StateStore(Path(directory), fixtures.CONFIG), shop, browser)
            decision = {"operation":"cart", "action":"reconcile", "decision":"keep_current",
                "cart_digest":review["cart_digest"], "menu_ref":ref}
            with self.assertRaisesRegex(HouseholdError, "baseline"):
                restarted.handle({**decision, "adopt_quantities":{"10":3}})
            restarted.handle({**decision, "adopt_quantities":{"10":2}})
            plan = restarted.store.read()["cart_plan"]
            self.assertEqual(plan["added_quantities"], {"10":2})
            successor = restarted._new_cart_plan({**ref, "revision":ref["revision"]+1}, {"10":4},
                {"10":"Pasta"}, {}, {}, set(), previous=plan)
            self.assertEqual(successor["baseline_quantities"], {"10":2})
            self.assertEqual(successor["added_quantities"], {"10":2})

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
