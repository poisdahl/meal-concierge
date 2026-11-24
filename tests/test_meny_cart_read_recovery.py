"""Offline coverage of interrupted cart fills and the reads used to recover them."""
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore, cart_summary
from meny import MENY_CART_TOTAL_WAIT_SECONDS, MenyBrowserError
from meny_catalog import MenyCatalogUnavailable, read_catalog
import recipe_import_sources
from retailer_recipes import read_meny_recipe_search
from service import Application
import test_meal_concierge as base


class InterruptedMenuFillTests(unittest.TestCase):
    def test_accepted_cart_protects_replacements_after_confirmed_removals(self):
        for immediate_read in (True, False):
            with self.subTest(immediate_read=immediate_read), tempfile.TemporaryDirectory() as directory:
                store, provider, browser, app, first = base.CartPlanTests.app(directory, "meny")
                other = "/varer/zzzz/pasta-12345678"
                self.assertTrue(base.CartPlanTests.sync(app, first, 4)["synced"])
                with store.locked() as state:
                    state["menu"] = base.CartPlanTests.menu(revision=2, digest="b" * 64)
                original = provider.call
                writes = []
                fail_next_read = False
                failure = MenyBrowserError("fixture", failure_class="quantity_not_settled",
                                           source="cart_readback", elapsed_ms=15000)

                def call(tool, arguments, **kwargs):
                    nonlocal fail_next_read
                    if tool == "manipulate_cart":
                        writes.append(arguments["operations"])
                        if len(writes) == 2:
                            fail_next_read = not immediate_read
                            raise failure
                    if tool == "get_cart" and fail_next_read:
                        fail_next_read = False
                        raise HouseholdError("readback unavailable")
                    result = original(tool, arguments, **kwargs)
                    if tool == "get_cart" and arguments.get("observe_order_mode"):
                        result = {**result, "ordinary_cart": True}
                    return result

                provider.call = call
                result = base.CartPlanTests.sync(app, first, requirements=[
                    {"product_id": first, "product_name": "Brokkoli", "quantity": 2},
                    {"product_id": other, "product_name": "Pasta", "quantity": 2},
                ])
                journal = store.read()["pending_cart_change"]
                self.assertEqual(journal["acknowledged"], {first: 2})
                self.assertEqual(result["operation_id"], journal["operation_id"])
                self.assertEqual(result["provider_failure"], failure.provider_failure)
                self.assertTrue(result["cart_write_pending"])
                # After the completed -2, a household member adds two of their own.
                provider._mutate_cart({"operations": [{"productId": first, "quantity": 2}]})
                app = Application(StateStore(store.directory, store.config), provider, browser)
                view = app.handle({"operation": "cart", "action": "get"})
                app.handle({"operation": "cart", "action": "accept_current",
                            "operation_id": view["operation_id"], "cart_digest": view["cart_digest"]})
                kept = app.handle({"operation": "cart", "action": "reconcile", "decision": "keep_current",
                                   "menu_ref": journal["managed_menu"], "cart_digest": view["cart_digest"],
                                   "accept_missing_product_ids": [other]})
                self.assertTrue(kept["reconciled"])
                self.assertEqual(store.read()["cart_plan"]["added_quantities"], {first: 1})
                with store.locked() as state:
                    state["menu"] = base.CartPlanTests.menu(revision=3, digest="c" * 64)
                self.assertTrue(base.CartPlanTests.sync(app, other, 1)["synced"])
                quantities, _ = app._cart_lines(cart_summary(provider.cart))
                self.assertEqual(quantities, {first: 3, other: 1})

    def test_reconcile_reports_the_original_failure_when_verification_also_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            store, provider, _browser, app, product = base.CartPlanTests.app(directory, "meny")
            self.assertTrue(base.CartPlanTests.sync(app, product, 3)["synced"])
            provider._mutate_cart({"operations": [{"productId": product, "quantity": -1}]})
            drift = base.CartPlanTests.sync(app, product, 3)
            failure = MenyBrowserError("fixture", failure_class="quantity_not_settled", source="cart_readback")
            original = provider.call
            lost = False

            def call(tool, arguments, **kwargs):
                nonlocal lost
                if tool == "manipulate_cart":
                    lost = True
                    raise failure
                if tool == "get_cart" and lost:
                    raise HouseholdError("readback unavailable")
                return original(tool, arguments, **kwargs)

            provider.call = call
            plan = drift["cart_plan"]
            result = app.handle({"operation": "cart", "action": "reconcile", "decision": "restore_missing",
                                 "menu_ref": plan["menu_ref"], "cart_digest": plan["cart_digest"]})
            self.assertTrue(result["outcome_unknown"])
            self.assertEqual(result["reason"], "cart_write_verification_unavailable")
            self.assertEqual(result["operation_id"], store.read()["pending_cart_change"]["operation_id"])
            self.assertEqual(result["provider_failure"], failure.provider_failure)


class LateDrawerTotalTests(unittest.TestCase):
    def test_zero_on_the_last_render_poll_gets_its_own_grace_period(self):
        fixture = base.MenyClientTests()
        zero = fixture.ZERO_TOTAL_DRAWER
        pending = {"ready": False, "authenticated": True, "root_count": 1}
        valid = {**zero, "ready": True, "subtotal": 19.9, "total": 19.9}
        for persists in (False, True):
            with self.subTest(persists=persists):
                client = fixture.client()
                zero_polls = 4 * MENY_CART_TOTAL_WAIT_SECONDS + 1 if persists else 3
                after = [fixture.OPEN_DRAWER, valid] if persists else [valid]
                client._eval = mock.Mock(side_effect=[
                    fixture.OPEN_DRAWER, *([pending] * 59), *([zero] * zero_polls), *after,
                ])
                with fixture.drawer_clock(client) as clock:
                    started = clock["now"]
                    reloads = []
                    client._invoke = lambda *args: reloads.append((args, clock["now"] - started))
                    self.assertEqual(client._read_cart()["total"], 19.9)
                expected = [(("reload",), 59 * 0.25 + MENY_CART_TOTAL_WAIT_SECONDS)] if persists else []
                self.assertEqual(reloads, expected)


class PublicReadDeadlineTests(unittest.TestCase):
    def test_stalled_dns_obeys_both_public_read_budgets_and_bounds_background_workers(self):
        release = threading.Event()
        slots = threading.BoundedSemaphore(2)

        def stalled(*args, **kwargs):
            release.wait(5)
            raise socket.gaierror("fixture resolver unavailable")

        with mock.patch.object(recipe_import_sources, "_PUBLIC_DNS_SLOTS", slots), \
                mock.patch.object(socket, "getaddrinfo", side_effect=stalled) as resolve, \
                mock.patch.object(socket, "socket") as connect:
            try:
                started = time.monotonic()
                with self.assertRaises(MenyCatalogUnavailable):
                    read_catalog("1234567890123", ["12345678"], timeout=0.04)
                with self.assertRaises(HouseholdError):
                    read_meny_recipe_search("pasta", 1, timeout=0.04)
                for _ in range(3):
                    with self.assertRaises(HouseholdError):
                        read_meny_recipe_search("pasta", 1, timeout=0.04)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(resolve.call_count, 2)
            finally:
                release.set()
                for _ in range(2):
                    self.assertTrue(slots.acquire(timeout=2))
                for _ in range(2):
                    slots.release()
            with self.assertRaises(HouseholdError):
                read_meny_recipe_search("pasta", 1, timeout=1)
            self.assertEqual(resolve.call_count, 3)
            connect.assert_not_called()
