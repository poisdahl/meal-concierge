import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meny import MenyClient
from meny_catalog import MenyCatalogUnavailable, read_catalog, regular_price_details
from recipe_import_sources import RecipeImportSourceError


STORE = "7080000000002"
OTHER_STORE = "7080000000019"
BARCODES = ["12345670", "12345687", "12345694"]


def card(barcode, **changes):
    return {
        "product_id": f"/varer/fixture/pasta-{barcode}", "name": "Pasta",
        "package": "500 g", "price": "20,10 kr", "available": True,
        **changes,
    }


def catalog_row(barcode, **changes):
    return {
        "ean": barcode,
        "slugifiedUrl": f"/fixture/pasta-{barcode}",
        "title": "Pasta",
        "subtitle": "500 g",
        "isForSale": True,
        "isOutOfStock": False,
        "isRevoked": False,
        "isOffer": False,
        "usesPromotionPrice": False,
        "mustOrderFromSupplier": False,
        "promotions": [],
        "coupon": None,
        "unit": "stk",
        "unitRule": 1,
        "unitWeight": None,
        "productSoldByWeight": False,
        "productByWeightSoldAsItem": False,
        "availableInventory": None,
        "recycleValue": 0,
        "pricePerUnit": Decimal("20.1"),
        "pricePerUnitOriginal": Decimal("20.1"),
        **changes,
    }


