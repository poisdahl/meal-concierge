"""Anonymous MENY reads exercise the same paths and payload scopes as the Dot."""
from decimal import Decimal
from email.message import Message
import io
import json
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError
import meny_public
from test_meal_concierge_retailer_recipes import RECIPE_PATH, RECIPE_URL, recipe_html, search_hit, search_response


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, url, *, content_type="application/json", length=None):
        super().__init__(body)
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if length is not None:
            self.headers["Content-Length"] = str(length)


class MenyPublicTests(unittest.TestCase):
    request = {"operation": "catalog", "provider": "meny", "response_view": "agent"}
    url = meny_public.PRODUCT_SEARCH + "?search=oats"

    def test_uses_anonymous_get_and_a_bounded_response(self):
        body = b'{"hits":[]}'
        response = Response(body, self.url, length=len(body))
        with patch.object(meny_public, "build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(meny_public.fetch(self.url, maximum=20, timeout=2, accept="application/json"),
                             (body, "application/json"))
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, self.url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(dict(request.header_items()), {"Accept": "application/json", "Accept-encoding": "identity"})
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, None, None, self.url))

    def test_invalid_route_is_rejected_before_network(self):
        routes = ("http://meny.no/", "https://meny.no.evil.invalid/oppskrifter/soup",
                  "https://meny.no/checkout", "https://meny.no/oppskrifter/soup?new=1",
                  self.url.replace("/1300/0?", "/1300/1234567890123?"), self.url + "#anchor")
        with patch.object(meny_public, "build_opener") as build:
            for url in routes:
                accept = "text/html" if urlsplit(url).netloc == "meny.no" else "application/json"
                with self.subTest(url=url), self.assertRaises((ValueError, HouseholdError)):
                    meny_public.fetch(url, maximum=20, timeout=2, accept=accept)
        build.assert_not_called()

    def test_invalid_response_is_not_an_empty_result(self):
        duplicate = Response(b"{}", self.url, length=2)
        duplicate.headers["Content-Length"] = "2"
        cases = (Response(b"too long" * 3, self.url), Response(b"{}", self.url, length=16),
                 Response(b"{}", self.url, content_type="text/html"), Response(b"{}", self.url + "&changed=1"),
                 duplicate)
        for response in cases:
            with self.subTest(response=response), patch.object(meny_public, "build_opener") as build:
                build.return_value.open.return_value = response
                with self.assertRaises(ValueError):
                    meny_public.fetch(self.url, maximum=20, timeout=2, accept="application/json")

    def test_whole_response_deadline_does_not_wait_for_dns_headers_or_eof(self):
        release = Event()
        ending = Event()

        def slow_response(*_args, **_kwargs):
            release.wait(2)
            ending.set()
            return b"{}", "application/json"

        with patch.object(meny_public, "read_response", side_effect=slow_response):
            try:
                with self.assertRaisesRegex(ValueError, "deadline passed"):
                    meny_public.fetch(self.url, maximum=20, timeout=0.01, accept="application/json")
            finally:
                release.set()
                self.assertTrue(ending.wait(2))

    def test_public_products_expose_only_scoped_catalog_facts(self):
        row = {"ean": "1234567890123", "title": "Synthetic oats", "subtitle": "1 kg",
               "slugifiedUrl": "/catalog/oats-1234567890123", "pricePerUnit": 12.30,
               "isForSale": True, "availableInventory": 10, "storeId": "private-marker"}
        body = json.dumps({"total": 2, "searchType": "normal", "hits": [row]}).encode()
        request = {**self.request, "action": "products", "query": " ris & grønt ", "limit": 1}
        with patch.object(meny_public, "fetch", return_value=(body, "application/json")) as fetch:
            result = meny_public.read(request)
        url = urlsplit(fetch.call_args.args[0])
        self.assertEqual(parse_qs(url.query), {"search": ["ris & grønt"], "page": ["1"],
                                             "page_size": ["1"], "fieldset": ["maximal"]})
        self.assertEqual(result, {"provider": "meny", "scope": "public_catalog", "query": "ris & grønt",
                                  "total_matches": 2, "products": [{"name": "Synthetic oats",
                                      "product_id": "/varer/catalog/oats-1234567890123", "ean": "1234567890123",
                                      "url": "https://meny.no/varer/catalog/oats-1234567890123",
                                      "package_label": "1 kg", "public_price_per_unit_ore": 1230}]})
        for changes in ({"ean": "2222222222222"}, {"slugifiedUrl": "/%2e%2e/oats-1234567890123"}):
            with self.subTest(changes=changes), self.assertRaises(HouseholdError):
                meny_public.product({**row, **changes})
        self.assertEqual(meny_public.product({**row, "pricePerUnit": Decimal("0.00")})["public_price_per_unit_ore"], 0)
        for value in (Decimal("12.345"), Decimal("Infinity"), True, "12.30"):
            self.assertNotIn("public_price_per_unit_ore", meny_public.product({**row, "pricePerUnit": value}))
        with patch.object(meny_public, "fetch", return_value=(body, "application/json")):
            with self.assertRaisesRegex(HouseholdError, "scope changed"):
                meny_public.read({**request, "limit": 2})
        with patch.object(meny_public, "fetch", return_value=(b'{"total":0,"searchType":"normal","hits":[]}', "application/json")):
            self.assertEqual(meny_public.read(request)["products"], [])

    def test_recipe_search_and_detail_keep_the_existing_identity_checks(self):
        hit = search_hit("synthetic-rice", "Synthetic rice")
        fetch = Mock(side_effect=[search_response([hit]), recipe_html()])
        with patch.object(meny_public, "fetch", fetch):
            search = meny_public.read({**self.request, "action": "recipes", "query": "rice"})
            detail = meny_public.read({**self.request, "action": "recipe_get", "recipe_id": search["recipes"][0]["recipe_id"]})
        self.assertEqual(search["scope"], "public_catalog")
        self.assertEqual(search["recipes"][0]["recipe_id"], RECIPE_PATH)
        self.assertEqual(fetch.call_args.args[0], RECIPE_URL)
        self.assertEqual(detail["recipe"]["source"]["url"], RECIPE_URL)
        self.assertEqual(detail["recipe"]["portions"], 4)
