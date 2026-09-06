"""Actual client/package probe against a production Application with synthetic I/O.

Use the pinned standalone runtime with python -I. This owns only its explicitly
supplied scratch root and children it starts. It does not use a native manager.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from contextlib import contextmanager
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve()
PRODUCT = HERE.parents[1]
sys.path.insert(0, str(PRODUCT / "clients"))
from package import build


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n")


def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def records(root):
    path = root / "application.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def attestation():
    import mcp
    import mcp.types
    assert sys.version_info[:3] == (3, 12, 12)
    assert sys.flags.isolated and sys.prefix != sys.base_prefix
    for name, module in [("mcp", mcp), ("mcp-types", mcp.types)]:
        assert importlib.metadata.version(name) == "2.1.1"
        assert Path(module.__file__).is_relative_to(sys.prefix)
    print(json.dumps({"python": sys.version.split()[0], "mcp": "2.1.1", "mcp-types": "2.1.1",
                      "imports": [mcp.__file__, mcp.types.__file__]}), flush=True)


def prepare(root):
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    release = root / "code/release"
    release.mkdir(parents=True)
    for path in PRODUCT.glob("*.py"):
        shutil.copyfile(path, release / path.name)
    shutil.copytree(PRODUCT / "skill", release / "skill")
    (release / "venv").symlink_to(sys.prefix)
    (root / "code/current").symlink_to("release")
    write_json(root / "runtime.json", {"code_root": str(root / "code"),
                                      "paths": {"socket": str(root / "service.sock")}})
    write_json(root / "config.json", {"household": "MC05-" + uuid.uuid4().hex,
                                      "instance": root.name, "provider": "mathem",
                                      "confirmation_policy": "fresh"})
    write_json(root / "synthetic-cart.json", {"items": [], "subtotal": 0, "delivery": None})


def serve(root):
    sys.path.insert(0, str(root / "code/current"))
    from core import StateStore
    from service import Application, Server, config
    from runtime_ownership import ownership

    def local_only(event, args):
        if event in {"socket.connect", "socket.bind"}:
            assert args[0].family == socket.AF_UNIX, "synthetic service attempted external networking"
    sys.addaudithook(local_only)

    class Shop:
        # Normalized cart shape from the existing Mathem contract fixture.
        def probe(self, **kwargs):
            return {"protocol_version": "2025-11-25", "server": {"name": "MC05 synthetic Mathem"}, "tool_count": 25}

        def call(self, tool, arguments, **kwargs):
            cart = json.loads((root / "synthetic-cart.json").read_text())
            if tool == "get_cart":
                delay = root / "delay-read-seconds"
                if delay.exists():
                    seconds = float(delay.read_text())
                    delay.unlink()
                    (root / "read-started").touch()
                    # Deliberately slow external response measures the client/bridge
                    # wall clock; it is not a provider deadline/SLA certificate.
                    time.sleep(seconds)
                return cart
            if tool == "manipulate_cart":
                for item in arguments["operations"]:
                    row = next((x for x in cart["items"] if x["product_id"] == item["productId"]), None)
                    if row is None:
                        row = {"product_id": item["productId"], "name": "Synthetic eggs", "quantity": 0, "price": 29.9}
                        cart["items"].append(row)
                    row["quantity"] += item["quantity"]
                cart["items"] = [x for x in cart["items"] if x["quantity"]]
                cart["subtotal"] = sum(x["quantity"] * x["price"] for x in cart["items"])
                write_json(root / "synthetic-cart.json", cart)
                append(root / "dispatch.jsonl", {"tool": tool, "arguments": arguments})
                if (root / "hold-response").exists():
                    wait_for(lambda: (root / "release-response").exists(), 120)
                return cart
            raise AssertionError("unconfigured synthetic provider call: " + tool)

    class ObservedApplication(Application):
        def handle(self, request):
            started = time.monotonic()
            try:
                result = super().handle(request)
            except Exception as exc:
                append(root / "application.jsonl", {"request": request, "error": str(exc)})
                raise
            append(root / "application.jsonl", {"request": request, "result": result, "pid": os.getpid(),
                                                "elapsed": time.monotonic() - started})
            return result

    with ownership(root / "state", root / "browser-profile", root / "browser-home",
                   root / "browser-run", None, os.getuid(), os.getgid()):
        app = ObservedApplication(StateStore(root / "state", config(root / "config.json")), Shop(), None)
        Server(root / "service.sock", os.getgid(), os.getuid(), app).run()


def wait_for(condition, timeout=30):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError("probe condition did not complete")
        time.sleep(0.05)


def healthy(root):
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(str(root / "service.sock"))
            connection.sendall(b'{"operation":"health","contract":1}\n')
            return json.loads(connection.recv(65536)).get("ok") is True
    except (OSError, ValueError):
        return False


@contextmanager
def service(root):
    with (root / "service.log").open("a") as log:
        process = subprocess.Popen([sys.executable, "-I", str(HERE), "--serve", str(root)],
                                   stdout=log, stderr=log, env={"PATH": os.defpath, "HOME": str(root)})
        try:
            wait_for(lambda: healthy(root) or process.poll() is not None)
            assert process.poll() is None, (root / "service.log").read_text()
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def claude_environment(root):
    env = dict(os.environ)
    env.update(CLAUDE_CONFIG_DIR=str(root / "claude-config"), CLAUDE_SECURESTORAGE_CONFIG_DIR="",
               CLAUDE_CODE_PLUGIN_CACHE_DIR=str(root / "claude-cache"),
               CLAUDE_CODE_TMPDIR=str(root / "claude-tmp"),
               DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return env


def claude_command(plugin, prompt, *, deny=False):
    return [shutil.which("claude"), "--print", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--setting-sources", "", "--plugin-dir", str(plugin),
            "--tools", "", "--permission-mode", "dontAsk",
            "--allowedTools", "mcp__plugin_meal-concierge_meal_concierge__meal_concierge_status" if deny else "mcp__plugin_meal-concierge_meal_concierge__*",
            "--max-turns", "6", prompt]


def toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + toml(v) for k, v in value.items()) + "}"
    return json.dumps(value)


def codex_command(root, marketplace, prompt, *, deny=False):
    name = json.loads((marketplace / ".agents/plugins/marketplace.json").read_text())["name"]
    client_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    assert name == "mc05-test-20260906", "native writes require the dedicated probe namespace"
    plugin = marketplace / "plugins/meal-concierge"
    version = json.loads((plugin / ".codex-plugin/plugin.json").read_text())["version"]
    cached = client_home / "plugins/cache" / name / "meal-concierge" / version
    expected = {"command": str(root / "code/current/venv/bin/python"),
                "args": ["-I", str(root / "code/current/mcp_server.py")],
                "env": {"MEAL_CONCIERGE_SOCKET": str(root / "service.sock")},
                "startup_timeout_sec": 20, "tool_timeout_sec": 700}
    for location in (plugin, cached):
        actual = json.loads((location / ".mcp.json").read_text())["mcpServers"]
        assert actual == {"meal_concierge": expected}, "source/cache attachment is not this synthetic service"
    plugins = {p.name + "@" + market.name: {"enabled": False}
               for market in (client_home / "plugins/cache").iterdir() if market.is_dir()
               for p in market.iterdir() if p.is_dir()}
    # Remote installed enablement overrides the plugin toggle; native per-server
    # policy remains local. Disable cached servers before enabling our attachment.
    for config in (client_home / "plugins/cache").glob("*/*/*/.mcp.json"):
        key = config.parents[1].name + "@" + config.parents[2].name
        policy = plugins[key].setdefault("mcp_servers", {})
        policy.update({server: {"enabled": False}
                       for server in json.loads(config.read_text()).get("mcpServers", {})})
    plugins["meal-concierge@" + name] = {"enabled": True, "mcp_servers": {
        "meal_concierge": {"default_tools_approval_mode": "prompt" if deny else "approve"}}}
    args = [shutil.which("codex"), "exec", "--ignore-user-config", "--ephemeral", "--strict-config",
            "--skip-git-repo-check", "-C", str(root), "--json"]
    settings = {
        "history.persistence": "none", "log_dir": str(root / "codex-log"),
        "sqlite_home": str(root / "codex-sqlite"), "approval_policy": "never",
        "features.shell_tool": False, "features.shell_snapshot": False,
        "features.apps": False, "mcp_servers": {},
        "marketplaces": {name: {"source_type": "local", "source": str(marketplace)}},
        "plugins": plugins,
    }
    for key, value in settings.items():
        args += ["-c", key + "=" + toml(value)]
    return args + [prompt]


def process_table():
    rows = subprocess.check_output(["ps", "-axo", "pid,ppid,pgid,lstart,comm"], text=True)
    table = {}
    for line in rows.splitlines()[1:]:
        parts = line.split(None, 8)
        if len(parts) == 9:
            table[int(parts[0])] = (int(parts[1]), int(parts[2]), " ".join(parts[3:8]), parts[8])
    return table


def remember_children(leader, children):
    table = process_table()
    ids = {pid for pid, row in children.items() if pid in table and table[pid][2] == row[2]}
    if leader in table and leader not in children:
        ids.add(leader)
    while True:
        found = {pid for pid, row in table.items() if row[0] in ids}
        if found <= ids:
            break
        ids |= found
    for pid in ids:
        if pid in table:
            children.setdefault(pid, table[pid])


def stop_client(process, children):
    remember_children(process.pid, children)
    # MCP bridges and code-mode helpers use their own groups on macOS. Track
    # descendants while the leader lives, then verify identity before signalling.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        current = process_table()
        for pid, row in children.items():
            if pid in current and current[pid][2] == row[2]:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        try:
            process.wait(timeout=5 if sig == signal.SIGTERM else 2)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = process_table()
            alive = {pid for pid, row in children.items() if pid in current and current[pid][2] == row[2]}
            if not alive:
                return
            time.sleep(0.1)
    raise AssertionError("task client descendants still running: " + str(sorted(alive)))


def native(root, client, package, name, prompt, *, deny=False, timeout=240, on_events=None):
    args = (codex_command(root, package, prompt, deny=deny) if client == "codex"
            else claude_command(package, prompt, deny=deny))
    env = dict(os.environ) if client == "codex" else claude_environment(root)
    start = time.monotonic()
    path = root / (name + ".jsonl")
    with path.open("w") as output, (root / (name + ".stderr")).open("w") as errors:
        process = subprocess.Popen(args, cwd=root, env=env, stdout=output, stderr=errors, start_new_session=True)
        children = {}
        try:
            while process.poll() is None:
                remember_children(process.pid, children)
                if time.monotonic() - start > timeout:
                    raise TimeoutError("native client exceeded probe time budget; inspect original attempt before any retry")
                if on_events:
                    on_events(read_events(path, partial=True))
                time.sleep(0.1)
        finally:
            stop_client(process, children)
    events = read_events(path)
    failures = [x for x in events if x.get("type") == "result" and x.get("is_error")]
    terminal = (any(x.get("type") == "turn.completed" for x in events)
                if client == "codex" else any(x.get("type") == "result" and not x.get("is_error") for x in events))
    assert process.returncode == 0 and not failures and terminal and not any(
        x.get("type") in {"error", "turn.failed"} for x in events), {
        "exit": process.returncode,
        "errors": [{"code": x.get("api_error_status"), "result": x.get("result")} for x in failures],
        "logs": str(path),
    }
    print(json.dumps({"case": name, "client": client,
                      "seconds": round(time.monotonic() - start, 2), "log": str(path)}), flush=True)
    return events


def read_events(path, *, partial=False):
    events = []
    lines = path.read_text().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if partial and index == len(lines) - 1 and not line.endswith("\n"):
            break
        if line.strip():
            events.append(json.loads(line))
    return events


def completed_calls(events):
    return [x["item"] for x in events if x.get("type") == "item.completed"
            and x.get("item", {}).get("type") == "mcp_tool_call"]


def lost_response(root, marketplace):
    class ExpectedDisconnect(Exception):
        pass

    assert not (root / "dispatch.jsonl").exists(), "use a fixture without earlier synthetic cart writes"
    with service(root) as process:
        (root / "hold-response").touch()
        pending = []

        def disconnect(events):
            if (root / "dispatch.jsonl").exists():
                pending.append(json.loads((root / "state/state.json").read_text()).get("pending_cart_change"))
                raise ExpectedDisconnect()

        try:
            native(root, "codex", marketplace, "lost", "This is an authorized synthetic cart test. Call meal_concierge_cart action=change operations=[{\"productId\":10,\"quantity\":1}] exactly once. Do not retry or call any other tool.", on_events=disconnect)
        except ExpectedDisconnect:
            pass
        else:
            raise AssertionError("client was not interrupted after synthetic dispatch")
        assert pending and pending[0], "no original pending attempt was captured"
        assert not completed_calls(read_events(root / "lost.jsonl", partial=True)), "the interrupted client already received a result"
        assert process.poll() is None, "interrupting the client stopped the core"
        (root / "release-response").touch()
        wait_for(lambda: any(x["request"]["operation"] == "cart" and x["request"].get("action") == "change" for x in records(root)))
        events = native(root, "codex", marketplace, "reconcile", "The previous synthetic cart.change was dispatched once and its client lost the response. Call meal_concierge_cart action=reconcile_change once, then action=get once. Report the observed quantity. Never resend the change or create a new intent.")
        calls = completed_calls(events)
        assert [x["arguments"].get("action") for x in calls] == ["reconcile_change", "get"], calls
        assert all(x["status"] == "completed" for x in calls)
        assert all(x.get("error") is None and not x["result"].get("is_error") for x in calls)
        reconciled, observed = [x["result"]["structured_content"] for x in calls]
        assert reconciled["reconciled"] is True and reconciled["cart_write_pending"] is False
        assert [(x["product_id"], x["quantity"]) for x in observed["items"]] == [(10, 1)]
        assert not json.loads((root / "state/state.json").read_text()).get("pending_cart_change")
        app_read = [x for x in records(root) if x["request"]["operation"] == "cart"
                    and x["request"].get("action") == "get"][-1]
        assert app_read["result"] == observed
        assert len((root / "dispatch.jsonl").read_text().splitlines()) == 1
        cart = json.loads((root / "synthetic-cart.json").read_text())
        assert [(x["product_id"], x["quantity"]) for x in cart["items"]] == [(10, 1)]
        print(json.dumps({"lost_response": "one dispatch, original pending attempt, read/reconcile only",
                          "synthetic_quantity": 1}), flush=True)


def codex_probe(root, marketplace, *, long_seconds=0):
    if long_seconds:
        with service(root):
            (root / "delay-read-seconds").write_text(str(long_seconds))
            events = native(root, "codex", marketplace, "long", "Call meal_concierge_checkout action=prepare exactly once. This is a synthetic Mathem cart with a deliberately slow read. Wait for its result and report whether it confirmed an order. Never submit or retry.", timeout=long_seconds + 240)
            calls = completed_calls(events)
            assert len(calls) == 1 and calls[0]["tool"] == "meal_concierge_checkout", calls
            assert calls[0]["result"]["structured_content"]["confirmed"] is False
            measured = [x for x in records(root) if x["request"]["operation"] == "checkout"][-1]["elapsed"]
            assert measured >= long_seconds
            print(json.dumps({"delayed_provider_read_seconds": measured, "live": False}), flush=True)
        return
    with ExitStack() as stack:
        holder = [service(root)]
        process = [holder[0].__enter__()]
        stack.callback(lambda: holder[0].__exit__(None, None, None))
        native(root, "codex", marketplace, "write", "This is an authorized isolated synthetic household test. Call meal_concierge_setup action=apply keep_current=true once, then meal_concierge_profile action=update changes={\"meals\":{\"portions\":3}} once. Report the result. No other actions.")
        before = len(records(root))
        events = native(root, "codex", marketplace, "deny", "Call meal_concierge_profile action=update changes={\"meals\":{\"portions\":9}} once. This tests client permission denial. Do not substitute any other action.", deny=True)
        assert not any(x["request"]["operation"] == "profile" for x in records(root)[before:])
        assert any(x["tool"] == "meal_concierge_profile"
                   and x.get("arguments", {}).get("action") == "update"
                   and "requires approval" in str(x.get("error"))
                   for x in completed_calls(events)), "no actual native permission denial observed"
        restarted = []

        def reconnect(events):
            if restarted or not any(x["tool"] == "meal_concierge_status" and x["status"] == "completed" for x in completed_calls(events)):
                return
            old_pid = process[0].pid
            holder[0].__exit__(None, None, None)
            holder[0] = service(root)
            process[0] = holder[0].__enter__()
            restarted.append({"old": old_pid, "new": process[0].pid})

        events = native(root, "codex", marketplace, "reconnect", "Call meal_concierge_status first. After receiving it, call meal_concierge_profile action=show. Make these two tool calls sequentially, not in parallel. Report the household and portions; do not change anything.", on_events=reconnect)
        assert restarted and restarted[0]["old"] != restarted[0]["new"], "no service restart happened between tool calls"
        profile = next(x for x in completed_calls(events) if x["tool"] == "meal_concierge_profile")
        assert profile["result"]["structured_content"]["profile"]["meals"]["portions"] == 3
        observed = [x for x in records(root) if x["request"]["operation"] == "profile" and x["request"].get("action") == "show"][-1]
        assert observed["pid"] == restarted[0]["new"], "profile was handled before the service restart"
        assert process[0].poll() is None
        print(json.dumps({"same_client_reconnect": restarted[0], "portions": 3}), flush=True)


def smoke(root):
    prepare(root)
    with service(root) as process:
        plugin = build("claude-code", root, root / "claude-package")
        result = subprocess.run([shutil.which("claude"), "plugin", "validate", str(plugin)],
                                env=claude_environment(root), capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        native(root, "claude-code", plugin, "first", "Use the Meal Concierge plugin to call meal_concierge_status, then meal_concierge_setup action=show. Report the exact household and portions. Do not call other tools.")
        assert any(x["request"]["operation"] == "status" and "result" in x for x in records(root))
        assert any(x["request"]["operation"] == "setup" and "result" in x for x in records(root))
        native(root, "claude-code", plugin, "write", "This is an authorized isolated synthetic household test. Call meal_concierge_setup action=apply keep_current=true once, then meal_concierge_profile action=update changes={\"meals\":{\"portions\":3}} once. Report the result. No other actions.")
        assert process.poll() is None
        before = len(records(root))
        events = native(root, "claude-code", plugin, "deny", "Call meal_concierge_profile action=update changes={\"meals\":{\"portions\":9}} once. This tests client permission denial. Do not substitute any other action.", deny=True)
        assert not any(x["request"]["operation"] == "profile" for x in records(root)[before:])
        assert any(x.get("type") == "result" and any("meal_concierge_profile" in y.get("tool_name", "")
                   for y in x.get("permission_denials", [])) for x in events), "no native Claude permission denial observed"
    with service(root) as restarted:
        native(root, "claude-code", plugin, "restart", "Call meal_concierge_status and meal_concierge_profile action=show. Report the household and portions, without changing anything.")
        assert restarted.poll() is None
        profile = [x for x in records(root) if x["request"]["operation"] == "profile" and x["request"].get("action") == "show"][-1]
        assert profile["result"]["profile"]["meals"]["portions"] == 3, profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--codex-marketplace", type=Path,
                        help="already built/test-installed marketplace for an existing synthetic --root")
    parser.add_argument("--long-seconds", type=float, default=0)
    parser.add_argument("--lost-response", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.serve:
        serve(args.serve)
    else:
        attestation()
        assert args.root and args.root.is_absolute(), "--root must be a fresh absolute scratch path"
        if args.codex_marketplace:
            assert json.loads((args.root / "config.json").read_text())["household"].startswith("MC05-")
            if args.lost_response:
                lost_response(args.root, args.codex_marketplace)
            else:
                codex_probe(args.root, args.codex_marketplace, long_seconds=args.long_seconds)
        else:
            smoke(args.root)


if __name__ == "__main__":
    main()
