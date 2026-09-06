"""Actual Application routing with synthetic store/account markers; no network."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import StateStore
from service import Application
import service
from service_common import email_automation_key


class SyntheticStore:
    def __init__(self, provider, account):
        self.provider = provider
        self.account = account
        self.calls = []

    def probe(self, **kwargs):
        self.calls.append(('probe', {}))
        return {'server': {'name': self.account}, 'tool_count': 11}

    def call(self, tool, arguments, **kwargs):
        self.calls.append((tool, deepcopy(arguments)))
        marker = {'provider': self.provider, 'account': self.account}
        if tool == 'product_search':
            return {**marker, 'products': []}
        if tool == 'get_cart':
            return {**marker, 'items': [], 'count': 0, 'total': 0}
        if tool == 'get_order':
            return {**marker, 'order_number': arguments['order_number'], 'status': 'paid_and_modifiable'}
        if tool == 'order_tracking':
            return {**marker, 'order_id': arguments['order_number'], 'status': 'paid_and_modifiable'}
        raise AssertionError(f'unexpected provider operation: {tool}')


class ProviderRoutingTests(unittest.TestCase):
    def test_real_startup_retains_mcp_followups_without_eager_retained_network(self):
        from test_provider_oauth import ProviderWire, seed, use_wire

        for selected in ("oda", "mathem", "meny"):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                tokens = root / "tokens"
                tokens.mkdir()
                for provider in ("oda", "mathem"):
                    seed(tokens, provider, expired=True)
                original_tokens = {p.name: p.read_bytes() for p in tokens.iterdir()}
                settings = {"provider": selected, "household": "Synthetic startup"}
                config_path = root / "config.json"
                config_path.write_text(json.dumps(settings))
                args = service.parser().parse_args([
                    "--config", str(config_path), "--state", str(root / "state"),
                    "--tokens", str(tokens), "--socket", str(root / "service.sock"),
                    "--browser-home", str(root / "browser"),
                    "--browser-profile", str(root / "browser/profile"),
                    "--browser-socket-directory", str(root / "browser/run"),
                ])
                store = StateStore(args.state, settings)
                with store.locked() as state:
                    state["email_jobs"] = [{
                        "provider": p, "order_id": "synthetic-original-order", "status": "pending",
                        "delivery_date": "2099-09-12", "recipient_snapshot": "synthetic@example.test",
                        "automation_protocol": 4,
                        "automation_key": email_automation_key(p, "synthetic-original-order"),
                    } for p in ("oda", "mathem")]
                original = store.read()
                captured = []
                startup_wire = ProviderWire(selected if selected != "meny" else "oda")
                with use_wire(startup_wire), patch.object(service.Server, "run", lambda server: captured.append(server.app)):
                    service.run(args)
                if selected == "meny":
                    self.assertEqual(startup_wire.requests, [])
                else:
                    self.assertEqual(len(startup_wire.token_forms), 1)
                    self.assertEqual(startup_wire.token_forms[0]["client_id"], [f"synthetic-{selected}-client"])
                for name, data in original_tokens.items():
                    if not name.startswith(f"{selected}-weekly"):
                        self.assertEqual((tokens / name).read_bytes(), data)
                app = captured[0]
                self.assertIs(app.email_provider_clients[selected], app.provider_client)
                for provider in ("oda", "mathem"):
                    wire = ProviderWire(provider, tool_results={
                        "get_order": {"order_number": "synthetic-original-order", "status": "paid_and_modifiable"},
                        "order_tracking": {"order_id": "synthetic-original-order", "status": "paid_and_modifiable"},
                    })
                    with use_wire(wire):
                        result = app.handle({
                            "operation": "email", "action": "reconcile", "provider": provider,
                            "order_id": "synthetic-original-order",
                        })
                    self.assertFalse(result["cancelled"])
                    self.assertEqual(len(wire.token_forms), 0 if provider == selected else 1)
                    if provider != selected:
                        self.assertEqual(wire.token_forms[0]["client_id"], [f"synthetic-{provider}-client"])
                    self.assertCountEqual([
                        {"name": call["name"], "arguments": call["arguments"]}
                        for call in wire.tool_calls
                    ], [
                        {"name": "get_order", "arguments": {"order_number": "synthetic-original-order"}},
                        {"name": "order_tracking", "arguments": {"order_number": "synthetic-original-order"}},
                    ])
                self.assertEqual(store.read(), original)

    def test_selected_store_and_persisted_followup_keep_separate_accounts(self):
        for selected in ('oda', 'mathem', 'meny'):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as directory:
                settings = {'provider': selected, 'household': 'Synthetic routing'}
                store = StateStore(directory, settings)
                clients = {p: SyntheticStore(p, f'{p}-retained-account') for p in ('oda', 'mathem', 'meny')}
                active = SyntheticStore(selected, f'{selected}-active-account')
                with store.locked() as state:
                    state['email_jobs'] = [{
                        'provider': p, 'order_id': 'same-order', 'status': 'pending',
                        'delivery_date': '2026-09-12', 'recipient_snapshot': 'synthetic@example.test',
                        'automation_protocol': 4, 'automation_key': email_automation_key(p, 'same-order'),
                    } for p in clients]
                saved = store.read()
                # Reopen the actual saved state, as service startup does. Even an
                # alternate entry for the selected store must not replace it.
                reopened = StateStore(directory, settings)
                app = Application(reopened, active, None, email_provider_clients=clients)
                status = app.handle({'operation': 'status'})
                self.assertEqual(status['integration']['provider'], selected)
                self.assertEqual(status['integration']['server']['name'], active.account)
                for operation, action, key in (('catalog', 'products', 'products'), ('cart', 'get', 'items')):
                    result = app.handle({'operation': operation, 'action': action, 'query': 'synthetic'})
                    self.assertEqual(result['account'], active.account)
                    self.assertEqual(result[key], [])
                self.assertEqual([tool for tool, _ in active.calls], ['probe', 'product_search', 'get_cart'])
                self.assertTrue(all(not client.calls for client in clients.values()))
                active.calls.clear()
                for bound in clients:
                    result = app.handle({'operation': 'email', 'action': 'reconcile', 'provider': bound, 'order_id': 'same-order'})
                    self.assertFalse(result['cancelled'])
                    expected = active if bound == selected else clients[bound]
                    reads = [('get_order', {'order_number': 'same-order'})]
                    # Selected MENY reads tracking from its order response;
                    # retained providers keep the separate tracking read.
                    if bound != selected or selected != 'meny':
                        reads.append(('order_tracking', {'order_number': 'same-order'}))
                    self.assertCountEqual(expected.calls, reads)
                    for client in [active, *clients.values()]:
                        if client is not expected:
                            self.assertEqual(client.calls, [])
                        client.calls.clear()
                # Read-only recovery leaves provider refs, keys and pending jobs intact.
                self.assertEqual(reopened.read(), saved)

    def test_shipped_preflight_command_keeps_arguments_and_provider_default(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_tokens = str(Path(directory) / 'not-logged-in')
            for entrypoint in ('oda.py', 'retail_mcp.py'):
                with self.subTest(entrypoint=entrypoint):
                    help_result = subprocess.run([sys.executable, str(CORE / entrypoint), '--help'], capture_output=True, text=True)
                    self.assertEqual(help_result.returncode, 0, help_result.stderr)
                    self.assertIn('--provider {oda,mathem}', help_result.stdout)
                    self.assertIn('--tokens', help_result.stdout)
                    self.assertIn('--state', help_result.stdout)
                    for provider, options in [('Oda', []), ('Mathem', ['--provider', 'mathem'])]:
                        result = subprocess.run([sys.executable, str(CORE / entrypoint), '--tokens', missing_tokens, *options], capture_output=True, text=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(f'{provider} login is required', result.stderr)
                        self.assertFalse(Path(missing_tokens).exists())


if __name__ == '__main__':
    unittest.main()
