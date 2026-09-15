"""Optional providers: real setup, private socket clients, bounded API boundaries."""
from copy import deepcopy
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(SOURCE.parents[1] / "scripts" / "tests"))
import test_meal_concierge_planner as fixtures
import recipe_import_sources as transport
import recipe_search_setup as setup
from service import Server
from web_recipes import search_web, DEFAULT_WEB_SEARCH


class OptionalSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({"household": "Search test"}))
        self.settings = deepcopy(DEFAULT_WEB_SEARCH)
        self.settings["broad"] = True

    def configure(self, backend="brave", anonymous=False):
        with patch("recipe_search_setup.os.isatty", return_value=True), patch("recipe_search_setup.getpass.getpass", return_value="synthetic-secret-marker"):
            result = setup.configure(self.config_path, backend, state_directory=self.root / "state", anonymous=anonymous)
        self.config = json.loads(self.config_path.read_text())
        self.assertNotIn("synthetic-secret-marker", json.dumps(result))
        self.assertNotIn("synthetic-secret-marker", self.config_path.read_text())
        return result

    def brave_response(self, url, **kwargs):
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(kwargs["search_api_key"], "synthetic-secret-marker")
        self.assertEqual(params["country"], ["NO"])
        # Brave accepts the language code "nb"; "no" is rejected with HTTP 422.
        self.assertEqual(params["search_lang"], ["nb"])
        self.assertEqual(params["safesearch"], ["strict"])
        return json.dumps({"type": "search", "query": {"original": params["q"][0]},
            "web": {"results": [{"title": "Kikertgryte", "url": "https://recipes.example/kikertgryte"}]}}).encode(), "application/json"

    def test_setup_keeps_key_private_outside_state_and_preserves_config(self):
        status = self.configure()
        self.assertEqual(self.config["household"], "Search test")
        self.assertEqual(status["backend"], "brave")
        self.assertTrue(status["credential_available"])
        self.assertFalse(status["authentication_verified"])
        key = Path(self.config["recipe_search"]["api_key_file"])
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(key.is_relative_to(self.root / "state"))
        self.configure("direct")
        self.assertTrue(key.exists())  # Switching does not silently delete credentials.
        self.assertEqual(self.config["recipe_search"], {"backend": "direct"})

    def test_setup_refuses_config_inside_state_before_prompt_or_secret_write(self):
        before = self.config_path.read_bytes()
        with patch("recipe_search_setup.getpass.getpass") as prompt:
            with self.assertRaisesRegex(ValueError, "outside"):
                setup.configure(self.config_path, "brave", state_directory=self.root)
            prompt.assert_not_called()
        self.assertFalse((self.root / "secrets").exists())
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_noninteractive_key_setup_leaves_config_unchanged(self):
        before = self.config_path.read_bytes()
        with patch("recipe_search_setup.os.isatty", return_value=False), patch("recipe_search_setup.getpass.getpass") as prompt:
            with self.assertRaisesRegex(ValueError, "interactive"):
                setup.configure(self.config_path, "brave", state_directory=self.root / "state")
            prompt.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_configured_default_brave_executes_once_and_does_not_store(self):
        self.configure()
        with patch("recipe_import_sources._get_bytes", side_effect=self.brave_response) as fetch, patch("recipe_import_sources.firecrawl_request") as firecrawl:
            result = search_web(self.settings, "kikertgryte", config=self.config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["backend"], "brave")
        self.assertTrue(result["broad_searched"])
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(len(result["results"]), 1)
        self.assertFalse(result["persisted"])
        self.assertNotIn("synthetic-secret-marker", json.dumps(result))
        fetch.assert_called_once()
        firecrawl.assert_not_called()

    def test_custom_only_query_is_scoped_and_local_filters_still_apply(self):
        self.configure()
        self.settings.update(broad=False, sites=[{"name": "Custom", "domain": "recipes.example", "enabled": True}])
        with patch("recipe_import_sources._get_bytes", side_effect=self.brave_response) as fetch:
            result = search_web(self.settings, "kikertgryte", config=self.config)
        self.assertEqual(len(result["results"]), 1)
        self.assertFalse(result["broad_searched"])
        self.assertIn("site:recipes.example", parse_qs(urlsplit(fetch.call_args.args[0]).query)["q"][0])
        self.settings["sites"][0]["domain"] = "other.example"
        with patch("recipe_import_sources._get_bytes", side_effect=self.brave_response):
            self.assertEqual(search_web(self.settings, "kikertgryte", config=self.config)["results"], [])

    def test_api_results_exclude_disabled_subdomains_nonpublic_and_non_https(self):
        self.settings["sites"][0]["enabled"] = False
        urls = ["https://sub.matprat.no/soup", "https://user:pass@recipes.example/soup",
                "https://127.0.0.1/soup", "https://localhost/soup", "https://recipes.example:8443/soup",
                "http://recipes.example/soup", "https://recipes.example/", "https://recipes.example:invalid/soup"]
        urls += ["https://recipes.example/soup" + str(i) for i in range(12)]
        with patch("recipe_import_sources.firecrawl_request", return_value={"web": [{"url": u} for u in urls]}):
            result = search_web(self.settings, "soup", backend="firecrawl")
        self.assertEqual(len(result["results"]), 8)
        self.assertTrue(all(r["url"].startswith("https://recipes.example/soup") for r in result["results"]))

    def test_brave_forwards_exclusions_and_refuses_oversized_scope_without_network(self):
        self.configure()
        self.settings["sites"][0]["enabled"] = False
        with patch("recipe_import_sources._get_bytes", side_effect=self.brave_response) as fetch:
            search_web(self.settings, "kikertgryte", config=self.config)
        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)["q"][0]
        self.assertIn("-site:matprat.no", query)
        self.settings.update(broad=False, sites=[{"name": "Site", "domain": f"{'x' * 40}{i}.example", "enabled": True} for i in range(32)])
        with patch("recipe_import_sources._get_bytes") as fetch:
            result = search_web(self.settings, "soup", config=self.config)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("too long", result["reason"])
        fetch.assert_not_called()

    def test_malformed_api_hits_differ_from_real_zero_results(self):
        self.configure()
        for hits in ([{}], [{"url": 17}], [{"url": "https://recipes.example/soup", "title": []}], {}, None, []):
            expected = "completed" if hits == [] else "unavailable"
            def response(url, **kwargs):
                query = parse_qs(urlsplit(url).query)["q"][0]
                return json.dumps({"type": "search", "query": {"original": query}, "web": {"results": hits}}).encode(), "application/json"
            with self.subTest(hits=hits), patch("recipe_import_sources._get_bytes", side_effect=response):
                result = search_web(self.settings, "soup", config=self.config)
                self.assertEqual(result["status"], expected)
            with patch("recipe_import_sources.firecrawl_request", return_value={"web": hits}):
                self.assertEqual(search_web(self.settings, "soup", backend="firecrawl")["status"], expected)

    def test_invalid_json_query_mismatch_error_page_not_no_results(self):
        self.configure()
        for raw, kind in [(b"{", "application/json"), (b"{}", "application/json"),
                (b'{"type":"search","query":{"original":"other"},"web":{"results":[]}}', "application/json"),
                (b"captcha", "text/html")]:
            with self.subTest(raw=raw), patch("recipe_import_sources._get_bytes", return_value=(raw, kind)):
                self.assertEqual(search_web(self.settings, "soup", config=self.config)["status"], "unavailable")

    def test_disabled_search_reads_no_key_and_calls_no_network(self):
        self.settings["enabled"] = False
        with patch("recipe_search_setup.load_search_key") as key, patch("recipe_import_sources._get_bytes") as fetch:
            result = search_web(self.settings, "soup", config={"recipe_search": "broken"})
        self.assertEqual(result["status"], "disabled")
        key.assert_not_called()
        fetch.assert_not_called()

    def test_missing_key_no_provider_fallback_and_explicit_host_works(self):
        config = {"recipe_search": {"backend": "brave"}}
        with patch("recipe_import_sources._get_bytes") as fetch, patch("recipe_import_sources.firecrawl_request") as firecrawl:
            result = search_web(self.settings, "soup", config=config)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["backend"], "brave")
            self.assertFalse(result["broad_searched"])
            self.assertEqual(result["coverage"], "none")
            self.assertTrue(result["pending_scopes"])
            result = search_web(self.settings, "soup", backend="host", config=config)
            self.assertEqual(result["status"], "host_search_required")
            self.assertFalse(result["searched"])
            fetch.assert_not_called()
            firecrawl.assert_not_called()

    def test_auth_rate_timeout_errors_never_fallback(self):
        self.configure()
        for error in ["HTTP 401", "HTTP 403", "HTTP 429", "source response timed out"]:
            with self.subTest(error=error), patch("recipe_import_sources._get_bytes", side_effect=transport.RecipeImportSourceError(error)) as fetch, patch("recipe_import_sources.firecrawl_request") as fallback:
                result = search_web(self.settings, "soup", config=self.config)
            self.assertEqual(result["status"], "unavailable")
            fetch.assert_called_once()
            fallback.assert_not_called()

    def test_key_files_reject_symlink_world_readable_and_bad_key_without_echo(self):
        self.configure()
        path = Path(self.config["recipe_search"]["api_key_file"])
        valid = path.read_bytes()
        for raw in (b"broken", b'{"api_key":"synthetic-secret-marker\\r\\nInjected:value"}', b"x" * 8193):
            path.write_bytes(raw)
            status = setup.search_status(self.config)
            self.assertEqual(status["status"], "unavailable")
            self.assertNotIn("synthetic-secret-marker", json.dumps(status))
        path.write_bytes(valid)
        path.chmod(0o644)
        self.assertEqual(setup.search_status(self.config)["status"], "unavailable")
        path.chmod(0o600)
        link = path.with_name("linked.json")
        link.symlink_to(path)
        self.config["recipe_search"]["api_key_file"] = str(link)
        self.assertEqual(setup.search_status(self.config)["status"], "unavailable")

    def test_firecrawl_key_belongs_only_to_configured_search_not_scraping(self):
        self.configure("firecrawl")
        with patch("recipe_import_sources.firecrawl_request", return_value={"web": []}) as fetch:
            result = search_web(self.settings, "soup", config=self.config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(fetch.call_args.kwargs, {"api_key": "synthetic-secret-marker"})
        self.configure("brave")
        with patch("recipe_import_sources.firecrawl_request", return_value={"web": []}) as fetch:
            search_web(self.settings, "soup", backend="firecrawl", config=self.config)
        self.assertEqual(fetch.call_args.kwargs, {})

    def test_real_cli_and_mcp_use_service_default_without_backend_argument(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        fixture = fixtures.WeeklyPlannerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.store.config["recipe_search"] = {"backend": "firecrawl"}
        socket_path = self.root / "test.sock"
        server = Server(socket_path, os.getgid(), os.getuid(), fixture.app)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            listener.listen(2)
            listener.settimeout(20)
            def serve():
                for _ in range(4):
                    connection, _ = listener.accept()
                    server._serve(connection)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            env = {**os.environ, "MEAL_CONCIERGE_SOCKET": str(socket_path)}
            with patch("recipe_import_sources.firecrawl_request", return_value={"web": [{"url": "https://matprat.no/oppskrifter/soup", "title": "Soup"}]}):
                cli = subprocess.run([sys.executable, str(SOURCE / "cli.py")], input=json.dumps({"operation": "recipes", "action": "web_search", "query": "soup"}), env=env, text=True, capture_output=True, timeout=20)
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertEqual(json.loads(cli.stdout)["result"]["backend"], "firecrawl")
                async def mcp():
                    params = StdioServerParameters(command=sys.executable, args=[str(SOURCE / "mcp_server.py")], env=env)
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            result = await client.call_tool("meal_concierge_recipe_web_search", {"query": "soup"})
                            return result.structured_content
                self.assertEqual(asyncio.run(mcp())["backend"], "firecrawl")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(fixture.app.recipes.search(), [])


class SearchTransportTests(unittest.TestCase):
    def test_fixed_provider_auth_serialization_and_public_only_connection(self):
        for url, payload, header in [("https://api.search.brave.com/res/v1/web/search?q=soup", None, b"X-Subscription-Token: synthetic\r\n"),
                ("https://api.firecrawl.dev/v2/search", {"query": "soup"}, b"Authorization: Bearer synthetic\r\n")]:
            sent = []
            with patch.object(transport._PinnedConnection, "send", side_effect=sent.append), patch.object(transport._PinnedConnection, "getresponse", side_effect=OSError("synthetic stop")):
                with self.assertRaises(transport.RecipeImportSourceError):
                    transport._get_bytes(url, maximum=100, json_body=payload, search_api_key="synthetic")
            self.assertIn(header, b"".join(sent))
            with patch.object(transport, "_PinnedConnection") as connection:
                connection.return_value.getresponse.side_effect = OSError("stop")
                with self.assertRaises(transport.RecipeImportSourceError):
                    transport._get_bytes(url, maximum=100, json_body=payload, search_api_key="synthetic")
                self.assertTrue(connection.call_args.kwargs["public_only"])
                self.assertTrue(connection.call_args.kwargs["tls"])

    def test_credentials_never_reach_other_paths_origins_or_private_dns(self):
        for url in ["https://recipes.example/soup", "http://api.search.brave.com/res/v1/web/search",
                "https://api.search.brave.com:8443/res/v1/web/search", "https://api.search.brave.com/res/v1/news/search",
                "https://api.firecrawl.dev/v2/scrape", "https://api.search.brave.com.evil.org/res/v1/web/search"]:
            with self.subTest(url=url), patch.object(transport, "_PinnedConnection") as connection:
                with self.assertRaises(transport.RecipeImportSourceError):
                    transport._get_bytes(url, maximum=100, search_api_key="synthetic")
                connection.assert_not_called()
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(transport.socket, "getaddrinfo", return_value=addresses), patch.object(transport.socket, "socket") as create:
            with self.assertRaises(transport.RecipeImportSourceError):
                transport._get_bytes("https://api.search.brave.com/res/v1/web/search?q=soup", maximum=100, search_api_key="synthetic")
            create.assert_not_called()

    def test_authenticated_search_redirect_never_followed(self):
        with patch.object(transport, "_PinnedConnection") as connection:
            connection.return_value.getresponse.return_value.status = 302
            with self.assertRaisesRegex(transport.RecipeImportSourceError, "redirects"):
                transport._get_bytes("https://api.search.brave.com/res/v1/web/search?q=soup", maximum=100, search_api_key="synthetic")
            connection.assert_called_once()
            connection.return_value.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
