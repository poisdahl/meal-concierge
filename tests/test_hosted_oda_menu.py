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
OATS = 541
TOMATO_PRICE = 1290
PASTA_PRICE = 2150
OATS_PRICE = 2490


def amount(ore):
    return f"{Decimal(ore) / 100:.2f}"


def cart_data(quantities=None, *, tomato_price=TOMATO_PRICE, oats_price=OATS_PRICE, grouped=False):
    prices = {TOMATO: tomato_price, PASTA: PASTA_PRICE, OATS: oats_price}
    products = {TOMATO: ("Synthetic chopped tomatoes", "400 g"), PASTA: ("Synthetic spaghetti", "500 g"),
                OATS: ("Synthetic oats", "1 kg")}
    items = [{"product": {"id": ref, "name": products[ref][0],
                          "description": products[ref][1], "price": amount(prices[ref])},
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

    def review_existing(self, baseline):
        first = self.begin("keep", preserve_existing=True)
        self.assertEqual([tool["tool"] for tool in first["hosted_request"]["tools"]],
                         ["get_delivery_addresses", "product_search", "get_cart"])
        self.step("observe_menu_search", **self.reads(first, data=baseline))
        return self.step("select_menu", choices=self.choices)

    def pre_add_existing(self, baseline):
        review = self.review_existing(baseline)
        fresh = self.step("approve_menu", review_digest=review["review_digest"])
        self.step("observe", **self.reads(fresh, data=baseline))
        return self.step("add")

    def apply_existing(self):
        baseline = cart_data({TOMATO: 1, OATS: 2})
        precheck = self.pre_add_existing(baseline)
        write = self.step("observe", **self.reads(precheck, data=baseline))
        filled = cart_data({TOMATO: 2, OATS: 2, PASTA: 1})
        applied = self.step("observe", **self.reads(self.ack(write, filled), data=filled))
        self.assertEqual(applied["status"], "applied")
        return write, baseline, filled

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
        self.assertEqual([row["item"] for row in quote["unanswered_stock"]], ["Spagetti"])
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
        self.assertTrue(self.step("defer")["maintenance_deferred"])
        program = self.root / "rollback-program"
        program.mkdir()
        with self.assertRaisesRegex(ValueError, "target program cannot read"):
            cart.require_idle(self.state, target_program=program)
        (program / "hosted_oda_menu.py").touch()
        cart.require_idle(self.state, target_program=program)
        self.identifier = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "original.*owns"):
            self.begin()

    def test_invalid_planning_manifest_fails_before_an_operation_is_created(self):
        install.write_json(self.home / "runtime.json", {"home": str(self.home), "code_root": None})
        with self.assertRaisesRegex(ValueError, "manifest is invalid"):
            self.begin()
        self.assertIsNone(install.bounded_json(self.registry / "ledger.json")["current"])

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

    def test_keep_preserves_existing_cart_and_restores_only_its_additions(self):
        applied_id = self.identifier
        write, baseline, filled = self.apply_existing()
        self.assertEqual({row["productId"]: row["quantity"] for row in write["hosted_request"]["arguments"]["operations"]},
                         {TOMATO: 1, PASTA: 1})
        applied = self.step("status")
        self.assertEqual(applied["quote"]["additional_merchandise_ore"], 3440)
        self.assertEqual(applied["quote"]["resulting_merchandise_ore"], 9710)
        self.assertEqual(applied["cart_review"]["line_amounts_ore"][str(OATS)], 4980)
        self.assertEqual(applied["cart_evidence_scope"], "historical_application")
        path = self.registry / (applied_id + ".json")
        original = path.read_bytes()
        self.identifier = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "latest verified"):
            cart.run(self.state, self.registry, self.identifier, {"action": "restore_menu", "applied_operation_id": applied_id},
                     "different_plugin_fixture", self.build)
        checking = self.step("restore_menu", applied_operation_id=applied_id)
        self.assertFalse(checking["dispatch"])
        self.assertFalse(checking["add_acknowledged"])
        self.assertEqual(checking["restores_operation_id"], applied_id)
        self.assertNotIn("cart_review", checking)
        self.assertNotIn("hosted_request", self.step("restore_menu", applied_operation_id=applied_id))
        with self.assertRaisesRegex(ValueError, "account/address context changed"):
            self.step("observe", **{**self.reads(checking, data=filled), "addresses": [{"id": 97, "isSelected": True}]})
        with self.assertRaisesRegex(ValueError, "cart changed"):
            changed = cart_data({TOMATO: 2, OATS: 2, PASTA: 1}, oats_price=OATS_PRICE + 1)
            self.step("observe", **self.reads(checking, data=changed))
        undo = self.step("observe", **self.reads(checking, data=filled))
        self.assertTrue(undo["dispatch"])
        self.assertEqual({row["productId"]: row["quantity"] for row in undo["hosted_request"]["arguments"]["operations"]},
                         {TOMATO: -1, PASTA: -1})
        received = self.ack(undo, baseline)
        self.assertEqual(received["status"], "verify_remove")
        self.assertTrue(received["remove_reply_lines_match"])
        self.assertTrue(received["independent_cart_read_required"])
        self.assertNotIn("restored_lines_match", received)
        self.assertNotIn("restoration_amounts_match", received)
        result = self.step("observe", **self.reads(received, data=baseline))
        self.assertEqual(result["status"], "restored")
        self.assertTrue(result["restoration_amounts_match"])
        self.assertTrue(result["restored_lines_match"])
        self.assertNotIn("independent_cart_read_required", result)
        self.assertEqual(path.read_bytes(), original)
        self.identifier = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "latest verified"):
            self.step("restore_menu", applied_operation_id=applied_id)

    def test_damaged_applied_menu_receipt_cannot_start_an_undo(self):
        applied_id = self.identifier
        self.apply_existing()
        path = self.registry / (applied_id + ".json")
        original = install.bounded_json(path)
        ledger = self.registry / "ledger.json"
        before = ledger.read_bytes()
        self.identifier = str(uuid.uuid4())
        for damaged in (None, {"items": [None]}):
            with self.subTest(quote=damaged):
                edited_menu = dict(original["menu"])
                if damaged is None:
                    del edited_menu["quote"]
                else:
                    edited_menu["quote"] = damaged
                install.write_json(path, {**original, "menu": edited_menu})
                with self.assertRaisesRegex(ValueError, "menu receipt is invalid"):
                    self.step("restore_menu", applied_operation_id=applied_id)
                self.assertEqual(ledger.read_bytes(), before)

    def test_existing_cart_must_match_at_both_preflight_checks(self):
        original = cart_data({TOMATO: 1, OATS: 2})
        review = self.review_existing(original)
        verifying = self.step("approve_menu", review_digest=review["review_digest"])
        changed_quantities = cart_data({TOMATO: 1, OATS: 1})
        with self.assertRaisesRegex(ValueError, "unexpected products"):
            self.step("observe", **self.reads(verifying, data=changed_quantities))
        self.step("observe", **self.reads(verifying, data=original))
        checking = self.step("add")
        with self.assertRaisesRegex(ValueError, "existing Oda cart changed after review"):
            self.step("observe", **self.reads(checking, data=cart_data({TOMATO: 1, OATS: 2}, oats_price=OATS_PRICE + 1)))
        self.assertEqual(self.step("status")["status"], "checking_add")

    def test_existing_unparsed_description_drift_is_detected(self):
        original = cart_data({OATS: 1})
        product = original["groups"][0]["items"][0]["product"]
        product["description"] = "Pakningsinfo ikke oppgitt"
        checking = self.pre_add_existing(original)
        product["description"] = "Pakning som beskrevet av produsenten"
        with self.assertRaisesRegex(ValueError, "existing Oda cart changed after review"):
            self.step("observe", **self.reads(checking, data=original))

    def test_keep_checks_original_details_of_existing_products_that_overlap_the_menu(self):
        baseline = cart_data({TOMATO: 1, OATS: 2})
        checking = self.pre_add_existing(baseline)
        write = self.step("observe", **self.reads(checking, data=baseline))
        filled = cart_data({TOMATO: 2, OATS: 2, PASTA: 1})
        tomato = next(line["product"] for line in filled["groups"][0]["items"] if line["product"]["id"] == TOMATO)
        tomato["description"] = "Økologisk, 400 g"
        result = self.step("observe", **self.reads(self.ack(write, filled), data=filled))
        self.assertEqual(result["status"], "added")
        self.assertFalse(result["cart_review"]["product_details_match"])
        self.assertFalse(result["cart_review"]["matches_review"])

    def test_fully_covered_menu_does_not_add_or_create_a_restore_entitlement(self):
        initial = self.identifier
        filled = cart_data({TOMATO: 3, OATS: 2, PASTA: 1})
        checking = self.pre_add_existing(filled)
        after = self.step("observe", **self.reads(checking, data=filled))
        self.assertEqual(after["status"], "unchanged")
        self.assertFalse(after["dispatch"])
        self.assertNotIn("hosted_request", after)
        self.assertEqual(after["quote"]["additional_package_count"], 0)
        self.assertTrue(after["cart_review"]["matches_review"])
        self.assertEqual(install.bounded_json(self.registry / "ledger.json")["write_generation"], 0)
        self.identifier = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "latest verified"):
            self.step("restore_menu", applied_operation_id=initial)

    def test_partial_cleanup_preserves_all_existing_quantities(self):
        baseline = cart_data({TOMATO: 1, OATS: 2})
        checking = self.pre_add_existing(baseline)
        write = self.step("observe", **self.reads(checking, data=baseline))
        underfilled = cart_data({TOMATO: 2, OATS: 2})
        receipt = self.ack(write, underfilled)
        self.assertEqual(receipt["partial_add_quantities"], {str(TOMATO): 1})
        self.assertEqual(self.step("observe", **self.reads(receipt, data=underfilled))["status"], "added")
        undo = self.step("observe", **self.reads(self.step("remove"), data=underfilled))
        self.assertEqual(undo["hosted_request"]["arguments"]["operations"], [{"productId": TOMATO, "quantity": -1}])

    def test_partial_reply_cannot_launder_a_removed_existing_item(self):
        baseline = cart_data({TOMATO: 1, OATS: 2})
        write = self.step("observe", **self.reads(self.pre_add_existing(baseline), data=baseline))
        wrong = self.ack(write, cart_data({TOMATO: 2, OATS: 1, PASTA: 1}))
        self.assertEqual(wrong["status"], "add_response_unverified")
        with self.assertRaisesRegex(ValueError, "does not allow"):
            self.step("remove")

    def test_deferred_nonempty_menu_requires_baseline_compatible_maintenance_target(self):
        baseline = cart_data({OATS: 2})
        write = self.step("observe", **self.reads(self.pre_add_existing(baseline), data=baseline))
        self.ack(write, cart_data({TOMATO: 2, OATS: 2, PASTA: 1}))
        self.step("defer")
        target = self.root / "program-rollback"
        target.mkdir()
        (target / "hosted_oda_menu.py").touch()
        with self.assertRaisesRegex(ValueError, "cannot preserve"):
            cart.require_idle(self.state, target_program=target)
        (target / "hosted_oda_cart.py").write_text("BASELINE_JOURNAL_VERSION = 1\n")
        cart.require_idle(self.state, target_program=target)

    def test_deferred_empty_keep_preserves_the_undo_receipt_on_maintenance(self):
        write = self.step("observe", **self.reads(self.pre_add("keep")))
        self.ack(write, cart_data({TOMATO: 2, PASTA: 1}))
        self.step("defer")
        target = self.root / "program-rollback"
        target.mkdir()
        (target / "hosted_oda_menu.py").touch()
        module = target / "hosted_oda_cart.py"
        module.write_text("BASELINE_JOURNAL_VERSION = 1\n")
        with self.assertRaisesRegex(ValueError, "undo receipt"):
            cart.require_idle(self.state, target_program=target)
        module.write_text("BASELINE_JOURNAL_VERSION = 1\nEMPTY_KEEP_JOURNAL_VERSION = 1\n")
        cart.require_idle(self.state, target_program=target)

    def test_deferred_linked_undo_requires_its_own_acknowledged_inverse(self):
        original = self.identifier
        _, baseline, _ = self.apply_existing()
        self.identifier = str(uuid.uuid4())
        self.step("restore_menu", applied_operation_id=original)
        self.step("defer")
        self.build = {**self.build, "source_commit": "e" * 40}
        recovered = self.step("observe_recovery", **self.reads(self.step("recheck"), data=baseline))
        self.assertEqual(recovered["status"], "added")
        self.assertEqual(recovered["recovery_observation"]["cart"], "baseline_unreserved")
        self.assertFalse(recovered["remove_acknowledged"])
        aborted = self.step("abort")
        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse(aborted["dispatch"])
        self.assertNotIn("hosted_request", aborted)

    def test_linked_undo_lost_reply_stays_pending_after_restart_and_baseline_read(self):
        original = self.identifier
        _, baseline, filled = self.apply_existing()
        self.identifier = str(uuid.uuid4())
        checking = self.step("restore_menu", applied_operation_id=original)
        inverse = self.step("observe", **self.reads(checking, data=filled))
        self.assertTrue(inverse["dispatch"])
        self.assertNotIn("hosted_request", self.step("restore_menu", applied_operation_id=original))
        evidence = self.step("observe_recovery", **self.reads(self.step("recheck"), data=baseline))
        self.assertEqual(evidence["status"], "awaiting_remove_response")
        self.assertFalse(evidence["remove_acknowledged"])
        with self.assertRaisesRegex(ValueError, "does not allow"):
            self.step("remove")
        with patch.object(cart, "now", return_value=hosted_oda.response_time(inverse["hosted_request"]["expires_at"])):
            self.step("defer")
        self.build = {**self.build, "source_commit": "e" * 40}
        with self.assertRaisesRegex(ValueError, "original active hosted Oda operation"):
            self.step("abort")
        self.assertEqual(self.step("status")["status"], "awaiting_remove_response")

    def test_newer_dispatched_operation_prevents_undo_of_old_keep_even_with_matched_cart(self):
        older = self.identifier
        _, _, filled = self.apply_existing()
        self.identifier = str(uuid.uuid4())
        checking = self.pre_add_existing(cart_data({OATS: 2}))
        write = self.step("observe", **self.reads(checking, data=cart_data({OATS: 2})))
        self.assertEqual(self.step("observe", **self.reads(self.ack(write, filled), data=filled))["status"], "applied")
        self.identifier = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "latest verified"):
            self.step("restore_menu", applied_operation_id=older)
