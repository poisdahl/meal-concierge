#!/usr/bin/env python3
"""One offline supplied-recipe/menu/PDF batch, with read-only result inspection."""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
from copy import deepcopy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

MAX_INPUT = 2 * 1024 * 1024
MAX_RECORD = 64 * 1024
MAX_PDF = 32 * 1024 * 1024
MAX_MENU = 8 * 1024 * 1024
MAX_COVERS = 24 * 1024 * 1024
CHUNK = 128 * 1024
GUIDANCE = "Offline export only; grocery actions, external sources and sending are unavailable."


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON number")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def _object(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError("unsupported or missing batch fields; " + GUIDANCE)


def _key(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value) is None:
        raise ValueError("request and recipe keys must be 1..80 safe characters")
    return value


def validate_batch(value):
    _object(value, {"request_id", "household", "recipes", "menu"}, {"request_id", "recipes", "menu"})
    _key(value["request_id"])
    household = value.get("household", "On-demand menu")
    if not isinstance(household, str) or not 1 <= len(household.strip()) <= 100:
        raise ValueError("household must be 1..100 characters")
    recipes, slots = value["recipes"], value["menu"]
    if not isinstance(recipes, list) or not 1 <= len(recipes) <= 14:
        raise ValueError("supply 1..14 recipes")
    keys = set()
    for entry in recipes:
        _object(entry, {"key", "recipe", "cover"}, {"key", "recipe"})
        key = _key(entry["key"])
        if key in keys:
            raise ValueError("recipe keys must be unique")
        keys.add(key)
        if not isinstance(entry["recipe"], dict):
            raise ValueError("recipe must be an object")
        if "cover" in entry and (not isinstance(entry["cover"], str) or not entry["cover"]):
            raise ValueError("cover must be a relative supplied file name")
    if not isinstance(slots, list) or not 1 <= len(slots) <= 14:
        raise ValueError("supply 1..14 dated menu entries")
    used, weeks = set(), set()
    for slot in slots:
        _object(slot, {"recipe", "date", "portions"}, {"recipe", "date", "portions"})
        key = _key(slot["recipe"])
        if key not in keys or key in used:
            raise ValueError("each menu entry must reference a different supplied recipe")
        used.add(key)
        day = slot["date"]
        if not isinstance(day, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
            raise ValueError("menu dates must be YYYY-MM-DD")
        calendar = date.fromisoformat(day).isocalendar()
        weeks.add((calendar.year, calendar.week))
        if type(slot["portions"]) is not int or not 1 <= slot["portions"] <= 100:
            raise ValueError("portions must be an integer from 1 to 100")
    if len(weeks) != 1:
        raise ValueError("one batch menu must fit within one ISO week")
    return value


def _private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("batch root must be a private owned directory, without symlinks")


def _read_file(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > maximum:
            raise ValueError("batch artifact must be a bounded owned regular file")
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("batch artifact exceeds its byte limit")
        return data


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(root, record):
    from core import _atomic_json
    if len(json.dumps(record, ensure_ascii=False).encode()) > MAX_RECORD:
        raise ValueError("batch result exceeds its byte limit")
    _atomic_json(root / "result.json", record)
    _sync_directory(root)


def _application(state, household):
    # Imports and construction are deliberately absent from inspect().
    from core import HouseholdError, StateStore
    from service import Application

    class UnavailableProvider:
        def probe(self, **kwargs):
            raise HouseholdError(GUIDANCE)

        def call(self, *args, **kwargs):
            raise HouseholdError(GUIDANCE)

    return Application(StateStore(state, {"household": household, "provider": "oda"}),
                       UnavailableProvider(), None, external_recipe_sources={})


def create(value, root, input_directory=None):
    value = validate_batch(value)
    # Only the fixed recipe/menu operations below can reach Application.handle.
    from core import HouseholdError
    from delivery_transport import export_chunks
    from recipe_assets import read_local_file
    from recipe_delivery import render_menu, render_pdf
    from runtime_ownership import file_lock

    covers, total = {}, 0
    for entry in value["recipes"]:
        if "cover" in entry:
            if input_directory is None:
                raise ValueError("supplied covers require --input-directory")
            data = read_local_file(Path(input_directory), entry["cover"])
            total += len(data)
            if total > MAX_COVERS:
                raise ValueError("supplied covers exceed the aggregate 24 MiB limit")
            covers[entry["key"]] = data
    bound_input = {"batch": value, "covers": {k: hashlib.sha256(v).hexdigest() for k, v in covers.items()}}
    input_digest = hashlib.sha256(json.dumps(bound_input, sort_keys=True, ensure_ascii=False,
                                             allow_nan=False).encode()).hexdigest()
    root = Path(root).absolute()
    root.mkdir(mode=0o700)  # Existing roots, including interrupted batches, are never adopted.
    _private_directory(root)
    _sync_directory(root.parent)
    record = {"format": 1, "status": "incomplete", "request_id": value["request_id"],
              "input_sha256": input_digest, "sent": False}
    _publish(root, record)
    state = root / "state"
    with ExitStack() as stack:
        for name in (".service-owner.lock", "recipes.sqlite3.owner.lock", "state.json.owner.lock"):
            stack.enter_context(file_lock(state / name))
        app = _application(state, value.get("household", "On-demand menu"))
        setup = app.handle({"operation": "setup", "action": "apply", "keep_current": True})
        if setup.get("configuration_required"):
            raise HouseholdError("batch setup requires additional information")
        saved = {}
        for entry in value["recipes"]:
            recipe = deepcopy(entry["recipe"])
            if entry["key"] in covers:
                image = recipe.get("image")
                if not isinstance(image, dict):
                    raise ValueError("a supplied cover requires recipe.image attribution metadata")
                image["asset_id"] = app.recipes.assets.import_bytes(covers[entry["key"]])
            result = app.handle({"operation": "recipes", "action": "save", "recipe": recipe,
                                 "idempotency_key": value["request_id"] + ":" + entry["key"]})
            saved[entry["key"]] = result["recipe"]
        calendar = date.fromisoformat(value["menu"][0]["date"]).isocalendar()
        menu = {"week": f"{calendar.year:04}-W{calendar.week:02}", "dishes": [], "schedule": []}
        for slot in value["menu"]:
            recipe = saved[slot["recipe"]]
            menu["dishes"].append({"recipe_ref": {"id": recipe["id"], "revision": recipe["revision"]},
                                   "portions": slot["portions"]})
            menu["schedule"].append({"day": slot["date"], "meal": recipe["name"], "portions": slot["portions"]})
        result = app.handle({"operation": "menu", "action": "save", "menu": menu})
        if not isinstance(result.get("menu"), dict):
            raise HouseholdError("batch menu requires additional information")
        snapshot = app.handle({"operation": "menu", "action": "get"})["menu"]
        # Freeze the materialized recipe quantities for service-free, read-only
        # planning. Later readers never reopen or initialize household state.
        from core import _atomic_json
        menu_bytes = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode()
        if len(menu_bytes) > MAX_MENU:
            raise ValueError("materialized menu exceeds its byte limit")
        _atomic_json(root / "menu.json", snapshot)
        menu_bytes = _read_file(root / "menu.json", MAX_MENU)
        rendered = render_menu(snapshot, app.recipes.assets)
        pdf = render_pdf(rendered)
        if not 0 < len(pdf) <= MAX_PDF:
            raise ValueError("PDF exceeds the supported 32 MiB bound")
        metadata = {"bytes": len(pdf), "sha256": hashlib.sha256(pdf).hexdigest(),
                    "content_type": "application/pdf", "filename": "export.pdf"}

        def read_chunk(offset):
            end = min(len(pdf), offset + CHUNK)
            return {**metadata, "offset": offset, "next_offset": end if end < len(pdf) else None,
                    "data_base64": base64.b64encode(pdf[offset:end]).decode("ascii")}

        artifact = export_chunks(read_chunk, root / "export.pdf")
        fd = os.open(root / "export.pdf", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if hashlib.sha256(_read_file(root / "export.pdf", MAX_PDF)).hexdigest() != metadata["sha256"]:
            raise ValueError("exported PDF verification failed")
        record.update(status="completed", artifact=artifact,
                      menu_snapshot={"filename": "menu.json", "bytes": len(menu_bytes),
                                     "sha256": hashlib.sha256(menu_bytes).hexdigest()},
                      menu_ref={key: snapshot[key] for key in ("menu_id", "revision", "digest")},
                      build=app.handle({"operation": "health"})["build"],
                      integration={"status": "unavailable", "message": GUIDANCE},
                      warnings=["Default household preferences were used; dietary needs were not confirmed.",
                                *rendered["warnings"]])
        _publish(root, record)
    return record


def inspect(root):
    root = Path(root).absolute()
    _private_directory(root)
    try:
        record = _json(_read_file(root / "result.json", MAX_RECORD))
    except FileNotFoundError:
        return {"status": "incomplete", "sent": False, "message": "No committed batch result; no replay performed."}
    if not isinstance(record, dict) or record.get("format") != 1 or record.get("sent") is not False:
        raise ValueError("invalid batch result")
    if record.get("status") == "incomplete":
        return record
    if record.get("status") != "completed":
        raise ValueError("unknown batch status")
    artifact = record.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("filename") != "export.pdf" or artifact.get("sent") is not False:
        raise ValueError("invalid batch PDF metadata")
    pdf = _read_file(root / "export.pdf", MAX_PDF)
    if not pdf.startswith(b"%PDF-") or len(pdf) != artifact.get("bytes") or hashlib.sha256(pdf).hexdigest() != artifact.get("sha256"):
        raise ValueError("completed batch PDF is missing or changed; no regeneration performed")
    if "menu_snapshot" in record:
        metadata = record["menu_snapshot"]
        if not isinstance(metadata, dict) or metadata.get("filename") != "menu.json":
            raise ValueError("invalid batch menu metadata")
        menu = _read_file(root / "menu.json", MAX_MENU)
        if len(menu) != metadata.get("bytes") or hashlib.sha256(menu).hexdigest() != metadata.get("sha256"):
            raise ValueError("completed batch menu is missing or changed; no regeneration performed")
    artifact["output_path"] = str(root / "export.pdf")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    batch = commands.add_parser("create")
    batch.add_argument("--root", required=True, type=Path)
    batch.add_argument("--input-directory", type=Path)
    read = commands.add_parser("inspect")
    read.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            data = sys.stdin.buffer.read(MAX_INPUT + 1)
            if len(data) > MAX_INPUT:
                raise ValueError("batch input exceeds 2 MiB")
            result = create(_json(data), args.root, args.input_directory)
        else:
            result = inspect(args.root)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
        return 0 if result.get("status") == "completed" else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "sent": False,
                          "next": "Preserve any batch root and use inspect; do not automatically replay."}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
