"""Real CLI/service/broker reads without MCP, preserving partial page facts."""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clients import muse
from core import HouseholdError, StateStore, cart_summary
from muse_browser import (claim_request, consume_request, digest, durable_publish, end_request,
                          process_start, read_json, respond_request)
from muse_native_provider import MuseNativeReadProvider, product_id
from test_muse_client import RUNNER, wait_for


ACCOUNT = {"url": "https://oda.com/no/account/delivery/",
           "edit_urls": ["https://oda.com/no/account/delivery/edit/101/"]}


def observation(tool, arguments):
    common = {"signed_in": True, "complete": True, "account": ACCOUNT}
    if tool == "get_delivery_addresses":
        return {**common, "url": ACCOUNT["url"], "rows": [{"edit_url": ACCOUNT["edit_urls"][0],
                "address": "Synthetic street 1, 0001 Synthetic city", "default": True, "selected": None}]}
    if tool == "get_cart":
        return {**common, "url": "https://oda.com/no/cart/", "empty": True, "items": [],
                "amount_rows": [], "delivery_text": "Monday 12 October", "address": None, "warnings": []}
    query = arguments["queries"][0]
    return {**common, "url": "https://oda.com/no/search/products/?q=" + quote_plus(query), "query": query,
            "page": 1, "size": arguments["size"], "hasMore": True,
            "products": [{"url": "https://oda.com/no/products/29829-synthetic-squash/", "name": "Synthetic squash",
                          "description": "250 g", "price": "39,90 kr", "unitPrice": "159,60 kr",
                          "unitName": "kg", "availability": True}]}


