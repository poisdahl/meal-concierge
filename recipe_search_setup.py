#!/usr/bin/env python3
"""Local, interactive setup and redacted status for optional recipe search APIs."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import stat

BACKENDS = ("direct", "host", "brave", "firecrawl")
GUIDE = "https://github.com/poisdahl/meal-concierge/blob/main/docs/recipe-search.md"
PROVIDERS = {
    "brave": "https://brave.com/search/api/",
    "firecrawl": "https://docs.firecrawl.dev/features/search",
}


def search_configuration(config):
    value = config.get("recipe_search", {"backend": "direct"})
    if (not isinstance(value, dict) or set(value) - {"backend", "api_key_file"}
            or not isinstance(value.get("backend"), str) or value["backend"] not in BACKENDS):
        raise ValueError("Optional recipe_search configuration is invalid; see recipe-search.md")
    if "api_key_file" in value:
        path = value["api_key_file"]
        if (value["backend"] not in PROVIDERS or not isinstance(path, str)
                or not path or not Path(path).is_absolute()):
            raise ValueError("Optional search API key requires an absolute secret-file path")
    return value


def validate_api_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError("Search API key must be nonempty printable ASCII without whitespace")
    return value


def load_search_key(config, backend):
    """Only the selected provider may use its configured secret. Never echo it."""
    selected = search_configuration(config)
    if selected["backend"] != backend:
        if backend == "firecrawl":
            return None  # Existing explicit anonymous Firecrawl remains supported.
        raise ValueError("Brave requires local API setup; see recipe-search.md")
    filename = selected.get("api_key_file")
    if filename is None:
        if backend == "firecrawl":
            return None
        raise ValueError("Brave requires a locally configured API key; see recipe-search.md")
    try:
        path = Path(filename)
        parent = path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError("private directory required")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("private file required")
            raw = handle.read(8193)
        if len(raw) > 8192:
            raise ValueError("oversized credential")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"api_key"}:
            raise ValueError("invalid credential")
        return validate_api_key(value["api_key"])
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Search API credential unavailable or invalid; use the local setup helper") from None


def search_status(config):
    result = {"choices": list(BACKENDS), "guide": GUIDE, "providers": PROVIDERS,
              "authentication_verified": False,
              "test": {"operation": "recipes", "action": "web_search", "query": "kikertgryte oppskrift"}}
    try:
        selected = search_configuration(config)
        backend = selected["backend"]
        result.update({"backend": backend, "credential_available": False,
                       "status": "host_search_required" if backend == "host" else "configured"})
        if backend in PROVIDERS:
            result["credential_available"] = load_search_key(config, backend) is not None
            result["status"] = "configured" if result["credential_available"] else "anonymous"
    except ValueError as exc:
        result.update({"status": "unavailable", "reason": str(exc)})
    result["note"] = "Configuration is not a successful live test. API searches share query/domain filters and may incur charges. No automatic fallback. Broad search requires web_search.broad=true."
    return result


def configure(config_path, backend, *, state_directory, anonymous=False):
    # Reuse the existing setup owner's serialization and durable private writes.
    from recipe_library_setup import _configuration_lock, _read_config, _private_directory, atomic_private_json
    with _configuration_lock(config_path):
        config = _read_config(config_path)
        selected = {"backend": backend}
        if backend in PROVIDERS and not anonymous:
            secrets = config_path.parent / "secrets"
            if secrets.resolve().is_relative_to(state_directory.resolve()):
                raise ValueError("Keep the config/secrets directory outside the backed-up state directory")
            if not os.isatty(0):
                raise ValueError("Run setup in a local interactive terminal; never pass keys through chat or arguments")
            key = validate_api_key(getpass.getpass(f"{backend} API key (hidden): "))
            # Outside the state directory: normal state/config backups contain no keys.
            _private_directory(secrets)
            path = secrets / "recipe-search" / f"{backend}.json"
            atomic_private_json(path, {"api_key": key})
            selected["api_key_file"] = str(path)
        config["recipe_search"] = selected
        atomic_private_json(config_path, config)
    return search_status(config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Existing installation config.json")
    parser.add_argument("--backend", choices=BACKENDS, help="Omit to show redacted status only")
    parser.add_argument("--state", type=Path, help="Actual state directory; required when configuring a key")
    parser.add_argument("--anonymous", action="store_true", help="Explicitly use Firecrawl without a key")
    args = parser.parse_args()
    if args.anonymous and args.backend != "firecrawl":
        parser.error("--anonymous requires --backend firecrawl")
    if args.backend in PROVIDERS and not args.anonymous and args.state is None:
        parser.error("--state requires the installation's actual state directory, to keep keys outside backups")
    try:
        path = args.config.expanduser().resolve(strict=True)
        if args.backend:
            result = configure(path, args.backend, state_directory=args.state, anonymous=args.anonymous)
            result["next"] = "Restart only this installation when idle, then run the returned test through its ordinary MCP/CLI connection. Broad settings are unchanged."
        else:
            from recipe_library_setup import _read_config
            result = search_status(_read_config(path))
        print(json.dumps(result, indent=2))
    except (ValueError, OSError):
        parser.exit(1, "Search setup failed; check the local config/permissions and run interactively. No credentials are printed.\n")


if __name__ == "__main__":
    main()
