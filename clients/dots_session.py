#!/usr/bin/env python3
"""Bounded foreground core commands with an explicit native-cloud host."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import select
import stat
import sys
import termios
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clients import dots
from core import HouseholdError, StateStore, cart_summary
from meny import normalize_cart_snapshot, normalize_meny_delivery_slot, meny_order_card_status
from product_observations import normalize_meny_product_search
import on_demand
from runtime_ownership import file_lock
from service import Application

CORE_OPERATIONS = {"health", "setup", "profile", "recipes", "menu", "feedback",
                   "pantry", "recurring", "product_favorites"}
OWNER_FILES = (".service-owner.lock", "recipes.sqlite3.owner.lock", "state.json.owner.lock")
MAX_LINE = 65536
PRODUCTS_APPLY_BUDGET_SECONDS = 600
ORDER_UI_EVIDENCE = {"evidence_kind": "host_attested_rendered_ui", "backend_freshness": "unverified"}


def order_ui_text(value, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
        raise HouseholdError("native rendered order text changed")
    return value.strip()


def order_ui_location(origin, path, query_keys, fragment, order_id=None):
    expected = (f"/trumf-profil/nettbutikk/bestilling/{order_id}" if order_id
                else "/trumf-profil/nettbutikk")
    keys = ["archived", "mworderid"] if order_id else []
    if (origin != "https://meny.no" or path != expected
            or fragment != ("" if order_id else "#/bestillinger")
            or not isinstance(query_keys, list) or any(not isinstance(key, str) for key in query_keys)
            or sorted(query_keys) != keys):
        raise HouseholdError("native rendered order route changed")
    return {"source_origin": origin, "source_path": path, "source_query_keys": keys}


def normalize_order_ui(result, *, limit=None, order_id=None):
    common = {"authenticated", "authenticated_count", "ready", "main_count", "heading_count",
              "source_origin", "source_path", "source_query_keys", "source_hash", "heading"}
    fields = ({"table_count", "columns", "rendered_row_count", "rows_complete", "pagination_count", "orders"}
              if order_id is None else {"order_number", "status_markers", "item_heading", "item_count",
                                      "item_table_count", "item_columns", "item_rows_complete", "products", "amounts"})
    dots.object_fields(result, common | fields, common | fields)
    if (result["authenticated"] is not True or result["ready"] is not True
            or any(type(result[key]) is not int or result[key] != 1
                   for key in ("authenticated_count", "main_count", "heading_count"))):
        raise HouseholdError("native rendered order scope is unavailable")
    evidence = {**ORDER_UI_EVIDENCE, **order_ui_location(result["source_origin"], result["source_path"],
                result["source_query_keys"], result["source_hash"], order_id)}
    if order_id is None:
        count, rows = result["rendered_row_count"], result["orders"]
        if (result["heading"] != "Bestillinger fra de siste 6 måneder"
                or type(result["table_count"]) is not int or result["table_count"] != 1
                or result["columns"] != ["BESTILLINGSKODE", "STATUS", "UTLEVERING", "TID", "SUM"]
                or type(result["pagination_count"]) is not int or result["pagination_count"] != 0
                or result["rows_complete"] is not True or type(count) is not int or not 1 <= count <= 10000
                or not isinstance(rows, list) or len(rows) != min(limit, count)):
            raise HouseholdError("native rendered history window is unavailable; empty history is not verified")
        orders, seen = [], set()
        for row in rows:
            keys = {"order_number", "cell_count", "links", "status_marker", "delivery_display", "time_display", "sum_display"}
            dots.object_fields(row, keys, keys)
            identity = row["order_number"]
            if (not isinstance(identity, str) or re.fullmatch(r"[0-9]{1,20}", identity) is None
                    or identity in seen or type(row["cell_count"]) is not int or row["cell_count"] != 5
                    or not isinstance(row["links"], list) or not 1 <= len(row["links"]) <= 2):
                raise HouseholdError("native rendered order identities changed")
            for link in row["links"]:
                dots.object_fields(link, {"origin", "path", "query_keys", "hash"}, {"origin", "path", "query_keys", "hash"})
                order_ui_location(link["origin"], link["path"], link["query_keys"], link["hash"], identity)
            seen.add(identity)
            orders.append({**evidence, "history_scope": "rendered_last_six_months", "order_number": identity,
                           "status": meny_order_card_status(order_ui_text(row["status_marker"], optional=True)),
                           **{key: order_ui_text(row[key], optional=True)
                              for key in ("delivery_display", "time_display", "sum_display")}})
        return {"provider": "meny", **evidence, "history_scope": "rendered_last_six_months",
                "rendered_row_count": count, "orders": orders}
    products, count, markers = result["products"], result["item_count"], result["status_markers"]
    if (result["order_number"] != order_id
            or re.fullmatch(r"BESTILLING\s+\S+", order_ui_text(result["heading"]), re.IGNORECASE) is None
            or not isinstance(markers, list) or len(markers) > 1
            or type(count) is not int or not 1 <= count <= 10000
            or result["item_heading"] != f"Bestilte varer ({count})"
            or type(result["item_table_count"]) is not int or result["item_table_count"] != 1
            or result["item_columns"] != ["VARE", "MENGDE"] or result["item_rows_complete"] is not True
            or not isinstance(products, list) or not 1 <= len(products) <= 1000):
        raise HouseholdError("native rendered order detail is incomplete or changed")
    items = []
    for item in products:
        dots.object_fields(item, {"name", "quantity"}, {"name", "quantity"})
        if type(item["quantity"]) is not int or not 1 <= item["quantity"] <= 10000:
            raise HouseholdError("native rendered order quantity changed")
        items.append({"name": order_ui_text(item["name"]), "quantity": item["quantity"]})
    if sum(item["quantity"] for item in items) != count:
        raise HouseholdError("native rendered order item count changed")
    amounts = result["amounts"]
    if not isinstance(amounts, dict) or set(amounts) - {"Betalt beløp (kort)"}:
        raise HouseholdError("native rendered order amount labels changed")
    return {"provider": "meny", **evidence, "order_number": order_id,
            "status": meny_order_card_status(order_ui_text(markers[0])) if markers else "unknown",
            "amount_displays": {key: order_ui_text(value) for key, value in amounts.items()},
            "order_total": None, "payment_status": "unknown", "productQuantityCount": count, "products": items}


class Input:
    def __init__(self):
        self.buffer = bytearray()

    def line(self, deadline):
        while True:
            if time.monotonic() >= deadline:
                raise HouseholdError("native host reply deadline reached; never repeat an uncertain write")
            index = self.buffer.find(b"\n")
            if index >= 0:
                if index > MAX_LINE:
                    raise HouseholdError("native host input exceeds its byte limit")
                data = bytes(self.buffer[:index])
                del self.buffer[:index + 1]
                return on_demand._json(data)
            if len(self.buffer) > MAX_LINE:
                raise HouseholdError("native host input exceeds its byte limit")
            if not select.select([sys.stdin], [], [], max(0, deadline - time.monotonic()))[0]:
                continue
            data = os.read(sys.stdin.fileno(), min(4096, MAX_LINE + 1 - len(self.buffer)))
            if not data:
                raise HouseholdError("native host input ended; never repeat an uncertain write")
            self.buffer.extend(data)


@contextmanager
def input_mode():
    # Native interactive executors use a PTY. Canonical mode truncates long
    # JSON lines; echo also mixes host inputs into machine-readable stdout.
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd) if os.isatty(fd) else None
    try:
        if original is not None:
            mode = termios.tcgetattr(fd)
            mode[3] &= ~(termios.ICANON | termios.ECHO)
            mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
            termios.tcsetattr(fd, termios.TCSANOW, mode)
        yield
    finally:
        if original is not None:
            termios.tcsetattr(fd, termios.TCSANOW, original)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def configuration(value):
    dots.object_fields(value, {"household", "browser_binding", "allow_cart_writes", "target_registry"}, {"household"})
    if not isinstance(value["household"], str) or not 1 <= len(value["household"].strip()) <= 150:
        raise ValueError("household must be bounded text")
    if type(value.get("allow_cart_writes", False)) is not bool:
        raise ValueError("allow_cart_writes must be a boolean")
    binding = value.get("browser_binding")
    if binding is not None:
        fields = {"origin", "browser_id", "tab_id", "account_sha256", "cart_context_sha256"}
        dots.object_fields(binding, fields, fields)
        if binding["origin"] != "https://meny.no":
            raise ValueError("only the native cloud MENY browser is supported")
        for field in ("browser_id", "tab_id"):
            if not isinstance(binding[field], str) or not 1 <= len(binding[field]) <= 150:
                raise ValueError("browser/tab identity is required")
        for field in ("account_sha256", "cart_context_sha256"):
            if not isinstance(binding[field], str) or re.fullmatch(r"[a-f0-9]{64}", binding[field]) is None:
                raise ValueError("approved account/cart context must have an exact SHA256 identity")
    if value.get("allow_cart_writes") and binding is None:
        raise ValueError("cart writes require an approved native cloud browser/account/cart binding")
    if "target_registry" in value:
        registry = value["target_registry"]
        if (binding is None or not isinstance(registry, str) or not registry
                or not Path(registry).is_absolute()
                or Path(registry).name != ".meal-concierge-dots-targets"):
            raise ValueError("target_registry requires a browser binding and an absolute shared registry path")
        path = Path(registry)
        dots.existing_root(path.parent)
        if str(path) != registry or path.resolve(strict=False) != path:
            raise ValueError("target_registry must have a canonical path")
    return value


def cart_writes_enabled(config, state):
    enabled = state.get("native_cart_writes_enabled", config.get("allow_cart_writes", False))
    if type(enabled) is not bool:
        raise ValueError("original native cart write policy is invalid")
    return enabled


def validate_cart_policy(request, config):
    if request.get("action") == "show":
        dots.object_fields(request, {"operation", "action"}, {"operation", "action"})
    elif request.get("action") == "set":
        fields = {"operation", "action", "enabled", "browser_binding"}
        dots.object_fields(request, fields, fields)
        if type(request["enabled"]) is not bool:
            raise ValueError("native cart write policy requires an explicit boolean")
        if config.get("browser_binding") is None or request["browser_binding"] != config["browser_binding"]:
            raise ValueError("native cart write policy requires the exact original browser/account/cart binding")
    else:
        raise ValueError("native cart write policy supports only show or set")


def cart_policy(store, config, request):
    with store.locked() as state:
        if state.get("native_config_sha256") != dots.digest(dots.encoded(config)):
            raise ValueError("original native configuration binding changed")
        if request["action"] == "set":
            if request["enabled"] and state.get("pending_cart_change"):
                raise ValueError("reconcile the original pending cart change before enabling writes")
            state["native_cart_writes_enabled"] = request["enabled"]
        return {"enabled": cart_writes_enabled(config, state)}


class NativeHost:
    def __init__(self, store, config, reader, command_id, deadline, request=None):
        self.store, self.config, self.reader = store, config, reader
        self.command_id, self.deadline = command_id, deadline
        self.calls = 0
        self.product_searches = 0
        self.request = request or {}
        self.last_cart = None

    def managed_intent(self, operations):
        if self.request.get("operation") != "products" or self.request.get("action") != "apply":
            return False
        if self.last_cart is None:
            raise HouseholdError("managed native batch requires a complete pre-write cart")
        before = self.app._cart_lines(cart_summary(self.last_cart))[0]
        expected = dict(before)
        for item in operations:
            product = item["productId"]
            quantity = expected.get(product, 0) + item["quantity"]
            if quantity < 0:
                raise HouseholdError("managed batch exceeds observed cart quantities")
            if quantity:
                expected[product] = quantity
            else:
                expected.pop(product, None)
        with self.store.locked() as state:
            pending = state.get("pending_cart_change")
            if pending:
                managed = pending.get("native_managed")
                if (not managed or managed["command_id"] != self.command_id
                        or managed["verified"] != before or pending["expected"] != before):
                    raise HouseholdError("reconcile the original native managed batch before dispatch")
                managed = deepcopy(managed)
            else:
                plan = state.get("cart_plan")
                digest = self.request.get("product_plan_digest")
                if (not isinstance(plan, dict) or not state.get("managed_product_apply_fence")
                        or plan.get("provider") != "meny" or state.get("order_change")
                        or re.fullmatch(r"[a-f0-9]{64}", digest or "") is None):
                    raise HouseholdError("original managed menu/product-plan intent is required")
                managed = {"command_id": self.command_id, "request": deepcopy(self.request),
                           "menu_ref": deepcopy(plan["menu_ref"]), "product_plan_digest": digest,
                           "context_digest": self.app._product_current_context(state),
                           "initial_plan": deepcopy(plan), "initial": before, "verified": before}
            state["pending_cart_change"] = {
                "provider": "meny", "order_change": None, "operations": deepcopy(operations),
                "before": before, "expected": expected, "native_managed": managed}
        return True

    def pending_binding(self):
        state = self.store.read()
        if state.get("native_config_sha256") != dots.digest(dots.encoded(self.config)):
            raise HouseholdError("original native household/browser configuration changed")
        return state.get("pending_cart_change")

    def exchange(self, operation, arguments, *, deadline=None, write=False):
        binding = self.config.get("browser_binding")
        if binding is None:
            raise HouseholdError("native cloud browser/account/cart binding is required; no browser started")
        self.pending_binding()
        self.calls += 1
        if self.calls > 64:
            raise HouseholdError("native host command exceeded its call limit")
        reply_seconds = 120 if operation in {"get_delivery_slots", "product_search"} and not write else 60
        deadline = min(self.deadline, deadline or self.deadline, time.monotonic() + reply_seconds)
        if deadline <= time.monotonic():
            raise HouseholdError("native host operation deadline reached")
        call_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=max(0, deadline - time.monotonic()))
        frame = {"kind": "native_host_request", "command_id": self.command_id, "call_id": call_id,
                 "provider": "meny", "operation": operation, "arguments": arguments,
                 "browser_binding": binding, "emitted_at": now.isoformat(), "expires_at": expires.isoformat(),
                 "effect": "cart_write" if write else "read"}
        if write:
            with self.store.locked() as state:
                if not cart_writes_enabled(self.config, state):
                    raise HouseholdError("native cart writes are disabled in this private household")
                pending = state.get("pending_cart_change")
                if (not pending or pending.get("provider") != "meny" or pending.get("order_change")
                        or pending.get("operations") != arguments.get("operations")
                        or state.get("native_config_sha256") != dots.digest(dots.encoded(self.config))):
                    raise HouseholdError("original core cart intent or native context is absent or changed")
                frame["before_quantities"] = pending["before"]
                frame["expected_quantities"] = pending["expected"]
                frame["preconditions"] = ["same approved cloud browser/account/cart context", "no order edit",
                                           "complete current cart equals before_quantities",
                                           "exact product and unique enabled unobscured control", "unexpired request"]
        emit(frame)  # Core intent and immutable household identity precede issuance.
        reply = self.reader.line(deadline)
        dots.object_fields(reply, {"reply_to", "browser_binding", "observed_at", "result", "error"},
                           {"reply_to", "browser_binding", "observed_at"})
        observed = dots.timestamp(reply["observed_at"])
        if (reply["reply_to"] != call_id or reply["browser_binding"] != binding
                or not now <= observed <= datetime.now(timezone.utc) < expires):
            raise HouseholdError("native host reply identity or lifetime changed")
        if ("result" in reply) == ("error" in reply):
            raise HouseholdError("native host must return exactly one result or error")
        if "error" in reply:
            raise HouseholdError("native host operation failed; reconcile any pending write without resend")
        if not isinstance(reply["result"], dict):
            raise HouseholdError("native host result must be an object")
        return reply["result"]

    def probe(self, **kwargs):
        self.verify_order_change(None, None, deadline=kwargs.get("deadline"))
        return {"status": "ready", "provider": "meny", "protocol_version": "native-host-stdio-v1",
                "server": {"name": "host-attested native cloud MENY"}, "tool_count": 7}

    def verify_order_change(self, order_id, code, *, deadline=None):
        if order_id is not None or code is not None:
            raise HouseholdError("native foreground adapter does not support existing-order edits")
        result = self.exchange("verify_new_cart", {}, deadline=deadline)
        if set(result) != {"authenticated", "new_cart"} or any(result[key] is not True for key in result):
            raise HouseholdError("native account or new-cart mode could not be verified")
        return result

    def call(self, tool, arguments, *, deadline=None, **kwargs):
        if tool == "get_orders":
            if (set(arguments) != {"page", "size"} or type(arguments["page"]) is not int
                    or arguments["page"] != 1 or type(arguments["size"]) is not int
                    or not 1 <= arguments["size"] <= 5):
                raise HouseholdError("native orders require a first rendered window of at most five rows")
            return normalize_order_ui(self.exchange(tool, arguments, deadline=deadline), limit=arguments["size"])
        if tool == "get_order":
            dots.object_fields(arguments, {"order_number"}, {"order_number"})
            order_id = arguments["order_number"]
            if not isinstance(order_id, str) or re.fullmatch(r"[0-9]{1,20}", order_id) is None:
                raise HouseholdError("native order requires an exact decimal identity")
            return normalize_order_ui(self.exchange(tool, arguments, deadline=deadline), order_id=order_id)
        if tool == "get_delivery_slots":
            dots.object_fields(arguments, {"delivery_date"}, set())
            requested_date = arguments.get("delivery_date")
            if requested_date is not None and (
                    not isinstance(requested_date, str)
                    or date.fromisoformat(requested_date).isoformat() != requested_date):
                raise HouseholdError("native delivery date must be a canonical ISO date")
            result = self.exchange(tool, arguments, deadline=deadline)
            if (set(result) != {"authenticated", "ready", "source_url", "dialog_count", "slots"}
                    or result["authenticated"] is not True or result["ready"] is not True
                    or result["source_url"] != "https://meny.no/varer"
                    or type(result["dialog_count"]) is not int or result["dialog_count"] != 1
                    or not isinstance(result["slots"], list) or not result["slots"]):
                raise HouseholdError("native delivery picker scope or rendered slots changed")
            slots, display = [], {}
            for raw in result["slots"]:
                slot = normalize_meny_delivery_slot(raw)
                if slot["slot_ref"] in display:
                    raise HouseholdError("native delivery slot identity changed")
                slots.append(slot)
                display[slot["slot_ref"]] = raw["display"]
            if sum(slot["selected"] for slot in slots) > 1:
                raise HouseholdError("native delivery selection is ambiguous")
            if requested_date is not None:
                slots = [slot for slot in slots if slot["slot_ref"].startswith(f"meny:{requested_date}T")]
                display = {slot["slot_ref"]: display[slot["slot_ref"]] for slot in slots}
            return {"provider": "meny", "slots": slots, "display": display}
        if tool == "product_search":
            queries, size = arguments.get("queries"), arguments.get("size")
            if (set(arguments) != {"queries", "page", "size"} or arguments["page"] != 1
                    or type(arguments["page"]) is not int or type(size) is not int or size != 5
                    or not isinstance(queries, list) or len(queries) != 1
                    or not isinstance(queries[0], str) or not 1 <= len(queries[0]) <= 200):
                raise HouseholdError("native product search requires one bounded first-page query")
            if self.request.get("operation") == "products" and self.request.get("action") == "apply":
                if self.product_searches:
                    raise HouseholdError("native apply validates one search per command; continue the saved validation")
                self.product_searches += 1  # Failed replies also consume this command's read allowance.
            result = self.exchange(tool, arguments, deadline=deadline)
            if (result.get("query") != queries[0] or type(result.get("page")) is not int
                    or result["page"] != 1 or type(result.get("requested_size")) is not int
                    or result["requested_size"] != size
                    or result.get("semantics") not in {"bounded_relevance_ranked", "bounded_personalized"}
                    or (result.get("semantics") == "bounded_personalized"
                        and result.get("sort_label") != "Anbefalt for deg")
                    or result.get("authenticated") is not True or result.get("ready") is not True
                    or type(result.get("heading_count")) is not int or result["heading_count"] != 1
                    or not isinstance(result.get("products"), list) or len(result["products"]) > size):
                raise HouseholdError("native product search scope or rendered results changed")
            for product in result["products"]:
                path = product.get("product_id") if isinstance(product, dict) else None
                if (not isinstance(path, str)
                        or re.fullmatch(r"/varer/[A-Za-z0-9._~%/-]+-[0-9]{4,14}", path) is None):
                    raise HouseholdError("native search product requires an exact public product path")
                try:
                    dots.public_path(path)
                except ValueError as exc:
                    raise HouseholdError(str(exc)) from exc
            normalized = normalize_meny_product_search(result)
            normalized["scope"]["requested_size"] = size
            normalized["scope"]["semantics"] = result["semantics"]
            if result["semantics"] == "bounded_personalized":
                normalized["scope"]["sort_label"] = result["sort_label"]
            return normalized
        if tool == "get_cart":
            self.last_cart = normalize_cart_snapshot(self.exchange("get_cart", {}, deadline=deadline))
            return self.last_cart
        if tool == "manipulate_cart":
            operations = arguments.get("operations")
            if (set(arguments) != {"operations"} or not isinstance(operations, list)
                    or not 1 <= len(operations) <= 2):
                raise HouseholdError("native cart dispatch supports only one bounded core batch")
            for item in operations:
                dots.object_fields(item, {"productId", "quantity"}, {"productId", "quantity"})
                if not isinstance(item["productId"], str):
                    raise HouseholdError("native cart product must be an exact path")
                dots.public_path(item["productId"])
                if (type(item["quantity"]) is not int or not 1 <= abs(item["quantity"]) <= 2
                        or re.fullmatch(r"/varer/[A-Za-z0-9._~%/-]+-[0-9]{4,14}", item["productId"]) is None):
                    raise HouseholdError("native cart batch requires exact paths and bounded deltas")
            if sum(abs(item["quantity"]) for item in operations) > 2:
                raise HouseholdError("native cart batch exceeds the existing two-click limit")
            managed = self.managed_intent(operations)
            result = self.exchange(tool, arguments, deadline=deadline, write=True)
            if set(result) != {"dispatched"} or result["dispatched"] is not True:
                raise HouseholdError("native cart dispatch ending is unknown; reconcile without resend")
            if not managed:
                return result
            cart = self.call("get_cart", {}, deadline=deadline)
            live = self.app._cart_lines(cart_summary(cart))[0]
            with self.store.locked() as state:
                pending = state["pending_cart_change"]
                if live != pending["expected"]:
                    raise HouseholdError("managed native batch readback changed; reconcile without resend")
                pending["native_managed"]["verified"] = live
            return cart
        raise HouseholdError("native foreground provider operation is not supported")


def regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("original core files must be owned regular files")
    finally:
        os.close(fd)


def native_ownership(stack, root, config, *, new):
    binding = config.get("browser_binding")
    if binding is None:
        return
    # One persistent journal owner per native target, shared across core roots.
    legacy = Path.home() / ".meal-concierge-dots-targets"
    registry = Path(config.get("target_registry", legacy))
    if new and registry != legacy and os.path.lexists(legacy):
        raise HouseholdError("original HOME target registry exists; do not select another ownership namespace")
    if new and not os.path.lexists(registry):
        registry.mkdir(mode=0o700)
        on_demand._sync_directory(registry.parent)
    registry = dots.existing_root(registry)
    identities = ({"browser_id": binding["browser_id"], "tab_id": binding["tab_id"]},
                  {"origin": binding["origin"], "account_sha256": binding["account_sha256"],
                   "cart_context_sha256": binding["cart_context_sha256"]})
    owner = {"root": str(root), "config_sha256": dots.digest(dots.encoded(config))}
    targets = sorted(dots.digest(dots.encoded(identity)) for identity in identities)
    for target in targets:
        lock = registry / (target + ".lock")
        if not new:
            on_demand._read_file(lock, 0)
        stack.enter_context(file_lock(lock))
    for target in targets:
        path = registry / (target + ".json")
        if not (new and not os.path.lexists(path)) and on_demand._read_file(path, MAX_LINE) != dots.encoded(owner):
            raise HouseholdError("native browser/cart target belongs to another original core household")
    for target in targets:
        path = registry / (target + ".json")
        if new and not os.path.lexists(path):
            dots.exclusive_json(path, owner, MAX_LINE)


def application(root, config, reader, command_id, deadline, *, new=False, request=None):
    state = root / "state"
    if not new:
        dots.existing_root(state)
        regular(state / "state.json")
        regular(state / "recipes.sqlite3")
        for name in OWNER_FILES:
            on_demand._read_file(state / name, 0)
    stack = ExitStack()
    try:
        native_ownership(stack, root, config, new=new)
        for name in OWNER_FILES:
            stack.enter_context(file_lock(state / name))
        store = StateStore(state, {"household": config["household"], "provider": "meny"})
        if new:
            with store.locked() as saved:
                saved["native_config_sha256"] = dots.digest(dots.encoded(config))
        host = NativeHost(store, config, reader, command_id, deadline, request)
        app = Application(store, host, None, external_recipe_sources={},
                          products_apply_budget_seconds=PRODUCTS_APPLY_BUDGET_SECONDS)
        host.app = app
        if new:
            app.recipes.search(limit=1)  # The core opens SQLite lazily; create the original bank once.
        return stack, app
    except BaseException:
        stack.close()
        raise


def init(root, value, reader):
    config = configuration(value)
    root = Path(root).absolute()
    if root.parent.resolve(strict=True) != root.parent:
        raise ValueError("core root parent must be canonical")
    if config.get("target_registry"):
        registry = Path(config["target_registry"])
        if registry == root or root in registry.parents:
            raise ValueError("target_registry must be shared outside the core root")
        legacy = Path.home() / ".meal-concierge-dots-targets"
        if registry != legacy and os.path.lexists(legacy):
            raise HouseholdError("original HOME target registry exists; do not select another ownership namespace")
    root.mkdir(mode=0o700)
    on_demand._sync_directory(root.parent)
    with file_lock(root / "command.lock"):
        dots.exclusive_json(root / "config.json", config, MAX_LINE)
        (root / "calls").mkdir(mode=0o700)
        on_demand._sync_directory(root)
        stack, app = application(root, config, reader, "init", time.monotonic() + 60, new=True)
        with stack:
            return {"health": app.handle({"operation": "health"}),
                    "setup": app.handle({"operation": "setup", "action": "show"})}


def command(root, value, reader):
    dots.object_fields(value, {"request_id", "request"}, {"request_id", "request"})
    command_id = value["request_id"]
    if not isinstance(command_id, str) or str(uuid.UUID(command_id)) != command_id:
        raise ValueError("request_id must be a canonical UUID")
    request = value["request"]
    if not isinstance(request, dict) or (request.get("operation") not in CORE_OPERATIONS | {"native_cart_policy"}
            and not (request.get("operation") == "products" and request.get("action", "prepare") in {"prepare", "get", "apply"})
            and not (request.get("operation") == "delivery" and request.get("action", "list") == "list")
            and not (request.get("operation") == "orders" and request.get("action", "list") in {"list", "get"})
            and not (request.get("operation") == "cart" and request.get("action", "get") in {"get", "ensure", "clear", "reconcile_change", "reconcile"})):
        raise ValueError("unsupported foreground core operation; no checkout, delivery selection or order edits")
    if "_restore_missing_cart_digest" in request:
        raise ValueError("_restore_missing_cart_digest is internal-only")
    if request.get("operation") == "delivery":
        dots.object_fields(request, {"operation", "action", "dates", "response_view", "view_offset",
                                    "view_limit", "view_section"}, {"operation"})
    if request.get("operation") == "orders":
        action = request.get("action", "list")
        fields = {"operation", "action", "response_view", "view_offset", "view_limit", "view_section"}
        dots.object_fields(request, fields | ({"limit"} if action == "list" else {"order_id"}),
                           {"operation"} | ({"order_id"} if action == "get" else set()))
        if action == "list":
            limit = request.get("limit", 5)
            if type(limit) is not int or not 1 <= limit <= 5:
                raise ValueError("native orders list limit must be one to five")
            request = {**request, "limit": limit}
        elif (not isinstance(request["order_id"], str)
              or re.fullmatch(r"[0-9]{1,20}", request["order_id"]) is None):
            raise ValueError("native orders get requires an exact decimal identity")
    if request.get("operation") == "products" and request.get("action") == "apply" and (
            request.get("partial_product_plan_digest") or request.get("partial_apply") or request.get("product_plan")):
        raise ValueError("native managed apply requires a complete reviewed product_plan_ref/digest")
    if request.get("operation") == "cart" and request.get("action") == "reconcile" and (
            request.get("decision") != "keep_current" or request.get("exclude_product_ids")):
        raise ValueError("native cart decisions support keep_current without quantity changes; then prepare/apply")
    restore = request.get("restore_missing", False)
    if (type(restore) is not bool or (restore and (
            request.get("operation") != "products" or request.get("action") != "apply"
            or not isinstance(request.get("cart_digest"), str)
            or re.fullmatch(r"[a-f0-9]{64}", request["cart_digest"]) is None))
            or (not restore and request.get("operation") == "products" and request.get("cart_digest") is not None)):
        raise ValueError("restore_missing requires full products.apply and its exact current cart_digest")
    root = dots.existing_root(root)
    on_demand._read_file(root / "command.lock", 0)
    with file_lock(root / "command.lock"):
        config_data = on_demand._read_file(root / "config.json", MAX_LINE)
        config = configuration(on_demand._json(config_data))
        if request["operation"] == "native_cart_policy":
            validate_cart_policy(request, config)
        record = {"input": value, "config_sha256": dots.digest(config_data)}
        calls = dots.existing_root(root / "calls")
        target = calls / command_id
        if os.path.lexists(target):
            dots.existing_root(target)
            if on_demand._read_file(target / "intent.json", MAX_LINE) != dots.encoded(record):
                raise ValueError("original command input/configuration conflicts; no replay")
            if not os.path.lexists(target / "result.json"):
                return {"ok": False, "status": "incomplete", "message": "Original command has no ending; no replay."}
            return on_demand._json(on_demand._read_file(target / "result.json", on_demand.MAX_MENU))
        # Fail missing originals BEFORE publishing a new command intent.
        state = dots.existing_root(root / "state")
        regular(state / "state.json")
        regular(state / "recipes.sqlite3")
        saved = on_demand._json(on_demand._read_file(state / "state.json", on_demand.MAX_MENU))
        if saved.get("native_config_sha256") != dots.digest(config_data):
            raise ValueError("original native configuration binding changed; no replacement account/context")
        write = ((request.get("operation") == "cart" and request.get("action") in {"ensure", "clear"})
                 or (request.get("operation") == "products" and request.get("action") == "apply"))
        if write and not cart_writes_enabled(config, saved):
            raise ValueError("native cart writes are disabled; no intent or browser operation started")
        target.mkdir(mode=0o700)
        on_demand._sync_directory(calls)
        dots.exclusive_json(target / "intent.json", record, MAX_LINE)
        try:
            budget = (PRODUCTS_APPLY_BUDGET_SECONDS
                      if request.get("operation") == "products" and request.get("action") == "apply" else 240)
            stack, app = application(root, config, reader, command_id, time.monotonic() + budget, request=request)
            with stack:
                result = (cart_policy(app.store, config, request) if request["operation"] == "native_cart_policy"
                          else app.handle(request))
                with app.store.locked() as current:
                    pending = current.get("pending_cart_change")
                    if (pending and pending.get("native_managed")
                            and pending["native_managed"]["command_id"] == command_id
                            and request.get("operation") == "products" and request.get("action") == "apply"
                            and app._native_managed_finalized(current, pending)):
                        current.pop("pending_cart_change")
                output = {"ok": True, "result": result}
        except (HouseholdError, OSError, ValueError, TypeError, KeyError, RuntimeError, UnicodeError) as exc:
            output = {"ok": False, "error": str(exc)}
        dots.exclusive_json(target / "result.json", output, on_demand.MAX_MENU)
        return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "call"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    with input_mode():
        emit({"kind": "core_ready", "input_max_bytes": MAX_LINE})
        return execute(args, Input())


def execute(args, reader):
    try:
        value = reader.line(time.monotonic() + 60)
        result = ({"ok": True, "result": init(args.root, value, reader)} if args.action == "init"
                  else command(args.root, value, reader))
        emit({"kind": "core_result", **result})
        return 0 if result["ok"] else 1
    except (HouseholdError, OSError, ValueError, TypeError, KeyError, RuntimeError, UnicodeError) as exc:
        emit({"kind": "core_result", "ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
