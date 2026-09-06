"""MC08 Linux integration on isolated pinned NanoClaw; no model or provider I/O.

Use Python 3.12.12 -I from the locked test runtime, MC08_SCRATCH for a fresh
private run root, NANOCLAW_ROOT for its fresh b76fcb3d source/dependencies, and
the task Node/Docker wrappers on PATH. Existing NanoClaw installations are
never accepted. Setup details and exact limits are in docs/nanoclaw.md.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import signal
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid


HERE = Path(__file__).resolve().parent
CORE = HERE.parent
sys.path.insert(0, str(HERE))
import test_mcp_runtime as mc01
sys.path.insert(0, str(CORE / "clients"))
from nanoclaw import build


def serve(root):
    mc01.isolated_runtime()
    sys.addaudithook(mc01.no_external_network)
    from test_meal_concierge_mathem import MathemShop
    from core import StateStore
    from service import Application, Server, config

    class ObservedApplication(Application):
        def handle(self, request):
            result = super().handle(request)
            with (root / "application.jsonl").open("a") as log:
                log.write(json.dumps({"request": request, "result": result}) + "\n")
            return result

    household = root / "household"
    app = ObservedApplication(StateStore(household / "state", config(household / "config.json")), MathemShop(), None)
    Server(root / "socket/service.sock", os.getgid(), os.getuid(), app).run()


def environment(root):
    return {"PATH": str(root / "bin") + ":" + os.defpath, "HOME": str(root / "home"),
            "TMPDIR": str(root / "tmp"), "PYTHONDONTWRITEBYTECODE": "1"}


def start_service(root):
    log = (root / "service.log").open("a")
    process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--serve", str(root)],
                               env=environment(root), stdout=log, stderr=log)
    log.close()
    deadline = time.monotonic() + 15
    import socket
    while True:
        if process.poll() is not None:
            raise AssertionError((root / "service.log").read_text()[-8000:])
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(root / "socket/service.sock"))
            return process
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                process.terminate()
                process.wait(timeout=5)
                raise AssertionError("service startup timeout")
            time.sleep(.05)


def stop_service(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def cleanup_containers(root):
    """Reconcile only recorded task identities; never sweep labels or prefixes."""
    journal = root / "container-names.jsonl"
    recorded = {r["name"]: r for r in map(json.loads, journal.read_text().splitlines())} if journal.exists() else {}
    if not recorded:
        return
    install = (root / "installation-id").read_text().strip()
    ids = root / "containers.jsonl"
    known = {(r["name"], r.get("attempt")): r["id"] for r in map(json.loads, ids.read_text().splitlines())} if ids.exists() else {}
    for name, entry in recorded.items():
        result = subprocess.run(["/usr/bin/docker", "inspect", name], capture_output=True, text=True, timeout=20)
        if result.returncode:
            if "no such object" in result.stderr.lower() or "no such container" in result.stderr.lower():
                continue
            raise AssertionError("could not reconcile owned container: " + result.stderr[-500:])
        item = json.loads(result.stdout)[0]
        labels = item["Config"]["Labels"]
        assert item["Name"] == "/" + name and name == "ncl-" + install + "-" + entry["session"]
        assert labels["nanoclaw-install"] == install and labels["nanoclaw-role"] == "agent"
        assert labels["nanoclaw-session"] == entry["session"]
        key = (name, entry.get("attempt"))
        assert key not in known or item["Id"] == known[key], "owned name was reused within one launch attempt"
        # A missing ID entry means the host died between Docker create and the
        # post-start journal. Reconcile and record its exact scoped ID first.
        with ids.open("a") as file:
            file.write(json.dumps({**entry, "id": item["Id"]}) + "\n")
        subprocess.run(["/usr/bin/docker", "rm", "-f", item["Id"]], check=True, capture_output=True, timeout=20)


def cleanup_helpers(root):
    journal = root / "helper-pids"
    for line in journal.read_text().splitlines() if journal.exists() else []:
        pid, started = line.split()
        proc = Path("/proc") / pid
        try:
            actual = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
            argv = proc.joinpath("cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        if actual != started:
            continue
        assert argv[0] == b"/usr/bin/docker" or str(root / "bin/docker").encode() in argv[:2]
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def cleanup(root):
    try:
        cleanup_containers(root)
    finally:
        cleanup_helpers(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=Path)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
        return
    mc01.isolated_runtime()
    assert sys.platform == "linux", "native container integration requires Linux"
    docker_gid = Path("/var/run/docker.sock").stat().st_gid
    assert os.getuid() == 0 or docker_gid in {os.getgid(), *os.getgroups()}, "host process needs access to the Docker socket group"
    root = Path(os.environ["MC08_SCRATCH"]).resolve()
    ncl = Path(os.environ["NANOCLAW_ROOT"]).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    assert root != temporary and root.is_relative_to(temporary) and ncl.is_relative_to(root)
    assert not (ncl / "data").exists() and not (root / "household").exists(), "fresh task state required"
    install = os.environ.get("NANOCLAW_INSTALL_ID", "mc-test-" + uuid.uuid4().hex[:8])
    assert re.fullmatch(r"[a-z0-9-]{1,20}", install), "use a short unique test installation ID"
    image = os.environ["NANOCLAW_TEST_IMAGE"]
    assert image and not any(c.isspace() for c in image), "specify the tested NanoClaw agent image"
    (root / "installation-id").write_text(install)
    for name in ("home", "tmp", "household", "socket", "probes"):
        (root / name).mkdir(mode=0o700)
    # Group traversal is needed for the independent wrong-UID socket probe;
    # it does not grant access to the private household directory.
    (root / "socket").chmod(0o750)
    (root / "household/config.json").write_text(json.dumps({
        "household": "MC08 synthetic household", "instance": "mc08", "provider": "mathem",
        "confirmation_policy": "fresh", "profile_overrides": {},
    }))
    from test_meal_concierge_recipes import full_recipe
    (root / "probes/recipe.json").write_text(json.dumps(full_recipe("MC08 synthetic dinner")))
    tree = ast.parse((CORE / "mcp_server.py").read_text())
    names = sorted(node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("meal_concierge_"))
    (root / "probes/schema.json").write_text(json.dumps(names))
    shutil.copyfile(HERE / "nanoclaw_integration_client.ts", root / "probes/client.ts")
    shutil.copyfile(HERE / "nanoclaw_integration_probe.ts", ncl / "mc08-probe.ts")
    (root / "bin/docker").write_text('#!/bin/sh\nprintf "%s %s\\n" "$$" "$(awk \'{print $22}\' /proc/$$/stat)" >> "$MC08_SCRATCH/helper-pids"\nexec /usr/bin/docker "$@"\n')
    (root / "bin/docker").chmod(0o700)
    service = start_service(root)
    host = None
    restarts = 0
    try:
        build(root / "package", Path(sys.base_prefix), Path(sys.prefix) / "lib/python3.12/site-packages", root / "socket")
        shutil.copytree(root / "package/template", ncl / "templates/meal-concierge")
        env = {**environment(root), "MC08_SCRATCH": str(root), "NANOCLAW_INSTALL_ID": install,
               "CONTAINER_IMAGE": image, "CONTAINER_CPU_LIMIT": "1", "CONTAINER_MEMORY_LIMIT": "1g"}
        for phase in ("first", "host-restart"):
            with (root / f"host-{phase}.log").open("w") as log:
                host = subprocess.Popen(["node", "--import", "tsx", "mc08-probe.ts"], cwd=ncl,
                                        env={**env, "MC08_PHASE": phase}, stdout=log, stderr=log)
            deadline = time.monotonic() + 150
            while host.poll() is None:
                assert time.monotonic() < deadline, "native host probe timed out"
                if (root / "restart-request").exists() and not (root / "restart-ready").exists():
                    old_pid = service.pid
                    stop_service(service)
                    service = start_service(root)
                    assert service.pid != old_pid
                    restarts += 1
                    (root / "restart-ready").write_text("replacement listener ready")
                time.sleep(.05)
            assert host.returncode == 0, (root / f"host-{phase}.log").read_text()[-12000:]
            assert service.poll() is None
            remaining = subprocess.check_output(["/usr/bin/docker", "ps", "-aq", "--filter", "label=nanoclaw-install=" + install], text=True)
            assert not remaining.strip(), "native host left containers for fallback cleanup"
        assert restarts == 1
        rows = [json.loads(line) for line in (root / "application.jsonl").read_text().splitlines()]
        writes = [r for r in rows if r["request"].get("operation") == "recipes" and r["request"].get("action") == "save"]
        assert len(writes) == 5, len(writes)
        for key, count in (("mc08-interactive", 3), ("mc08-occurrence", 2)):
            attempts = [r for r in writes if r["request"].get("idempotency_key") == key]
            assert len(attempts) == count
            assert len({(r["result"]["recipe"]["id"], r["result"]["recipe"]["revision"]) for r in attempts}) == 1
        with sqlite3.connect(root / "household/state/recipes.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 2
        print(json.dumps({"native_template_sdk": "passed", "same_container_same_bridge_service_restart": "passed",
                          "host_session_restart": "passed", "native_task_retry": "passed", "service_restarts": restarts,
                          "model_auth_attachments_full_daemon": "not exercised"}))
    finally:
        if host is not None and host.poll() is None:
            host.terminate()
            try:
                host.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host.kill()
                host.wait(timeout=5)
        stop_service(service)
        cleanup(root)


if __name__ == "__main__":
    main()
