import unittest

from checkout_identity import checkout_line_mismatch, checkout_lines_match, review_checkout_lines


def product(name="Kaffe", description="Malt, 500 g", brand="God", quantity=1, **extra):
    return {"name": name, "description": description, "brand": brand,
            "quantity": quantity, **extra}


def row(title="Kaffe", subtitle="Malt, 500 g, God", quantity=1, **extra):
    return {"title": title, "subtitle": subtitle, "quantity": quantity, **extra}


class CheckoutIdentityTests(unittest.TestCase):
    def test_review_resolves_only_unmatched_cosmetic_display_with_frozen_digest(self):
        expected = [product("Kaffe", product_id="sku-a")]
        actual = [row("Kaffe®")]
        proposed = review_checkout_lines(expected, actual, binding={"cart": "v1"})
        self.assertFalse(proposed["matched"])
        issue = proposed["issue"]
        self.assertEqual(issue["expected"], [{"index": 0, **expected[0]}])
        self.assertEqual(issue["actual"], [{"index": 0, **actual[0]}])
        self.assertEqual(len(issue["digest"]), 64)
        decision = {"digest": issue["digest"], "decisions": [
            {"expected_index": 0, "actual_index": 0, "reason": "Merchant adds a trademark symbol"}]}
        accepted = review_checkout_lines(expected, actual, binding={"cart": "v1"}, review=decision)
        self.assertEqual(accepted, {"matched": True, "review": decision})
        self.assertEqual(review_checkout_lines(expected, actual, binding={"cart": "v1"},
                                               review=accepted["review"]), accepted)
        with self.assertRaisesRegex(ValueError, "stale"):
            review_checkout_lines(expected, actual, binding={"cart": "v2"}, review=accepted["review"])
        with self.assertRaisesRegex(ValueError, "stale"):
            review_checkout_lines(expected, [row("Kaffe™")], binding={"cart": "v1"},
                                  review=accepted["review"])
        self.assertEqual(review_checkout_lines(expected, actual, binding={"cart": "v1", "account": "x"})["issue"]["digest"],
                         review_checkout_lines(expected, actual, binding={"account": "x", "cart": "v1"})["issue"]["digest"])

    def test_review_automatically_accepts_empty_and_exact_checkout(self):
        self.assertEqual(review_checkout_lines([], [], binding="delivery-only"), {"matched": True})
        self.assertEqual(review_checkout_lines([product()], [row()], binding="cart"), {"matched": True})

    def test_review_requires_complete_unique_reasoned_assignment(self):
        expected = [product("Kaffe", product_id="sku-a"), product("Te", "20 poser", "Nord", product_id="sku-b")]
        actual = [row("Tea", "20 poser, Nord"), row("Kaffe®")]
        issue = review_checkout_lines(expected, actual, binding="cart")['issue']
        self.assertEqual([entry["index"] for entry in issue["expected"]], [0, 1])
        self.assertEqual([entry["index"] for entry in issue["actual"]], [0, 1])
        for decisions in (
            [{"expected_index": 0, "actual_index": 1, "reason": "same"}],
            [{"expected_index": 0, "actual_index": 1, "reason": "same"},
             {"expected_index": 1, "actual_index": 1, "reason": "same"}],
            [{"expected_index": 0, "actual_index": 1, "reason": " "},
             {"expected_index": 1, "actual_index": 0, "reason": "same"}],
        ):
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                review_checkout_lines(expected, actual, binding="cart",
                                      review={"digest": issue["digest"], "decisions": decisions})
        decisions = [{"expected_index": 1, "actual_index": 0, "reason": "Translated title"},
                     {"expected_index": 0, "actual_index": 1, "reason": "Trademark suffix"}]
        accepted = review_checkout_lines(expected, actual, binding="cart",
                                         review={"digest": issue["digest"], "decisions": decisions})
        self.assertTrue(accepted["matched"])
        self.assertEqual([entry["expected_index"] for entry in accepted["review"]["decisions"]], [0, 1])

    def test_review_cannot_override_quantity_ids_or_native_conflict(self):
        expected = [product("Kaffe", product_id="sku-a")]
        for actual in ([row("Kaffe®", quantity=2)],
                       [row("Kaffe®", product_id="sku-b")],
                       [row("Kaffe®", identity_conflict=True)], [], [row(), row()]):
            with self.subTest(actual=actual):
                issue = review_checkout_lines(expected, actual, binding="cart")['issue']
                self.assertIn("refresh", issue["next"])
                with self.assertRaisesRegex(ValueError, "cannot override"):
                    review_checkout_lines(expected, actual, binding="cart", review={
                        "digest": issue["digest"], "decisions": [
                            {"expected_index": 0, "actual_index": 0, "reason": "same"}]})
        self.assertFalse(checkout_lines_match(expected, [row(identity_conflict=True)]))

    def test_proven_ids_and_exact_display_pairs_are_locked(self):
        expected = [product("Kaffe", product_id="sku-a"),
                    product("Te", "20 poser", "Nord", product_id="sku-b"),
                    product("Melk", "1 l", "Tine", product_id="sku-c")]
        actual = [row("Changed merchant title", "Other", product_id="sku-a"),
                  row("Te", "20 poser, Nord"), row("Milk", "1 l, Tine")]
        issue = review_checkout_lines(expected, actual, binding="cart")['issue']
        self.assertEqual([entry["index"] for entry in issue["expected"]], [2])
        self.assertEqual([entry["index"] for entry in issue["actual"]], [2])
        with self.assertRaisesRegex(ValueError, "remaps"):
            review_checkout_lines(expected, actual, binding="cart", review={
                "digest": issue["digest"], "decisions": [
                    {"expected_index": 0, "actual_index": 2, "reason": "same"}]})
        self.assertTrue(review_checkout_lines(expected, actual, binding="cart", review={
            "digest": issue["digest"], "decisions": [
                {"expected_index": 2, "actual_index": 2, "reason": "English title"}]})["matched"])

    def test_duplicate_native_ids_cannot_be_reviewed(self):
        expected = [product("Kaffe", product_id="sku-a"), product("Te", product_id="sku-a")]
        actual = [row("Kaffe"), row("Te")]
        issue = review_checkout_lines(expected, actual, binding="cart")['issue']
        self.assertIn("ambiguous product IDs", issue["next"])
        with self.assertRaisesRegex(ValueError, "cannot override"):
            review_checkout_lines(expected, actual, binding="cart", review={
                "digest": issue["digest"], "decisions": []})
        actual = [row("Kaffe", product_id="sku-a"), row("Te", product_id="sku-a")]
        expected = [product("Kaffe", product_id="sku-a"),
                    product("Te", product_id="sku-b")]
        self.assertIn("ambiguous product IDs",
                      review_checkout_lines(expected, actual, binding="cart")['issue']["next"])

    def test_review_cannot_invent_an_assignment_for_indistinguishable_products(self):
        same_expected = [product(product_id="sku-a"), product(product_id="sku-b")]
        different_expected = [product("Kaffe Blå", product_id="sku-a"),
                              product("Kaffe Rød", product_id="sku-b")]
        for expected, actual in (
            (same_expected, [row(), row()]),
            (same_expected, [row("Kaffe®"), row("Kaffe™")]),
            (different_expected, [row(), row()]),
        ):
            with self.subTest(expected=expected, actual=actual):
                issue = review_checkout_lines(expected, actual, binding="cart")["issue"]
                self.assertIn("indistinguishable", issue["next"])
                with self.assertRaisesRegex(ValueError, "cannot override indistinguishable"):
                    review_checkout_lines(expected, actual, binding="cart", review={
                        "digest": issue["digest"], "decisions": [
                            {"expected_index": index, "actual_index": index,
                             "reason": "Same coffee, package and brand."}
                            for index in range(2)]})

    def test_native_identity_or_quantity_can_distinguish_equal_displays(self):
        expected = [product(product_id="sku-a"), product(product_id="sku-b")]
        actual = [row(product_id="sku-b"), row()]
        self.assertEqual(review_checkout_lines(expected, actual, binding="cart"),
                         {"matched": True})
        self.assertEqual(review_checkout_lines(
            [product(quantity=1), product(quantity=2)],
            [row(quantity=2), row(quantity=1)], binding="cart"), {"matched": True})

    def test_empty_checkout_assignment_is_valid_only_when_both_sides_are_empty(self):
        self.assertTrue(checkout_lines_match([], []))
        self.assertIsNone(checkout_line_mismatch([], []))
        for expected, actual in (([], [row()]), ([product()], []), (None, []), ([], None)):
            with self.subTest(expected=expected, actual=actual):
                self.assertFalse(checkout_lines_match(expected, actual))

    def test_exact_visible_fields_and_integer_valued_js_quantity(self):
        self.assertTrue(checkout_lines_match([product(quantity=1.0)], [row()]))
        self.assertTrue(checkout_lines_match(
            [product("God Kaffe Malt, 500g")], [row()]))
        self.assertTrue(checkout_lines_match(
            [product("CAFÉ", "Malt, 500g", "GOD")],
            [row("cafe\u0301", "  malt,   500 g, god  ")]))
        self.assertFalse(checkout_lines_match(
            [product("Kaffe!", "Malt, 500 g", "God")], [row()]))

    def test_brand_prefix_and_complete_description_suffix_are_finite_alternatives(self):
        expected = [product("God Kaffe Malt, 500g")]
        self.assertTrue(checkout_lines_match(expected, [row("Kaffe")]))
        self.assertTrue(checkout_lines_match(expected, [row("God Kaffe")]))
        self.assertTrue(checkout_lines_match(expected, [row("Kaffe Malt, 500 g")]))

    def test_does_not_delete_mild_or_repeated_god_from_the_name(self):
        self.assertFalse(checkout_lines_match(
            [product("Mild Yoghurt", "500 g", "Tine")],
            [row("Yoghurt", "500 g, Tine")]))
        self.assertFalse(checkout_lines_match(
            [product("God Kaffe God", "Malt", "God")],
            [row("Kaffe", "Malt, God")]))
        self.assertFalse(checkout_lines_match(
            [product("Kaffe Kaffe", "Malt", "")],
            [row("Kaffe", "Malt")]))

    def test_unit_spacing_is_equivalent_but_wrong_weight_is_not(self):
        self.assertTrue(checkout_lines_match(
            [product("Kaffe 500g", "Malt, 500g", "God")],
            [row("Kaffe", "Malt, 500 g, God")]))
        self.assertFalse(checkout_lines_match(
            [product("Kaffe", "Malt, 500 g", "God")],
            [row("Kaffe", "Malt, 400 g, God")]))

    def test_quantity_must_be_bound_to_each_row(self):
        expected = [product("Kaffe", quantity=2), product("Te", "20 poser", "Nord", quantity=1)]
        actual = [row("Kaffe", quantity=1), row("Te", "20 poser, Nord", quantity=2)]
        self.assertFalse(checkout_lines_match(expected, actual))
        self.assertFalse(checkout_lines_match(
            [product("Kaffe", quantity=1), product("Kaffe", quantity=1)],
            [row("Kaffe", quantity=2)]))
        for quantity in (0, -1, 1.5, True, float("nan")):
            with self.subTest(quantity=quantity):
                self.assertFalse(checkout_lines_match([product()], [row(quantity=quantity)]))

    def test_order_and_complete_comma_fields_matter(self):
        self.assertFalse(checkout_lines_match(
            [product("Kaffe", "Malt, 500 g", "God")],
            [row("Kaffe", "500 g, Malt, God")]))
        self.assertFalse(checkout_lines_match(
            [product("Kaffe Malt", "Malt fin, 500 g", "God")],
            [row("Kaffe", "Malt fin, 500 g, God")]))
        self.assertFalse(checkout_lines_match(
            [product("Purre, Norge", "Sør-Norge, 1 stk", "")],
            [row("Purre", "Sør-Norge, 1 stk")]))

    def test_decimal_comma_remains_inside_one_field(self):
        self.assertTrue(checkout_lines_match(
            [product("Saft 0,5l", "0,5 l, Sitron", "")],
            [row("Saft", "0,5 l, Sitron")]))
        self.assertFalse(checkout_lines_match(
            [product("Saft 5l", "0,5 l, Sitron", "")],
            [row("Saft", "0,5 l, Sitron")]))

    def test_ids_override_display_only_when_both_are_row_proven_and_equal(self):
        self.assertTrue(checkout_lines_match(
            [product("Kaffe", product_id="sku-a")],
            [row("Changed merchant display", "Other", product_id="sku-a")]))
        self.assertFalse(checkout_lines_match(
            [product("Kaffe", product_id="sku-a")],
            [row(product_id="sku-b")]))
        self.assertFalse(checkout_lines_match(
            [product("Kaffe", quantity=2, product_id="sku-a")],
            [row(quantity=1, product_id="sku-a")]))
        self.assertTrue(checkout_lines_match(
            [product("Kaffe", product_id="sku-a")], [row()]))

    def test_one_to_one_assignment_must_be_unique(self):
        expected = [product(), product()]
        actual = [row(), row()]
        self.assertEqual(checkout_line_mismatch(expected, actual),
                         "checkout row assignment is ambiguous")
        self.assertFalse(checkout_lines_match(expected, actual))
        self.assertTrue(checkout_lines_match(
            [product(product_id="sku-a"), product(product_id="sku-b")],
            [row(product_id="sku-b"), row(product_id="sku-a")]))
        self.assertTrue(checkout_lines_match(
            [product("God Kaffe"), product("Kaffe")],
            [row("Kaffe"), row("God Kaffe")]))

    def test_more_than_two_hundred_distinct_rows_and_bounded_diagnostics(self):
        expected = [product(f"Product {index}") for index in range(201)]
        actual = [row(f"Product {index}") for index in range(201)]
        self.assertTrue(checkout_lines_match(expected, actual))
        changed = [row(f"Other {index}") for index in range(201)]
        reason = checkout_line_mismatch(expected, changed)
        self.assertIn("expected rows 0,1,2,3,4,5,6,7 (+193 more)", reason)
        self.assertLess(len(reason), 128)

    def test_diagnostics_do_not_include_product_text(self):
        reason = checkout_line_mismatch([product("Private Product")], [row("Other")])
        self.assertIn("expected rows 0", reason)
        self.assertNotIn("Private", reason)


if __name__ == "__main__":
    unittest.main()
