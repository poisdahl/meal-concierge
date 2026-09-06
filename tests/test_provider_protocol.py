"""Real MCP/HTTP client negotiation against a synthetic provider transport.

Run with Python 3.12 and tests/mcp-requirements.txt, outside the fleet roots.
Only HTTP provider responses and stored credentials are synthetic; the native
OAuth and MCP code run without Hermes or external network. Both providers share RetailMcpClient's transport.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
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

        real_client = httpx2.AsyncClient
        for provider in ("oda", "mathem"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as root:
                client_instance = RetailMcpClient(root, provider=provider)
                Path(root, client_instance.server_name + ".json").write_text(json.dumps({"access_token": "synthetic-access"}))
                Path(root, client_instance.server_name + ".client.json").write_text(json.dumps({"client_id": "synthetic-client"}))
                calls = []

                def respond(request):
                    self.assertEqual(request.method, "POST")
                    self.assertEqual(request.headers["authorization"], "Bearer synthetic-access")
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

                with patch.object(httpx2, "AsyncClient", client):
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
