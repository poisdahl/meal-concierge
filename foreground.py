#!/usr/bin/env python3
"""Call one installed household tool in a foreground process."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE))

import cli
import install
from build_identity import verify_release_layout
from rpc_client import ServiceError, merchant_mutation
from runtime_ownership import file_lock


LOCAL_TOOLS = frozenset({"health", "status", "setup", "profile", "recipes", "menu",
                         "shopping_list", "feedback", "recipe_delivery", "recipe_pack", "schedule", "products"})
STORE_TOOLS = frozenset({"store_check", "catalog", "cart", "delivery", "orders", "checkout",
                         "recurring", "product_favorites"})
RECEIPT_LIMIT = 8 * 1024 * 1024
RECOVERY_READS = {
    "cart": {("cart", "get"), ("cart", "reconcile_change")},
    "delivery": {("delivery", "list"), ("cart", "get")},
    "checkout": {("checkout", "reconcile")},
    "orders": {("orders", "get"), ("orders", "cancel_reconcile"), ("orders", "remove_reconcile"), ("cart", "get")},
}
RECOVERY_READS["products"] = RECOVERY_READS["cart"]
TARGET_FIELDS = {"slot_ref", "order_id", "confirmation_id", "menu_ref", "product_plan_ref", "product_plan_digest",
                 "cart_digest", "idempotency_key", "occurrence"}


def request_id(value):
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except (AttributeError, ValueError):
        pass
    raise argparse.ArgumentTypeError("request ID must be a canonical UUID")


def original_path(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("the original installation must contain private, owned household files")


def request_digest(request, outputs):
    binding = {"request": request, "outputs": {name: str(path.absolute()) for name, path in outputs.items() if path}}
    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def read_receipt(directory, identifier):
    path = directory / f"{identifier}.json"
    if not os.path.lexists(path):
        return None
    original_path(path)
    record = install.bounded_json(path, RECEIPT_LIMIT)
    if not isinstance(record, dict) or record.get("request_id") != identifier or record.get("format") != 1:
        raise ValueError("foreground receipt does not match its original request ID")
    return record


def replay_value(value):
    """A historical response cannot grant a native send a second time."""
    if isinstance(value, list):
        return [replay_value(item) for item in value]
    if isinstance(value, dict):
        result = {key: replay_value(item) for key, item in value.items()}
        if value.get("dispatch") is True:
            result.update(dispatch=False, next="Reconcile the original native dispatch. This is its historical receipt.")
        return result
    return value


def output_matches(result):
    fd = os.open(result["output_path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_size == result.get("bytes") and 0 < info.st_size <= 32 * 1024 * 1024
                and hashlib.file_digest(file, "sha256").hexdigest() == result.get("sha256"))


def receipt_view(record, identifier):
    if record is None:
        return {"ok": False, "status": "not_found", "request_id": identifier}, 1
    receipt = {"request_id": identifier, "receipt_build": record["build"], "replayed": True}
    if record.get("reconciliation"):
        receipt["reconciliation"] = record["reconciliation"]
    if record.get("target"):
        receipt["original_target"] = record["target"]
    if "response" not in record:
        return {**receipt, "ok": False, "status": "incomplete",
                "operation": record["operation"], "action": record["action"],
                "recovery": "Inspect and reconcile the original operation before any new write."}, 1
    response = replay_value(record["response"])
    result = response.get("result") or {}
    if result.get("output_path"):
        try:
            if output_matches(result):
                return {**response, **receipt}, record["exit_code"]
        except OSError:
            pass
        return {**receipt, "ok": False, "status": "output_unavailable", "historic_file": result,
                "recovery": "Export the same frozen part to a new path with a new read-only request."}, 1
    return {**response, **receipt}, record["exit_code"]


def may_dispatch(request):
    operation, action = request["operation"], request.get("action")
    return merchant_mutation(operation, action) or (
        operation == "checkout" and action in {None, "prepare", "renew_review"}
    ) or (operation == "orders" and action == "remove_prepare")


def uncertain(response):
    result = response.get("result") or {}
    return (response.get("outcome") == "unknown" or result.get("outcome") == "unknown"
            or result.get("outcome_unknown") is True or result.get("status") in {"uncertain", "outcome_unknown"})


def unresolved(directory):
    marker = directory / "merchant-in-flight.json"
    if not os.path.lexists(marker):
        return None
    original_path(marker)
    pending = install.bounded_json(marker)
    if not isinstance(pending, dict):
        raise ValueError("original merchant receipt marker is invalid")
    identifier = request_id(pending.get("request_id", ""))
    record = read_receipt(directory, identifier)
    if record is None:
        raise ValueError("the original merchant receipt is missing; recover it before another write")
    if record.get("reconciliation") or ("response" in record and not uncertain(record["response"])):
        return None
    return identifier


def acknowledge(directory, identifier, checked_id, evidence):
    record = read_receipt(directory, identifier)
    if not record or unresolved(directory) != identifier:
        raise ValueError("no unresolved merchant call matches this request ID")
    if not 1 <= len(evidence.strip()) <= 500:
        raise ValueError("record bounded evidence of the original operation's verified outcome")
    checked = read_receipt(directory, checked_id)
    response = (checked or {}).get("response", {})
    if (not checked or response.get("ok") is not True or uncertain(response)
            or checked.get("issued_at", 0) <= record.get("issued_at", 0)
            or (checked["operation"], checked["action"]) not in RECOVERY_READS[record["operation"]]):
        raise ValueError("provide a successful, later foreground read of the original merchant operation")
    for key in ("order_id", "confirmation_id"):
        expected = record.get("target", {}).get(key)
        if expected is not None and checked.get("target", {}).get(key) != expected:
            raise ValueError("the reconciliation read does not bind the original order or confirmation")
    # This records the operator's reconciliation. It changes no core journals
    # and grants no retry, payment, delivery or cart authority.
    record["reconciliation"] = {"checked_request_id": checked_id, "evidence": evidence.strip()}
    install.write_json(directory / f"{identifier}.json", record)
    return {"ok": True, "request_id": identifier, "status": "reconciliation_recorded"}, 0


def receipt_directory(state, *, create=False):
    from on_demand import _sync_directory

    directory = state / "foreground-calls"
    marker = state / ".foreground-receipts.json"
    binding = directory / "ledger.json"
    present = os.path.lexists(directory)
    if present:
        original_path(directory, directory=True)
    phase, ledger_id = None, None
    if os.path.lexists(marker):
        original_path(marker)
        metadata = install.bounded_json(marker)
        if not isinstance(metadata, dict):
            raise ValueError("original foreground receipt ledger metadata is invalid")
        phase, ledger_id = metadata.get("phase"), request_id(metadata.get("ledger_id", ""))
        if phase not in {"initializing", "ready"} or (phase == "ready" and not os.path.lexists(binding)):
            raise ValueError("original foreground receipt ledger is missing; recover it before continuing")
    if os.path.lexists(binding):
        original_path(binding)
        if not ledger_id or install.bounded_json(binding) != {"ledger_id": ledger_id}:
            raise ValueError("original foreground receipt ledger binding changed")
    if create and phase != "ready":
        if not ledger_id:
            ledger_id = str(uuid.uuid4())
            install.write_json(marker, {"phase": "initializing", "ledger_id": ledger_id})
        if not present:
            directory.mkdir(mode=0o700)
            _sync_directory(state)
        install.write_json(binding, {"ledger_id": ledger_id})
        install.write_json(marker, {"phase": "ready", "ledger_id": ledger_id})
    return directory


def verify_store(app):
    """Read the current signed-in cart and check the installed browser's account."""
    from core import cart_contents, HouseholdError

    deadline = time.monotonic() + 120
    if app.provider in {"oda", "mathem"} and app.integration.get("status") != "ready":
        raise HouseholdError("complete private retailer OAuth and check the connection first")
    with app._browser_operation(deadline):
        if app.provider == "meny":
            app.browser.verify_order_change(None, None, deadline=deadline)
        cart = app.provider_client.call("get_cart", {}, deadline=deadline)
        lines, _ = app._cart_lines(cart_contents(cart, require_quantities=True))
        count = cart.get("productQuantityCount", cart.get("count"))
        if type(count) is not int or count != sum(lines.values()):
            raise HouseholdError("the authenticated cart did not return complete quantities")
        if app.provider in {"oda", "mathem"}:
            addresses = app.provider_client.call("get_delivery_addresses", {}, deadline=deadline).get("result")
            selected = [row.get("address") for row in addresses
                        if isinstance(row, dict) and row.get("isSelected") is True] if isinstance(addresses, list) else []
            if len(selected) != 1 or not isinstance(selected[0], str) or not selected[0].strip():
                raise HouseholdError("select one existing retailer account delivery address before verifying the browser")
            app.browser.verify_connection_account(selected[0], deadline=deadline)
    return {"provider": app.provider, "authenticated_cart": True,
            **({"new_cart": True} if app.provider == "meny" else {"checkout_account_matches_oauth": True})}


