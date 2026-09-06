"""Generated packages attach to one existing production service; no client auth."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client_package_probe as probe


@asynccontextmanager
async def connect(plugin):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    server = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["meal_concierge"]
    params = StdioServerParameters(**{key: server[key] for key in ("command", "args", "env")})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=10) as session:
            await session.initialize()
            yield session


class ClientPackages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pkg-", dir=os.environ.get("MC05_SCRATCH", "/tmp"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "service"
        probe.prepare(self.root)

    def test_both_native_package_shapes_share_release_skill_and_service(self):
        with probe.service(self.root) as process:
            plugins = [probe.build(client, self.root, self.root / client) for client in ("codex", "claude-code")]
            for plugin in plugins:
                self.assertEqual((plugin / "skills/meal-concierge/SKILL.md").read_bytes(),
                                 (self.root / "code/current/skill/SKILL.md").read_bytes())
                self.assertFalse((plugin / "state").exists())
                self.assertFalse((plugin / "service.py").exists())

            async def exercise():
                async with connect(plugins[0]) as first, connect(plugins[1]) as second:
                    inventory = await first.list_tools()
                    self.assertIn("meal_concierge_setup", {x.name for x in inventory.tools})
                    update = await first.call_tool("meal_concierge_profile", {"action": "update", "changes": {"meals": {"portions": 3}}})
                    self.assertFalse(update.is_error, update)
                    result = await second.call_tool("meal_concierge_profile", {"action": "show"})
                    self.assertFalse(result.is_error, result)
                    self.assertEqual(result.structured_content["profile"]["meals"]["portions"], 3)
                    self.assertEqual(json.loads(result.content[0].text), result.structured_content)
            asyncio.run(exercise())
            self.assertIsNone(process.poll(), "bridge teardown stopped the shared service")
        with probe.service(self.root):
            async def reconnect():
                async with connect(plugins[0]) as session:
                    result = await session.call_tool("meal_concierge_profile", {"action": "show"})
                    self.assertEqual(result.structured_content["profile"]["meals"]["portions"], 3)
            asyncio.run(reconnect())

    def test_unavailable_service_creates_no_package_and_no_core(self):
        output = self.root / "unavailable"
        with self.assertRaises(subprocess.CalledProcessError):
            probe.build("codex", self.root, output)
        self.assertFalse(output.exists())
        self.assertFalse(probe.healthy(self.root))

    def test_existing_output_is_preserved(self):
        output = self.root / "existing"
        output.mkdir()
        (output / "keep").write_text("unrelated")
        with probe.service(self.root):
            with self.assertRaises(FileExistsError):
                probe.build("codex", self.root, output)
        self.assertEqual((output / "keep").read_text(), "unrelated")
        self.assertEqual(list(output.iterdir()), [output / "keep"])

    def test_changed_release_skill_changes_native_cache_version(self):
        with probe.service(self.root):
            first = probe.build("codex", self.root, self.root / "first")
            source = self.root / "code/current/skill/SKILL.md"
            source.write_text(source.read_text() + "\nSynthetic updated release instruction.\n")
            second = probe.build("codex", self.root, self.root / "second")
            before = json.loads((first / ".codex-plugin/plugin.json").read_text())
            after = json.loads((second / ".codex-plugin/plugin.json").read_text())
            self.assertNotEqual(before["version"], after["version"])
            self.assertIn("Synthetic updated release instruction.", (second / "skills/meal-concierge/SKILL.md").read_text())

    def test_native_probe_rejects_cache_bound_to_another_household(self):
        with probe.service(self.root):
            market = self.root / "marketplace"
            plugin = probe.build("codex", self.root, market)
        catalog = market / ".agents/plugins/marketplace.json"
        metadata = json.loads(catalog.read_text())
        metadata["name"] = "mc05-test-20260906"
        probe.write_json(catalog, metadata)
        version = json.loads((plugin / ".codex-plugin/plugin.json").read_text())["version"]
        client_home = self.root / "client"
        cache = client_home / "plugins/cache/mc05-test-20260906/meal-concierge" / version
        cache.mkdir(parents=True)
        wrong = json.loads((plugin / ".mcp.json").read_text())
        wrong["mcpServers"]["meal_concierge"]["env"]["MEAL_CONCIERGE_SOCKET"] = "/other-household/service.sock"
        probe.write_json(cache / ".mcp.json", wrong)
        with patch.dict(os.environ, {"CODEX_HOME": str(client_home)}):
            with self.assertRaisesRegex(AssertionError, "not this synthetic service"):
                probe.codex_command(self.root, market, "must never dispatch")

    def test_second_owner_fails_without_replacing_listener(self):
        with probe.service(self.root) as first:
            second = subprocess.run([sys.executable, "-I", str(probe.HERE), "--serve", str(self.root)],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(second.returncode, 0)
            self.assertIsNone(first.poll())
            self.assertTrue(probe.healthy(self.root))


if __name__ == "__main__":
    unittest.main()
