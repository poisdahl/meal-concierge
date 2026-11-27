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
from clients import dots, dots_meny_delivery, dots_meny_edit, dots_meny_handoff
from core import HouseholdError, STATE_VERSION, StateStore, _flush_drive_cache, cart_summary
from menu_planning import digest as planning_digest
from meny import (MenyCartStoppedError, meny_order_card_status, normalize_cart_snapshot,
                  normalize_meny_delivery_slot, normalize_product_ref)
from product_observations import normalize_meny_product_search
from retailer_recipes import meny_recipe_path
import on_demand
from runtime_ownership import file_lock
from service import Application

CORE_OPERATIONS = {"health", "setup", "profile", "recipes", "menu", "feedback",
                   "pantry", "recurring", "product_favorites", "shopping_list"}
OWNER_FILES = (".service-owner.lock", "recipes.sqlite3.owner.lock", "state.json.owner.lock")
LEGACY_RESET_INTENT = "native-cart-legacy-reset-intent.json"
TAB_REBIND = "native_tab_rebind"
TAB_HISTORY = "native_tab_rebind_history"
TAB_PLANS_RETIRED = "native_tab_product_plans_retired"
MAX_LINE = 65536
PRODUCTS_APPLY_BUDGET_SECONDS = 600
MENY_PRODUCT_PATH = re.compile(r"/varer/[A-Za-z0-9._~%/-]+-[0-9]{4,14}")
PRE_CLICK_STOP = {"dispatched": False, "stopped_before_first_click": True, "no_pending_clicks": True}
HOST_FAILURE = "native host operation failed; reconcile any pending write without resend"
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
        raise HouseholdError(HOST_FAILURE)
    if not isinstance(reply["result"], dict):
        raise HouseholdError("native host result must be an object")
    return reply["result"], observed


def delivery_picker(result, requested_date=None, *, recovery_read=False):
    fields = {"authenticated", "ready", "source_url", "dialog_count", "slots"}
    if recovery_read and result.get("no_selectable_windows") is True and result.get("slots") == []:
        fields.add("no_selectable_windows")
    if (set(result) != fields
            or result["authenticated"] is not True or result["ready"] is not True
            or result["source_url"] != "https://meny.no/varer"
            or type(result["dialog_count"]) is not int or result["dialog_count"] != 1
            or not isinstance(result["slots"], list) or not result["slots"] and "no_selectable_windows" not in fields):
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


def cart_dispatch_frame(state, config_digest, pending):
    directory = state / "native-cart-dispatch"
    identity = cart_dispatch_digest(config_digest, pending)
    attempt = directory / identity
    if not os.path.lexists(directory):
        return attempt, None
    dots.existing_root(directory)
    if not os.path.lexists(attempt):
        return attempt, None
    dots.existing_root(attempt)
    frame = attempt / "frame.json"
    if not os.path.lexists(frame):
        return attempt, None
    frame = on_demand._json(on_demand._read_file(frame, MAX_LINE))
    fields = {"batch_sha256", "command_id", "call_id", "emitted_at", "expires_at"}
    dots.object_fields(frame, fields, fields)
    if (frame["batch_sha256"] != identity
            or any(not isinstance(frame[key], str) or str(uuid.UUID(frame[key])) != frame[key]
                   for key in ("command_id", "call_id"))
            or not dots.timestamp(frame["emitted_at"]) < dots.timestamp(frame["expires_at"])):
        raise HouseholdError("original native cart frame record changed")
    return attempt, frame


def cart_dispatch_status(state, config_digest, pending, *, sync_ending=False):
    attempt, frame = cart_dispatch_frame(state, config_digest, pending)
    if frame is None:
        return "no_frame_record"
    ending_path = attempt / "ack.json"
    if not os.path.lexists(ending_path):
        return "frame_prepared"
    ending = on_demand._json(on_demand._read_file(ending_path, MAX_LINE))
    fields = {"command_id", "call_id", "observed_at"}
    dots.object_fields(ending, fields | {"outcome"}, fields)
    if (any(ending[key] != frame[key] for key in ("command_id", "call_id"))
            or not dots.timestamp(frame["emitted_at"]) <= dots.timestamp(ending["observed_at"]) < dots.timestamp(frame["expires_at"])
            or ("outcome" in ending and ending["outcome"] != "not_dispatched")):
        raise HouseholdError("original native cart acknowledgement record changed")
    if sync_ending:
        sync_native_record(ending_path)
    return "host_stopped_before_click" if "outcome" in ending else "host_acknowledged"


