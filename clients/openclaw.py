#!/usr/bin/env python3
"""Translate `install.py attach` JSON into an OpenClaw configuration fragment."""
from __future__ import annotations

import json
from pathlib import Path
import sys


def configuration(attachment: dict) -> dict:
    """Keep the installed bridge and canonical skill; start no household service."""
    command = attachment.get("command")
    args = attachment.get("args")
    env = attachment.get("env")
    skill = attachment.get("skill")
    if not isinstance(command, str) or not Path(command).is_absolute():
        raise ValueError("attachment.command must be an absolute executable path")
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        raise ValueError("attachment.args must be a nonempty string array")
    if not isinstance(env, dict) or set(env) != {"MEAL_CONCIERGE_SOCKET"}:
        raise ValueError("attachment.env must contain only MEAL_CONCIERGE_SOCKET")
    if not isinstance(env["MEAL_CONCIERGE_SOCKET"], str) or not Path(env["MEAL_CONCIERGE_SOCKET"]).is_absolute():
        raise ValueError("attachment socket must be an absolute path")
    if not isinstance(skill, str) or not Path(skill).is_absolute() or Path(skill).name != "SKILL.md":
        raise ValueError("attachment.skill must be an absolute SKILL.md path")
    return {
        "mcp": {"servers": {"meal-concierge": {
            "command": command, "args": args, "env": env,
            "transport": "stdio", "requestTimeoutMs": 660000,
        }}},
        "skills": {"load": {"extraDirs": [str(Path(skill).parent)]}},
    }


def main() -> None:
    attachment = json.load(sys.stdin)
    if not isinstance(attachment, dict):
        raise ValueError("expected install.py attach JSON object on stdin")
    print(json.dumps(configuration(attachment), indent=2))


if __name__ == "__main__":
    main()
