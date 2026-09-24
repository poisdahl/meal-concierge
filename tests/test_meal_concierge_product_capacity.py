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
from core import HouseholdError, StateStore, cart_summary
from product_planner import ingredient_search, partial_product_plan_digest
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
            size = 50 if query in {"garlic", "hvitløk"} else 200
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
        return [{"requirement_id": row["requirement_id"], "candidate_refs": [p["product_ref"] for p in self.provider.entry(ingredient_search(row["identity"], self.app.provider))["products"]]} for row in plan["requirements"]]

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

    def test_full_week_pantry_defaults_to_purchase_then_explicit_stock_applies(self):
        self.save_week(unresolved=True)
        first = self.prepare()
        self.assertEqual(len(first["requirements"]), 40)
        self.assertEqual(sum(len(row["sources"]) for row in first["requirements"]), 52)
        decisions = [{"source": {"collection":"dishes", "recipe_index":i, "ingredient_index":7},
                      "action": "omit" if item == "sesame" else "have_all"}
                     for i, item in enumerate(("salt","sugar","sesame"))]
        decisions.append({"source": {"collection": "dishes", "recipe_index": 0, "ingredient_index": 5},
                          "action": "have_quantity", "quantity": {"numerator": 101, "denominator": 2}, "unit": "g"})
        included = self.prepare(ingredient_decisions=decisions)
        plan = self.prepare(candidate_approvals=self.approvals(included), ingredient_decisions=decisions)
        counts = self.assert_totals(plan, DINNERS, pantry_onion=Fraction(101, 2))
        self.provider.calls.clear()
        result = self.apply(plan)
        self.assertTrue(result["applied"])
        self.assertEqual(result["price_verification"], "unchanged")
        self.assertEqual(len(self.provider.quantities), 37)
        self.assertEqual(sum(self.provider.quantities.values()), sum(counts.values()))
        self.assertEqual(sum(tool == "product_search" for tool, _, _ in self.provider.calls), 37)
        writes = [args for tool, args, _ in self.provider.calls if tool == "manipulate_cart"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(writes[0]["operations"]), 37)
        self.assertEqual(self.store.read()["cart_plan"]["product_plan_digest"], plan["product_plan_digest"])
        self.assertEqual(len({kw["deadline"] for tool, _, kw in self.provider.calls if tool == "product_search"}), 1)

    def test_full_apply_authority_retains_exact_refs_for_unrelated_delta_and_correction(self):
        dinners = deepcopy(DINNERS)
        dinners[0] = dinners[0] + [
            (f"week staple {index}", 25) for index in range(14)
        ]
        refs = self.save_recipes([
            recipe(f"Dinner {index}", rows) for index, rows in enumerate(dinners)
        ])
        self.menu = self.app.handle({
            "operation": "menu", "action": "save",
            "menu": {"week": "2026-W37", "dishes": refs, "salads": []},
        })["menu"]
        plan = self.complete()
        self.assertEqual(len(plan["requirements"]), 51)
        applied_rows = {row["identity"]: row for row in plan["requirements"]}
        applied_refs = {
            identity: [product["product_ref"] for product in row["selection"]["products"]]
            for identity, row in applied_rows.items()
        }
        salmon_candidates = self.provider.entry("salmon")["products"]
        selected_ref = salmon_candidates[0]["product_ref"]
        cheaper_later_ref = salmon_candidates[1]["product_ref"]
        self.assertEqual(applied_refs["salmon"], [selected_ref])

        result = self.apply(plan)
        self.assertTrue(result["applied"])
        cart_plan = self.store.read()["cart_plan"]
        authority = cart_plan["product_plan_authority"]
        expected_authority = self.app._partial_plan_authority(plan)
        self.assertEqual(authority["context"], expected_authority["context"])
        self.assertEqual(
            authority["selected_requirements"],
            expected_authority["selected_requirements"],
        )
        self.assertEqual(authority["product_plan_digest"], plan["product_plan_digest"])
        self.assertEqual(authority["cart_digest"], cart_plan["last_synced_digest"])
        self.assertEqual(
            authority["authority_digest"],
            mp.digest({key: value for key, value in authority.items()
                       if key != "authority_digest"}),
        )
        with self.store.locked() as state:
            state["cart_plan"]["approved_cart_digest"] = state["cart_plan"][
                "last_synced_digest"
            ]

        cheaper_option = salmon_candidates[1]["purchase_options"][0]
        cheaper_option.update(
            merchandise_ore=50, mandatory_deposit_ore=0, total_payable_ore=50,
        )
        ginger = applied_rows["ginger"]
        ginger_ref = applied_refs["ginger"][0]
        self.provider.calls.clear()
        prepared = self.app.handle({
            "operation": "products", "action": "prepare",
            "menu_ref": self.app._cart_menu_ref(self.menu),
            "candidate_approvals": [{
                "requirement_id": ginger["requirement_id"],
                "candidate_refs": [ginger_ref],
            }],
        })
        self.assertIsNotNone(prepared["apply_arguments"])
        prepared_rows = {
            row["identity"]: row for row in prepared["product_plan"]["requirements"]
        }
        self.assertEqual(
            [product["product_ref"] for product in prepared_rows["salmon"]["selection"]["products"]],
            applied_refs["salmon"],
        )
        self.assertEqual(
            prepared_rows["salmon"]["candidate_approval"]["candidate_refs"],
            [selected_ref],
        )
        self.assertNotIn(
            "manipulate_cart", [tool for tool, _arguments, _kwargs in self.provider.calls]
        )
        self.assertEqual(
            sum(tool == "product_search" for tool, _arguments, _kwargs in self.provider.calls),
            len(plan["requirements"]),
        )
        self.assertEqual(
            {tool for tool, _arguments, _kwargs in self.provider.calls},
            {"get_cart", "product_search"},
        )
        self.assertEqual(
            sum(tool == "get_cart" for tool, _arguments, _kwargs in self.provider.calls),
            1,
        )

        corrected = self.app.handle({
            "operation": "products", "action": "prepare",
            "product_plan_ref": prepared["product_plan_ref"],
            "candidate_approvals": [{
                "requirement_id": applied_rows["salmon"]["requirement_id"],
                "candidate_refs": [cheaper_later_ref],
            }],
        })
        corrected_rows = {
            row["identity"]: row for row in corrected["product_plan"]["requirements"]
        }
        self.assertEqual(
            [product["product_ref"] for product in corrected_rows["salmon"]["selection"]["products"]],
            [cheaper_later_ref],
        )
        for identity in set(applied_refs) - {"salmon"}:
            self.assertEqual(
                [product["product_ref"] for product in corrected_rows[identity]["selection"]["products"]],
                applied_refs[identity],
                identity,
            )

    def test_full_apply_authority_fails_closed_on_legacy_and_identity_drift(self):
        self.save_week()
        plan = self.complete()
        self.assertTrue(self.apply(plan)["applied"])
        baseline = self.store.read()

        cases = {
            "legacy": lambda state: state["cart_plan"].pop("product_plan_authority"),
            "menu": lambda state: state["menu"].update(
                revision=state["menu"]["revision"] + 1
            ),
            "plan": lambda state: state["cart_plan"].update(
                product_plan_digest="0" * 64
            ),
            "cart": lambda state: state["cart_plan"].update(
                last_synced_digest="0" * 64
            ),
            "authority digest": lambda state: state["cart_plan"][
                "product_plan_authority"
            ].update(authority_digest="0" * 64),
            "mixed partial and full": lambda state: state["cart_plan"].update({
                "partial_product_plan_digest": partial_product_plan_digest(plan),
                "partial_product_plan_approvals": self.app._plan_approvals(
                    plan, selected_only=True,
                ),
                "partial_product_plan_authority": self.app._partial_plan_authority(plan),
            }),
            "scope": lambda state: (
                state["menu"]["dishes"][0]["ingredients"][0]["quantity"].update(
                    numerator=301
                ),
                state["menu"]["dishes"][0]["shopping_requirements"][0][
                    "quantity"
                ].update(numerator=301),
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(drift=label):
                with self.store.locked() as state:
                    state.clear()
                    state.update(deepcopy(baseline))
                    mutate(state)
                    menu_ref = self.app._cart_menu_ref(state["menu"])
                self.provider.calls.clear()
                with self.assertRaisesRegex(
                    HouseholdError,
                    "mix partial and full|digest-bound authority|authority is invalid|no longer matches|scope or context changed",
                ):
                    self.app.handle({
                        "operation": "products", "action": "prepare",
                        "menu_ref": menu_ref,
                    })
                self.assertNotIn(
                    "manipulate_cart",
                    [tool for tool, _arguments, _kwargs in self.provider.calls],
                )

    def test_full_apply_authority_supports_empty_exact_selection(self):
        self.save_week()
        initial = self.prepare()
        decisions = [
            {"source": deepcopy(source), "action": "have_all"}
            for row in initial["requirements"] for source in row["sources"]
        ]
        covered = self.prepare(ingredient_decisions=decisions)
        self.assertEqual(covered["requirements"], [])
        applied = self.apply(covered)
        self.assertTrue(applied["applied"])
        self.assertTrue(applied["nothing_to_buy"])
        authority = self.store.read()["cart_plan"]["product_plan_authority"]
        self.assertEqual(authority["selected_requirements"], [])
        self.assertEqual(authority["context"]["ingredient_decisions"], decisions)

        self.provider.calls.clear()
        refreshed = self.app.handle({
            "operation": "products", "action": "prepare",
            "menu_ref": self.app._cart_menu_ref(self.menu),
        })
        self.assertEqual(refreshed["product_plan"]["requirements"], [])
        self.assertEqual(refreshed["product_plan"]["ingredient_decisions"], decisions)
        self.assertIsNotNone(refreshed["apply_arguments"])
        self.assertEqual(
            [tool for tool, _arguments, _kwargs in self.provider.calls],
            ["get_cart"],
        )

    def test_full_apply_authority_supports_empty_selection_with_due_recurring(self):
        self.save_week()
        initial = self.prepare()
        recurring_ref = self.provider.entry("salmon")["products"][0]["product_ref"]
        self.app.handle({
            "operation": "recurring", "action": "add", "item": {
                "product_id": recurring_ref, "product_name": "Recurring fixture",
                "quantity": 1,
                "schedule": {"every": 1, "unit": "weeks", "anchor": "2026-W37"},
            },
        })
        decisions = [
            {"source": deepcopy(source), "action": "have_all"}
            for row in initial["requirements"] for source in row["sources"]
        ]
        covered = self.prepare(ingredient_decisions=decisions)
        self.assertEqual(covered["requirements"], [])
        self.assertTrue(self.apply(covered)["applied"])
        authority = self.store.read()["cart_plan"]["product_plan_authority"]
        self.assertEqual(authority["selected_requirements"], [])
        self.assertEqual(self.provider.quantities[recurring_ref], 1)

        self.provider.calls.clear()
        refreshed = self.app.handle({
            "operation": "products", "action": "prepare",
            "menu_ref": self.app._cart_menu_ref(self.menu),
        })
        self.assertEqual(refreshed["product_plan"]["requirements"], [])
        self.assertEqual(refreshed["product_plan"]["ingredient_decisions"], decisions)
        self.assertEqual(
            [tool for tool, _arguments, _kwargs in self.provider.calls],
            ["get_cart"],
        )

    def test_full_apply_seed_reads_live_cart_and_fails_closed_on_drift(self):
        self.save_week()
        plan = self.complete()
        self.assertTrue(self.apply(plan)["applied"])
        selected_ref = next(
            product["product_ref"]
            for row in plan["requirements"] for product in row["selection"]["products"]
        )
        self.provider.quantities[selected_ref] += 1
        self.provider.calls.clear()
        with self.assertRaisesRegex(HouseholdError, "retailer cart changed"):
            self.app.handle({
                "operation": "products", "action": "prepare",
                "menu_ref": self.app._cart_menu_ref(self.menu),
            })
        self.assertEqual(
            [tool for tool, _arguments, _kwargs in self.provider.calls],
            ["get_cart"],
        )
        fenced = self.store.read()["cart_plan"]
        self.assertEqual(fenced["status"], "needs_input")

        self.provider.calls.clear()
        reconciled = self.app.handle({
            "operation": "cart", "action": "reconcile",
            "menu_ref": self.app._cart_menu_ref(self.menu),
            "decision": "keep_current",
            "cart_digest": fenced["pending_cart_digest"],
        })
        self.assertTrue(reconciled["reconciled"])
        current = self.store.read()["cart_plan"]
        self.assertEqual(current["status"], "active")
        self.assertNotIn("product_plan_digest", current)
        self.assertNotIn("product_plan_authority", current)
        self.assertNotIn("product_plan_summary", current)
        self.assertNotIn(
            "manipulate_cart",
            [tool for tool, _arguments, _kwargs in self.provider.calls],
        )

        self.provider.calls.clear()
        recovered = self.app.handle({
            "operation": "products", "action": "prepare",
            "menu_ref": self.app._cart_menu_ref(self.menu),
        })
        self.assertEqual(recovered["product_plan"]["status"], "needs_input")
        self.assertEqual(
            sum(tool == "product_search" for tool, _arguments, _kwargs in self.provider.calls),
            len(plan["requirements"]),
        )
        self.assertNotIn(
            "manipulate_cart",
            [tool for tool, _arguments, _kwargs in self.provider.calls],
        )

    def test_full_apply_seed_rejects_local_transition_during_cart_read(self):
        self.save_week()
        plan = self.complete()
        self.assertTrue(self.apply(plan)["applied"])
        baseline = self.store.read()
        cases = {
            "product plan": lambda cart_plan: cart_plan.update(
                product_plan_digest="0" * 64
            ),
            "last synced cart": lambda cart_plan: cart_plan.update(
                last_synced_digest="0" * 64
            ),
            "reconciliation": lambda cart_plan: cart_plan.update(
                status="needs_input", pending_cart_digest="0" * 64,
            ),
            "requirements": lambda cart_plan: cart_plan.update(
                menu_required_quantities={"999": 1},
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(transition=label):
                with self.store.locked() as state:
                    state.clear()
                    state.update(deepcopy(baseline))

                def change_plan_during_read(tool, _arguments):
                    if tool == "get_cart":
                        with self.store.locked() as state:
                            mutate(state["cart_plan"])

                self.provider.on_call = change_plan_during_read
                self.provider.calls.clear()
                with self.assertRaisesRegex(
                    HouseholdError, "changed during cart verification",
                ):
                    self.app.handle({
                        "operation": "products", "action": "prepare",
                        "menu_ref": self.app._cart_menu_ref(self.menu),
                    })
                self.assertEqual(
                    [tool for tool, _arguments, _kwargs in self.provider.calls],
                    ["get_cart"],
                )

    def test_full_apply_cart_drift_does_not_overwrite_newer_reconciliation(self):
        self.save_week()
        plan = self.complete()
        self.assertTrue(self.apply(plan)["applied"])
        selected_ref = next(
            product["product_ref"]
            for row in plan["requirements"] for product in row["selection"]["products"]
        )
        newer_digest = "c" * 64

        def install_newer_state_and_cart_drift(tool, _arguments):
            if tool != "get_cart":
                return
            self.provider.quantities[selected_ref] += 1
            with self.store.locked() as state:
                state["cart_plan"].update({
                    "status": "needs_input",
                    "last_synced_digest": newer_digest,
                    "pending_cart_digest": newer_digest,
                    "approved_cart_digest": None,
                })

        self.provider.on_call = install_newer_state_and_cart_drift
        self.provider.calls.clear()
        with self.assertRaisesRegex(HouseholdError, "changed during cart verification"):
            self.app.handle({
                "operation": "products", "action": "prepare",
                "menu_ref": self.app._cart_menu_ref(self.menu),
            })
        current = self.store.read()["cart_plan"]
        self.assertEqual(current["status"], "needs_input")
        self.assertEqual(current["last_synced_digest"], newer_digest)
        self.assertEqual(current["pending_cart_digest"], newer_digest)
        self.assertIsNone(current["approved_cart_digest"])
        self.assertEqual(
            [tool for tool, _arguments, _kwargs in self.provider.calls],
            ["get_cart"],
        )

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

    def test_selected_line_partial_apply_is_idempotent_and_full_apply_completes(self):
        self.save_week()
        initial = self.prepare()
        selected = next(row for row in initial["requirements"] if row["identity"] == "salmon")
        candidate = self.provider.entry("salmon")["products"][0]
        response = self.app.handle({
            "operation": "products", "action": "prepare",
            "menu_ref": self.app._cart_menu_ref(self.menu),
            "candidate_approvals": [{
                "requirement_id": selected["requirement_id"],
                "candidate_refs": [candidate["product_ref"]],
            }],
        })
        self.assertIsNone(response["apply_arguments"])
        arguments = response["partial_apply_arguments"]
        self.assertTrue(arguments["partial_apply"])
        self.assertNotIn("product_plan", arguments)
        self.provider.calls.clear()
        first = self.app.handle({
            "operation": "products", **arguments, "cart_change_requested": True,
        })
        self.assertEqual(
            sum(tool == "product_search" for tool, _arguments, _kwargs in self.provider.calls),
            3,
        )
        second = self.app.handle({
            "operation": "products", **arguments, "cart_change_requested": True,
        })
        self.assertTrue(first["partial_applied"])
        self.assertTrue(second["partial_applied"])
        self.assertTrue(second["cart"]["idempotent"])
        partial_state = self.store.read()["cart_plan"]
        self.assertNotIn("product_plan_digest", partial_state)
        self.assertEqual(partial_state["partial_product_plan_digest"], arguments["partial_product_plan_digest"])
        gate = self.app._cart_checkout_gate(cart_summary(self.provider.cart()), self.store.read()["menu"])
        self.assertEqual(gate["reason"], "weekly_menu_products_incomplete")

        # Search churn for an unresolved line does not invalidate the selected line.
        self.provider.entry("ginger")["products"].reverse()
        third = self.app.handle({
            "operation": "products", **arguments, "cart_change_requested": True,
        })
        self.assertTrue(third["partial_applied"])

        ginger = next(row for row in initial["requirements"] if row["identity"] == "ginger")
        ginger_candidate = self.provider.entry("ginger")["products"][0]
        next_response = self.app.handle({
            "operation": "products", "action": "prepare",
            "menu_ref": self.app._cart_menu_ref(self.menu),
            "candidate_approvals": [{
                "requirement_id": ginger["requirement_id"],
                "candidate_refs": [ginger_candidate["product_ref"]],
            }],
        })
        next_partial = self.app.handle({
            "operation": "products", **next_response["partial_apply_arguments"],
            "cart_change_requested": True,
        })
        self.assertTrue(next_partial["partial_applied"])
        self.assertEqual(next_partial["remaining_issue_count"], 35)
        self.assertIn(candidate["product_ref"], self.provider.quantities)
        self.assertIn(ginger_candidate["product_ref"], self.provider.quantities)

        full = self.complete()
        completed = self.apply(full)
        self.assertTrue(completed["applied"])
        final_state = self.store.read()["cart_plan"]
        self.assertEqual(final_state["product_plan_digest"], full["product_plan_digest"])
        self.assertNotIn("partial_product_plan_digest", final_state)
        expected = self.assert_totals(full, DINNERS)
        self.assertEqual(self.provider.quantities[candidate["product_ref"]], expected["salmon"])

    def test_partial_seed_keeps_exact_applied_ref_through_unrelated_continuations(self):
        self.save_week()
        initial = self.prepare()
        rows = {row["identity"]: row for row in initial["requirements"]}
        salmon_candidates = self.provider.entry("salmon")["products"]
        selected_ref = salmon_candidates[0]["product_ref"]
        cheaper_later_ref = salmon_candidates[1]["product_ref"]
        first = self.app.handle({
            "operation": "products", "action": "prepare",
            "menu_ref": self.app._cart_menu_ref(self.menu),
            "candidate_approvals": [{
                "requirement_id": rows["salmon"]["requirement_id"],
                "candidate_refs": [selected_ref, cheaper_later_ref],
            }],
        })
        applied = self.app.handle({
            "operation": "products", **first["partial_apply_arguments"],
            "cart_change_requested": True,
        })
        self.assertTrue(applied["partial_applied"])
        authority = self.store.read()["cart_plan"]["partial_product_plan_authority"]
        self.assertEqual(
            authority["selected_requirements"][0]["selection"]["products"][0]["product_ref"],
            selected_ref,
        )

        cheaper_option = salmon_candidates[1]["purchase_options"][0]
        cheaper_option.update(
            merchandise_ore=50, mandatory_deposit_ore=0, total_payable_ore=50,
        )
        ginger_ref = self.provider.entry("ginger")["products"][0]["product_ref"]
        prepared_c = self.app.handle({
            "operation": "products", "action": "prepare",
            "menu_ref": self.app._cart_menu_ref(self.menu),
            "candidate_approvals": [{
                "requirement_id": rows["ginger"]["requirement_id"],
                "candidate_refs": [ginger_ref],
            }],
        })
        prepared_c_rows = {
            row["requirement_id"]: row for row in prepared_c["product_plan"]["requirements"]
        }
        self.assertEqual(
            prepared_c_rows[rows["salmon"]["requirement_id"]]["selection"]["products"][0]["product_ref"],
            selected_ref,
        )

        cod_ref = self.provider.entry("cod")["products"][0]["product_ref"]
        prepared_d = self.app.handle({
            "operation": "products", "action": "prepare",
            "product_plan_ref": prepared_c["product_plan_ref"],
            "candidate_approvals": [{
                "requirement_id": rows["cod"]["requirement_id"],
                "candidate_refs": [cod_ref],
            }],
        })
        prepared_d_rows = {
            row["requirement_id"]: row for row in prepared_d["product_plan"]["requirements"]
        }
        salmon = prepared_d_rows[rows["salmon"]["requirement_id"]]
        self.assertEqual(salmon["selection"]["products"][0]["product_ref"], selected_ref)
        self.assertEqual(salmon["candidate_approval"]["candidate_refs"], [selected_ref])

        with self.store.locked() as state:
            saved_authority = deepcopy(
                state["cart_plan"].pop("partial_product_plan_authority")
            )
        with self.assertRaisesRegex(HouseholdError, "lack exact digest-bound authority"):
            self.app.handle({
                "operation": "products", "action": "prepare",
                "menu_ref": self.app._cart_menu_ref(self.menu),
            })
        saved_authority["selected_requirements"][0]["candidate_approval"][
            "candidate_refs"
        ] = [cheaper_later_ref]
        with self.store.locked() as state:
            state["cart_plan"]["partial_product_plan_authority"] = saved_authority
        with self.assertRaisesRegex(HouseholdError, "do not match their recorded authority"):
            self.app.handle({
                "operation": "products", "action": "prepare",
                "menu_ref": self.app._cart_menu_ref(self.menu),
            })

    def test_later_partial_revalidates_every_prior_selection(self):
        self.save_week()
        initial = self.prepare()
        salmon = next(row for row in initial["requirements"] if row["identity"] == "salmon")
        salmon_candidate = self.provider.entry("salmon")["products"][0]
        first = self.app.handle({
            "operation": "products", "action": "prepare", "menu_ref": self.app._cart_menu_ref(self.menu),
            "candidate_approvals": [{"requirement_id": salmon["requirement_id"], "candidate_refs": [salmon_candidate["product_ref"]]}],
        })
        applied = self.app.handle({
            "operation": "products", **first["partial_apply_arguments"], "cart_change_requested": True,
        })
        self.assertTrue(applied["partial_applied"])
        ginger = next(row for row in initial["requirements"] if row["identity"] == "ginger")
        ginger_candidate = self.provider.entry("ginger")["products"][0]
        original_candidate = deepcopy(salmon_candidate)
        mutations = {
            "availability": lambda value: value.update(availability="unavailable"),
            "package size": lambda value: value["package"].update(quantity={"numerator": 100, "denominator": 1}),
            "price": lambda value: value["purchase_options"][0].update(
                merchandise_ore=value["purchase_options"][0]["merchandise_ore"] + 100,
                total_payable_ore=value["purchase_options"][0]["total_payable_ore"] + 100,
            ),
            "name": lambda value: value.update(name="Changed salmon package"),
        }
        for label, mutate in mutations.items():
            with self.subTest(drift=label):
                salmon_candidate.clear()
                salmon_candidate.update(deepcopy(original_candidate))
                mutate(salmon_candidate)
                second = self.app.handle({
                    "operation": "products", "action": "prepare", "menu_ref": self.app._cart_menu_ref(self.menu),
                    "candidate_approvals": [{"requirement_id": ginger["requirement_id"], "candidate_refs": [ginger_candidate["product_ref"]]}],
                })
                result = self.app.handle({
                    "operation": "products", **second["partial_apply_arguments"], "cart_change_requested": True,
                })
                self.assertFalse(result["applied"])
                self.assertEqual(result["status"], "needs_input")
                self.assertNotIn(ginger_candidate["product_ref"], self.provider.quantities)
        salmon_candidate.clear()
        salmon_candidate.update(original_candidate)

    def test_lost_partial_apply_response_leaves_checkout_fenced(self):
        self.save_week()
        initial = self.prepare()
        selected = next(row for row in initial["requirements"] if row["identity"] == "salmon")
        candidate = self.provider.entry("salmon")["products"][0]
        response = self.app.handle({
            "operation": "products", "action": "prepare", "menu_ref": self.app._cart_menu_ref(self.menu),
            "candidate_approvals": [{"requirement_id": selected["requirement_id"], "candidate_refs": [candidate["product_ref"]]}],
        })
        original = self.app._cart_sync

        def lost_response(request, deadline):
            original(request, deadline)
            raise HouseholdError("synthetic lost response")

        with mock.patch.object(self.app, "_cart_sync", side_effect=lost_response):
            with self.assertRaisesRegex(HouseholdError, "lost response"):
                self.app.handle({
                    "operation": "products", **response["partial_apply_arguments"], "cart_change_requested": True,
                })
        self.assertIn("managed_product_apply_fence", self.store.read())
        gate = self.app._cart_checkout_gate(cart_summary(self.provider.cart()), self.store.read()["menu"])
        self.assertEqual(gate["reason"], "managed_product_apply_incomplete")

    def test_current_profile_minimums_block_checkout_after_product_apply(self):
        self.save_week()
        self.assertTrue(self.menu["weekly_plan_complete"])
        plan = self.complete()
        self.assertTrue(self.apply(plan)["applied"])
        with self.store.locked() as state:
            state["profile"]["meals"]["dinner_days"] = 6
            state["profile"]["diet"].update({
                "minimum_fish_portions": 3,
                "minimum_legume_dinners": 0,
                "minimum_wholegrain_or_potato_dinners": 0,
                "minimum_vegetable_types": 0,
            })
        gate = self.app._cart_checkout_gate(cart_summary(self.provider.cart()), self.store.read()["menu"])
        self.assertEqual(gate["reason"], "weekly_menu_minimums_unsatisfied")
        self.assertNotEqual(gate["minimum_evaluation"]["status"], "pass")

    def test_saved_week_shape_change_blocks_product_preparation(self):
        self.save_week()
        self.assertTrue(self.menu["weekly_plan_complete"])
        with self.store.locked() as state:
            state["profile"]["meals"]["dinner_days"] = 6
        with self.assertRaisesRegex(HouseholdError, "complete weekly menu"):
            self.prepare()
        self.assertFalse(any(name == "manipulate_cart" for name, _, _ in self.provider.calls))

    def test_selected_line_price_drift_blocks_partial_apply_without_cart_write(self):
        self.save_week()
        initial = self.prepare()
        selected = next(row for row in initial["requirements"] if row["identity"] == "salmon")
        candidate = self.provider.entry("salmon")["products"][0]
        response = self.app.handle({
            "operation": "products", "action": "prepare",
            "menu_ref": self.app._cart_menu_ref(self.menu),
            "candidate_approvals": [{
                "requirement_id": selected["requirement_id"],
                "candidate_refs": [candidate["product_ref"]],
            }],
        })
        changed_option = self.provider.entry("salmon")["products"][0]["purchase_options"][0]
        changed_option["merchandise_ore"] += 1
        changed_option["total_payable_ore"] += 1
        result = self.app.handle({
            "operation": "products", **response["partial_apply_arguments"],
            "cart_change_requested": True,
        })
        self.assertFalse(result["applied"])
        self.assertEqual(result["status"], "needs_input")
        self.assertFalse(self.provider.quantities)

    def test_deadline_stops_dispatch_and_preserves_all_needs(self):
        self.save_week()
        clock = [1000.0]
        def advance(tool, args):
            clock[0] += 80
        self.provider.on_call = advance
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            plan = self.prepare()
        self.assertEqual(len(self.provider.calls), 3)
        self.assertTrue(all(kw["deadline"] == 1235 for _, _, kw in self.provider.calls))
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
                if searches[0] == 3:
                    clock[0] = 1240
        self.provider.on_call = advance
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            result = self.apply(plan)
        self.assertFalse(result["applied"])
        self.assertEqual(searches[0], 3)
        self.assertEqual(result["status"], "validating")
        self.assertIn("continue_arguments", result)
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
            result = self.apply(plan)
        self.assertFalse(result["applied"])
        self.assertTrue(result["outcome_unknown"])
        self.assertTrue(result["cart_write_pending"])
        self.assertTrue(result["cart_reconciliation_required"])
        self.assertEqual(result["menu_ref"], self.app._cart_menu_ref(self.menu))
        self.assertEqual(result["product_plan_digest"], plan["product_plan_digest"])
        self.assertIsNone(result["synced"])
        self.assertEqual(result["cart_plan"]["status"], "needs_input")
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

    def test_full_scope_preserves_unresolved_line_above_read_slice(self):
        refs = self.save_recipes([recipe("Boundary", [(f"food{i}", 100) for i in range(63)], undecided="salt")])
        self.menu = self.app.handle({"operation": "menu", "action": "save", "menu": {"week": "2026-W37", "dishes": refs, "salads": []}})["menu"]
        first = self.prepare()
        self.assertEqual(len(first["requirements"]), 64)
        self.assertEqual(len(self.provider.calls), 64)
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
        expanded = self.prepare()
        self.assertEqual(len(expanded["requirements"]), 64)
        self.assertTrue(any(row["item"] == "unknown" for row in expanded["unresolved_requirements"]))
        self.assertEqual(len(self.provider.calls), 64)

    def test_three_week_alternatives_reuse_observations_and_batch_shopping(self):
        with self.store.locked() as state:
            for target in ("minimum_fish_portions", "minimum_legume_dinners", "minimum_wholegrain_or_potato_dinners", "minimum_vegetable_types"):
                state["profile"]["diet"][target] = 0
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

    def test_verified_cart_price_does_not_require_a_second_catalog_pass(self):
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
        self.assertEqual(result["price_verification"], "unchanged")
        self.assertNotIn("fresh_product_plan", result)
        self.assertEqual(sum(tool == "product_search" for tool, _, _ in self.provider.calls), 37)

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
        try:
            from mcp_server import MCP_PRODUCT_WIRE_BUDGET, _bounded_product_result, _mcp_text_wire_chars
        except ModuleNotFoundError as exc:
            if exc.name == "mcp":
                self.skipTest("MCP test dependency is not installed")
            raise
        response = {
            "apply_arguments": None,
            "partial_apply_arguments": {
                "action": "apply", "partial_apply": True,
                "menu_ref": self.app._cart_menu_ref(self.menu),
                "candidate_approvals": self.app._plan_approvals(plan, selected_only=True),
                "ingredient_decisions": [], "budget_ore": None, "price_mode": "exact",
                "partial_product_plan_digest": plan["partial_product_plan_digest"],
            },
            "product_plan": plan,
        }
        compact = _bounded_product_result(response)
        self.assertIn("partial_apply_arguments", compact)
        self.assertLess(
            _mcp_text_wire_chars(json.dumps(compact, ensure_ascii=False, separators=(",", ":"))),
            MCP_PRODUCT_WIRE_BUDGET,
        )

    def agent_products(self, action="prepare", **arguments):
        return self.app.handle({"operation": "products", "action": action,
                                "response_view": "agent", **arguments})

    def assert_agent_wire(self, value):
        from mcp_server import _mcp_text_wire_chars
        self.assertEqual(value["projection"], "agent")
        self.assertLess(_mcp_text_wire_chars(json.dumps(value, ensure_ascii=False, separators=(",", ":"))), 45_000)

    def read_product_pages(self, first):
        rows = list(first["requirements"])
        page = first
        self.assert_agent_wire(page)
        while page["next_offset"] is not None:
            page = self.agent_products("get", product_plan_ref=first["product_plan_ref"],
                                       offset=page["next_offset"])
            self.assert_agent_wire(page)
            rows.extend(page["requirements"])
        return rows

    def test_agent_large_week_recovers_pages_and_applies_short_handle(self):
        dinners = [rows + [(f"extra food {day}-{index}", 30) for index in range(3)]
                   for day, rows in enumerate(DINNERS)]
        refs = self.save_recipes([recipe(f"Dinner {day}", rows) for day, rows in enumerate(dinners)])
        self.menu = self.app.handle({"operation": "menu", "action": "save",
            "menu": {"week": "2026-W37", "dishes": refs, "salads": []}})["menu"]
        menu_ref = self.app._cart_menu_ref(self.menu)
        original_entry = self.provider.entry

        def large_entry(query):
            observed = original_entry(query)
            for candidate in observed["products"]:
                candidate["dietary_evidence"] = {"ingredients": "Observed retailer ingredient text. " * 220}
            return observed

        self.provider.entry = large_entry
        raw = self.prepare()
        self.assertEqual(len(raw["requirements"]), 58)
        self.assertGreater(len(json.dumps(raw).encode()), 2 * 1024 * 1024)
        first = self.agent_products(menu_ref=menu_ref)
        self.assertEqual(first["progress"]["requirement_count"], 58)
        snapshot = self.store.read()["menu_planning"]["prepared"][first["product_plan_ref"]]["snapshot"]
        self.assertLess(len(json.dumps(snapshot).encode()), 300_000)
        self.assertNotIn("dietary_evidence", json.dumps(snapshot))
        self.provider.calls.clear()
        rows = self.read_product_pages(first)
        exact = self.agent_products("get", product_plan_ref=first["product_plan_ref"],
                                    requirement_id=rows[19]["requirement_id"])
        self.assertEqual(exact["requirements"], [rows[19]])
        self.assertEqual(self.provider.calls, [])
        approvals = [{"requirement_id": row["requirement_id"],
                      "candidate_refs": [row["candidates"][0]["product_ref"]]} for row in rows]
        # Discard a successful continuation response, then recover its exact
        # newly rotated handle after a service restart without another search.
        self.agent_products(product_plan_ref=first["product_plan_ref"], candidate_approvals=approvals[:8])
        self.app = Application(self.store, self.provider, object())
        self.provider.calls.clear()
        recovered = self.agent_products("get", menu_ref=menu_ref)
        self.assertEqual(recovered["progress"]["selected_count"], 8)
        self.assertEqual(self.provider.calls, [])
        with self.assertRaisesRegex(HouseholdError, "stale"):
            self.agent_products("get", product_plan_ref=first["product_plan_ref"])
        final = self.agent_products(product_plan_ref=recovered["product_plan_ref"], candidate_approvals=approvals[8:])
        self.assertEqual(final["status"], "prepared")
        self.assertEqual(set(final["apply_arguments"]), {"action", "product_plan_ref", "product_plan_digest"})
        applied = self.agent_products(**final["apply_arguments"], cart_change_requested=True)
        self.assert_agent_wire(applied)
        self.assertTrue(applied["applied"], applied)
        self.assertEqual(len(self.provider.quantities), 58)

    def test_agent_unsaved_preview_get_continue_and_apply_boundary(self):
        references = self.save_recipes([recipe("Unsaved dinner", [("carrots", 100), ("onion", 50)])])
        plan = self.app.handle({"operation": "menu", "action": "plan", "planner_input": {
            "selection_mode": "agent", "week": "2026-W37", "dates": ["2026-09-07"],
            "portions": 2, "candidates": [{"recipe_ref": references[0]["recipe_ref"]}],
        }})["plan"]
        planner_ref = plan["save_ref"]
        first = self.agent_products(planner_ref=planner_ref)
        self.assertTrue(first["preview"])
        self.assertIsNone(self.store.read()["menu"])
        self.provider.calls.clear()
        read = self.agent_products("get", planner_ref=planner_ref)
        self.assertEqual(first["product_plan_ref"], read["product_plan_ref"])
        self.assertEqual(self.provider.calls, [])
        approvals = [{"requirement_id": row["requirement_id"],
                      "candidate_refs": [row["candidates"][0]["product_ref"]]} for row in read["requirements"]]
        complete = self.agent_products(product_plan_ref=read["product_plan_ref"], candidate_approvals=approvals)
        self.assertEqual(complete["status"], "prepared")
        self.assertNotIn("apply_arguments", complete)
        self.provider.calls.clear()
        with self.assertRaisesRegex(HouseholdError, "preview cannot apply"):
            self.agent_products("apply", product_plan_ref=complete["product_plan_ref"],
                product_plan_digest=complete["product_plan_digest"], cart_change_requested=True)
        self.assertEqual(self.provider.calls, [])
        self.assertIsNone(self.store.read()["menu"])

    def test_agent_short_apply_rejects_changed_facts_and_stale_menu(self):
        self.save_week()
        raw = self.complete()
        first = self.agent_products("get", menu_ref=self.app._cart_menu_ref(self.menu))
        self.provider.entry("salmon")["products"][0]["purchase_options"][0].update(
            merchandise_ore=200, total_payable_ore=200)
        self.provider.calls.clear()
        result = self.agent_products(**first["apply_arguments"], cart_change_requested=True)
        self.assertFalse(result["applied"])
        self.assertIn("fresh_product_plan", result)
        self.assert_agent_wire(result)
        self.assertNotIn("manipulate_cart", [tool for tool, _, _ in self.provider.calls])
        fresh = self.agent_products("get", product_plan_ref=result["fresh_product_plan"]["product_plan_ref"])
        self.assertNotEqual(fresh["product_plan_digest"], raw["product_plan_digest"])
        with self.store.locked() as state:
            state["menu"]["revision"] += 1
        self.provider.calls.clear()
        with self.assertRaisesRegex(HouseholdError, "stale"):
            self.agent_products(**fresh["apply_arguments"], cart_change_requested=True)
        self.assertEqual(self.provider.calls, [])

    def test_agent_short_partial_apply_and_uncertain_full_write_keep_outcomes(self):
        self.save_week()
        first = self.agent_products(menu_ref=self.app._cart_menu_ref(self.menu))
        row = first["requirements"][0]
        partial = self.agent_products(product_plan_ref=first["product_plan_ref"], candidate_approvals=[{
            "requirement_id": row["requirement_id"], "candidate_refs": [row["candidates"][0]["product_ref"]]}])
        applied = self.agent_products(**partial["partial_apply_arguments"], cart_change_requested=True)
        self.assert_agent_wire(applied)
        self.assertTrue(applied["partial_applied"])
        self.assertEqual(applied["status"], "partial_applied")
        self.assertEqual(applied["remaining_issue_count"], 36)
        self.assertIn("partial_product_plan_digest", self.store.read()["cart_plan"])
        self.complete()
        complete = self.agent_products("get", menu_ref=self.app._cart_menu_ref(self.menu))
        clock = [1000.0]

        def uncertain(tool, _arguments):
            if tool == "manipulate_cart":
                clock[0] = 1240
                raise HouseholdError("synthetic uncertain mutation")

        self.provider.on_call = uncertain
        with mock.patch("planning_operations.time.monotonic", side_effect=lambda: clock[0]):
            result = self.agent_products(**complete["apply_arguments"], cart_change_requested=True)
        self.assert_agent_wire(result)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertTrue(result["cart_write_pending"])
        self.assertTrue(result["cart_reconciliation_required"])
        self.assertEqual(result["cart_plan"]["menu_ref"], self.app._cart_menu_ref(self.menu))
        self.assertIn("cart_digest", result["cart_plan"])

    def test_agent_issues_pages_include_unquantified_source_positions(self):
        refs = self.save_recipes([recipe("Unknown serving input", [("carrots", 100)])])
        self.menu = self.app.handle({"operation": "menu", "action": "save",
            "menu": {"week": "2026-W37", "dishes": refs, "salads": []}})["menu"]
        with self.store.locked() as state:
            state["menu"]["dishes"][0]["shopping_requirements"].append({
                "item": "unspecified garnish", "scalable": False})
        result = self.agent_products(menu_ref=self.app._cart_menu_ref(self.menu))
        self.provider.calls.clear()
        issues = self.agent_products(**result["issues_arguments"], limit=1)
        self.assert_agent_wire(issues)
        self.assertEqual(issues["issues"][0]["ingredient_index"], 1)
        self.assertEqual(issues["issues"][0]["reason"], "non_scalable_quantity_unresolved")
        self.assertEqual(self.provider.calls, [])

    def test_agent_shared_package_dependency_and_short_apply_count_once(self):
        refs = self.save_recipes([recipe("Two vegetable cuts", [("carrots", 100), ("onion", 100)])])
        self.menu = self.app.handle({"operation": "menu", "action": "save",
            "menu": {"week": "2026-W37", "dishes": refs, "salads": []}})["menu"]
        candidate = product("901", "Mixed vegetables", 500, "g", [option(100)])

        def shared_entry(query):
            self.provider.catalog[query] = observation(query, [candidate])
            return self.provider.catalog[query]

        self.provider.entry = shared_entry
        first = self.agent_products(menu_ref=self.app._cart_menu_ref(self.menu))
        ids = [row["requirement_id"] for row in first["requirements"]]
        shared = {"requirement_ids": ids, "package_count": 1,
                  "quantity_basis": "One 500 g vegetable package covers both 100 g needs."}
        complete = self.agent_products(product_plan_ref=first["product_plan_ref"], candidate_approvals=[{
            "requirement_id": member, "candidate_refs": ["901"], "shared_package": shared} for member in ids])
        self.assertEqual(complete["totals"]["package_count"], 1)
        changed = self.agent_products(product_plan_ref=complete["product_plan_ref"], candidate_approvals=[{
            "requirement_id": ids[0], "candidate_refs": ["901"]}])
        self.assertEqual(changed["progress"]["selected_count"], 1)
        self.assertNotIn("apply_arguments", changed)
        restored = self.agent_products(product_plan_ref=changed["product_plan_ref"], candidate_approvals=[{
            "requirement_id": member, "candidate_refs": ["901"], "shared_package": shared} for member in ids])
        result = self.agent_products(**restored["apply_arguments"], cart_change_requested=True)
        self.assertTrue(result["applied"], result)
        self.assertEqual(self.provider.quantities, {"901": 1})