@unittest.skipUnless(sys.platform.startswith("linux"), "Muse cloud process identity is Linux")
class NativeReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nr-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ops = self.root / "ops"
        self.ops.mkdir(mode=0o700)
        self.broker = self.ops / "browser"
        self.broker.mkdir(mode=0o700)
        for name in ("requests", "claims", "consumed", "responses", "endings", "closed"):
            (self.broker / name).mkdir(mode=0o700)
        self.home = self.root / "h"
        muse.initialize(self.home, "oda", "Synthetic household", credential_name="custom.synthetic",
                        operation_directory=self.ops)
        self.marker = (self.home / "muse-client.json").read_bytes()
        now = datetime.now(timezone.utc)
        historical = {"version": 1, "request_id": str(uuid.uuid4()), "provider": "oda",
                      "operation": "checkout_click", "task_id": "synthetic-original-task",
                      "owner_pid": os.getpid(), "owner_start": process_start(os.getpid()),
                      "issued_at": now.isoformat(), "expires_at": (now + timedelta(seconds=30)).isoformat(),
                      "payload": {"synthetic_order": True}}
        name = historical["request_id"] + ".json"
        durable_publish(self.broker / "requests" / name, historical)
        claim_request(self.broker, historical["request_id"], historical["task_id"])
        consume_request(self.broker, historical["request_id"], historical["task_id"])
        response = {"request_id": historical["request_id"], "request_digest": digest(historical),
                    "task_id": historical["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
                    "task_state": "waiting_for_information", "facts": {}}
        respond_request(self.broker, historical["request_id"], historical["task_id"], response)
        end_request(self.broker, historical["request_id"], historical["task_id"],
                    {**response, "task_state": "completed", "observed_at": datetime.now(timezone.utc).isoformat(),
                     "facts": {"confirmed": False, "cancelled": True, "retry_allowed": False}})
        self.history = {path: path.read_bytes() for path in self.broker.glob("*/*.json")}
        self.transform = lambda tool, facts: facts
        self.state = "completed"
        self.omit_next = False
        self.seen, self.failures = [], []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.produce)
        self.thread.start()
        self.addCleanup(self.finish)
        self.process = subprocess.Popen([sys.executable, "-I", "-B", "-c", getattr(self, "runner", RUNNER),
            str(ROOT / "clients/muse.py"), "run", "--home", str(self.home),
            "--provider-transport", getattr(self, "transport", "browser_readonly"), "--browser-directory", str(self.broker),
            "--browser-task-id", "synthetic-original-task"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        wait_for(lambda: (self.home / "service.sock").exists())
        self.rpc({"operation": "health"})

    def finish(self):
        self.stop.set()
        self.thread.join(3)
        if hasattr(self, "process"):
            self.process.terminate()
            self.process.communicate(timeout=5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.failures, [])

    def produce(self):
        served = {path.name for path in self.history if path.parent.name == "requests"}
        while not self.stop.wait(0.01):
            for path in (self.broker / "requests").glob("*.json"):
                if path.name in served:
                    continue
                served.add(path.name)
                try:
                    record = read_json(path)
                    self.assertEqual(record["operation"], "provider_read")
                    tool = record["payload"]["tool"]
                    self.seen.append(tool)
                    if self.omit_next:
                        self.omit_next = False
                        continue
                    facts = self.transform(tool, observation(tool, record["payload"]["arguments"]))
                    claim_request(self.broker, record["request_id"], record["task_id"])
                    response = {"request_id": record["request_id"], "request_digest": digest(record),
                                "task_id": record["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
                                "task_state": self.state, "facts": facts}
                    respond_request(self.broker, record["request_id"], record["task_id"], response)
                except Exception as error:
                    self.failures.append(repr(error))

    def rpc(self, request, *, ok=True):
        result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "cli.py")],
            input=json.dumps(request), text=True, capture_output=True, timeout=10,
            env={**os.environ, "MEAL_CONCIERGE_SOCKET": str(self.home / "service.sock")})
        self.assertEqual(result.returncode, 0 if ok else 1, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["ok"], ok)
        return value.get("result", value)

    def restart_with_short_reads(self, transport):
        self.process.terminate()
        self.process.communicate(timeout=5)
        socket = self.home / "service.sock"
        socket.unlink(missing_ok=True)
        runner = RUNNER.replace("sys.argv = sys.argv[1:]", """
sys.path.insert(0, str(__import__('pathlib').Path(sys.argv[1]).resolve().parents[1]))
import muse_native_provider
muse_native_provider.READ_SECONDS = 1.0
sys.argv = sys.argv[1:]
""")
        self.process = subprocess.Popen([sys.executable, "-I", "-B", "-c", runner,
            str(ROOT / "clients/muse.py"), "run", "--home", str(self.home),
            "--provider-transport", transport, "--browser-directory", str(self.broker),
            "--browser-task-id", "synthetic-original-task"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        wait_for(lambda: socket.exists())

    def test_expired_unclaimed_startup_read_recovers_on_explicit_status(self):
        for transport in ("browser_readonly", "browser_cart"):
            with self.subTest(transport=transport):
                self.omit_next = True
                self.restart_with_short_reads(transport)
                health = self.rpc({"operation": "health"})
                self.assertEqual(health["integration"]["status"], "unavailable")
                records = [read_json(path) for path in (self.broker / "requests").glob("*.json")]
                expired = next(record for record in records if record["owner_pid"] == self.process.pid)
                name = expired["request_id"] + ".json"
                originals = {self.broker / area / name: (self.broker / area / name).read_bytes()
                             for area in ("requests", "closed")}
                for area in ("claims", "consumed", "responses", "endings", "publications"):
                    self.assertFalse((self.broker / area / name).exists())
                store = StateStore(self.home / "state", muse.load_home(self.home))
                before, count = store.read(), len(self.seen)
                status = self.rpc({"operation": "status"})
                self.assertEqual(status["integration"]["status"], "ready")
                self.assertEqual(self.seen[count:], ["get_delivery_addresses"])
                self.rpc({"operation": "status"})
                self.assertEqual(len(self.seen), count + 1)
                self.assertEqual(store.read(), before)
                self.assertEqual((self.home / "muse-client.json").read_bytes(), self.marker)
                for path, original in originals.items():
                    self.assertEqual(path.read_bytes(), original)

    def test_unresolved_startup_read_blocks_status_before_new_publication(self):
        self.state = "waiting_for_information"
        self.restart_with_short_reads("browser_cart")
        self.assertEqual(self.rpc({"operation": "health"})["integration"]["status"], "unavailable")
        before = set((self.broker / "requests").glob("*.json"))
        self.state = "completed"
        self.assertEqual(self.rpc({"operation": "status"})["integration"]["status"], "unavailable")
        self.assertEqual(set((self.broker / "requests").glob("*.json")), before)

    def test_real_reads_preserve_unknowns_original_home_and_saved_plan(self):
        settings = muse.load_home(self.home)
        store = StateStore(self.home / "state", settings)
        with store.locked() as value:
            value["cart_plan"] = {"provider": "oda", "status": "ordered", "menu_ref": {},
                                  "product_names": {"29829": "Synthetic squash"}, "baseline_quantities": {},
                                  "required_quantities": {"29829": 1}, "added_quantities": {"29829": 1},
                                  "last_synced_quantities": {"29829": 1}, "start_as_extra_product_ids": [],
                                  "last_synced_digest": "a" * 64, "approved_cart_digest": None,
                                  "pending_cart_digest": None}
        before = store.read()
        cart = self.rpc({"operation": "cart", "action": "get"})
        self.assertEqual(cart["items"], [])
        self.assertEqual(cart["count"], 0)
        self.assertIsNone(cart["total"])
        self.assertIsNone(cart["delivery"]["slot_id"])
        self.assertNotIn("cart_digest", cart)
        self.assertNotIn("meal_concierge_cart_plan", cart)
        addresses = self.rpc({"operation": "delivery", "action": "addresses"})
        self.assertIsNone(addresses["result"][0]["isSelected"])
        products = self.rpc({"operation": "catalog", "action": "products", "query": "squash", "limit": 1})
        self.assertEqual(products["query"], "squash")
        self.assertEqual(products["observation_scope"], "browser_read_only")
        product = products["products"][0]
        self.assertEqual(product["purchase_options"][0]["merchandise_ore"], 3990)
        self.assertEqual(product["display"]["price"], "39,90 kr")
        self.assertEqual(product["display"]["unit_price"], "159,60 kr")
        status = self.rpc({"operation": "status"})
        self.assertEqual(status["store_readiness"]["provider_transport"], "browser_readonly")
        self.assertEqual(store.read(), before)
        self.assertEqual((self.home / "muse-client.json").read_bytes(), self.marker)
        for path, original in self.history.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(list((self.broker / "closed").glob("*.json"))), 4)

    def test_writes_and_lossy_projection_refused_before_dispatch(self):
        before = list(self.seen)
        for operation, action in (("cart", "change"), ("cart", "clear"), ("products", "apply"),
                                  ("delivery", "select"), ("checkout", "prepare"),
                                  ("orders", "cancel_prepare"), ("orders", "list")):
            self.rpc({"operation": operation, "action": action}, ok=False)
        self.rpc({"operation": "cart", "action": "get", "response_view": "agent"}, ok=False)
        self.assertEqual(self.seen, before)

    def test_signed_out_latches_without_retry_or_status_reprobe(self):
        self.transform = lambda tool, facts: {**facts, "signed_in": False}
        self.rpc({"operation": "cart", "action": "get"}, ok=False)
        count = len(self.seen)
        status = self.rpc({"operation": "status"})
        self.assertEqual(status["integration"]["status"], "unavailable")
        self.rpc({"operation": "catalog", "action": "products", "query": "squash"}, ok=False)
        self.assertEqual(len(self.seen), count)

    def test_changed_account_latches_without_status_reprobe(self):
        self.transform = lambda tool, facts: {**facts, "account": {
            **ACCOUNT, "edit_urls": ["https://oda.com/no/account/delivery/edit/102/"]}}
        self.rpc({"operation": "cart", "action": "get"}, ok=False)
        count = len(self.seen)
        self.assertEqual(self.rpc({"operation": "status"})["integration"]["status"], "unavailable")
        self.assertEqual(len(self.seen), count)

    def test_unknown_completeness_or_paused_task_never_becomes_cart_read(self):
        self.transform = lambda tool, facts: {**facts, "complete": None}
        self.rpc({"operation": "cart", "action": "get"}, ok=False)
        self.transform = lambda tool, facts: facts
        self.state = "waiting_for_information"
        self.rpc({"operation": "cart", "action": "get"}, ok=False)
        count = len(self.seen)
        self.rpc({"operation": "cart", "action": "get"}, ok=False)
        self.assertEqual(len(self.seen), count)

    def test_shared_provider_lock_blocks_before_native_request(self):
        descriptor = os.open(self.ops / ".oda-household.lock", os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = list(self.seen)
            self.rpc({"operation": "cart", "action": "get"}, ok=False)
            self.assertEqual(self.seen, before)
        finally:
            os.close(descriptor)

    def test_emitted_cart_contract_does_not_accept_display_account_or_flat_amount_rows(self):
        for changed in ({"account": "Synthetic account"},
                        {"amount_rows": ["Total inkl. mva 39,90 kr"]}):
            with self.subTest(changed=changed):
                self.transform = lambda tool, facts: {**facts, **changed}
                self.rpc({"operation": "cart", "action": "get"}, ok=False)
        requests = [read_json(path) for path in (self.broker / "requests").glob("*.json")]
        cart_requests = [record for record in requests if record["payload"].get("tool") == "get_cart"]
        self.assertEqual(len(cart_requests), 2)
        for record in cart_requests:
            contract = record["payload"]["facts_contract"]
            self.assertIn("account is an object with exactly url and edit_urls", contract)
            self.assertIn("amount_rows is an array of objects with exactly label,value", contract)
            self.assertIn("literal subtitle string (empty only for verified absence)", contract)
            self.assertIn("If subtitle is unknown, stop", contract)
            self.assertIn("never cached or invented values", contract)


class PageBoundaryTests(unittest.TestCase):
    def test_catalog_normalizes_observed_price_without_losing_literal_display(self):
        arguments = {"queries": ["squash"], "page": 1, "size": 1}
        now = datetime.now(timezone.utc)
        response = {"request_id": "synthetic-request", "observed_at": now.isoformat()}
        receipt = {"issued_at": (now - timedelta(seconds=1)).isoformat(),
                   "expires_at": (now + timedelta(seconds=30)).isoformat()}
        facts = observation("product_search", arguments)
        result = MuseNativeReadProvider._catalog(facts, arguments, response, receipt)
        self.assertEqual(result["products"][0]["purchase_options"][0]["merchandise_ore"], 3990)
        self.assertEqual(result["products"][0]["display"]["price"], "39,90 kr")
        facts["products"][0]["price"] = "Fra 39,90 kr"
        result = MuseNativeReadProvider._catalog(facts, arguments, response, receipt)
        self.assertEqual(result["products"][0]["purchase_options"][0]["price_kind"], "unavailable")
        self.assertEqual(result["products"][0]["display"]["price"], "Fra 39,90 kr")

    def test_product_id_digit_bound_is_checked_before_integer_conversion(self):
        with self.assertRaises(HouseholdError):
            product_id("https://oda.com/no/products/" + "9" * 5000 + "-synthetic/")

    def test_catalog_search_redirect_preserves_url_and_query_boundary(self):
        arguments = {"queries": ["gul squash"], "page": 1, "size": 1}
        now = datetime.now(timezone.utc)
        response = {"request_id": "synthetic-request", "observed_at": now.isoformat()}
        receipt = {"issued_at": (now - timedelta(seconds=1)).isoformat(),
                   "expires_at": (now + timedelta(seconds=30)).isoformat()}
        facts = observation("product_search", arguments)
        for path in ("/no/search/", "/no/search/products/"):
            with self.subTest(path=path):
                facts["url"] = "https://oda.com" + path + "?q=gul%20squash"
                result = MuseNativeReadProvider._catalog(facts, arguments, response, receipt)
                self.assertEqual(result["source"]["url"], facts["url"])
                self.assertEqual(result["products"][0]["product_id"], 29829)
        for url in ("https://oda.com/no/search/products/?q=other",
                    "https://oda.com/no/search/recipes/?q=gul%20squash"):
            with self.subTest(url=url):
                facts["url"] = url
                with self.assertRaisesRegex(HouseholdError, "search page changed"):
                    MuseNativeReadProvider._catalog(facts, arguments, response, receipt)

    def test_deduplicated_catalog_keeps_display_bound_to_its_product_id(self):
        arguments = {"queries": ["squash"], "page": 1, "size": 3}
        now = datetime.now(timezone.utc)
        response = {"request_id": "synthetic-request", "observed_at": now.isoformat()}
        receipt = {"issued_at": (now - timedelta(seconds=1)).isoformat(),
                   "expires_at": (now + timedelta(seconds=30)).isoformat()}
        facts = observation("product_search", arguments)
        first = facts["products"][0]
        facts["products"] = [first, dict(first), {**first,
            "url": "https://oda.com/no/products/9287-synthetic-other/", "price": "27,60 kr"}]
        result = MuseNativeReadProvider._catalog(facts, arguments, response, receipt)
        self.assertEqual([(p["product_id"], p["display"]["price"],
                           p["purchase_options"][0]["merchandise_ore"]) for p in result["products"]],
                         [(29829, "39,90 kr", 3990), (9287, "27,60 kr", 2760)])

    def test_observed_total_preserves_literal_row(self):
        facts = observation("get_cart", {})
        facts["amount_rows"] = [{"label": "Total inkl. mva", "value": "39,90kr"}]
        result = MuseNativeReadProvider._cart(facts)
        self.assertEqual(result["total"], 39.90)
        self.assertEqual(result["amount_rows"], facts["amount_rows"])

    def test_shared_cart_summary_preserves_observed_or_unknown_address(self):
        for address in ("Eksempelveien 1", None):
            with self.subTest(address=address):
                facts = observation("get_cart", {})
                facts["address"] = address
                facts["empty"] = False
                facts["items"] = [{"url": "https://oda.com/no/products/29829-synthetic-squash/",
                                   "title": "Synthetic squash", "subtitle": "250 g",
                                   "quantity": 1, "price": "39,90 kr"}]
                facts["amount_rows"] = [{"label": "Total inkl. mva", "value": "39,90kr"}]
                result = MuseNativeReadProvider._cart(facts)
                summary = cart_summary(result)
                self.assertEqual(summary["delivery"]["address"], address)
                self.assertEqual(summary["delivery"]["address"], result["delivery"]["address"])
                self.assertIsNone(summary["delivery"]["slot_id"])
                self.assertEqual(summary["total"], 39.90)
                self.assertNotIn("amounts", summary)

    def test_cart_subtitle_requires_literal_string_including_verified_empty(self):
        facts = observation("get_cart", {})
        facts["empty"] = False
        facts["items"] = [{"url": "https://oda.com/no/products/29829-synthetic-squash/",
                           "title": "Synthetic squash", "subtitle": "", "quantity": 1, "price": None}]
        self.assertEqual(MuseNativeReadProvider._cart(facts)["items"][0]["description"], "")
        facts["items"][0]["subtitle"] = None
        with self.assertRaises(HouseholdError):
            MuseNativeReadProvider._cart(facts)

    def test_validation_keeps_provider_and_browser_locks_until_account_is_pinned(self):
        with tempfile.TemporaryDirectory() as temporary:
            ops = Path(temporary)
            (ops / "browser").mkdir(mode=0o700)
            for name in ("requests", "claims", "consumed", "responses", "endings", "closed"):
                (ops / "browser" / name).mkdir(mode=0o700)
            provider = MuseNativeReadProvider(ops, "synthetic-original-task")
            entered, release = threading.Event(), threading.Event()
            seen, failures = [], []
            def response(*args, **kwargs):
                seen.append(args[1]["tool"])
                return {"response": {"task_state": "completed", "request_id": "synthetic-request",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "facts": observation("get_cart", {})}}
            provider.bridge.request = response
            def validate(facts):
                entered.set()
                if not release.wait(3):
                    raise AssertionError("synthetic validation barrier timed out")
                return MuseNativeReadProvider._cart(facts)
            provider._cart = validate
            def first():
                try:
                    provider.call("get_cart", {})
                except Exception as error:
                    failures.append(error)
            thread = threading.Thread(target=first)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertIsNone(provider._account_ids)
                with self.assertRaisesRegex(HouseholdError, "another Oda operation"):
                    provider.call("get_cart", {})
                descriptor = os.open(ops / "browser" / ".owner.lock", os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
                self.assertEqual(seen, ["get_cart"])
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(provider._account_ids, {101})

    def test_empty_cart_keeps_unobserved_amounts_and_delivery_identity_unknown(self):
        result = MuseNativeReadProvider._cart(observation("get_cart", {}))
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["total"])
        self.assertIsNone(result["delivery"]["slot_id"])
        self.assertIsNone(result["delivery"]["address"])
        self.assertNotIn("cart_digest", result)

    def test_operation_lease_spans_nested_calls_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            ops = Path(temporary)
            (ops / "browser").mkdir(mode=0o700)
            for name in ("requests", "claims", "consumed", "responses", "endings", "closed"):
                (ops / "browser" / name).mkdir(mode=0o700)
            provider = MuseNativeReadProvider(ops, "synthetic-original-task")
            provider.bridge.request = lambda *_args, **_kwargs: {"response": {
                "task_state": "completed", "request_id": "synthetic-request",
                "observed_at": datetime.now(timezone.utc).isoformat(), "facts": observation("get_cart", {})}}
            other = MuseNativeReadProvider(ops, "synthetic-other-task")
            with self.assertRaisesRegex(RuntimeError, "synthetic finalization failure"):
                with provider.operation():
                    provider.call("get_cart", {})
                    with provider.operation():
                        provider.call("get_cart", {})
                    with self.assertRaisesRegex(HouseholdError, "another Oda operation"):
                        other.call("get_cart", {})
                    raise RuntimeError("synthetic finalization failure")
            with other.operation():
                pass

    def test_default_address_is_not_invented_selected_address(self):
        result = MuseNativeReadProvider._addresses(observation("get_delivery_addresses", {}))
        self.assertTrue(result["result"][0]["isDefault"])
        self.assertIsNone(result["result"][0]["isSelected"])
        facts = observation("get_delivery_addresses", {})
        facts["rows"][0]["edit_url"] = "https://oda.com/no/account/delivery/edit/102/"
        with self.assertRaisesRegex(HouseholdError, "disagree"):
            MuseNativeReadProvider._addresses(facts)

    def test_count_zero_without_explicit_empty_state_is_refused(self):
        facts = observation("get_cart", {})
        facts["empty"] = False
        with self.assertRaisesRegex(HouseholdError, "explicit empty"):
            MuseNativeReadProvider._cart(facts)


if __name__ == "__main__":
    unittest.main()
