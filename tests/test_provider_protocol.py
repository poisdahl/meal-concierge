"""Real MCP/HTTP client negotiation against a synthetic provider transport.

Run with Python 3.12 and tests/mcp-requirements.txt, outside the fleet roots.
Only the Hermes OAuth boundary and HTTP provider are synthetic; no credentials
or external network are used. Both providers share RetailMcpClient's transport.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx2
from mcp.types import LATEST_PROTOCOL_VERSION

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retail_mcp import RetailMcpClient, REQUIRED_TOOLS
from core import HouseholdError


class ProviderProtocolTests(unittest.TestCase):
    def test_handshake_and_negotiated_headers_reach_provider(self):
        negotiated = "2025-11-25"
        self.assertNotEqual(LATEST_PROTOCOL_VERSION, negotiated)

        class Storage:
            def __init__(self, *args, **kwargs):
                pass

            def has_cached_tokens(self):
                return True

        class SyntheticAuth(httpx2.Auth):
            def __init__(self, **kwargs):
                pass

        oauth = {
            "tools.mcp_oauth": SimpleNamespace(
                HermesTokenStorage=Storage,
                _build_client_metadata=lambda _: {},
                _configure_callback_port=lambda *args: None,
                _make_callback_waiter=lambda *args, **kwargs: None,
                _make_redirect_handler=lambda *args: None,
            ),
            "tools.mcp_oauth_manager": SimpleNamespace(
                _HERMES_PROVIDER_CLS=SyntheticAuth,
            ),
        }
        real_client = httpx2.AsyncClient
        for provider in ("oda", "mathem"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as root:
                calls = []

                def respond(request):
                    self.assertEqual(request.method, "POST")
                    message = json.loads(request.content)
                    method = message["method"]
                    calls.append(method)
                    header = request.headers.get("mcp-protocol-version")
                    if method == "initialize":
                        self.assertEqual(message["params"]["protocolVersion"], negotiated)
                        # The transport owns negotiation; no incompatible
                        # client-wide default may override its handshake.
                        self.assertIn(header, (None, negotiated))
                        result = {
                            "protocolVersion": negotiated,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "synthetic-provider", "version": "1"},
                        }
                    else:
                        self.assertEqual(header, negotiated)
                        if method == "notifications/initialized":
                            return httpx2.Response(202)
                        if method == "tools/list":
                            result = {"tools": [
                                {"name": name, "inputSchema": {"type": "object"}}
                                for name in sorted(REQUIRED_TOOLS)
                            ]}
                        else:
                            self.assertEqual(method, "tools/call")
                            self.assertIn(message["params"]["name"], {"get_cart", "product_search"})
                            result = {"content": [], "structuredContent": {"synthetic": True}}
                    return httpx2.Response(200, json={
                        "jsonrpc": "2.0", "id": message["id"], "result": result,
                    })

                def client(**kwargs):
                    return real_client(transport=httpx2.MockTransport(respond), **kwargs)

                with patch.dict(sys.modules, oauth), patch.object(httpx2, "AsyncClient", client):
                    client_instance = RetailMcpClient(root, provider=provider)
                    result = client_instance.call("get_cart", {})
                    with self.assertRaisesRegex(HouseholdError, "Product search result changed"):
                        client_instance.call("product_search", {"queries": ["synthetic"]})
                self.assertEqual(result, {"synthetic": True})
                self.assertEqual(calls, [
                    "initialize", "notifications/initialized", "tools/list", "tools/call",
                ] * 2)


if __name__ == "__main__":
    unittest.main()
