"""Regressions from public product labels in the Grok/Oda field report.

Only catalog label strings are retained. Product IDs, prices, household state
and provider effects below are synthetic.
"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import StateStore
from product_observations import normalize_retail_product_search, parse_package
from service import Application


class GrokFieldPackageTests(unittest.TestCase):
    def test_fixed_size_after_oda_descriptors(self):
        for label, amount, unit in (
            ("Norge, 1 stk", 1, "count"),
            ("Norge, 2 stk", 2, "count"),
            ("Norge, 200 g", 200, "g"),
            ("Naturell, 200 g", 200, "g"),
            ("Skinnfri, 450 g", 450, "g"),
            ("Saktevoksende kylling, 850 g", 850, "g"),
            ("Revet, 100 g", 100, "g"),
            ("Lagret i 18–24 måneder, 200 g", 200, "g"),
            ("22 Mnd, 200 g", 200, "g"),
            ("i Pose, Etiopia/ Kenya, 20 g", 20, "g"),
            ("i Pose, Etiopia / Kenya, 20 g", 20, "g"),
            ("i Beger/pose, Nederland/ Italia, 20 g", 20, "g"),
            ("Vår Laveste Pris Nederland/ Spania, 250 g", 250, "g"),
            ("Wiig Gartneri Norge, 250 g", 250, "g"),
            ("Maks 10 per kunde, Norge, 200 g", 200, "g"),
            ("Flate, Peru/ Zimbabwe, 150 g", 150, "g"),
            ("Norge, Klasse 2, 1 stk", 1, "count"),
            ("Maks 3 til nedsatt pris, Norge, 1 stk", 1, "count"),
            ("Vår Laveste Pris, Norge, 250 g", 250, "g"),
            ("0,5%, 1 l", 1000, "ml"),
            ("8 stk", 8, "count"),
            ("0,3 l", 300, "ml"),
        ):
            with self.subTest(label=label):
                package = parse_package(label, provider="oda")
                self.assertIsNotNone(package)
                self.assertEqual(package["quantity"], {"numerator": amount, "denominator": 1})
                self.assertEqual(package["unit"], unit)

    def test_declared_count_and_total_weight_both_remain_available(self):
        for label, count, grams in (("5 stk, 400 g", 5, 400), ("Fine, 6stk, 480 g", 6, 480),
                                    ("2 stk, 230 g", 2, 230), ("14 stk, 770 g", 14, 770)):
            with self.subTest(label=label):
                self.assertEqual(parse_package(label, provider="oda"), {
                    "quantity": {"numerator": grams, "denominator": 1}, "unit": "g",
                    "item_count": 1, "contained_count": count,
                })

    def test_descriptors_do_not_hide_uncertain_or_competing_sizes(self):
        for label in (
            "Norge, ca. 200 g", "Norge, minst 200 g", "Norge, 200-300 g",
            "Norge, 200 g eller 300 g", "Minimum, 200 g", "Omtrent, 200 g",
            "Variabel vekt, 200 g", "500 g per pose, 2 stk", "2 stk, 500 g hver",
            "Norge, 2 stk, 500 g per stk", "Norge, 500 g, 1 kg", "Norge, 0 g",
            "Norge, 200 g,", "Norge,, 200 g", "Ukjent 5, 200 g",
        ):
            with self.subTest(label=label):
                self.assertIsNone(parse_package(label, provider="oda"))


class Retailer:
    """Raw Oda search contract, plus an in-memory cart; no network/browser."""

    def __init__(self):
        self.labels = {"Agurk": "Norge, 1 stk", "Hjertesalat": "Norge, 2 stk",
                       "Kyllingfilet": "Naturell, 200 g", "Pitabrød": "5 stk, 400 g",
                       "Kyllinglår": "Saktevoksende kylling, 850 g",
                       "Basilikum": "i Pose, Etiopia/ Kenya, 20 g", "Parmesan": "Revet, 100 g",
                       "Cherrytomater": "Vår Laveste Pris Nederland/ Spania, 250 g",
                       "Sukkererter": "Maks 10 per kunde, Norge, 200 g"}
        self.ids = {item: index for index, item in enumerate(self.labels, 1)}
        self.cart = {"items": [], "count": 0, "subtotal": 0.0, "total": 0.0}
        self.writes = []
        self.deposit_known = False

    def probe(self):
        return {"protocol_version": "fixture", "server": {"name": "synthetic"}, "tool_count": 3}

    def call(self, name, arguments, **kwargs):
        if name == "product_search":
            query = arguments["queries"][0]
            item = next(item for item in self.labels if item.casefold() == query.casefold())
            observation = normalize_retail_product_search({"result": [{"query": query, "hasMore": False, "products": [{
                "id": self.ids[item], "name": item, "description": self.labels[item],
                "price": "10.00", "availability": {"isAvailable": True},
            }]}]})
            observation["scope"]["requested_size"] = arguments["size"]
            if self.deposit_known:
                # Synthetic provider evidence, never inferred for real Oda.
                observation["products"][0]["purchase_options"][0].update(
                    mandatory_deposit_ore=0, total_payable_ore=1000)
            return observation
        if name == "get_cart":
            return deepcopy(self.cart)
        if name == "manipulate_cart":
            self.writes.append(deepcopy(arguments))
            for operation in arguments["operations"]:
                ref, quantity = int(operation["productId"]), operation["quantity"]
                self.cart["items"].append({"product_id": ref, "name": next(
                    item for item, key in self.ids.items() if key == ref),
                    "quantity": quantity, "price": 10.0})
            self.cart["count"] = sum(item["quantity"] for item in self.cart["items"])
            self.cart["subtotal"] = self.cart["total"] = 10.0 * self.cart["count"]
            return deepcopy(self.cart)
        raise AssertionError(name)


class GrokFieldProductFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.retailer = Retailer()
        self.app = Application(StateStore(Path(self.temp.name), {
            "instance": "grok-field-fixture", "household": "Synthetic", "provider": "oda",
            "profile_overrides": {},
        }), self.retailer, object())
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})
        recipe = self.app.handle({"operation": "recipes", "action": "save", "recipe": {
            "schema_version": 2, "name": "Synthetic dinner", "portions": 2,
            "ingredients": [{"item": name, "raw": f"{amount} {unit} {name}",
                             "quantity": amount, "unit": unit}
                            for name, amount, unit in (("Agurk", 0.5, "stk"),
                                ("Hjertesalat", 3, "stk"), ("Kyllingfilet", 400, "g"),
                                ("Pitabrød", 7, "stk"), ("Kyllinglår", 425, "g"),
                                ("Basilikum", 30, "g"), ("Parmesan", 150, "g"),
                                ("Cherrytomater", 150, "g"), ("Sukkererter", 300, "g"))],
            "steps": ["Cook the synthetic dinner."],
            "source": {"kind": "user", "relationship": "user_supplied"},
            "rights": {"storage": "full"},
        }, "idempotency_key": "field-recipe"})["recipe"]
        saved = self.app.handle({"operation": "menu", "action": "save", "menu": {
            "week": "2026-W38", "dishes": [{"recipe_ref": {
                "id": recipe["id"], "revision": recipe["revision"]}, "portions": 2}], "salads": [],
        }})["menu"]
        self.menu_ref = {key: saved[key] for key in ("menu_id", "revision", "digest")}

    def prepare(self, **kwargs):
        first = self.app.handle({"operation": "products", "action": "prepare",
                                "menu_ref": self.menu_ref})["product_plan"]
        approvals = [{"requirement_id": row["requirement_id"], "candidate_refs": [
            self.retailer.ids[row["item"]]]} for row in first["requirements"]]
        return self.app.handle({"operation": "products", "action": "prepare",
                                "menu_ref": self.menu_ref, "candidate_approvals": approvals,
                                **kwargs})["product_plan"]

    def test_raw_search_through_saved_menu_prepare_and_cart_apply(self):
        plan = self.prepare(price_mode="estimate")
        self.assertEqual(plan["status"], "prepared", plan["unresolved_requirements"])
        self.assertEqual(plan["totals"]["package_count"], 15)
        self.assertEqual(plan["totals"]["merchandise_ore"], 15000)
        self.assertIsNone(plan["totals"]["total_payable_ore"])
        self.assertEqual(self.retailer.writes, [])
        result = self.app.handle({"operation": "products", "action": "apply",
                                 "product_plan": plan, "product_plan_digest": plan["product_plan_digest"],
                                 "cart_change_requested": True})
        self.assertTrue(result["applied"])
        self.assertEqual(len(self.retailer.writes), 1)
        self.assertEqual({item["product_id"]: item["quantity"] for item in self.retailer.cart["items"]},
                         {1: 1, 2: 2, 3: 2, 4: 2, 5: 1, 6: 2, 7: 2, 8: 1, 9: 2})

    def test_quantity_limits_are_visible_and_prices_are_not_extended_past_them(self):
        for prefix, kind in (("Maks 1 til nedsatt pris", "discount_price"), ("Maks 1 per kunde", "per_customer")):
            with self.subTest(prefix=prefix):
                self.retailer.labels["Kyllingfilet"] = prefix + ", Naturell, 200 g"
                plan = self.prepare(price_mode="estimate")
                row = next(row for row in plan["unresolved_requirements"] if row["item"] == "Kyllingfilet")
                self.assertEqual(row["candidate_diagnostics"][0]["reason"], "package_limit_exceeded")
                self.assertEqual(row["candidate_diagnostics"][0]["package_limit"], {"count": 1, "kind": kind})
                self.assertEqual(self.retailer.writes, [])

    def test_exact_price_path_uses_declared_count_and_obeys_quantity_limit(self):
        self.retailer.deposit_known = True
        plan = self.prepare()
        self.assertEqual(plan["status"], "prepared", plan["unresolved_requirements"])
        self.assertEqual(plan["totals"]["package_count"], 15)
        self.assertEqual(plan["totals"]["total_payable_ore"], 15000)
        self.retailer.labels["Pitabrød"] = "Maks 1 til nedsatt pris, 5 stk, 400 g"
        plan = self.prepare()
        self.assertEqual(plan["status"], "needs_input")
        row = next(row for row in plan["unresolved_requirements"] if row["item"] == "Pitabrød")
        self.assertEqual(row["candidate_diagnostics"][0]["reason"], "package_limit_exceeded")
        self.assertEqual(self.retailer.writes, [])

    def test_unknown_deposit_is_distinct_from_package_and_unit_problems(self):
        plan = self.prepare()
        self.assertEqual(plan["status"], "needs_input")
        for row in plan["unresolved_requirements"]:
            self.assertEqual(row["candidate_diagnostics"][0]["reason"], "deposit_unobserved")
        self.retailer.labels["Kyllingfilet"] = "Naturell, 200 ml"
        self.retailer.labels["Agurk"] = "Norge, ca. 1 stk"
        plan = self.prepare(price_mode="estimate")
        diagnostics = {row["item"]: row["candidate_diagnostics"][0] for row in plan["unresolved_requirements"]}
        self.assertEqual(diagnostics["Agurk"]["reason"], "package_size_unresolved")
        self.assertEqual(diagnostics["Kyllingfilet"]["reason"], "unit_conversion_required")
        self.assertEqual(diagnostics["Kyllingfilet"]["required_unit"], "g")
        self.assertEqual(diagnostics["Kyllingfilet"]["observed_unit"], "ml")
        self.assertEqual(self.retailer.writes, [])


if __name__ == "__main__":
    unittest.main()
