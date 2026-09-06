"""MC41 real MCP/CLI -> Unix Server/Application with synthetic local recipes.

Run with the pinned MCP 2.1.1 Python using -I -B. This verifies local-bank
selection during a source outage, not an Oda/Mathem recipe API contract.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve()
SOURCE = HERE.parents[1]
SCRATCH = Path(os.environ.get("MC41_SCRATCH", tempfile.gettempdir()))
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


def synthetic_recipe(index):
    return {
        "name": f"Synthetic dinner {index}", "portions": 2,
        "ingredients": [{"raw": f"200 g gulrot {index}", "item": f"gulrot {index}",
                         "quantity": 200, "unit": "g", "scalable": True}],
        "steps": ["Prepare this synthetic ingredient carefully. " * 50],
        "times": {"active_minutes": 30}, "tags": ["Synthetic"],
        "source": {"kind": "user", "publisher": "MC41 synthetic fixture",
                   "external_id": str(index), "relationship": "user_supplied"},
        "rights": {"storage": "full", "credit": "Synthetic test content"},
    }


def serve(root, empty):
    sys.path.insert(0, str(SOURCE))
    from core import HouseholdError, StateStore
    from service import Application, Server

    def no_network(event, args):
        if event in {"socket.connect", "socket.bind"}:
            assert args[0].family == socket.AF_UNIX, "test attempted non-Unix network access"
    sys.addaudithook(no_network)

    class UnavailableProvider:
        def probe(self, **kwargs):
            return {"protocol_version": "2025-11-25", "tool_count": 0,
                    "server": {"name": "MC41 synthetic unavailable source", "version": "fixture"}}

        def call(self, tool, arguments, **kwargs):
            with (root / "provider.jsonl").open("a") as log:
                log.write(json.dumps({"tool": tool, "arguments": arguments}) + "\n")
            if tool != "recipe_search":
                raise AssertionError("unexpected provider operation: " + tool)
            raise HouseholdError("synthetic recipe source unavailable")

    config = {"household": "MC41 runtime synthetic", "instance": root.name, "provider": "oda",
              "confirmation_policy": "fresh", "profile_overrides": {"recipes": {"sources": {
                  "internal": True, "oda": True, "meny": False, "mathem": False,
                  "themealdb": False, "wikibooks": False}}}}
    app = Application(StateStore(root / "state", config), UnavailableProvider(), None)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        origins = {}
        if not empty:
            for index in range(1, 8):
                if index % 2:
                    stored = app.recipes.save(synthetic_recipe(index), idempotency_key=f"seed-{index}")
                else:
                    stored = app.recipes.import_pack_record(synthetic_recipe(index), pack_id="synthetic-mc41",
                        recipe_id=str(index), version="1")["recipe"]
                origins[stored["id"]] = stored["entry_origin"]
            # Drafts make the compact catalog genuinely span multiple pages.
            for index in range(8, 30):
                app.recipes.save(synthetic_recipe(index), status="draft", idempotency_key=f"seed-{index}")
        manifest_path.write_text(json.dumps({"origins": origins}))
    Server(root / "s.sock", os.getgid(), os.getuid(), app).run()


@unittest.skipUnless(MCP_AVAILABLE, "requires the pinned MCP 2.1.1 runtime")
class RecipeSelectionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertEqual(importlib.metadata.version("mcp"), "2.1.1")
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="rt-", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sock = self.root / "s.sock"
        self.process = None
        self.addCleanup(self.stop_service)
        self.empty = self._testMethodName == "test_empty_bank_and_unavailable_provider_never_authorize_ai"
        await self.start_service()

    async def start_service(self):
        log = (self.root / "service.log").open("a")
        self.addCleanup(log.close)
        args = [sys.executable, "-I", "-B", str(HERE), "--serve", str(self.root)]
        if self.empty:
            args.append("--empty")
        self.process = subprocess.Popen(args, stdout=log, stderr=log,
            env={"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root)})
        for _ in range(200):
            if self.process.poll() is not None:
                self.fail((self.root / "service.log").read_text())
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(.1)
                    connection.connect(str(self.sock))
                    connection.sendall(b'{"operation":"health","contract":1}\n')
                    response = json.loads(connection.recv(65536))
                    if response.get("ok"):
                        return
            except (OSError, ValueError):
                pass
            await asyncio.sleep(.05)
        self.fail("service health startup timeout")

    def stop_service(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    @asynccontextmanager
    async def client(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(command=sys.executable,
            args=["-I", "-B", str(SOURCE / "mcp_server.py")], cwd=str(self.root),
            env={"MEAL_CONCIERGE_SOCKET": str(self.sock), "HOME": str(self.root), "TMPDIR": str(self.root)})
        with (self.root / "bridge.log").open("a") as log:
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=120) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.server_info.name, "meal-concierge")
                    yield client

    async def call(self, client, tool, **arguments):
        result = await client.call_tool("meal_concierge_" + tool, arguments)
        self.assertFalse(result.is_error, result)
        self.assertIsInstance(result.structured_content, dict)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        return result.structured_content

    async def cli(self, request):
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-B", str(SOURCE / "cli.py"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root),
                 "MEAL_CONCIERGE_SOCKET": str(self.sock)})
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), timeout=120)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        self.assertEqual(process.returncode, 0, stderr.decode() + stdout.decode())
        response = json.loads(stdout)
        self.assertTrue(response["ok"])
        return response["result"]

    def bank_counts(self):
        with sqlite3.connect(f"file:{self.root / 'state/recipes.sqlite3'}?mode=ro", uri=True) as db:
            return (db.execute("SELECT count(*) FROM recipes").fetchone()[0],
                    db.execute("SELECT count(*) FROM recipe_favorites WHERE is_favorite=1").fetchone()[0])

    @staticmethod
    def week():
        future = datetime.now(ZoneInfo("Europe/Oslo")).date() + timedelta(days=7)
        return future.strftime("%G-W%V")

    async def test_compact_pages_and_no_ref_week_save_cli_restart(self):
        before = self.bank_counts()
        origins = json.loads((self.root / "manifest.json").read_text())["origins"]
        async with self.client() as client:
            self.assertIn("meal_concierge_menu", {tool.name for tool in (await client.list_tools()).tools})
            await self.call(client, "setup", action="apply", keep_current=True)
            page = await self.call(client, "recipe_discovery", projection="summary", source="internal", limit=3)
            self.assertIsNotNone(page["next_cursor"])
            following = await self.call(client, "recipe_discovery", projection="summary", source="internal",
                                        limit=3, cursor=page["next_cursor"])
            self.assertTrue({r["recipe_ref"]["id"] for r in page["recipes"]}.isdisjoint(
                r["recipe_ref"]["id"] for r in following["recipes"]))
            full = []
            for summary in page["recipes"]:
                self.assertNotIn("steps", summary)
                self.assertNotIn("ingredients", summary)
                self.assertEqual(summary["detail_fields"]["ingredients"], "not_loaded")
                ref = summary["recipe_ref"]
                detail = (await self.call(client, "recipes", action="get", recipe_id=ref["id"], revision=ref["revision"]))["recipe"]
                self.assertEqual((detail["id"], detail["revision"]), (ref["id"], ref["revision"]))
                full.append(detail)
            summary_bytes = len(json.dumps(page["recipes"]).encode())
            full_bytes = len(json.dumps(full).encode())
            self.assertLess(summary_bytes, full_bytes / 2)
            started = time.monotonic()
            planned = (await self.call(client, "menu", action="plan", planner_input={"week": self.week()}))["plan"]
            cold_seconds = time.monotonic() - started
            self.assertEqual(planned["status"], "planned")
            slots = planned["selection"]["slots"]
            self.assertEqual(len(slots), 7)
            self.assertEqual(len({slot["date"] for slot in slots}), 7)
            self.assertEqual({origins[slot["reference"]["recipe_ref"]["id"]] for slot in slots}, {"user", "bundled"})
            self.assertFalse(planned["discovery"]["ai_fallback_eligible"])
            self.assertEqual(next(s["status"] for s in planned["discovery"]["sources"] if s["source"] == "oda"), "unavailable")
            self.assertEqual(self.bank_counts(), before)
            started = time.monotonic()
            via_cli = (await self.cli({"operation": "menu", "action": "plan", "planner_input": {"week": self.week()}}))["plan"]
            warm_seconds = time.monotonic() - started
            self.assertEqual(via_cli["selection_digest"], planned["selection_digest"])
            print(json.dumps({"mc41_runtime": {"summary_bytes": summary_bytes, "full_bytes": full_bytes,
                "mcp_cold_seconds": round(cold_seconds, 3), "cli_warm_seconds": round(warm_seconds, 3),
                "explored_states": planned["explored_states"], "discovery_work": planned["discovery"]["work"]}}), flush=True)
            saved = (await self.call(client, "menu", action="save", planner_handoff=planned["save_handoff"]))["menu"]
            repeated = await self.call(client, "menu", action="save", planner_handoff=planned["save_handoff"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(repeated["menu"], saved)
        self.stop_service()
        await self.start_service()
        async with self.client() as client:
            restored = (await self.call(client, "menu"))["menu"]
            self.assertEqual(restored, saved)
        self.assertEqual(self.bank_counts(), before)
        calls = [json.loads(line) for line in (self.root / "provider.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertEqual({call["tool"] for call in calls}, {"recipe_search"})
        state = json.loads((self.root / "state/state.json").read_text())
        self.assertEqual(state["order_snapshots"], {})
        self.assertIsNone(state["cart_plan"])

    async def test_empty_bank_and_unavailable_provider_never_authorize_ai(self):
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            result = (await self.call(client, "menu", action="plan", planner_input={"week": self.week()}))["plan"]
            self.assertNotEqual(result["status"], "planned")
            self.assertFalse(result["discovery"]["ai_fallback_eligible"])
            self.assertEqual(result["discovery"]["suitable_count"], 0)
            self.assertEqual(next(s["status"] for s in result["discovery"]["sources"] if s["source"] == "oda"), "unavailable")
        self.assertEqual(self.bank_counts(), (0, 0))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]), "--empty" in sys.argv[3:])
    else:
        unittest.main()
