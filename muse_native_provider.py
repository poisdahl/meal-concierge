"""Read-only Oda page observations from Muse's existing native browser profile."""
from collections.abc import Mapping
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import math
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from clients.muse import MAX_PRODUCTS, validate_response as catalog_response
from core import HouseholdError, cart_contents
from muse_browser import (NativeBridge, account_links, digest, native_amount_minor, text,
                          read_json, required_json, request_record, timestamp, transition_lock,
                          validate_claim, validate_response)
from muse_mcp import oda_operation_lock, private_operation_directory


READ_SECONDS = 90.0
TOOLS = {"product_search", "get_cart", "get_delivery_addresses"}
FACTS_COMMON = (
    "Return only one literal JSON object with exactly the listed keys, no prose or extra keys. "
    "Use fresh observed facts from the original continuous profile, never cached or invented values. "
    "signed_in and complete are booleans. account is an object with exactly url and edit_urls: "
    "url is the freshly observed https://oda.com/no/account/delivery/ route; edit_urls is an array "
    "of ALL actual absolute https://oda.com/no/account/delivery/edit/<positive-integer>/ links. "
    "Establish sign-in explicitly on that account page in the same flow. "
)
FACTS_CONTRACTS = {
    "get_cart": FACTS_COMMON + (
        "Root keys: url,signed_in,complete,account,empty,items,amount_rows,delivery_text,address,warnings. "
        "url must be the actual https://oda.com/no/cart/ route. empty is a boolean consistent with items. "
        "items is the complete array of objects with exactly url,title,subtitle,quantity,price: actual "
        "product URL, literal title, literal subtitle string (empty only for verified absence), "
        "positive integer quantity, nullable "
        "literal price string. If subtitle is unknown, stop instead of returning null or empty. "
        "amount_rows is an array of objects with exactly label,value, both "
        "literal strings; never an array of flat strings. Return all observed amount rows; missing "
        "totals remain unknown, do not invent a Total row. delivery_text and address are literal "
        "strings or null; warnings is an array of literal strings. Observe the complete ordinary "
        "cart, not an order-edit cart."
    ),
    "get_delivery_addresses": FACTS_COMMON + (
        "Root keys: url,signed_in,complete,account,rows. url must be the actual delivery-account route. "
        "rows is the complete array of objects with exactly edit_url,address,default,selected: "
        "actual absolute edit URL, literal address, boolean standard-address marker, and selected "
        "as an actually observed boolean or null. A standard marker does not prove selection."
    ),
    "product_search": FACTS_COMMON + (
        "Root keys: url,signed_in,complete,account,query,page,size,hasMore,products. Preserve the actual "
        "matching search URL and requested query,page,size; hasMore is an observed boolean. products "
        "is the bounded array of objects with exactly url,name,description,price,unitPrice,unitName,"
        "availability: actual product URL, required literal name, nullable literal strings for the "
        "other text fields, and availability as an observed boolean or null."
    ),
}


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
        self._operation = threading.local()

    @contextmanager
    def operation(self):
        """Keep one operation's reads, action and finalization under shared custody."""
        if getattr(self._operation, "held", False):
            yield
            return
        with oda_operation_lock(self.operation_directory), self.bridge.custody():
            self._operation.held = True
            try:
                yield
            finally:
                self._operation.held = False

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
        with self.operation():
            if self.terminal_failure:
                raise HouseholdError(self.terminal_failure)
            receipt = self.bridge.request("provider_read", {"tool": tool, "arguments": dict(arguments),
                                          "facts_contract": FACTS_CONTRACTS[tool]},
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
                     and url.path in {"/no/search/", "/no/search/products/"}
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


class MuseCartNotDispatched(HouseholdError):
    def __init__(self, pending):
        super().__init__("Muse native cart task refused before dispatch")
        self.pending = pending


class MuseNativeCartProvider(MuseNativeReadProvider):
    """One explicitly enabled unit delta, using the ordinary core cart journal."""

    @contextmanager
    def change(self, app, request):
        with self.operation():
            self._operation.change = {"app": app, "request": request, "dispatched": False}
            try:
                yield
            finally:
                del self._operation.change

    def binding(self, store):
        if not self._account_ids or self.terminal_failure:
            raise HouseholdError("Muse cart policy requires verified original account evidence")
        return digest({"state_directory": str(store.directory.resolve()), "configuration": store.config,
                       "operation_directory": str(self.operation_directory),
                       "task_id": self.bridge.task_id, "account_links": sorted(self._account_ids)})

    def validate_cart_reconciliation(self, pending, store):
        marker = pending.get("native_cart_binding")
        if (not getattr(self._operation, "held", False) or not isinstance(marker, Mapping)
                or set(marker) != {"binding_digest", "intent_id"}
                or marker["binding_digest"] != self.binding(store)
                or not isinstance(marker["intent_id"], str)
                or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", marker["intent_id"]) is None):
            raise HouseholdError("original Muse cart binding and native custody are required for reconciliation")
        # The unique intent is part of the digest: identical later deltas cannot
        # reuse an earlier task's refusal or completion.
        with transition_lock(self.bridge.directory):
            matches = []
            for path in (self.bridge.directory / "requests").glob("*.json"):
                record = read_json(path)
                if not isinstance(record, Mapping) or not isinstance(record.get("payload"), Mapping):
                    raise HouseholdError("Muse browser request record changed; preserve the journal")
                if record.get("operation") == "cart_change" and record["payload"].get("intent_digest") == digest(pending):
                    matches.append(request_record(self.bridge.directory, path.stem, self.bridge.task_id, active=False)[1])
            if len(matches) != 1:
                raise HouseholdError("Muse original cart request is unavailable or ambiguous; preserve the journal")
            record = matches[0]
            name = record["request_id"] + ".json"
            if not (self.bridge.directory / "claims" / name).exists():
                if record["payload"] != self._cart_payload(pending):
                    raise HouseholdError("Muse original unclaimed cart payload changed; preserve the journal")
                for kind in ("claims", "consumed", "responses", "endings", "publications"):
                    path = self.bridge.directory / kind / name
                    if path.exists() or path.is_symlink():
                        raise HouseholdError("Muse cart admission evidence exists; preserve the journal")
                closed = required_json(self.bridge.directory / "closed" / name)
                if (not isinstance(closed, Mapping)
                        or set(closed) != {"request_digest", "task_id", "closed_at"}
                        or closed["request_digest"] != digest(record)
                        or closed["task_id"] != record["task_id"]
                        or not timestamp(record["expires_at"]) <= timestamp(closed["closed_at"]) <= datetime.now(timezone.utc)):
                    raise HouseholdError("Muse original unclaimed cart request has no valid expired closure")
                return "not_dispatched"
        try:
            ending = read_json(self.bridge.directory / "endings" / name)
        except FileNotFoundError:
            ending = read_json(self.bridge.directory / "responses" / name)
        validate_response(record, ending, fresh=False)
        validate_claim(self.bridge.directory, record, response=ending)
        if ending["task_state"] != "completed":
            raise HouseholdError("Muse original cart task has no actual ending")
        return "not_dispatched" if ending["facts"] == {"dispatch": "not_dispatched"} else "ended"

    def _cart_payload(self, pending):
        return {
            "intent_digest": digest(pending), "operations": pending["operations"],
            "before_quantities": pending["before"], "expected_quantities": pending["expected"],
            "account": {"url": "https://oda.com/no/account/delivery/",
                        "edit_urls": ["https://oda.com/no/account/delivery/edit/" + str(key) + "/"
                                      for key in sorted(self._account_ids)]},
            "cart_url": "https://oda.com/no/cart/", "order_change": None,
            "preconditions": ["fresh explicit sign-in and exact account links",
                              "complete ordinary cart equals before_quantities; no order edit",
                              "one exact product and unique enabled visible unobscured control",
                              "consume the original unexpired permit immediately before one unit click",
                              "never repeat a click; retain the actual task ending"]}

    def call(self, tool, arguments, *, deadline=None):
        change = getattr(self._operation, "change", None)
        if tool != "manipulate_cart":
            result = super().call(tool, arguments, deadline=deadline)
            if tool == "get_cart" and change is not None and not change["dispatched"]:
                app = change["app"]
                before = app._cart_lines(cart_contents(result))[0]
                if app._cart_digest(before) != change["request"]["cart_digest"]:
                    raise HouseholdError("Muse cart changed since its reviewed cart_digest; no action dispatched")
                change["before"] = before
            return result
        if change is None or not getattr(self._operation, "held", False) or change["dispatched"]:
            raise HouseholdError("Muse cart dispatch requires its original guarded unit-change operation")
        fields(arguments, {"operations"})
        app, request = change["app"], change["request"]
        operations = arguments["operations"]
        expected_operations = [{"productId": int(row["product_id"]), "quantity": row["quantity"]}
                               for row in request["operations"]]
        state = app.store.read()
        pending = deepcopy(state.get("pending_cart_change"))
        binding = self.binding(app.store)
        policy = state.get("muse_native_cart_policy") or {}
        if (not isinstance(pending, Mapping) or pending.get("provider") != "oda"
                or pending.get("order_change") is not None
                or not isinstance(pending.get("native_cart_binding"), Mapping)
                or pending["native_cart_binding"].get("binding_digest") != binding
                or pending["native_cart_binding"] != change.get("marker")
                or pending.get("operations") != operations or operations != expected_operations
                or pending.get("before") != change.get("before")
                or state.get("pending_checkout") or state.get("pending_cancellation")):
            raise HouseholdError("Muse original enabled cart policy or pending intent changed; preserve its journal")
        if policy != {"enabled": True, "binding_digest": binding}:
            raise MuseCartNotDispatched(pending)
        facts = self.bridge.request("cart_change", self._cart_payload(pending), deadline=deadline)
        if facts == {"dispatch": "not_dispatched"}:
            raise MuseCartNotDispatched(pending)
        if facts != {"dispatch": "dispatched"}:
            raise HouseholdError("Muse native cart action outcome is unknown; reconcile without replay")
        change["dispatched"] = True
        return facts
