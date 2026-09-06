#!/usr/bin/env python3
"""Build an owner-local Codex or Claude Code plugin for an existing service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PRODUCT = Path(__file__).resolve().parents[1]
NAME = "meal-concierge"
VERSION = "0.1.0"
TOOL_TIMEOUT_SECONDS = 700


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def build(client: str, home: Path, output: Path) -> Path:
    """Resolve attachment through the installer; never start or modify the core."""
    if client not in {"codex", "claude-code"}:
        raise ValueError("client must be codex or claude-code")
    attached = subprocess.run(
        [sys.executable, "-I", str(PRODUCT / "install.py"), "attach", "--home", str(home)],
        text=True, capture_output=True, check=True,
    )
    attachment = json.loads(attached.stdout)
    skill = Path(attachment["skill"])
    # Read the release's maintained instruction before creating the destination.
    skill_text = skill.read_text()
    server = {key: attachment[key] for key in ("command", "args", "env")}
    if client == "codex":
        server.update(startup_timeout_sec=20, tool_timeout_sec=TOOL_TIMEOUT_SECONDS)
    else:
        server["timeout"] = TOOL_TIMEOUT_SECONDS * 1000
    identity = json.dumps({"server": server, "skill": skill_text}, sort_keys=True).encode()
    manifest = {
        "name": NAME, "version": VERSION + "+" + hashlib.sha256(identity).hexdigest()[:12],
        "description": "Meal planning and groceries through your independently running Meal Concierge service.",
        "author": {"name": "Meal Concierge contributors"},
        "homepage": "https://github.com/poisdahl/meal-concierge",
        "repository": "https://github.com/poisdahl/meal-concierge",
        "license": "MIT", "skills": "./skills/", "mcpServers": "./.mcp.json",
    }
    if client == "codex":
        manifest["interface"] = {
            "displayName": "Meal Concierge",
            "shortDescription": "Plan meals and manage groceries",
            "longDescription": manifest["description"],
            "developerName": "Meal Concierge contributors", "category": "Productivity",
            "capabilities": ["Read", "Write"],
            "defaultPrompt": ["Show my Meal Concierge setup."],
        }
    # A fresh destination also prevents overwriting a different household package.
    output.mkdir(parents=True, exist_ok=False)
    plugin = output / "plugins" / NAME
    manifest_dir = ".codex-plugin" if client == "codex" else ".claude-plugin"
    write_json(plugin / manifest_dir / "plugin.json", manifest)
    write_json(plugin / ".mcp.json", {"mcpServers": {"meal_concierge": server}})
    skill_dir = plugin / "skills" / NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(skill_text)
    if client == "codex":
        marketplace = {
            "name": NAME, "interface": {"displayName": "Meal Concierge"},
            "plugins": [{"name": NAME, "source": {"source": "local", "path": "./plugins/" + NAME},
                         "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                         "category": "Productivity"}],
        }
        write_json(output / ".agents/plugins/marketplace.json", marketplace)
    else:
        write_json(output / ".claude-plugin/marketplace.json", {
            "name": NAME, "owner": {"name": "Meal Concierge contributors"},
            "plugins": [{"name": NAME, "source": "./plugins/" + NAME}],
        })
    shutil.copyfile(PRODUCT / "LICENSE", plugin / "LICENSE")
    return plugin


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=["codex", "claude-code"])
    parser.add_argument("--home", type=Path, default=Path(os.environ.get(
        "MEAL_CONCIERGE_HOME", str(Path.home() / ".local/share/meal-concierge"))))
    parser.add_argument("--output", type=Path, required=True, help="new local marketplace directory")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        plugin = build(args.client, args.home.expanduser().resolve(), args.output.expanduser().absolute())
    except subprocess.CalledProcessError as exc:
        parser.exit(1, "Cannot attach to the existing service:\n" + exc.stderr)
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(plugin)


if __name__ == "__main__":
    main()