class NativeHost:
    owns_managed_cart_journal = True

    def __init__(self, store, config, reader, command_id, deadline, request=None):
        self.store, self.config, self.reader = store, config, reader
        self.command_id, self.deadline = command_id, deadline
        self.calls = 0
        self.product_searches = 0
        self.acknowledged_cart_batches = 0
        self.request = request or {}
        self.last_cart = None

    def managed_intent(self, operations):
        if self.request.get("operation") != "products" or self.request.get("action") != "apply":
            return False
        if self.last_cart is None:
            raise HouseholdError("managed native batch requires a complete pre-write cart")
        before = self.app._cart_lines(cart_summary(self.last_cart))[0]
        return self.app._native_managed_intent(self.command_id, self.request, before, operations)

    def pending_binding(self, operation):
        state = self.store.read()
        if state.get("native_config_sha256") != dots.digest(dots.encoded(self.config)):
            raise HouseholdError("original native household/browser configuration changed")
        allowed_edit_reads = dots_meny_edit.READS_BY_ACTION.get(self.request.get("action"), ())
        if (dots_meny_edit.active(state)
                and (self.request.get("operation") != "orders" or operation not in allowed_edit_reads)):
            raise HouseholdError("keep the original MENY edit tab; reconcile the edit before another browser operation")
        return state.get("pending_cart_change")

    def verify_original_account(self):
        proof = self.exchange("verify_observation_context", {})
        binding = self.config["browser_binding"]
        identity = {key: binding[key] for key in ("account_sha256", "cart_context_sha256")}
        if (proof != {**identity, "authenticated": True, "new_cart": True}
                or proof["authenticated"] is not True or proof["new_cart"] is not True):
            raise HouseholdError("the original MENY account and ordinary cart context could not be verified")

    def select_delivery(self, record, started):
        review, monotonic_now = record["review"], time.monotonic()
        expiry = min(dots.timestamp(record["expires_at"]), dots.timestamp(review["window"]["start_at"]))
        deadline = min(self.deadline, monotonic_now + 60, started + 150,
                       monotonic_now + (expiry - datetime.now(timezone.utc)).total_seconds())
        if deadline <= monotonic_now:
            raise HouseholdError("MENY delivery preflight expired before dispatch")
        binding = record["binding"]
        arguments = {"window": record["review"]["window"], "calendar_label": binding["calendar_label"],
                     "address_sha256": binding["address_sha256"], "goods_sha256": binding["goods_sha256"],
                     "original_selected": binding["selected"]}
        frame = native_frame(self.command_id, "select_delivery_window", arguments,
                             self.config["browser_binding"], deadline)
        frame.update(effect="delivery_selection", review_id=record["review_id"], review_digest=record["review_digest"],
                     stop_before_first_click_result=PRE_CLICK_STOP)
        pending = {**record, "status": "dispatching", "dispatch_command_id": self.command_id,
                   "call_id": frame["call_id"], "dispatch_expires_at": frame["expires_at"]}
        with self.store.locked() as state:
            if (state.get(dots_meny_delivery.STATE_KEY) != record
                    or any(state.get(key) for key in dots_meny_delivery.ORDINARY)):
                raise HouseholdError("the original MENY delivery review or cart context changed")
            state[dots_meny_delivery.STATE_KEY] = pending
        result, seen = native_reply(self.reader, frame, deadline)
        if set(result) == PRE_CLICK_STOP.keys() and all(result[key] is expected for key, expected in PRE_CLICK_STOP.items()):
            pending = {**pending, "status": "delivery_selection_not_dispatched", "observed_at": seen.isoformat(),
                       "dispatch_outcome": "host_stopped_before_first_click"}
        elif set(result) == {"dispatched"} and result["dispatched"] is True:
            pending = {**pending, "status": "dispatched", "acknowledged_at": seen.isoformat()}
        else:
            raise HouseholdError("MENY delivery dispatch ending is unknown; reconcile without resend")
        with self.store.locked() as state:
            state[dots_meny_delivery.STATE_KEY] = pending
        return pending

    def exchange(self, operation, arguments, *, deadline=None, write=False, observation_time=False):
        binding = self.config.get("browser_binding")
        if binding is None:
            raise HouseholdError("native cloud browser/account/cart binding is required; no browser started")
        self.pending_binding(operation)
        self.calls += 1
        if self.calls > 64:
            raise HouseholdError("native host command exceeded its call limit")
        reply_seconds = 120 if operation in {"get_delivery_slots", "product_search", "recipe_search",
                                              "read_edit_receipt", "review_edit_checkout"} and not write else 60
        deadline = min(self.deadline, deadline or self.deadline, time.monotonic() + reply_seconds)
        if deadline <= time.monotonic():
            raise HouseholdError("native host operation deadline reached")
        frame = native_frame(self.command_id, operation, arguments, binding, deadline, write=write)
        if operation == "review_checkout":
            frame["effect"] = "checkout_review"
        ordinary_write = (write and self.request.get("operation") == "cart"
                          and self.request.get("action") in {"ensure", "clear"})
        if ordinary_write:
            frame["stop_before_first_click_result"] = PRE_CLICK_STOP
        if write:
            with self.store.locked() as state:
                if not cart_writes_enabled(self.config, state):
                    raise HouseholdError("native cart writes are disabled in this private household")
                pending = state.get("pending_cart_change")
                if (not pending or pending.get("provider") != "meny" or pending.get("order_change")
                        or pending.get("operations") != arguments.get("operations")
                        or state.get("native_config_sha256") != dots.digest(dots.encoded(self.config))):
                    raise HouseholdError("original core cart intent or native context is absent or changed")
                if ordinary_write:
                    frame["operation_id"] = pending["operation_id"]
                frame["before_quantities"] = pending["before"]
                frame["expected_quantities"] = pending["expected"]
                frame["preconditions"] = ["same approved cloud browser/account/cart context", "no order edit",
                                           "complete current cart equals before_quantities",
                                           "exact product and unique enabled unobscured control", "unexpired request"]
                attempt = prepare_cart_dispatch(self.store.directory, state["native_config_sha256"], pending, frame)
        result, observed = native_reply(self.reader, frame, deadline)  # Original write intent precedes emission.
        if write:
            stopped = (ordinary_write and set(result) == PRE_CLICK_STOP.keys()
                       and all(result[key] is expected for key, expected in PRE_CLICK_STOP.items()))
            if not stopped and (set(result) != {"dispatched"} or result["dispatched"] is not True):
                raise HouseholdError("native cart dispatch ending is unknown; reconcile without resend")
            ending = {"command_id": self.command_id, "call_id": frame["call_id"], "observed_at": observed.isoformat()}
            if stopped:
                ending["outcome"] = "not_dispatched"
            publish_native_record(attempt / "ack.json", ending)
            if stopped:
                raise MenyCartStoppedError("native host confirmed the cart batch stopped before its first click")
            if ordinary_write:
                self.acknowledged_cart_batches += 1
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
            dots.object_fields(arguments, {"delivery_date", "recovery_read"}, set())
            requested_date = arguments.get("delivery_date")
            if requested_date is not None and (
                    not isinstance(requested_date, str)
                    or date.fromisoformat(requested_date).isoformat() != requested_date):
                raise HouseholdError("native delivery date must be a canonical ISO date")
            return delivery_picker(self.exchange(tool, arguments, deadline=deadline), requested_date,
                                   recovery_read=arguments.get("recovery_read") is True)
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
                        or MENY_PRODUCT_PATH.fullmatch(path) is None):
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
                        or MENY_PRODUCT_PATH.fullmatch(item["productId"]) is None):
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


