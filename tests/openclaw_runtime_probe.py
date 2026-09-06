"""Actual installed OpenClaw registry/skill/filter/lifecycle probe; no model auth.

Use a pinned Python environment with -I. Source and scratch must be isolated
task paths. This is native discovery evidence, not model/menu/import acceptance.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import socket
import signal

sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--openclaw", type=Path, required=True)
    args = parser.parse_args()
    source, scratch = args.source.resolve(), args.scratch.resolve()
    scratch.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    for name in ("home", "tmp", "config", "cache", "data", "state", "workspace"):
        (scratch / name).mkdir()
    env = {
        "PATH": str(args.node.parent) + os.pathsep + os.defpath,
        "HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"),
        "XDG_CONFIG_HOME": str(scratch / "config"), "XDG_CACHE_HOME": str(scratch / "cache"),
        "XDG_DATA_HOME": str(scratch / "data"), "XDG_STATE_HOME": str(scratch / "state"),
        "OPENCLAW_HOME": str(scratch / "home"),
        "OPENCLAW_STATE_DIR": str(scratch / "state"),
        "OPENCLAW_CONFIG_PATH": str(scratch / "config/openclaw.json"),
    }
    Path(env["OPENCLAW_CONFIG_PATH"]).write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(scratch / "workspace"), "skills": ["meal-concierge"]}},
        "gateway": {"mode": "local", "bind": "loopback"},
        "discovery": {"mdns": {"mode": "off"}},
        "browser": {"enabled": False}, "channels": {},
        "cron": {"enabled": False}, "update": {"checkOnStart": False},
        "tools": {"allow": ["bundle-mcp"]},
        "logging": {"file": str(scratch / "openclaw.log")},
    }))

    def bridge_pids():
        output = subprocess.check_output(["/bin/ps", "-axo", "pid=,args="], text=True)
        expected = f"{sys.executable} -I {source / 'mcp_server.py'}"
        return [int(fields[0]) for line in output.splitlines()
                if len(fields := line.strip().split(None, 1)) == 2 and fields[1] == expected]

    def cleanup_bridges():
        # OpenClaw launches each stdio bridge detached, outside the CLI's PGID.
        # This source is task-exclusive, checked empty before any native spawn.
        owned = bridge_pids()
        for sig in (signal.SIGCONT, signal.SIGTERM, signal.SIGKILL):
            for pid in owned:
                if pid in bridge_pids():
                    try:
                        assert os.getpgid(pid) == pid, "unexpected bridge process-group ownership"
                        os.killpg(pid, sig)
                    except ProcessLookupError:
                        pass
            if sig != signal.SIGKILL:
                time.sleep(.1)
        assert not bridge_pids(), "failed to clean up task-owned bridges"

    @contextmanager
    def bridge_guard():
        try:
            yield
        finally:
            remaining = bridge_pids()
            if remaining:
                print("Cleaning up leaked task bridge PIDs:", remaining, file=sys.stderr)
                cleanup_bridges()
                if sys.exc_info()[0] is None:
                    raise AssertionError("native client leaked a bridge; task cleanup was required")

    def native(*command, expect=0, json_output=False, stall_bridge=False):
        process = subprocess.Popen([str(args.node), str(args.openclaw), *command],
                                   env=env, cwd=scratch, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            if stall_bridge:
                deadline = time.monotonic() + 15
                while not bridge_pids():
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(.01)
                for pid in bridge_pids():
                    os.kill(pid, signal.SIGSTOP)
            stdout, stderr = process.communicate(timeout=.2 if stall_bridge else 60)
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            cleanup_bridges()
            process.communicate(timeout=5)
            raise
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        with (scratch / "native.jsonl").open("a") as log:
            log.write(json.dumps({"command": list(command), "returncode": result.returncode,
                                  "stdout": result.stdout, "stderr": result.stderr}) + "\n")
        assert result.returncode == expect, (command, result.stdout, result.stderr)
        return json.loads(result.stdout) if json_output else result.stdout

    def reaped():
        deadline = time.monotonic() + 10
        while bridge_pids() and time.monotonic() < deadline:
            time.sleep(.05)
        assert not bridge_pids(), "native OpenClaw left its stdio bridge alive"

    # Reuse the existing synthetic external-provider fixture and real Server.
    sys.path.insert(0, str(source / "tests"))
    import test_mcp_runtime as runtime
    spec = importlib.util.spec_from_file_location("mc_openclaw", Path(__file__).parents[1] / "clients/openclaw.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    os.environ["MC01_SCRATCH"] = str(scratch)
    versions = runtime.isolated_runtime()
    assert not bridge_pids(), "source already belongs to an active bridge"
    version = native("--version").strip()
    assert "2026.9.2" in version, version
    with runtime.household() as (household, process), bridge_guard():
        attachment = {"command": sys.executable, "args": ["-I", str(source / "mcp_server.py")],
                      "env": {"MEAL_CONCIERGE_SOCKET": str(household / "service.sock")},
                      "skill": str(source / "skill/SKILL.md")}
        fragment = adapter.configuration(attachment)
        native("mcp", "set", "meal-concierge", json.dumps(fragment["mcp"]["servers"]["meal-concierge"]))
        native("config", "set", "skills.load.extraDirs", json.dumps(fragment["skills"]["load"]["extraDirs"]), "--strict-json")
        skills = native("skills", "info", "meal-concierge", "--json", json_output=True)
        assert skills["name"] == "meal-concierge", skills
        native("mcp", "doctor", "meal-concierge", "--json", json_output=True)
        expected = {"meal-concierge__" + n.name for n in ast.parse((source / "mcp_server.py").read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name.startswith("meal_concierge_")}
        def complete_catalog(discovered):
            server = discovered["servers"]["meal-concierge"]
            utilities = set()
            for capability, names in (("resources", ("resources_list", "resources_read")),
                                      ("prompts", ("prompts_list", "prompts_get"))):
                if server.get(capability):
                    utilities.update("meal-concierge__" + name for name in names)
            assert server["tools"] == len(expected), discovered
            assert set(discovered["tools"]) == expected | utilities, discovered
            assert discovered["diagnostics"] == [], discovered
        for _ in range(2):
            discovered = native("mcp", "probe", "meal-concierge", "--json", json_output=True)
            complete_catalog(discovered)
            reaped()
            assert process.poll() is None and (household / "service.sock").exists()
        native("mcp", "tools", "meal-concierge", "--include", "meal_concierge_status")
        filtered = native("mcp", "probe", "meal-concierge", "--json", json_output=True)
        assert filtered["tools"] == ["meal-concierge__meal_concierge_status"], filtered
        native("mcp", "configure", "meal-concierge", "--disable")
        native("mcp", "probe", "meal-concierge", "--json", expect=1)
        native("mcp", "configure", "meal-concierge", "--enable", "--clear-tools")
        restored = native("mcp", "probe", "meal-concierge", "--json", json_output=True)
        complete_catalog(restored)
        reaped()
        assert process.poll() is None
        try:
            native("mcp", "probe", "meal-concierge", "--json", stall_bridge=True)
            raise AssertionError("stalled native probe unexpectedly completed")
        except subprocess.TimeoutExpired:
            assert not bridge_pids(), "timeout cleanup left a detached bridge"
            print("Native probe timeout: exact detached bridge cleanup passed", file=sys.stderr)
        # Independently confirm the original service remains responsive.
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(household / "service.sock"))
            client.sendall(b'{"operation":"health","contract":1}\n')
            assert json.loads(client.recv(65536))["ok"] is True
        native("mcp", "unset", "meal-concierge")
    print(json.dumps({"openclaw": version, "python": versions["python"],
                      "mcp": versions["dependencies"]["mcp"], "native_tools": len(expected),
                      "discovery_filters_skill_cleanup_service_survival": "passed",
                      "forced_timeout_detached_bridge_cleanup": "passed",
                      "model_menu_import_scheduling": "pending"}, indent=2))


if __name__ == "__main__":
    main()
