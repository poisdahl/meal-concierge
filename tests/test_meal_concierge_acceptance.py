"""Complete household workflows and regressions from the independent review."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore, cart_summary
from service import Application
from test_meal_concierge import CONFIG, FakeBrowser, MutableFakeOda
import test_meal_concierge as flow_fixture
from test_meal_concierge_recipes import full_recipe, menu


class ReviewAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name), {**CONFIG, "provider": "oda"})
        self.provider = MutableFakeOda()
        self.browser = FakeBrowser()
        self.browser.oda = self.provider
        self.app = Application(self.store, self.provider, self.browser)
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})

    def save_menu(self, name="Dinner", **extra):
        return self.app.handle({"operation": "menu", "action": "save", "menu": menu("2026-W40", full_recipe(name)), **extra})["menu"]

    def test_old_menu_requirements_never_mutate_new_menu_cart(self):
        old = self.save_menu()
        old_ref = self.app._cart_menu_ref(old)
        self.save_menu("Replacement", menu_ref=old_ref)
        before = deepcopy(self.provider.cart)
        with self.assertRaisesRegex(HouseholdError, "stale"):
            self.app.handle({"operation": "cart", "action": "sync", "menu_ref": old_ref,
                             "requirements": [{"product_id": "10", "product_name": "Old dinner product", "quantity": 2}]})
        self.assertEqual(before, self.provider.cart)

    def test_missing_menu_reference_rejected_before_provider_dispatch(self):
        self.save_menu()
        calls = len(self.provider.calls)
        with self.assertRaisesRegex(HouseholdError, "exact menu_ref"):
            self.app.handle({"operation": "cart", "action": "sync", "requirements": [{"product_id": "10", "product_name": "Pasta", "quantity": 2}]})
        self.assertEqual(calls, len(self.provider.calls))

    def test_profile_and_recurring_invalid_writes_are_atomic(self):
        self.store.update_profile({"diet": {"leafy_green_days": [1, 4]}})
        before = self.store.read()
        for change in ({"meals": {"portions": 0}}, {"meals": {"people": "two"}}, {"diet": {"avoid": "fish"}},
                       {"diet": {"leafy_green_days": [0, 8]}}, {"diet": {"leafy_green_days": [5, 4]}},
                       {"diet": {"leafy_green_days": ["Monday", "Friday"]}}):
            with self.subTest(change=change), self.assertRaises(HouseholdError):
                self.app.handle({"operation": "profile", "action": "update", "changes": change})
            self.assertEqual(before, self.store.read())
        with self.assertRaises(HouseholdError):
            self.app.handle({"operation": "recurring", "action": "add", "item": {
                "product_id": "10", "product_name": "Milk", "quantity": 1,
                "schedule": {"every": 0, "unit": "weeks"}}})
        self.assertEqual(before, self.store.read())

    def test_recurring_intervals_keep_anchor_across_restart(self):
        with mock.patch("service.now", return_value=datetime(2026, 9, 1, tzinfo=timezone.utc)):
            for product, unit in (("10", "weeks"), ("20", "months")):
                self.app.handle({"operation": "recurring", "action": "add", "item": {
                    "product_id": product, "product_name": "Fixed item", "quantity": 1,
                    "schedule": {"every": 2, "unit": unit}}})
        reopened = Application(StateStore(Path(self.temp.name), {**CONFIG, "provider": "oda"}), self.provider, self.browser)
        for when, expected in (("2026-09-01", {"10", "20"}), ("2026-09-08", {"20"}), ("2026-09-15", {"10", "20"}), ("2026-10-01", {"10"}), ("2026-11-01", {"10", "20"})):
            due = reopened.handle({"operation": "recurring", "action": "due", "date": when})["due"]
            self.assertEqual(expected, {item["product_id"] for item in due}, when)

    def test_recipe_reference_defaults_to_household_portions(self):
        saved = self.app.handle({"operation": "recipes", "action": "save", "recipe": full_recipe(), "idempotency_key": "portion-recipe"})["recipe"]
        result = self.app.handle({"operation": "menu", "action": "save", "menu": menu("2026-W40", {"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}})})["menu"]
        self.assertEqual(2, result["dishes"][0]["portions"])
        self.assertEqual({"numerator": 200, "denominator": 1}, result["dishes"][0]["shopping_requirements"][0]["quantity"])

    def test_cart_ready_continues_to_confirmed_order_after_restart(self):
        case = flow_fixture.FlowTests("test_cart_ready_occurrence_must_be_carried_into_manual_checkout")
        case.setUp()
        self.addCleanup(case.tearDown)
        case.test_cart_ready_occurrence_must_be_carried_into_manual_checkout()
        pending = case.store.read()["pending_checkout"]
        reopened = Application(StateStore(case.store.directory, case.store.config), case.oda, case.browser)
        with mock.patch("service.now", return_value=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc)):
            result = reopened.handle({"operation": "checkout", "action": "confirm", "confirmation_id": pending["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertEqual(1, case.browser.checkout_clicks)

    def test_waiting_meny_discovery_rechecks_pending_checkout(self):
        self.app.provider = "meny"
        acquired = threading.Event()
        errors = []
        def discover():
            acquired.set()
            try:
                self.app._provider_recipe_candidates("meny", "soup", 1)
            except HouseholdError as exc:
                errors.append(str(exc))
        self.app.browser_lock.acquire()
        worker = threading.Thread(target=discover)
        worker.start()
        self.assertTrue(acquired.wait(1))
        with self.store.locked() as state:
            state["pending_checkout"] = {"status": "awaiting_user_payment"}
        self.app.browser_lock.release()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(errors))
        self.assertFalse(any(tool == "recipe_search" for tool, _ in self.provider.calls))

    def test_upgrade_preserves_manual_cart_ready_confirmation(self):
        case = flow_fixture.FlowTests("test_cart_ready_occurrence_must_be_carried_into_manual_checkout")
        case.setUp()
        self.addCleanup(case.tearDown)
        case.test_cart_ready_occurrence_must_be_carried_into_manual_checkout()
        import json
        state = case.store.read()
        state["version"] = 11
        state["menu_planning"].pop("prepared")
        state["pending_checkout"].pop("automatic_checkout")
        case.store.path.write_text(json.dumps(state))
        reopened = Application(StateStore(case.store.directory, case.store.config), case.oda, case.browser)
        with mock.patch("service.now", return_value=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc)):
            result = reopened.handle({"operation": "checkout", "action": "confirm", "confirmation_id": state["pending_checkout"]["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertEqual(case.browser.checkout_clicks, 1)
        self.assertTrue((case.store.directory / "state-v11.backup.json").exists())

    def test_pantry_aggregation_keeps_gross_and_exact_net_quantities(self):
        from product_planner import menu_requirements
        import test_meal_concierge_products as products
        value = products.menu({"item": "Flour", "quantity": 0.5, "unit": "kg"}, {"item": "Flour", "quantity": 300, "unit": "g"})
        source = lambda index: {"collection": "dishes", "recipe_index": 0, "ingredient_index": index}
        requirements, unresolved = menu_requirements(value, ingredient_decisions=[
            {"source": source(0), "action": "have_all"},
            {"source": source(1), "action": "have_quantity", "quantity": 0.1, "unit": "kg"},
        ])
        self.assertEqual(unresolved, [])
        self.assertEqual(requirements[0]["quantity"], {"numerator": 200, "denominator": 1})
        self.assertEqual(requirements[0]["gross_quantity"], {"numerator": 800, "denominator": 1})
        self.assertEqual(requirements[0]["confirmed_pantry_quantity"], {"numerator": 600, "denominator": 1})
        with self.assertRaisesRegex(HouseholdError, "compatible unit"):
            menu_requirements(value, ingredient_decisions=[{"source": source(0), "action": "have_quantity", "quantity": 1, "unit": "dl"}])

    def test_unknown_deposit_budget_uses_all_known_minimum_costs(self):
        import test_meal_concierge_products as products
        value = products.menu({"item": "A", "quantity": 1, "unit": "stk"}, {"item": "B", "quantity": 1, "unit": "stk"})
        requirements, _ = products.menu_requirements(value)
        observations = {}
        approvals = []
        for index, requirement in enumerate(requirements):
            offer = products.option(100, deposit=100 if index == 0 else 0)
            if index == 1:
                offer["mandatory_deposit_ore"] = None
                offer.pop("total_payable_ore")
            ref = str(index + 1)
            observations[requirement["requirement_id"]] = products.observation(requirement["item"], [products.product(ref, requirement["item"], 1, "count", [offer])])
            approvals.append({"requirement_id": requirement["requirement_id"], "candidate_refs": [ref]})
        plan = products.build_product_plan(provider="oda", binding={}, menu=value, observations=observations, candidate_approvals=approvals, price_mode="estimate", budget_ore=250)
        self.assertEqual(plan["status"], "needs_input")
        self.assertEqual(plan["budget_status"], "exceeded")
        self.assertIsNone(plan["totals"]["total_payable_ore"])
        self.assertEqual(plan["unresolved_requirements"][-1]["known_minimum_ore"], 300)

    def test_cart_drift_and_new_requirements_invalidate_product_approval(self):
        import test_meal_concierge_products as products
        case = products.ProductRuntimeTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        plan = case.prepare(approve=True)
        result = case.app.handle({"operation": "products", "action": "apply", "product_plan": plan, "product_plan_digest": plan["product_plan_digest"], "cart_change_requested": True})
        self.assertTrue(result["applied"])
        case.provider.cart["items"][0]["quantity"] = 2
        changed = case.app.handle({"operation": "cart", "action": "sync", "menu_ref": case.menu_ref, "requirements": [{"product_id": "10", "product_name": "Fixture Mel", "quantity": 3}]})
        self.assertFalse(changed["synced"])
        self.assertNotIn("product_plan_digest", case.store.read()["cart_plan"])

    def test_all_ingredients_at_home_finishes_without_provider_cart_write(self):
        import test_meal_concierge_products as products
        case = products.ProductRuntimeTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        plan = case.app.handle({"operation": "products", "action": "prepare", "menu_ref": case.menu_ref,
            "ingredient_decisions": [{"source": {"collection": "dishes", "recipe_index": 0, "ingredient_index": 0}, "action": "have_all"}]})["product_plan"]
        self.assertEqual(plan["status"], "prepared")
        result = case.app.handle({"operation": "products", "action": "apply", "product_plan": plan, "product_plan_digest": plan["product_plan_digest"], "cart_change_requested": True})
        self.assertTrue(result["nothing_to_buy"])
        status = case.app.handle({"operation": "status"})
        self.assertEqual(status["workflow"]["next_action"]["operation"], "menu")
        self.assertFalse(any(name == "manipulate_cart" for name, _ in case.provider.calls))

    def test_cooking_mcp_wrapper_and_feedback_reach_runtime(self):
        import runpy
        import types
        sys.path.insert(0, str(Path(__file__).parent))
        import test_meal_concierge_replanning as replanning
        case = replanning.ReplanningTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        class ToolServer:
            def __init__(self, *args, **kwargs): pass
            def tool(self, **kwargs): return lambda function: function
        module = types.ModuleType("mcp.server.mcpserver")
        module.MCPServer = ToolServer
        exceptions = types.ModuleType("mcp.server.mcpserver.exceptions")
        exceptions.ToolError = RuntimeError
        with mock.patch.dict(sys.modules, {"mcp.server.mcpserver": module, "mcp.server.mcpserver.exceptions": exceptions}):
            surface = runpy.run_path(str(Path(__file__).resolve().parents[1] / "mcp_server.py"))
        for name in ("meal_concierge_cooking", "meal_concierge_feedback"):
            surface[name].__globals__["rpc"] = lambda operation, **arguments: case.app.handle({"operation": operation, **arguments})
        slot = case.menu["slots"][0]
        cooked = surface["meal_concierge_cooking"](action="mark_cooked", menu_id=case.menu["menu_id"], expected_revision=case.menu["revision"], slot_id=slot["slot_id"], idempotency_key="cooking-reported")
        self.assertTrue(cooked)
        target = case.app.handle({"operation": "menu", "action": "get"})["feedback_targets"][0]
        experience = surface["meal_concierge_feedback"](action="experience", target=target, experience={"actual_active_minutes": 27, "portion_fit": "right", "leftover_portions": 1}, idempotency_key="experience-reported")
        self.assertEqual(experience["event"]["kind"], "experience")
        inspected = case.app.handle({"operation": "feedback", "action": "inspect"})
        self.assertEqual(inspected["cooking_experiences"][0]["experience"]["actual_active_minutes"], 27)
        prepared = case.prepare(planner_input={"candidates": case.candidates[3:]})
        successor = case.apply(prepared)["menu"]
        assessment = case.app.handle({"operation": "menu", "action": "assess"})["assessment"]
        self.assertTrue(assessment["ready"])
        self.assertEqual(assessment["dinner_days"], {"expected": 3, "verified": 3})

    def test_mealie_crash_recovery_cleanup_and_new_intent_use_real_adapter(self):
        import test_meal_concierge_recipes as recipes_fixture
        from recipe_libraries import RecipeLibraryExternalMissingError
        fixture = recipes_fixture.MealieAdapterTests()
        fixture.setUp()
        adapter, _ = fixture.adapter(*fixture.capability_responses())
        capabilities = adapter.capabilities()
        records, counts = {}, {"POST": 0, "PATCH": 0, "DELETE": 0}
        def remote(method, path, **arguments):
            if path == "/api/users/self":
                return deepcopy(fixture.fixture["authenticated_user"])
            if method == "POST":
                counts[method] += 1
                slug = "fixture-import-" + str(counts[method])
                identifier = f"11111111-1111-4111-8111-{counts[method]:012d}"
                records[identifier] = {"id": identifier, "slug": slug, "name": arguments["body"]["name"], "updatedAt": "2026-09-05T00:00:00Z", "recipeIngredient": [], "recipeInstructions": [], "notes": [], "tags": [], "extras": {}, "description": "", "orgURL": None}
                return slug
            identifier = path.rsplit("/", 1)[-1]
            record = next((value for key, value in records.items() if identifier in {key, value["slug"]}), None)
            if record is None:
                raise RecipeLibraryExternalMissingError("exact fixture missing")
            if method == "PATCH":
                counts[method] += 1
                if counts[method] == 1:
                    raise OSError("crash before content write")
                record.update(deepcopy(arguments["body"]))
                record["tags"] = [{"name": tag} for tag in record["tags"]]
                record["updatedAt"] = "2026-09-05T00:01:00Z"
            if method == "DELETE":
                counts[method] += 1
                records.pop(record["id"])
            return deepcopy(record)
        adapter._request = remote
        adapter.capabilities = lambda: capabilities
        settings = {**CONFIG, "primary_recipe_library_id": "family-mealie", "recipe_libraries": [fixture.connection]}
        directory = Path(self.temp.name) / "recovery"
        def reopen():
            app = Application(StateStore(directory, settings), self.provider, self.browser, recipe_library_adapters={"family-mealie": adapter})
            app.handle({"operation": "setup", "action": "apply", "keep_current": True})
            return app
        app = reopen()
        ref = app.recipes.persist_discovery(full_recipe("Recoverable import", external_id="recoverable-fixture"))["discovery_ref"]
        intent = {"operation": "recipes", "action": "save", "discovery_ref": ref, "idempotency_key": "import-first"}
        # This create was already journaled by the previous external-primary release.
        app.recipes.begin_library_create(ref, "family-mealie", idempotency_key="import-first")
        failed = app.handle(intent)
        self.assertEqual(failed["status"], "uncertain")
        app = reopen()
        recovered = app.handle({"operation": "recipes", "action": "import_recovery", "operation_id": failed["operation_id"]})
        self.assertEqual(recovered["recovery"]["status"], "incomplete_import")
        self.assertEqual(counts, {"POST": 1, "PATCH": 1, "DELETE": 0})
        exact = recovered["recovery"]["library_recipe_ref"]
        # Editing the stub makes it ordinary user content and stops cleanup suggestions.
        records[exact["recipe_id"]]["description"] = "User work"
        changed = app.handle({"operation": "recipes", "action": "import_recovery", "operation_id": failed["operation_id"]})
        self.assertEqual(changed["recovery"]["status"], "unresolved")
        records[exact["recipe_id"]]["description"] = ""
        deletion = app.handle({"operation": "recipes", "action": "delete_prepare", "library_recipe_ref": exact, "operation_id": failed["operation_id"]})
        deleted = app.handle({"operation": "recipes", "action": "delete_confirm", "confirmation_id": deletion["confirmation_id"], "idempotency_key": "remove-exact-stub"})
        self.assertEqual(deleted["status"], "confirmed")
        closed = app.handle({"operation": "recipes", "action": "import_recovery", "operation_id": failed["operation_id"], "deletion_operation_id": deleted["operation_id"]})
        self.assertEqual(closed["error_code"], "incomplete_import_removed")
        self.assertEqual(app.handle(intent)["status"], "failed")
        saved = app.handle({**intent, "idempotency_key": "import-new-explicit-intent"})
        self.assertEqual(saved["status"], "confirmed")
        self.assertEqual(saved["library_id"], "builtin")
        self.assertEqual(counts, {"POST": 1, "PATCH": 1, "DELETE": 1})

    def test_lost_mealie_post_response_recovers_marker_without_creating_again(self):
        import test_meal_concierge_recipes as recipes_fixture
        from recipe_library_mealie import _marker
        fixture = recipes_fixture.MealieAdapterTests()
        fixture.setUp()
        adapter, _ = fixture.adapter()
        operation = fixture.operation()
        stub = {"id": fixture.recipe_id, "slug": "exact-marker-stub", "name": "Hermes import " + _marker(operation["operation_id"]), "updatedAt": "2026-09-05T00:00:00Z", "recipeIngredient": [], "recipeInstructions": [], "tags": [], "notes": [], "extras": {}}
        calls = []
        def remote(method, path, **arguments):
            calls.append((method, path))
            if path == "/api/recipes":
                return {"items": [deepcopy(stub)], "page": 1, "per_page": 50, "total": 1, "total_pages": 1}
            return deepcopy(stub)
        adapter._request = remote
        found = adapter.inspect_incomplete_create(full_recipe("Missing response"), operation, {"provider_principal": "fixture", "provider_binding": "a" * 64})
        self.assertFalse(found["complete"])
        self.assertEqual(found["library_recipe_ref"]["recipe_id"], fixture.recipe_id)
        self.assertEqual([method for method, _ in calls], ["GET", "GET"])

    def test_pantry_completion_removes_owned_menu_goods_and_later_sync_invalidates_it(self):
        import test_meal_concierge_products as products
        case = products.ProductRuntimeTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        apply = lambda plan: case.app.handle({"operation": "products", "action": "apply", "product_plan": plan, "product_plan_digest": plan["product_plan_digest"], "cart_change_requested": True})
        self.assertTrue(apply(case.prepare(approve=True))["applied"])
        plan = case.app.handle({"operation": "products", "action": "prepare", "menu_ref": case.menu_ref,
            "ingredient_decisions": [{"source": {"collection": "dishes", "recipe_index": 0, "ingredient_index": 0}, "action": "have_all"}]})["product_plan"]
        result = apply(plan)
        self.assertTrue(result["applied"])
        self.assertTrue(result["nothing_to_buy"])
        self.assertTrue(result["cart_changed"])
        self.assertIn("product_plan_completion", case.store.read())
        self.assertEqual(case.provider.cart["items"], [])
        with case.store.locked() as state:
            state["product_plan_completion"] = {"menu_ref": case.menu_ref, "nothing_to_buy": True}
        case.app.handle({"operation": "cart", "action": "sync", "menu_ref": case.menu_ref,
            "requirements": [{"product_id": "10", "product_name": "Fixture Mel", "quantity": 2}]})
        self.assertNotIn("product_plan_completion", case.store.read())


if __name__ == "__main__":
    unittest.main()

class GroceryTopupTests(unittest.TestCase):
    def household(self, provider="oda"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return flow_fixture.CartPlanTests.app(temp.name, provider)

    def quantities(self, app, provider):
        return app._cart_lines(cart_summary(provider.cart))[0]

    def ensure(self, app, product, minimum):
        return app.handle({"operation": "cart", "action": "ensure", "requirements": [
            {"product_id": product, "product_name": "Exact selected product", "quantity": minimum}]})

    def approve(self, app, provider):
        state = app.store.read()
        question = app._cart_checkout_gate(cart_summary(provider.cart), state["menu"])
        if question:
            app.handle({"operation": "cart", "action": "reconcile", "menu_ref": app._cart_menu_ref(state["menu"]),
                        "decision": "keep_current", "cart_digest": question["cart_plan"]["cart_digest"]})

    def order(self, provider):
        provider.orders = [{"orderNumber": "topup-order", "grossAmount": 100.0,
            "deliveryDate": "2026-09-05", "deliverySlotDisplay": "Lør 5. sep 09:00 - 12:00",
            "deliveryAddressId": 7,
            "products": [{"product": {"id": 10, "name": "Cheese"}, "quantity": 2, "totalGrossAmount": "100.00"}]}]

    def test_menu_topups_are_preserved_across_repeat_restart_and_new_requirements(self):
        for name in ("oda", "meny"):
            with self.subTest(provider=name):
                store, provider, browser, app, product = self.household(name)
                flow_fixture.CartPlanTests.sync(app, product, 2)
                self.approve(app, provider)
                original_menu = store.read()["menu"]
                self.assertTrue(self.ensure(app, product, 3)["ensured"])
                self.assertEqual(self.quantities(app, provider)[product], 3)
                self.assertIsNone(app._cart_checkout_gate(cart_summary(provider.cart), original_menu))
                reopened = Application(StateStore(store.directory, store.config), provider, browser)
                writes = sum(tool == "manipulate_cart" for tool, _ in provider.calls)
                self.assertTrue(self.ensure(reopened, product, 3)["idempotent"])
                self.assertEqual(writes, sum(tool == "manipulate_cart" for tool, _ in provider.calls))
                flow_fixture.CartPlanTests.sync(reopened, product, 4)
                self.assertEqual(self.quantities(app, provider)[product], 5)
                self.assertEqual(store.read()["menu"], original_menu)

    def test_meny_large_topup_is_split_into_verified_batches(self):
        store, provider, _browser, app, product = self.household("meny")
        result = self.ensure(app, product, 6)
        self.assertTrue(result["ensured"])
        batches = [args["operations"] for tool, args in provider.calls if tool == "manipulate_cart"]
        self.assertEqual([sum(abs(row["quantity"]) for row in batch) for batch in batches], [2, 2, 1])
        self.assertEqual(self.quantities(app, provider)[product], 6)
        self.assertFalse(store.read().get("pending_cart_change"))

    def test_observed_topup_remains_extra_when_menu_requirements_change(self):
        store, provider, _, app, product = self.household()
        flow_fixture.CartPlanTests.sync(app, product, 2)
        self.approve(app, provider)
        original = provider.call

        def lose_write_reply(tool, args, **kwargs):
            result = original(tool, args, **kwargs)
            if tool == "manipulate_cart":
                raise HouseholdError("lost write reply")
            return result

        with mock.patch.object(provider, "call", side_effect=lose_write_reply):
            recovered = self.ensure(app, product, 3)
        self.assertTrue(recovered["cart_target_observed"])
        state = store.read()
        self.assertEqual(state["cart_plan"]["supplemental_quantities"], {product: 1})
        self.assertIsNone(app._cart_checkout_gate(cart_summary(provider.cart), state["menu"]))
        flow_fixture.CartPlanTests.sync(app, product, 4)
        self.assertEqual(self.quantities(app, provider), {product: 5})

    def test_reducing_same_menu_removes_only_acknowledged_additions(self):
        for observed in (True, False):
            with self.subTest(observed=observed):
                store, provider, _, app, product = self.household()
                original = provider.call

                def update_cart(tool, args, **kwargs):
                    result = original(tool, args, **kwargs)
                    if tool == "manipulate_cart" and observed:
                        raise HouseholdError("lost write reply")
                    return result

                with mock.patch.object(provider, "call", side_effect=update_cart):
                    first = flow_fixture.CartPlanTests.sync(app, product, 2, start_as_extra_product_ids=[product])
                self.assertTrue(first["synced"])
                self.assertEqual(self.quantities(app, provider), {product: 3})
                self.assertEqual(store.read()["cart_plan"]["added_quantities"], {} if observed else {product: 2})
                changed = flow_fixture.CartPlanTests.sync(app, product, 1)
                self.assertTrue(changed["synced"])
                self.assertEqual(self.quantities(app, provider), {product: 3 if observed else 2})
                self.assertEqual(changed["applied_operations"], [] if observed else [{"productId": int(product), "quantity": -1}])

    def test_accepted_unrelated_groceries_survive_same_menu_requirement_change(self):
        store, provider, _, app, product = self.household()
        flow_fixture.CartPlanTests.sync(app, product, 2)
        original = provider.call

        def concurrent_edit(tool, args, **kwargs):
            result = original(tool, args, **kwargs)
            if tool == "manipulate_cart":
                provider._mutate_cart({"operations": [{"productId": 99, "quantity": 2}]})
                raise HouseholdError("lost write reply")
            return result

        with mock.patch.object(provider, "call", side_effect=concurrent_edit):
            with self.assertRaisesRegex(HouseholdError, "lost write reply"):
                self.ensure(app, "20", 1)
        view = app.handle({"operation": "cart", "action": "get"})
        accepted = app.handle({"operation": "cart", "action": "accept_current",
                               "operation_id": view["operation_id"], "cart_digest": view["cart_digest"]})
        self.assertEqual(accepted["status"], "owner_accepted_cart")
        result = flow_fixture.CartPlanTests.sync(app, product, 1)
        self.assertTrue(result["synced"])
        self.assertEqual(result["applied_operations"], [{"productId": int(product), "quantity": -1}])
        self.assertEqual(self.quantities(app, provider), {product: 1, "20": 1, "99": 2})
        self.assertFalse(store.read().get("pending_cart_change"))

    def test_inconsistent_observed_count_keeps_raw_and_managed_write_pending(self):
        for managed, count_key in ((False, "productQuantityCount"), (True, "count")):
            with self.subTest(managed=managed):
                store, provider, _, app, product = self.household()
                original = provider.call
                writes = []

                def inconsistent_readback(tool, args, **kwargs):
                    result = original(tool, args, **kwargs)
                    if tool == "manipulate_cart":
                        writes.append(deepcopy(args))
                        raise HouseholdError("lost write reply")
                    if tool == "get_cart" and writes:
                        return {**result, count_key: 9}
                    return result

                with mock.patch.object(provider, "call", side_effect=inconsistent_readback):
                    if managed:
                        result = flow_fixture.CartPlanTests.sync(app, product, 2)
                        self.assertTrue(result["cart_write_pending"])
                    else:
                        with self.assertRaisesRegex(HouseholdError, "lost write reply"):
                            self.ensure(app, product, 2)
                    pending = store.read()["pending_cart_change"]
                    with self.assertRaises(HouseholdError):
                        app.handle({"operation": "cart", "action": "reconcile_change"})
                    self.assertEqual(store.read()["pending_cart_change"], pending)
                result = app.handle({"operation": "cart", "action": "reconcile_change"})
                self.assertTrue(result["cart_target_observed"])
                self.assertFalse(store.read().get("pending_cart_change"))
                self.assertEqual(len(writes), 1)

    def test_excluded_supplement_does_not_return_with_changed_menu_requirements(self):
        store, provider, _browser, app, product = self.household()
        flow_fixture.CartPlanTests.sync(app, product, 1)
        self.approve(app, provider)
        self.ensure(app, "20", 1)
        provider._mutate_cart({"operations": [{"productId": 99, "quantity": 1}]})
        question = app._cart_checkout_gate(cart_summary(provider.cart), store.read()["menu"])
        digest = question["cart_plan"]["cart_digest"]
        removed = app.handle({"operation": "cart", "action": "reconcile", "menu_ref": app._cart_menu_ref(store.read()["menu"]),
                              "cart_digest": digest, "decision": "keep_current", "exclude_product_ids": ["20"]})
        self.assertTrue(removed["reconciled"])
        flow_fixture.CartPlanTests.sync(app, product, 2)
        self.assertNotIn("20", self.quantities(app, provider))
        self.assertNotIn("20", store.read()["cart_plan"]["supplemental_quantities"])

    def test_late_provider_write_is_not_repeated_even_after_restart(self):
        store, provider, browser, app, product = self.household()
        original = provider.call
        queued = []
        def delayed(tool, args, **kwargs):
            if tool == "manipulate_cart":
                queued.append(deepcopy(args))
                raise HouseholdError("transport timed out before delayed acknowledgement")
            return original(tool, args, **kwargs)
        provider.call = delayed
        with self.assertRaisesRegex(HouseholdError, "timed out"):
            self.ensure(app, product, 2)
        reopened = Application(StateStore(store.directory, store.config), provider, browser)
        with self.assertRaisesRegex(HouseholdError, "reconcile_change"):
            self.ensure(reopened, product, 2)
        with self.assertRaisesRegex(HouseholdError, "still uncertain"):
            reopened.handle({"operation": "cart", "action": "reconcile_change"})
        with self.assertRaisesRegex(HouseholdError, "reconcile_change"):
            reopened.handle({"operation": "checkout", "action": "prepare"})
        self.assertEqual(len(queued), 1)
        # Use the observed cart as the shopping result, without asserting
        # which shopper made it or repeating our original delta.
        provider._mutate_cart(queued[0])
        provider.call = original
        result = reopened.handle({"operation": "cart", "action": "reconcile_change"})
        self.assertTrue(result["cart_target_observed"])
        receipt = store.read()["cart_change_receipts"][result["operation_id"]]
        self.assertEqual(receipt["dispatch_outcome"], "unknown")
        self.assertEqual(receipt["original_operation"]["expected"], {str(product): 2})
        self.assertFalse(store.read().get("pending_cart_change"))
        self.assertEqual(len(queued), 1)

    def test_checkout_prepare_rereads_after_lost_write_and_unavailable_read(self):
        store, provider, browser, app, product = self.household()
        original = provider.call
        writes = []
        def lose_replies(tool, args, **kwargs):
            if tool == "get_cart" and writes:
                raise HouseholdError("temporary read outage")
            result = original(tool, args, **kwargs)
            if tool == "manipulate_cart":
                writes.append(deepcopy(args))
                raise HouseholdError("lost write reply")
            return result

        provider.call = lose_replies
        with self.assertRaisesRegex(HouseholdError, "lost write reply"):
            self.ensure(app, product, 2)
        journal = store.read()["pending_cart_change"]
        provider.call = original
        reopened = Application(StateStore(store.directory, store.config), provider, browser)
        with mock.patch.object(reopened, "_checkout", return_value={"status": "prepared"}) as prepare:
            self.assertEqual(reopened.handle({"operation": "checkout"})["status"], "prepared")
            prepare.assert_called_once()
        self.assertEqual(len(writes), 1)
        final = store.read()
        self.assertFalse(final.get("pending_cart_change"))
        self.assertEqual(final["cart_change_receipts"][journal["operation_id"]]["original_operation"], journal)

    def test_meny_empty_cart_after_uncertain_batch_can_be_accepted_without_replay(self):
        store, provider, browser, app, product = self.household("meny")
        provider.cart.update(items=[], count=0, subtotal=0.0, total=0.0)
        other = "/varer/middagstilbehor/pasta/pasta-1234567891234"
        original_call = provider.call
        attempts = []
        ordinary = True
        def interrupted(tool, arguments, **kwargs):
            if tool == "manipulate_cart":
                attempts.append(deepcopy(arguments))
                raise HouseholdError("MENY cart change is uncertain")
            cart = original_call(tool, arguments, **kwargs)
            return {**cart, "ordinary_cart": ordinary} if tool == "get_cart" and arguments.get("observe_order_mode") else cart
        provider.call = interrupted
        with self.assertRaisesRegex(HouseholdError, "uncertain"):
            app.handle({"operation": "cart", "action": "change", "operations": [
                {"product_id": product, "quantity": 1}, {"product_id": other, "quantity": 1}]})
        journal = store.read()["pending_cart_change"]
        app = Application(StateStore(store.directory, store.config), provider, browser)
        with self.assertRaisesRegex(HouseholdError, "does not match the pending operation"):
            app.handle({"operation": "cart", "action": "reconcile_change", "operation_id": "old-operation"})
        shown = app.handle({"operation": "cart", "action": "reconcile_change", "response_view": "agent"})
        self.assertTrue(shown["accept_current_available"])
        self.assertTrue(shown["outcome_unknown"])
        self.assertEqual(shown["count"], 0)
        self.assertEqual(shown["operation_id"], journal["operation_id"])
        accept = {"operation": "cart", "action": "accept_current", "operation_id": shown["operation_id"],
                  "cart_digest": shown["cart_digest"]}
        with self.assertRaisesRegex(HouseholdError, "read the current pending"):
            app.handle({**accept, "operation_id": "different"})
        for field, value in [("order_change", {"order_id": "123"}), ("pending_checkout", {"status": "awaiting_confirmation"}),
                             ("pending_cancellation", {"status": "awaiting_confirmation"})]:
            with self.subTest(field=field):
                with store.locked() as state:
                    state[field] = value
                with self.assertRaises(HouseholdError):
                    app.handle(accept)
                with store.locked() as state:
                    state.pop(field)
        # A managed batch with a proven ending keeps its own readback path.
        for fields in [{"managed_menu": {"revision": 1}, "stopped_before_next_click": True},
                       {"native_cart_binding": {"ref": "bound"}}, {"order_change": {"order_id": "123"}}]:
            with self.subTest(journal_fields=sorted(fields)):
                with store.locked() as state:
                    state["pending_cart_change"] = {**journal, **fields}
                with self.assertRaises(HouseholdError):
                    app.handle(accept)
        with store.locked() as state:
            state["pending_cart_change"] = deepcopy(journal)
        ordinary = False
        with self.assertRaisesRegex(HouseholdError, "ordinary signed-in cart"):
            app.handle(accept)
        self.assertNotIn("accept_current_available", app.handle({"operation": "cart", "action": "get"}))
        ordinary = True
        provider._mutate_cart({"operations": [{"productId": product, "quantity": 1}]})
        with self.assertRaisesRegex(HouseholdError, "cart changed since"):
            app.handle(accept)
        provider._mutate_cart({"operations": [{"productId": product, "quantity": -1}]})
        self.assertEqual(app.handle({"operation": "cart", "action": "get"})["cart_digest"], shown["cart_digest"])
        self.assertEqual(store.read()["pending_cart_change"], journal)
        accepted = app.handle(accept)
        self.assertEqual(accepted["status"], "owner_accepted_cart")
        self.assertEqual(app.handle(accept)["status"], "previously_accepted")
        receipt = store.read()["cart_change_receipts"][shown["operation_id"]]
        self.assertEqual(receipt["original_operation"], journal)
        self.assertEqual(receipt["dispatch_outcome"], "unknown")
        self.assertEqual(len(attempts), 1)
        provider.call = original_call
        result = self.ensure(app, other, 1)
        self.assertTrue(result["ensured"])
        self.assertEqual(self.quantities(app, provider), {other: 1})

    def test_accept_current_after_partial_meny_clear_preserves_the_correct_shopping_scope(self):
        cases = [
            # saved menu, observed remaining units, original fence
            (False, 0, None), (False, 2, None), (True, 0, None), (True, 2, None),
            (True, 0, "unrelated"), (False, 0, "legacy"), (False, 2, "legacy"),
        ]
        for saved_menu, remaining, fence_kind in cases:
            with self.subTest(saved_menu=saved_menu, remaining=remaining, fence=fence_kind):
                store, provider, browser, app, product = self.household("meny")
                if not saved_menu:
                    with store.locked() as state:
                        state.pop("menu")
                self.ensure(app, product, 4)
                prior = {"menu_ref": app._cart_menu_ref(store.read().get("menu")), "started_at": "earlier"}
                if fence_kind == "unrelated":
                    with store.locked() as state:
                        state["managed_product_apply_fence"] = deepcopy(prior)
                original = provider.call
                removals = []
                def lose_last_batch(tool, arguments, **kwargs):
                    if tool == "manipulate_cart":
                        removals.append(deepcopy(arguments))
                        if len(removals) == 2:
                            if remaining == 0:
                                original(tool, arguments, **kwargs)
                            raise HouseholdError("lost final cart reply")
                    cart = original(tool, arguments, **kwargs)
                    if tool == "get_cart" and arguments.get("observe_order_mode"):
                        return {**cart, "ordinary_cart": True}
                    return cart
                provider.call = lose_last_batch
                digest = app.handle({"operation": "cart", "action": "get"})["cart_digest"]
                with self.assertRaisesRegex(HouseholdError, "lost final cart reply"):
                    app.handle({"operation": "cart", "action": "clear", "cart_digest": digest})
                if fence_kind == "legacy":
                    with store.locked() as state:
                        state["pending_cart_change"].pop("clear_id")
                        state["managed_product_apply_fence"] = prior
                journal = store.read()["pending_cart_change"]
                app = Application(StateStore(store.directory, store.config), provider, browser)
                shown = app.handle({"operation": "cart", "action": "reconcile_change"})
                self.assertEqual(shown["cart"]["count"], remaining)
                result = app.handle({"operation": "cart", "action": "accept_current",
                                     "operation_id": shown["operation_id"], "cart_digest": shown["cart_digest"]})
                self.assertEqual(result["status"], "owner_accepted_cart")
                state = store.read()
                receipt = state["cart_change_receipts"][shown["operation_id"]]
                self.assertEqual(receipt["original_operation"], journal)
                self.assertEqual(receipt["dispatch_outcome"], "unknown")
                self.assertEqual(len(removals), 2)
                self.assertNotIn("product_plan_completion", state)
                if not remaining:
                    self.assertNotIn("cart_plan", state)
                if fence_kind == "unrelated":
                    self.assertEqual(state["managed_product_apply_fence"], prior)
                elif saved_menu and remaining:
                    self.assertEqual(state["managed_product_apply_fence"]["clear_id"], journal["clear_id"])
                    self.assertIsNone(state["cart_plan"]["approved_cart_digest"])
                    self.assertIn("fresh saved-menu product review", result["next"])
                else:
                    self.assertNotIn("managed_product_apply_fence", state)
                    provider.call = original
                    self.assertTrue(self.ensure(app, product, remaining + 1)["ensured"])

    def test_accept_current_ordinary_cart_with_legacy_journal_and_stale_review(self):
        store, provider, _, app, product = self.household()
        before = self.quantities(app, provider)
        journal = {"before": before, "expected": {str(product): 4}, "operations": [{"productId": int(product), "quantity": 3}],
                   "provider": "oda", "order_change": None}
        with store.locked() as state:
            state["pending_cart_change"] = deepcopy(journal)
        def read():
            return app.handle({"operation": "cart", "action": "get", "response_view": "agent"})
        view = read()
        self.assertTrue(view["accept_current_available"])
        args = {"operation": "cart", "action": "accept_current", "cart_digest": view["cart_digest"],
                "operation_id": view["operation_id"]}
        with self.assertRaises(HouseholdError):
            app.handle({**args, "operation_id": "other"})
        provider._mutate_cart({"operations": [{"productId": int(product), "quantity": 1}]})
        with self.assertRaisesRegex(HouseholdError, "changed since"):
            app.handle(args)
        args["cart_digest"] = read()["cart_digest"]
        self.assertEqual(store.read()["pending_cart_change"], journal)
        with mock.patch.object(provider, "call", wraps=provider.call) as calls:
            self.assertEqual(app.handle(args)["status"], "owner_accepted_cart")
            self.assertEqual(app.handle(args)["status"], "previously_accepted")
            self.assertTrue(all(call.args[0] == "get_cart" for call in calls.call_args_list))
        self.assertEqual(store.read()["cart_change_receipts"][args["operation_id"]]["original_operation"], journal)
        self.assertFalse(store.read().get("pending_cart_change"))

    def test_accept_physical_cart_after_original_menu_is_cleared(self):
        store, provider, _, app, product = self.household()
        original = provider.call
        def lose(tool, args, **kwargs):
            if tool == "manipulate_cart":
                raise HouseholdError("lost cart reply")
            return original(tool, args, **kwargs)
        with mock.patch.object(provider, "call", side_effect=lose):
            result = flow_fixture.CartPlanTests.sync(app, product, 2)
        self.assertTrue(result["cart_write_pending"])
        journal = store.read()["pending_cart_change"]
        app.handle({"operation": "menu", "action": "clear", "menu_ref": journal["managed_menu"]})
        shown = app.handle({"operation": "cart", "action": "get", "response_view": "agent"})
        with mock.patch.object(provider, "call", wraps=provider.call) as reads:
            accepted = app.handle({"operation": "cart", "action": "accept_current",
                                   "cart_digest": shown["cart_digest"], "operation_id": shown["operation_id"]})
            self.assertEqual([call.args[0] for call in reads.call_args_list], ["get_cart"])
        self.assertEqual(accepted["status"], "owner_accepted_cart")
        state = store.read()
        self.assertIsNone(state.get("menu"))
        self.assertIsNone(state.get("pending_cart_change"))
        self.assertEqual(state["cart_change_receipts"][shown["operation_id"]]["original_operation"], journal)

    def test_matching_legacy_cart_with_unsubmitted_cancellation_recovers(self):
        store, provider, _, app, product = self.household()
        quantities = self.quantities(app, provider)
        journal = {"before": {}, "expected": quantities, "operations": [{"productId": int(product), "quantity": 1}],
                   "provider": "oda", "order_change": None}
        cancellation = {"status": "awaiting_confirmation", "order_id": "synthetic-other"}
        with store.locked() as state:
            state.update(pending_cart_change=journal, pending_cancellation=cancellation)
        result = app.handle({"operation": "cart", "action": "reconcile_change"})
        self.assertTrue(result["cart_target_observed"])
        self.assertEqual(store.read()["pending_cancellation"], cancellation)

    def test_completed_raw_batch_with_concurrent_edit_requires_review_after_restart(self):
        store, provider, browser, app, product = self.household()
        flow_fixture.CartPlanTests.sync(app, product, 1)
        self.approve(app, provider)
        original = provider.call
        def concurrent_edit(tool, args, **kwargs):
            result = original(tool, args, **kwargs)
            if tool == "manipulate_cart":
                provider._mutate_cart({"operations": [{"productId": 99, "quantity": 1}]})
            return result
        provider.call = concurrent_edit
        with self.assertRaisesRegex(HouseholdError, "needs readback"):
            self.ensure(app, product, 2)
        pending = store.read()["pending_cart_change"]
        self.assertTrue(pending["dispatch_finished"])
        reopened = Application(StateStore(store.directory, store.config), provider, browser)
        observed = reopened.handle({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(observed["operation_id"], pending["operation_id"])
        self.assertFalse(observed["reconciled"])
        self.assertEqual(observed["status"], "needs_input")
        current = store.read()
        self.assertFalse(current.get("pending_cart_change"))
        self.assertEqual(current["cart_change_receipts"][pending["operation_id"]]["status"], "completed_with_cart_drift")
        self.assertFalse(current["cart_plan"].get("supplemental_quantities"))

    def test_terminal_managed_reply_survives_lost_readback_for_both_retailers(self):
        for merchant in ("oda", "mathem"):
            with self.subTest(merchant=merchant):
                store, provider, browser, app, product = self.household(merchant)
                original = provider.call
                dispatched = False
                def lose_read(tool, args, **kwargs):
                    nonlocal dispatched
                    if dispatched and tool == "get_cart":
                        raise HouseholdError("synthetic read unavailable")
                    result = original(tool, args, **kwargs)
                    if tool == "manipulate_cart":
                        dispatched = True
                    return result
                provider.call = lose_read
                outcome = flow_fixture.CartPlanTests.sync(app, product, 2)
                self.assertTrue(outcome["cart_write_pending"])
                pending = store.read()["pending_cart_change"]
                self.assertTrue(pending["dispatch_finished"])
                provider.call = original
                reopened = Application(StateStore(store.directory, store.config), provider, browser)
                observed = reopened.handle({"operation": "cart", "action": "reconcile_change"})
                self.assertTrue(observed["reconciled"])
                self.assertEqual(observed["operation_id"], pending["operation_id"])
                self.assertFalse(store.read().get("pending_cart_change"))
                self.assertEqual(store.read()["cart_plan"]["status"], "needs_input")

    def test_existing_oda_order_counts_ordered_goods_and_real_editability(self):
        store, provider, _browser, app, _product = self.household()
        self.order(provider)
        provider.cart.update(items=[], count=0, subtotal=0.0)
        # Provider still permits this specific order after a nominal 20:00 cutoff.
        with mock.patch("service.now", return_value=datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)):
            self.assertTrue(app.handle({"operation": "orders", "action": "change_begin", "order_id": "topup-order"})["editing"])
            self.assertTrue(self.ensure(app, "10", 2)["idempotent"])
            self.ensure(app, "10", 3)
            self.assertEqual(self.quantities(app, provider), {"10": 1})
            self.assertTrue(self.ensure(app, "10", 3)["idempotent"])
        provider.tracking = "paid_and_not_modifiable"
        with self.assertRaisesRegex(HouseholdError, "no longer allows"):
            self.ensure(app, "20", 1)
        self.assertEqual(self.quantities(app, provider), {"10": 1})
        self.assertTrue(app.handle({"operation": "orders", "action": "change_abort", "order_id": "topup-order", "retain_cart": True})["cart_retained"])
        self.assertIsNone(store.read()["order_change"])
        with self.assertRaisesRegex(HouseholdError, "not currently modifiable"):
            app.handle({"operation": "orders", "action": "change_begin", "order_id": "topup-order"})

    def test_existing_cart_is_preserved_and_requires_exact_destination_approval(self):
        store, provider, _browser, app, _product = self.household()
        self.order(provider)
        baseline = deepcopy(provider.cart)
        question = app.handle({"operation": "orders", "action": "change_begin", "order_id": "topup-order"})
        self.assertTrue(question["cart_confirmation_required"])
        self.assertEqual(provider.cart, baseline)
        self.assertIsNone(store.read()["order_change"])
        request = {"operation": "orders", "action": "change_begin", "order_id": "topup-order", "cart_digest": question["cart_digest"]}
        self.assertTrue(app.handle(request)["editing"])
        self.assertTrue(app.handle({"operation": "orders", "action": "change_abort", "order_id": "topup-order"})["aborted"])
        self.assertEqual(provider.cart, baseline)
        provider._mutate_cart({"operations": [{"productId": 99, "quantity": 1}]})
        self.assertTrue(app.handle(request)["cart_confirmation_required"])
        request["cart_digest"] = app._cart_digest(self.quantities(app, provider))
        app.handle(request)
        provider._mutate_cart({"operations": [{"productId": 100, "quantity": 1}]})
        for operation in ({"operation": "checkout", "action": "prepare"},
                          {"operation": "cart", "action": "ensure", "requirements": [{"product_id": "10", "product_name": "Cheese", "quantity": 1}]}):
            with self.assertRaisesRegex(HouseholdError, "changed outside"):
                app.handle(operation)
        self.assertIn("100", self.quantities(app, provider))

    def test_meny_disabled_order_edit_controls_do_not_dispatch(self):
        client = flow_fixture.MenyClientTests().client()
        client._get_order = mock.Mock(return_value={"orderNumber": "99990001", "code": "TEST-CODE"})
        client._eval = mock.Mock(return_value={"ready": False})
        client._invoke = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "cannot be changed now"):
            client.begin_order_change("99990001")
        client._invoke.assert_not_called()

    def test_parallel_minimum_requests_and_uncertain_second_meny_batch(self):
        store, provider, browser, app, product = self.household("meny")
        original = provider.call
        writes = []
        read_lost = False
        def interrupted(tool, args, **kwargs):
            nonlocal read_lost
            if tool == "manipulate_cart":
                writes.append(deepcopy(args))
            elif tool == "get_cart" and len(writes) == 2 and not read_lost:
                read_lost = True
                raise HouseholdError("second completed batch readback lost")
            return original(tool, args, **kwargs)
        provider.call = interrupted
        with self.assertRaisesRegex(HouseholdError, "needs readback"):
            self.ensure(app, product, 6)
        self.assertEqual(self.quantities(app, provider)[product], 5)
        reopened = Application(StateStore(store.directory, store.config), provider, browser)
        self.assertEqual(reopened.handle({"operation": "status"})["workflow"]["next_action"]["action"], "reconcile_change")
        reopened.handle({"operation": "cart", "action": "reconcile_change"})
        provider.call = original
        results, errors = [], []
        def ensure():
            try:
                results.append(self.ensure(reopened, product, 6))
            except Exception as error:
                errors.append(str(error))
        threads = [threading.Thread(target=ensure) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(self.quantities(app, provider)[product], 6)
        self.assertEqual(store.read()["cart_plan"]["supplemental_quantities"][product], 5)

    def test_missing_supplement_needs_review_and_restore_survives_new_menu(self):
        store, provider, _browser, app, product = self.household()
        provider.cart.update(items=[], count=0, subtotal=0.0)
        flow_fixture.CartPlanTests.sync(app, product, 1)
        self.approve(app, provider)
        self.ensure(app, product, 3)
        provider._mutate_cart({"operations": [{"productId": 10, "quantity": -2}]})
        question = app._cart_checkout_gate(cart_summary(provider.cart), store.read()["menu"])
        self.assertEqual(question["cart_plan"]["items"][0]["missing_quantity"], 2)
        restored = app.handle({"operation": "cart", "action": "reconcile", "decision": "restore_missing",
                              "menu_ref": app._cart_menu_ref(store.read()["menu"]), "cart_digest": question["cart_plan"]["cart_digest"]})
        self.assertTrue(restored["reconciled"])
        plan = store.read()["cart_plan"]
        self.assertEqual(plan["added_quantities"][product], 1)
        self.assertEqual(plan["supplemental_quantities"][product], 2)
        with store.locked() as state:
            state["menu"] = flow_fixture.CartPlanTests.menu(revision=2, digest="b" * 64)
        flow_fixture.CartPlanTests.sync(app, product, 2)
        self.assertEqual(store.read()["cart_plan"]["supplemental_quantities"][product], 2)
        self.assertEqual(app._cart_target(store.read()["cart_plan"])[product], 4)

    def test_meny_proven_stop_recovers_partial_without_resending(self):
        from meny import MenyCartStoppedError
        for applied in (0, 1):
            with self.subTest(applied=applied):
                store, provider, _browser, app, product = self.household("meny")
                original = provider.call
                def stopped(tool, args, **kwargs):
                    if tool == "manipulate_cart":
                        acknowledged = [{"productId": product, "quantity": 1}] if applied else []
                        if acknowledged:
                            provider._mutate_cart({"operations": acknowledged})
                        raise MenyCartStoppedError("product control is unavailable", acknowledged)
                    return original(tool, args, **kwargs)
                provider.call = stopped
                result = self.ensure(app, product, 3)
                self.assertTrue(result["stopped"])
                self.assertFalse(result["ensured"])
                self.assertFalse(store.read().get("pending_cart_change"))
                self.assertEqual(self.quantities(app, provider)[product], 1 + applied)
                provider.call = original
                self.ensure(app, product, 3)
                self.assertEqual(self.quantities(app, provider)[product], 3)

    def test_actual_meny_adapter_distinguishes_unavailable_from_uncertain_click(self):
        from meny import MenyCartStoppedError
        client = flow_fixture.MenyClientTests().client()
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._sleep = mock.Mock()
        client._product_control = mock.Mock(return_value={"authenticated": True, "ready": False})
        client._click_cart_control = mock.Mock()
        with self.assertRaises(MenyCartStoppedError) as caught:
            client._change_cart({"operations": [{"productId": flow_fixture.MENY_PRODUCT, "quantity": 1}]})
        self.assertEqual(caught.exception.applied_operations, [])
        client._click_cart_control.assert_not_called()
        client._product_control.return_value = {"authenticated": True, "ready": True, "quantity": 1, "label": "add"}
        def dispatched_then_failed(_product, _label, before_dispatch, _same_control):
            before_dispatch()
            raise HouseholdError("transport response lost")
        client._click_cart_control.side_effect = dispatched_then_failed
        with self.assertRaises(HouseholdError) as caught:
            client._change_cart({"operations": [{"productId": flow_fixture.MENY_PRODUCT, "quantity": 1}]})
        self.assertNotIsInstance(caught.exception, MenyCartStoppedError)

    def test_changed_cart_before_first_managed_batch_requests_fresh_review_without_dispatch(self):
        store, provider, _browser, app, product = self.household("meny")
        original = app._cart_provider_call
        reads = []
        def changed(tool, arguments, *, deadline):
            self.assertEqual(tool, "get_cart")
            reads.append(tool)
            if len(reads) == 3:
                provider._mutate_cart({"operations": [{"productId": product, "quantity": 1}]})
            if len(reads) > 3:
                raise HouseholdError("later inspection unavailable")
            return original(tool, arguments, deadline=deadline)
        app._cart_provider_call = changed
        result = flow_fixture.CartPlanTests.sync(app, product, 6)
        self.assertEqual(result["reason"], "cart_changed_before_first_batch")
        self.assertEqual(result["status"], "needs_input")
        self.assertFalse(result["cart_write_pending"])
        self.assertFalse(store.read().get("pending_cart_change"))
        self.assertEqual(len(reads), 3)

    def test_managed_meny_typed_stop_records_only_earlier_completed_batches(self):
        from retail_mcp import RetailPreDispatchError
        for stop_at in (1, 2):
            with self.subTest(stop_at=stop_at):
                store, provider, _browser, app, product = self.household("meny")
                original = app._cart_provider_call
                attempts = []
                def stop(tool, arguments, *, deadline):
                    if tool == "manipulate_cart":
                        attempts.append(deepcopy(store.read()["pending_cart_change"]))
                        if len(attempts) == stop_at:
                            raise RetailPreDispatchError("deadline before dispatch")
                    return original(tool, arguments, deadline=deadline)
                app._cart_provider_call = stop
                if stop_at == 1:
                    with self.assertRaises(RetailPreDispatchError):
                        flow_fixture.CartPlanTests.sync(app, product, 6)
                else:
                    stopped = flow_fixture.CartPlanTests.sync(app, product, 6)
                    self.assertFalse(stopped["synced"])
                    self.assertEqual(stopped["reason"], "cart_write_stopped_before_click")
                    self.assertNotIn("outcome_unknown", stopped)
                    self.assertEqual(store.read()["cart_plan"]["added_quantities"], {product: 2})
                self.assertFalse(store.read().get("pending_cart_change"))
                receipt = store.read()["cart_change_receipts"][attempts[-1]["operation_id"]]
                self.assertEqual(receipt["status"], "not_dispatched" if stop_at == 1 else "read_back")
                self.assertEqual(len(attempts), stop_at)
                self.assertEqual(self.quantities(app, provider)[product], 1 + 2 * (stop_at - 1))

    def test_raw_meny_zero_click_proof_needs_no_post_stop_read(self):
        from meny import MenyCartStoppedError
        store, provider, _browser, app, product = self.household("meny")
        original = provider.call
        stopped = False
        def stop(tool, args, **kwargs):
            nonlocal stopped
            if tool == "manipulate_cart":
                stopped = True
                raise MenyCartStoppedError("no enabled item control")
            if tool == "get_cart" and stopped:
                self.fail("a zero-click terminal disposition does not need a second cart read")
            return original(tool, args, **kwargs)
        provider.call = stop
        result = self.ensure(app, product, 3)
        self.assertEqual(result["status"], "not_dispatched")
        self.assertFalse(store.read().get("pending_cart_change"))
        self.assertIn(result["operation_id"], store.read()["cart_change_receipts"])

    def test_stopped_meny_batch_keeps_journal_until_earlier_click_is_read_back(self):
        from meny import MenyCartStoppedError
        store, provider, _browser, app, product = self.household("meny")
        original = provider.call
        def delayed_prefix(tool, args, **kwargs):
            if tool == "manipulate_cart":
                raise MenyCartStoppedError("stopped before second click", [{"productId": product, "quantity": 1}])
            return original(tool, args, **kwargs)
        provider.call = delayed_prefix
        with self.assertRaisesRegex(HouseholdError, "still uncertain"):
            self.ensure(app, product, 3)
        self.assertTrue(store.read().get("pending_cart_change"))
        with self.assertRaisesRegex(HouseholdError, "reconcile_change"):
            self.ensure(app, product, 3)
        provider._mutate_cart({"operations": [{"productId": product, "quantity": 1}]})
        app.handle({"operation": "cart", "action": "reconcile_change"})
        provider.call = original
        self.ensure(app, product, 3)
        self.assertEqual(self.quantities(app, provider)[product], 3)

    def test_meny_cart_removal_preserves_the_dispatch_boundary(self):
        from meny import MenyCartStoppedError
        client = flow_fixture.MenyClientTests().client()
        product = flow_fixture.MENY_PRODUCT
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._sleep = mock.Mock()
        client._product_control = mock.Mock(return_value={"authenticated": True, "ready": False})
        client._read_cart = mock.Mock(return_value={"items": [{"product_id": product, "quantity": 1}]})
        client._click_cart_remove_control = mock.Mock(side_effect=HouseholdError("control unavailable"))
        with self.assertRaises(MenyCartStoppedError) as caught:
            client._change_cart({"operations": [{"productId": product, "quantity": -1}]})
        self.assertEqual(caught.exception.applied_operations, [])
        def lost_response(_product, _quantity, _code, before_dispatch):
            before_dispatch()
            raise HouseholdError("response lost")
        client._click_cart_remove_control.side_effect = lost_response
        with self.assertRaises(HouseholdError) as caught:
            client._change_cart({"operations": [{"productId": product, "quantity": -1}]})
        self.assertNotIsInstance(caught.exception, MenyCartStoppedError)
