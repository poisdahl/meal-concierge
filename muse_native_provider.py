"""Read-only Oda page observations from Muse's existing native browser profile."""
from collections.abc import Mapping
from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import math
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from clients.muse import MAX_PRODUCTS, validate_response as catalog_response
from core import HouseholdError, cart_contents, oslo_local_timestamp, validate_delivery_slot
from retail_mcp import ODA_MONTHS, oda_cart_delivery_window
from muse_browser import (NativeBridge, account_links, digest, native_amount_minor, text,
                          read_json, required_json, request_record, timestamp, transition_lock,
                          validate_claim, validate_response)
from muse_mcp import oda_operation_lock, private_operation_directory


READ_SECONDS = 110.0
ORDER_READ_SECONDS = 180.0
TOOLS = {"product_search", "get_cart", "get_delivery_addresses", "get_orders", "get_order", "order_tracking", "get_delivery_slots"}
FACTS_COMMON = (
    "Return only one literal JSON object with exactly the listed keys, no prose or extra keys. "
    "Use fresh observed facts from the original continuous profile, never cached or invented values. "
    "signed_in and complete are booleans. account is an object with exactly url and edit_urls: "
    "url is the freshly observed https://oda.com/no/account/delivery/ route; edit_urls is an array "
    "of ALL actual absolute https://oda.com/no/account/delivery/edit/<positive-integer>/ links. "
    "Establish sign-in explicitly on that account page in the same flow. "
)
ORDER_IDENTITY_FACTS = (
    "url must be the actual https://oda.com/no/account/orders/<requested-order_number>/ route. "
    "reference is the independently visible order reference on that detail page, not an echoed "
    "request or a reference inferred only from the URL. It must equal requested order_number. "
    "Observe only ordinary read-only detail sections; never edit, cancel, reorder, pay, download "
    "a receipt or open an external tracking/payment page. Missing fields remain null. "
)
FACTS_CONTRACTS = {
    "get_delivery_slots": FACTS_COMMON + (
        "Root keys: url,signed_in,complete,account,heading,held_delivery_text,cells,dismissed. "
        "Inspect only the ordinary upcoming-delivery picker from Home/cart; open and dismiss it "
        "without selecting. heading is its literal heading; held_delivery_text is the nullable "
        "literal current cart held-delivery window, not a date derived from the request. "
        "cells is the complete displayed picker section, each with exactly date_text,window_text,"
        "button,unavailable_text,expanded. date_text is the literal date-column label including "
        "weekday; window_text is the literal time-window row label. button is null or an object "
        "with exactly label,enabled: the literal price-button name and actual boolean enabled "
        "state. unavailable_text is null or literal Ikke tilgjengelig. expanded is the actual "
        "boolean AX expanded state or null; it is only a hint, never selection proof. dismissed "
        "is true only after the picker closed without changing selection. No merchant slot IDs, "
        "computed years/timezones or selected booleans may be invented."
    ),
    "get_order": FACTS_COMMON + ORDER_IDENTITY_FACTS + (
        "Root keys: url,signed_in,complete,account,reference,status,delivery_text,payment_status,"
        "amount_rows,goods_complete,items. status is the nullable literal detail heading; "
        "delivery_text is nullable literal delivery text. payment_status is the nullable literal "
        "explicit payment badge (such as Betalt), never inferred from delivery or payment method. "
        "amount_rows contains objects with exactly label,value, both literal strings. "
        "complete means the requested detail metadata was observed; it does not prove goods "
        "completeness. goods_complete is a boolean requiring independent verification that ALL "
        "goods rows were observed, including substitutions/refunds; a header item count alone "
        "or visible partial rows do not prove this. If goods completeness is unproved or the "
        "section is unavailable, goods_complete=false and items=null, never []. Otherwise items "
        "is the complete array of objects with exactly url,name,quantity,price,labels: actual "
        "product URL or null when no link exists, required literal name, separately observed "
        "signed integer quantity or null, nullable literal price, and an array of literal status "
        "pill labels. Refund quantities can be negative; package sizes never establish quantity. "
        "A verified empty section may use goods_complete=true,items=[]. Never infer payment "
        "completion, cancellation eligibility or backend freshness."
    ),
    "order_tracking": FACTS_COMMON + ORDER_IDENTITY_FACTS + (
        "Root keys: url,signed_in,complete,account,reference,tracking_status,delivery_text. "
        "tracking_status is a nullable literal EXPLICIT tracking indicator. The order heading "
        "or delivery-location button does not establish tracking: without a separate explicit "
        "tracking indicator return null. delivery_text is nullable literal delivery text. "
        "Do not infer paid, modifiable, cancelled or backend freshness from delivery status."
    ),
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
    "get_orders": FACTS_COMMON + (
        "Root keys: url,signed_in,complete,account,page,size,hasMore,orders. "
        "url is the actual https://oda.com/no/account/orders/ route. Echo requested page and size. "
        "Return the first at most size currently rendered order cards in their displayed order; "
        "complete describes that observed prefix, never the entire historical account. "
        "hasMore is an observed boolean: true when additional rendered cards or a history load-more "
        "control remain beyond this prefix. orders is an array of objects with exactly "
        "url,reference,status,delivery_text,total_text: actual absolute order-detail link, its "
        "literal route reference, and nullable literal displayed status, delivery and total strings. "
        "References may be alphanumeric. Missing card fields stay null. Do not visit order edit or "
        "cancellation controls, infer payment or tracking, or claim backend freshness."
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


def native_upcoming_date(label, today):
    match = re.fullmatch(r"([a-zæøå]+)\.?\s+(\d{1,2})\.?\s+([a-zæøå]+)\.?", text(label).casefold())
    weekdays = {"man": 0, "mandag": 0, "tir": 1, "tirsdag": 1, "ons": 2, "onsdag": 2,
                "tor": 3, "torsdag": 3, "fre": 4, "fredag": 4, "lør": 5, "lørdag": 5,
                "søn": 6, "søndag": 6}
    if match is None or match[1] not in weekdays or match[3][:3] not in ODA_MONTHS:
        raise HouseholdError("Muse native calendar needs its literal weekday, day and month")
    month, day = ODA_MONTHS[match[3][:3]], int(match[2])
    year = today.year + ((month, day) < (today.month, today.day))
    try:
        result = date(year, month, day)
    except ValueError:
        raise HouseholdError("Muse native calendar date is invalid") from None
    if result.weekday() != weekdays[match[1]]:
        raise HouseholdError("Muse native calendar weekday contradicts the inferred upcoming date")
    return result


def native_delivery_times(label):
    match = re.fullmatch(r"(?:kl\.?\s*)?([01]?\d|2[0-3])(?::([0-5]\d))?\s*[-–]\s*"
                         r"([01]?\d|2[0-3])(?::([0-5]\d))?", text(label).casefold())
    if match is None:
        raise HouseholdError("Muse native delivery time row is unavailable")
    return f"{int(match[1]):02d}:{match[2] or '00'}", f"{int(match[3]):02d}:{match[4] or '00'}"


def native_delivery_price(label):
    match = re.fullmatch(r"(?:kr\s*)?(0|[1-9]\d{0,6})(?:,([0-9]{2}))?(?:\s*(?:kr|NOK))?", text(label), re.I)
    if match is None:
        raise HouseholdError("Muse native delivery button lacks an exact price")
    return int(match[1]) * 100 + int(match[2] or '00')


class MuseNativeReadProvider:
    provider, label = "oda", "Oda"

    def __init__(self, operation_directory, task_id, *, accepted_account=None):
        self.operation_directory = private_operation_directory(operation_directory)
        self.bridge = NativeBridge(self.operation_directory / "browser", task_id)
        self._terminal_failure = None
        self._accepted_account = deepcopy(accepted_account)
        self._account_ids = account_links(accepted_account) if accepted_account is not None else None
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

    def validate_account(self, account):
        """Check a complete observed account, including financial observations."""
        if self.terminal_failure:
            raise HouseholdError(self.terminal_failure)
        references = account_links(account)
        if self._account_ids is not None and references != self._account_ids:
            self._terminal_failure = "Oda native browser account evidence changed; reconcile the original profile."
            raise HouseholdError(self.terminal_failure)
        return references

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
        elif tool == "get_delivery_slots":
            if set(arguments) - {"delivery_date"}:
                raise HouseholdError("Muse native delivery reads preserve the original address")
            requested = arguments.get("delivery_date")
            if requested is not None:
                try:
                    if date.fromisoformat(requested).isoformat() != requested:
                        raise ValueError
                except (TypeError, ValueError):
                    raise HouseholdError("Muse native delivery date must be a canonical ISO date") from None
        elif tool == "get_orders":
            fields(arguments, {"page", "size"})
            if (type(arguments["page"]) is not int or arguments["page"] != 1
                    or type(arguments["size"]) is not int or not 1 <= arguments["size"] <= 100):
                raise HouseholdError("Muse native order history requires one bounded page-1 prefix")
        elif tool in {"get_order", "order_tracking"}:
            fields(arguments, {"order_number"})
            if (not isinstance(arguments["order_number"], str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", arguments["order_number"]) is None):
                raise HouseholdError("Muse native order read requires a bounded detail-route reference")
        elif arguments:
            raise HouseholdError("Muse native account/cart read takes no arguments")
        if deadline is not None and (type(deadline) not in {int, float} or not math.isfinite(deadline)):
            raise HouseholdError("Muse native provider deadline must be finite")
        seconds = ORDER_READ_SECONDS if tool in {"get_order", "order_tracking"} else READ_SECONDS
        cutoff = time.monotonic() + seconds
        if deadline is not None:
            cutoff = min(cutoff, deadline)
        # A composed native financial adapter borrows this same operation;
        # nested account reads must not acquire a second browser lease.
        with self.operation():
            if self.terminal_failure:
                raise HouseholdError(self.terminal_failure)
            payload = {"tool": tool, "arguments": dict(arguments), "facts_contract": FACTS_CONTRACTS[tool]}
            if self._accepted_account is not None:
                payload["accepted_account"] = deepcopy(self._accepted_account)
            receipt = self.bridge.request("provider_read", payload,
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
            references = self.validate_account(facts.get("account"))
            if facts.get("complete") is not True:
                raise HouseholdError("Muse native provider needs the complete requested page section")
            source = {"kind": "host_attested", "url": facts.get("url"),
                      "request_id": response["request_id"], "observed_at": response["observed_at"]}
            result = self.validate_read_facts(tool, arguments, facts, response, receipt)
            self._account_ids = references
            if tool == "get_cart":
                self._operation.cart_delivery_observation = (deepcopy(result["delivery"]), response["observed_at"])
            return {**result, "source": source, "observation_scope": "browser_read_only"}

    @classmethod
    def validate_read_facts(cls, tool, arguments, facts, response, receipt):
        """Validate a successful observation without establishing account continuity."""
        if (not isinstance(facts, Mapping) or facts.get("signed_in") is not True
                or facts.get("complete") is not True):
            raise HouseholdError("Muse native read needs complete signed-in facts")
        account_links(facts.get("account"))
        if tool == "get_delivery_addresses":
            return cls._addresses(facts)
        if tool == "get_cart":
            return cls._cart(facts)
        if tool == "get_delivery_slots":
            return cls._delivery_slots(facts, arguments, response, receipt)
        if tool == "get_orders":
            return cls._orders(facts, arguments)
        if tool in {"get_order", "order_tracking"}:
            return cls._order_detail(facts, arguments, tracking=tool == "order_tracking")
        if tool == "product_search":
            return cls._catalog(facts, arguments, response, receipt)
        raise HouseholdError("Muse native read tool is unavailable")

    @staticmethod
    def _delivery_slots(facts, arguments, response, receipt):
        fields(facts, {"url", "signed_in", "complete", "account", "heading", "held_delivery_text", "cells", "dismissed"})
        url = urlsplit(facts["url"]) if isinstance(facts["url"], str) else None
        if (url is None or url.scheme != "https" or url.netloc != "oda.com"
                or url.path not in {"/no/", "/no/cart/"} or url.fragment
                or text(facts["heading"]) != "Velg leveringstid" or facts["dismissed"] is not True):
            raise HouseholdError("Muse native upcoming delivery picker was not observed and dismissed")
        observed = timestamp(response["observed_at"]).astimezone(ZoneInfo("Europe/Oslo"))
        issued = timestamp(receipt["issued_at"]).astimezone(ZoneInfo("Europe/Oslo"))
        if observed.date() != issued.date():
            raise HouseholdError("Muse delivery observation crossed midnight; read fresh calendar context")
        held = None
        if facts["held_delivery_text"] is not None:
            held = oda_cart_delivery_window({"slot_id": None, "display": text(facts["held_delivery_text"])},
                today=observed.date(), allow_unknown_slot_id=True)
        cells = facts["cells"]
        if not isinstance(cells, list) or not cells or len(cells) > 500:
            raise HouseholdError("Muse native picker needs one complete nonempty displayed section")
        slots, displays, provenance, unavailable, seen = [], {}, {}, [], set()
        for cell in cells:
            fields(cell, {"date_text", "window_text", "button", "unavailable_text", "expanded"})
            day_label, window = text(cell["date_text"]), text(cell["window_text"])
            if cell["expanded"] is not None and type(cell["expanded"]) is not bool:
                raise HouseholdError("Muse native expanded hint must be observed or unknown")
            button = cell["button"]
            if cell["unavailable_text"] is not None:
                if text(cell["unavailable_text"]) != "Ikke tilgjengelig" or button is not None:
                    raise HouseholdError("Muse native unavailable cell evidence conflicts")
                unavailable.append(deepcopy(cell))
                continue
            fields(button, {"label", "enabled"})
            if type(button["enabled"]) is not bool:
                raise HouseholdError("Muse native delivery button enabled state is unknown")
            if not button["enabled"]:
                unavailable.append(deepcopy(cell))
                continue
            day = native_upcoming_date(day_label, observed.date())
            start, end = native_delivery_times(window)
            start_at, end_at = oslo_local_timestamp(day, start), oslo_local_timestamp(day, end)
            if timestamp(end_at) <= observed:
                unavailable.append(deepcopy(cell))
                continue
            reference = f"oda-native:{day.isoformat()}:{start}-{end}"
            if reference in seen:
                raise HouseholdError("Muse native delivery cell identity is ambiguous")
            seen.add(reference)
            price = native_delivery_price(text(button["label"]))
            selected = bool(held and held["date"] == day.isoformat()
                            and held["start"] == start and held["end"] == end)
            slot = validate_delivery_slot({"slot_ref": reference, "provider_slot_id": None,
                "start_at": start_at, "end_at": end_at, "price_ore": price,
                "price_kind": "exact", "selected": selected})
            slots.append(slot)
            displays[reference] = f"{day_label} {window} {text(button['label'])}"
            provenance[reference] = {"raw": deepcopy(cell), "observed_at": response["observed_at"],
                "year_basis": "next_nonpast_date", "timezone_basis": "provider_policy",
                "currency_basis": "provider_policy", "selection_basis": "fresh_cart_window" if selected else None}
        if held and sum(slot["selected"] for slot in slots) != 1:
            raise HouseholdError("Muse held cart delivery has no unique enabled picker cell")
        requested = arguments.get("delivery_date")
        visible_slots = [slot for slot in slots if requested is None
                         or datetime.fromisoformat(slot["start_at"]).date().isoformat() == requested]
        return {"slots": visible_slots, "display": {s["slot_ref"]: displays[s["slot_ref"]] for s in visible_slots},
                "native_slot_observations": {s["slot_ref"]: provenance[s["slot_ref"]] for s in visible_slots},
                "unavailable_cells": unavailable,
                "availability": {"date": requested, "selectable": len(visible_slots), "scope": "rendered_picker_section"}}

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
                "deliveryAddress": None if facts["address"] is None else text(facts["address"]),
                "warnings": [text(value) for value in warnings]}

    @staticmethod
    def _orders(facts, arguments):
        fields(facts, {"url", "signed_in", "complete", "account", "page", "size", "hasMore", "orders"})
        if (facts["url"] != "https://oda.com/no/account/orders/"
                or type(facts["page"]) is not int or facts["page"] != arguments["page"]
                or type(facts["size"]) is not int or facts["size"] != arguments["size"]
                or type(facts["hasMore"]) is not bool):
            raise HouseholdError("Muse native order history page changed")
        cards = facts["orders"]
        if (not isinstance(cards, list) or len(cards) > arguments["size"]
                or not cards and facts["hasMore"]):
            raise HouseholdError("Muse native order history prefix is unavailable")
        rows, seen = [], set()
        for card in cards:
            fields(card, {"url", "reference", "status", "delivery_text", "total_text"})
            match = (re.fullmatch(r"https://oda\.com/no/account/orders/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/",
                                 card["url"]) if isinstance(card["url"], str) else None)
            if match is None or card["reference"] != match[1] or match[1] in seen:
                raise HouseholdError("Muse native order history identity is invalid or repeated")
            seen.add(match[1])
            rows.append({"orderNumber": match[1], "url": card["url"],
                         "status": None if card["status"] is None else text(card["status"]),
                         "delivery_display": None if card["delivery_text"] is None else text(card["delivery_text"]),
                         "sum_display": None if card["total_text"] is None else text(card["total_text"])})
        return {"orders": rows, "page": facts["page"], "size": facts["size"], "hasMore": facts["hasMore"],
                "history_scope": {"kind": "rendered_prefix", "page": facts["page"],
                                  "size": facts["size"], "hasMore": facts["hasMore"]},
                "rendered_row_count": len(rows), "evidence_kind": "browser_rendered",
                "backend_freshness": "unverified", "source_origin": "https://oda.com",
                "source_path": "/no/account/orders/", "source_query_keys": []}

    @staticmethod
    def _order_detail(facts, arguments, *, tracking):
        common = {"url", "signed_in", "complete", "account", "reference", "delivery_text"}
        fields(facts, common | ({"tracking_status"} if tracking else
               {"status", "payment_status", "amount_rows", "goods_complete", "items"}))
        reference = arguments.get("order_number")
        if (not isinstance(reference, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", reference) is None
                or facts["reference"] != reference
                or facts["url"] != f"https://oda.com/no/account/orders/{reference}/"):
            raise HouseholdError("Muse native order detail identity changed")
        result = {"order_id" if tracking else "orderNumber": reference,
                  "evidence_kind": "browser_rendered", "backend_freshness": "unverified",
                  "source_origin": "https://oda.com", "source_path": f"/no/account/orders/{reference}/",
                  "source_query_keys": [],
                  "delivery_display": None if facts["delivery_text"] is None else text(facts["delivery_text"])}
        if tracking:
            return {**result, "status": None if facts["tracking_status"] is None else text(facts["tracking_status"])}
        result.update(status=None if facts["status"] is None else text(facts["status"]),
                      payment_status=None if facts["payment_status"] is None else text(facts["payment_status"]))
        rows = facts["amount_rows"]
        if not isinstance(rows, list) or len(rows) > 500:
            raise HouseholdError("Muse native order amount rows are invalid")
        result["amount_displays"] = []
        for row in rows:
            fields(row, {"label", "value"})
            result["amount_displays"].append({"label": text(row["label"]), "value": text(row["value"])})
        complete, items = facts["goods_complete"], facts["items"]
        if type(complete) is not bool or (not complete and items is not None):
            raise HouseholdError("Muse native order goods completeness is unavailable")
        result["goods_complete"] = complete
        if not complete:
            return result
        if not isinstance(items, list) or len(items) > 1000:
            raise HouseholdError("Muse native order goods are invalid")
        result["products"] = []
        for row in items:
            fields(row, {"url", "name", "quantity", "price", "labels"})
            name = text(row["name"])
            quantity, labels = row["quantity"], row["labels"]
            if (not name or quantity is not None and (type(quantity) is not int or abs(quantity) > 1_000_000)
                    or not isinstance(labels, list) or len(labels) > 20):
                raise HouseholdError("Muse native order goods row is invalid")
            result["products"].append({"id": None if row["url"] is None else product_id(row["url"]),
                "name": name, "quantity": quantity,
                "price": None if row["price"] is None else text(row["price"]),
                "status_labels": [text(label) for label in labels]})
        return result

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
    """Bound unit effects for raw cart changes and shared product-plan applies."""

    # Nonfinancial plan sync needs complete quantities; a missing displayed total stays unknown.
    managed_cart_summary = staticmethod(cart_contents)

    @contextmanager
    def change(self, app, request):
        with self.operation():
            self._operation.change = {"app": app, "request": request, "dispatched": False}
            try:
                yield
            finally:
                del self._operation.change

    @contextmanager
    def managed_apply(self, app, request, command_id):
        with self.change(app, request):
            self._operation.change.update(managed_request=deepcopy(request),
                                          command_id=command_id, last_cart=None)
            yield

    @staticmethod
    def _cart_intent_digest(pending):
        frozen = deepcopy(pending)
        if frozen.get("native_managed"):
            # At dispatch the verified prefix equals this unit's immutable before.
            frozen["native_managed"]["verified"] = frozen["before"]
        return digest(frozen)

    def binding(self, store):
        if not self._account_ids or self.terminal_failure:
            raise HouseholdError("Muse cart policy requires verified original account evidence")
        return digest({"state_directory": str(store.directory.resolve()), "configuration": store.config,
                       "operation_directory": str(self.operation_directory),
                       "task_id": self.bridge.task_id, "account_links": sorted(self._account_ids)})

    @staticmethod
    def delivery_cart_summary(cart):
        # Slot/quantity verification is nonfinancial; keep absent amounts unknown.
        return {**cart_contents(cart), "total": cart.get("total"), "delivery": deepcopy(cart.get("delivery"))}

    def cart_delivery_window(self, delivery, *, today=None):
        observation = getattr(self._operation, "cart_delivery_observation", None)
        if (not observation or not isinstance(delivery, Mapping)
                or delivery.get("slot_id") is not None
                or delivery.get("display") != observation[0].get("display")):
            raise HouseholdError("Muse native delivery comparison needs the original fresh cart observation")
        observed_day = timestamp(observation[1]).astimezone(ZoneInfo("Europe/Oslo")).date()
        if today is not None and today != observed_day:
            raise HouseholdError("Muse cart calendar context crossed midnight; read fresh")
        return oda_cart_delivery_window(delivery, today=observed_day, allow_unknown_slot_id=True)

    def cart_delivery_matches_slot(self, delivery, value, *, today=None):
        slot = validate_delivery_slot(value)
        start, end = [timestamp(slot[key]).astimezone(ZoneInfo("Europe/Oslo")) for key in ("start_at", "end_at")]
        if (slot["provider_slot_id"] is not None or start.date() != end.date()
                or slot["slot_ref"] != f"oda-native:{start.date().isoformat()}:{start:%H:%M}-{end:%H:%M}"):
            return False
        window = self.cart_delivery_window(delivery, today=today)
        return (window["date"] == start.date().isoformat()
                and window["start"] == start.strftime("%H:%M") and window["end"] == end.strftime("%H:%M"))

    @staticmethod
    def delivery_selection_arguments(slot):
        normalized = validate_delivery_slot(slot)
        if normalized["provider_slot_id"] is not None or not normalized["slot_ref"].startswith("oda-native:"):
            raise HouseholdError("Muse delivery requires its exact local native slot reference")
        return {"delivery_slot_ref": normalized["slot_ref"]}

    @contextmanager
    def delivery_selection(self, app, request):
        with self.operation():
            self._operation.delivery = {"app": app, "request": deepcopy(request)}
            try:
                yield
            finally:
                del self._operation.delivery

    def _select_delivery(self, arguments, *, deadline):
        fields(arguments, {"delivery_slot_ref"})
        context = getattr(self._operation, "delivery", None)
        if (context is None or not getattr(self._operation, "held", False)
                or arguments["delivery_slot_ref"] != context["request"].get("slot_ref")):
            raise HouseholdError("Muse delivery selection requires its exact original public request")
        app = context["app"]
        state = app.store.read()
        if any(state.get(key) for key in ("pending_checkout", "pending_cart_change", "pending_cancellation", "order_change")):
            raise HouseholdError("finish the original pending operation before selecting delivery")
        observation = getattr(self._operation, "slot_observation", None)
        reference = arguments["delivery_slot_ref"]
        slot = next((row for row in observation["slots"] if row["slot_ref"] == reference), None) if observation else None
        if slot is None or (datetime.now(timezone.utc) - timestamp(observation["source"]["observed_at"])).total_seconds() > 30:
            raise HouseholdError("Muse delivery target requires its freshly observed original cell")
        original = observation["native_slot_observations"][reference]
        cart = self.call("get_cart", {}, deadline=deadline)
        before = app._cart_lines(cart_contents(cart))[0]
        if slot["selected"] and self.cart_delivery_matches_slot(cart["delivery"], slot):
            return {"selected": True, "idempotent": True}
        payload = {"binding_digest": self.binding(app.store), "slot": deepcopy(slot),
            "target": deepcopy(original["raw"]), "observed_at": original["observed_at"],
            "before_quantities": before, "account": deepcopy(self._accepted_account) if self._accepted_account else {
                "url": "https://oda.com/no/account/delivery/", "edit_urls": [
                    f"https://oda.com/no/account/delivery/edit/{key}/" for key in sorted(self._account_ids)]},
            "effect": "one_delivery_cell_selection", "preconditions": [
                "fresh original sign-in and ENTIRE account set; complete ordinary cart equals before_quantities",
                "same one unique enabled visible date-column/time-row/price-button target, price unchanged",
                "calendar interpretation still belongs to the observation's current local day",
                "consume original unexpired permit immediately before one cell click; never repeat",
                "preserve every cart quantity; retain actual task ending; independently read selected delivery"]}
        result = self.bridge.request("delivery_select", payload, deadline=deadline)
        if result == {"dispatch": "not_dispatched"}:
            raise HouseholdError("Muse delivery was not selected; inspect the current picker before a new request")
        if result != {"dispatch": "selected_once"}:
            raise HouseholdError("Muse delivery selection is uncertain; reconcile the original native task without replay")
        after = self.call("get_cart", {}, deadline=deadline)
        if app._cart_lines(cart_contents(after))[0] != before:
            raise HouseholdError("Cart drifted during delivery selection; preserve the observed goods without repair")
        return {"selected": True}

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
                if record.get("operation") == "cart_change" and record["payload"].get("intent_digest") == self._cart_intent_digest(pending):
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
            "intent_digest": self._cart_intent_digest(pending), "operations": pending["operations"],
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
        if tool == "select_delivery_slot":
            return self._select_delivery(arguments, deadline=deadline)
        change = getattr(self._operation, "change", None)
        if tool != "manipulate_cart":
            result = super().call(tool, arguments, deadline=deadline)
            if tool == "get_delivery_slots":
                self._operation.slot_observation = deepcopy(result)
            if tool == "get_cart" and change is not None:
                if "managed_request" in change:
                    change["last_cart"] = result
                elif not change["dispatched"]:
                    app = change["app"]
                    before = app._cart_lines(cart_contents(result))[0]
                    if app._cart_digest(before) != change["request"]["cart_digest"]:
                        raise HouseholdError("Muse cart changed since its reviewed cart_digest; no action dispatched")
                    change["before"] = before
            return result
        if change is not None and "managed_request" in change:
            return self._managed_units(arguments, deadline=deadline)
        return self._dispatch_unit(arguments, deadline=deadline)

    def _managed_units(self, arguments, *, deadline):
        fields(arguments, {"operations"})
        change = self._operation.change
        app = change["app"]
        cart = change["last_cart"]
        if cart is None:
            raise HouseholdError("managed native batch requires a complete pre-write cart")
        for operation in arguments["operations"]:
            direction = 1 if operation["quantity"] > 0 else -1
            for _ in range(abs(operation["quantity"])):
                if deadline is not None and time.monotonic() >= deadline:
                    raise HouseholdError("managed cart deadline reached; reconcile the original verified prefix")
                before = app._cart_lines(cart_contents(cart))[0]
                unit = [{"productId": operation["productId"], "quantity": direction}]
                app._native_managed_intent(change["command_id"], change["managed_request"], before, unit)
                marker = {"binding_digest": self.binding(app.store), "intent_id": str(uuid.uuid4())}
                with app.store.locked() as state:
                    state["pending_cart_change"]["native_cart_binding"] = marker
                change.update(request={"operations": [{"product_id": str(operation["productId"]),
                                                        "quantity": direction}]},
                              before=before, marker=marker, dispatched=False)
                self._dispatch_unit({"operations": unit}, deadline=deadline)
                cart = self.call("get_cart", {}, deadline=deadline)
                app._verify_native_managed_batch(change["command_id"],
                                                app._cart_lines(cart_contents(cart))[0])
        return cart

    def _dispatch_unit(self, arguments, *, deadline):
        change = getattr(self._operation, "change", None)
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
        # The complete reviewed apply was admitted once by its original policy.
        # Disabling future admission does not replace or revoke that bounded command.
        if "managed_request" not in change and policy != {"enabled": True, "binding_digest": binding}:
            raise MuseCartNotDispatched(pending)
        facts = self.bridge.request("cart_change", self._cart_payload(pending), deadline=deadline)
        if facts == {"dispatch": "not_dispatched"}:
            raise MuseCartNotDispatched(pending)
        if facts != {"dispatch": "dispatched"}:
            raise HouseholdError("Muse native cart action outcome is unknown; reconcile without replay")
        change["dispatched"] = True
        return facts
