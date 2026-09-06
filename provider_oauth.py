"""Owner-local Oda/Mathem OAuth with the pinned MCP SDK and legacy file names.

Every caller holds RetailMcpClient's provider lock. A private write-ahead record
separates uncertain token exchange from recoverable local file publication.
"""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.oauth2 import check_registration_usable
from mcp.client.auth.utils import credentials_match_issuer
from mcp.shared.auth import (
    AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata,
    OAuthMetadata, OAuthToken, ProtectedResourceMetadata,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import HouseholdError


def require_https(value) -> None:
    url = urlsplit(str(value))
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise HouseholdError("Provider OAuth requires an HTTPS endpoint")


def private_json(path: Path, value: dict) -> None:
    """Publish one durable private record; never truncate the previous value."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".oauth-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ProviderTokenStorage:
    def __init__(self, directory: Path, server_name: str, label: str):
        self.directory = Path(directory)
        self.server_name = server_name
        self.label = label
        self.tokens_path = self.directory / f"{server_name}.json"
        self.client_path = self.directory / f"{server_name}.client.json"
        self.meta_path = self.directory / f"{server_name}.meta.json"
        self.transaction_path = self.directory / f"{server_name}.pending.json"

    def read(self, path: Path) -> dict | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(descriptor) as stream:
                value = json.loads(stream.read(1024 * 1024 + 1))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (OSError, ValueError):
            raise HouseholdError(f"{self.label} login storage is invalid") from None

    def recover(self) -> None:
        pending = self.read(self.transaction_path)
        if pending is None or pending.get("status") == "pending":
            return
        if pending.get("status") != "ready":
            raise HouseholdError(f"{self.label} login transaction is invalid")
        try:
            OAuthToken.model_validate(pending["tokens"])
            OAuthClientInformationFull.model_validate(pending["client"])
            OAuthMetadata.model_validate(pending["metadata"])
        except (KeyError, ValueError):
            raise HouseholdError(f"{self.label} login transaction is invalid") from None
        for path, field in ((self.meta_path, "metadata"), (self.client_path, "client"), (self.tokens_path, "tokens")):
            private_json(path, pending[field])
        self.transaction_path.unlink()
        sync_directory(self.directory)

    def require_resolved(self) -> None:
        self.recover()
        if self.transaction_path.exists():
            raise HouseholdError(f"{self.label} login is required: previous token exchange outcome is uncertain")

    def begin_exchange(self) -> None:
        private_json(self.transaction_path, {"status": "pending"})

    def publish(self, tokens: OAuthToken, client: OAuthClientInformationFull, metadata: dict) -> None:
        token_data = tokens.model_dump(mode="json", exclude_none=True)
        if tokens.expires_in is not None:
            token_data["expires_at"] = time.time() + tokens.expires_in
        private_json(self.transaction_path, {
            "status": "ready", "tokens": token_data,
            "client": client.model_dump(mode="json", exclude_none=True), "metadata": metadata,
        })
        self.recover()

    async def get_tokens(self) -> OAuthToken | None:
        data = self.read(self.tokens_path)
        if data is None:
            return None
        try:
            expiry = data.pop("expires_at", None)
            if expiry is None and data.get("expires_in") is not None:
                expiry = self.tokens_path.stat().st_mtime + int(data["expires_in"])
            if expiry is not None:
                data["expires_in"] = max(0, int(expiry - time.time()))
            return OAuthToken.model_validate(data)
        except (OSError, TypeError, ValueError, OverflowError):
            raise HouseholdError(f"{self.label} login storage is invalid") from None

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self.read(self.client_path)
        try:
            return OAuthClientInformationFull.model_validate(data) if data is not None else None
        except ValueError:
            raise HouseholdError(f"{self.label} login registration is invalid") from None

    async def set_client_info(self, value: OAuthClientInformationFull) -> None:
        # The SDK context stages DCR until publish() has the complete grant.
        pass

    async def set_tokens(self, value: OAuthToken) -> None:
        # The SDK context stages tokens until publish() has the complete grant.
        pass

    async def status(self) -> dict:
        """Read-only cutover preflight; no recovery, normalization or refresh."""
        tokens = await self.get_tokens()
        client = await self.get_client_info()
        metadata = self.read(self.meta_path)
        if metadata is not None:
            try:
                OAuthMetadata.model_validate(metadata)
            except ValueError:
                raise HouseholdError(f"{self.label} login metadata is invalid") from None
        pending = self.read(self.transaction_path)
        exchange = pending.get("status") if pending else "none"
        if exchange not in ("none", "pending", "ready"):
            exchange = "invalid"
        return {"tokens_present": tokens is not None, "registration_present": client is not None,
                "metadata_present": metadata is not None,
                "expires_in": tokens.expires_in if tokens else None,
                "exchange": exchange}


class _PrivateOAuthLogs(logging.Filter):
    def filter(self, record):
        # SDK exception values and DEBUG discovery URLs may carry credentials.
        record.msg = "Provider OAuth diagnostic; use the returned login status"
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


logging.getLogger("mcp.client.auth.oauth2").addFilter(_PrivateOAuthLogs())


class ProviderOAuth(OAuthClientProvider):
    def __init__(self, endpoint: str, storage: ProviderTokenStorage, *, login=None):
        metadata = OAuthClientMetadata(
            client_name="Meal Concierge", redirect_uris=[login.uri] if login else None,
            token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
        )
        super().__init__(endpoint, metadata, storage,
                         redirect_handler=login.redirect if login else None,
                         callback_handler=login.callback if login else None)
        self.storage = storage
        self.login = login
        self.saved = False

    async def _initialize(self):
        if self.login and not self.saved:
            self.storage.recover()
        else:
            self.storage.require_resolved()
        await super()._initialize()
        cached = self.storage.read(self.storage.meta_path)
        if cached:
            try:
                self.context.oauth_metadata = OAuthMetadata.model_validate(cached)
                if cached.get("_resource"):
                    self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(cached["_resource"])
                    await self._validate_resource_match(self.context.protected_resource_metadata)
            except ValueError:
                raise HouseholdError(f"{self.storage.label} login metadata is invalid") from None
        if self.login and not self.saved:
            self.context.clear_tokens()  # only memory; the prior login remains intact
        elif self.context.current_tokens:
            self.context.update_token_expiry(self.context.current_tokens)
        if self.context.client_info:
            try:
                check_registration_usable(self.context.client_info)
            except Exception:
                raise HouseholdError(f"{self.storage.label} login registration is unsupported") from None
            if cached and not credentials_match_issuer(self.context.client_info, str(self.context.oauth_metadata.issuer), None):
                raise HouseholdError(f"{self.storage.label} login registration belongs to another issuer")

    def _metadata(self):
        if self.context.oauth_metadata is None:
            raise HouseholdError(f"{self.storage.label} login is required: OAuth metadata is missing")
        for field in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
            endpoint = getattr(self.context.oauth_metadata, field, None)
            if endpoint:
                require_https(endpoint)
        value = self.context.oauth_metadata.model_dump(mode="json", exclude_none=True)
        if self.context.protected_resource_metadata:
            value["_resource"] = self.context.protected_resource_metadata.model_dump(mode="json", exclude_none=True)
        return value

    async def _perform_authorization(self):
        self._metadata()
        return await super()._perform_authorization()

    async def _exchange_token_authorization_code(self, code, verifier):
        request = await super()._exchange_token_authorization_code(code, verifier)
        self._metadata()  # refuse a guessed token endpoint
        self.storage.begin_exchange()
        return request

    async def _handle_token_response(self, response):
        try:
            await super()._handle_token_response(response)
            if not self.context.current_tokens.access_token.strip():
                raise ValueError()
            self.storage.publish(self.context.current_tokens, self.context.client_info, self._metadata())
            self.saved = True
        except Exception:
            raise HouseholdError(f"{self.storage.label} login token exchange did not complete; retry requires explicit login") from None

    async def async_auth_flow(self, request):
        if self.login and not self.saved:
            # This is a bidirectional generator: async-for would lose responses.
            inner = super().async_auth_flow(request)
            try:
                outgoing = await inner.__anext__()
                while True:
                    require_https(outgoing.url)
                    response = yield outgoing
                    outgoing = await inner.asend(response)
            except StopAsyncIteration:
                return
            except Exception:
                raise HouseholdError(f"{self.storage.label} login did not complete") from None
            finally:
                await inner.aclose()
        else:
            async with self.context.lock:
                require_https(request.url)
                # A previous HTTP flow may have lost a rotated token or failed
                # local publication. Reconcile disk before every new dispatch.
                await self._initialize()
                self.context.protocol_version = request.headers.get("mcp-protocol-version")
                if not self.context.current_tokens or not self.context.client_info:
                    raise HouseholdError(f"{self.storage.label} login is required")
                if not self.context.is_token_valid():
                    if not self.context.can_refresh_token():
                        raise HouseholdError(f"{self.storage.label} login is required: token expired")
                    self._metadata()
                    refresh = await self._refresh_token()
                    self.storage.begin_exchange()
                    response = yield refresh
                    if response.status_code != 200:
                        raise HouseholdError(f"{self.storage.label} login is required: token refresh failed")
                    try:
                        tokens = OAuthToken.model_validate_json(await response.aread())
                        if not tokens.access_token.strip():
                            raise ValueError()
                    except ValueError:
                        raise HouseholdError(f"{self.storage.label} login is required: token refresh response is invalid") from None
                    prior = self.context.current_tokens
                    if tokens.refresh_token is None:
                        tokens.refresh_token = prior.refresh_token
                    if tokens.scope is None:
                        tokens.scope = prior.scope
                    self.storage.publish(tokens, self.context.client_info, self._metadata())
                    self.context.current_tokens = tokens
                    self.context.update_token_expiry(tokens)
                self._add_auth_header(request)
                response = yield request
                # Never register, open a browser or replay a provider request.
                if response.status_code in {401, 403}:
                    raise HouseholdError(f"{self.storage.label} login is required: provider rejected authorization")


def build_auth(directory: Path, server_name: str, label: str, endpoint: str) -> ProviderOAuth:
    return ProviderOAuth(endpoint, ProviderTokenStorage(directory, server_name, label))


class LoopbackLogin:
    def __init__(self, storage: ProviderTokenStorage, *, open_browser: bool = True):
        self.storage = storage
        self.open_browser = open_browser
        try:
            data = storage.read(storage.client_path)
            client = OAuthClientInformationFull.model_validate(data) if data is not None else None
            uris = client.redirect_uris if client else None
            uri = str(uris[0]) if uris else "http://127.0.0.1:0/callback"
            parsed = urlsplit(uri)
            port = (parsed.port if parsed.port is not None else 80) if uris else 0
        except (TypeError, ValueError):
            raise HouseholdError("login registration has an invalid callback URI") from None
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.query or parsed.fragment:
            raise HouseholdError("login requires the existing registration's supported loopback redirect")
        self.path = parsed.path or "/"
        self.result = None
        self.expected_state = None
        self.event = threading.Event()
        self.url_path = storage.directory / f"{storage.server_name}.authorize.json"
        login = self

        class Callback(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(2)
                super().setup()

            def log_message(self, *args):
                pass

            def do_GET(self):
                target = urlsplit(self.path)
                query = parse_qs(target.query)
                valid = (target.path == login.path and len(query.get("state", [])) == 1
                         and query["state"][0] == login.expected_state
                         and len(query.get("code", [])) == 1 and not query.get("error")
                         and len(query.get("iss", [])) <= 1)
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Login received. Return to Meal Concierge." if valid else b"Invalid login callback.")
                if valid and not login.event.is_set():
                    login.result = AuthorizationCodeResult(code=query["code"][0], state=query["state"][0], iss=query.get("iss", [None])[0])
                    login.event.set()

        if uris and port == 0:
            raise HouseholdError("existing login redirect has no usable callback port")
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Callback)
        self.uri = uri if uris else f"http://{parsed.hostname}:{self.server.server_port}{self.path}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.url_path.unlink(missing_ok=True)

    async def redirect(self, url):
        self.expected_state = parse_qs(urlsplit(url).query).get("state", [None])[0]
        private_json(self.url_path, {"authorization_url": url})
        print(f"Open the authorization URL in private file {self.url_path}; callback port {self.server.server_port}.", flush=True)
        if self.open_browser:
            webbrowser.open(url)

    async def callback(self):
        while not self.event.is_set():
            await asyncio.sleep(.05)
        return self.result


async def login_client(client, login, timeout):
    auth = ProviderOAuth(client.endpoint, login.storage, login=login)
    try:
        result = await client._run_async(None, {}, timeout, auth=auth)
        if not auth.saved:
            raise HouseholdError("Provider did not complete OAuth login")
        return {"login": "saved", "provider": client.provider, "connection": result["status"]}
    except Exception:
        if auth.saved:
            return {"login": "saved", "provider": client.provider, "connection": "unavailable"}
        raise HouseholdError(f"{client.label} login did not complete; preserve the auth files and inspect --status") from None


def main():
    from retail_mcp import RetailMcpClient
    parser = argparse.ArgumentParser(description="Explicit local provider OAuth login; never orders or changes a cart")
    parser.add_argument("--tokens", type=Path, required=True)
    parser.add_argument("--provider", choices=("oda", "mathem"), required=True)
    parser.add_argument("--status", action="store_true", help="read secret-free stored auth status without changing files or contacting the provider")
    parser.add_argument("--no-browser", action="store_true", help="write only the private authorization URL file for a headless host")
    parser.add_argument("--timeout", type=int, default=300, choices=range(1, 601), metavar="SECONDS")
    args = parser.parse_args()
    client = RetailMcpClient(args.tokens, provider=args.provider)
    try:
        storage = ProviderTokenStorage(args.tokens, client.server_name, client.label)
        if args.status:
            print(json.dumps(asyncio.run(storage.status())))
            return
        args.tokens.mkdir(mode=0o700, parents=True, exist_ok=True)
        with client._lock():
            storage.recover()
            with LoopbackLogin(storage, open_browser=not args.no_browser) as login:
                print(json.dumps(asyncio.run(login_client(client, login, args.timeout))))
    except (HouseholdError, OSError):
        raise SystemExit("Provider login did not complete; inspect the private login state and retry explicitly.") from None


if __name__ == "__main__":
    main()
