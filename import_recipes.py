#!/usr/bin/env python3
"""Bounded native JSON/JSONL import for one household recipe bank."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from recipes import MAX_IMPORT_RECORDS, MAX_RECIPE_BYTES, RecipeError, RecipeStore
from recipe_assets import RecipeAssetError, read_local_file, sanitize_image


MAX_IMPORT_BYTES = 64 * 1024 * 1024


def _json_records(path: Path) -> Iterator[Any]:
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise RecipeError("import file is too large")
    if path.suffix.casefold() == ".jsonl":
        count = 0
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    if len(line.encode()) > MAX_RECIPE_BYTES * 2:
                        raise RecipeError(f"JSONL line {line_number} is too large")
                    try:
                        value = json.loads(line)
                    except (ValueError, RecursionError) as exc:
                        raise RecipeError(f"JSONL line {line_number} is invalid") from exc
                    if not isinstance(value, dict):
                        raise RecipeError(f"JSONL line {line_number} is invalid")
                    count += 1
                    if count > MAX_IMPORT_RECORDS:
                        raise RecipeError(f"import exceeds {MAX_IMPORT_RECORDS} recipes")
                    yield value
        except (OSError, UnicodeDecodeError) as exc:
            raise RecipeError("JSONL import is unreadable or invalid") from exc
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RecipeError("JSON import is unreadable or invalid") from exc
    values = value if isinstance(value, list) else [value]
    if len(values) > MAX_IMPORT_RECORDS:
        raise RecipeError(f"import exceeds {MAX_IMPORT_RECORDS} recipes")
    yield from values


def main() -> None:
    parser = argparse.ArgumentParser(description="Import native Hermes Recipe JSON or JSONL")
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--image", help="import one explicitly supplied relative image attachment")
    parser.add_argument("--import-root", type=Path, help="trusted local root containing that image attachment")
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", choices=("active", "draft"), default="active")
    parser.add_argument("--backup", type=Path, help="write a verified SQLite backup before a committed import")
    args = parser.parse_args()
    if args.image is not None:
        if args.path is not None or args.import_root is None or args.backup is not None:
            parser.error("--image requires --import-root and cannot be combined with a recipe path or --backup")
    elif args.path is None or args.import_root is not None:
        parser.error("supply a recipe path, or --image with --import-root")
    state_path = args.state_directory / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise SystemExit("state directory has no readable household state") from exc
    household = state.get("household") if isinstance(state, dict) else None
    if not isinstance(household, str) or not household:
        raise SystemExit("state directory has no household identity")
    store = RecipeStore(args.state_directory / "recipes.sqlite3", household)
    try:
        if args.image is not None:
            if args.dry_run:
                rendition = sanitize_image(read_local_file(args.import_root, args.image))
                asset_id = "sha256:" + hashlib.sha256(rendition).hexdigest()
            else:
                asset_id = store.assets.import_file(args.import_root, args.image)
            print(json.dumps({"asset_id": asset_id, "dry_run": args.dry_run}, sort_keys=True))
            return
        backup = None
        if args.backup:
            if args.dry_run:
                raise RecipeError("--backup is not used with --dry-run")
            backup = str(store.backup(args.backup))
        result = store.import_records(_json_records(args.path), dry_run=args.dry_run, default_status=args.status, provider=state.get("provider"))
    except (OSError, RecipeError, RecipeAssetError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({**result, "backup": backup}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