def publish_native_record(path, record):
    # The final immutable name must never expose a partially written record.
    if not os.path.lexists(path):
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            dots.exclusive_json(temp, record, MAX_LINE)
            os.link(temp, path, follow_symlinks=False)
        finally:
            temp.unlink(missing_ok=True)
    if on_demand._read_file(path, MAX_LINE) != dots.encoded(record):
        raise HouseholdError("native record conflicts with its original owner or request")
    sync_native_record(path)


def sync_native_record(path):
    # A previous publication may have failed after making the final name visible.
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
        _flush_drive_cache(descriptor)
        on_demand._sync_directory(path.parent)
        _flush_drive_cache(descriptor)
    finally:
        os.close(descriptor)


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


def load_bound_state(root, config_data, config, *, allow_reset_recovery=False):
    state = dots.existing_root(root / "state")
    regular(state / "state.json")
    regular(state / "recipes.sqlite3")
    saved = on_demand._json(on_demand._read_file(state / "state.json", on_demand.MAX_MENU))
    active = config
    expected = dots.digest(config_data)
    intent_path = state / LEGACY_RESET_INTENT
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
        if on_demand._read_file(intent_path, MAX_LINE) != dots.encoded(intent):
            raise ValueError("original native cart reset intent changed")
    past = saved.get(TAB_HISTORY, [])
    if not isinstance(past, list) or (past and TAB_REBIND not in saved):
        raise ValueError("the original browser binding history changed")
    seen = {entry["browser_binding"]["tab_id"] for entry in (config, active) if entry.get("browser_binding")}
    for rebind in (*past, *([saved[TAB_REBIND]] if TAB_REBIND in saved else [])):
        fields = {"base_config_sha256", "previous_tab", "tab_id", "observed_at"}
        dots.object_fields(rebind, fields | {"request_sha256", "delivery_recovery"}, fields)
        tab_id = observation_tab_id(rebind["tab_id"])
        if (active.get("browser_binding") is None or rebind["base_config_sha256"] != dots.digest(config_data)
                or observation_tab_id(rebind["previous_tab"]) != active["browser_binding"]["tab_id"] or tab_id in seen):
            raise ValueError("original native browser rebind receipt changed")
        dots.timestamp(rebind["observed_at"])
        if ("request_sha256" in rebind and (not isinstance(rebind["request_sha256"], str)
                or re.fullmatch(r"[a-f0-9]{64}", rebind["request_sha256"]) is None)):
            raise ValueError("the original native browser rebind request changed")
        if "delivery_recovery" in rebind:
            ending = rebind["delivery_recovery"]
            if ("request_sha256" not in rebind or not isinstance(ending, dict)
                    or ending.get("status") not in dots_meny_delivery.OBSERVATION_ENDINGS
                    or ending.get("dispatch") is not False):
                raise ValueError("the original native delivery recovery receipt changed")
        seen.add(tab_id)
        active = with_tab(config, tab_id)
        expected = dots.digest(dots.encoded(active))
    if saved.get("native_config_sha256") != expected:
        raise ValueError("original native configuration binding changed; no replacement account/context")
    if active == config and os.path.lexists(intent_path):
        if not allow_reset_recovery:
            raise HouseholdError("the legacy reset must be recovered before starting a new command")
        intent = on_demand._json(on_demand._read_file(intent_path, MAX_LINE))
        digest = legacy_reset_digest(state, config, saved)
        tab_id = observation_tab_id(intent["tab_id"])
        if not digest or dots.encoded(intent) != dots.encoded(legacy_reset_intent(tab_id, digest)):
            raise HouseholdError("the original legacy reset archive is missing or the prior journal changed")
    return state, saved, active


