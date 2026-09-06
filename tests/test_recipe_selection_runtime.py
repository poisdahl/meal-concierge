"""MC41 real MCP/CLI -> Unix Server/Application with synthetic local recipes.

Run with the pinned MCP 2.1.1 Python using -I -B. This verifies local-bank
selection during a source outage, not an Oda/Mathem recipe API contract.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
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
class MenuProjectionTests(unittest.TestCase):
    def test_rejection_groups_preserve_order_missing_metadata_and_input(self):
        spec = importlib.util.spec_from_file_location("menu_projection_test", SOURCE / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        first = {"name": "Draft", "recipe_ref": {"id": "one", "revision": 1},
                 "hard_constraints": {"status": "fail", "reasons": ["draft"]},
                 "detail_fields": {"steps": "not_loaded"}, "source": {"author": None}}
        second = {**first, "name": "Other", "recipe_ref": {"id": "two", "revision": 2}}
        missing = {"name": "No metadata", "source": {"author": None}}
        explicit_null = {**missing, "detail_fields": None}
        rejected = [first, second, missing, explicit_null, first]
        original = {"status": "no_plan", "discovery": {"rejected": rejected, "unknown": ["kept"]}}
        before = deepcopy(original)
        projected = module._menu_plan_projection(original)
        groups = projected["discovery"]["rejected_groups"]
        self.assertEqual([len(group["recipes"]) for group in groups], [2, 1, 1, 1])
        self.assertEqual(groups[0]["hard_constraints"], first["hard_constraints"])
        self.assertEqual(groups[0]["detail_fields"], first["detail_fields"])
        reconstructed = [{**recipe, **{key: value for key, value in group.items() if key != "recipes"}}
                         for group in groups for recipe in group["recipes"]]
        self.assertEqual(reconstructed, rejected)
        self.assertEqual(projected["discovery"]["unknown"], ["kept"])
        self.assertEqual(original, before)


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
        text = json.loads(result.content[0].text)
        if tool == "menu":
            self.assertEqual(len(result.content), 1)
            self.assertEqual(result.content[0].type, "text")
            self.assertIsNone(result.structured_content)
            self.assertIsInstance(text, dict)
            self.assertEqual(result.content[0].text, json.dumps(text, ensure_ascii=False, separators=(",", ":")))
            self.last_menu_text = result.content[0].text
        else:
            self.assertIsInstance(result.structured_content, dict)
            self.assertEqual(text, result.structured_content)
        return text

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
            setup_gate = await self.call(client, "menu", action="plan", planner_input={"week": self.week()})
            self.assertNotIn("plan", setup_gate)
            self.assertTrue(setup_gate["configuration_required"])
            self.assertEqual(setup_gate, await self.cli({"operation": "menu", "action": "plan", "interactive": True,
                                                       "planner_input": {"week": self.week()}}))
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
            self.assertEqual(via_cli["save_handoff"], {**planned["save_ref"], "selection": planned["selection"]})
            self.assertLess(len(json.dumps(planned["save_ref"])), len(json.dumps(via_cli["save_handoff"])) / 4)
            self.assertEqual(planned["alternatives"], [])
            print(json.dumps({"mc41_runtime": {"summary_bytes": summary_bytes, "full_bytes": full_bytes,
                "mcp_cold_seconds": round(cold_seconds, 3), "cli_warm_seconds": round(warm_seconds, 3),
                "explored_states": planned["explored_states"], "discovery_work": planned["discovery"]["work"]}}), flush=True)
            saved = (await self.call(client, "menu", action="save", planner_ref=planned["save_ref"]))["menu"]
            repeated = await self.call(client, "menu", action="save", planner_ref=planned["save_ref"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(repeated["menu"], saved)
        self.stop_service()
        await self.start_service()
        async with self.client() as client:
            restored = (await self.call(client, "menu"))["menu"]
            self.assertEqual(restored, saved)
            replay = await self.call(client, "menu", action="save", planner_ref=planned["save_ref"])
            self.assertTrue(replay["idempotent"])
            self.assertEqual(replay["menu"], saved)
        self.assertEqual(self.bank_counts(), before)
        calls = [json.loads(line) for line in (self.root / "provider.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertEqual({call["tool"] for call in calls}, {"recipe_search"})
        state = json.loads((self.root / "state/state.json").read_text())
        self.assertEqual(state["order_snapshots"], {})
        self.assertIsNone(state["cart_plan"])

    async def test_three_complete_mcp_alternatives_save_and_stale_rejection(self):
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            request = {"week": self.week(), "alternatives": 3}
            planned = (await self.call(client, "menu", action="plan", planner_input=request))["plan"]
            # Regression: 22 draft rejections previously pushed three handoffs to 68,840 chars.
            # The native acceptance client truncates each whole result at 64,000.
            self.assertLess(len(self.last_menu_text), 64000)
            print(json.dumps({"mc41_mcp_three_alternatives_chars": len(self.last_menu_text)}), flush=True)
            full = (await self.cli({"operation": "menu", "action": "plan", "planner_input": request}))["plan"]
            choices = [{"save_ref": planned["save_ref"], "selection": planned["selection"]}, *planned["alternatives"]]
            handoffs = [{**choice["save_ref"], "selection": choice["selection"]} for choice in choices]
            self.assertEqual(len(handoffs), 3)
            self.assertEqual(handoffs, full["save_handoffs"])
            self.assertEqual(full["selection"], full["selections"][0])
            self.assertEqual(full["request"], full["canonical_input"]["request"])
            self.assertEqual(planned["effective_profile"], full["canonical_input"]["profile"])
            self.assertEqual(planned["effective_feedback"], full["canonical_input"]["feedback"])
            for field in ("candidate_evaluations", "work_limits", "input_digest", "selection_digest",
                          "planner_version", "explored_states", "cooking_experiences"):
                self.assertEqual(planned[field], full[field])
            for field in ("sources", "unknown"):
                self.assertEqual(planned["discovery"][field], full["discovery"][field])
            restored_rejections = []
            for group in planned["discovery"]["rejected_groups"]:
                metadata = {key: value for key, value in group.items() if key != "recipes"}
                restored_rejections.extend({**recipe, **metadata} for recipe in group["recipes"])
            self.assertEqual(restored_rejections, full["discovery"]["rejected"])
            self.assertEqual(len(restored_rejections), 22)
            self.assertTrue({"canonical_input", "selections", "save_handoff", "save_handoffs", "request"}.isdisjoint(planned))

            # A failed plan has no handoff carrying its request or explanation.
            insufficient = {"week": self.week(), "candidates": full["request"]["candidates"][:1]}
            no_plan = (await self.call(client, "menu", action="plan", planner_input=insufficient))["plan"]
            no_plan_cli = (await self.cli({"operation": "menu", "action": "plan", "planner_input": insufficient}))["plan"]
            self.assertEqual(no_plan["status"], "no_plan")
            for field in ("request", "issues", "candidate_evaluations"):
                self.assertEqual(no_plan[field], no_plan_cli[field])
            self.assertNotIn("save_ref", no_plan)

            await self.call(client, "profile", action="update", changes={"meals": {"portions": 3}})
            stale = await client.call_tool("meal_concierge_menu", {"action": "save", "planner_ref": choices[1]["save_ref"]})
            self.assertTrue(stale.is_error)
            self.assertIn("stale", stale.content[0].text.lower())
            self.assertIsNone((await self.call(client, "menu"))["menu"])
            fresh = (await self.call(client, "menu", action="plan", planner_input=request))["plan"]
            alternative = fresh["alternatives"][1]["save_ref"]
            saved = (await self.call(client, "menu", action="save", planner_ref=alternative))["menu"]
            self.assertEqual((await self.call(client, "menu"))["menu"], saved)
            self.assertEqual(saved["planner_selection"]["selection_digest"], alternative["selection_digest"])

    async def test_resolved_handoff_supports_unsaved_products_and_feedback(self):
        async with self.client() as client:
            await self.call(client, "setup", action="apply", keep_current=True)
            planned = (await self.call(client, "menu", action="plan",
                                       planner_input={"week": self.week()}))["plan"]
            handoff = (await self.call(client, "menu", action="resolve_handoff",
                                      planner_ref=planned["save_ref"]))["planner_handoff"]
            self.assertEqual(handoff, {**planned["save_ref"], "selection": planned["selection"]})
            self.assertIsNone((await self.call(client, "menu"))["menu"])
            # Explicit synthetic pantry coverage avoids any product-provider effects.
            decisions = [{"source": {"collection": "dishes", "recipe_index": i,
                                      "ingredient_index": 0}, "action": "have_all"}
                         for i in range(7)]
            prepared = await self.call(client, "products", action="prepare",
                                       planner_handoff=handoff, ingredient_decisions=decisions)
            self.assertEqual(prepared["product_plan"]["binding"]["planner_handoff"], handoff)
            self.assertEqual(prepared["product_plan"]["requirements"], [])
            accepted = await self.call(client, "feedback", action="accept",
                                      planner_handoff=handoff, idempotency_key="accept-resolved")
            replay = await self.call(client, "feedback", action="accept",
                                    planner_handoff=handoff, idempotency_key="accept-resolved")
            self.assertEqual(replay["event"], accepted["event"])
            fresh = (await self.call(client, "menu", action="plan",
                                     planner_input={"week": self.week()}))["plan"]
            current = (await self.call(client, "menu", action="resolve_handoff",
                                      planner_ref=fresh["save_ref"]))["planner_handoff"]
            slot = current["selection"]["slots"][0]
            await self.call(client, "feedback", action="reject", planner_handoff=current,
                            recipe_key=slot["recipe_key"], reference=slot["reference"],
                            idempotency_key="reject-resolved")
            stale = await client.call_tool("meal_concierge_menu", {
                "action": "resolve_handoff", "planner_ref": fresh["save_ref"]})
            self.assertTrue(stale.is_error)
            self.assertIn("stale", stale.content[0].text.lower())
            self.assertIsNone((await self.call(client, "menu"))["menu"])
        calls = [json.loads(line) for line in (self.root / "provider.jsonl").read_text().splitlines()]
        self.assertEqual({row["tool"] for row in calls}, {"recipe_search"})

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
