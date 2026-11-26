#!/usr/bin/env python3
"""Call one installed store-free household tool in a foreground process."""
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
from rpc_client import ServiceError
from runtime_ownership import file_lock


TOOLS = frozenset({"health", "status", "setup", "profile", "recipes", "menu", "shopping_list",
                   "feedback", "recipe_delivery", "recipe_pack", "schedule", "products"})
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
        return {**receipt, "ok": False, "status": "incomplete",
                "operation": record["operation"], "action": record["action"],
                "recovery": "Inspect the original operation before any retry."}, 1
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


def validate_request(request):
    if request["operation"] not in TOOLS or any(key.startswith("_") for key in request):
        raise ValueError("use a discovered public operation and public arguments only")
    action = request.get("action")
    if action is not None and not isinstance(action, str):
        raise ValueError("action must be text")
    if request["operation"] == "products" and action != "record_ingredients":
        raise ValueError("the no-store products operation supports record_ingredients")


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

    app = make_application()
    target = {key: request[key] for key in ("recipe_ref", "menu_ref", "job_id", "part_id", "request_id", "idempotency_key")
              if key in request}
    record = {"format": 1, "request_id": identifier, "input_sha256": digest,
              "operation": request["operation"], "action": request.get("action"), "target": target,
              "build": {key: build.get(key) for key in ("source_commit", "release_tag", "source_modified")}}
    path = directory / f"{identifier}.json"
    install.write_json(path, record)

    def send(operation, **arguments):
        try:
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


def execute(options):
    if options.action == "call":
        request = cli.read_request(sys.stdin.buffer)
        validate_request(request)
        request.setdefault("response_view", "agent")
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
        if options.action != "receipt" and settings["provider"] != "none":
            raise ValueError("the foreground runner requires an existing provider=none household")
        state = Path(meta["paths"]["state"])
        with install.data_ownership(meta):
            original_path(state, directory=True)
            original_path(state / "state.json")
            original_path(state / "recipes.sqlite3")
            directory = receipt_directory(state)
            if options.action == "receipt":
                return receipt_view(read_receipt(directory, options.request_id), options.request_id)
            if options.action == "tools":
                return {"ok": True, "provider": "none", "household": settings["household"], "build": build,
                        "operations": sorted(TOOLS), "products_actions": ["record_ingredients"],
                        "skill": str(SOURCE / "skill/SKILL.md")}, 0

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
    options = parser.parse_args()
    try:
        value, code = execute(options)
    except SystemExit:
        value, code = {"ok": False, "error": "installed household could not start; run install.sh doctor with the original home"}, 1
    except (OSError, RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        value, code = {"ok": False, "error": str(exc)}, 1
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
