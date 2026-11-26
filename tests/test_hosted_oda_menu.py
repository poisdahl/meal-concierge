"""Saved-menu merchandising and reversible hosted cart application."""
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hosted_oda
import hosted_oda_cart as cart
import hosted_oda_menu as menu
import install


TOMATO = 291
PASTA = 873
TOMATO_PRICE = 1290
PASTA_PRICE = 2150


def amount(ore):
    return f"{Decimal(ore) / 100:.2f}"


def cart_data(quantities=None, *, tomato_price=TOMATO_PRICE, grouped=False):
    prices = {TOMATO: tomato_price, PASTA: PASTA_PRICE}
    items = [{"product": {"id": ref, "name": "Synthetic chopped tomatoes" if ref == TOMATO else "Synthetic spaghetti",
                          "description": "400 g" if ref == TOMATO else "500 g", "price": amount(prices[ref])},
              "quantity": quantity,
              "totalGrossAmount": amount(prices[ref] * quantity)} for ref, quantity in (quantities or {}).items()]
    subtotal = amount(sum(prices[ref] * quantity for ref, quantity in (quantities or {}).items()))
    group = {"items": items}
    if grouped:
        group.update(id=42, groupType="recipes", title="Unexpected group")
    return {"groups": [group] if items else [], "productQuantityCount": sum((quantities or {}).values()),
            "totalGrossAmount": subtotal, "url": "https://oda.com/no/cart/", "deliverySlot": None,
            "totalBreakdown": {**dict.fromkeys(hosted_oda.BREAKDOWN_FIELDS, "0.00"), "subtotal": subtotal, "total": subtotal}}


class HostedOdaMenuTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home, self.state = self.root / "planning", self.root / "oda-state"
        self.home.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        program, planning_state = self.root / "program", self.home / "state"
        release = program / "release"
        release.mkdir(parents=True)
        (program / "current").symlink_to(release, target_is_directory=True)
        planning_state.mkdir(mode=0o700)
        self.planning_file = planning_state / "state.json"
        settings = self.home / "config.json"
        install.write_json(self.home / "runtime.json", {"manager": "external", "home": str(self.home),
                           "code_root": str(program), "release": str(release),
                           "paths": {"state": str(planning_state), "config": str(settings)}})
        install.write_json(program / "owner.json", {"home": str(self.home)})
        install.write_json(settings, {"provider": "none"})
        saved = {"menu_id": "menu_synthetic_oda", "revision": 1, "digest": "d" * 64, "phase": "draft",
                 "dishes": [{"shopping_requirements": [
                     {"item": "Hakkede tomater", "quantity": 600, "unit": "g", "scalable": True},
                     {"item": "Spagetti", "quantity": 250, "unit": "g", "scalable": True}]}], "salads": []}
        self.planning = {"provider": "none", "menu": saved, "profile": {},
                         "menu_ingredient_decisions": {
                             "menu_ref": {key: saved[key] for key in ("menu_id", "revision", "digest")},
                             "decisions": [{"source": {"collection": "dishes", "recipe_index": 0, "ingredient_index": 0},
                                            "action": "have_quantity", "quantity": 40, "unit": "g"}]}}
        install.write_json(self.planning_file, self.planning)
        self.enterContext(patch.object(menu.install, "data_ownership", side_effect=lambda _: nullcontext()))
        self.enterContext(patch.object(cart, "DOT_WORKSPACE", self.root))
        self.registry = self.root / cart.REGISTRY_NAME
        self.identifier = str(uuid.uuid4())
        self.build = {"source_commit": "a" * 40, "source_sha256": "b" * 64,
                      "installed_runtime_sha256": "c" * 64}
        self.addresses = [{"id": 99, "isSelected": True}]
        context = menu.menu_source(self.planning, str(self.home))
        self.source = context["binding"]
        self.choices = {row["requirement_id"]: TOMATO if "tomat" in row["item"].lower() else PASTA
                        for row in context["requirements"]}

    def step(self, action, **fields):
        return cart.run(self.state, self.registry, self.identifier, {"action": action, **fields},
                        "plugin_fixture", self.build)

    def begin(self, mode="restore", **overrides):
        return self.step("begin_menu", **{
            "planning_home": str(self.home), "mode": mode, "isolated_new_cart": True,
            "authorized_addresses": self.addresses,
            **{key: self.source[key] for key in ("menu_ref", "list_ref", "stock_ref")}, **overrides,
        })

    def reads(self, response, *, data=None, price=TOMATO_PRICE):
        frame = response["hosted_request"]
        observed = cart.now().isoformat()
        fields = {"frame_id": frame["frame_id"], "plugin_id": frame["plugin_id"],
                  "addresses": self.addresses, "address_observed_at": observed}
        for tool in frame["tools"]:
            if tool["tool"] == "product_search":
                products = []
                for query in tool["arguments"]["queries"]:
                    is_tomato = "tomat" in query.lower()
                    products.append({"query": query, "hasMore": False, "products": [{
                        "id": TOMATO if is_tomato else PASTA,
                        "name": "Synthetic chopped tomatoes" if is_tomato else "Synthetic spaghetti",
                        "availability": {"isAvailable": True}, "description": "400 g" if is_tomato else "500 g",
                        "price": amount(price if is_tomato else PASTA_PRICE),
                    }]})
                fields.update(catalog={"result": products[::-1]}, products_observed_at=observed,
                              catalog_arguments=tool["arguments"])
            if tool["tool"] == "get_cart":
                fields.update(cart=cart_data() if data is None else data, cart_observed_at=observed)
        return fields

    def ack(self, write, data):
        frame = write["hosted_request"]
        return self.step("acknowledge", **{key: frame[key] for key in ("frame_id", "plugin_id", "tool", "arguments")},
                         dispatched_at=frame["issued_at"], observed_at=cart.now().isoformat(),
                         result_source="original_attached_mutation_response", is_error=False, invocation_id=None, cart=data)

    def prepare(self, mode="restore"):
        found = self.step("observe_menu_search", **self.reads(self.begin(mode)))
        self.assertEqual(found["status"], "menu_choices")
        self.assertEqual(len(found["candidates"]), 2)
        return self.step("select_menu", choices=self.choices)

    def pre_add(self, mode="restore"):
        review = self.prepare(mode)
        baseline = self.step("approve_menu", review_digest=review["review_digest"])
        self.step("observe", **self.reads(baseline))
        return self.step("add")

    def test_verified_menu_cart_round_trip_preserves_prior_receipt_and_planning_state(self):
        saved = self.planning_file.read_bytes()
        menu_id, older_id = self.identifier, str(uuid.uuid4())
        self.identifier = older_id
        self.step("begin", product_id=345, isolated_new_cart=True, authorized_addresses=self.addresses)
        self.step("abort")
        self.identifier = menu_id
        pre_add = self.pre_add()
        quote = self.step("status")["quote"]
        by_product = {row["product_id"]: row for row in quote["items"]}
        self.assertEqual(by_product[TOMATO]["required"], {"numerator": 560, "denominator": 1})
        self.assertEqual(by_product[TOMATO]["package_count"], 2)
        self.assertEqual(by_product[PASTA]["package_count"], 1)
        self.assertEqual(quote["merchandise_ore"], 4730)
        write = self.step("observe", **self.reads(pre_add))
        self.assertTrue(write["dispatch"])
        self.assertEqual({row["productId"]: row["quantity"] for row in write["hosted_request"]["arguments"]["operations"]},
                         {TOMATO: 2, PASTA: 1})
        self.assertFalse(self.step("status")["dispatch"])
        self.assertNotIn("hosted_request", self.begin())
        with self.assertRaisesRegex(ValueError, "no original read frame"):
            self.step("observe", **self.reads(pre_add))
        filled = cart_data({TOMATO: 2, PASTA: 1})
        readback = self.ack(write, filled)
        added = self.step("observe", **self.reads(readback, data=filled))
        self.assertTrue(added["add_reply_prices"]["matches_review"])
        self.assertEqual(added["cart_review"]["breakdown_ore"]["subtotal"], 4730)
        self.assertTrue(added["cart_review"]["matches_review"])
        before_remove = self.step("remove")
        with self.assertRaisesRegex(ValueError, "grouped"):
            self.step("observe", **self.reads(before_remove, data=cart_data({TOMATO: 2, PASTA: 1}, grouped=True)))
        removal = self.step("observe", **self.reads(before_remove, data=filled))
        self.assertEqual({row["productId"]: row["quantity"] for row in removal["hosted_request"]["arguments"]["operations"]},
                         {TOMATO: -2, PASTA: -1})
        restored = self.step("observe", **self.reads(self.ack(removal, cart_data())))
        self.assertEqual(restored["status"], "restored")
        self.assertTrue(restored["add_acknowledged"] and restored["remove_acknowledged"])
        self.assertTrue(restored["restoration_amounts_match"])
        self.assertEqual(self.planning_file.read_bytes(), saved)
        self.assertIsNone(install.bounded_json(self.registry / "ledger.json")["current"])
        self.identifier = older_id
        self.assertEqual(self.step("status")["status"], "aborted")

    def test_source_review_and_prices_must_match_before_dispatch(self):
        with self.assertRaisesRegex(ValueError, "current menu, shopping list and stock"):
            self.begin(stock_ref="stale")
        result = self.prepare()
        with self.assertRaisesRegex(ValueError, "exact Oda menu"):
            self.step("approve_menu", review_digest="other")
        self.step("observe", **self.reads(self.step("approve_menu", review_digest=result["review_digest"])))
        pre_add = self.step("add")
        with self.assertRaisesRegex(ValueError, "merchandise price changed"):
            self.step("observe", **self.reads(pre_add, price=TOMATO_PRICE + 1))
        self.planning["profile"]["household"] = "Edited profile"
        install.write_json(self.planning_file, self.planning)
        with self.assertRaisesRegex(ValueError, "household profile changed"):
            self.step("observe", **self.reads(pre_add))
        self.assertFalse(self.step("status")["dispatch"])
        self.assertEqual(self.step("abort")["status"], "aborted")

    def test_native_search_is_bound_to_success_and_exact_paging(self):
        reading = self.reads(self.begin())
        with self.assertRaisesRegex(ValueError, "original hosted Oda product search"):
            self.step("observe_menu_search", **{**reading, "catalog_arguments": {**reading["catalog_arguments"], "page": 2}})
        with self.assertRaisesRegex(ValueError, "successful structured Oda"):
            self.step("observe_menu_search", **{**reading, "catalog": {**reading["catalog"], "isError": True}})
        self.assertEqual(self.step("status")["status"], "menu_search")

    def test_unapproved_original_reply_is_fenced_and_never_authorizes_cleanup(self):
        write = self.step("observe", **self.reads(self.pre_add()))
        wrong = self.ack(write, cart_data({TOMATO: 3}))
        self.assertEqual(wrong["status"], "add_response_unverified")
        for action in ("add", "remove", "abort"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.step(action)
        observed = self.step("observe_recovery", **self.reads(self.step("recheck"), data=cart_data()))
        self.assertEqual(observed["status"], "add_response_unverified")
        self.assertFalse(observed["dispatch"])
        self.identifier = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "original.*owns"):
            self.begin()

    def test_proven_partial_cart_can_restore_only_verified_subset(self):
        write = self.step("observe", **self.reads(self.pre_add()))
        partial = cart_data({TOMATO: 1})
        reading = self.ack(write, partial)
        self.assertEqual(reading["partial_add_quantities"], {str(TOMATO): 1})
        with self.assertRaisesRegex(ValueError, "does not allow"):
            self.step("remove")
        with self.assertRaisesRegex(ValueError, "unexpected products"):
            self.step("observe", **self.reads(reading, data=cart_data({TOMATO: 2})))
        checked = self.step("observe", **self.reads(reading, data=partial))
        self.assertEqual(checked["status"], "added")
        self.assertFalse(checked["cart_review"]["matches_review"])
        inverse = self.step("observe", **self.reads(self.step("remove"), data=partial))
        self.assertEqual(inverse["hosted_request"]["arguments"], {"operations": [{"productId": TOMATO, "quantity": -1}]})
        empty = cart_data()
        read = self.ack(inverse, empty)
        empty["totalGrossAmount"] = "50.00"
        empty["totalBreakdown"].update(surcharge="50.00", total="50.00")
        restored = self.step("observe", **self.reads(read, data=empty))
        self.assertEqual(restored["status"], "restored")
        self.assertFalse(restored["restoration_amounts_match"])
        self.assertEqual(restored["restored_amounts_ore"]["surcharge"], 5000)

    def test_oversized_prewrite_journal_preserves_original_readable_state(self):
        self.step("observe_menu_search", **self.reads(self.begin()))
        original = self.step("status")
        real_quote = menu.quote

        def oversized(context, views, choices):
            return {**real_quote(context, views, choices), "oversized": "x" * cart.JOURNAL_LIMIT}

        with patch.object(menu, "quote", side_effect=oversized), self.assertRaisesRegex(ValueError, "journal limit"):
            self.step("select_menu", choices=self.choices)
        self.assertEqual(self.step("status"), original)
        self.assertEqual(self.step("abort")["status"], "aborted")

    def test_keep_mode_checks_original_and_fresh_prices_and_preserves_cleanup(self):
        write = self.step("observe", **self.reads(self.pre_add(mode="keep")))
        changed = cart_data({TOMATO: 2, PASTA: 1}, tomato_price=TOMATO_PRICE + 10)
        read = self.ack(write, changed)
        observed = self.step("observe", **self.reads(read, data=cart_data({TOMATO: 2, PASTA: 1})))
        self.assertEqual(observed["status"], "added")
        self.assertFalse(observed["add_reply_prices"]["matches_review"])
        self.assertEqual(self.step("remove")["status"], "checking_remove")

    def test_keep_mode_requires_package_in_the_independent_cart_read(self):
        write = self.step("observe", **self.reads(self.pre_add(mode="keep")))
        filled = cart_data({TOMATO: 2, PASTA: 1})
        read = self.ack(write, filled)
        filled["groups"][0]["items"][0]["product"]["description"] = "200 g"
        added = self.step("observe", **self.reads(read, data=filled))
        self.assertEqual(added["status"], "added")
        self.assertFalse(added["cart_review"]["product_details_match"])
        self.assertEqual(self.step("remove")["status"], "checking_remove")

    def test_keep_mode_finishes_with_no_second_dispatch(self):
        write = self.step("observe", **self.reads(self.pre_add(mode="keep")))
        filled = cart_data({TOMATO: 2, PASTA: 1})
        result = self.step("observe", **self.reads(self.ack(write, filled), data=filled))
        self.assertEqual(result["status"], "applied")
        self.assertNotIn("hosted_request", result)
        self.assertFalse(result["dispatch"])
        self.assertEqual(self.step("status"), result)

    def test_keep_mode_does_not_finish_if_an_incomplete_cart_reports_the_full_price(self):
        write = self.step("observe", **self.reads(self.pre_add(mode="keep")))
        partial = cart_data({TOMATO: 1, PASTA: 1})
        partial["groups"][0]["items"][0]["totalGrossAmount"] = amount(2 * TOMATO_PRICE)
        partial["totalGrossAmount"] = amount(2 * TOMATO_PRICE + PASTA_PRICE)
        partial["totalBreakdown"].update(subtotal=partial["totalGrossAmount"], total=partial["totalGrossAmount"])
        result = self.step("observe", **self.reads(self.ack(write, partial), data=partial))
        self.assertEqual(result["status"], "added")
        self.assertFalse(result["cart_review"]["quantities_match"])
        self.assertFalse(result["cart_review"]["matches_review"])
