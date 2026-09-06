"""Whole-week capacity through Application, using a bounded synthetic retailer."""
from copy import deepcopy
from datetime import date
from fractions import Fraction
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import batch_planning as bp
import menu_planning as mp
from core import HouseholdError, StateStore
from service import Application
from test_meal_concierge_products import observation, option, product

# Seven synthetic dinners for two people. Five distinct foods per dinner plus
# shared onion/garlic: 49 quantified rows aggregate to 37 shopping needs.
DINNERS = [
    [("salmon", 300), ("potatoes", 400), ("broccoli", 250), ("lemon", 60), ("yoghurt", 100)],
    [("chicken", 300), ("brown rice", 150), ("carrots", 200), ("peas", 150), ("ginger", 20)],
    [("chickpeas", 300), ("couscous", 150), ("courgette", 200), ("aubergine", 200), ("feta", 100)],
    [("cod", 300), ("barley", 150), ("leek", 200), ("celeriac", 200), ("cream", 100)],
    [("lentils", 200), ("tomatoes", 300), ("spinach", 150), ("mushrooms", 150), ("pasta", 150)],
    [("turkey", 300), ("tortillas", 200), ("avocado", 150), ("cabbage", 200), ("corn", 100)],
    [("tofu", 300), ("noodles", 150), ("pak choi", 200), ("bell pepper", 150), ("cucumber", 150)],
]
DINNERS = [rows + [("onion", 100), ("garlic", 10)] for rows in DINNERS]


def recipe(name, rows, *, undecided=None):
    ingredients = [{"item": item, "raw": f"{amount} g {item}", "quantity": amount, "unit": "g"} for item, amount in rows]
    if undecided:
        ingredients.append({"item": undecided, "raw": f"5 g {undecided}", "quantity": 5, "unit": "g",
                            "optional": undecided == "sesame", "pantry": undecided != "sesame"})
    return {"schema_version": 2, "name": name, "portions": 2, "ingredients": ingredients,
            "steps": ["Cook the synthetic dinner."], "source": {"kind": "user", "relationship": "user_supplied"},
            "rights": {"storage": "full"}}


class Retailer:
    def __init__(self):
        self.catalog = {}
        self.calls = []
        self.quantities = {}
        self.fail_query = None
        self.on_call = None

    def probe(self):
        return {"protocol_version": "fixture", "server": {"name": "synthetic"}, "tool_count": 3}

    def entry(self, query):
        if query not in self.catalog:
            ref = str(len(self.catalog) * 10 + 1)
            size = 50 if query == "garlic" else 200
            # Five candidates; the first strictly dominates equal-size others.
            self.catalog[query] = observation(query, [product(str(int(ref) + i), query, size, "g", [option(100 + i * 50)]) for i in range(5)])
        return self.catalog[query]

    def cart(self):
        names = {p["product_ref"]: p["name"] for obs in self.catalog.values() for p in obs["products"]}
        items = [{"product_id": int(ref), "name": names[ref], "quantity": count, "price": count} for ref, count in self.quantities.items() if count]
        return {"items": items, "count": sum(self.quantities.values()), "subtotal": sum(self.quantities.values()), "total": sum(self.quantities.values())}

    def call(self, tool, arguments, **kwargs):
        self.calls.append((tool, deepcopy(arguments), kwargs))
        if self.on_call:
            self.on_call(tool, arguments)
        if tool == "product_search":
            query = arguments["queries"][0]
            if query == self.fail_query:
                raise HouseholdError("synthetic source timeout")
            return deepcopy(self.entry(query))
        if tool == "get_cart":
            return self.cart()
        if tool == "manipulate_cart":
            for operation in arguments["operations"]:
                ref = str(operation["productId"])
                self.quantities[ref] = self.quantities.get(ref, 0) + operation["quantity"]
            return self.cart()
        raise AssertionError(tool)


class ProductCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name), {"instance": "capacity", "household": "Synthetic", "provider": "oda", "profile_overrides": {}})
        self.provider = Retailer()
        self.app = Application(self.store, self.provider, object())
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})
        patch = mock.patch.object(Application, "_household_today", return_value=date(2026, 9, 7))
        patch.start()
        self.addCleanup(patch.stop)

    def save_recipes(self, values):
        return [{"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}, "portions": 2}
                for index, value in enumerate(values)
                for saved in [self.app.handle({"operation": "recipes", "action": "save", "recipe": value, "idempotency_key": f"recipe-{index}"})["recipe"]]]

    def save_week(self, *, unresolved=False):
        refs = self.save_recipes([recipe(f"Dinner {i}", rows, undecided=("salt", "sugar", "sesame")[i] if unresolved and i < 3 else None) for i, rows in enumerate(DINNERS)])
        self.menu = self.app.handle({"operation": "menu", "action": "save", "menu": {"week": "2026-W37", "dishes": refs, "salads": []}})["menu"]
        return refs

    def prepare(self, **kwargs):
        return self.app.handle({"operation": "products", "action": "prepare", "menu_ref": self.app._cart_menu_ref(self.menu), **kwargs})["product_plan"]

    def approvals(self, plan):
        return [{"requirement_id": row["requirement_id"], "candidate_refs": [p["product_ref"] for p in self.provider.entry(row["identity"])["products"]]} for row in plan["requirements"]]

    def complete(self):
        initial = self.prepare()
        return self.prepare(candidate_approvals=self.approvals(initial))

    def apply(self, plan):
        return self.app.handle({"operation": "products", "action": "apply", "product_plan": plan,
                                "product_plan_digest": plan["product_plan_digest"], "cart_change_requested": True})

    def assert_totals(self, plan, dinners, *, pantry_onion=0):
        # Independent arithmetic: sum raw authored grams, subtract pantry once,
        # then round each compatible total up to known synthetic package size.
        grams = {}
        for rows in dinners:
            for name, amount in rows:
                grams[name] = grams.get(name, Fraction()) + Fraction(amount)
        grams["onion"] = grams.get("onion", Fraction()) - pantry_onion
        grams = {k: v for k, v in grams.items() if v > 0}
        counts = {name: math.ceil(amount / (50 if name == "garlic" else 200)) for name, amount in grams.items()}
        self.assertEqual(plan["status"], "prepared")
        self.assertEqual(len(plan["requirements"]), len(grams))
        self.assertEqual(plan["totals"]["package_count"], sum(counts.values()))
        self.assertEqual(plan["totals"]["total_payable_ore"], 100 * sum(counts.values()))
        for row in plan["requirements"]:
            self.assertEqual(Fraction(**row["quantity"]), grams[row["identity"]])
            self.assertEqual(row["selection"]["package_count"], counts[row["identity"]])
        return counts

    def test_full_week_unresolved_pantry_then_apply_all_37_needs(self):
        self.save_week(unresolved=True)
        first = self.prepare()
        self.assertEqual(len(first["requirements"]), 37)
        structural = [row for row in first["unresolved_requirements"] if "ingredient_index" in row]
        self.assertEqual({row["item"] for row in structural}, {"salt", "sugar", "sesame"})
        self.assertEqual(sum(len(row["sources"]) for row in first["requirements"]) + len(structural), 52)
        decisions = [{"source": {k: row[k] for k in ("collection", "recipe_index", "ingredient_index")},
                      "action": "omit" if row["item"] == "sesame" else "have_all"} for row in structural]
        decisions.append({"source": {"collection": "dishes", "recipe_index": 0, "ingredient_index": 5},
                          "action": "have_quantity", "quantity": {"numerator": 101, "denominator": 2}, "unit": "g"})
        plan = self.prepare(candidate_approvals=self.approvals(first), ingredient_decisions=decisions)
        counts = self.assert_totals(plan, DINNERS, pantry_onion=Fraction(101, 2))
        self.provider.calls.clear()
        result = self.apply(plan)
        self.assertTrue(result["applied"])
        self.assertEqual(result["price_verification"], "unchanged")
        self.assertEqual(len(self.provider.quantities), 37)
        self.assertEqual(sum(self.provider.quantities.values()), sum(counts.values()))
        self.assertEqual(sum(tool == "product_search" for tool, _, _ in self.provider.calls), 3 * 37)
        writes = [args for tool, args, _ in self.provider.calls if tool == "manipulate_cart"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(writes[0]["operations"]), 37)
        self.assertEqual(self.store.read()["cart_plan"]["product_plan_digest"], plan["product_plan_digest"])
        self.assertEqual(len({kw["deadline"] for _, _, kw in self.provider.calls}), 1)

    def test_partial_timeout_retains_successes_and_all_remaining_needs(self):
        self.save_week()
        self.provider.fail_query = "ginger"
        plan = self.prepare()
        self.assertEqual(len(plan["requirements"]), 37)
        self.assertEqual(sum("observation" in row for row in plan["requirements"]), 36)
        self.assertIn({"requirement_id": next(row["requirement_id"] for row in plan["requirements"] if row["identity"] == "ginger"), "item": "ginger", "reason": "provider_search_unavailable_or_scope_changed"}, plan["unresolved_requirements"])
        self.provider.calls.clear()
        with self.assertRaisesRegex(HouseholdError, "complete prepared"):
            self.apply(plan)
        self.assertEqual(self.provider.calls, [])

    def test_deadline_stops_dispatch_and_preserves_all_needs(self):
        self.save_week()
        clock = [1000.0]
        def advance(tool, args):
            clock[0] += 80
        self.provider.on_call = advance
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            plan = self.prepare()
        self.assertEqual(len(self.provider.calls), 3)
        self.assertTrue(all(kw["deadline"] == 1240 for _, _, kw in self.provider.calls))
        self.assertEqual(len(plan["requirements"]), 37)
        self.assertEqual(sum(r["reason"] == "provider_search_deadline" for r in plan["unresolved_requirements"]), 34)
        self.assertEqual(plan["status"], "needs_input")
        self.assertFalse(self.provider.quantities)

    def test_deadline_at_final_freshness_stage_never_writes(self):
        self.save_week()
        plan = self.complete()
        self.provider.calls.clear()
        clock = [1000.0]
        searches = [0]
        def advance(tool, args):
            if tool == "product_search":
                searches[0] += 1
                if searches[0] == 38:
                    clock[0] = 1240
        self.provider.on_call = advance
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            result = self.apply(plan)
        self.assertFalse(result["applied"])
        self.assertEqual(searches[0], 38)
        self.assertNotIn("manipulate_cart", [tool for tool, _, _ in self.provider.calls])

    def test_deadline_after_uncertain_write_requires_reconciliation(self):
        self.save_week()
        plan = self.complete()
        clock = [1000.0]
        def advance(tool, args):
            if tool == "manipulate_cart":
                clock[0] = 1240
                raise HouseholdError("synthetic uncertain mutation")
        self.provider.on_call = advance
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(HouseholdError, "deadline"):
                self.apply(plan)
        self.assertEqual(self.store.read()["cart_plan"]["status"], "needs_input")
        self.provider.on_call = None
        self.provider.calls.clear()
        self.assertFalse(self.apply(plan)["applied"])
        self.assertNotIn("manipulate_cart", [tool for tool, _, _ in self.provider.calls])

    def test_truncated_and_stale_binding_do_not_dispatch(self):
        self.save_week()
        plan = self.complete()
        changed = deepcopy(plan)
        changed["requirements"].pop()
        self.provider.calls.clear()
        with self.assertRaisesRegex(HouseholdError, "digest changed"):
            self.apply(changed)
        self.assertEqual(self.provider.calls, [])
        with self.store.locked() as state:
            state["menu"]["revision"] += 1
        with self.assertRaisesRegex(HouseholdError, "stale"):
            self.apply(plan)
        self.assertEqual(self.provider.calls, [])

    def test_exact_64_and_65_with_resolved_plus_unresolved_count(self):
        refs = self.save_recipes([recipe("Boundary", [(f"food{i}", 100) for i in range(63)], undecided="salt")])
        self.menu = self.app.handle({"operation": "menu", "action": "save", "menu": {"week": "2026-W37", "dishes": refs, "salads": []}})["menu"]
        first = self.prepare()
        self.assertEqual(len(first["requirements"]), 63)
        self.assertEqual(len(self.provider.calls), 63)
        # Include the source's exact salt amount: the complete real plan has 64.
        decision = {"source": {"collection": "dishes", "recipe_index": 0, "ingredient_index": 63}, "action": "include"}
        included = self.prepare(ingredient_decisions=[decision])
        complete = self.prepare(ingredient_decisions=[decision], candidate_approvals=self.approvals(included))
        self.assertEqual(complete["status"], "prepared")
        self.assertEqual(len(complete["requirements"]), 64)
        self.assertEqual(complete["scope"]["maximum_candidates_per_requirement"], 5)
        self.assertEqual(complete["scope"]["maximum_combinations_per_requirement"], 10000)
        with self.store.locked() as state:
            state["menu"]["dishes"][0]["shopping_requirements"].append({"item": "unknown", "scalable": False})
        self.provider.calls.clear()
        with self.assertRaisesRegex(HouseholdError, "at most 64"):
            self.prepare()
        self.assertEqual(self.provider.calls, [])

    def test_three_week_alternatives_reuse_observations_and_batch_shopping(self):
        refs = self.save_recipes([recipe(f"Dinner {i}", rows) for i, rows in enumerate(DINNERS)])
        planner_input = {"week": "2026-W37", "dates": [f"2026-09-{day:02}" for day in range(7, 14)],
                         "portions": 2, "candidates": [{"recipe_ref": r["recipe_ref"]} for r in refs], "alternatives": 3}
        request = {"operation": "products", "action": "lowest_cost", "planner_input": planner_input}
        first = self.app.handle(request)["cost_comparison"]
        self.assertEqual(len(first["alternatives"]), 3)
        request["candidate_approvals"] = self.approvals(first["alternatives"][0]["product_plan"])
        self.provider.calls.clear()
        result = self.app.handle(request)["cost_comparison"]
        self.assertEqual(result["status"], "compared")
        self.assertEqual(len(self.provider.calls), 37)
        for alternative in result["alternatives"]:
            self.assert_totals(alternative["product_plan"], DINNERS)
        self.menu = self.app.handle({"operation": "menu", "action": "save", "planner_handoff": result["selected_handoff"]})["menu"]
        source, destination = self.menu["slots"][:2]
        spec = {"source_slot_id": source["slot_id"], "source_snapshot_digest": source["snapshot_digest"],
                "prepared_portions": "4", "consumed_at_source": "2", "suitability": {"source": "current_user", "value": "suitable"},
                "storage": {"source": "current_user", "method": "refrigerated", "max_interval_days": 2},
                "leftovers": [{"slot_id": destination["slot_id"], "portions": "2"}]}
        batch = self.app.handle({"operation": "menu", "action": "batch_prepare", "menu_ref": mp.menu_ref(self.menu), "batch_spec": spec})["batch_plan"]
        dishes_by_key = {d["recipe_key"]: d for d in self.menu["dishes"]}
        source_index = int(dishes_by_key[source["recipe_key"]]["name"].split()[-1])
        removed_index = int(dishes_by_key[destination["recipe_key"]]["name"].split()[-1])
        self.menu = self.app.handle({"operation": "menu", "action": "batch_apply", "batch_plan": batch,
                                    "batch_confirmation": {"batch_digest": batch["batch_digest"], "statement": bp.CONFIRMATION_STATEMENT}})["menu"]
        cooked = [rows for i, rows in enumerate(DINNERS) if i != removed_index] + [DINNERS[source_index]]
        plan = self.complete()
        self.assert_totals(plan, cooked)
        self.assertGreater(len(plan["requirements"]), 20)
        self.assertEqual(len(self.menu["slots"]), 7)
        self.assertNotIn("manipulate_cart", [tool for tool, _, _ in self.provider.calls])

    def test_three_alternatives_allow_192_approvals_but_each_menu_stays_64(self):
        refs = self.save_recipes([recipe(f"Alternative {i}", [(f"choice{i} food{j}", 100) for j in range(64)]) for i in range(3)])
        request = {"operation": "products", "action": "lowest_cost", "planner_input": {
            "week": "2026-W37", "dates": ["2026-09-07"], "portions": 2,
            "candidates": [{"recipe_ref": r["recipe_ref"]} for r in refs], "alternatives": 3}}
        first = self.app.handle(request)["cost_comparison"]
        request["candidate_approvals"] = [approval for a in first["alternatives"] for approval in self.approvals(a["product_plan"])]
        self.assertEqual(len(request["candidate_approvals"]), 192)
        self.provider.calls.clear()
        result = self.app.handle(request)["cost_comparison"]
        self.assertEqual(result["status"], "compared")
        self.assertEqual(len(self.provider.calls), 192)
        self.assertEqual([len(a["product_plan"]["requirements"]) for a in result["alternatives"]], [64, 64, 64])
        self.assertEqual([a["product_plan"]["totals"]["total_payable_ore"] for a in result["alternatives"]], [6400] * 3)
        self.assertLess(len(json.dumps(result).encode()), 2 * 1024 * 1024 - 4096)
        # One budget for all alternatives: retain all 192 needs even when only
        # two provider reads fit, with no rank/cost claim from partial prices.
        self.provider.calls.clear()
        clock = [1000.0]
        self.provider.on_call = lambda tool, args: clock.__setitem__(0, clock[0] + 120)
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            partial = self.app.handle(request)["cost_comparison"]
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(partial["status"], "unavailable")
        self.assertIsNone(partial["comparison_claim"])
        self.assertEqual(sum(len(a["product_plan"]["requirements"]) for a in partial["alternatives"]), 192)
        self.assertEqual(sum(r["reason"] == "provider_search_deadline" for a in partial["alternatives"] for r in a["product_plan"]["unresolved_requirements"]), 190)

    def test_expiry_between_final_cart_read_and_mutation_prevents_dispatch(self):
        self.save_week()
        plan = self.complete()
        clock = [1000.0]
        reads = [0]
        def expire_after_prewrite_read(tool, args):
            if tool == "get_cart":
                reads[0] += 1
                if reads[0] == 2:
                    clock[0] = 1240
        self.provider.on_call = expire_after_prewrite_read
        self.provider.calls.clear()
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(HouseholdError, "deadline"):
                self.apply(plan)
        self.assertEqual(reads[0], 2)
        self.assertNotIn("manipulate_cart", [tool for tool, _, _ in self.provider.calls])
        self.assertFalse(self.provider.quantities)

    def test_postwrite_read_deadline_reports_unavailable_price(self):
        self.save_week()
        plan = self.complete()
        clock = [1000.0]
        reads = [0]
        def expire_after_cart_verification(tool, args):
            if tool == "get_cart":
                reads[0] += 1
                if reads[0] == 3:
                    clock[0] = 1240
        self.provider.on_call = expire_after_cart_verification
        self.provider.calls.clear()
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            result = self.apply(plan)
        self.assertTrue(result["applied"])
        self.assertEqual(result["price_verification"], "unavailable_after_cart_write")
        self.assertEqual(len(result["fresh_product_plan"]["requirements"]), 37)
        self.assertTrue(all(r["reason"] == "provider_search_deadline" for r in result["fresh_product_plan"]["unresolved_requirements"]))
        self.assertEqual(sum(tool == "product_search" for tool, _, _ in self.provider.calls), 74)

    def test_calculation_deadline_keeps_observed_but_unfinished_requirements(self):
        self.save_week()
        approvals = self.approvals(self.prepare())
        # Model elapsed read/calculation work without replacing the solver or
        # its bounds: all reads fit, only a prefix of calculations fits.
        from itertools import count
        clock = count(1000, 4)
        self.provider.calls.clear()
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: next(clock)):
            plan = self.prepare(candidate_approvals=approvals)
        self.assertEqual(len(self.provider.calls), 37)
        self.assertEqual(len(plan["requirements"]), 37)
        self.assertTrue(all("observation" in row for row in plan["requirements"]))
        selected = sum(row["status"] == "selected" for row in plan["requirements"])
        self.assertGreater(selected, 0)
        self.assertLess(selected, 37)
        self.assertEqual(len(plan["unresolved_requirements"]), 37 - selected)
        self.assertTrue(all(row["reason"] == "product_planning_deadline" for row in plan["unresolved_requirements"]))
        self.assertEqual(plan["status"], "needs_input")
        self.assertNotIn("totals", plan)
