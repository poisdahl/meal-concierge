"""Hostile or malformed text from pages and stores costs bounded work and nothing else."""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from product_observations import _package_descriptor, parse_package
import web_recipes
from web_recipes import DEFAULT_WEB_SEARCH, search_web, url_enabled


def meal(**fields):
    return {"idMeal": "52772", "strMeal": "Lentil soup", "strInstructions": "Simmer the lentils.",
            "strIngredient1": "Lentils", "strMeasure1": "200 g", **fields}


class HostileTextTests(unittest.TestCase):
    def test_a_product_description_with_many_slashes_is_parsed_in_bounded_time(self):
        # 24 slash-joined words and one token that cannot match took seconds, doubling per slash.
        hostile = "/".join("a" for _ in range(40)) + " 1"
        started = time.monotonic()
        self.assertFalse(_package_descriptor(hostile))
        self.assertIsNone(parse_package(hostile + ", 500 g", provider="oda"))
        self.assertLess(time.monotonic() - started, 1)
        accepted = {"Italia/Hellas": True, "Norge / Sverige": True, "u.s./eu": True, "Hel/halv/kvart": True,
                    "Bio-dynamisk/øko.": True, "L'Oréal/Paris": True, "Fersk/frossen laks": True,
                    "a//b": False, "/a": False, "a/": False, "Italia/2": False, "ca 500": False}
        for text, expected in accepted.items():
            with self.subTest(text=text):
                self.assertEqual(_package_descriptor(text), expected)
        self.assertEqual(parse_package("Italia/Hellas, 500 g", provider="oda")["unit"], "g")

    def test_a_disabled_site_stays_excluded_under_other_spellings_of_its_name(self):
        settings = deepcopy(DEFAULT_WEB_SEARCH)
        settings["broad"] = True
        settings["sites"][0]["enabled"] = False  # matprat.no
        for url in ("https://www.matprat.no/oppskrifter/x", "https://www.ｍatprat.no/oppskrifter/x",
                    "https://www.ma­tprat.no/oppskrifter/x", "https://WWW.MATPRAT.NO./oppskrifter/x"):
            with self.subTest(url=url):
                self.assertFalse(url_enabled(url, settings))
        self.assertTrue(url_enabled("https://www.tine.no/oppskrifter/x", settings))
        self.assertTrue(url_enabled("https://other.example/recipe", settings))
        self.assertTrue(url_enabled("https://xn--bl-zia.example/recipe", settings))
        # A name that cannot be a hostname is not allowed through.
        self.assertFalse(url_enabled("https://" + "a" * 70 + ".example/recipe", settings))
        self.assertFalse(url_enabled("https://evilmatprat.no:8443/x", settings))
        # Encoding a huge non-ASCII name is quadratic; such an address is refused first.
        started = time.monotonic()
        self.assertFalse(url_enabled("https://" + "".join(chr(0x4E00 + index) for index in range(16_000)) + ".example/x", settings))
        self.assertFalse(url_enabled("https://" + "ø" * 300 + ".example/x", settings))
        self.assertFalse(url_enabled(None, settings))
        self.assertLess(time.monotonic() - started, 1)

    def test_one_site_with_a_deeply_nested_reply_does_not_lose_the_other_sites_hits(self):
        def source_search(domain, query):
            if domain == "godfisk.no":
                raise RecursionError("maximum recursion depth exceeded while decoding a JSON document")
            if domain == "matprat.no":
                return [{"title": "Linsesuppe", "url": "https://www.matprat.no/oppskrifter/familien/linsesuppe/"}]
            return []

        with patch.object(web_recipes, "_source_search", source_search):
            result = search_web(deepcopy(DEFAULT_WEB_SEARCH), "linsesuppe", backend="direct")
        self.assertEqual(result["status"], "completed")
        self.assertEqual([hit["title"] for hit in result["results"]], ["Linsesuppe"])
        statuses = {source["domain"]: source["status"] for source in result["sources"]}
        self.assertEqual((statuses["godfisk.no"], statuses["matprat.no"]), ("unavailable", "completed"))
        self.assertEqual(result["coverage"], "partial")



if __name__ == "__main__":
    unittest.main()
