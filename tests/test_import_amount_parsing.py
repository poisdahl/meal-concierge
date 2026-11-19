"""Source amounts in a household's own imports: exact when stated, unresolved when not."""
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipe_import_readers import RecipeImportReaderError, _whole_quote, read_transcript, source_candidate
from recipe_quantities import parse_measure
from recipes import RecipeError, normalize_recipe, source_ingredient, source_yield


def amount(row):
    quantity = row["quantity"]
    return None if quantity is None else (quantity["numerator"], quantity["denominator"], row["unit"])


def webpage(*lines, language="nb", yield_text="4 personer"):
    return source_candidate({"extracted": {
        "name": "Soup", "language": language, "ingredients": list(lines), "steps": ["Simmer."],
        "yield_text": yield_text, "description": "", "notes": "", "tags": [], "times": {}, "credit": "",
        "source": {"kind": "web", "url": "https://recipes.example/soup", "relationship": "user_supplied"},
    }})


class UnresolvedLineTests(unittest.TestCase):
    def test_a_unit_word_inside_a_descriptive_line_is_not_kept_as_the_unit(self):
        cases = (
            "4 store kyllingfileter uten skinn og bein, ca. 150 g per stk",
            "2 large boneless skinless chicken breasts (about 200 g each)",
            "2 bokser hakkede tomater à 400 g hver",
        )
        for text in cases:
            with self.subTest(text=text):
                row = source_ingredient(text, language="nb")
                self.assertIsNone(row["quantity"])
                self.assertIsNone(row["unit"])
                self.assertEqual(row["item"], text)
                self.assertFalse(row["scalable"])
        # The whole recipe used to be rejected with "ingredients[1].unit is too long".
        recipe = normalize_recipe({**webpage("200 g linser", *cases), "rights": {"storage": "full"}})
        self.assertEqual([amount(row) for row in recipe["ingredients"]], [(200, 1, "g"), None, None, None])
        self.assertEqual([row["original_text"] for row in recipe["ingredients"][1:]], list(cases))

    def test_two_measured_foods_on_one_web_line_both_reach_the_cart_plan(self):
        from product_planner import build_product_plan, cart_requirements, menu_requirements
        from recipes import scale_recipe
        from test_meal_concierge_products import observation, option, product

        candidate = webpage("200 g gulrot og 100 g løk", "2dl fløte + 1dl melk", "50 g gulrot")
        recipe = scale_recipe(normalize_recipe(candidate), 4)
        menu = {"dishes": [recipe], "salads": []}
        requirements, unresolved = menu_requirements(menu)
        self.assertFalse(unresolved)
        self.assertEqual({r["item"]: r["quantity"]["numerator"] for r in requirements},
                         {"gulrot": 250, "løk": 100, "fløte": 200, "melk": 100})
        observed, approved = {}, []
        for row in requirements:
            candidate_product = product(row["item"], row["item"], 100, row["unit"], [option(100)])
            observed[row["requirement_id"]] = observation(row["search"], [candidate_product])
            approved.append({"requirement_id": row["requirement_id"], "candidate_refs": [row["item"]]})
        plan = build_product_plan(provider="oda", binding={}, menu=menu, observations=observed,
                                  candidate_approvals=approved)
        self.assertEqual({row["product_id"]: row["quantity"] for row in cart_requirements(plan)},
                         {"gulrot": 3, "løk": 1, "fløte": 2, "melk": 1})
        for piece, qty_unit in zip(candidate["ingredients"][:4],
                ("200 g gulrot", "100 g løk", "2dl fløte", "1dl melk")):
            self.assertEqual(piece["original_text"], qty_unit)
            self.assertEqual(piece["evidence"]["quantity"]["input"], qty_unit)

    def test_unknown_compound_or_alternative_does_not_buy_only_the_first_amount(self):
        for text in ("1 ss olje eller 2 ss smør", "1 dl melk + 2 dl til pensling",
                     "200 g gulrot (og 100 g løk)", "2 dl fløte og 1 dl ukjent vare"):
            with self.subTest(text=text):
                candidate = webpage(text)
                self.assertEqual(len(candidate["ingredients"]), 1)
                row = candidate["ingredients"][0]
                self.assertEqual(row["original_text"], text)
                self.assertIsNone(row["quantity"])
                self.assertFalse(row["scalable"])

    def test_optional_marker_only_applies_to_its_named_component(self):
        for text, optionality in (
            ("200 g gulrot og 100 g løk (kan sløyfes)", [False, True]),
            ("200 g gulrot (optional) + 100 g løk", [True, False]),
            ("1 dl melk + 2 dl ukjent vare (optional)", [False]),
            ("1 dl melk (optional) + 2 dl ukjent vare", [False]),
        ):
            with self.subTest(text=text):
                rows = normalize_recipe(webpage(text))["ingredients"]
                self.assertEqual([row["optional"] for row in rows], optionality)

    def test_transcript_splits_named_amounts_with_the_complete_page_quote_as_evidence(self):
        def transcript(lines):
            return read_transcript({"kind": "pasted_text",
                "pages": [{"page": 1, "text": "\n".join(["Suppe", "4 personer", *lines, "Kok alt."])}],
                "interpretation": {"name": "Suppe", "language": "nb", "yield": {"page": 1, "quote": "4 personer"},
                    "ingredients": [{"page": 1, "quote": line} for line in lines],
                    "steps": [{"page": 1, "quote": "Kok alt."}]}})["candidate"]

        line = "200 g gulrot og 100 g løk (valgfri)"
        rows = transcript([line])["ingredients"]
        self.assertEqual([(r["item"], amount(r), r["optional"]) for r in rows],
                         [("gulrot", (200, 1, "g"), False), ("løk", (100, 1, "g"), True)])
        for row in rows:
            self.assertEqual(row["evidence"]["quantity"]["input"], "Page 1: " + line)
            self.assertEqual(row["evidence"]["unit"]["input"], "Page 1: " + line)
        rows = transcript([line] + ["5 g salt"] * 199)["ingredients"]
        self.assertEqual(len(rows), 200)
        self.assertFalse(rows[0]["scalable"])
        self.assertFalse(rows[0]["optional"])
        self.assertEqual(rows[-1]["item"], "salt")
        with self.assertRaisesRegex(RecipeImportReaderError, "alternatives"):
            transcript(["200 g gulrot eller 100 g løk"])
        with self.assertRaisesRegex(RecipeImportReaderError, "dependencies"):
            transcript(["200 g gulrot og 100 g saus fra i går"])

    def test_a_structured_measure_still_reports_its_unsupported_unit(self):
        row = source_ingredient("2 ladles ris", item="ris", measure="2 ladles")
        self.assertEqual((row["quantity"], row["unit"], row["item"]), (None, "ladles", "ris"))

    def test_a_number_that_may_be_thousands_or_decimals_is_not_an_exact_amount(self):
        for text in ("1,200 g plain flour", "1.500 g mel", "2,000 ml vann", "999.999 g sukker"):
            with self.subTest(text=text):
                row = source_ingredient(text, language="en")
                self.assertIsNone(row["quantity"])
                self.assertFalse(row["scalable"])
                self.assertEqual(row["item"], text)
        self.assertEqual(parse_measure("1,200 g"), (None, "g"))
        exact = {"1200 g mel": (1200, 1, "g"), "1,25 dl melk": (5, 4, "dl"), "1,5 kg poteter": (3, 2, "kg"),
                 "0,125 l fløte": (1, 8, "l"), "0.375 kg smør": (3, 8, "kg"), "12,5 g gjær": (25, 2, "g"),
                 "1,2000 g salt": (6, 5, "g"), "1000,5 g mel": (2001, 2, "g")}
        for text, expected in exact.items():
            with self.subTest(text=text):
                self.assertEqual(amount(source_ingredient(text, language="nb")), expected)

    def test_a_line_with_a_second_amount_is_not_read_as_the_first_amount_only(self):
        for text in ("1 dl + 2 ss sukker", "1 ss og 1 ts sukker", "20 g plus 20 g parmesan",
                     "2 dl and 1 tbsp cream", "1 dl +½ dl melk"):
            with self.subTest(text=text):
                row = source_ingredient(text, language="nb")
                self.assertIsNone(row["quantity"])
                self.assertIsNone(row["unit"])
                self.assertEqual(row["item"], text)
        for text in ("1 dl pluss 2 ss sukker", "1 dl & 2 ss sukker", "1 dl samt 2 ss sukker",
                     "250 g til 300 g sopp", "250 g - 300 g sopp", "250 g – 300 g sopp", "1 kg 200 g mel",
                     "1 dl / 2 dl melk", "1 dl eller 2 dl melk", "1 dl or 2 dl milk", "1 dl | 2 dl milk"):
            with self.subTest(text=text):
                self.assertIsNone(source_ingredient(text, language="nb")["quantity"])
        for text in ("250g - 300g sopp", "250g til 300g sopp", "1kg 200g mel"):
            with self.subTest(text=text):
                self.assertIsNone(source_ingredient(text, language="nb", attached_metric_units=True)["quantity"])
        # Words after the amount are still the ingredient.
        self.assertEqual(source_ingredient("2 ss olje og salt", language="nb")["item"], "olje og salt")
        self.assertEqual(amount(source_ingredient("1 dl plussmelk", language="nb")), (1, 1, "dl"))
        percent = source_ingredient("1 dl 2% melk", language="nb")
        self.assertEqual((amount(percent), percent["item"]), ((1, 1, "dl"), "2% melk"))
        self.assertEqual(source_ingredient("2 ss olje til steking", language="nb")["item"], "olje til steking")

    def test_text_spanning_several_lines_is_not_read_as_one_ingredient(self):
        row = source_ingredient("2 dl melk\n3 egg", language="nb")
        self.assertEqual((row["quantity"], row["item"]), (None, "2 dl melk\n3 egg"))
        self.assertEqual(amount(source_ingredient("2 dl melk\r\n", language="nb")), (2, 1, "dl"))

    def test_a_number_from_structured_data_keeps_its_decimal_reading(self):
        # Mealie stores the quantity as a number, so three decimals are an eighth, not thousands.
        for measure, expected in (("1.125 kilogram", (9, 8, "kilogram")), ("2.125 liter", (17, 8, "liter")),
                                  ("1.333 tablespoon", (1333, 1000, "tablespoon"))):
            with self.subTest(measure=measure):
                row = source_ingredient(measure + " mel", item="mel", measure=measure, numeric_measure=True)
                self.assertEqual(amount(row), expected)
                self.assertTrue(row["scalable"])
        # A measure that was written text keeps the ambiguity.
        written = source_ingredient("1,200 g flour", item="flour", measure="1,200 g", language="en")
        self.assertEqual((written["quantity"], written["unit"]), (None, "g"))

    def test_long_runs_of_whitespace_do_not_make_unit_matching_slow(self):
        # One such 500-character line took about 150 ms; a page can carry hundreds.
        line = "a" + " " * 498 + "b"
        started = time.monotonic()
        rows = [source_ingredient(line, language="nb") for _ in range(50)]
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(all(row["quantity"] is None and row["original_text"] == line for row in rows))
        spaced = source_ingredient("2    dl \t melk", language="nb")
        self.assertEqual((amount(spaced), spaced["item"], spaced["original_text"]), ((2, 1, "dl"), "melk", "2    dl \t melk"))

    def test_a_unit_that_grows_when_case_folded_is_rejected_as_too_long(self):
        recipe = {"schema_version": 2, "name": "Soup", "portions": 2, "steps": ["Simmer."],
                  "source": {"kind": "user", "relationship": "user_supplied"}, "rights": {"storage": "full"},
                  "ingredients": [{"item": "salt", "quantity": 1, "unit": "ß" * 40, "scalable": False}]}
        with self.assertRaisesRegex(RecipeError, r"ingredients\[0\]\.unit is too long"):
            normalize_recipe(recipe)
        recipe["ingredients"][0]["unit"] = "ß" * 20
        stored = normalize_recipe(recipe)
        self.assertEqual(normalize_recipe(stored), stored)
        # The limit is measured as the stored form is measured when read back:
        # this unit folds to 42 code points that recompose to 21, and still loads.
        recipe["ingredients"][0]["unit"] = "ǰ" * 21
        normalize_recipe(normalize_recipe(recipe))


