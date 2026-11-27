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
from urllib.parse import parse_qsl, urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clients import dots
from core import HouseholdError, STATE_VERSION, StateStore, cart_summary
from meny import normalize_cart_snapshot, normalize_meny_delivery_slot, meny_order_card_status
from product_observations import normalize_meny_product_search
from retailer_recipes import meny_recipe_path
import on_demand
from runtime_ownership import file_lock
from service import Application

CORE_OPERATIONS = {"health", "setup", "profile", "recipes", "menu", "feedback",
                   "pantry", "recurring", "product_favorites", "shopping_list"}
OWNER_FILES = (".service-owner.lock", "recipes.sqlite3.owner.lock", "state.json.owner.lock")
LEGACY_RESET_INTENT = "native-cart-legacy-reset-intent.json"
MAX_LINE = 65536
PRODUCTS_APPLY_BUDGET_SECONDS = 600
RENDERED_UI_EVIDENCE = {"evidence_kind": "host_attested_rendered_ui", "backend_freshness": "unverified"}
ORDER_COLUMNS = ["BESTILLINGSKODE", "STATUS", "UTLEVERING", "TID", "SUM"]
ARCHIVED_DETAIL_LAYOUT = {"source_archived": True, "order_number_label": "Ordrenummer",
                          "heading_matches_pickup_code": True, "item_table_class": "ws-order-details-cart",
                          "item_disclosure_expanded": True}


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
    layout_fields = {"table_class"} if order_id is None else ARCHIVED_DETAIL_LAYOUT.keys()
    dots.object_fields(result, common | fields | layout_fields, common | fields)
    if (result["authenticated"] is not True or result["ready"] is not True
            or any(type(result[key]) is not int or result[key] != 1
                   for key in ("authenticated_count", "main_count", "heading_count"))):
        raise HouseholdError("native rendered order scope is unavailable")
    evidence = {**RENDERED_UI_EVIDENCE, **order_ui_location(result["source_origin"], result["source_path"],
                result["source_query_keys"], result["source_hash"], order_id)}
    if order_id is None:
        count, rows = result["rendered_row_count"], result["orders"]
        columns = result["columns"]
        receipt_column = columns == ORDER_COLUMNS + [""]
        if (result["heading"] != "Bestillinger fra de siste 6 måneder"
                or (receipt_column and result.get("table_class") != "ws-order-history-table")
                or (not receipt_column and columns != ORDER_COLUMNS)
                or type(result["table_count"]) is not int or result["table_count"] != 1
                or type(result["pagination_count"]) is not int or result["pagination_count"] != 0
                or result["rows_complete"] is not True or type(count) is not int or not 1 <= count <= 10000
                or not isinstance(rows, list) or len(rows) != min(limit, count)):
            raise HouseholdError("native rendered history window is unavailable; empty history is not verified")
        orders, seen = [], set()
        for row in rows:
            keys = {"order_number", "cell_count", "links", "status_marker", "delivery_display", "time_display", "sum_display"}
            if receipt_column:
                keys |= {"receipt_cell_class", "receipt_label", "same_archived_url"}
            dots.object_fields(row, keys, keys)
            identity = row["order_number"]
            if (not isinstance(identity, str) or re.fullmatch(r"[0-9]{1,20}", identity) is None
                    or identity in seen or type(row["cell_count"]) is not int or row["cell_count"] != len(columns)
                    or not isinstance(row["links"], list) or not 1 <= len(row["links"]) <= 2):
                raise HouseholdError("native rendered order identities changed")
            if receipt_column and (len(row["links"]) != 2 or row["receipt_cell_class"] != "ws-order-history-table__receipt"
                                   or row["receipt_label"] != "Se kvittering" or row["same_archived_url"] is not True):
                raise HouseholdError("native rendered order receipt link changed")
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
    archived_layout = bool(result.keys() & ARCHIVED_DETAIL_LAYOUT.keys())
    if archived_layout and any(type(result.get(key)) is not type(value) or result.get(key) != value
                               for key, value in ARCHIVED_DETAIL_LAYOUT.items()):
        raise HouseholdError("native archived receipt fields or item disclosure changed")
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
            "receipt_layout": "archived" if archived_layout else "unclassified",
            "status": meny_order_card_status(order_ui_text(markers[0])) if markers else "unknown",
            "amount_displays": {key: order_ui_text(value) for key, value in amounts.items()},
            "order_total": None, "payment_status": "unknown", "productQuantityCount": count, "products": items}


