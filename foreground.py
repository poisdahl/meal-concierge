#!/usr/bin/env python3
"""Call one installed household tool in a foreground process."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
from rpc_client import ServiceError
from runtime_ownership import file_lock


TOOLS = frozenset({"health", "status", "setup", "profile", "recipes", "menu", "shopping_list",
                   "feedback", "recipe_delivery", "recipe_pack", "schedule", "products", "catalog"})
RECEIPT_LIMIT = 8 * 1024 * 1024


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
            result.update(dispatch=False, next="Reconcile the original native dispatch using its token.")
        return result
    return value


def output_matches(result):
    from recipe_assets import ASSET_ID

    expected = result.get("sha256")
    if expected is None and result.get("image_status") == "available" and result.get("content_type") == "image/jpeg":
        asset_id = (result.get("image") or {}).get("asset_id")
        match = ASSET_ID.fullmatch(asset_id) if isinstance(asset_id, str) else None
        expected = match[1] if match else None
    fd = os.open(result["output_path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_size == result.get("bytes") and 0 < info.st_size <= 32 * 1024 * 1024
                and hashlib.file_digest(file, "sha256").hexdigest() == expected)


def receipt_view(record, identifier):
    if record is None:
        return {"ok": False, "status": "not_found", "request_id": identifier}, 1
    receipt = {"request_id": identifier, "receipt_build": record["build"], "replayed": True}
    if record.get("target"):
        receipt["original_target"] = record["target"]
    if "response" not in record:
        if record.get("hosted_read"):
            from hosted_oda import response_time

            if datetime.now(timezone.utc) >= response_time(record["hosted_read"]["expires_at"]):
                return {**receipt, "ok": False, "status": "hosted_read_expired",
                        "recovery": "Use a new request ID for a fresh read."}, 1
        return {**receipt, "ok": False, "status": "incomplete",
                "operation": record["operation"], "action": record["action"],
                "recovery": "Inspect the original operation before any retry."}, 1
    response = replay_value(record["response"])
    result = response.get("result") or {}
    if record.get("operation") == "catalog" and result.get("scope") == "public_catalog":
        return {**response, **receipt, "status": "historical_public_catalog"}, record["exit_code"]
    if result.get("output_path"):
        try:
            if output_matches(result):
                return {**response, **receipt}, record["exit_code"]
        except OSError:
            pass
        return {**receipt, "ok": False, "status": "output_unavailable", "historic_file": result,
                "recovery": "Export the same frozen part to a new path with a new read-only request."}, 1
    return {**response, **receipt}, record["exit_code"]


def validate_request(request):
    if request["operation"] not in TOOLS or any(key.startswith("_") for key in request):
        raise ValueError("use a discovered public operation and public arguments only")
    action = request.get("action")
    if action is not None and not isinstance(action, str):
        raise ValueError("action must be text")
    if request["operation"] == "products" and action != "record_ingredients":
        raise ValueError("the no-store products operation supports record_ingredients")
    if request["operation"] == "catalog":
        from meny_public import validate

        validate(request)


def call(directory, identifier, request, outputs, make_application, build):
    from service import HouseholdError, validate_request_value

    validate_request(request)
    validate_request_value(request)
    binding = {"request": request, "outputs": {name: str(path.absolute()) for name, path in outputs.items() if path}}
    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    record = read_receipt(directory, identifier)
    if record is not None:
        if record["input_sha256"] != digest:
            raise ValueError("request ID belongs to a different input")
        return receipt_view(record, identifier)

    public_catalog = request["operation"] == "catalog"
    app = None if public_catalog else make_application()
    target = {key: request[key] for key in ("recipe_ref", "menu_ref", "job_id", "part_id", "request_id", "idempotency_key")
              if key in request}
    record = {"format": 1, "request_id": identifier, "input_sha256": digest,
              "operation": request["operation"], "action": request.get("action"), "target": target,
              "build": {key: build.get(key) for key in ("source_commit", "release_tag", "source_modified")}}
    path = directory / f"{identifier}.json"
    install.write_json(path, record)

    def send(operation, **arguments):
        try:
            if public_catalog:
                from meny_public import read

                return read({"operation": operation, **arguments})
            if operation == "menu" or (operation == "recipes" and arguments.get("action") == "discover"):
                arguments.setdefault("interactive", True)
            result = app.handle({"operation": operation, **arguments})
            if (operation == "recipe_delivery" and arguments.get("action") == "read"
                    and "data_base64" in result and outputs.get("delivery_output") is None):
                raise ValueError("use --delivery-output to transfer a frozen attachment")
            return result
        except (HouseholdError, TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
            raise ServiceError(str(exc), outcome="unknown" if getattr(exc, "outcome_unknown", False) else None) from exc

    response, code = cli.perform(request, send, **outputs)
    if len(json.dumps(response, allow_nan=False, ensure_ascii=False).encode()) > cli.MAX_INPUT:
        raise ValueError("foreground response exceeded its byte limit; inspect the original receipt")
    record.update(response=response, exit_code=code)
    install.write_json(path, record)
    return {**response, "request_id": identifier, "receipt_build": record["build"]}, code


def hosted_call(directory, identifier, request, plugin, build, *, selection=None):
    import hosted_oda

    tool, arguments = selection or hosted_oda.tool_request(request)
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()
    record = read_receipt(directory, identifier)
    if record is not None:
        if record.get("hosted_read", {}).get("plugin_id") != plugin:
            raise ValueError("request does not belong to this hosted Oda installation")
        expected = record.get("input_sha256", digest if request == hosted_oda.CART_REQUEST else None)
        if digest != expected:
            raise ValueError("request ID belongs to a different input")
        return receipt_view(record, identifier)
    frame = hosted_oda.issue(plugin, tool, arguments)
    binding = {key: value for key, value in frame.items() if key != "arguments"}
    binding["arguments_sha256"] = hosted_oda.arguments_digest(arguments)
    record = {"format": 1, "request_id": identifier, "operation": request["operation"], "action": request["action"],
              "input_sha256": digest,
              "hosted_read": binding, "runtime_sha256": build["installed_runtime_sha256"],
              "source_sha256": build["source_sha256"],
              "build": {key: build.get(key) for key in ("source_commit", "release_tag", "source_modified")}}
    install.write_json(directory / f"{identifier}.json", record)
    return {"ok": True, "status": "awaiting_hosted_read", f"{request['operation']}_read": False, "hosted_request": frame,
            "request_id": identifier, "receipt_build": record["build"]}, 0


def hosted_complete(directory, identifier, reply, plugin, build):
    import hosted_oda

    record = read_receipt(directory, identifier)
    frame = (record or {}).get("hosted_read", {})
    if (not record or frame.get("plugin_id") != plugin
            or record.get("runtime_sha256") != build["installed_runtime_sha256"]
            or record.get("source_sha256") != build["source_sha256"]
            or record["build"].get("source_commit") != build.get("source_commit")):
        raise ValueError("use the original hosted Oda request and its installed build")
    if "response" in record:
        raise ValueError("the original hosted Oda read already ended; use receipt to inspect it")
    response = hosted_oda.complete(frame, reply)
    ending = {**response, "request_id": identifier, "receipt_build": record["build"]}
    if len(json.dumps(ending, ensure_ascii=False, allow_nan=False).encode()) > cli.MAX_INPUT:
        raise ValueError("hosted Oda response exceeded the output limit")
    code = 0 if response["ok"] else 1
    if code == 0:
        receipt = {"ok": True, "status": "historical_" + response["status"], "observed_at": response["observed_at"],
                   "evidence": response["evidence"], "next": "Make a new request for a current result."}
    else:
        receipt = response
    frame.pop("arguments", None)
    record.update(response=receipt, exit_code=code)
    install.write_json(directory / f"{identifier}.json", record)
    return ending, code


def execute(options):
    if options.action == "call":
        request = cli.read_request(sys.stdin.buffer)
        request.setdefault("response_view", "agent")
    elif options.action in {"complete-hosted", "hosted-cart"}:
        from on_demand import _json

        data = sys.stdin.buffer.read(cli.MAX_INPUT + 1)
        if len(data) > cli.MAX_INPUT:
            raise ValueError("hosted Oda reply exceeds the size limit")
        reply = _json(data)
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
        plugin = install.hosted_oda_plugin(meta, settings)
        if options.action != "receipt" and settings["provider"] != "none" and plugin is None:
            raise ValueError("the foreground runner requires store-free or hosted Oda installation")
        if options.action == "complete-hosted" and plugin is None:
            raise ValueError("complete-hosted requires a hosted Oda installation")
        if options.action == "hosted-cart" and plugin is None:
            raise ValueError("hosted-cart requires a hosted Oda installation")
        if options.action == "call" and plugin is None:
            validate_request(request)
        state = Path(meta["paths"]["state"])
        with install.data_ownership(meta):
            original_path(state, directory=True)
            original_path(state / "state.json")
            original_path(state / "recipes.sqlite3")
            directory = receipt_directory(state)
            if options.action == "receipt":
                return receipt_view(read_receipt(directory, options.request_id), options.request_id)
            if options.action == "tools":
                if plugin is not None:
                    from hosted_oda import TOOLS as ODA_TOOLS

                    return {"ok": True, "provider": "oda", "household": settings["household"], "build": build,
                            "transport": "hosted_mcp", "plugin_id": plugin, "operations": sorted(ODA_TOOLS),
                            **{f"{operation}_actions": sorted(actions) for operation, actions in ODA_TOOLS.items()},
                            "cart_write_command": "hosted-cart",
                            "menu_shopping_command": "hosted-cart",
                            "menu_shopping_start": "begin_menu",
                            "skill": str(SOURCE / "skill/SKILL.md")}, 0
                from meny_public import ACTIONS as MENY_CATALOG_ACTIONS

                return {"ok": True, "provider": "none", "household": settings["household"], "build": build,
                        "operations": sorted(TOOLS), "products_actions": ["record_ingredients"],
                        "catalog": {"provider": "meny", "scope": "public_catalog", "actions": MENY_CATALOG_ACTIONS},
                        "skill": str(SOURCE / "skill/SKILL.md")}, 0

            if options.action == "complete-hosted":
                return hosted_complete(directory, options.request_id, reply, plugin, build)
            if options.action == "hosted-cart":
                import hosted_oda_cart

                directory = hosted_oda_cart.workspace_registry(state)
                return hosted_oda_cart.run(state, directory, options.request_id, reply, plugin, build), 0
            if plugin is not None:
                import hosted_oda

                if options.image_output or options.delivery_output:
                    raise ValueError("hosted Oda foreground reads have no file outputs")
                selection = hosted_oda.tool_request(request)
                directory = receipt_directory(state, create=True)
                return hosted_call(directory, options.request_id, request, plugin, build, selection=selection)
            directory = receipt_directory(state, create=True)

            def make_application():
                from runtime_rollback import note_runtime_open

                service_options = service.parser().parse_args(install.service_args(meta)[4:])
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
    command = subparsers.add_parser("call")
    command.add_argument("request_id", type=request_id)
    cli.output_options(command)
    subparsers.add_parser("receipt").add_argument("request_id", type=request_id)
    subparsers.add_parser("complete-hosted").add_argument("request_id", type=request_id)
    cart = subparsers.add_parser("hosted-cart", help="prepare and apply an approved hosted Oda cart operation")
    cart.add_argument("request_id", type=request_id)
    options = parser.parse_args()
    try:
        value, code = execute(options)
    except SystemExit:
        value, code = {"ok": False, "error": "installed household could not start; run install.sh doctor with the original home"}, 1
    except (OSError, RuntimeError, ValueError, RecursionError, argparse.ArgumentTypeError) as exc:
        value, code = {"ok": False, "error": str(exc)}, 1
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