class WholeQuoteTests(unittest.TestCase):
    TEXT = ("Suppe\n14 porsjoner\n\n1,5 dl fløte\n1500 g poteter\n12 fedd hvitløk\n1½ dl melk\n"
            "2 ss olje eller smør\n15 dl vann, eller 5 dl vann og buljong\n\nKok alt i 20 minutter.")

    def source(self, *, ingredient="1,5 dl fløte", step="Kok alt i 20 minutter.", yield_quote="14 porsjoner"):
        return {"kind": "pasted_text", "pages": [{"page": 1, "text": self.TEXT}], "interpretation": {
            "name": "Suppe", "language": "nb", "yield": {"page": 1, "quote": yield_quote},
            "ingredients": [{"page": 1, "quote": ingredient}], "steps": [{"page": 1, "quote": step}]}}

    def test_a_quote_that_cuts_a_number_or_word_is_not_source_evidence(self):
        # Each of these is a substring of the page, and a different amount than the page states.
        cases = [{"ingredient": quote} for quote in
                 ("5 dl fløte", "500 g poteter", "2 fedd hvitløk", "½ dl melk", "1,5 dl fl", ",5 dl fløte")]
        cases += [{"yield_quote": "4 porsjoner"}, {"step": "ok alt i 20 minutter."}, {"step": "Kok alt i 2"}]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RecipeImportReaderError, "starts or ends inside a number or word"):
                    read_transcript(self.source(**changes))

    def test_whole_quotes_and_source_supported_choices_still_pass(self):
        recipe = read_transcript(self.source())["candidate"]
        self.assertEqual(recipe["portions"], 14.0)
        self.assertEqual(amount(recipe["ingredients"][0]), (3, 2, "dl"))
        for quote, expected in (("1500 g poteter", (1500, 1, "g")), ("12 fedd hvitløk", (12, 1, "fedd")),
                                ("1½ dl melk", (3, 2, "dl")), ("2 ss olje", (2, 1, "ss")),
                                # A later whole occurrence counts even when an earlier one is cut.
                                ("5 dl vann", (5, 1, "dl"))):
            with self.subTest(quote=quote):
                row = read_transcript(self.source(ingredient=quote))["candidate"]["ingredients"][0]
                self.assertEqual(amount(row), expected)
                evidence = row["evidence"]["quantity"]
                self.assertEqual((evidence["basis"], evidence["input"]), ("source", "Page 1: " + quote))
        for step in ("Kok alt", "Kok alt i 20 minutter", "i 20 minutter."):
            with self.subTest(step=step):
                self.assertEqual(read_transcript(self.source(step=step))["candidate"]["steps"], [step])
        with self.assertRaisesRegex(RecipeImportReaderError, "absent from its supplied page"):
            read_transcript(self.source(ingredient="2 dl fløte"))

    def test_a_quote_cannot_take_part_of_a_spaced_number_a_range_or_a_multiplied_amount(self):
        cases = (("500 g poteter", "1 500 g poteter"), ("500 g poteter", "1\u00a0500 g poteter"),
                 ("1/2 dl melk", "1 1/2 dl melk"), ("½ dl melk", "1 ½ dl melk"),
                 ("2 ss sukker", "1-2 ss sukker"), ("2 ss sukker", "1–2 ss sukker"),
                 ("2 dl fløte", "1,5-2 dl fløte"), ("400 g tomater", "2 x 400 g tomater"),
                 ("400 g tomater", "2x400 g tomater"), ("4 porsjoner", "2-4 porsjoner"), ("1", "1 500 g"))
        cases += (("500 g poteter", "1  500 g poteter"), ("500 g poteter", "1\t500 g poteter"),
                  ("2 ss sukker", "1 / 2 ss sukker"), ("2 ss sukker", "1  -  2 ss sukker"),
                  ("400 g tomater", "2  x  400 g tomater"), ("1", "1  500 g"),
                  ("1", "1  / 2 ss"), ("2", "2  x  400 g tomater"))
        for quote, text in cases:
            with self.subTest(quote=quote, text=text):
                self.assertFalse(_whole_quote("Suppe\n" + text + "\nKok alt.", quote))
        # The same quote as a line of its own, after a list number or after a sentence, is whole.
        for quote, text in (("500 g poteter", "500 g poteter"), ("2 ss sukker", "1. 2 ss sukker"),
                            ("2 ss sukker", "Kok i 10 minutter. 2 ss sukker"), ("400 g tomater", "boks 400 g tomater"),
                            ("2 dl fløte", "1 løk\n2 dl fløte"), ("1 500 g poteter", "1 500 g poteter")):
            with self.subTest(quote=quote, text=text):
                self.assertTrue(_whole_quote("Suppe\n" + text + "\nKok alt.", quote))

    def test_text_glued_to_a_list_number_or_heading_can_still_be_quoted(self):
        # A page reader adds no separator between inline elements.
        text = "Suppe\n4 porsjoner\nSaus2 dl fløte\n1 løk\n1Kok vannet.\n2Tilsett fløte1"
        for quote in ("2 dl fløte", "Kok vannet.", "Tilsett fløte"):
            with self.subTest(quote=quote):
                self.assertTrue(_whole_quote(text, quote))

    def test_a_page_that_repeats_the_quote_is_not_searched_without_limit(self):
        started = time.monotonic()
        self.assertFalse(_whole_quote("a" * 65536, "a" * 8000))
        self.assertFalse(_whole_quote("ab" * 32768, "ab" * 4000))
        self.assertLess(time.monotonic() - started, 1)


