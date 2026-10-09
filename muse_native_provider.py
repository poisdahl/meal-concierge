"""Read-only Oda page observations from Muse's existing native browser profile."""
from collections.abc import Mapping
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

from clients.muse import MAX_PRODUCTS, validate_response as catalog_response
from core import HouseholdError
from muse_browser import NativeBridge, account_links, native_amount_minor, text
from muse_mcp import oda_operation_lock, private_operation_directory


READ_SECONDS = 90.0
TOOLS = {"product_search", "get_cart", "get_delivery_addresses"}


def fields(value, names):
    if not isinstance(value, Mapping) or set(value) != set(names):
        raise HouseholdError("Muse native provider observation fields changed")


def product_id(url):
    if not isinstance(url, str):
        raise HouseholdError("Muse native product URL is unavailable")
    match = re.fullmatch(r"https://oda\.com/no/products/([1-9][0-9]{0,15})-[^/?#]+/", url)
    if match is None or not 0 < int(match[1]) < 2**53:
        raise HouseholdError("Muse native product URL is invalid")
    return int(match[1])


def native_price(value):
    if value is None:
        return None
    literal = text(value)
    if len(value.encode("utf-8")) > 300:
        raise HouseholdError("Muse native product price text is invalid")
    match = re.fullmatch(r"(0|[1-9][0-9]{0,6}),([0-9]{2}) ?(?:kr|NOK)", literal)
    if match is None:
        # A range or other unfamiliar display does not prove an exact price.
        return None
    return match[1] + "." + match[2]