def normalize_recipe_search_ui(result, query, limit, observed_at):
    fields = {"source_url", "query", "authenticated", "ready", "heading_count", "displayed_count", "empty", "recipes"}
    dots.object_fields(result, fields, fields)
    url = urlsplit(result["source_url"]) if isinstance(result["source_url"], str) else None
    recipes, count = result["recipes"], result["displayed_count"]
    if (url is None or (url.scheme, url.netloc, url.path, url.fragment) != ("https", "meny.no", "/sok", "")
            or sorted(parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2))
                != [("expanded", "recipes"), ("query", query)]
            or result["query"] != query or result["authenticated"] is not True or result["ready"] is not True
            or type(result["heading_count"]) is not int or result["heading_count"] != 1
            or type(count) is not int or not 0 <= count <= 10000 or result["empty"] is not (count == 0)
            or not isinstance(recipes, list) or len(recipes) != min(count, limit)):
        raise HouseholdError("native recipe search scope or rendered results changed")
    normalized, seen = [], set()
    for recipe in recipes:
        dots.object_fields(recipe, {"recipe_id", "name", "summary"}, {"recipe_id", "name"})
        path = meny_recipe_path(recipe["recipe_id"])
        name, summary = recipe["name"], recipe.get("summary")
        if (path.endswith("/") or path in seen or not isinstance(name, str) or not 1 <= len(name.strip()) <= 250
                or (summary is not None and (not isinstance(summary, str) or len(summary) > 500))):
            raise HouseholdError("native recipe search requires distinct exact retailer recipe links and names")
        dots.public_path(path)
        seen.add(path)
        normalized.append({**recipe, "name": name.strip(), "recipe_url": "https://meny.no" + path})
    return {"provider": "meny", "query": query, "recipes": normalized, "observed_at": observed_at.isoformat(),
            "scope": {"source_url": result["source_url"], "requested_size": limit, **RENDERED_UI_EVIDENCE}}


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


def native_frame(command_id, operation, arguments, binding, deadline, *, write=False):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=max(0, deadline - time.monotonic()))
    return {"kind": "native_host_request", "command_id": command_id, "call_id": str(uuid.uuid4()),
            "provider": "meny", "operation": operation, "arguments": arguments,
            "browser_binding": binding, "emitted_at": now.isoformat(), "expires_at": expires.isoformat(),
            "effect": "cart_write" if write else "read"}


def native_reply(reader, frame, deadline):
    emit(frame)
    reply = reader.line(deadline)
    dots.object_fields(reply, {"reply_to", "browser_binding", "observed_at", "result", "error"},
                       {"reply_to", "browser_binding", "observed_at"})
    observed = dots.timestamp(reply["observed_at"])
    if (reply["reply_to"] != frame["call_id"] or reply["browser_binding"] != frame["browser_binding"]
            or not dots.timestamp(frame["emitted_at"]) <= observed <= datetime.now(timezone.utc)
            < dots.timestamp(frame["expires_at"])):
        raise HouseholdError("native host reply identity or lifetime changed")
    if ("result" in reply) == ("error" in reply):
        raise HouseholdError("native host must return exactly one result or error")
    if "error" in reply:
        raise HouseholdError("native host operation failed; reconcile any pending write without resend")
    if not isinstance(reply["result"], dict):
        raise HouseholdError("native host result must be an object")
    return reply["result"], observed


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


def cart_dispatch_digest(config_digest, pending):
    # The ending and readback can be saved later without changing the original batch.
    batch = {key: value for key, value in pending.items() if key not in {"dispatch_finished", "native_managed"}}
    if "native_managed" in pending:
        batch["native_managed"] = {key: value for key, value in pending["native_managed"].items()
                                   if key != "verified"}
    return dots.digest(dots.encoded({"config_sha256": config_digest, "batch": batch}))


def prepare_cart_dispatch(state, config_digest, pending, frame):
    directory = state / "native-cart-dispatch"
    if not os.path.lexists(directory):
        directory.mkdir(mode=0o700)
        on_demand._sync_directory(state)
    dots.existing_root(directory)
    identity = cart_dispatch_digest(config_digest, pending)
    attempt = directory / identity
    attempt.mkdir(mode=0o700)
    on_demand._sync_directory(directory)
    dots.exclusive_json(attempt / "frame.json", {
        "batch_sha256": identity, **{key: frame[key] for key in ("command_id", "call_id", "emitted_at", "expires_at")}
    }, MAX_LINE)
    return attempt