class HouseholdImportAmountTests(unittest.TestCase):
    def test_a_metric_unit_written_against_its_number_is_an_exact_source_amount(self):
        cases = [
            ("600g carrots", (600, 1, "g"), "carrots"),
            ("1,5kg poteter", (3, 2, "kg"), "poteter"),
            ("1l vann", (1, 1, "l"), "vann"),
            ("125ML melk", (125, 1, "ml"), "melk"),
            ("2.5dl fløte", (5, 2, "dl"), "fløte"),
        ]
        for text, expected, item in cases:
            with self.subTest(text=text):
                row = source_ingredient(text, language="nb", attached_metric_units=True)
                self.assertEqual(amount(row), expected)
                self.assertEqual(row["item"], item)
                self.assertTrue(row["scalable"])
                self.assertEqual(row["original_text"], text)
                self.assertEqual(row["evidence"]["quantity"], {"basis": "source", "input": text})
                self.assertEqual(row["evidence"]["unit"], {"basis": "source", "input": text})

    def test_counts_containers_and_ambiguous_attached_amounts_still_need_a_reading(self):
        # A bare count or a container has no exact amount; the host supplies an estimate.
        for text in ("1 løk", "2 eggs", "½ sitron", "1 boks hermetiske tomater", "2 cups flour",
                     "600g", "1l", "2gulrøtter", "1 liten løk", "salt", "1,200g flour", "20g + 20g parmesan"):
            with self.subTest(text=text):
                row = source_ingredient(text, language="nb", attached_metric_units=True)
                self.assertIsNone(row["quantity"])
                self.assertFalse(row["scalable"])
                self.assertEqual(row["item"], text)

    def test_other_callers_do_not_read_attached_units(self):
        # The curated collection build recovers these itself, with its own review.
        unchanged = source_ingredient("600g carrots", language="en")
        self.assertIsNone(unchanged["quantity"])
        self.assertFalse(unchanged["scalable"])

    def test_norwegian_person_yields_are_person_portions(self):
        for text in ("4 personer", "1 person", "4 pers", "4 pers.", "6 portioner"):
            with self.subTest(text=text):
                self.assertEqual(source_yield(text)[1], float(text.split()[0]))
        # A physical yield, a bare number or a range is still not a person count.
        for text in ("2 brød", "4", "4-6 personer"):
            with self.subTest(text=text):
                self.assertIsNone(source_yield(text)[1])

    def test_pasted_text_and_webpage_imports_read_attached_metric_units(self):
        text = "Suppe\n4 personer\n\n1 løk\n500g linser\n\nKok alt."
        result = read_transcript({"kind": "pasted_text", "pages": [{"page": 1, "text": text}], "interpretation": {
            "name": "Suppe", "language": "nb", "yield": {"page": 1, "quote": "4 personer"},
            "ingredients": [{"page": 1, "quote": "1 løk"}, {"page": 1, "quote": "500g linser"}],
            "steps": [{"page": 1, "quote": "Kok alt."}]}})["candidate"]
        self.assertEqual(result["portions"], 4.0)
        self.assertEqual(result["portions_evidence"]["basis"], "source")
        self.assertEqual([amount(row) for row in result["ingredients"]], [None, (500, 1, "g")])
        self.assertEqual(result["ingredients"][1]["evidence"]["quantity"]["input"], "Page 1: 500g linser")
        page = webpage("140g red lentils", "2 eggs", language="en")
        self.assertEqual(page["portions"], 4.0)
        self.assertEqual([amount(row) for row in page["ingredients"]], [(140, 1, "g"), None])

    def test_a_published_yield_list_uses_its_labelled_servings(self):
        # Recipe plugins publish recipeYield ["4", "4 servings"], joined by the reader.
        page = webpage("200 g linser", yield_text="4; 4 servings")
        self.assertEqual(page["portions"], 4.0)
        self.assertEqual((page["portions_evidence"]["basis"], page["portions_evidence"]["input"]),
                         ("source", "4; 4 servings"))
        self.assertEqual((page["yield"]["original_text"], page["yield"]["unit"]), ("4; 4 servings", "servings"))
        for yield_text in ("4; 12 muffins", "4", "2 brød; 20 skiver"):
            with self.subTest(yield_text=yield_text):
                self.assertIsNone(webpage("200 g linser", yield_text=yield_text)["portions"])


if __name__ == "__main__":
    unittest.main()