def call(directory, identifier, request, outputs, make_application, build):
    from service import HouseholdError, MenyBrowserError, validate_request_value

    validate_request_value(request)
    binding = request_digest(request, outputs)
    record = read_receipt(directory, identifier)
    if record is not None:
        if record["input_sha256"] != binding:
            raise ValueError("request ID belongs to a different input; no command was replayed")
        return receipt_view(record, identifier)

    will_dispatch = may_dispatch(request)
    pending = unresolved(directory)
    if will_dispatch and pending is not None:
        return {"ok": False, "status": "reconciliation_required", "original_request_id": pending,
                "dispatched": False}, 1
    app = make_application()
    record = {"format": 1, "request_id": identifier, "input_sha256": binding,
              "operation": request["operation"], "action": request.get("action"),
              "issued_at": time.time_ns(),
              "target": {key: request[key] for key in TARGET_FIELDS & request.keys()},
              "build": {key: build.get(key) for key in ("source_commit", "release_tag", "source_modified")}}
    path = directory / f"{identifier}.json"
    install.write_json(path, record)
    if will_dispatch:
        install.write_json(directory / "merchant-in-flight.json", {"request_id": identifier})

    def send(operation, **arguments):
        try:
            if operation == "store_check":
                if set(arguments) - {"response_view"}:
                    raise ValueError("store_check accepts no store operation or account arguments")
                return verify_store(app)
            if operation == "menu":
                arguments.setdefault("interactive", True)
            result = app.handle({"operation": operation, **arguments})
            if (operation == "recipe_delivery" and arguments.get("action") == "read"
                    and "data_base64" in result and outputs.get("delivery_output") is None):
                raise ValueError("use --delivery-output to transfer a frozen attachment")
            return result
        except (HouseholdError, TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
            raise ServiceError(str(exc), outcome="unknown" if getattr(exc, "outcome_unknown", False) else None,
                               provider_failure=exc.provider_failure if isinstance(exc, MenyBrowserError) else None) from exc

    response, code = cli.perform(request, send, **outputs)
    if will_dispatch and not response["ok"] and response.get("dispatched") is not False:
        response.update(ServiceError(response["error"], outcome="unknown").outcome_fields())
    if len(json.dumps(response, allow_nan=False, ensure_ascii=False).encode()) > cli.MAX_INPUT:
        raise ValueError("foreground response exceeded its byte limit; inspect the original receipt")
    record.update(response=response, exit_code=code)
    install.write_json(path, record)
    return {**response, "request_id": identifier, "receipt_build": record["build"]}, code


def execute(options):
    home = options.home.expanduser().resolve(strict=True)
    original_path(home, directory=True)
    with file_lock(home / ".installer.lock"):
        meta = install.completed_manifest(home)
        code_root = Path(meta["code_root"])
        if (meta["manager"] != "external" or meta["home"] != str(home)
                or Path(meta["release"]).resolve() != SOURCE or (code_root / "current").resolve() != SOURCE
                or Path(sys.prefix).resolve() != SOURCE / "venv"):
            raise ValueError("use the selected external installation's current foreground runner and private home")
        if install.bounded_json(code_root / "owner.json").get("home") != str(home):
            raise ValueError("the selected program belongs to a different installation")
        verify_release_layout(SOURCE)
        import service

        build = service.BUILD_IDENTITY
        expected = meta.get("runtime_identity")
        if (build.get("identity_status") != "recorded" or not expected
                or any(build.get(key) != value for key, value in expected.items())):
            raise ValueError("running build failed installed runtime verification; inspect the original installation")
        settings = service.config(Path(meta["paths"]["config"]))
        tools = LOCAL_TOOLS | (STORE_TOOLS if settings["provider"] != "none" else frozenset())
        state = Path(meta["paths"]["state"])
        with install.data_ownership(meta):
            original_path(state, directory=True)
            original_path(state / "state.json")
            original_path(state / "recipes.sqlite3")
            directory = receipt_directory(state)
            if options.action == "tools":
                return {"ok": True, "provider": settings["provider"], "household": settings["household"],
                        "build": build, "operations": sorted(tools), "skill": str(SOURCE / "skill/SKILL.md"),
                        **({"products_actions": ["record_ingredients"]} if settings["provider"] == "none" else {}),
                        "unresolved_merchant_request_id": unresolved(directory)}, 0
            if options.action == "receipt":
                return receipt_view(read_receipt(directory, options.request_id), options.request_id)
            if options.action == "acknowledge":
                return acknowledge(directory, options.request_id, options.checked_receipt, options.evidence)

            request = cli.read_request(sys.stdin.buffer)
            operation = request["operation"]
            if operation not in tools or any(key.startswith("_") for key in request):
                raise ValueError("use a discovered public operation and public arguments only")
            if request.get("action") is not None and not isinstance(request["action"], str):
                raise ValueError("action must be text")
            if operation == "products" and settings["provider"] == "none" and request.get("action") != "record_ingredients":
                raise ValueError("the no-store products operation supports record_ingredients")
            request.setdefault("response_view", "agent")
            directory = receipt_directory(state, create=True)

            def make_application():
                from runtime_rollback import note_runtime_open

                service_options = service.parser().parse_args(install.service_args(meta)[4:])
                os.environ["PATH"] = meta["runtime_path"]
                service.validate_browser_prerequisites(service_options, settings)
                note_runtime_open(state)
                return service.build_application(service_options, settings)

            outputs = {"image_output": options.image_output, "delivery_output": options.delivery_output}
            return call(directory, options.request_id, request, outputs, make_application, build)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path, help="absolute private installation home")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("tools", help="show available operations, installed skill and build")
    for action in ("call", "receipt", "acknowledge"):
        command = subparsers.add_parser(action)
        command.add_argument("request_id", type=request_id)
        if action == "call":
            cli.output_options(command)
        if action == "acknowledge":
            command.add_argument("--checked-receipt", type=request_id, required=True, help="fresh read receipt UUID")
            command.add_argument("--evidence", required=True, help="verified reconciliation of this exact merchant call")
    try:
        value, code = execute(parser.parse_args())
    except (OSError, RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        value, code = {"ok": False, "error": str(exc)}, 1
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