def cart_dispatch_status(state, config_digest, pending):
    directory = state / "native-cart-dispatch"
    identity = cart_dispatch_digest(config_digest, pending)
    attempt = directory / identity
    if not os.path.lexists(directory):
        return "no_frame_record"
    dots.existing_root(directory)
    if not os.path.lexists(attempt):
        return "no_frame_record"
    dots.existing_root(attempt)
    frame = attempt / "frame.json"
    if not os.path.lexists(frame):
        return "no_frame_record"
    frame = on_demand._json(on_demand._read_file(frame, MAX_LINE))
    fields = {"batch_sha256", "command_id", "call_id", "emitted_at", "expires_at"}
    dots.object_fields(frame, fields, fields)
    if (frame["batch_sha256"] != identity
            or any(not isinstance(frame[key], str) or str(uuid.UUID(frame[key])) != frame[key]
                   for key in ("command_id", "call_id"))
            or not dots.timestamp(frame["emitted_at"]) < dots.timestamp(frame["expires_at"])):
        raise HouseholdError("original native cart frame record changed")
    ending = attempt / "ack.json"
    if not os.path.lexists(ending):
        return "frame_prepared"
    ending = on_demand._json(on_demand._read_file(ending, MAX_LINE))
    fields = {"command_id", "call_id", "observed_at"}
    dots.object_fields(ending, fields, fields)
    if (any(ending[key] != frame[key] for key in ("command_id", "call_id"))
            or not dots.timestamp(frame["emitted_at"]) <= dots.timestamp(ending["observed_at"]) < dots.timestamp(frame["expires_at"])):
        raise HouseholdError("original native cart acknowledgement record changed")
    return "host_acknowledged"


class NativeHost:
    owns_managed_cart_journal = True

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
        return self.app._native_managed_intent(self.command_id, self.request, before, operations)

    def pending_binding(self):
        state = self.store.read()
        if state.get("native_config_sha256") != dots.digest(dots.encoded(self.config)):
            raise HouseholdError("original native household/browser configuration changed")
        return state.get("pending_cart_change")

    def exchange(self, operation, arguments, *, deadline=None, write=False, observation_time=False):
        binding = self.config.get("browser_binding")
        if binding is None:
            raise HouseholdError("native cloud browser/account/cart binding is required; no browser started")
        self.pending_binding()
        self.calls += 1
        if self.calls > 64:
            raise HouseholdError("native host command exceeded its call limit")
        reply_seconds = 120 if operation in {"get_delivery_slots", "product_search", "recipe_search"} and not write else 60
        deadline = min(self.deadline, deadline or self.deadline, time.monotonic() + reply_seconds)
        if deadline <= time.monotonic():
            raise HouseholdError("native host operation deadline reached")
        frame = native_frame(self.command_id, operation, arguments, binding, deadline, write=write)
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
                attempt = prepare_cart_dispatch(self.store.directory, state["native_config_sha256"], pending, frame)
        result, observed = native_reply(self.reader, frame, deadline)  # Original write intent precedes emission.
        if write:
            if set(result) != {"dispatched"} or result["dispatched"] is not True:
                raise HouseholdError("native cart dispatch ending is unknown; reconcile without resend")
            dots.exclusive_json(attempt / "ack.json", {
                "command_id": self.command_id, "call_id": frame["call_id"], "observed_at": observed.isoformat(),
            }, MAX_LINE)
        return (result, observed) if observation_time else result

    def probe(self, **kwargs):
        self.verify_order_change(None, None, deadline=kwargs.get("deadline"))
        return {"status": "ready", "provider": "meny", "protocol_version": "native-host-stdio-v1",
                "server": {"name": "host-attested native cloud MENY"}, "tool_count": 8}

    def verify_order_change(self, order_id, code, *, deadline=None):
        if order_id is not None or code is not None:
            raise HouseholdError("native foreground adapter does not support existing-order edits")
        result = self.exchange("verify_new_cart", {}, deadline=deadline)
        if set(result) != {"authenticated", "new_cart"} or any(result[key] is not True for key in result):
            raise HouseholdError("native account or new-cart mode could not be verified")
        return result

    def call(self, tool, arguments, *, deadline=None, **kwargs):
        if tool == "recipe_search":
            if (self.request.get("operation"), self.request.get("action")) != ("catalog", "recipes"):
                raise HouseholdError("use catalog.recipes for native rendered recipe links")
            dots.object_fields(arguments, {"query", "page", "size"}, {"query", "page", "size"})
            query, limit = arguments["query"], arguments["size"]
            if (not isinstance(query, str) or not 1 <= len(query) <= 200 or query != " ".join(query.split())
                    or type(arguments["page"]) is not int or arguments["page"] != 1
                    or type(limit) is not int or not 1 <= limit <= 5):
                raise HouseholdError("native recipe search requires one bounded first-page query")
            result, observed = self.exchange(tool, arguments, deadline=deadline, observation_time=True)
            return normalize_recipe_search_ui(result, query, limit, observed)
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
            result, observed = self.exchange(tool, arguments, deadline=deadline, observation_time=True)
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
            normalized = normalize_meny_product_search(result, observed_at=observed.isoformat())
            normalized["scope"]["requested_size"] = size
            normalized["scope"]["semantics"] = result["semantics"]
            if result["semantics"] == "bounded_personalized":
                normalized["scope"]["sort_label"] = result["sort_label"]
            if self.request.get("operation") == "catalog":
                normalized["scope"].update(RENDERED_UI_EVIDENCE)
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
            if not managed:
                return result
            with self.store.locked() as state:
                pending = state.get("pending_cart_change") or {}
                if ((pending.get("native_managed") or {}).get("command_id") != self.command_id
                        or pending.get("operations") != operations):
                    raise HouseholdError("original native cart batch changed after acknowledgement")
                pending["dispatch_finished"] = True
            cart = self.call("get_cart", {}, deadline=deadline)
            live = self.app._cart_lines(cart_summary(cart))[0]
            self.app._verify_native_managed_batch(self.command_id, live)
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


