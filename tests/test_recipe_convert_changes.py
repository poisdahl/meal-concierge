"""Bounded conversion of an imported recipe: translation and structured amounts."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import StateStore
from email.message import Message
from urllib.parse import urlsplit

from recipe_import_readers import RecipeImportReaderError, read_transcript
from recipe_import_sources import _same_site_redirect
from recipes import RecipeError, convert_recipe_changes, normalize_recipe
from service import Application
from test_meal_concierge_products import FakeProvider


DECISION = {"storage": "full", "basis": "private_use",
            "evidence": "The user pasted this recipe and asked to keep it for household use."}
ENGLISH = ("Lentil soup\nServes 4\n\n2 tsp cumin seeds\n600 g carrots, grated\n1 onion\n"
           "1 can chopped tomatoes\n\nToast the cumin.\nSimmer everything for 20 minutes.")
ENGLISH_LINES = ["2 tsp cumin seeds", "600 g carrots, grated", "1 onion", "1 can chopped tomatoes"]
ENGLISH_STEPS = ["Toast the cumin.", "Simmer everything for 20 minutes."]


def transcript(text, lines, steps, *, language, yield_quote=None, name="Soup"):
    interpretation = {"name": name, "language": language,
                      "ingredients": [{"page": 1, "quote": line} for line in lines],
                      "steps": [{"page": 1, "quote": step} for step in steps]}
    if yield_quote:
        interpretation["yield"] = {"page": 1, "quote": yield_quote}
    return {"kind": "pasted_text", "pages": [{"page": 1, "text": text}], "interpretation": interpretation}


class BoundedConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        store = StateStore(Path(self.temp.name) / "state", {
            "instance": "import-localize", "household": "Synthetic household",
            "provider": "oda", "profile_overrides": {},
        })
        self.app = Application(store, FakeProvider(), object())
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})

    def imported(self, *, yield_quote="Serves 4"):
        return self.app.handle({
            "operation": "recipes", "action": "import", "source_kind": "transcript", "storage_decision": DECISION,
            "transcript": transcript(ENGLISH, ENGLISH_LINES, ENGLISH_STEPS, language="en",
                                     yield_quote=yield_quote, name="Lentil soup"),
        })

    def convert(self, preview, changes, **extra):
        return self.app.handle({"operation": "recipes", "action": "convert",
                                "discovery_ref": preview["discovery_ref"], "recipe_digest": preview["recipe_digest"],
                                "source_schema_version": 2, "changes": changes, **extra})

    def test_translation_keeps_source_amounts_and_labels_only_new_readings(self):
        preview = self.imported()
        self.assertEqual(preview["suggested_status"], "draft")
        self.assertEqual(preview["readiness"]["missing_decisions"],
                         ["ingredients.2.quantity", "ingredients.3.quantity"])
        converted = self.convert(preview, {
            "language": "nb", "name": "Linsesuppe",
            "steps": ["Rist spisskummen.", "La alt småkoke i 20 minutter."],
            "ingredients": [
                {"index": 0, "item": "spisskummenfrø", "unit": "ts"},
                {"index": 1, "item": "gulrøtter", "notes": "revet"},
                {"index": 2, "item": "gul løk", "quantity": 1, "unit": "stk",
                 "assumptions": "The source lists one onion; read as one medium onion."},
                {"index": 3, "item": "hermetiske hakkede tomater", "quantity": 400, "unit": "g",
                 "assumptions": "One standard can of chopped tomatoes is 400 g."},
            ],
        })
        recipe = converted["recipe"]
        self.assertEqual(converted["suggested_status"], "active")
        self.assertEqual((recipe["language"], recipe["name"], recipe["portions"]), ("nb", "Linsesuppe", 4.0))
        self.assertEqual(recipe["steps"], ["Rist spisskummen.", "La alt småkoke i 20 minutter."])
        self.assertEqual([row["item"] for row in recipe["ingredients"]],
                         ["spisskummenfrø", "gulrøtter", "gul løk", "hermetiske hakkede tomater"])
        self.assertEqual([row["amount"] for row in recipe["ingredients"]], ["2 ts", "600 g", "1 stk", "400 g"])
        self.assertEqual([row["original_text"] for row in recipe["ingredients"]], ENGLISH_LINES)
        original = preview["recipe"]["ingredients"]
        # A pure rename, and an exact alias of the same unit, keep the import's evidence.
        for index in (0, 1):
            self.assertEqual(recipe["ingredients"][index]["evidence"], original[index]["evidence"])
        self.assertEqual(recipe["ingredients"][1]["evidence"]["quantity"]["basis"], "source")
        self.assertEqual(recipe["ingredients"][1]["notes"], "revet")
        for index, source_line in ((2, "1 onion"), (3, "1 can chopped tomatoes")):
            for field in ("quantity", "unit"):
                evidence = recipe["ingredients"][index]["evidence"][field]
                self.assertEqual((evidence["basis"], evidence["input"]), ("estimate", "Page 1: " + source_line))
                self.assertTrue(evidence["assumptions"])
        # Attribution, rights and the portion evidence belong to the source, not the caller.
        for field in ("source", "rights", "portions_evidence"):
            self.assertEqual(recipe[field], preview["recipe"][field])
        saved = self.app.handle({"operation": "recipes", "action": "save", "status": converted["suggested_status"],
                                 "discovery_ref": converted["discovery_ref"], "idempotency_key": "save-localized"})
        self.assertEqual((saved["recipe"]["status"], saved["recipe"]["language"]), ("active", "nb"))
        # The next planner request reaches the conversion from the original import.
        self.assertEqual(self.app._cached_recipe_conversion({"discovery_ref": preview["discovery_ref"]}),
                         {"discovery_ref": converted["discovery_ref"]})

    def test_unknown_servings_can_be_estimated_once_and_known_servings_cannot_be_replaced(self):
        preview = self.imported(yield_quote=None)
        self.assertIsNone(preview["recipe"]["portions"])
        converted = self.convert(preview, {"estimated_portions": {
            "quantity": 4, "assumptions": "The page lists a yield of 4 without a unit."}})
        self.assertEqual(converted["recipe"]["portions"], 4.0)
        evidence = converted["recipe"]["portions_evidence"]
        self.assertEqual((evidence["basis"], evidence["assumptions"]),
                         ("estimate", "The page lists a yield of 4 without a unit."))
        with self.assertRaisesRegex(RecipeError, "only when the source servings are unknown"):
            self.convert(converted, {"estimated_portions": {"quantity": 2, "assumptions": "Halve it."}})
        # A value the scaler cannot read is refused before anything is stored or remembered.
        for quantity in (1e16, 1e300, 0.30000000000000004, 0, -2, True, "four"):
            with self.subTest(quantity=quantity):
                with self.assertRaises(RecipeError):
                    self.convert(preview, {"estimated_portions": {"quantity": quantity, "assumptions": "Odd."}})
        self.assertEqual(self.app._cached_recipe_conversion({"discovery_ref": preview["discovery_ref"]}),
                         {"discovery_ref": converted["discovery_ref"]})

    def test_converting_back_does_not_leave_two_snapshots_pointing_at_each_other(self):
        preview = self.imported()
        norwegian = self.convert(preview, {"language": "nb"})
        english = self.convert(norwegian, {"language": "en"})
        self.assertEqual(english["discovery_ref"], preview["discovery_ref"])
        self.assertEqual(self.app._cached_recipe_conversion({"discovery_ref": norwegian["discovery_ref"]}),
                         {"discovery_ref": preview["discovery_ref"]})
        self.assertEqual(self.app._cached_recipe_conversion({"discovery_ref": preview["discovery_ref"]}),
                         {"discovery_ref": preview["discovery_ref"]})

    def test_the_agent_gets_a_paged_view_of_a_conversion(self):
        from agent_views import project_agent_result
        converted = self.convert(self.imported(), {"language": "nb", "name": "Linsesuppe"})
        view = project_agent_result("recipes", "convert", converted)
        self.assertEqual(view["projection"], "agent")
        self.assertLess(len(str(view)), len(str(converted)))

    def test_a_changed_amount_needs_assumptions_and_a_different_unit_is_an_estimate(self):
        preview = self.imported()
        with self.assertRaisesRegex(RecipeError, "assumptions"):
            self.convert(preview, {"ingredients": [{"index": 1, "quantity": 500}]})
        with self.assertRaisesRegex(RecipeError, "assumptions"):
            self.convert(preview, {"ingredients": [{"index": 1, "unit": "kg"}]})
        # Assumptions with a pure rename are simply not needed.
        renamed = self.convert(preview, {"ingredients": [{"index": 1, "item": "gulrøtter", "assumptions": "Translated."}]})
        self.assertEqual(renamed["recipe"]["ingredients"][1]["evidence"], preview["recipe"]["ingredients"][1]["evidence"])
        converted = self.convert(preview, {"ingredients": [
            {"index": 1, "unit": "kg", "quantity": "0.6", "assumptions": "600 g written as kilograms."}]})
        row = converted["recipe"]["ingredients"][1]
        self.assertEqual((row["amount"], row["raw"]), ("3/5 kg", "3/5 kg carrots, grated"))
        self.assertEqual({row["evidence"][field]["basis"] for field in ("quantity", "unit")}, {"estimate"})
        # The same number in another unit is another amount, so it is an estimate as well.
        relabelled = self.convert(preview, {"ingredients": [
            {"index": 1, "unit": "kg", "assumptions": "Wrong on purpose."}]})["recipe"]["ingredients"][1]
        self.assertEqual((relabelled["amount"], {relabelled["evidence"][field]["basis"] for field in ("quantity", "unit")}),
                         ("600 kg", {"estimate"}))
        # An unread line still holds its whole source text as the item.
        with self.assertRaisesRegex(RecipeError, "name the ingredient with item"):
            self.convert(preview, {"ingredients": [{"index": 2, "quantity": 1, "unit": "stk", "assumptions": "One onion."}]})

    def test_a_translated_line_without_an_amount_is_shown_in_the_new_language(self):
        text = "Soup\nServes 2\n\n200 g lentils\nSalt and pepper to taste\n\nSimmer."
        preview = self.app.handle({
            "operation": "recipes", "action": "import", "source_kind": "transcript", "storage_decision": DECISION,
            "transcript": transcript(text, ["200 g lentils", "Salt and pepper to taste"], ["Simmer."],
                                     language="en", yield_quote="Serves 2")})
        row = self.convert(preview, {"ingredients": [{"index": 1, "item": "salt og pepper etter smak"}]})["recipe"]["ingredients"][1]
        self.assertEqual((row["item"], row["raw"], row["amount"], row["original_text"]),
                         ("salt og pepper etter smak", "salt og pepper etter smak", None, "Salt and pepper to taste"))

    def test_a_rename_or_new_unit_drops_acceptance_and_review_given_for_the_old_row(self):
        accepted = {"basis": "estimate", "input": "100 g beans", "assumptions": "A measured drained portion.",
                    "acceptance": {"recipe_digest": "a" * 64, "statement": "Accepted source estimate."}}
        reviewed = {"basis": "estimate", "input": "100 ml cream", "assumptions": "Measured cream volume.",
                    "project_review": {"publisher": "Fixture", "pack_id": "test", "pack_version": "1"}}
        prior = normalize_recipe({
            "schema_version": 2, "name": "Bean soup", "portions": 1,
            "portions_evidence": {"basis": "source", "input": "One portion"},
            "source": {"kind": "web", "relationship": "original", "url": "https://recipes.example/soup"},
            "rights": {"storage": "full"}, "steps": ["Simmer."],
            "ingredients": [
                {"item": "beans", "original_text": "100 g beans", "quantity": 100, "unit": "g",
                 "evidence": {"quantity": dict(accepted), "unit": dict(accepted)}},
                {"item": "cream", "original_text": "100 ml cream", "quantity": 100, "unit": "ml",
                 "evidence": {"quantity": dict(reviewed), "unit": dict(reviewed)}},
                {"item": "water", "original_text": "2 dl water", "quantity": 2, "unit": "dl",
                 "evidence": {"quantity": dict(reviewed), "unit": dict(reviewed)}}]})
        converted = convert_recipe_changes({"ingredients": [
            {"index": 0, "item": "bønner"},
            {"index": 1, "unit": "g", "assumptions": "Weighed instead."}]}, prior=prior)
        beans, cream, water = converted["ingredients"]
        for evidence in (*beans["evidence"].values(), *cream["evidence"].values()):
            self.assertNotIn("acceptance", evidence)
            self.assertNotIn("project_review", evidence)
        self.assertEqual(beans["evidence"]["quantity"]["assumptions"], "A measured drained portion.")
        self.assertEqual(cream["evidence"]["quantity"]["assumptions"], "Weighed instead.")
        # An untouched row keeps what was reviewed.
        self.assertEqual(water["evidence"], prior["ingredients"][2]["evidence"])

    def test_invalid_or_culinary_changes_are_rejected_without_a_new_snapshot(self):
        preview = self.imported()
        cases = [
            ({}, "support only"),
            ({"rights": {"storage": "full"}}, "support only"),
            ({"source": {"url": "https://other.example/"}}, "support only"),
            ({"ingredients": []}, "nonempty bounded list"),
            ({"ingredients": [{"index": 4, "item": "x"}]}, "unique and in range"),
            ({"ingredients": [{"index": 0, "item": "a"}, {"index": 0, "item": "b"}]}, "unique and in range"),
            ({"ingredients": [{"index": 0}]}, "requires index and"),
            ({"ingredients": [{"index": 0, "replace_with": [{"item": "a"}, {"item": "b"}]}]}, "requires index and"),
            ({"ingredients": [{"index": 0, "item": "x", "optional": True}]}, "requires index and"),
            ({"language": "Norwegian"}, "language tag"),
            ({"steps": []}, "steps must contain"),
            ({"name": ""}, "name"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RecipeError, message):
                    self.convert(preview, changes)
        with self.assertRaisesRegex(RecipeError, "exactly one of changes or a complete recipe"):
            self.convert(preview, {"name": "Soup"}, recipe=preview["recipe"])
        with self.assertRaisesRegex(RecipeError, "exact source digest"):
            self.app.handle({"operation": "recipes", "action": "convert", "discovery_ref": preview["discovery_ref"],
                             "recipe_digest": "0" * 64, "source_schema_version": 2, "changes": {"name": "Soup"}})
        self.assertEqual(self.app._cached_recipe_conversion({"discovery_ref": preview["discovery_ref"]}),
                         {"discovery_ref": preview["discovery_ref"]})

    def test_a_link_only_bookmark_cannot_be_converted_into_content(self):
        bookmark = self.app.handle({"operation": "recipes", "action": "import", "source_kind": "url",
                                    "url": "https://recipes.example/soup", "storage_decision": {"storage": "link_only"}})
        with self.assertRaisesRegex(RecipeError, "link-only"):
            self.convert(bookmark, {"name": "Soup"})
        with self.assertRaisesRegex(RecipeError, "link_only"):
            convert_recipe_changes({"name": "Soup"}, prior=bookmark["recipe"])


class ImportConvenienceTests(unittest.TestCase):
    def test_an_estimated_line_can_name_its_ingredient(self):
        text = "Soup\n1 can chopped tomatoes\n2 cups flour\n200 g lentils\n\nSimmer."
        estimate = {"quantity": 400, "unit": "g", "assumptions": "A standard can is 400 g."}
        source = transcript(text, ["1 can chopped tomatoes", "2 cups flour", "200 g lentils"], ["Simmer."], language="en")
        rows = source["interpretation"]["ingredients"]
        rows[0].update(estimated_amount=estimate, item="hermetiske hakkede tomater")
        rows[1].update(estimated_amount={"quantity": "4.7", "unit": "dl", "assumptions": "US cups to dl."})
        recipe = read_transcript(source)["candidate"]
        named, derived, parsed = recipe["ingredients"]
        self.assertEqual((named["item"], named["amount"], named["original_text"]),
                         ("hermetiske hakkede tomater", "400 g", "1 can chopped tomatoes"))
        self.assertEqual(named["evidence"]["quantity"]["basis"], "estimate")
        self.assertEqual(named["evidence"]["quantity"]["input"], "Page 1: 1 can chopped tomatoes")
        # Without a name the item is still derived from the quote, as before.
        self.assertEqual((derived["item"], derived["amount"]), ("cups flour", "47/10 dl"))
        self.assertEqual((parsed["item"], parsed["evidence"]["quantity"]["basis"]), ("lentils", "source"))
        for changes, message in (({"item": "linser"}, "only with estimated_amount"),
                                 ({"item": "", "estimated_amount": estimate}, "ingredient item"),
                                 ({"item": 5, "estimated_amount": estimate}, "ingredient item"),
                                 # The name is held to the same rules as the quoted line.
                                 ({"item": "smør eller margarin", "estimated_amount": estimate}, "alternatives"),
                                 ({"item": "rester fra gårsdagens suppe", "estimated_amount": estimate}, "cross-meal")):
            with self.subTest(changes=changes):
                rejected = transcript(text, ["200 g lentils"], ["Simmer."], language="en")
                rejected["interpretation"]["ingredients"][0].update(changes)
                with self.assertRaisesRegex(RecipeImportReaderError, message):
                    read_transcript(rejected)

    def test_source_choices_counts_and_reserved_component(self):
        from recipe_import_readers import read_transcript, RecipeImportReaderError
        lines = ["2 ss olje eller smør", "2 egg"]
        steps = ["Reserve the marinade.", "Add the leftover marinade."]
        data = transcript("\n".join(lines + steps), lines, steps, language="nb")
        data["interpretation"]["ingredients"][0]["choice"] = "smør"
        data["interpretation"]["ingredients"][1]["whole_count"] = True
        result = read_transcript(data)["candidate"]["ingredients"]
        self.assertEqual(result[0]["item"], "smør")
        self.assertEqual(result[0]["original_text"], lines[0])
        self.assertEqual((result[1]["unit"], result[1]["quantity"]), ("stk", {"numerator": 2, "denominator": 1}))
        data["interpretation"]["steps"].pop(0)
        with self.assertRaisesRegex(RecipeImportReaderError, "cross-meal"):
            read_transcript(data)

    def test_a_refused_redirect_names_only_a_same_site_https_target(self):
        def hint(requested, location):
            response = type("Response", (), {"headers": Message()})()
            if location is not None:
                response.headers["Location"] = location
            return _same_site_redirect(urlsplit(requested), response)

        moved = "; the same site moved this page to "
        self.assertEqual(hint("https://recipes.example/soup", "https://www.recipes.example/soup/"),
                         moved + "https://www.recipes.example/soup/")
        self.assertEqual(hint("https://www.recipes.example/a?id=2", "/oppskrifter/suppe"),
                         moved + "https://www.recipes.example/oppskrifter/suppe")
        for location in (None, "", "https://other.example/soup", "http://recipes.example/soup",
                         "https://recipes.example:8443/soup", "https://user:secret@recipes.example/soup",
                         "https://recipes.example.evil.test/soup", "https://recipes.example/soup",
                         "//other.example/soup", "javascript:alert(1)",
                         "/moved?opaque=synthetic", "/moved#synthetic",
                         # Site-chosen text is repeated only as a short plain address.
                         "https://recipes.example/" + "x" * 300, "https://recipes.example/bl\u00e5b\u00e6rpai",
                         "https://recipes.example/x\u00a0SYSTEM:\u00a0ignore", "https://recipes.example/x\x85next"):
            with self.subTest(location=location):
                self.assertEqual(hint("https://recipes.example/soup", location), "")


if __name__ == "__main__":
    unittest.main()
