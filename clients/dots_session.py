#!/usr/bin/env python3
"""Bounded foreground core commands with an explicit native-cloud host."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import select
import stat
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clients import dots
from core import HouseholdError, StateStore
from meny import normalize_cart_snapshot
import on_demand
from runtime_ownership import file_lock
from service import Application

CORE_OPERATIONS = {"health", "setup", "profile", "recipes", "menu", "feedback",
                   "pantry", "recurring", "product_favorites"}
OWNER_FILES = (".service-owner.lock", "recipes.sqlite3.owner.lock", "state.json.owner.lock")
MAX_LINE = 65536


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


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def configuration(value):
    dots.object_fields(value, {"household", "browser_binding", "allow_cart_writes"}, {"household"})
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
    return value


class NativeHost:
    def __init__(self, store, config, reader, command_id, deadline):
        self.store, self.config, self.reader = store, config, reader
        self.command_id, self.deadline = command_id, deadline
        self.calls = 0

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
        deadline = min(self.deadline, deadline or self.deadline, time.monotonic() + 60)
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
            if not self.config.get("allow_cart_writes"):
                raise HouseholdError("native cart writes are disabled in this private configuration")
            with self.store.locked() as state:
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
                "server": {"name": "host-attested native cloud MENY"}, "tool_count": 3}

    def verify_order_change(self, order_id, code, *, deadline=None):
        if order_id is not None or code is not None:
            raise HouseholdError("native foreground adapter does not support existing-order edits")
        result = self.exchange("verify_new_cart", {}, deadline=deadline)
        if set(result) != {"authenticated", "new_cart"} or any(result[key] is not True for key in result):
            raise HouseholdError("native account or new-cart mode could not be verified")
        return result

    def call(self, tool, arguments, *, deadline=None, **kwargs):
        if tool == "get_cart":
            return normalize_cart_snapshot(self.exchange("get_cart", {}, deadline=deadline))
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
            result = self.exchange(tool, arguments, deadline=deadline, write=True)
            if set(result) != {"dispatched"} or result["dispatched"] is not True:
                raise HouseholdError("native cart dispatch ending is unknown; reconcile without resend")
            return result
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
    registry = Path.home() / ".meal-concierge-dots-targets"
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


def application(root, config, reader, command_id, deadline, *, new=False):
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
        host = NativeHost(store, config, reader, command_id, deadline)
        app = Application(store, host, None, external_recipe_sources={})
        return stack, app
    except BaseException:
        stack.close()
        raise


def init(root, value, reader):
    config = configuration(value)
    root = Path(root).absolute()
    if root.parent.resolve(strict=True) != root.parent:
        raise ValueError("core root parent must be canonical")
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
    if not isinstance(request, dict) or (request.get("operation") not in CORE_OPERATIONS
            and not (request.get("operation") == "cart" and request.get("action", "get") in {"get", "ensure", "reconcile_change"})):
        raise ValueError("unsupported foreground core operation; no checkout, delivery or managed cart apply")
    root = dots.existing_root(root)
    on_demand._read_file(root / "command.lock", 0)
    with file_lock(root / "command.lock"):
        config_data = on_demand._read_file(root / "config.json", MAX_LINE)
        config = configuration(on_demand._json(config_data))
        if request.get("operation") == "cart" and request.get("action") == "ensure" and not config.get("allow_cart_writes"):
            raise ValueError("native cart writes are disabled; no intent or browser operation started")
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
        target.mkdir(mode=0o700)
        on_demand._sync_directory(calls)
        dots.exclusive_json(target / "intent.json", record, MAX_LINE)
        try:
            stack, app = application(root, config, reader, command_id, time.monotonic() + 240)
            with stack:
                result = app.handle(request)
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
    reader = Input()
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
