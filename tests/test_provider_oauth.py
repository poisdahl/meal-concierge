"""Standalone OAuth acceptance through the real pinned SDK and retail client.

Only external HTTP responses/browser navigation are synthetic. OAuth messages
follow the pinned SDK's RFC discovery, DCR and PKCE contracts; MCP messages use
the existing observed initialize/list/call envelope. No live accounts are used.
Run with the isolated Python 3.12 runtime and tests/mcp-requirements.txt.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager, redirect_stdout
import hashlib
import http.client
import importlib.metadata
import io
import json
import logging
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))
import provider_oauth
from provider_oauth import LoopbackLogin, ProviderTokenStorage, login_client
from retail_mcp import RetailMcpClient, REQUIRED_TOOLS
from core import HouseholdError


TEST_ROOT = Path("/tmp/meal-concierge-mc04-20260906")
ISSUER = "https://login.synthetic.invalid/"
SECRET_MARKER = "SYNTHETIC-SECRET-DO-NOT-LOG"
REAL_ASYNC_CLIENT = httpx2.AsyncClient


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def metadata(issuer=ISSUER):
    return {
        "issuer": issuer,
        "authorization_endpoint": issuer + "authorize",
        "token_endpoint": issuer + "token",
        "registration_endpoint": issuer + "register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["read", "offline_access"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
    }


def seed(directory, provider="oda", *, expired=False, absolute=True, issuer=ISSUER):
    client = RetailMcpClient(directory, provider=provider)
    storage = ProviderTokenStorage(Path(directory), client.server_name, client.label)
    token = {
        "access_token": f"synthetic-{provider}-old",
        "refresh_token": f"synthetic-{provider}-refresh",
        "token_type": "bearer", "expires_in": 3600, "scope": "read offline_access",
    }
    if absolute:
        token["expires_at"] = time.time() + (-60 if expired else 3600)
    write_json(storage.tokens_path, token)
    if expired and not absolute:
        old = time.time() - 7200
        os.utime(storage.tokens_path, (old, old))
    write_json(storage.client_path, {
        "client_id": f"synthetic-{provider}-client",
        "token_endpoint_auth_method": "none",
        "redirect_uris": [], "client_name": None, "client_uri": "",
        "grant_types": ["authorization_code", "refresh_token"],
    })
    write_json(storage.meta_path, metadata(issuer))
    return client, storage


class ProviderWire:
    """Synthetic OAuth provider + MCP endpoint behind an actual httpx2 client."""

    def __init__(self, provider="oda", *, token_outcome="rotate", reject_call=None,
                 unavailable_after_login=False, on_refresh=None, public=False,
                 initial_expires_in=3600, redirect_refresh=None, discovery_issuer=ISSUER,
                 tool_results=None):
        self.provider = provider
        self.endpoint = RetailMcpClient(TEST_ROOT, provider=provider).endpoint
        self.token_outcome = token_outcome
        self.reject_call = reject_call
        self.unavailable_after_login = unavailable_after_login
        self.on_refresh = on_refresh
        self.public = public
        self.initial_expires_in = initial_expires_in
        self.redirect_refresh = redirect_refresh
        self.discovery_issuer = discovery_issuer
        self.tool_results = tool_results or {}
        self.requests = []
        self.methods = []
        self.token_forms = []
        self.registrations = []
        self.tool_calls = []
        self.authorization = None

    async def __call__(self, request):
        self.requests.append((request.method, str(request.url)))
        path = request.url.path
        if path.startswith("/.well-known/oauth-protected-resource"):
            return httpx2.Response(200, json={
                "resource": self.endpoint, "authorization_servers": [self.discovery_issuer],
                "scopes_supported": ["read", "offline_access"],
            })
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json=metadata())
        if path == "/register":
            assert request.method == "POST"
            registration = json.loads(request.content)
            self.registrations.append(registration)
            assert registration["grant_types"] == ["authorization_code", "refresh_token"]
            return httpx2.Response(201, json={
                **registration, "client_id": f"synthetic-{self.provider}-registered",
                "client_secret": None, "client_uri": "", "application_type": None,
            })
        if path == "/token":
            assert request.method == "POST"
            form = parse_qs(request.content.decode())
            self.token_forms.append(form)
            grant = form["grant_type"][0]
            assert grant in {"refresh_token", "authorization_code"}
            if grant == "authorization_code":
                assert self.authorization is not None
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(form["code_verifier"][0].encode()).digest()
                ).rstrip(b"=").decode()
                assert challenge == self.authorization["code_challenge"][0]
                assert form["redirect_uri"] == self.authorization["redirect_uri"]
                assert form["code"] == ["synthetic-authorization-code"]
            elif self.on_refresh:
                self.on_refresh()
            if self.redirect_refresh:
                return httpx2.Response(307, headers={"location": self.redirect_refresh})
            if self.token_outcome == "timeout":
                raise httpx2.ReadTimeout("synthetic provider response lost", request=request)
            if self.token_outcome == "malformed":
                return httpx2.Response(200, json={
                    "access_token": SECRET_MARKER, "expires_in": {"private": SECRET_MARKER},
                })
            if isinstance(self.token_outcome, int):
                return httpx2.Response(self.token_outcome, json={
                    "error": "invalid_grant", "error_description": SECRET_MARKER,
                })
            result = {"access_token": f"synthetic-{self.provider}-new", "expires_in": 3600}
            if self.token_outcome == "empty":
                result["access_token"] = ""
            if grant == "authorization_code":
                result["expires_in"] = self.initial_expires_in
            if self.token_outcome != "omit":
                suffix = "initial-refresh" if grant == "authorization_code" else "rotated"
                result["refresh_token"] = f"synthetic-{self.provider}-{suffix}"
            return httpx2.Response(200, json=result)
        assert str(request.url) == self.endpoint, str(request.url)
        assert request.method == "POST"
        message = json.loads(request.content)
        method = message["method"]
        self.methods.append(method)
        authorization = request.headers.get("authorization")
        if authorization is None and not self.public:
            return httpx2.Response(401, headers={
                "www-authenticate": 'Bearer resource_metadata="' +
                self.endpoint.rsplit("/", 1)[0] + '/.well-known/oauth-protected-resource/mcp"',
            })
        assert self.public or authorization in {
            f"Bearer synthetic-{self.provider}-old", f"Bearer synthetic-{self.provider}-new",
        }
        if self.unavailable_after_login:
            return httpx2.Response(503)
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"synthetic-{self.provider}-provider", "version": "1"},
            }
        elif method == "notifications/initialized":
            return httpx2.Response(202)
        elif method == "tools/list":
            result = {"tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in sorted(REQUIRED_TOOLS)
            ]}
        elif method == "tools/call":
            self.tool_calls.append(message["params"])
            if self.reject_call:
                return httpx2.Response(self.reject_call, headers={
                    "www-authenticate": 'Bearer error="insufficient_scope", scope="additional"',
                })
            value = self.tool_results.get(message["params"]["name"], {"synthetic": True})
            result = {"content": [], "structuredContent": value}
        else:
            raise AssertionError(method)
        return httpx2.Response(200, json={
            "jsonrpc": "2.0", "id": message["id"], "result": result,
        })


@contextmanager
def use_wire(wire):
    def client(**kwargs):
        return REAL_ASYNC_CLIENT(transport=httpx2.MockTransport(wire), **kwargs)

    with patch.object(httpx2, "AsyncClient", client), patch.dict(sys.modules, {
        "tools.mcp_oauth": None, "tools.mcp_oauth_manager": None,
    }):
        yield


def process_call(directory, output, release=None):
    """Spawned process executes the production flock and provider auth path."""
    def paused_refresh():
        output.put(("refresh_started",))
        if not release.wait(15):
            raise RuntimeError("test release was not signalled")

    wire = ProviderWire(on_refresh=paused_refresh if release is not None else None)
    try:
        with use_wire(wire):
            result = RetailMcpClient(directory).call("get_cart", {})
        output.put(("success", result, len(wire.token_forms)))
    except Exception as exc:
        output.put(("error", type(exc).__name__, str(exc), len(wire.requests)))


class ProviderOAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for distribution in ("mcp", "mcp-types"):
            if importlib.metadata.version(distribution) != "2.1.1":
                raise AssertionError(f"{distribution} must be pinned to 2.1.1")
        TEST_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oauth-acceptance-", dir=TEST_ROOT)
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def snapshot(self, directory=None):
        return {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode & 0o777)
            for path in (directory or self.directory).iterdir() if path.is_file()
        }

    def test_cached_legacy_token_uses_real_sdk_without_hermes(self):
        client, storage = seed(self.directory)
        before = {path: value for path, value in self.snapshot().items()}
        wire = ProviderWire()
        with use_wire(wire):
            self.assertEqual(client.call("get_cart", {}), {"synthetic": True})
        self.assertEqual(wire.methods, ["initialize", "notifications/initialized", "tools/list", "tools/call"])
        self.assertEqual(wire.token_forms, [])
        self.assertEqual(wire.registrations, [])
        self.assertEqual(storage.tokens_path.read_bytes(), before[storage.tokens_path.name][0])

    def test_absolute_and_legacy_mtime_expiry_refresh_before_dispatch(self):
        for absolute in (True, False):
            with self.subTest(absolute=absolute):
                client, storage = seed(self.directory, expired=True, absolute=absolute)
                wire = ProviderWire()
                with use_wire(wire):
                    self.assertEqual(client.call("get_cart", {}), {"synthetic": True})
                self.assertEqual(wire.requests[0], ("POST", ISSUER + "token"))
                self.assertEqual(len(wire.token_forms), 1)
                self.assertEqual(wire.token_forms[0]["refresh_token"], ["synthetic-oda-refresh"])
                self.assertEqual(wire.token_forms[0]["client_id"], ["synthetic-oda-client"])
                saved = json.loads(storage.tokens_path.read_text())
                self.assertEqual(saved["refresh_token"], "synthetic-oda-rotated")
                self.assertEqual(saved["scope"], "read offline_access")
                self.assertGreater(saved["expires_at"], time.time() + 3500)
                self.assertFalse(storage.transaction_path.exists())
                self.assertEqual(storage.tokens_path.stat().st_mode & 0o777, 0o600)

    def test_refresh_omissions_retain_refresh_token_and_scope_across_restart(self):
        client, storage = seed(self.directory, expired=True)
        wire = ProviderWire(token_outcome="omit")
        with use_wire(wire):
            client.call("get_cart", {})
            RetailMcpClient(self.directory).call("get_cart", {})
        self.assertEqual(len(wire.token_forms), 1)
        token = json.loads(storage.tokens_path.read_text())
        self.assertEqual(token["refresh_token"], "synthetic-oda-refresh")
        self.assertEqual(token["scope"], "read offline_access")

    def test_401_and_403_never_register_or_replay_mutation(self):
        client, storage = seed(self.directory)
        original = storage.tokens_path.read_bytes()
        for status in (401, 403):
            with self.subTest(status=status):
                wire = ProviderWire(reject_call=status)
                with use_wire(wire), self.assertRaisesRegex(HouseholdError, "rejected authorization"):
                    client.call("manipulate_cart", {"synthetic": True})
                self.assertEqual(wire.methods.count("tools/call"), 1)
                self.assertEqual(wire.registrations, [])
                self.assertEqual(wire.token_forms, [])
                self.assertEqual(storage.tokens_path.read_bytes(), original)

    def test_missing_login_fails_before_any_provider_request(self):
        wire = ProviderWire()
        with use_wire(wire), self.assertRaisesRegex(HouseholdError, "login is required"):
            RetailMcpClient(self.directory).call("get_cart", {})
        self.assertEqual(wire.requests, [])

    def test_uncertain_refresh_survives_errors_timeout_and_restart(self):
        for outcome in ("timeout", "malformed", 400, 503):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory(dir=self.directory) as root:
                client, storage = seed(Path(root), expired=True)
                old = storage.tokens_path.read_bytes()
                wire = ProviderWire(token_outcome=outcome)
                with use_wire(wire), self.assertRaises(HouseholdError) as first:
                    client.call("get_cart", {})
                self.assertNotIn(SECRET_MARKER, str(first.exception))
                self.assertEqual(len(wire.token_forms), 1)
                self.assertEqual(wire.methods, [])
                self.assertEqual(storage.tokens_path.read_bytes(), old)
                self.assertEqual(json.loads(storage.transaction_path.read_text())["status"], "pending")
                restarted = ProviderWire()
                with use_wire(restarted), self.assertRaisesRegex(HouseholdError, "outcome is uncertain"):
                    RetailMcpClient(root).call("get_cart", {})
                self.assertEqual(restarted.requests, [])

    def test_same_auth_instance_cannot_repeat_uncertain_refresh(self):
        client, storage = seed(self.directory, expired=True)
        wire = ProviderWire(token_outcome="timeout")

        async def attempt_twice():
            auth = provider_oauth.ProviderOAuth(client.endpoint, storage)
            async with REAL_ASYNC_CLIENT(transport=httpx2.MockTransport(wire), auth=auth) as http:
                with self.assertRaises(httpx2.ReadTimeout):
                    await http.post(client.endpoint, json={"method": "initialize"})
                with self.assertRaisesRegex(HouseholdError, "outcome is uncertain"):
                    await http.post(client.endpoint, json={"method": "initialize"})

        with client._lock():
            asyncio.run(attempt_twice())
        self.assertEqual(len(wire.token_forms), 1)
        self.assertEqual(wire.methods, [])

    def test_refresh_redirects_do_not_forward_credentials(self):
        for target in (ISSUER + "moved-token", "https://elsewhere.synthetic.invalid/collect"):
            with self.subTest(target=target), tempfile.TemporaryDirectory(dir=self.directory) as root:
                client, storage = seed(Path(root), expired=True)
                wire = ProviderWire(redirect_refresh=target)
                with use_wire(wire), self.assertRaisesRegex(HouseholdError, "redirect"):
                    client.call("get_cart", {})
                self.assertEqual(wire.requests, [("POST", ISSUER + "token")])
                self.assertEqual(json.loads(storage.transaction_path.read_text())["status"], "pending")

    def test_insecure_stored_metadata_fails_before_refresh_dispatch(self):
        for field in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
            with self.subTest(field=field):
                client, storage = seed(self.directory, expired=True)
                value = metadata()
                value[field] = "http://insecure.synthetic.invalid/endpoint"
                write_json(storage.meta_path, value)
                wire = ProviderWire()
                with use_wire(wire), self.assertRaisesRegex(HouseholdError, "HTTPS"):
                    client.call("get_cart", {})
                self.assertEqual(wire.requests, [])
                self.assertFalse(storage.transaction_path.exists())

    def test_ready_transaction_recovers_after_local_publication_failure(self):
        client, storage = seed(self.directory, expired=True)
        old = storage.tokens_path.read_bytes()
        original_write = provider_oauth.private_json
        failed = []

        def fail_publication(path, value):
            if path == storage.client_path and not failed:
                failed.append(True)
                raise OSError("synthetic publication interruption")
            return original_write(path, value)

        wire = ProviderWire()
        with use_wire(wire), patch.object(provider_oauth, "private_json", fail_publication):
            with self.assertRaises(HouseholdError):
                client.call("get_cart", {})
        self.assertEqual(storage.tokens_path.read_bytes(), old)
        self.assertEqual(json.loads(storage.transaction_path.read_text())["status"], "ready")
        with use_wire(wire):
            self.assertEqual(RetailMcpClient(self.directory).call("get_cart", {}), {"synthetic": True})
        self.assertEqual(len(wire.token_forms), 1)
        self.assertFalse(storage.transaction_path.exists())
        self.assertEqual(json.loads(storage.tokens_path.read_text())["refresh_token"], "synthetic-oda-rotated")

    def test_provider_identity_and_locks_remain_separate(self):
        oda, oda_storage = seed(self.directory, "oda", expired=True)
        mathem, mathem_storage = seed(self.directory, "mathem", expired=True)
        self.assertNotEqual(oda.endpoint, mathem.endpoint)
        self.assertNotEqual(oda.server_name, mathem.server_name)
        old_mathem = mathem_storage.tokens_path.read_bytes()
        with oda._lock(), mathem._lock():
            self.assertTrue((self.directory / ".oda-household.lock").exists())
            self.assertTrue((self.directory / ".mathem-household.lock").exists())
        with use_wire(ProviderWire("oda")):
            oda.call("get_cart", {})
        self.assertEqual(mathem_storage.tokens_path.read_bytes(), old_mathem)
        new_oda = oda_storage.tokens_path.read_bytes()
        wire = ProviderWire("mathem")
        with use_wire(wire):
            mathem.call("get_cart", {})
        self.assertEqual(wire.token_forms[0]["client_id"], ["synthetic-mathem-client"])
        self.assertEqual(wire.token_forms[0]["refresh_token"], ["synthetic-mathem-refresh"])
        self.assertEqual(oda_storage.tokens_path.read_bytes(), new_oda)

    def test_concurrent_processes_have_one_refresh_owner(self):
        seed(self.directory, expired=True)
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        release = context.Event()
        processes = []
        try:
            first = context.Process(target=process_call, args=(str(self.directory), output, release))
            processes.append(first)
            first.start()
            self.assertEqual(output.get(timeout=15), ("refresh_started",))
            second = context.Process(target=process_call, args=(str(self.directory), output))
            processes.append(second)
            second.start()
            blocked = output.get(timeout=15)
            self.assertEqual(blocked[0:2], ("error", "HouseholdError"))
            self.assertIn("another Oda operation is active", blocked[2])
            self.assertEqual(blocked[3], 0)
            release.set()
            self.assertEqual(output.get(timeout=15), ("success", {"synthetic": True}, 1))
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
            third = context.Process(target=process_call, args=(str(self.directory), output))
            processes.append(third)
            third.start()
            self.assertEqual(output.get(timeout=15), ("success", {"synthetic": True}, 0))
            third.join(timeout=15)
            self.assertEqual(third.exitcode, 0)
        finally:
            release.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            output.close()
            output.join_thread()

    def browser(self, wire, login, *, invalid_first=False):
        def open_browser(url):
            wire.authorization = parse_qs(urlsplit(url).query)
            self.assertEqual(wire.authorization["code_challenge_method"], ["S256"])
            self.assertEqual(wire.authorization["redirect_uri"], [login.uri])
            self.assertEqual(wire.authorization["resource"], [wire.endpoint])
            self.assertIn("offline_access", wire.authorization["scope"][0].split())
            self.assertEqual(json.loads(login.url_path.read_text())["authorization_url"], url)
            self.assertEqual(login.url_path.stat().st_mode & 0o777, 0o600)
            target = urlsplit(login.uri)

            def send(state):
                connection = http.client.HTTPConnection("127.0.0.1", target.port, timeout=3)
                try:
                    connection.request("GET", target.path + "?" + urlencode({
                        "code": "synthetic-authorization-code", "state": state, "iss": ISSUER,
                    }))
                    response = connection.getresponse()
                    status, body = response.status, response.read()
                    self.assertNotIn(b"synthetic-authorization-code", body)
                    return status
                finally:
                    connection.close()

            if invalid_first:
                self.assertEqual(send("wrong-state"), 400)
                self.assertFalse(login.event.is_set())
            self.assertEqual(send(wire.authorization["state"][0]), 200)
            return True
        return open_browser

    def test_explicit_login_uses_sdk_discovery_pkce_and_real_callback(self):
        for provider in ("oda", "mathem"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory(dir=self.directory) as root:
                client = RetailMcpClient(root, provider=provider)
                storage = ProviderTokenStorage(Path(root), client.server_name, client.label)
                wire = ProviderWire(provider)
                output = io.StringIO()
                with client._lock(), LoopbackLogin(storage) as login, redirect_stdout(output):
                    with use_wire(wire), patch.object(provider_oauth.webbrowser, "open", self.browser(wire, login, invalid_first=True)):
                        result = asyncio.run(login_client(client, login, 5))
                    self.assertEqual(result, {"login": "saved", "provider": provider, "connection": "ready"})
                    self.assertEqual(len(wire.registrations), 1)
                    self.assertEqual(wire.token_forms[0]["grant_type"], ["authorization_code"])
                    self.assertEqual(wire.methods, ["initialize", "initialize", "notifications/initialized", "tools/list"])
                    self.assertNotIn("code_challenge", output.getvalue())
                    self.assertNotIn(wire.authorization["state"][0], output.getvalue())
                self.assertFalse(login.url_path.exists())
                self.assertFalse(storage.transaction_path.exists())
                self.assertEqual(json.loads(storage.client_path.read_text())["issuer"], ISSUER)
                self.assertEqual(json.loads(storage.client_path.read_text())["redirect_uris"], [login.uri])
                restarted = ProviderWire(provider)
                with use_wire(restarted):
                    self.assertEqual(RetailMcpClient(root, provider=provider).call("get_cart", {}), {"synthetic": True})
                self.assertEqual(restarted.token_forms, [])

    def test_public_mcp_success_does_not_claim_login_saved(self):
        client = RetailMcpClient(self.directory)
        storage = ProviderTokenStorage(self.directory, client.server_name, client.label)
        wire = ProviderWire(public=True)
        with client._lock(), LoopbackLogin(storage, open_browser=False) as login:
            with use_wire(wire), self.assertRaises(HouseholdError):
                asyncio.run(login_client(client, login, 5))
        self.assertEqual(wire.registrations, [])
        self.assertEqual(wire.token_forms, [])
        self.assertFalse(storage.tokens_path.exists())

    def test_initial_expiry_refreshes_durably_in_same_login_connection(self):
        client = RetailMcpClient(self.directory)
        storage = ProviderTokenStorage(self.directory, client.server_name, client.label)
        wire = ProviderWire(initial_expires_in=0)
        with client._lock(), LoopbackLogin(storage) as login, redirect_stdout(io.StringIO()):
            with use_wire(wire), patch.object(provider_oauth.webbrowser, "open", self.browser(wire, login)):
                result = asyncio.run(login_client(client, login, 5))
        self.assertEqual(result["connection"], "ready")
        self.assertEqual([form["grant_type"][0] for form in wire.token_forms], ["authorization_code", "refresh_token"])
        self.assertEqual(wire.token_forms[1]["refresh_token"], ["synthetic-oda-initial-refresh"])
        self.assertEqual(json.loads(storage.tokens_path.read_text())["refresh_token"], "synthetic-oda-rotated")
        self.assertFalse(storage.transaction_path.exists())

    def test_empty_initial_access_token_is_not_published(self):
        client, storage = seed(self.directory)
        before = {path: path.read_bytes() for path in (storage.tokens_path, storage.client_path, storage.meta_path)}
        wire = ProviderWire(token_outcome="empty")
        with client._lock(), LoopbackLogin(storage) as login, redirect_stdout(io.StringIO()):
            with use_wire(wire), patch.object(provider_oauth.webbrowser, "open", self.browser(wire, login)):
                with self.assertRaises(HouseholdError):
                    asyncio.run(login_client(client, login, 5))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(json.loads(storage.transaction_path.read_text())["status"], "pending")

    def test_insecure_discovery_is_not_contacted(self):
        client = RetailMcpClient(self.directory)
        storage = ProviderTokenStorage(self.directory, client.server_name, client.label)
        wire = ProviderWire(discovery_issuer="http://insecure.synthetic.invalid/")
        with client._lock(), LoopbackLogin(storage, open_browser=False) as login:
            with use_wire(wire), self.assertRaises(HouseholdError):
                asyncio.run(login_client(client, login, 5))
        self.assertTrue(all(url.startswith("https://") for _, url in wire.requests))
        self.assertEqual(wire.registrations, [])
        self.assertEqual(wire.token_forms, [])

    def test_idle_browser_preconnect_does_not_block_callback_or_shutdown(self):
        client = RetailMcpClient(self.directory)
        storage = ProviderTokenStorage(self.directory, client.server_name, client.label)
        wire = ProviderWire()
        login = LoopbackLogin(storage)
        login.__enter__()
        idle = socket.create_connection(("127.0.0.1", login.server.server_port), timeout=2)
        shutdown = None
        try:
            with client._lock(), redirect_stdout(io.StringIO()), use_wire(wire):
                with patch.object(provider_oauth.webbrowser, "open", self.browser(wire, login)):
                    result = asyncio.run(login_client(client, login, 5))
            self.assertEqual(result["connection"], "ready")
            shutdown = threading.Thread(target=login.__exit__, daemon=True)
            shutdown.start()
            shutdown.join(timeout=3)
            self.assertFalse(shutdown.is_alive(), "an idle socket blocked callback shutdown")
        finally:
            idle.close()
            if shutdown is None:
                login.__exit__()
            else:
                shutdown.join(timeout=3)

    def test_cancelled_reregistration_preserves_existing_credentials(self):
        client, storage = seed(self.directory, issuer="https://old.synthetic.invalid/")
        old_client = json.loads(storage.client_path.read_text())
        old_client["issuer"] = "https://old.synthetic.invalid/"
        write_json(storage.client_path, old_client)
        before = {path: path.read_bytes() for path in (storage.client_path, storage.tokens_path, storage.meta_path)}
        wire = ProviderWire()
        with client._lock(), LoopbackLogin(storage, open_browser=False) as login, redirect_stdout(io.StringIO()):
            with use_wire(wire), self.assertRaises((HouseholdError, TimeoutError)):
                asyncio.run(login_client(client, login, .15))
        self.assertEqual(len(wire.registrations), 1)
        self.assertEqual(wire.token_forms, [])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse(login.url_path.exists())
        self.assertFalse(storage.transaction_path.exists())

    def test_existing_loopback_redirect_is_reused_and_saved_token_survives_outage(self):
        client, storage = seed(self.directory)
        with LoopbackLogin(storage, open_browser=False) as reserved:
            uri = reserved.uri.replace("127.0.0.1", "localhost")
        old_client = json.loads(storage.client_path.read_text())
        old_client["redirect_uris"] = [uri]
        write_json(storage.client_path, old_client)
        wire = ProviderWire(unavailable_after_login=True)
        with client._lock(), LoopbackLogin(storage) as login, redirect_stdout(io.StringIO()):
            self.assertEqual(login.uri, uri)
            with use_wire(wire), patch.object(provider_oauth.webbrowser, "open", self.browser(wire, login)):
                result = asyncio.run(login_client(client, login, 5))
        self.assertEqual(result, {"login": "saved", "provider": "oda", "connection": "unavailable"})
        self.assertEqual(wire.registrations, [])
        self.assertEqual(json.loads(storage.tokens_path.read_text())["access_token"], "synthetic-oda-new")
        self.assertEqual(json.loads(storage.client_path.read_text())["redirect_uris"], [uri])

    def test_login_errors_do_not_log_token_response_or_callback_secrets(self):
        client = RetailMcpClient(self.directory)
        storage = ProviderTokenStorage(self.directory, client.server_name, client.label)
        wire = ProviderWire(token_outcome="malformed")
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        logger = logging.getLogger("mcp.client.auth.oauth2")
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with client._lock(), LoopbackLogin(storage) as login, redirect_stdout(capture):
                with use_wire(wire), patch.object(provider_oauth.webbrowser, "open", self.browser(wire, login)):
                    with self.assertRaises(HouseholdError) as failed:
                        asyncio.run(login_client(client, login, 5))
            self.assertNotIn(SECRET_MARKER, capture.getvalue() + str(failed.exception))
            self.assertNotIn(wire.authorization["state"][0], capture.getvalue())
            self.assertNotIn("synthetic-authorization-code", capture.getvalue())
            self.assertFalse(storage.tokens_path.exists())
            self.assertFalse(storage.client_path.exists())
            self.assertEqual(json.loads(storage.transaction_path.read_text())["status"], "pending")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

    def test_status_is_secret_free_and_does_not_recover_or_create_files(self):
        _, storage = seed(self.directory)
        for pending in ("pending", "ready", {"secret": SECRET_MARKER}):
            with self.subTest(pending=pending):
                write_json(storage.transaction_path, {"status": pending})
                before = self.snapshot()
                status = asyncio.run(storage.status())
                self.assertEqual(status["exchange"], pending if isinstance(pending, str) else "invalid")
                self.assertTrue(status["tokens_present"])
                self.assertEqual(self.snapshot(), before)
                self.assertNotIn("synthetic-oda", json.dumps(status))
                self.assertNotIn(SECRET_MARKER, json.dumps(status))
        missing = self.directory / "absent"
        result = subprocess.run([
            sys.executable, "-I", "-B", str(PRODUCT / "provider_oauth.py"),
            "--tokens", str(missing), "--provider", "oda", "--status",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["tokens_present"])
        self.assertFalse(missing.exists())

    def test_application_uses_native_auth_and_retains_original_provider_followup(self):
        from core import StateStore
        from service import Application
        from service_common import email_automation_key

        state_root = self.directory / "household"
        token_root = self.directory / "tokens"
        token_root.mkdir(mode=0o700)
        oda, oda_storage = seed(token_root, "oda", expired=True)
        mathem, _ = seed(token_root, "mathem", expired=True)
        store = StateStore(state_root, {"provider": "mathem", "household": "Synthetic OAuth acceptance"})
        with store.locked() as state:
            state["email_jobs"] = [{
                "provider": "oda", "order_id": "synthetic-original-order", "status": "pending",
                "delivery_date": "2099-09-12", "recipient_snapshot": "synthetic@example.test",
                "automation_protocol": 4,
                "automation_key": email_automation_key("oda", "synthetic-original-order"),
            }]
        original = store.read()
        active_wire = ProviderWire("mathem", tool_results={"get_cart": {"items": [], "count": 0, "total": 0}})
        with use_wire(active_wire):
            app = Application(store, mathem, None, email_provider_clients={"oda": oda})
            status = app.handle({"operation": "status"})
            cart = app.handle({"operation": "cart", "action": "get"})
        self.assertEqual(status["integration"]["provider"], "mathem")
        self.assertEqual(status["integration"]["status"], "ready")
        self.assertEqual(status["integration"]["server"]["name"], "synthetic-mathem-provider")
        self.assertEqual(cart["items"], [])
        self.assertEqual(len(active_wire.token_forms), 1)
        self.assertEqual(json.loads(oda_storage.tokens_path.read_text())["access_token"], "synthetic-oda-old")
        retained_wire = ProviderWire("oda", tool_results={
            "order_tracking": {"order_id": "synthetic-original-order", "status": "paid_and_modifiable"},
            "get_order": {"order_number": "synthetic-original-order", "status": "paid_and_modifiable"},
        })
        with use_wire(retained_wire):
            result = app.handle({
                "operation": "email", "action": "reconcile", "provider": "oda",
                "order_id": "synthetic-original-order",
            })
        self.assertFalse(result["cancelled"])
        self.assertEqual(len(retained_wire.token_forms), 1)
        self.assertEqual(retained_wire.token_forms[0]["client_id"], ["synthetic-oda-client"])
        self.assertEqual([
            {"name": call["name"], "arguments": call["arguments"]}
            for call in retained_wire.tool_calls
        ], [
            {"name": "order_tracking", "arguments": {"order_number": "synthetic-original-order"}},
            {"name": "get_order", "arguments": {"order_number": "synthetic-original-order"}},
        ])
        self.assertEqual(StateStore(state_root, store.config).read(), original)


if __name__ == "__main__":
    unittest.main()
