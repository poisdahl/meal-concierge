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
import uuid

SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE))

import cli
import install
from build_identity import verify_release_layout
from rpc_client import ServiceError, merchant_mutation
from runtime_ownership import file_lock


LOCAL_TOOLS = frozenset({"health", "status", "setup", "profile", "recipes", "menu",
                         "shopping_list", "feedback", "recipe_delivery", "recipe_pack", "schedule"})
STORE_TOOLS = frozenset({"catalog", "products", "cart", "delivery", "orders", "checkout",
                         "recurring", "product_favorites"})
RECEIPT_LIMIT = 8 * 1024 * 1024


def request_id(value):
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except ValueError:
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
    if record.get("request_id") != identifier or record.get("format") != 1:
        raise ValueError("foreground receipt does not match its original request ID")
    return record


def receipt_view(record, identifier):
    if record is None:
        return {"ok": False, "status": "not_found", "request_id": identifier}, 1
    if "response" not in record:
        return {"ok": False, "status": "incomplete", "request_id": identifier,
                "operation": record["operation"], "action": record["action"],
                "recovery": "Inspect and reconcile the original operation before retrying. No command was replayed."}, 1
    return {**record["response"], "request_id": identifier, "replayed": True}, record["exit_code"]


def may_dispatch(request):
    operation, action = request["operation"], request.get("action")
    return merchant_mutation(operation, action) or (
        operation == "checkout" and action in {None, "prepare", "renew_review"}
    ) or (operation == "orders" and action == "remove_prepare")


def uncertain(response):
    result = response.get("result") or {}
    return (response.get("outcome") == "unknown" or result.get("outcome") == "unknown"
            or result.get("outcome_unknown") is True or result.get("status") == "outcome_unknown")


def unresolved(directory):
    marker = directory / "merchant-in-flight.json"
    if not os.path.lexists(marker):
        return None
    original_path(marker)
    identifier = request_id(install.bounded_json(marker)["request_id"])
    record = read_receipt(directory, identifier)
    if record is None:
        raise ValueError("the original merchant receipt is missing; recover it before another write")
    if record.get("reconciliation") or ("response" in record and not uncertain(record["response"])):
        return None
    return identifier


def acknowledge(directory, identifier, evidence):
    record = read_receipt(directory, identifier)
    if not record or unresolved(directory) != identifier:
        raise ValueError("no unresolved merchant call matches this request ID")
    if not 1 <= len(evidence.strip()) <= 500:
        raise ValueError("record bounded evidence of the original operation's verified outcome")
    # This records the operator's reconciliation. It changes no core journals
    # and grants no retry, payment, delivery or cart authority.
    record["reconciliation"] = evidence.strip()
    install.write_json(directory / f"{identifier}.json", record)
    return {"ok": True, "request_id": identifier, "status": "reconciliation_recorded"}, 0


def call(directory, identifier, request, outputs, make_application, build):
    from on_demand import _sync_directory
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
    if not directory.exists():
        directory.mkdir(mode=0o700)
        _sync_directory(directory.parent)
    record = {"format": 1, "request_id": identifier, "input_sha256": binding,
              "operation": request["operation"], "action": request.get("action"),
              "build": {key: build.get(key) for key in ("source_commit", "release_tag", "source_modified")}}
    path = directory / f"{identifier}.json"
    install.write_json(path, record)
    if will_dispatch:
        install.write_json(directory / "merchant-in-flight.json", {"request_id": identifier})

    def send(operation, **arguments):
        try:
            return app.handle({"operation": operation, **arguments})
        except (HouseholdError, TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
            raise ServiceError(str(exc), outcome="unknown" if getattr(exc, "outcome_unknown", False) else None,
                               provider_failure=exc.provider_failure if isinstance(exc, MenyBrowserError) else None) from exc

    response, code = cli.perform(request, send, **outputs)
    if len(json.dumps(response, allow_nan=False, ensure_ascii=False).encode()) > cli.MAX_INPUT:
        raise ValueError("foreground response exceeded its byte limit; inspect the original receipt")
    record.update(response=response, exit_code=code)
    install.write_json(path, record)
    return {**response, "request_id": identifier}, code


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
            directory = state / "foreground-calls"
            if os.path.lexists(directory):
                original_path(directory, directory=True)
            if options.action == "tools":
                return {"ok": True, "provider": settings["provider"], "household": settings["household"],
                        "build": build, "operations": sorted(tools), "skill": str(SOURCE / "skill/SKILL.md"),
                        "unresolved_merchant_request_id": unresolved(directory)}, 0
            if options.action == "receipt":
                return receipt_view(read_receipt(directory, options.request_id), options.request_id)
            if options.action == "acknowledge":
                return acknowledge(directory, options.request_id, options.evidence)

            request = cli.read_request(sys.stdin.buffer)
            if request["operation"] not in tools or any(key.startswith("_") for key in request):
                raise ValueError("use a discovered public operation and public arguments only")
            if request["operation"] == "recipe_delivery" and request.get("action") == "read" and not options.delivery_output:
                raise ValueError("recipe_delivery read requires --delivery-output to transfer the frozen part")
            request.setdefault("response_view", "agent")

            def make_application():
                from runtime_rollback import note_runtime_open

                service_options = service.parser().parse_args(install.service_args(meta)[4:])
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
            command.add_argument("--evidence", required=True, help="verified reconciliation of this exact merchant call")
    try:
        value, code = execute(parser.parse_args())
    except (OSError, RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        value, code = {"ok": False, "error": str(exc)}, 1
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