def tab_target(binding, tab_id):
    return dots.digest(dots.encoded({"browser_id": binding["browser_id"], "tab_id": tab_id}))


def target_owner(root, config):
    return {"root": str(root), "config_sha256": dots.digest(dots.encoded(config))}


def publish_reset_record(path, record):
    # The final immutable name must never expose a partially written record.
    if not os.path.lexists(path):
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            dots.exclusive_json(temp, record, MAX_LINE)
            os.link(temp, path, follow_symlinks=False)
        finally:
            temp.unlink(missing_ok=True)
    if on_demand._read_file(path, MAX_LINE) != dots.encoded(record):
        raise HouseholdError("legacy reset reservation conflicts with the original owner or request")
    on_demand._sync_directory(path.parent)


def native_ownership(stack, root, config, *, new, active_tab=None, observation_tab=None, reserving=False):
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
    account = {key: binding[key] for key in ("origin", "account_sha256", "cart_context_sha256")}
    owner = target_owner(root, config)
    targets = {tab_target(binding, binding["tab_id"]), dots.digest(dots.encoded(account))}
    if active_tab is not None:
        targets.add(tab_target(binding, active_tab))
    observation = tab_target(binding, observation_tab) if observation_tab is not None else None
    for target in sorted(targets | ({observation} if observation else set())):
        lock = registry / (target + ".lock")
        if not new and target in targets:
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
    if observation:
        path = registry / (observation + ".json")
        if os.path.lexists(path) and (not reserving or on_demand._read_file(path, MAX_LINE) != dots.encoded(owner)):
            raise HouseholdError("the observation tab already belongs to an original core household")
    return registry


def lock_core_files(stack, state, *, new=False):
    for name in OWNER_FILES:
        if not new:
            on_demand._read_file(state / name, 0)
        stack.enter_context(file_lock(state / name))


def application(root, config, reader, command_id, deadline, *, new=False, request=None, active_config=None):
    state = root / "state"
    if not new:
        dots.existing_root(state)
        regular(state / "state.json")
        regular(state / "recipes.sqlite3")
    stack = ExitStack()
    try:
        active = config if active_config is None else active_config
        active_tab = active["browser_binding"]["tab_id"] if active != config else None
        native_ownership(stack, root, config, new=new, active_tab=active_tab)
        lock_core_files(stack, state, new=new)
        store = StateStore(state, {"household": config["household"], "provider": "meny"})
        if new:
            with store.locked() as saved:
                saved["native_config_sha256"] = dots.digest(dots.encoded(config))
        host = NativeHost(store, active, reader, command_id, deadline, request)
        app = Application(store, host, None,
                          products_apply_budget_seconds=PRODUCTS_APPLY_BUDGET_SECONDS)
        # Native writes require their original acknowledgement and bound readback.
        app.allow_observed_cart_recovery = False
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


def observation_tab_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 150 or not value.isprintable() or value != value.strip():
        raise ValueError("a distinct native cloud observation tab is required")
    return value


def with_tab(config, tab_id):
    return {**config, "browser_binding": {**config["browser_binding"], "tab_id": tab_id}}


def legacy_reset_intent(tab_id, digest):
    return {"tab_id": tab_id, "legacy_reset_digest": digest, "accept_unknown_dispatch": True,
            "original_executor_retired": True}


