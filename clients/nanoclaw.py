#!/usr/bin/env python3
"""Build a native NanoClaw template for an independently running household.

Only the stdio bridge and canonical skill enter the template. The operator
attaches a dedicated socket directory and compatible Python runtime separately.
This command never starts a service or changes an existing NanoClaw group.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import stat


SOURCE = Path(__file__).resolve().parents[1]
PREFIX = "/workspace/extra/meal-concierge"


def build(output: Path, python_base: Path, site_packages: Path, socket_directory: Path) -> dict:
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a new directory")
    python_base = python_base.resolve(strict=True)
    site_packages = site_packages.resolve(strict=True)
    socket_directory = socket_directory.resolve(strict=True)
    if not (python_base / "bin/python3.12").is_file():
        raise ValueError("Python base must contain bin/python3.12 for the container architecture")
    if not (site_packages / "mcp").is_dir():
        raise ValueError("site-packages must contain the pinned MCP runtime")
    # This is an actual host filesystem boundary. Do not turn a convenient
    # household parent mount into access to its database, config or credentials.
    entries = {entry.name: entry for entry in socket_directory.iterdir()}
    if not set(entries) <= {"service.sock", "service.sock.owner.lock"}:
        raise ValueError("socket directory must contain only service.sock and its ownership lock")
    if "service.sock" not in entries or not stat.S_ISSOCK(entries["service.sock"].lstat().st_mode):
        raise ValueError("service.sock must be an existing Unix socket, not a symlink")
    lock = entries.get("service.sock.owner.lock")
    if lock and (not stat.S_ISREG(lock.lstat().st_mode) or lock.stat().st_size != 0):
        raise ValueError("socket ownership lock must be an empty regular file")

    mounts = [
        {"hostPath": str(python_base), "containerPath": "meal-concierge-python", "readonly": True},
        {"hostPath": str(site_packages), "containerPath": "meal-concierge-site", "readonly": True},
        {"hostPath": str(socket_directory), "containerPath": "meal-concierge-socket", "readonly": True},
    ]
    server = {
        "type": "stdio",
        "command": "env",
        "args": [PREFIX + "-python/bin/python3.12", "-S", "-P", "-B", "${PLUGIN_ROOT}/bridge/mcp_server.py"],
        "env": {"PYTHONPATH": PREFIX + "-site", "MEAL_CONCIERGE_SOCKET": PREFIX + "-socket/service.sock"},
    }
    template = output / "template"
    (template / "bridge").mkdir(parents=True)
    (template / "skills/meal-concierge").mkdir(parents=True)
    for name in ("mcp_server.py", "rpc_client.py"):
        shutil.copyfile(SOURCE / name, template / "bridge" / name)
    shutil.copyfile(SOURCE / "skill/SKILL.md", template / "skills/meal-concierge/SKILL.md")
    (template / "plugin.json").write_text(json.dumps({
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "meal-concierge", "version": "1.0.0",
        "description": "Meal Concierge bridge to this owner's independently running household service.",
    }, indent=2) + "\n")
    (template / "mcp.json").write_text(json.dumps({
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {"meal_concierge": server},
    }, indent=2) + "\n")
    attachment = {
        "additionalMounts": mounts,
        "allowlistRoots": [{"path": m["hostPath"], "allowReadWrite": False} for m in mounts],
        "template": str(template),
        "authority": "Only the configured owner UID and host root; all participants of an attached group share that authority.",
    }
    (output / "attachment.json").write_text(json.dumps(attachment, indent=2) + "\n")
    return attachment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python-base", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--socket-directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.python_base, args.site_packages, args.socket_directory), indent=2))


if __name__ == "__main__":
    main()
