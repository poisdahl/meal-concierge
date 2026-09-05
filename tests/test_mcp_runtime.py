"""MC-01: real SDK/stdio bridge -> real Unix Server/Application, synthetic Mathem.

Run in a fresh Python 3.12.12 venv with mcp-requirements.txt, using python -I.
This deliberately lives outside the dependency-light fleet unittest roots.
--codex additionally runs an installed, already authenticated Codex CLI; it
does not register a server or change saved client configuration.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from contextlib import asynccontextmanager, contextmanager
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve()
CORE = HERE.parents[1]
ROOT = CORE
sys.path[:0] = [str(CORE), str(HERE.parent), str(ROOT)]


def isolated_runtime():
    assert sys.version_info[:3] == (3, 12, 12), "tested runtime is Python 3.12.12"
    assert sys.flags.isolated and sys.prefix != sys.base_prefix, "use a fresh venv with python -I"
    assert "include-system-site-packages = false" in (Path(sys.prefix) / "pyvenv.cfg").read_text()
    for name in ("hermes_cli", "hermes_agent", "tools"):
        assert importlib.util.find_spec(name) is None, f"unexpected inherited module: {name}"
    for path in sys.path:
        if "site-packages" in path:
            assert Path(path).is_relative_to(sys.prefix), path
    expected = dict(line.split("==") for line in (HERE.parent / "mcp-requirements.txt").read_text().splitlines() if line and not line.startswith("#"))
    actual = {d.metadata["Name"].lower().replace("_", "-"): d.version for d in importlib.metadata.distributions()}
    assert actual == expected, (actual, expected)
    return {"python": sys.version.split()[0], "dependencies": actual, "hermes": "absent"}


def no_external_network(event, args):
    # Actual socket authentication, framing and dispatch remain untouched.
    if event in {"socket.connect", "socket.bind"}:
        import socket
        assert args[0].family == socket.AF_UNIX, "synthetic probe attempted network access"


def serve(root):
    isolated_runtime()
    sys.addaudithook(no_external_network)
    from test_meal_concierge_mathem import MathemShop
    from core import StateStore
    from service import Application, Server, config

    class Shop(MathemShop):
        def probe(self, **kwargs):
            return {**super().probe(**kwargs), "server": {"name": root.name, "version": "synthetic"}}

        def call(self, tool, arguments, **kwargs):
            if tool == "manipulate_cart" and (root / "hold").exists():
                (root / "dispatched").write_text(json.dumps(arguments))
                deadline = time.monotonic() + 20
                while not (root / "release").exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("test failed to release synthetic provider")
                    time.sleep(0.02)
            result = super().call(tool, arguments, **kwargs)
            if tool == "product_search":
                # The existing Mathem fixture defaults to the number returned;
                # product planning requires the actual requested search bound.
                result["scope"]["requested_size"] = arguments.get("size", 1)
            with (root / "provider.jsonl").open("a") as log:
                log.write(json.dumps({"tool": tool, "arguments": arguments}) + "\n")
            return result

    class ObservedApplication(Application):
        def handle(self, request):
            result = super().handle(request)
            with (root / "application.jsonl").open("a") as log:
                log.write(json.dumps({"request": request, "result": result}) + "\n")
            return result

    app = ObservedApplication(StateStore(root / "state", config(root / "config.json")), Shop(), None)
    Server(root / "service.sock", os.getgid(), os.getuid(), app).run()


@contextmanager
def household():
    # /tmp keeps the Darwin Unix socket path below its 104-byte bound.
    with tempfile.TemporaryDirectory(prefix="mc01-", dir=os.environ.get("MC01_SCRATCH", "/tmp")) as directory:
        root = Path(directory)
        (root / "config.json").write_text(json.dumps({
            "household": "MC01-" + uuid.uuid4().hex, "instance": root.name,
            "provider": "mathem", "confirmation_policy": "fresh", "profile_overrides": {},
        }))
        with (root / "service.log").open("w+") as log:
            process = subprocess.Popen([sys.executable, "-I", str(HERE), "--serve", str(root)],
                                       env={"PATH": os.defpath, "HOME": str(root)}, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while not (root / "service.sock").exists():
                    assert process.poll() is None, (root / "service.log").read_text()
                    assert time.monotonic() < deadline, "test socket startup timeout"
                    time.sleep(0.02)
                try:
                    yield root, process
                except BaseException:
                    for name in ("service.log", "bridge.log", "codex.stderr"):
                        path = root / name
                        if path.exists():
                            print(name + ":\n" + path.read_text()[-12000:], file=sys.stderr)
                    raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@asynccontextmanager
async def session(root, *, killable=False):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    args = ["-I", str(HERE), "--bridge", str(root)] if killable else ["-I", str(CORE / "mcp_server.py")]
    params = StdioServerParameters(command=sys.executable, args=args,
                                  env={"MEAL_CONCIERGE_SOCKET": str(root / "service.sock"), "HOME": str(root)}, cwd=str(root))
    with (root / "bridge.log").open("a") as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=15) as client:
                initialized = await client.initialize()
                assert initialized.server_info.name == "meal-concierge"
                assert initialized.server_info.version == "2.0.0"
                yield client, initialized


async def call(client, tool, **args):
    result = await client.call_tool("meal_concierge_" + tool, args)
    assert not result.is_error, result
    assert isinstance(result.structured_content, dict), result
    text = json.loads(result.content[0].text)
    assert text == result.structured_content, "text/structured output diverged"
    return text


async def wait_file(path):
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(0.02)


async def sdk_checks(root, process):
    from core import cart_summary
    from test_meal_concierge_recipes import full_recipe, menu

    async with session(root) as (client, initialized):
        discovered = await client.list_tools()
        source = ast.parse((CORE / "mcp_server.py").read_text())
        expected = {n.name for n in source.body if isinstance(n, ast.FunctionDef) and n.name.startswith("meal_concierge_")}
        assert {t.name for t in discovered.tools} == expected
        for tool in discovered.tools:
            assert tool.input_schema["type"] == "object"
            assert tool.output_schema["type"] == "object"
        schemas = {t.name: t.input_schema for t in discovered.tools}
        assert schemas["meal_concierge_catalog"]["required"] == ["action"]
        assert schemas["meal_concierge_catalog"]["properties"]["action"]["enum"] == ["products", "recipes", "usuals"]
        assert "mathem" in json.dumps(schemas["meal_concierge_email"]["properties"]["provider"])
        status = await call(client, "status")
        marker = json.loads((root / "config.json").read_text())["household"]
        assert status["household"] == marker
        assert status["integration"]["server"]["name"] == root.name
        assert status["currency"] == "SEK"
        await call(client, "setup", action="apply", keep_current=True)
        await call(client, "profile", action="update", changes={"meals": {"portions": 3}})
        catalog = await call(client, "catalog", action="products", query="ägg")
        product = catalog["products"][0]
        assert product["provider"] == "mathem" and product["product_ref"] == 10
        await call(client, "product_favorites", action="add", product_id=str(product["product_id"]), product_name=product["name"], quantity=2)
        favorite = (await call(client, "product_favorites"))["product_favorites"][0]
        assert (favorite["product_id"], favorite["product_name"], favorite["quantity"]) == ("10", "Ägg", 2)
        recipe = full_recipe("Synthetic ägg middag")
        recipe["ingredients"] = [{"raw": "8 stk ägg", "quantity": 8, "unit": "stk", "item": "ägg", "scalable": True}]
        saved = await call(client, "recipe_write", recipe=recipe, idempotency_key="mc01-recipe")
        ref = {"id": saved["recipe"]["id"], "revision": saved["recipe"]["revision"]}
        same = await call(client, "recipe_write", recipe=recipe, idempotency_key="mc01-recipe")
        assert same["recipe"]["id"] == ref["id"]
        fetched = await call(client, "recipes", action="get", recipe_id=ref["id"], revision=ref["revision"])
        assert fetched["recipe"]["id"] == ref["id"]
        saved_menu = (await call(client, "menu", action="save", menu=menu("2026-W40", {"recipe_ref": ref})))["menu"]
        menu_ref = {key: saved_menu[key] for key in ("menu_id", "revision", "digest")}
        plan = (await call(client, "products", menu_ref=menu_ref))["product_plan"]
        assert plan["binding"]["menu_ref"] == menu_ref and plan["status"] == "needs_input"
        assert any(r["reason"] == "exact_candidate_scope_needs_user_approval" for r in plan["unresolved_requirements"]), plan
        approval = {"requirement_id": plan["requirements"][0]["requirement_id"], "candidate_refs": [product["product_ref"]]}
        approved_plan = (await call(client, "products", menu_ref=menu_ref, candidate_approvals=[approval]))["product_plan"]
        assert approved_plan["binding"]["menu_ref"] == menu_ref
        assert approved_plan["requirements"][0]["candidate_approval"]["candidate_refs"] == [10]
        bad = await client.call_tool("meal_concierge_products", {"menu_ref": {**menu_ref, "revision": menu_ref["revision"] + 1}})
        assert bad.is_error and "stale" in bad.content[0].text, bad
        invalid = await client.call_tool("meal_concierge_profile", {"action": "update", "changes": {"meals": {"portions": 0}}})
        assert invalid.is_error and "portion" in invalid.content[0].text, invalid
        schema_error = await client.call_tool("meal_concierge_catalog", {"action": "invalid"})
        assert schema_error.is_error, schema_error
        denied = await call(client, "products", action="apply")
        assert denied["applied"] is False and "request" in denied["reason"]
        await call(client, "cart", action="change", operations=[{"productId": "10", "quantity": 2}])
        manual = await call(client, "checkout", action="prepare")
        assert manual["manual_checkout_required"] and not manual["confirmed"] and manual["currency"] == "SEK"
        await call(client, "cart", action="change", operations=[{"productId": "10", "quantity": -2}])
        await call(client, "product_favorites", action="remove", product_id="10")
        print(json.dumps({"sdk": "passed", "protocol": initialized.protocol_version, "tools": len(expected), "identity": marker}), flush=True)

    async with session(root, killable=True) as (client, _):
        reconnected = await call(client, "menu")
        assert {key: reconnected["menu"][key] for key in menu_ref} == menu_ref
        profile = await call(client, "profile")
        assert profile["profile"]["meals"]["portions"] == 3
        (root / "hold").touch()
        pending = asyncio.create_task(client.call_tool("meal_concierge_cart", {"action": "change", "operations": [{"productId": "10", "quantity": 1}]}))
        await wait_file(root / "dispatched")
        # Kill only this probe's stdio bridge after confirmed dispatch. Do not
        # mistake any timeout for provider success, or retry the lost mutation.
        await asyncio.sleep(1)
        assert not pending.done()
        os.kill(int((root / "bridge.pid").read_text()), signal.SIGKILL)
        from mcp.shared.exceptions import MCPError
        try:
            await pending
        except MCPError as exc:
            assert "closed" in str(exc).lower(), exc
        else:
            raise AssertionError("disconnected MCP call unexpectedly succeeded")
        (root / "release").touch()

    async with session(root) as (client, _):
        # Application completion, not the disconnected client's exception,
        # establishes the actual result. A read/reconcile never redispatches.
        async with asyncio.timeout(10):
            while True:
                records = [json.loads(line) for line in (root / "application.jsonl").read_text().splitlines()]
                if any(r["request"].get("operations") == [{"productId": "10", "quantity": 1}] for r in records):
                    break
                await asyncio.sleep(0.02)
        assert process.poll() is None
        cart = await call(client, "cart")
        assert cart_summary(cart)["items"][0]["quantity"] == 1
        await call(client, "cart", action="reconcile_change")
        writes = [json.loads(line) for line in (root / "provider.jsonl").read_text().splitlines() if json.loads(line)["tool"] == "manipulate_cart"]
        assert len(writes) == 3, writes
        assert (await call(client, "status"))["household"] == marker
        await call(client, "profile", action="reset", paths=["meals.portions"])
        print(json.dumps({"reconnect": "same service/menu/profile", "interruption": "bridge killed after dispatch; Application completed once; no retry", "provider_writes": len(writes)}), flush=True)


def cli_checks(root, service):
    """Real standalone JSON process: no site-packages, same service contract."""
    from test_meal_concierge_recipes import full_recipe
    env = {"PATH": os.defpath, "HOME": str(root), "MEAL_CONCIERGE_SOCKET": str(root / "service.sock")}
    command = [sys.executable, "-S", "-P", str(CORE / "cli.py")]

    def invoke(request, expected=0):
        raw = request if isinstance(request, str) else json.dumps(request)
        result = subprocess.run(command, input=raw, text=True, capture_output=True, env=env, timeout=15)
        assert result.returncode == expected, result.stderr + result.stdout
        return json.loads(result.stdout)

    marker = json.loads((root / "config.json").read_text())["household"]
    assert invoke({"operation": "status"})["result"]["household"] == marker
    assert invoke("{", 2)["dispatched"] is False
    invalid = invoke({"operation": "profile", "action": "update", "changes": {"meals": {"portions": 0}}}, 1)
    assert "portions" in invalid["error"] and "outcome" not in invalid
    request = {"operation": "recipes", "action": "save", "recipe": full_recipe("MC02 CLI"), "idempotency_key": "mc02-cli-1"}
    first = invoke(request)["result"]["recipe"]
    repeated = invoke(request)["result"]["recipe"]
    assert (first["id"], first["revision"]) == (repeated["id"], repeated["revision"])
    request["recipe"]["name"] = "conflicting content"
    assert "idempotency" in invoke(request, 1)["error"]
    ref = invoke({"operation": "recipes", "action": "get", "recipe_id": first["id"], "revision": first["revision"]})
    assert ref["result"]["recipe"]["id"] == first["id"]
    (root / "hold").touch()
    pending = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        pending.stdin.write(json.dumps({"operation": "cart", "action": "change", "operations": [{"productId": "10", "quantity": 1}]}).encode())
        pending.stdin.close()
        deadline = time.monotonic() + 10
        while not (root / "dispatched").exists():
            assert pending.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
        pending.kill()
        pending.wait(timeout=5)
        (root / "release").touch()
        deadline = time.monotonic() + 10
        while True:
            rows = [json.loads(line) for line in (root / "application.jsonl").read_text().splitlines()]
            if any(r["request"].get("operations") == [{"productId": "10", "quantity": 1}] for r in rows):
                break
            assert time.monotonic() < deadline, "provider completion was not observed"
            time.sleep(0.02)
        from core import cart_summary
        assert cart_summary(invoke({"operation": "cart", "action": "get"})["result"])["items"][0]["quantity"] == 1
        invoke({"operation": "cart", "action": "reconcile_change"})
        writes = [json.loads(line) for line in (root / "provider.jsonl").read_text().splitlines() if json.loads(line)["tool"] == "manipulate_cart"]
        assert len(writes) == 1
        assert service.poll() is None
    finally:
        if pending.poll() is None:
            pending.kill()
            pending.wait(timeout=5)
    print(json.dumps({"cli": "passed without site-packages", "lost_response": "one write, read/reconcile, no retry", "exact_key_ref_error": "passed"}), flush=True)


def codex_check(root, executable):
    version = subprocess.check_output([executable, "--version"], text=True).strip()
    server = {"command": sys.executable, "args": ["-I", str(CORE / "mcp_server.py")],
              "env": {"MEAL_CONCIERGE_SOCKET": str(root / "service.sock"), "HOME": str(root)},
              "required": True, "enabled_tools": ["meal_concierge_status", "meal_concierge_profile"],
              "default_tools_approval_mode": "approve"}
    # Build TOML inline tables, without a shell or persistent config mutation.
    def toml(value):
        if isinstance(value, dict):
            return "{" + ",".join(json.dumps(k) + "=" + toml(v) for k, v in value.items()) + "}"
        return json.dumps(value)
    args = [executable, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "-C", str(root),
            "--json", "-c", "mcp_servers=" + toml({"mc01": server}), "-c", "features.shell_tool=false",
            "-c", "features.apps=false", "-c", "features.plugins=false",
            "Use the mc01 MCP tools to call meal_concierge_status once and meal_concierge_profile with action=show once. "
            "This is an authorized disposable synthetic test. Report the exact household marker returned by the tools. "
            "Do not read files, use other tools, change settings, or perform external actions."]
    before = len((root / "application.jsonl").read_text().splitlines())
    result = subprocess.run(args, text=True, capture_output=True, timeout=180)
    (root / "codex.jsonl").write_text(result.stdout)
    (root / "codex.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    completed = [e["item"] for e in events if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "mcp_tool_call"]
    marker = json.loads((root / "config.json").read_text())["household"]
    assert any(c.get("tool") == "meal_concierge_status" and marker in json.dumps(c.get("result")) and c.get("status") == "completed" for c in completed), result.stdout
    assert any(c.get("tool") == "meal_concierge_profile" and c.get("status") == "completed" for c in completed), result.stdout
    recorded = [json.loads(line) for line in (root / "application.jsonl").read_text().splitlines()[before:]]
    for entry in completed:
        assert entry["server"] == "mc01" and entry["error"] is None
        assert any(r["request"]["operation"] == entry["tool"].removeprefix("meal_concierge_")
                   and r["result"] == entry["result"]["structured_content"] for r in recorded), entry
    print(json.dumps({"client": version, "command": args[:-1], "mcp_calls": completed, "test_instance": marker}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--codex", help="path to an already authenticated Codex executable")
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
    elif args.bridge:
        isolated_runtime()
        (args.bridge / "bridge.pid").write_text(str(os.getpid()))
        runpy.run_path(str(CORE / "mcp_server.py"), run_name="__main__")
    else:
        print(json.dumps(isolated_runtime()), flush=True)
        sys.addaudithook(no_external_network)
        with household() as (root, process):
            asyncio.run(sdk_checks(root, process))
            if args.codex:
                codex_check(root, args.codex)
        with household() as (root, process):
            cli_checks(root, process)


if __name__ == "__main__":
    main()
