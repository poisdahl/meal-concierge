#!/usr/bin/env python3
"""Local browser prerequisite discovery and validation; never opens a browser."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Mapping, Sequence


AGENT_BROWSER_VERSION = "0.33.1"
MAX_BROWSER_WRAPPER_BYTES = 1024 * 1024


def executable(value, candidates: Sequence[str], label: str, *, search_path=None) -> str:
    choices = [value] if value is not None else candidates
    for name in choices:
        path = (Path(name).expanduser() if "/" in str(name)
                else Path(shutil.which(str(name), path=search_path) or "/nonexistent"))
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.absolute())
    raise RuntimeError(f"{label} is missing; supply its explicit executable path")


def adapter_candidates() -> list[str]:
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return [
        "agent-browser",
        str(Path.home() / ".local/lib/meal-concierge/node_modules/.bin/agent-browser"),
        str(hermes_home / "node/bin/agent-browser"),
    ]


def chrome_candidates() -> list[str]:
    return [
        "chromium", "chromium-browser", "google-chrome-stable", "google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        str(Path.home() / "Applications/Chromium.app/Contents/MacOS/Chromium"),
    ]


def validate_browser_paths(
    adapter: str | Path,
    chrome: str | Path,
    *,
    runtime_path: str | None = None,
) -> dict[str, str]:
    adapter_path = executable(adapter, (), "agent-browser", search_path=runtime_path)
    chrome_path = executable(chrome, (), "non-snap Chrome/Chromium", search_path=runtime_path)
    environment = None if runtime_path is None else {**os.environ, "PATH": runtime_path}
    try:
        result = subprocess.run(
            [adapter_path, "--version"], capture_output=True, text=True,
            check=True, timeout=10, env=environment,
        )
        if re.fullmatch(
            rf"(?:agent-browser\s+)?v?{re.escape(AGENT_BROWSER_VERSION)}",
            result.stdout.strip(),
        ) is None:
            raise RuntimeError(f"install the tested agent-browser@{AGENT_BROWSER_VERSION}")
        chrome_file = Path(chrome_path)
        with chrome_file.open("rb") as handle:
            head = handle.read(4096)
            if head.startswith(b"#!"):
                handle.seek(0)
                head = handle.read(MAX_BROWSER_WRAPPER_BYTES + 1)
                if len(head) > MAX_BROWSER_WRAPPER_BYTES:
                    raise RuntimeError(
                        "browser wrapper is too large to validate; supply the real non-snap executable"
                    )
            head = head.lower()
        if (str(chrome_file).startswith("/snap/")
                or str(chrome_file.resolve()).startswith("/snap/")
                or any(marker in head for marker in (
                    b"/snap/bin/chromium", b"/usr/bin/snap", b"snap run chromium", b"exec snap ",
                ))):
            raise RuntimeError("Snap Chromium cannot access the private browser profile")
        result = subprocess.run(
            [chrome_path, "--version"], capture_output=True, text=True,
            check=True, timeout=10, env=environment,
        )
        if re.search(r"\b(?:Chromium|Google Chrome)\b", result.stdout + result.stderr) is None:
            raise RuntimeError("browser executable is not Chromium or Google Chrome")
    except (RuntimeError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc)) from exc
    return {"browser_binary": adapter_path, "browser_executable": chrome_path}


def discover_browser_paths(
    agent_browser=None,
    browser_executable=None,
    existing: Mapping[str, str] | None = None,
    *,
    runtime_path: str | None = None,
) -> dict[str, str]:
    existing = existing or {}
    adapter_value = (agent_browser if agent_browser is not None
                     else existing.get("browser_binary"))
    chrome_value = (browser_executable if browser_executable is not None
                    else existing.get("browser_executable"))
    adapter = executable(
        adapter_value, adapter_candidates(), "agent-browser", search_path=runtime_path,
    )
    chrome = executable(
        chrome_value, chrome_candidates(), "non-snap Chrome/Chromium", search_path=runtime_path,
    )
    return validate_browser_paths(adapter, chrome, runtime_path=runtime_path)


def browser_launch_settings(mode="headless", display=None, xauthority=None) -> dict[str, str]:
    """Validate durable launch choices, independently of an existing browser daemon."""
    if mode not in {"headless", "headed"}:
        raise RuntimeError("browser mode must be headless or headed")
    if mode == "headless":
        if display is not None or xauthority is not None:
            raise RuntimeError("browser display and Xauthority require headed mode")
        return {"mode": mode}
    if sys.platform.startswith("linux") and not display:
        raise RuntimeError("headed browser requires --browser-display for the visible host desktop")
    result = {"mode": mode}
    if display is not None:
        if not isinstance(display, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", display):
            raise RuntimeError("browser display is invalid")
        result["display"] = display
    if xauthority is not None:
        path = Path(xauthority)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.R_OK):
            raise RuntimeError("browser Xauthority must be a readable absolute file path")
        result["xauthority"] = str(path)
    return result


def validate_browser_authority_access(settings: Mapping[str, str], uid: int, gid: int) -> None:
    path = settings.get("xauthority")
    if path is None or os.geteuid() != 0 or uid == 0:
        return

    def drop_privileges():
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", "import os,sys; sys.exit(0 if os.access(sys.argv[1], os.R_OK) else 1)", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=drop_privileges, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("cannot verify display authorization access for the browser user") from exc
    if result.returncode:
        raise RuntimeError("configured browser Xauthority is not readable by the browser user")


def browser_launch_environment(settings: Mapping[str, str]) -> dict[str, str]:
    return {variable: settings[key] for key, variable in (
        ("display", "DISPLAY"), ("xauthority", "XAUTHORITY")
    ) if key in settings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-browser", default=os.environ.get("MEAL_CONCIERGE_AGENT_BROWSER") or None)
    parser.add_argument("--browser-executable", default=os.environ.get("MEAL_CONCIERGE_BROWSER_EXECUTABLE") or None)
    args = parser.parse_args()
    paths = discover_browser_paths(args.agent_browser, args.browser_executable)
    print(paths["browser_binary"])
    print(paths["browser_executable"])


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        sys.exit(str(exc))