def inspect_cart(root):
    root = dots.existing_root(root)
    on_demand._read_file(root / "command.lock", 0)
    with file_lock(root / "command.lock"):
        data = on_demand._read_file(root / "config.json", MAX_LINE)
        config = configuration(on_demand._json(data))
        state, saved, active = load_bound_state(root, data, config, allow_reset_recovery=True)
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


def valid_ordinary_cart_batch(pending):
    required = {"provider", "operation_id", "request_digest", "before", "expected", "order_change", "operations"}
    optional = {"cart_only", "clear_requested", "clear_id", "dispatch_finished"}
    if (not isinstance(pending, dict) or not required <= pending.keys() <= required | optional
            or pending["provider"] != "meny" or pending["order_change"] is not None
            or pending.get("dispatch_finished", False) is not False
            or not isinstance(pending["operation_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", pending["operation_id"])
            or not isinstance(pending["request_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", pending["request_digest"])
            or any(pending[key] is not True for key in ("cart_only", "clear_requested") if key in pending)
            or ("clear_id" in pending) != ("clear_requested" in pending)):
        return False
    if "clear_id" in pending:
        clear_id = pending["clear_id"]
        if "cart_only" in pending or not isinstance(clear_id, str) or not re.fullmatch(r"[a-f0-9]{32}", clear_id):
            return False
    for quantities in (pending["before"], pending["expected"]):
        if not isinstance(quantities, dict) or any(
                not isinstance(key, str) or not key or type(amount) is not int or amount <= 0
                for key, amount in quantities.items()):
            return False
        try:
            if any(normalize_product_ref(key) != key for key in quantities):
                return False
        except HouseholdError:
            return False
    operations = pending["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= 2:
        return False
    quantities = dict(pending["before"])
    clicks = 0
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {"productId", "quantity"}:
            return False
        product, delta = operation["productId"], operation["quantity"]
        if (not isinstance(product, str) or MENY_PRODUCT_PATH.fullmatch(product) is None
                or type(delta) is not int or not 1 <= abs(delta) <= 2
                or (pending.get("cart_only") and delta < 0) or (pending.get("clear_requested") and delta > 0)):
            return False
        try:
            dots.public_path(product)
        except ValueError:
            return False
        clicks += abs(delta)
        updated = quantities.get(product, 0) + delta
        if updated < 0:
            return False
        if updated:
            quantities[product] = updated
        else:
            quantities.pop(product, None)
    return clicks <= 2 and quantities == pending["expected"]


def legacy_reset_digest(state, config, saved):
    pending = saved.get("pending_cart_change")
    if (config.get("browser_binding") is None or saved.get("version") != STATE_VERSION
            or saved.get("provider") != "meny" or saved.get("household") != config["household"]
            or "native_cart_legacy_reset" in saved or "native_cart_dispatch_protocol" in saved
            or cart_writes_enabled(config, saved)
            or not valid_ordinary_cart_batch(pending)
            or pending["operation_id"] in (saved.get("cart_change_receipts") or {})
            or any(saved.get(key) for key in ("pending_checkout", "pending_cancellation", "order_change"))):
        return None
    dispatches = state / "native-cart-dispatch"
    if os.path.lexists(dispatches) and next(dots.existing_root(dispatches).iterdir(), None) is not None:
        return None  # Retain any dispatch evidence, even if it cannot be attributed to this batch.
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


class DeliveryObservation:
    def __init__(self, read):
        self.read = read

    def exchange(self, operation, arguments):
        if operation != "delivery_context":
            raise HouseholdError("replacement-tab delivery reconciliation only reads the original delivery context")
        return self.read(operation, arguments)[0]

    def call(self, operation, arguments):
        if operation == "get_cart":
            return read_observation_cart(self.read)[0]
        if operation == "get_delivery_slots":
            return delivery_picker(self.read(operation, arguments)[0], recovery_read=True)
        raise HouseholdError("replacement-tab delivery reconciliation only reads the original cart and picker")


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


def rebind_tab(root, value, reader):
    fields = {"tab_id", "previous_tab_id", "original_tab_retired"}
    dots.object_fields(value, fields | {"delivery_recovery", "edit_recovery"}, fields)
    tab_id = observation_tab_id(value["tab_id"])
    previous_tab = observation_tab_id(value["previous_tab_id"])
    if value["original_tab_retired"] is not True:
        raise ValueError("verify that the original browser tab is retired before replacing it")
    request_digest = dots.digest(dots.encoded(value))
    root = dots.existing_root(root)
    on_demand._read_file(root / "command.lock", 0)
    with ExitStack() as stack:
        stack.enter_context(file_lock(root / "command.lock"))
        data = on_demand._read_file(root / "config.json", MAX_LINE)
        config = configuration(on_demand._json(data))
        directory, saved, active = load_bound_state(root, data, config)
        binding = active.get("browser_binding")
        history = [*saved.get(TAB_HISTORY, []), *([saved[TAB_REBIND]] if TAB_REBIND in saved else [])]
        original = next((item for item in history if item["tab_id"] == tab_id and item["previous_tab"] == previous_tab), None)
        if original is not None:
            if (original.get("request_sha256", request_digest if "delivery_recovery" not in value else None)
                    != request_digest):
                raise HouseholdError("the replacement tab belongs to a different original recovery request")
            return {"status": "browser_rebound", "observed_at": original["observed_at"],
                    "observation_scope": "historical", "writes_enabled": False, **RENDERED_UI_EVIDENCE,
                    **({"delivery_recovery": {**original["delivery_recovery"], "observation_scope": "historical"}}
                       if "delivery_recovery" in original else {})}
        retired = {item["previous_tab"] for item in history}
        if config.get("browser_binding") is not None:
            retired.add(config["browser_binding"]["tab_id"])
        if binding is None or previous_tab != binding["tab_id"] or tab_id == previous_tab or tab_id in retired:
            raise ValueError("use a new tab in the original MENY cloud browser")
        if any(saved.get(key) for key in dots_meny_delivery.ORDINARY):
            raise HouseholdError("resolve the original MENY operation before replacing its browser tab")
        edit = saved.get(dots_meny_edit.STATE_KEY) or {}
        edit_recovery = value.get("edit_recovery")
        if dots_meny_edit.active(saved):
            if (edit_recovery != edit["edit_id"] or (edit["status"] == "edit_owner_pending"
                    and datetime.now(timezone.utc) < dots.timestamp(edit["owner_handoff_admit_by"]))):
                raise HouseholdError("use the original MENY edit and a retired handoff for read-only tab recovery")
        elif edit_recovery is not None:
            raise HouseholdError("no active original MENY edit matches this tab recovery")
        pending = saved.get(dots_meny_delivery.STATE_KEY) or {}
        recovery = value.get("delivery_recovery")
        if pending.get("status") in dots_meny_delivery.PENDING:
            if recovery is None:
                raise HouseholdError("include the original MENY delivery review to reconcile it on the replacement tab")
            keys = {"review_id", "review_digest"}
            dots.object_fields(recovery, keys | {"owner_accepted_unknown", "original_executor_stopped"}, keys)
            if (any(pending[field] != recovery[field] for field in keys)
                    or type(recovery.get("owner_accepted_unknown", False)) is not bool
                    or recovery.get("owner_accepted_unknown") is True and recovery.get("original_executor_stopped") is not True
                    or datetime.now(timezone.utc) < dots.timestamp(pending["dispatch_expires_at"])):
                raise HouseholdError("use the exact original expired MENY delivery dispatch for read-only recovery")
        elif recovery is not None:
            # A prior run may settle the delivery and fail before publishing the new tab.
            if (not isinstance(recovery, dict) or pending.get("status") not in dots_meny_delivery.OBSERVATION_ENDINGS
                    or any(pending.get(field) != recovery.get(field) for field in ("review_id", "review_digest"))):
                raise HouseholdError("no original MENY delivery attempt matches this recovery")
        registry = native_ownership(stack, root, config, new=False, active_tab=binding["tab_id"],
                                    observation_tab=tab_id, reserving=True)
        lock_core_files(stack, directory)
        store = StateStore(directory, {"household": config["household"], "provider": "meny"})
        read = verify_observation(reader, binding, tab_id)
        cart, observed = read_observation_cart(read)
        if edit_recovery is not None and cart["items"]:
            raise HouseholdError("verify the original empty ordinary MENY cart before edit tab recovery")
        settled = None
        if pending.get("status") in dots_meny_delivery.PENDING:
            settled = dots_meny_delivery.readback(DeliveryObservation(read), store, pending,
                                                  accept_current=recovery.get("owner_accepted_unknown", False),
                                                  original_account_verified=True)
        elif recovery is not None:
            settled = dots_meny_delivery.ending_view(pending)
        publish_native_record(registry / (tab_target(binding, tab_id) + ".json"), target_owner(root, config))
        with store.locked() as state:
            if (state.get("native_config_sha256") != saved["native_config_sha256"]
                    or any(state.get(key) for key in dots_meny_delivery.ORDINARY)
                    or (state.get(dots_meny_delivery.STATE_KEY) or {}).get("status") in dots_meny_delivery.PENDING):
                raise HouseholdError("the original MENY browser configuration changed")
            if history:
                state[TAB_HISTORY] = history
            state[TAB_REBIND] = {"base_config_sha256": dots.digest(data), "previous_tab": binding["tab_id"],
                                 "tab_id": tab_id, "observed_at": observed, "request_sha256": request_digest,
                                 **({"delivery_recovery": settled} if settled is not None else {})}
            state["native_config_sha256"] = dots.digest(dots.encoded(with_tab(config, tab_id)))
            state["native_cart_writes_enabled"] = False
            state["product_selection_generation"] = uuid.uuid4().hex
        return {"status": "browser_rebound", "observed_at": observed, "writes_enabled": False, **RENDERED_UI_EVIDENCE,
                **({"delivery_recovery": settled} if settled is not None else {})}


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
        directory, saved, active = load_bound_state(root, data, config, allow_reset_recovery=True)
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
            lock_core_files(stack, directory)
            state_lock = directory / "state.lock"
            on_demand._read_file(state_lock, 0)
            with file_lock(state_lock):
                _, locked_state, _ = load_bound_state(root, data, config)
                if locked_state.get("native_cart_legacy_reset") != previous:
                    raise HouseholdError("original legacy reset receipt changed")
                sync_native_record(directory / "state.json")
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
            owner = registry / (tab_target(original, tab_id) + ".json")
            publish_native_record(owner, target_owner(root, config))
            publish_native_record(intent, value)
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


def finish_native_cart_stop(state, pending):
    if not valid_ordinary_cart_batch(pending):
        raise HouseholdError("original pre-click ending conflicts with the pending MENY batch")
    receipts = state.setdefault("cart_change_receipts", {})
    operation = pending["operation_id"]
    if operation in receipts:
        raise HouseholdError("original pre-click ending conflicts with an existing MENY receipt")
    receipts[operation] = {"status": "not_dispatched", "provider": "meny", "request_digest": pending["request_digest"]}
    state.pop("pending_cart_change")
    return {"reconciled": False, "cart_write_pending": False, "status": "not_dispatched"}


def accept_native_current(root, app, config, reader, request):
    fields = {"operation", "action", "operation_id", "original_request_id", "cart_digest",
              "accept_unknown_dispatch", "original_executor_retired"}
    dots.object_fields(request, fields, fields)
    original_id = request["original_request_id"]
    if (request["accept_unknown_dispatch"] is not True or request["original_executor_retired"] is not True
            or not isinstance(original_id, str) or str(uuid.UUID(original_id)) != original_id
            or request["cart_digest"] != app._cart_digest({})):
        raise HouseholdError("accepting the empty cart requires the original request, owner approval and a retired executor")
    saved = app.store.read()
    pending = saved.get("pending_cart_change")
    if (cart_writes_enabled(config, saved) or not valid_ordinary_cart_batch(pending)
            or not pending.get("clear_requested") or pending["operation_id"] != request["operation_id"]
            or list(pending["before"].values()) != [1] or pending["expected"]
            or pending["operation_id"] in (saved.get("cart_change_receipts") or {})
            or any(saved.get(key) for key in ("pending_checkout", "pending_cancellation", "order_change"))):
        raise HouseholdError("accept_current requires the original pending single-unit MENY removal with writes disabled")
    directory, config_digest = app.store.directory, saved["native_config_sha256"]
    attempt, frame = cart_dispatch_frame(directory, config_digest, pending)
    if (frame is None or frame["command_id"] != original_id
            or dots.timestamp(frame["expires_at"]) > datetime.now(timezone.utc)
            or os.path.lexists(attempt / "ack.json")):
        raise HouseholdError("the original MENY removal frame or its unacknowledged ending changed")
    calls = dots.existing_root(root / "calls" / original_id)
    record = on_demand._json(on_demand._read_file(calls / "intent.json", MAX_LINE))
    original = record["input"]["request"]
    receipt = on_demand._json(on_demand._read_file(calls / "result.json", on_demand.MAX_MENU))
    # Application strips projection fields before the core journals cart input.
    projection = {"response_view", "view_offset", "view_limit", "view_section"}
    original_digest = planning_digest({key: value for key, value in original.items()
                                       if key not in projection and not key.startswith("_")})
    if (record["input"]["request_id"] != original_id or record["config_sha256"] != config_digest
            or (original.get("operation"), original.get("action")) != ("cart", "clear")
            or original.get("cart_digest") != app._cart_digest(pending["before"])
            or original_digest != pending["request_digest"] or receipt != {"ok": False, "error": HOST_FAILURE}):
        raise HouseholdError("the original single-unit removal request or correlated host failure changed")

    binding = config["browser_binding"]
    read = verify_observation(reader, binding, binding["tab_id"])
    cart, observed_at = read_observation_cart(read)
    if cart["count"] or cart["total"] or cart["delivery"] is not None:
        raise HouseholdError("owner acceptance requires a fresh empty ordinary MENY cart without a delivery slot")
    with app.store.locked() as state:
        if (state.get("native_config_sha256") != config_digest or cart_writes_enabled(config, state)
                or state.get("pending_cart_change") != pending
                or pending["operation_id"] in (state.get("cart_change_receipts") or {})
                or cart_dispatch_frame(directory, config_digest, pending) != (attempt, frame)
                or os.path.lexists(attempt / "ack.json")):
            raise HouseholdError("original MENY removal changed before owner acceptance")
        app.allow_observed_cart_recovery = True
        app._archive_observed_cart_target(state, pending, {}, owner_accepted=True)
        app._finish_cart_clear(state, pending, {}, {}, owner_accepted=True)
        state["cart_change_receipts"][pending["operation_id"]]["native_owner_review"] = {
            "frame": frame, "observed_at": observed_at, "original_executor_retired": True,
        }
    return {"status": "owner_accepted_cart", "historical_dispatch": "unknown", "cart_write_pending": False,
            "writes_enabled": False, **RENDERED_UI_EVIDENCE, "observed_at": observed_at}


def reconcile_cart_change(app, request):
    operation = request.get("operation_id")
    with app.store.locked() as state:
        reset = state.get("native_cart_legacy_reset")
        pending = state.get("pending_cart_change")
        if reset and operation == reset["pending_cart_change"]["operation_id"]:
            return {"reconciled": False, "cart_write_pending": bool(pending), "status": "archived_unknown",
                    "historical_dispatch": "unknown"}
        if not pending:
            if operation is None:
                return {"reconciled": False, "cart_write_pending": False, "status": "no_pending"}
            receipt = (state.get("cart_change_receipts") or {}).get(operation) if isinstance(operation, str) else None
            if (receipt and receipt.get("status") == "owner_accepted_cart"
                    and receipt.get("dispatch_outcome") == "unknown" and receipt.get("native_owner_review")):
                sync_native_record(app.store.path)
                return {"reconciled": False, "cart_write_pending": False, "status": "owner_accepted_cart",
                        "historical_dispatch": "unknown"}
            if (not receipt or receipt.get("provider") != "meny"
                    or receipt.get("status") not in ("read_back", "completed_with_cart_drift", "not_dispatched")):
                raise HouseholdError("no current or recorded MENY cart operation matches this reference")
            return {"reconciled": receipt["status"] == "read_back", "cart_write_pending": False,
                    "status": receipt["status"]}
        if pending and pending["provider"] == "meny":
            original = {key: value for key, value in pending.items() if key != "dispatch_finished"}
            if operation in (None, app._cart_operation_ref(pending), app._cart_operation_ref(original)):
                ending = cart_dispatch_status(app.store.directory, state["native_config_sha256"], pending,
                                              sync_ending=True)
                if ending == "host_stopped_before_click":
                    return finish_native_cart_stop(state, pending)
                if ending == "host_acknowledged":
                    pending["dispatch_finished"] = True
                    if operation is not None:
                        request = {**request, "operation_id": app._cart_operation_ref(pending)}
    return app.handle(request)


def command(root, value, reader):
    dots.object_fields(value, {"request_id", "request"}, {"request_id", "request"})
    command_id = value["request_id"]
    if not isinstance(command_id, str) or str(uuid.UUID(command_id)) != command_id:
        raise ValueError("request_id must be a canonical UUID")
    request = value["request"]
    handoff = isinstance(request, dict) and (request.get("operation"), request.get("action")) in (
        ("checkout", "prepare"), ("orders", "change_handoff"))
    native_edit = (isinstance(request, dict) and request.get("operation") == "orders"
                   and isinstance(request.get("action"), str) and request["action"] in dots_meny_edit.ACTIONS)
    if not isinstance(request, dict) or (request.get("operation") not in CORE_OPERATIONS | {"native_cart_policy"}
            and not handoff
            and not native_edit
            and not (request.get("operation") == "catalog" and request.get("action") in {"products", "recipes"})
            and not (request.get("operation") == "products" and request.get("action", "prepare") in {"prepare", "get", "apply"})
            and not (request.get("operation") == "delivery" and request.get("action", "list") in {
                "list", "prepare", "select", "reconcile", "accept_current"})
            and not (request.get("operation") == "orders" and request.get("action", "list") in {"list", "get"})
            and not (request.get("operation") == "cart" and request.get("action", "get") in {
                "get", "ensure", "clear", "reconcile_change", "reconcile", "accept_current"})):
        raise ValueError("unsupported foreground core operation")
    if "_restore_missing_cart_digest" in request:
        raise ValueError("_restore_missing_cart_digest is internal-only")
    native_delivery = request.get("operation") == "delivery" and request.get("action", "list") != "list"
    if handoff:
        dots_meny_handoff.validate(request)
    if native_edit:
        dots_meny_edit.validate(request)
    if native_delivery:
        dots_meny_delivery.validate(request)
    elif request.get("operation") == "delivery":
        dots.object_fields(request, {"operation", "action", "dates", "response_view", "view_offset",
                                    "view_limit", "view_section"}, {"operation"})
    if request.get("operation") == "orders" and not (handoff or native_edit):
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
                bindings = [saved["native_config_sha256"]]
                original_tabs = [item["previous_tab"] for item in saved.get(TAB_HISTORY, [])]
                if TAB_REBIND in saved:
                    original_tabs.append(saved[TAB_REBIND]["previous_tab"])
                if "native_cart_legacy_reset" in saved:
                    original_tabs.append(saved["native_cart_legacy_reset"]["replacement_tab"])
                bindings.extend(dots.digest(dots.encoded(with_tab(config, tab))) for tab in original_tabs)
                if not any(intent == dots.encoded({**base_record, "config_sha256": binding}) for binding in bindings):
                    raise ValueError("original command input/configuration conflicts; no replay")
            if not os.path.lexists(target / "result.json"):
                return {"ok": False, "status": "incomplete", "message": "Original command has no ending; no replay."}
            result = on_demand._json(on_demand._read_file(target / "result.json", on_demand.MAX_MENU))
            if result["ok"] and (native_delivery or handoff or native_edit):
                result["result"].update(observation_scope="historical", dispatch=False)
                if result["result"].get("owner_handoff_ready") is True:
                    result["result"].update(owner_handoff_ready=False)
                    result["result"].pop("next_control", None)
                    result["result"].pop("owner_handoff_admit_by", None)
            return result
        # Fail missing originals BEFORE publishing a new command intent.
        _, saved, active = load_bound_state(root, config_data, config)
        if request["operation"] == "native_cart_policy":
            validate_cart_policy(request, active)
        apply = (request.get("operation"), request.get("action")) == ("products", "apply")
        if active != config and apply and not request.get("product_plan_ref"):
            raise ValueError("prepare and review a new saved product plan after resetting the legacy cart")
        write = apply or (request.get("operation") == "cart" and request.get("action") in {"ensure", "clear"})
        enabling_writes = request.get("operation") == "native_cart_policy" and request.get("enabled") is True
        if dots_meny_edit.active(saved) and (write or enabling_writes or native_delivery or handoff):
            raise ValueError("reconcile the original MENY order edit before another write or handoff")
        if (dots_meny_edit.active(saved)
                and not native_edit and request["operation"] in {"orders", "catalog", "cart", "delivery", "products"}):
            raise ValueError("keep the original MENY edit tab; reconcile the edit before another browser operation")
        if ((saved.get(dots_meny_delivery.STATE_KEY) or {}).get("status") in dots_meny_delivery.PENDING
                and (write or enabling_writes)):
            raise ValueError("reconcile the original MENY delivery selection before changing the cart")
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
                rebind = saved.get(TAB_REBIND)
                if (apply and rebind and "request_sha256" not in rebind
                        and saved.get(TAB_PLANS_RETIRED) != saved["native_config_sha256"]):
                    with app.store.locked() as state:
                        if (any(state.get(key) for key in dots_meny_delivery.ORDINARY)
                                or (state.get(dots_meny_delivery.STATE_KEY) or {}).get("status") in dots_meny_delivery.PENDING):
                            raise HouseholdError("reconcile the original MENY operation before retiring stale approvals")
                        state["product_selection_generation"] = uuid.uuid4().hex
                        state[TAB_PLANS_RETIRED] = saved["native_config_sha256"]
                    raise HouseholdError("prepare and review a new MENY product plan after rebinding the browser")
                if write:
                    with app.store.locked() as state:
                        if not state.get("pending_cart_change"):
                            # Distinguish new admitted writes before the core can create a pending batch.
                            state.setdefault("native_cart_dispatch_protocol", 1)
                if request["operation"] == "cart" and request.get("action") == "reconcile_change":
                    result = reconcile_cart_change(app, request)
                elif request["operation"] == "cart" and request.get("action") == "accept_current":
                    result = accept_native_current(root, app, active, reader, request)
                elif request["operation"] == "native_cart_policy":
                    result = cart_policy(app.store, active, request)
                elif native_delivery:
                    result = dots_meny_delivery.handle(app.provider_client, app.store, request, command_id)
                elif handoff:
                    result = dots_meny_handoff.handle(app.provider_client, app.store, request)
                elif native_edit:
                    result = dots_meny_edit.handle(app.provider_client, app.store, request, command_id)
                else:
                    result = app.handle(request)
                if write and request["operation"] == "cart" and result.get("status") == "not_dispatched":
                    if request["action"] == "clear":
                        result.pop("ensured", None)
                        result["cleared"] = False
                    finished = app.provider_client.acknowledged_cart_batches
                    if finished:
                        result.update(status="partially_applied", batch_status="not_dispatched",
                                      completed_batches=finished)
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
    parser.add_argument("action", choices=("init", "call", "inspect-cart", "observe", "reset-legacy-cart", "rebind-tab"))
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
            elif args.action == "rebind-tab":
                result = {"ok": True, "result": rebind_tab(args.root, value, reader)}
            else:
                result = command(args.root, value, reader)
        emit({"kind": "core_result", **result})
        return 0 if result["ok"] else 1
    except (HouseholdError, OSError, ValueError, TypeError, KeyError, RuntimeError, UnicodeError) as exc:
        emit({"kind": "core_result", "ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