def load_bound_state(root, config_data, config):
    state = dots.existing_root(root / "state")
    regular(state / "state.json")
    regular(state / "recipes.sqlite3")
    saved = on_demand._json(on_demand._read_file(state / "state.json", on_demand.MAX_MENU))
    active = config
    expected = dots.digest(config_data)
    if "native_cart_legacy_reset" in saved:
        reset = saved["native_cart_legacy_reset"]
        fields = {"base_config_sha256", "original_pending_sha256", "pending_cart_change", "replacement_tab",
                  "historical_dispatch", "observed_at"}
        dots.object_fields(reset, fields, fields)
        tab_id = observation_tab_id(reset["replacement_tab"])
        binding = config.get("browser_binding")
        if (binding is None or tab_id == binding["tab_id"] or reset["base_config_sha256"] != expected
                or reset["historical_dispatch"] != "unknown"
                or type(saved.get("native_cart_writes_enabled")) is not bool
                or reset["original_pending_sha256"] != dots.digest(dots.encoded(reset["pending_cart_change"]))):
            raise ValueError("original native cart reset receipt changed")
        dots.timestamp(reset["observed_at"])
        active = with_tab(config, tab_id)
        # Old clients compare against the original file digest and fail closed.
        expected = dots.digest(dots.encoded(active))
        intent = legacy_reset_intent(tab_id, reset["original_pending_sha256"])
        if on_demand._read_file(state / LEGACY_RESET_INTENT, MAX_LINE) != dots.encoded(intent):
            raise ValueError("original native cart reset intent changed")
    if saved.get("native_config_sha256") != expected:
        raise ValueError("original native configuration binding changed; no replacement account/context")
    return state, saved, active


def inspect_cart(root):
    root = dots.existing_root(root)
    on_demand._read_file(root / "command.lock", 0)
    with file_lock(root / "command.lock"):
        data = on_demand._read_file(root / "config.json", MAX_LINE)
        config = configuration(on_demand._json(data))
        state, saved, active = load_bound_state(root, data, config)
        result = cart_journal_status(state, active, saved)
        candidate = legacy_reset_digest(state, config, saved)
        if candidate:
            result["legacy_reset_digest"] = candidate
        if "native_cart_legacy_reset" in saved:
            result["legacy_reset_status"] = "archived_unknown"
        return result


def cart_journal_status(state, config, saved):
    pending = saved.get("pending_cart_change")
    ending = cart_dispatch_status(state, saved["native_config_sha256"], pending) if pending else "none"
    return {"pending": bool(pending), "writes_enabled": cart_writes_enabled(config, saved),
            "dispatch_evidence": ending, "core_acknowledged": bool(pending and pending.get("dispatch_finished") is True)}


