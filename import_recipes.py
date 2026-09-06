#!/usr/bin/env python3
"""Native recipe imports and explicitly offline private export and restore."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
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


def _household_state(state_directory: Path) -> dict:
    try:
        state = json.loads((state_directory / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise RecipeError("state directory has no readable household state") from exc
    household = state.get("household") if isinstance(state, dict) else None
    if not isinstance(household, str) or not household:
        raise RecipeError("state directory has no household identity")
    return state


@contextmanager
def _offline_store(args):
    import install
    from runtime_ownership import file_lock

    home = args.installation_home.expanduser().resolve()
    if not home.is_dir():
        raise RecipeError("private recipe operations require an existing installation home")
    with file_lock(home / ".installer.lock"):
        try:
            meta = json.loads((home / "runtime.json").read_text(encoding="utf-8"))
            paths = meta["paths"]
            if (meta.get("format") != 1 or Path(meta["home"]).resolve() != home
                    or meta.get("manager") not in {"systemd", "launchd"}
                    or not isinstance(meta.get("name"), str)
                    or any(not isinstance(paths[key], str) or not Path(paths[key]).is_absolute()
                           for key in ("state", "browser_profile", "browser_home", "browser_socket_directory", "socket"))):
                raise ValueError()
            state_directory = Path(paths["state"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise RecipeError("private recipe operations require a readable matching runtime.json installation manifest") from exc
        if args.state_directory is not None and args.state_directory.resolve() != state_directory.resolve():
            raise RecipeError("--state-directory differs from the installation manifest")
        # The calling process retains both installer and runtime lifetime
        # ownership. It never stops, restarts or adopts an existing service.
        with install.offline(meta):
            state = _household_state(state_directory)
            yield RecipeStore(state_directory / "recipes.sqlite3", state["household"])


def _export_private(args) -> dict:
    from recipe_portable import export_private_archive
    with _offline_store(args) as store:
        manifest = export_private_archive(args.export_private, store)
    return {"exported": True, "kind": "private", "destination": str(args.export_private),
            "recipes_count": manifest["recipes_count"], "records_count": manifest["records_count"]}


def _restore_private(args) -> dict:
    from recipe_portable import restore_private_archive
    with _offline_store(args) as store:
        return restore_private_archive(args.restore_private, store)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import native recipe JSON/JSONL or export/restore private recipes offline")
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--image", help="import one explicitly supplied relative image attachment")
    parser.add_argument("--import-root", type=Path, help="trusted local root containing that image attachment")
    parser.add_argument("--state-directory", type=Path)
    private_mode = parser.add_mutually_exclusive_group()
    private_mode.add_argument("--export-private", type=Path, help="write a new private recipe/history archive from a stopped installation")
    private_mode.add_argument("--restore-private", type=Path, help="restore exact private recipe history into a stopped installation without overwriting conflicts")
    parser.add_argument("--installation-home", type=Path, help="explicit installation home containing runtime.json; required for private export/restore")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", choices=("active", "draft"))
    parser.add_argument("--backup", type=Path, help="write a verified SQLite backup before a committed import")
    args = parser.parse_args()
    if args.export_private is not None or args.restore_private is not None:
        operation = "export" if args.export_private is not None else "restore"
        if (args.installation_home is None or args.path is not None or args.image is not None
                or args.import_root is not None or args.backup is not None or args.dry_run or args.status is not None):
            parser.error("private export/restore requires --installation-home and cannot be combined with import/image/backup/dry-run/status options")
        try:
            result = _export_private(args) if operation == "export" else _restore_private(args)
        except (RecipeError, RecipeAssetError) as exc:
            raise SystemExit(str(exc)) from exc
        except RuntimeError as exc:
            raise SystemExit(f"private {operation} requires a stopped installation with available ownership locks") from exc
        except OSError as exc:
            raise SystemExit(f"private {operation} input or destination is unavailable") from exc
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if operation == "restore" and result["status"] != "complete":
            raise SystemExit(1)
        return
    if args.installation_home is not None or args.state_directory is None:
        parser.error("normal imports require --state-directory; --installation-home is only used with private export/restore")
    if args.image is not None:
        if args.path is not None or args.import_root is None or args.backup is not None:
            parser.error("--image requires --import-root and cannot be combined with a recipe path or --backup")
    elif args.path is None or args.import_root is not None:
        parser.error("supply a recipe path, or --image with --import-root")
    try:
        state = _household_state(args.state_directory)
    except RecipeError as exc:
        raise SystemExit(str(exc)) from exc
    store = RecipeStore(args.state_directory / "recipes.sqlite3", state["household"])
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
        result = store.import_records(_json_records(args.path), dry_run=args.dry_run, default_status=args.status or "active", provider=state.get("provider"))
    except (OSError, RecipeError, RecipeAssetError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({**result, "backup": backup}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
