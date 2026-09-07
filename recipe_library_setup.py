#!/usr/bin/env python3
"""Interactive local setup for optional personal recipe-library connections."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import getpass
import json
import os
from pathlib import Path
import stat
import subprocess
import sqlite3
import tempfile
from typing import Any, Mapping

from recipe_libraries import (
    MAX_SECRET_BYTES,
    RecipeLibraryError,
    load_library_secret,
    load_optional_adapter,
    normalize_library_configuration,
    retired_library_configuration,
    secret_path,
    validate_library_id,
    verified_capabilities,
)
from recipes import RecipeError, RecipeStore


def _read_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecipeLibraryError("household config is unavailable") from exc
    if not isinstance(value, dict):
        raise RecipeLibraryError("household config is invalid")
    normalize_library_configuration(value)
    return value


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    status = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
        raise RecipeLibraryError("private setup directory is invalid")
    os.chmod(path, 0o700)


def atomic_private_json(path: Path, value: object) -> None:
    if path.parent.name == "recipe-libraries":
        _private_directory(path.parent.parent)
    _private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _configuration_lock(config_path: Path):
    """Serialize setup helpers across prompts and all config/credential writes."""
    descriptor = os.open(
        config_path.with_name(f".{config_path.name}.lock"),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _prompt_credential() -> dict[str, Any]:
    raw = getpass.getpass("Credential JSON (hidden; never passed as a command argument): ")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecipeLibraryError("credential JSON is invalid") from exc
    if (
        not isinstance(value, dict)
        or not value
        or len(value) > 20
        or not all(isinstance(key, str) for key in value)
        or len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_SECRET_BYTES
    ):
        raise RecipeLibraryError("credential JSON must be a non-empty object")
    return value


def _confirm(expected: str) -> None:
    entered = input(f"Type exactly '{expected}' to confirm: ")
    if entered != expected:
        raise RecipeLibraryError("no change made")


def _probe(connection: Mapping[str, Any], credential: Mapping[str, Any]) -> dict[str, Any]:
    try:
        adapter = load_optional_adapter(connection, credential)
        capabilities = verified_capabilities(adapter, connection)
    except RecipeLibraryError:
        raise
    except Exception as exc:
        raise RecipeLibraryError("recipe library read-only probe failed") from exc
    if not capabilities["search"] or not capabilities["get"]:
        raise RecipeLibraryError("connection cannot meet the required read-only search/get contract")
    return capabilities


def _restart_running_service() -> None:
    if os.uname().sysname == "Darwin":
        label = os.environ.get("MEAL_CONCIERGE_LAUNCHD_LABEL", "com.hermes-agent.meal-concierge")
        target = f"gui/{os.getuid()}/{label}"
        status = subprocess.run(
            ["launchctl", "print", target],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if status.returncode != 0 or "state = running" not in status.stdout:
            return
        subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            check=True,
            stdin=subprocess.DEVNULL,
        )
    else:
        subprocess.run(
            ["systemctl", "--user", "try-restart", "meal-concierge.service"],
            check=True,
            stdin=subprocess.DEVNULL,
        )


def _restart_after_change(args: argparse.Namespace) -> None:
    if not getattr(args, "no_restart", False):
        _restart_running_service()


def _connection(config: Mapping[str, Any], library_id: str) -> dict[str, Any]:
    normalized = normalize_library_configuration(config)
    matches = [item for item in normalized["recipe_libraries"] if item["library_id"] == library_id]
    if len(matches) != 1:
        raise RecipeLibraryError("library_id must name one exact configured recipe library")
    return matches[0]


def _ensure_no_active_operations(state_directory: Path, library_id: str) -> None:
    database = state_directory / "recipes.sqlite3"
    if not database.exists():
        return
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0
        )
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_operations'"
            ).fetchone()
            if table is None:
                return
            active = connection.execute(
                "SELECT 1 FROM library_operations WHERE library_id=? "
                "AND status IN ('pending','uncertain') LIMIT 1",
                (library_id,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RecipeLibraryError("recipe library operation journal is unavailable") from exc
    if active is not None:
        raise RecipeLibraryError(
            "resolve pending or uncertain operations before removing this connection"
        )


def add_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    with _configuration_lock(args.config):
        return _add_connection(args, _read_config(args.config))


def _add_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    library_id = validate_library_id(args.library_id, allow_builtin=False)
    current = normalize_library_configuration(config)
    if library_id in {item["library_id"] for item in current["recipe_libraries"]}:
        raise RecipeLibraryError("library_id is already configured")
    allow_insecure = False
    if str(args.base_url).startswith("http://") and not any(
        marker in str(args.base_url).casefold() for marker in ("localhost", "127.0.0.1", "[::1]")
    ):
        _confirm(f"allow insecure HTTP for {library_id}")
        allow_insecure = True
    candidate = {
        "library_id": library_id,
        "provider": args.provider,
        "base_url": args.base_url,
        "read_only": True,
    }
    if args.display_name is not None:
        candidate["display_name"] = args.display_name
    if allow_insecure:
        candidate["allow_insecure_http"] = True
    proposed = dict(config)
    proposed["recipe_libraries"] = [*current["recipe_libraries"], candidate]
    normalized = normalize_library_configuration(proposed)
    checked = next(item for item in normalized["recipe_libraries"] if item["library_id"] == library_id)
    credential = _prompt_credential()
    capabilities = _probe(checked, credential)
    _confirm(f"add {library_id}")
    path = secret_path(args.home, library_id)
    if path.exists():
        raise RecipeLibraryError("credential file already exists; use update-credential")
    _private_directory(path.parent.parent)
    atomic_private_json(path, credential)
    config_written = False
    try:
        proposed["recipe_libraries"] = normalized["recipe_libraries"]
        proposed.setdefault("primary_recipe_library_id", current["primary_recipe_library_id"])
        atomic_private_json(args.config, proposed)
        config_written = True
        state_directory = getattr(args, "state_directory", None) or args.home / "state"
        RecipeStore(state_directory / "recipes.sqlite3", proposed["household"]).enable_library_connection(library_id)
    except Exception as exc:
        if config_written:
            atomic_private_json(args.config, config)
        path.unlink(missing_ok=True)
        if isinstance(exc, RecipeError):
            raise RecipeLibraryError(str(exc)) from exc
        raise
    _restart_after_change(args)
    return {"changed": True, "library_id": library_id, "primary": False, "capabilities": capabilities}


def test_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    library_id = validate_library_id(args.library_id, allow_builtin=False)
    connection = _connection(config, library_id)
    credential = load_library_secret(args.home, library_id)
    return {"changed": False, "library_id": library_id, "capabilities": _probe(connection, credential)}


def update_credential(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    with _configuration_lock(args.config):
        return _update_credential(args, _read_config(args.config))


def _update_credential(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    library_id = validate_library_id(args.library_id, allow_builtin=False)
    connection = _connection(config, library_id)
    credential = _prompt_credential()
    capabilities = _probe(connection, credential)
    _confirm(f"update credential {library_id}")
    path = secret_path(args.home, library_id)
    _private_directory(path.parent.parent)
    atomic_private_json(path, credential)
    _restart_after_change(args)
    return {"changed": True, "library_id": library_id, "capabilities": capabilities}


def _retirement_inventory(state_directory: Path, library_ids: list[str], household: str) -> dict[str, Any]:
    """Read retained local obligations without migration, cleanup or resolution."""
    counts = {library: {"pending_operations": 0, "uncertain_operations": 0,
                        "unfinished_migration_plans": 0, "migration_mappings": 0}
              for library in library_ids}
    result = {"scope": "local_journals_and_mappings", "bank_status": "absent", "libraries": counts}
    database = state_directory / "recipes.sqlite3"
    try:
        database.lstat()
    except FileNotFoundError:
        return result
    try:
        database = database.resolve(strict=True)
        # A read-only WAL connection can still create source-side SHM files.
        # Never checkpoint or recover the source to obtain this inventory.
        descriptor = os.open(database, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RecipeLibraryError("retirement inventory requires a regular recipe bank")
            header = os.read(descriptor, 100)
        finally:
            os.close(descriptor)
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise RecipeLibraryError("retirement inventory requires a checkpointed rollback-journal bank")
        if any(path.exists() and path.stat().st_size for path in (Path(str(database) + "-wal"), Path(str(database) + "-journal"))):
            raise RecipeLibraryError("retirement inventory requires an idle recipe bank")
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE key IN ('household','schema_version')"))
            if metadata.get("household") != household or metadata.get("schema_version") not in {"1", "2", "3", "4", "5", "6"}:
                raise RecipeLibraryError("retirement inventory household or schema differs")
            version = int(metadata["schema_version"])
            for library, values in counts.items():
                if version >= 3:
                    for status, count in connection.execute(
                            "SELECT status,count(*) FROM library_operations WHERE library_id=? AND status IN ('pending','uncertain') GROUP BY status", (library,)):
                        values[status + "_operations"] = count
                if version >= 5:
                    values["unfinished_migration_plans"] = connection.execute(
                        "SELECT count(*) FROM migration_plans p WHERE (json_extract(p.preview,'$.source_library_id')=? OR json_extract(p.preview,'$.destination_library_id')=?) "
                        "AND (NOT EXISTS (SELECT 1 FROM migration_items i WHERE i.plan_id=p.plan_id) OR EXISTS "
                        "(SELECT 1 FROM migration_items i WHERE i.plan_id=p.plan_id AND (COALESCE(json_extract(i.progress,'$.copy_status'),'')!='confirmed' "
                        "OR COALESCE(json_extract(i.progress,'$.metadata_status'),'')!='complete')))", (library, library)).fetchone()[0]
                if version >= 5:
                    values["migration_mappings"] = connection.execute(
                        "SELECT count(*) FROM migration_mappings WHERE source_library=? OR destination_library=?", (library, library)).fetchone()[0]
            result["bank_status"] = "ready"
            return result
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        raise RecipeLibraryError("retirement local obligation inventory is unavailable") from exc


def retire_external(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    """Retain source bindings and journals; change only stopped routing policy."""
    import install
    from runtime_ownership import file_lock

    home = args.home.expanduser().resolve()
    try:
        if not home.is_dir():
            raise RecipeLibraryError("retirement requires an existing installation home")
        with file_lock(home / ".installer.lock"):
            try:
                meta = json.loads((home / "runtime.json").read_text(encoding="utf-8"))
                paths = meta["paths"]
                if (meta.get("format") != 1 or Path(meta["home"]).resolve() != home
                        or meta.get("manager") not in {"systemd", "launchd"}
                        or not isinstance(meta.get("name"), str)
                        or any(not isinstance(paths[key], str) or not Path(paths[key]).is_absolute()
                               for key in ("config", "state", "browser_profile", "browser_home", "browser_socket_directory", "socket"))
                        or Path(paths["config"]).resolve() != args.config.resolve()
                        or args.state_directory is not None and Path(paths["state"]).resolve() != args.state_directory.resolve()):
                    raise ValueError()
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                raise RecipeLibraryError("retirement requires a matching runtime.json configuration and state") from exc
            config_path = Path(paths["config"]).resolve()
            with install.offline(meta), _configuration_lock(config_path):
                current = _read_config(config_path)
                proposed = {**current, **retired_library_configuration(current)}
                retained = [c["library_id"] for c in proposed["recipe_libraries"] if c["library_id"] != "builtin"]
                inventory = _retirement_inventory(Path(paths["state"]), retained, current.get("household"))
                changed = proposed != current
                if changed:
                    atomic_private_json(config_path, proposed)
                return {"changed": changed, "primary_recipe_library_id": "builtin",
                        "retained_read_only_libraries": retained, "retained_obligations": inventory,
                        "migration_complete": False,
                        "next_action": "Inspect retained sources and copy or reconcile outstanding work before considering connection removal.",
                        "service_started": False}
    except RecipeLibraryError:
        raise
    except RuntimeError as exc:
        raise RecipeLibraryError("retirement requires a stopped installation with available ownership locks") from exc
    except OSError as exc:
        raise RecipeLibraryError("retirement configuration or installation is unavailable") from exc


def remove_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    with _configuration_lock(args.config):
        return _remove_connection(args, _read_config(args.config))


def _remove_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    library_id = validate_library_id(args.library_id, allow_builtin=False)
    normalized = normalize_library_configuration(config)
    _connection(config, library_id)
    if normalized["primary_recipe_library_id"] == library_id:
        raise RecipeLibraryError("retire the external primary before removing this connection")
    state_directory = getattr(args, "state_directory", None) or args.home / "state"
    _ensure_no_active_operations(state_directory, library_id)
    _confirm(f"remove {library_id} and its credential")
    recipe_store = RecipeStore(state_directory / "recipes.sqlite3", config["household"])
    try:
        recipe_store.disable_library_connection(library_id)
    except RecipeError as exc:
        raise RecipeLibraryError(str(exc)) from exc
    original_config = deepcopy(config)
    config["recipe_libraries"] = [
        item for item in normalized["recipe_libraries"] if item["library_id"] != library_id
    ]
    config.update(normalize_library_configuration(config))
    try:
        atomic_private_json(args.config, config)
    except Exception:
        recipe_store.enable_library_connection(library_id)
        raise
    try:
        secret_path(args.home, library_id).unlink(missing_ok=True)
    except OSError as exc:
        atomic_private_json(args.config, original_config)
        try:
            recipe_store.enable_library_connection(library_id)
        except RecipeError as rollback_exc:
            raise RecipeLibraryError(
                "credential removal failed and journal rollback is unavailable"
            ) from rollback_exc
        raise RecipeLibraryError("credential removal failed; no change made") from exc
    _restart_after_change(args)
    return {"changed": True, "library_id": library_id, "removed": True}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Configure optional Meal Concierge recipe libraries locally")
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--home", type=Path, required=True)
    result.add_argument(
        "--state-directory", type=Path,
        help="Recipe state directory; defaults to HOME/state",
    )
    result.add_argument(
        "--no-restart", action="store_true",
        help="Do not restart a user service; use only when an approved external supervisor restart follows",
    )
    commands = result.add_subparsers(dest="action", required=True)
    add = commands.add_parser("add")
    add.add_argument("--library-id", required=True)
    add.add_argument("--provider", choices=("mealie", "recipesage"), required=True)
    add.add_argument("--base-url", required=True)
    add.add_argument("--display-name")
    commands.add_parser("retire-external", help="Use builtin for new writes and retain external readers; requires a stopped installation")
    for action in ("test", "update-credential", "remove"):
        command = commands.add_parser(action)
        command.add_argument("--library-id", required=True)
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        config = _read_config(args.config)
        handler = {
            "add": add_connection,
            "test": test_connection,
            "update-credential": update_credential,
            "remove": remove_connection,
            "retire-external": retire_external,
        }[args.action]
        result = handler(args, config)
    except RecipeLibraryError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