class MuseNativeReadProvider:
    provider, label = "oda", "Oda"

    def __init__(self, operation_directory, task_id):
        self.operation_directory = private_operation_directory(operation_directory)
        self.bridge = NativeBridge(self.operation_directory / "browser", task_id)
        self._terminal_failure = None
        self._account_ids = None

    @property
    def terminal_failure(self):
        return self._terminal_failure

    def probe(self):
        self.call("get_delivery_addresses", {})
        return {"server": {"name": "Muse native browser read observations"},
                "protocol_version": None, "tool_count": None}

    def call(self, tool, arguments, *, deadline=None):
        if tool not in TOOLS:
            raise HouseholdError("Muse browser read mode refuses this provider operation")
        if self.terminal_failure:
            raise HouseholdError(self.terminal_failure)
        if not isinstance(arguments, Mapping):
            raise HouseholdError("Muse native provider arguments must be an object")
        if tool == "product_search":
            fields(arguments, {"queries", "page", "size"})
            queries = arguments["queries"]
            if (not isinstance(queries, list) or len(queries) != 1
                    or not isinstance(queries[0], str) or not queries[0].strip()
                    or len(queries[0]) > 200 or type(arguments["page"]) is not int
                    or arguments["page"] != 1 or type(arguments["size"]) is not int
                    or not 1 <= arguments["size"] <= MAX_PRODUCTS):
                raise HouseholdError("Muse native catalog requires one bounded page-1 query")
        elif arguments:
            raise HouseholdError("Muse native account/cart read takes no arguments")
        if deadline is not None and (type(deadline) not in {int, float} or not math.isfinite(deadline)):
            raise HouseholdError("Muse native provider deadline must be finite")
        cutoff = time.monotonic() + READ_SECONDS
        if deadline is not None:
            cutoff = min(cutoff, deadline)
        # Both locks are nonblocking. The read-only mode never installs checkout,
        # whose account read would otherwise re-enter browser custody.
        with oda_operation_lock(self.operation_directory), self.bridge.custody():
            if self.terminal_failure:
                raise HouseholdError(self.terminal_failure)
            receipt = self.bridge.request("provider_read", {"tool": tool, "arguments": dict(arguments)},
                                          deadline=cutoff, include_response=True)
            response = receipt["response"]
            if response["task_state"] != "completed":
                raise HouseholdError("Muse native provider needs an actual completed observation")
            facts = response["facts"]
            if not isinstance(facts, Mapping) or type(facts.get("signed_in")) is not bool:
                raise HouseholdError("Muse native provider sign-in evidence is unavailable")
            if not facts["signed_in"]:
                self._terminal_failure = "Oda native browser login is required; preserve the existing profile and task records."
                raise HouseholdError(self.terminal_failure)
            references = account_links(facts.get("account"))
            if self._account_ids is not None and references != self._account_ids:
                self._terminal_failure = "Oda native browser account evidence changed; reconcile the original profile."
                raise HouseholdError(self.terminal_failure)
            if facts.get("complete") is not True:
                raise HouseholdError("Muse native provider needs the complete requested page section")
            source = {"kind": "host_attested", "url": facts.get("url"),
                      "request_id": response["request_id"], "observed_at": response["observed_at"]}
            if tool == "get_delivery_addresses":
                result = self._addresses(facts)
            elif tool == "get_cart":
                result = self._cart(facts)
            else:
                result = self._catalog(facts, arguments, response, receipt)
            self._account_ids = references
            return {**result, "source": source, "observation_scope": "browser_read_only"}

    @staticmethod
    def _addresses(facts):
        fields(facts, {"url", "signed_in", "complete", "account", "rows"})
        if facts["url"] != "https://oda.com/no/account/delivery/":
            raise HouseholdError("Muse native address page changed")
        rows = facts["rows"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
            raise HouseholdError("Muse native address rows are unavailable")
        result, ids = [], set()
        for row in rows:
            fields(row, {"edit_url", "address", "default", "selected"})
            refs = account_links({"url": facts["url"], "edit_urls": [row["edit_url"]]})
            reference = next(iter(refs))
            address = text(row["address"])
            if (reference in ids or not address or type(row["default"]) is not bool
                    or row["selected"] is not None and type(row["selected"]) is not bool):
                raise HouseholdError("Muse native address observation is invalid")
            ids.add(reference)
            result.append({"id": reference, "address": address,
                           "isDefault": row["default"], "isSelected": row["selected"]})
        if ids != account_links(facts["account"]):
            raise HouseholdError("Muse native address rows and account links disagree")
        if sum(row["isSelected"] is True for row in result) > 1:
            raise HouseholdError("Muse native selected address is ambiguous")
        return {"result": result}

    @staticmethod
    def _cart(facts):
        fields(facts, {"url", "signed_in", "complete", "account", "empty", "items",
                       "amount_rows", "delivery_text", "address", "warnings"})
        if facts["url"] != "https://oda.com/no/cart/":
            raise HouseholdError("Muse native cart page changed")
        rows = facts["items"]
        if (type(facts["empty"]) is not bool or not isinstance(rows, list) or len(rows) > 200
                or facts["empty"] != (len(rows) == 0)):
            raise HouseholdError("Muse native cart needs complete rows or an explicit empty state")
        items = []
        for row in rows:
            fields(row, {"url", "title", "subtitle", "quantity", "price"})
            reference, title = product_id(row["url"]), text(row["title"])
            if (not title or type(row["quantity"]) is not int or not 1 <= row["quantity"] <= 1_000_000
                    or row["price"] is not None and not isinstance(row["price"], str)):
                raise HouseholdError("Muse native cart line is invalid")
            items.append({"product_id": str(reference), "name": title, "description": text(row["subtitle"]),
                          "quantity": row["quantity"], "price": None if row["price"] is None else text(row["price"])})
        amounts = facts["amount_rows"]
        if not isinstance(amounts, list) or len(amounts) > 30:
            raise HouseholdError("Muse native cart amount rows are invalid")
        observed_amounts, totals = [], []
        for row in amounts:
            fields(row, {"label", "value"})
            label, value = text(row["label"]), text(row["value"])
            observed_amounts.append({"label": label, "value": value})
            if label.casefold() in {"total inkl. mva", "total inkl. mva.", "total"}:
                totals.append(native_amount_minor("Total inkl. MVA", value) / 100)
        if len(totals) > 1:
            raise HouseholdError("Muse native cart total is ambiguous")
        warnings = facts["warnings"]
        if not isinstance(warnings, list) or len(warnings) > 200:
            raise HouseholdError("Muse native cart warnings are invalid")
        return {"items": items, "count": sum(row["quantity"] for row in items),
                "total": totals[0] if totals else None, "amount_rows": observed_amounts,
                "delivery": {"display": None if facts["delivery_text"] is None else text(facts["delivery_text"]),
                             "slot_id": None, "address": None if facts["address"] is None else text(facts["address"])},
                "warnings": [text(value) for value in warnings]}

    @staticmethod
    def _catalog(facts, arguments, response, receipt):
        fields(facts, {"url", "signed_in", "complete", "account", "query", "page", "size", "hasMore", "products"})
        try:
            url = urlsplit(facts["url"])
            valid = (url.scheme == "https" and url.hostname == "oda.com" and url.port in {None, 443}
                     and url.username is None and url.password is None and not url.fragment
                     and url.path == "/no/search/"
                     and parse_qs(url.query).get("q") == arguments["queries"])
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise HouseholdError("Muse native catalog search page changed")
        products = facts["products"]
        if not isinstance(products, list):
            raise HouseholdError("Muse native catalog products are unavailable")
        normalized, displays = [], {}
        for row in products:
            fields(row, {"url", "name", "description", "price", "unitPrice", "unitName", "availability"})
            reference = product_id(row["url"])
            normalized.append({**{key: row[key] for key in row if key != "url"},
                               "id": reference, "price": native_price(row["price"])})
            displays.setdefault(reference, row["price"])
        request = {"request_id": response["request_id"], "provider": "oda", "query": arguments["queries"][0],
                   "page": 1, "size": arguments["size"],
                   "emitted_at": receipt["issued_at"], "expires_at": receipt["expires_at"]}
        value = {"request_id": response["request_id"], "provider": "oda", "query": facts["query"],
                 "page": facts["page"], "size": facts["size"], "source_url": facts["url"],
                 "observed_at": response["observed_at"], "hasMore": facts["hasMore"], "products": normalized}
        result = catalog_response(request, value)
        for product in result["products"]:
            literal = displays[product["product_id"]]
            if literal is not None:
                product["display"]["price"] = text(literal)
        return result
