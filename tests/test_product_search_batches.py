"""Native search batches preserve exact query/size identity."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError
from product_observations import normalize_retail_product_search_batch
from retail_mcp import RetailMcpClient


def batch(query, ref):
    return {"query": query, "hasMore": True, "products": [{"id": ref, "name": query,
        "description": "500 g", "price": "10.00", "unitPrice": "20.00", "unitName": "kilogram",
        "availability": {"isAvailable": True}}]}


class ProductBatchTests(unittest.TestCase):
    def test_query_mapping_does_not_depend_on_response_order(self):
        value = normalize_retail_product_search_batch({"result": [batch("beans", 2), batch("spinach", 1)]},
                                                    ["spinach", "beans"], size=5)
        self.assertEqual(value["spinach"]["products"][0]["product_ref"], 1)
        self.assertEqual(value["beans"]["products"][0]["product_ref"], 2)
        self.assertTrue(all(row["scope"]["requested_size"] == 5 for row in value.values()))

    def test_malformed_query_and_candidate_scope_fail_closed(self):
        valid = {"result": [batch("spinach", 1), batch("beans", 2)]}
        variants = []
        for mutator in (
            lambda v: v["result"].pop(),
            lambda v: v["result"].append(batch("extra", 3)),
            lambda v: v["result"][1].update(query="spinach"),
            lambda v: v["result"][1].update(query="substitute"),
            lambda v: v["result"][1].pop("query"),
            lambda v: v["result"][1].update(products=[]),
        ):
            value = deepcopy(valid)
            mutator(value)
            variants.append(value)
        # Empty exact result is valid; it does not assert availability.
        empty = variants.pop()
        self.assertEqual(normalize_retail_product_search_batch(empty, ["spinach", "beans"], size=5)["beans"]["products"], [])
        too_many = deepcopy(valid)
        too_many["result"][0]["products"] *= 6
        variants.append(too_many)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(HouseholdError):
                normalize_retail_product_search_batch(value, ["spinach", "beans"], size=5)
        for queries in (["same", "same"], list(map(str, range(9))), [], [True]):
            with self.subTest(queries=queries), self.assertRaises(HouseholdError):
                normalize_retail_product_search_batch(valid, queries, size=5)

    def test_oda_uses_one_native_call_and_mathem_keeps_single_queries(self):
        for provider, count in (("oda", 1), ("mathem", 2)):
            client = RetailMcpClient("/unused", provider=provider)
            with mock.patch.object(client, "call", return_value={}) as call:
                client.product_search_batch(["spinach", "beans"], size=5, deadline=1234)
            self.assertEqual(call.call_count, count)
            for args, kwargs in call.call_args_list:
                self.assertEqual(args[0], "product_search")
                self.assertEqual(args[1]["page"], 1)
                self.assertEqual(args[1]["size"], 5)
                self.assertEqual(kwargs["deadline"], 1234)
                self.assertEqual(len(args[1]["queries"]), 2 if provider == "oda" else 1)
