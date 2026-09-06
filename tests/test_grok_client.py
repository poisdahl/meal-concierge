"""Real SDK/socket/HTTP transport smoke for Grok's synthetic cloud harness.

Run with the pinned runtime's python -I -B. Actual Grok model, file attachment
and rendering observations are separate gates, not simulated by these tests.
"""
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

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent


class GrokRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='meal-concierge-grok-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sock = self.root / 'service.sock'
        self.log = (self.root / 'service.log').open('w+')
        self.addCleanup(self.log.close)
        self.process = subprocess.Popen([
            sys.executable, '-I', '-B', str(HERE / 'grok_runtime_probe.py'),
            '--source', str(SOURCE), '--root', str(self.root / 'data'), '--socket', str(self.sock),
        ], stdout=self.log, stderr=self.log,
            env={'PATH': os.defpath, 'HOME': str(self.root), 'TMPDIR': str(self.root)})
        self.addCleanup(self.stop_service)
        for _ in range(200):
            if self.sock.exists():
                break
            if self.process.poll() is not None:
                self.log.seek(0)
                self.fail(self.log.read())
            await asyncio.sleep(.05)
        self.assertTrue(self.sock.exists())

    def stop_service(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)

    @asynccontextmanager
    async def client(self):
        params = StdioServerParameters(command=sys.executable,
            args=['-I', '-B', str(SOURCE / 'mcp_server.py')],
            env={'MEAL_CONCIERGE_SOCKET': str(self.sock), 'HOME': str(self.root)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=90) as client:
                await client.initialize()
                yield client

    async def call(self, client, name, **arguments):
        result = await client.call_tool('meal_concierge_' + name, arguments)
        self.assertFalse(result.is_error, result)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        return result.structured_content

    async def test_fresh_native_bridge_retains_state_and_uses_actual_http_transport(self):
        async with self.client() as client:
            tools = await client.list_tools()
            self.assertIn('meal_concierge_orders', {item.name for item in tools.tools})
            status = await self.call(client, 'status')
            self.assertIn('MC09 SYNTHETIC', json.dumps(status))
            await self.call(client, 'setup', action='apply', keep_current=True)
            await self.call(client, 'profile', action='update', changes={'meals': {'portions': 3}})
        async with self.client() as client:
            profile = await self.call(client, 'profile')
            self.assertEqual(profile['profile']['meals']['portions'], 3)
            cart = await self.call(client, 'cart', action='get')
            self.assertIn('Fullkornspasta', json.dumps(cart))
        methods = [json.loads(line)['method'] for line in (self.root / 'data/http.jsonl').read_text().splitlines()]
        self.assertIn('initialize', methods)
        self.assertIn('tools/list', methods)
        self.assertIn('tools/call', methods)

    async def test_original_order_binding_and_lost_ack_reconcile_once(self):
        async with self.client() as client:
            prepared = await self.call(client, 'orders', action='cancel_prepare', order_id='mc09-original-order')
            confirmation = prepared['confirmation_id']
            wrong = await client.call_tool('meal_concierge_orders', {
                'action': 'cancel_confirm', 'order_id': 'mc09-unrelated-order', 'confirmation_id': confirmation})
            self.assertTrue(wrong.is_error)
            self.assertIn('cancellation confirmation does not match the prepared order', wrong.content[0].text)
            self.assertFalse((self.root / 'data/browser.jsonl').exists())
            before = json.loads((self.root / 'data/synthetic-provider.json').read_text())
            self.assertEqual(before['cancellations'], [])
            (self.root / 'data/lose-cancel-response').touch()
            uncertain = await client.call_tool('meal_concierge_orders', {
                'action': 'cancel_confirm', 'order_id': 'mc09-original-order', 'confirmation_id': confirmation})
            self.assertTrue(uncertain.is_error)
        async with self.client() as client:
            result = await self.call(client, 'orders', action='cancel_reconcile',
                                     order_id='mc09-original-order', confirmation_id=confirmation)
            self.assertTrue(result['cancelled'])
        provider = json.loads((self.root / 'data/synthetic-provider.json').read_text())
        self.assertEqual(provider['cancellations'], ['mc09-original-order'])
        self.assertEqual(provider['tracking']['mc09-unrelated-order'], 'paid_and_modifiable')

    async def test_unavailable_contract_variants_are_explicit(self):
        async with self.client() as client:
            for mode in ('timeout', 'missing-capability', 'malformed'):
                with self.subTest(mode=mode):
                    (self.root / 'data/fault').write_text(mode)
                    result = await client.call_tool('meal_concierge_cart', {'action': 'get'})
                    self.assertTrue(result.is_error, result)
            # Read-only cart get intentionally returns the provider document.
            # A partial document must retain missing totals, never invent zero.
            (self.root / 'data/fault').write_text('partial')
            partial = await self.call(client, 'cart', action='get')
            self.assertEqual(partial, {'items': []})


if __name__ == '__main__':
    unittest.main()
