"""A partial MENY menu fill finishes despite one-product-at-a-time reads."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import time as real_time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore  # noqa: E402
from meny import MenyBrowserError, MenyCartStoppedError  # noqa: E402
from product_observations import normalize_meny_product_search  # noqa: E402
from product_planner import menu_requirements  # noqa: E402
from service import Application  # noqa: E402
import planning_operations  # noqa: E402
import product_planner  # noqa: E402
import service  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "products"
OBSERVED_AT = "2026-09-04T12:00:00+00:00"
SECONDS_PER_PRODUCT_READ = 10


class FakeClock:
    """The planner's monotonic clock; everything else uses the real time module."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(real_time, name)


class SlowMeny:
    """MENY-like store: each product read costs one page load."""

    def __init__(self, clock):
        self.clock = clock
        fixture = json.loads((FIXTURES / "meny_product_observations.json").read_text(encoding="utf-8"))
        observation = normalize_meny_product_search(fixture["response"], observed_at=OBSERVED_AT)
        self.template = next(item for item in observation["products"] if item["name"] == "Lettmelk 0,5%")
        self.calls = []
        self.failing_query = None
        self.seconds_per_read = SECONDS_PER_PRODUCT_READ
        self.cart = {"items": [], "count": 0, "subtotal": 0.0, "total": 0.0, "delivery": None}

    def product_for(self, query):
        number = int(query.rsplit(" ", 1)[1])
        product = deepcopy(self.template)
        product["product_ref"] = self.ref(number)
        product["name"] = f"Lettmelk {number}"
        return product

    def ref(self, number):
        # A distinct, valid MENY product path per test product.
        prefix, digits = self.template["product_ref"].rsplit("-", 1)
        return f"{prefix}-{int(digits) + number:0{len(digits)}d}"

    def probe(self, **_kwargs):
        return {"protocol_version": "fixture", "server": {"name": "fixture"}, "tool_count": 1}

    def call(self, tool_name, arguments, **_kwargs):
        self.calls.append((tool_name, deepcopy(arguments)))
        if tool_name == "product_search":
            self.clock.now += self.seconds_per_read
            query = arguments["queries"][0]
            if query == self.failing_query:
                raise MenyBrowserError("MENY search page failed", failure_class="adapter_rejected",
                                       source="adapter_stderr")
            return {"provider": "meny", "query": query, "observed_at": OBSERVED_AT,
                    "scope": {"kind": "provider_search", "page": 1, "requested_size": 5, "returned": 1,
                              "semantics": "bounded_relevance_ranked"},
                    "products": [self.product_for(query)]}
        if tool_name == "get_cart":
            return deepcopy(self.cart)
        if tool_name == "manipulate_cart":
            for operation in arguments["operations"]:
                existing = next((item for item in self.cart["items"] if item["product_id"] == operation["productId"]), None)
                if existing is None:
                    existing = {"product_id": operation["productId"], "name": "Lettmelk", "quantity": 0, "price": 0.0}
                    self.cart["items"].append(existing)
                existing["quantity"] += operation["quantity"]
                existing["price"] = 21.8 * existing["quantity"]
            self.cart["count"] = sum(item["quantity"] for item in self.cart["items"])
            self.cart["subtotal"] = self.cart["total"] = sum(item["price"] for item in self.cart["items"])
            return deepcopy(self.cart)
        raise AssertionError(tool_name)

    def reads(self):
        return sum(name == "product_search" for name, _ in self.calls)

    def writes(self):
        return sum(name == "manipulate_cart" for name, _ in self.calls)