def legacy_reset_digest(state, config, saved):
    pending = saved.get("pending_cart_change")
    required = {"provider", "operation_id", "request_digest", "before", "expected", "order_change", "operations"}
    optional = {"cart_only", "clear_requested", "clear_id", "dispatch_finished"}
    if (config.get("browser_binding") is None or saved.get("version") != STATE_VERSION
            or saved.get("provider") != "meny" or saved.get("household") != config["household"]
            or "native_cart_legacy_reset" in saved or cart_writes_enabled(config, saved)
            or not isinstance(pending, dict) or not required <= pending.keys() <= required | optional
            or pending["provider"] != "meny" or pending["order_change"] is not None
            or pending.get("dispatch_finished", False) is not False
            or not isinstance(pending["operation_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", pending["operation_id"])
            or not isinstance(pending["request_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", pending["request_digest"])
            or not isinstance(pending["before"], dict) or not isinstance(pending["expected"], dict)
            or not isinstance(pending["operations"], list) or not 1 <= len(pending["operations"]) <= 2
            or pending["operation_id"] in (saved.get("cart_change_receipts") or {})
            or any(saved.get(key) for key in ("pending_checkout", "pending_cancellation", "order_change"))):
        return None
    dispatches = state / "native-cart-dispatch"
    if os.path.lexists(dispatches):
        dots.existing_root(dispatches)
        if os.path.lexists(dispatches / cart_dispatch_digest(saved["native_config_sha256"], pending)):
            return None  # Includes incomplete frame directories or orphaned acknowledgements.
    return dots.digest(dots.encoded(pending))


def observation_exchange(reader, command_id, target, operation, arguments):
    deadline = time.monotonic() + 120
    frame = native_frame(command_id, operation, arguments, target, deadline)
    frame["scope"] = "current_account_observation"
    frame["preconditions"] = ["same approved cloud browser on this observation tab",
                              "fresh matching account before and after the observation",
                              "fresh matching ordinary cart context without an order edit"]
    return native_reply(reader, frame, deadline)


def observation_reply(reader, command_id, target, identity, operation, arguments):
    reply, observed_at = observation_exchange(reader, command_id, target, operation, arguments)
    fields = {"verified_context", "observation"}
    dots.object_fields(reply, fields, fields)
    verified = reply["verified_context"]
    if (verified != {**identity, "account_verified_before_and_after": True}
            or verified["account_verified_before_and_after"] is not True):
        raise HouseholdError("replacement tab did not verify the original MENY account and ordinary cart")
    return reply["observation"], observed_at.isoformat()


def verify_observation(reader, binding, tab_id):
    target = {"origin": binding["origin"], "browser_id": binding["browser_id"], "tab_id": tab_id}
    identity = {key: binding[key] for key in ("account_sha256", "cart_context_sha256")}
    command_id = str(uuid.uuid4())
    proof, _ = observation_exchange(reader, command_id, target, "verify_observation_context", {})
    if proof != {**identity, "authenticated": True, "new_cart": True} or (
            proof["authenticated"] is not True or proof["new_cart"] is not True):
        raise HouseholdError("replacement tab did not verify the original MENY account and ordinary cart")

    def read(operation, arguments):
        return observation_reply(reader, command_id, target, identity, operation, arguments)

    return read


def read_observation_cart(read):
    observed, timestamp = read("get_cart", {})
    fields = {"source_url", "new_cart", "snapshot"}
    dots.object_fields(observed, fields, fields)
    if observed["source_url"] != "https://meny.no/varer" or observed["new_cart"] is not True:
        raise HouseholdError("replacement tab did not show the ordinary MENY cart at /varer")
    try:
        return normalize_cart_snapshot(observed["snapshot"]), timestamp
    except OverflowError:
        raise HouseholdError("rendered MENY cart numbers exceed the supported range") from None


def current_account_observation(root, value, reader):
    dots.object_fields(value, {"tab_id", "request"}, {"tab_id", "request"})
    tab_id, request = observation_tab_id(value["tab_id"]), value["request"]
    dots.object_fields(request, {"operation", "action", "limit", "include_first_receipt"}, {"operation", "action"})
    is_cart = request == {"operation": "cart", "action": "get"}
    limit, receipt = request.get("limit", 1), request.get("include_first_receipt", False)
    if not is_cart and ((request["operation"], request["action"]) != ("orders", "list")
                        or type(limit) is not int or not 1 <= limit <= 5 or type(receipt) is not bool):
        raise ValueError("observation supports only current cart.get or orders.list with optional first receipt")

    root = dots.existing_root(root)
    on_demand._read_file(root / "command.lock", 0)
    with ExitStack() as stack:
        stack.enter_context(file_lock(root / "command.lock"))
        data = on_demand._read_file(root / "config.json", MAX_LINE)
        config = configuration(on_demand._json(data))
        state, saved, active = load_bound_state(root, data, config)
        binding = active.get("browser_binding")
        if binding is None or tab_id == binding["tab_id"]:
            raise ValueError("use a separate native cloud tab for current-account observation")
        native_ownership(stack, root, config, new=False, active_tab=binding["tab_id"], observation_tab=tab_id)
        lock_core_files(stack, state)
        _, saved, locked_active = load_bound_state(root, data, config)
        if active != locked_active:
            raise HouseholdError("active MENY browser tab changed during observation setup")
        pending = saved.get("pending_cart_change")
        status = cart_journal_status(state, active, saved)
        if (not pending or pending.get("provider") != "meny" or pending.get("order_change") or status["writes_enabled"]
                or any(saved.get(key) for key in ("pending_checkout", "pending_cancellation", "order_change"))):
            raise HouseholdError("current-account observation requires a pending ordinary MENY cart change and disabled writes")
        read = verify_observation(reader, binding, tab_id)
        result = {"observation_scope": "current_account", **RENDERED_UI_EVIDENCE,
                  "pending_cart_guard": status}
        if is_cart:
            cart, timestamp = read_observation_cart(read)
            return {**result, "cart": cart_summary(cart), "observed_at": timestamp}
        observed, timestamp = read("get_orders", {"page": 1, "size": limit})
        orders = normalize_order_ui(observed, limit=limit)
        result.update(history=orders, observed_at=timestamp)
        if receipt:
            order_id = orders["orders"][0]["order_number"]
            observed, timestamp = read("get_order", {"order_number": order_id})
            detail = normalize_order_ui(observed, order_id=order_id)
            if detail["receipt_layout"] != "archived":
                raise HouseholdError("the first displayed MENY order has no verified archived receipt")
            result["first_receipt"] = {**detail, "observed_at": timestamp}
        return result


def reset_legacy_cart(root, value, reader):
    fields = {"tab_id", "legacy_reset_digest", "accept_unknown_dispatch", "original_executor_retired"}
    dots.object_fields(value, fields, fields)
    tab_id = observation_tab_id(value["tab_id"])
    digest = value["legacy_reset_digest"]
    if (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            or value["accept_unknown_dispatch"] is not True or value["original_executor_retired"] is not True):
        raise ValueError("reset requires the exact pending digest, owner acceptance of unknown dispatch and a retired executor")
    root = dots.existing_root(root)
    on_demand._read_file(root / "command.lock", 0)
    with ExitStack() as stack:
        stack.enter_context(file_lock(root / "command.lock"))
        dots.existing_root(root / "calls")
        data = on_demand._read_file(root / "config.json", MAX_LINE)
        config = configuration(on_demand._json(data))
        directory, saved, active = load_bound_state(root, data, config)
        original = config.get("browser_binding")
        if original is None or tab_id == original["tab_id"]:
            raise ValueError("reset needs a separate tab in the original MENY cloud browser")
        previous = saved.get("native_cart_legacy_reset")
        intent = directory / LEGACY_RESET_INTENT
        if os.path.lexists(intent) and on_demand._read_file(intent, MAX_LINE) != dots.encoded(value):
            raise ValueError("the legacy reset is already reserved for a different request")
        result = {"status": "archived_unknown", "historical_dispatch": "unknown", "writes_enabled_at_reset": False}
        if previous is not None:
            if (tab_id, digest) != (previous["replacement_tab"], previous["original_pending_sha256"]):
                raise ValueError("the original legacy cart reset already has a different receipt")
            native_ownership(stack, root, config, new=False, active_tab=active["browser_binding"]["tab_id"])
            return {**result, "observed_at": previous["observed_at"], "idempotent": True}
        if legacy_reset_digest(directory, config, saved) != digest:
            raise HouseholdError("original pending MENY cart is ineligible or its reset digest changed")
        registry = native_ownership(stack, root, config, new=False, observation_tab=tab_id, reserving=True)
        lock_core_files(stack, directory)
        store = StateStore(directory, {"household": config["household"], "provider": "meny"})
        with store.locked() as state:
            if (state.get("native_config_sha256") != dots.digest(data)
                    or legacy_reset_digest(directory, config, state) != digest):
                raise HouseholdError("original pending MENY cart changed before reset")
            read = verify_observation(reader, original, tab_id)
            cart, observed_at = read_observation_cart(read)
            if cart["count"] != 0 or cart["total"] != 0 or cart["delivery"] is not None:
                raise HouseholdError("legacy reset requires a freshly observed empty MENY cart without a delivery slot")
            publish_reset_record(intent, value)
            owner = registry / (tab_target(original, tab_id) + ".json")
            publish_reset_record(owner, target_owner(root, config))
            # Publish the archive, the new binding and the retired approvals together.
            state["native_cart_legacy_reset"] = {
                "base_config_sha256": dots.digest(data), "original_pending_sha256": digest,
                "pending_cart_change": state.pop("pending_cart_change"), "replacement_tab": tab_id,
                "historical_dispatch": "unknown", "observed_at": observed_at,
            }
            state["native_config_sha256"] = dots.digest(dots.encoded(with_tab(config, tab_id)))
            state["native_cart_writes_enabled"] = False
            state["product_selection_generation"] = uuid.uuid4().hex
            for key in ("cart_plan", "delivery_selection", "partial_product_validation", "product_plan_completion",
                        "managed_product_apply_fence"):
                state.pop(key, None)
            prepared = state["menu_planning"]["prepared"]
            for key in tuple(prepared):
                if isinstance(prepared[key], dict) and prepared[key].get("kind") == "product_plan":
                    del prepared[key]
        return {**result, "observed_at": observed_at, "idempotent": False}


def restore_cart_acknowledgement(app, request):
    with app.store.locked() as state:
        pending = state.get("pending_cart_change")
        if not pending or pending["provider"] != "meny":
            return request
        supplied = request.get("operation_id")
        if supplied not in (None, app._cart_operation_ref(pending)):
            original = {key: value for key, value in pending.items() if key != "dispatch_finished"}
            if supplied != app._cart_operation_ref(original):
                return request
        if cart_dispatch_status(app.store.directory, state["native_config_sha256"], pending) != "host_acknowledged":
            return request
        pending["dispatch_finished"] = True
        if supplied is not None:
            return {**request, "operation_id": app._cart_operation_ref(pending)}
    return request


def reconcile_cart_change(app, request):
    state = app.store.read()
    reset = state.get("native_cart_legacy_reset")
    pending = state.get("pending_cart_change")
    operation = request.get("operation_id")
    if reset and (operation == reset["pending_cart_change"]["operation_id"] or (operation is None and not pending)):
        return {"reconciled": False, "cart_write_pending": bool(pending), "status": "archived_unknown",
                "historical_dispatch": "unknown"}
    return app.handle(restore_cart_acknowledgement(app, request))


def command(root, value, reader):
    dots.object_fields(value, {"request_id", "request"}, {"request_id", "request"})
    command_id = value["request_id"]
    if not isinstance(command_id, str) or str(uuid.UUID(command_id)) != command_id:
        raise ValueError("request_id must be a canonical UUID")
    request = value["request"]
    if not isinstance(request, dict) or (request.get("operation") not in CORE_OPERATIONS | {"native_cart_policy"}
            and not (request.get("operation") == "catalog" and request.get("action") in {"products", "recipes"})
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
        base_record = {"input": value, "config_sha256": dots.digest(config_data)}
        calls = dots.existing_root(root / "calls")
        target = calls / command_id
        if os.path.lexists(target):
            dots.existing_root(target)
            intent = on_demand._read_file(target / "intent.json", MAX_LINE)
            if intent != dots.encoded(base_record):
                _, saved, _ = load_bound_state(root, config_data, config)
                active_record = {**base_record, "config_sha256": saved["native_config_sha256"]}
                if intent != dots.encoded(active_record):
                    raise ValueError("original command input/configuration conflicts; no replay")
            if not os.path.lexists(target / "result.json"):
                return {"ok": False, "status": "incomplete", "message": "Original command has no ending; no replay."}
            return on_demand._json(on_demand._read_file(target / "result.json", on_demand.MAX_MENU))
        # Fail missing originals BEFORE publishing a new command intent.
        _, saved, active = load_bound_state(root, config_data, config)
        if request["operation"] == "native_cart_policy":
            validate_cart_policy(request, active)
        apply = (request.get("operation"), request.get("action")) == ("products", "apply")
        if active != config and apply and not request.get("product_plan_ref"):
            raise ValueError("prepare and review a new saved product plan after resetting the legacy cart")
        write = apply or (request.get("operation") == "cart" and request.get("action") in {"ensure", "clear"})
        if write and not cart_writes_enabled(active, saved):
            raise ValueError("native cart writes are disabled; no intent or browser operation started")
        target.mkdir(mode=0o700)
        on_demand._sync_directory(calls)
        record = {**base_record, "config_sha256": saved["native_config_sha256"]}
        dots.exclusive_json(target / "intent.json", record, MAX_LINE)
        try:
            budget = PRODUCTS_APPLY_BUDGET_SECONDS if apply else 240
            stack, app = application(root, config, reader, command_id, time.monotonic() + budget,
                                     request=request, active_config=active)
            with stack:
                if request["operation"] == "cart" and request.get("action") == "reconcile_change":
                    result = reconcile_cart_change(app, request)
                elif request["operation"] == "native_cart_policy":
                    result = cart_policy(app.store, active, request)
                else:
                    result = app.handle(request)
                if (active != config and request["operation"] == "products"
                        and result.get("product_plan_ref") and result.get("apply_arguments")):
                    result["apply_arguments"] = {"action": "apply", "product_plan_ref": result["product_plan_ref"],
                                                 "product_plan_digest": result["apply_arguments"]["product_plan_digest"]}
                if apply:
                    app._finalize_native_managed_apply(command_id)
                output = {"ok": True, "result": result}
        except (HouseholdError, OSError, ValueError, TypeError, KeyError, RuntimeError, UnicodeError) as exc:
            output = {"ok": False, "error": str(exc)}
        dots.exclusive_json(target / "result.json", output, on_demand.MAX_MENU)
        return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "call", "inspect-cart", "observe", "reset-legacy-cart"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "inspect-cart":
        return execute(args, None)
    with input_mode():
        emit({"kind": "core_ready", "input_max_bytes": MAX_LINE})
        return execute(args, Input())


def execute(args, reader):
    try:
        if args.action == "inspect-cart":
            result = {"ok": True, "result": inspect_cart(args.root)}
        else:
            value = reader.line(time.monotonic() + 60)
            if args.action == "init":
                result = {"ok": True, "result": init(args.root, value, reader)}
            elif args.action == "observe":
                result = {"ok": True, "result": current_account_observation(args.root, value, reader)}
            elif args.action == "reset-legacy-cart":
                result = {"ok": True, "result": reset_legacy_cart(args.root, value, reader)}
            else:
                result = command(args.root, value, reader)
        emit({"kind": "core_result", **result})
        return 0 if result["ok"] else 1
    except (HouseholdError, OSError, ValueError, TypeError, KeyError, RuntimeError, UnicodeError) as exc:
        emit({"kind": "core_result", "ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