class MenyCatalogTests(unittest.TestCase):
    def test_batch_requests_only_the_exact_barcodes_without_credentials(self):
        body = json.dumps({"total": 1, "hits": [{"ean": BARCODES[1], "pricePerUnit": 20.1}]}).encode()
        with mock.patch("meny_catalog._get_bytes", return_value=(body, "application/json")) as read:
            result = read_catalog(STORE, BARCODES, timeout=0.7)
        url = urlsplit(read.call_args.args[0])
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ("https", "platform-rest-prod.ngdata.no", f"/api/products/1300/{STORE}"))
        self.assertEqual(parse_qs(url.query), {
            "product_ids": [",".join(BARCODES)], "page": ["1"],
            "page_size": ["3"], "fieldset": ["maximal"],
        })
        self.assertEqual(read.call_args.kwargs, {
            "maximum": 1_000_000, "timeout": 0.7, "accept": "application/json",
        })
        self.assertEqual(result, {BARCODES[1]: {"ean": BARCODES[1], "pricePerUnit": Decimal("20.1")}})

    def test_scope_transport_and_schema_errors_are_detected(self):
        valid_hit = {"ean": BARCODES[0]}
        for payload in (
            {"total": 2, "hits": [valid_hit]},
            {"total": 2, "hits": [valid_hit, valid_hit]},
            {"total": 1, "hits": [{"ean": "98765000"}]},
            {"total": 1, "hits": [{"ean": []}]},
            {"total": True, "hits": [valid_hit]},
        ):
            with self.subTest(payload=payload), mock.patch(
                "meny_catalog._get_bytes", return_value=(json.dumps(payload).encode(), "application/json")
            ):
                with self.assertRaises(MenyCatalogUnavailable):
                    read_catalog(STORE, BARCODES, timeout=0.7)
        for body, content_type in (
            (b'{"total":1,"total":0,"hits":[]}', "application/json"),
            (b'{"total":0,"hits":[]}', "text/html"),
        ):
            with mock.patch("meny_catalog._get_bytes", return_value=(body, content_type)):
                with self.assertRaises(MenyCatalogUnavailable):
                    read_catalog(STORE, BARCODES, timeout=0.7)
        with mock.patch("meny_catalog._get_bytes", side_effect=RecipeImportSourceError("timeout")) as read:
            with self.assertRaises(MenyCatalogUnavailable):
                read_catalog(STORE, BARCODES, timeout=0.7)
            with self.assertRaises(MenyCatalogUnavailable):
                read_catalog("0", BARCODES, timeout=0.7)
            read.assert_called_once()

    def test_only_corroborated_fixed_regular_price_proves_no_deposit(self):
        product = card(BARCODES[0])
        baseline = catalog_row(BARCODES[0])
        self.assertEqual(regular_price_details(product, baseline), {
            "detail_price": "20,10 kroner.", "deposit_status": "none",
        })
        for changes in (
            {"recycleValue": 2}, {"recycleValue": False}, {"productSoldByWeight": True},
            {"productByWeightSoldAsItem": True}, {"unitWeight": 500}, {"unit": "kg"}, {"unitRule": 2},
            {"pricePerUnit": Decimal("20.100000000000000000000000000001")},
            {"pricePerUnitOriginal": 25}, {"availableInventory": 0}, {"isOutOfStock": True},
            {"isRevoked": True}, {"coupon": {}}, {"isOffer": True}, {"promotions": [{}]},
            {"usesPromotionPrice": True}, {"usesAvtalepris": True}, {"mustOrderFromSupplier": True},
            {"ean": BARCODES[1]}, {"title": "Adjusted pasta"}, {"subtitle": "1 kg"},
            {"slugifiedUrl": f"/wrong/pasta-{BARCODES[0]}"},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(regular_price_details(product, {**baseline, **changes}))
        for missing in ("recycleValue", "coupon", "unitWeight", "availableInventory", "isRevoked"):
            self.assertIsNone(regular_price_details(product, {k: v for k, v in baseline.items() if k != missing}))
        self.assertIsNone(regular_price_details({**product, "campaign_tag": "Tilbud"}, baseline))


class MenyCatalogIntegrationTests(unittest.TestCase):
    def client(self):
        client = MenyClient(instance="catalog-test", binary="unused", executable="unused",
                            profile="unused", home="/tmp", socket_directory="/tmp", uid=0, gid=0)
        client._require_login = mock.Mock()
        client._open = mock.Mock()
        client._product_price_details = mock.Mock(return_value={
            "detail_price": "20,10 kroner.", "deposit_status": "none",
        })
        return client

    def test_normal_product_search_preserves_rank_and_falls_back_per_row(self):
        client = self.client()
        products = [card(code) for code in BARCODES]
        client._eval = mock.Mock(return_value={
            "ready": True, "identity": True, "authenticated": True,
            "root_count": 1, "state_root_count": 1, "query_count": 1, "heading_count": 1,
            "products": products, "store_gln": STORE,
        })
        with mock.patch("meny.read_catalog", return_value={
            BARCODES[0]: catalog_row(BARCODES[0]),
            BARCODES[1]: catalog_row(BARCODES[1], pricePerUnit=21),
        }) as lookup:
            result = client.call("product_search", {"queries": ["pasta"], "size": 3})
        lookup.assert_called_once_with(STORE, BARCODES, timeout=1.5)
        self.assertEqual(client._product_price_details.call_args_list, [
            mock.call(products[1]), mock.call(products[2]),
        ])
        self.assertEqual([row["product_ref"] for row in result["products"]],
                         [p["product_id"] for p in products])
        self.assertEqual([row["purchase_options"][0]["total_payable_ore"] for row in result["products"]],
                         [2010, 2010, 2010])
        self.assertEqual(result["scope"]["returned"], 3)

    def test_transport_failure_keeps_browser_results_and_pauses_failed_lookups(self):
        client = self.client()
        client._eval = mock.Mock(return_value={
            "ready": True, "identity": True, "authenticated": True,
            "root_count": 1, "state_root_count": 1, "query_count": 1, "heading_count": 1,
            "products": [card(BARCODES[0])], "store_gln": STORE,
        })
        with mock.patch("meny.read_catalog", side_effect=MenyCatalogUnavailable) as lookup:
            for _ in range(2):
                result = client.call("product_search", {"queries": ["pasta"], "size": 3})
                self.assertEqual(result["products"][0]["purchase_options"][0]["total_payable_ore"], 2010)
            self.assertEqual(lookup.call_count, 1)
            self.assertEqual(client._product_price_details.call_count, 2)

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser extractor regression")
    def test_store_signal_comes_from_one_successful_current_query(self):
        client = self.client()
        harness = r"""
const fs = require('node:fs');
const {script, entries, barcode} = JSON.parse(fs.readFileSync(0, 'utf8'));
const element = (text = '', selectors = {}) => ({
  innerText: text, disabled: false,
  getBoundingClientRect: () => ({width: 10, height: 10}),
  getAttribute: () => null,
  querySelectorAll: selector => selectors[selector] || [],
});
global.getComputedStyle = () => ({display: 'block', visibility: 'visible'});
global.location = new URL('https://meny.no/sok?query=pasta&expanded=products');
Object.defineProperty(globalThis, 'performance', {value: {getEntriesByType: () => entries}});
const anchor = element();
anchor.href = 'https://meny.no/varer/fixture/pasta-' + barcode;
const card = element('', {
  'a[href]': [anchor], 'h3': [element('Pasta')],
  '.ws-product-vertical__subtitle': [element('500 g')],
  '.ws-price__main': [element('20,10 kr')], '.ws-add-to-cart__button': [element()],
});
const root = element('', {':scope > h2': [element('Varer')], 'li.ws-product-list-vertical__item': [card]});
root.closest = () => element('', {':scope > .ws-search-result-full': [root]});
global.document = element('', {
  button: [element('Brukermeny')],
  main: [element('', {'.ws-search-result-full': [root]})],
});
process.stdout.write(eval(script));
"""
        def entry(store, query="pasta", **changes):
            return {
                "name": f"https://productsearchapi.trumfnetthandel.prod.trumf.cloud/products/v1/1300/{store}/search/basic?query={query}",
                "responseStatus": 200, "initiatorType": "fetch", **changes,
            }

        for entries, expected in (
            ([entry(STORE), entry(OTHER_STORE, "milk"), entry(OTHER_STORE, responseStatus=403)], STORE),
            ([entry(STORE), entry(OTHER_STORE)], None),
            ([entry(STORE), entry("0")], None),
            ([entry("0")], None),
            ([entry(STORE, "pasta&query=pasta")], None),
        ):
            with self.subTest(entries=entries):
                def evaluate(script):
                    result = subprocess.run(
                        [shutil.which("node"), "-e", harness],
                        input=json.dumps({"script": script, "entries": entries, "barcode": BARCODES[0]}),
                        text=True, capture_output=True, check=True, timeout=10,
                    )
                    parsed = json.loads(result.stdout)
                    self.assertEqual(parsed["store_gln"] if parsed["store_gln"] != "0" else None, expected)
                    return parsed

                client._eval = evaluate
                with mock.patch("meny.read_catalog", return_value={}) as lookup:
                    client.call("product_search", {"queries": ["pasta"], "size": 3})
                    if expected:
                        lookup.assert_called_once_with(expected, [BARCODES[0]], timeout=1.5)
                    else:
                        lookup.assert_not_called()