class PartialApplyTimeBudgetTests(unittest.TestCase):
    def household(self, approved, unresolved=True):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        clock = FakeClock()
        for module in (planning_operations, product_planner, service):
            patcher = mock.patch.object(module, "time", clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        store = StateStore(Path(temp.name), {"instance": "test", "household": "Test", "provider": "meny",
                                             "profile_overrides": {}})
        menu = {"menu_id": "menu_slow_meny", "revision": 1, "digest": "c" * 64, "phase": "draft",
                "dishes": [{"shopping_requirements": [
                    {"item": f"Lettmelk {number}", "quantity": 1, "unit": "l", "scalable": True}
                    for number in range(1, approved + 1 + int(unresolved))]}],
                "salads": []}
        with store.locked() as state:
            state["menu"] = deepcopy(menu)
        provider = SlowMeny(clock)
        app = Application(store, provider, mock.Mock())
        requirements = menu_requirements(menu)[0]
        approvals = []
        for row in requirements:
            number = int(row["item"].rsplit(" ", 1)[1])
            if number <= approved:  # the last requirement stays unresolved
                approvals.append({"requirement_id": row["requirement_id"],
                                  "candidate_refs": [provider.ref(number)]})
        prepared = app.handle({"operation": "products", "action": "prepare", "menu_ref": app._cart_menu_ref(menu),
                               "candidate_approvals": approvals})
        while prepared.get("continue_arguments"):
            prepared = app.handle({"operation": "products", **prepared["continue_arguments"]})
        if unresolved:
            self.assertIn("partial_apply_arguments", prepared, prepared.get("product_plan", {}).get("status"))
        return store, provider, app, prepared

    def finish(self, app, result, limit=6):
        calls = 1
        while result.get("status") == "validating":
            self.assertLess(calls, limit, result)
            result = app.handle({"operation": "products", **result["continue_arguments"]})
            calls += 1
        return result, calls

    def test_large_partial_fill_continues_across_calls_and_writes_once(self):
        store, provider, app, prepared = self.household(approved=26)
        reads_before = provider.reads()
        request = {"operation": "products", "partial_apply": True, "cart_change_requested": True,
                   "action": "apply", "product_plan_ref": prepared["product_plan_ref"],
                   "partial_product_plan_digest": prepared["partial_apply_arguments"]["partial_product_plan_digest"]}
        first = app.handle(request)
        self.assertEqual(first["status"], "validating", first)
        self.assertFalse(first["applied"])
        self.assertGreater(first["validation_progress"]["pending_search_count"], 0)
        self.assertEqual(provider.writes(), 0)
        self.assertEqual(provider.cart["items"], [])
        result = first
        calls = 1
        while result.get("status") == "validating":
            self.assertLess(calls, 5)
            result = app.handle({"operation": "products", **result["continue_arguments"]})
            calls += 1
        self.assertTrue(result.get("partial_applied"), result)
        self.assertEqual(provider.cart["count"], 26)
        # Each selected product was read once for the write, with no second pre-write read.
        self.assertEqual(provider.reads() - reads_before, 26)
        self.assertFalse(store.read().get("partial_product_validation"))

    def test_meny_write_waits_for_a_full_budget_after_long_reads(self):
        store, provider, app, prepared = self.household(approved=12)
        arguments = {"operation": "products", **prepared["partial_apply_arguments"], "cart_change_requested": True}
        first = app.handle(arguments)
        # Twelve reads leave less than the MENY write reserve; nothing is written yet.
        self.assertEqual(first["status"], "validating", first)
        self.assertEqual(first["validation_progress"]["pending_search_count"], 0)
        self.assertEqual(first["validation_progress"]["pending_cart_write"], 1)
        self.assertEqual(provider.writes(), 0)
        reads = provider.reads()
        second = app.handle({"operation": "products", **first["continue_arguments"]})
        self.assertTrue(second.get("partial_applied"), second)
        self.assertEqual(provider.reads(), reads)
        self.assertEqual(provider.cart["count"], 12)


    def test_a_selected_read_that_keeps_failing_ends_the_fill_with_its_failure(self):
        for unresolved in (True, False):
            with self.subTest(partial=unresolved):
                store, provider, app, prepared = self.household(approved=3, unresolved=unresolved)
                provider.failing_query = "Lettmelk 2"
                key = "partial_apply_arguments" if unresolved else "apply_arguments"
                result, calls = self.finish(app, app.handle({"operation": "products", **prepared[key],
                                                             "cart_change_requested": True}))
                self.assertEqual(result["status"], "needs_input", result)
                self.assertEqual(result["reason"], "selected product reads keep failing")
                failure, = result["read_failures"]
                self.assertEqual((failure["query"], failure["attempts"]), ("Lettmelk 2", 2))
                self.assertEqual(failure["provider_failure"]["class"], "adapter_rejected")
                self.assertLessEqual(calls, 3)
                self.assertEqual(app._product_apply_view(result)["read_failures"], result["read_failures"])
                self.assertEqual(provider.writes(), 0)
                self.assertFalse(store.read().get("partial_product_validation"))
                # Once the store reads again, the same apply completes.
                provider.failing_query = None
                done, _ = self.finish(app, app.handle({"operation": "products", **prepared[key],
                                                       "cart_change_requested": True}))
                self.assertTrue(done.get("applied"), done)

    def test_compact_apply_ref_survives_failed_reads_and_restart(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                store, provider, app, prepared = self.household(approved=3, unresolved=partial)
                agent = {"operation": "products", "response_view": "agent"}
                reference = prepared["product_plan_ref"]
                page = app.handle({**agent, "action": "get", "product_plan_ref": reference})
                args = page["partial_apply_arguments" if partial else "apply_arguments"]
                request = {**agent, **args, "cart_change_requested": True}
                provider.failing_query = "Lettmelk 2"
                result = app.handle(request)
                for _ in range(3):
                    if result["status"] != "validating":
                        break
                    result = app.handle({**agent, **result["continue_arguments"]})
                self.assertEqual(result["status"], "needs_input")
                self.assertEqual(result["product_plan_ref"], reference)
                self.assertEqual(result["read_failures"][0]["query"], provider.failing_query)
                self.assertEqual(provider.writes(), 0)

                provider.failing_query = None
                restarted = Application(store, provider, mock.Mock())
                page = restarted.handle({**agent, "action": "get", "product_plan_ref": reference})
                self.assertEqual(page["partial_apply_arguments" if partial else "apply_arguments"], args)
                reads_before = provider.reads()
                self.assertTrue(restarted.handle(request)["applied"])
                self.assertEqual(provider.reads() - reads_before, 3)
                self.assertEqual(provider.writes(), 2)  # three units in bounded MENY batches

    def test_confirmed_login_loss_stops_searching_and_updates_service_readiness(self):
        store, provider, app, prepared = self.household(approved=3, unresolved=False)
        menu_ref = app._cart_menu_ref(store.read()["menu"])
        page = app.handle({"operation": "products", "action": "get", "product_plan_ref": prepared["product_plan_ref"]})
        for arguments in ({"action": "prepare", "menu_ref": menu_ref},
                          {**page["apply_arguments"], "cart_change_requested": True}):
            with self.subTest(action=arguments["action"]):
                app.integration = {"status": "ready", "provider": "meny"}
                with mock.patch.object(provider, "call", side_effect=HouseholdError(
                        "MENY login is required in the configured browser profile")) as read:
                    with self.assertRaisesRegex(HouseholdError, "MENY login is required"):
                        app.handle({"operation": "products", "response_view": "agent", **arguments})
                self.assertEqual(read.call_count, 1)
                self.assertEqual(read.call_args.args[0], "product_search")
                self.assertEqual(app.integration["status"], "awaiting_login")
                self.assertEqual(provider.writes(), 0)

    def test_compact_apply_refreshes_checks_started_before_login_loss(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                store, provider, app, prepared = self.household(approved=3, unresolved=partial)
                agent = {"operation": "products", "response_view": "agent"}
                page = app.handle({**agent, "action": "get", "product_plan_ref": prepared["product_plan_ref"]})
                args = page["partial_apply_arguments" if partial else "apply_arguments"]
                request = {**agent, **args, "cart_change_requested": True}
                with mock.patch.object(planning_operations, "MAX_REQUIREMENTS", 1):
                    progress = app.handle(request)
                self.assertEqual(progress["validation_progress"]["pending_search_count"], 2)
                with mock.patch.object(provider, "call", side_effect=HouseholdError(
                        "MENY login is required in the configured browser profile")):
                    with self.assertRaisesRegex(HouseholdError, "MENY login is required"):
                        app.handle({**agent, **progress["continue_arguments"]})
                reads = provider.reads()
                restarted = Application(store, provider, mock.Mock())
                page = restarted.handle({**agent, "action": "get", "product_plan_ref": prepared["product_plan_ref"]})
                if partial:
                    resume = {**page["partial_apply_arguments"], "cart_change_requested": True}
                else:
                    resume = page["continue_arguments"]
                    self.assertEqual(page["validation_progress"]["pending_search_count"], 3)
                    self.assertTrue(page["validation_progress"]["restarted"])
                self.assertTrue(restarted.handle({**agent, **resume})["applied"])
                self.assertEqual(provider.reads() - reads, 3)

    def test_compact_prepare_resumes_after_login_loss_and_rereads_all_prices(self):
        store, provider, app, _prepared = self.household(approved=4, unresolved=False)
        agent = {"operation": "products", "response_view": "agent"}
        menu_ref = app._cart_menu_ref(store.read()["menu"])
        with mock.patch.object(planning_operations, "MAX_REQUIREMENTS", 1):
            page = app.handle({**agent, "action": "prepare", "menu_ref": menu_ref})
        self.assertEqual(page["progress"]["pending_search_count"], 3)
        continuation = page["continue_arguments"]
        with mock.patch.object(provider, "call", side_effect=HouseholdError(
                "MENY login is required in the configured browser profile")):
            with self.assertRaisesRegex(HouseholdError, "MENY login is required"):
                app.handle({**agent, **continuation})
        restarted = Application(store, provider, mock.Mock())
        page = restarted.handle({**agent, "action": "get", "product_plan_ref": continuation["product_plan_ref"]})
        self.assertEqual(page["continue_arguments"], continuation)
        self.assertEqual(page["progress"]["pending_search_count"], 4)
        provider.template["purchase_options"][0].update(merchandise_ore=7999, total_payable_ore=7999)
        reads = provider.reads()
        result = restarted.handle({"operation": "products", **page["continue_arguments"]})
        rows = result["product_plan"]["requirements"]
        self.assertEqual(provider.reads() - reads, 4)
        self.assertEqual({row["observation"]["products"][0]["purchase_options"][0]["merchandise_ore"]
                          for row in rows}, {7999})
        self.assertNotIn("continue_arguments", result)
        self.assertEqual(provider.writes(), 0)

    def test_expired_search_prices_are_reread_while_failed_queries_eventually_settle(self):
        store, provider, app, _prepared = self.household(approved=5, unresolved=False)
        start = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
        app._now = lambda: start + timedelta(seconds=provider.clock.now)
        menu_ref = app._cart_menu_ref(store.read()["menu"])
        original = provider.call

        def fail_three_searches(tool, arguments, **kwargs):
            if tool == "product_search" and arguments["queries"][0].split()[-1] in {"1", "2", "3"}:
                raise MenyBrowserError("synthetic product page unavailable",
                                       failure_class="adapter_rejected", source="adapter_stderr")
            return original(tool, arguments, **kwargs)

        with mock.patch.object(provider, "call", side_effect=fail_three_searches):
            result = app.handle({"operation": "products", "action": "prepare", "menu_ref": menu_ref})
            self.assertIn("continue_arguments", result)
            for attempt in range(1, 9):
                # Every continuation crosses the real 30-minute price window.
                provider.clock.now += 1801
                current_price = 2180 + attempt
                provider.template["purchase_options"][0].update(
                    merchandise_ore=current_price, total_payable_ore=current_price)
                result = app.handle({"operation": "products", **result["continue_arguments"]})
                if "continue_arguments" not in result:
                    break
            self.assertNotIn("continue_arguments", result)
            rows = result["product_plan"]["requirements"]
            products = [row["observation"]["products"][0] for row in rows if row.get("observation")]
            self.assertEqual({product["name"] for product in products}, {"Lettmelk 4", "Lettmelk 5"})
            self.assertEqual({product["purchase_options"][0]["merchandise_ore"] for product in products}, {current_price})
            self.assertEqual(provider.writes(), 0)

    def test_a_fill_that_stops_before_its_next_click_needs_only_the_cart_review(self):
        store, provider, app, prepared = self.household(approved=3, unresolved=False)
        original = provider.call

        def stop_after_first_click(tool_name, arguments, **kwargs):
            if tool_name == "manipulate_cart" and provider.writes() == 0:
                first = dict(arguments["operations"][0], quantity=1)
                original(tool_name, {"operations": [first]})
                raise MenyCartStoppedError("MENY operation deadline reached", [first])
            return original(tool_name, arguments, **kwargs)

        provider.call = stop_after_first_click
        apply = {"operation": "products", **prepared["apply_arguments"], "cart_change_requested": True}
        result, _calls = self.finish(app, app.handle(apply))
        self.assertIs(result["applied"], False, result)
        view = app._product_apply_view(result)
        self.assertEqual(view["status"], "needs_input")
        self.assertEqual(view["reason"], "cart_write_stopped_before_click")
        for key in ("outcome_unknown", "cart_write_pending", "operation_id"):
            self.assertNotIn(key, view)
        self.assertFalse(store.read().get("pending_cart_change"))
        self.assertEqual(provider.cart["count"], 1)
        # The household's review finishes the fill; the next apply then completes.
        restored = app.handle({"operation": "cart", "action": "reconcile", "decision": "restore_missing",
                               "menu_ref": result["menu_ref"], "cart_digest": view["cart_plan"]["cart_digest"]})
        self.assertTrue(restored["reconciled"], restored)
        self.assertEqual(provider.cart["count"], 3)
        done, _calls = self.finish(app, app.handle(apply))
        self.assertTrue(done.get("applied"), done)
        self.assertEqual(provider.cart["count"], 3)
        self.assertEqual(provider.writes(), 2)

    def test_a_short_apply_budget_still_writes_without_deferring_forever(self):
        store, provider, app, prepared = self.household(approved=12)
        app.products_apply_budget_seconds = 120
        result, calls = self.finish(app, app.handle({"operation": "products", **prepared["partial_apply_arguments"],
                                                     "cart_change_requested": True}))
        self.assertTrue(result.get("partial_applied"), result)
        self.assertLessEqual(calls, 3)
        self.assertEqual(provider.cart["count"], 12)


    def test_a_context_change_that_rereads_every_product_can_defer_the_write_again(self):
        store, provider, app, prepared = self.household(approved=12)
        arguments = {"operation": "products", **prepared["partial_apply_arguments"], "cart_change_requested": True}
        first = app.handle(arguments)
        self.assertEqual(first["validation_progress"]["pending_cart_write"], 1)
        reads = provider.reads()
        with store.locked() as state:  # changes the product context, not the selection
            state["recurring_fulfilled"] = {"unrelated": "2026-11-01"}
        second = app.handle({"operation": "products", **first["continue_arguments"]})
        self.assertEqual(provider.reads() - reads, 12)
        self.assertEqual(second["status"], "validating", second)
        self.assertEqual(second["validation_progress"]["pending_cart_write"], 1)
        self.assertEqual(provider.writes(), 0)
        third = app.handle({"operation": "products", **second["continue_arguments"]})
        self.assertTrue(third.get("partial_applied"), third)
        self.assertEqual(provider.cart["count"], 12)


class MenyPrepareWindowTests(unittest.TestCase):
    def test_a_menu_whose_reads_outlast_five_minutes_still_finishes_preparing(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        clock = FakeClock()
        for module in (planning_operations, product_planner, service):
            patcher = mock.patch.object(module, "time", clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        store = StateStore(Path(temp.name), {"instance": "test", "household": "Test", "provider": "meny",
                                             "profile_overrides": {}})
        menu = {"menu_id": "menu_window", "revision": 1, "digest": "d" * 64, "phase": "draft",
                "dishes": [{"shopping_requirements": [
                    {"item": f"Lettmelk {number}", "quantity": 1, "unit": "l", "scalable": True}
                    for number in range(1, 31)]}], "salads": []}
        with store.locked() as state:
            state["menu"] = deepcopy(menu)
        provider = SlowMeny(clock)
        provider.seconds_per_read = 20  # a MENY search plus its product details
        app = Application(store, provider, mock.Mock())
        start = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
        app._now = lambda: start + timedelta(seconds=clock.now - 1000)
        result = app.handle({"operation": "products", "action": "prepare", "menu_ref": app._cart_menu_ref(menu)})
        calls = 1
        while result.get("continue_arguments"):
            self.assertLess(calls, 8, "preparation restarted before its last read")
            clock.now += 60  # the agent's own time between calls
            result = app.handle({"operation": "products", **result["continue_arguments"]})
            calls += 1
        self.assertEqual(provider.reads(), 30)

    def test_preparation_finishes_with_persistently_failing_searches_unresolved(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        clock = FakeClock()
        for module in (planning_operations, product_planner, service):
            patcher = mock.patch.object(module, "time", clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        store = StateStore(Path(temp.name), {"instance": "test", "household": "Test", "provider": "meny",
                                             "profile_overrides": {}})
        menu = {"menu_id": "menu_failing", "revision": 1, "digest": "e" * 64, "phase": "draft",
                "dishes": [{"shopping_requirements": [
                    {"item": f"Lettmelk {number}", "quantity": 1, "unit": "l", "scalable": True}
                    for number in range(1, 7)]}], "salads": []}
        with store.locked() as state:
            state["menu"] = deepcopy(menu)
        provider = SlowMeny(clock)
        failing = {"Lettmelk 2", "Lettmelk 5"}  # e.g. a search page that never renders
        original = provider.call

        def call(tool_name, arguments, **kwargs):
            if tool_name == "product_search" and arguments["queries"][0].casefold() in {item.casefold() for item in failing}:
                provider.calls.append((tool_name, deepcopy(arguments)))
                raise MenyBrowserError("MENY search results did not finish rendering",
                                       failure_class="adapter_rejected", source="adapter_stderr")
            return original(tool_name, arguments, **kwargs)

        provider.call = call
        app = Application(store, provider, mock.Mock())
        result = app.handle({"operation": "products", "action": "prepare", "menu_ref": app._cart_menu_ref(menu)})
        calls = 1
        while result.get("continue_arguments"):
            self.assertLess(calls, 8, "preparation kept retrying failing searches")
            result = app.handle({"operation": "products", **result["continue_arguments"]})
            calls += 1
        searched = {row["item"] for row in result["product_plan"]["requirements"]
                    if (row.get("observation") or {}).get("products")}
        self.assertEqual(searched, {f"Lettmelk {number}" for number in range(1, 7)} - failing)
        attempts = [arguments["queries"][0].casefold() for name, arguments in provider.calls if name == "product_search"]
        for item in failing:
            self.assertGreaterEqual(attempts.count(item.casefold()), 2)


if __name__ == "__main__":
    unittest.main()
