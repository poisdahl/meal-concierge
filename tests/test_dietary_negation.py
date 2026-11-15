"""A negation before an allergen must be a word of its own."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dietary_assessment import assess, matches, title_positive_match


def conditions(name, evidence, *terms, kind="allergy"):
    profile = {"diet": {"rules": [{"kind": kind, "term": term} for term in terms]}}
    return {finding["term"]: finding["condition"]
            for finding in assess(profile, {"name": name, "product_ref": 1, "dietary_evidence": evidence})}


class NegationBoundaryTests(unittest.TestCase):
    def test_a_word_that_only_ends_like_a_negation_does_not_hide_an_allergen(self):
        # "Gluten" ends in "uten" (without) and "Cappuccino" in "no".
        self.assertEqual(conditions("Kake", {"allergens": "Gluten Egg Melk"}, "egg", "melk"),
                         {"egg": "conflict", "melk": "conflict"})
        self.assertEqual(conditions("Cappuccino melk 1 l", {}, "melk"), {"melk": "conflict"})
        self.assertEqual(conditions("Pasta", {"ingredients": "Durumhvete, gluten egg"}, "egg", kind="never_buy"),
                         {"egg": "conflict"})
        for term, value in (("egg", "gluten egg"), ("milk", "cappuccino milk"), ("melk", "sauten melk"),
                            ("egg", "piano egg"), ("melk", "lutan melk")):
            with self.subTest(value=value):
                self.assertTrue(matches(term, value))
        self.assertTrue(title_positive_match("egg", "Gluten eggnudler"))
        self.assertTrue(title_positive_match("melk", "Cappuccino melkesjokolade"))

    def test_a_plant_word_must_be_its_own_word_and_an_allergen_list_names_allergens(self):
        # "Lakris" ends in "ris" (rice) and "licorice" in "rice": neither makes it a plant milk.
        self.assertEqual(conditions("Lakris melk 1 l", {}, "melk"), {"melk": "conflict"})
        self.assertEqual(conditions("Licorice milk", {}, "milk"), {"milk": "conflict"})
        self.assertTrue(title_positive_match("melk", "Lakris melkesjokolade"))
        # Allergen rows are joined by spaces; "Soya Melk" in that field is soy and milk.
        self.assertEqual(conditions("Kake", {"allergens": "Gluten Soya Melk"}, "melk"), {"melk": "conflict"})
        self.assertEqual(conditions("Kake", {"may_contain": ["Soya", "Melk"]}, "melk"), {"melk": "conflict"})
        # A plant drink in the ingredient list or the product name is still not dairy evidence.
        self.assertEqual(conditions("Havredrikk", {"ingredients": "vann, havre, soya melk"}, "melk"), {"melk": "unknown"})
        self.assertEqual(conditions("Soya melk 1 l", {}, "melk"), {"melk": "unknown"})
        self.assertEqual(conditions("Kokosmelk", {"ingredients": "kokos melk, vann"}, "melk"), {"melk": "unknown"})

    def test_real_negations_still_deny_the_allergen(self):
        for term, value in (("egg", "uten egg"), ("egg", "Pasta uten egg"), ("milk", "no milk"), ("melk", "utan melk"),
                            ("egg", "does not contain egg"), ("egg", "contains no traces of egg"),
                            ("melk", "inneholder ikke melk"), ("egg", "fri for egg"), ("melk", "uten tilsatt melk"),
                            ("egg", "egg-free"), ("melk", "melk fri")):
            with self.subTest(value=value):
                self.assertFalse(matches(term, value))
        self.assertFalse(title_positive_match("egg", "Pasta uten eggnudler"))
        self.assertFalse(title_positive_match("milk", "Bar without milk chocolate"))
        self.assertEqual(conditions("Kjeks", {"allergens": "Gluten. Uten egg"}, "egg"), {"egg": "unknown"})


if __name__ == "__main__":
    unittest.main()
