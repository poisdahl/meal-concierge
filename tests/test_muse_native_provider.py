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
    if tool == "get_orders":
        return {**common, "url": "https://oda.com/no/account/orders/", "page": arguments["page"],
                "size": arguments["size"], "hasMore": True,
                "orders": [{"url": "https://oda.com/no/account/orders/SYNTHETIC-A1/",
                            "reference": "SYNTHETIC-A1", "status": "Levert",
                            "delivery_text": "Monday 12 October", "total_text": None}]}
    if tool in {"get_order", "order_tracking"}:
        reference = arguments["order_number"]
        detail = {**common, "url": f"https://oda.com/no/account/orders/{reference}/",
                  "reference": reference, "delivery_text": "Synthetic delivery text"}
        if tool == "order_tracking":
            return {**detail, "tracking_status": None}
        return {**detail, "status": "Levert", "payment_status": "Betalt",
                "amount_rows": [{"label": "Total inkl. MVA", "value": "123,45kr"}],
                "goods_complete": False, "items": None}
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
        self.finite_producer = False
        self.defer_tool, self.deferred_record = None, None
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
                    if self.finite_producer:
                        producer = [sys.executable, "-I", "-B", str(ROOT / "muse_browser_producer.py")]
                        args = ["--directory", str(self.broker), "--request-id", record["request_id"],
                                "--task-id", record["task_id"]]
                        claimed = subprocess.run([*producer, "claim", *args], capture_output=True, timeout=5)
                        self.assertEqual(claimed.returncode, 0, claimed.stderr)
                        if tool == self.defer_tool:
                            self.deferred_record = (record, facts)
                            continue
                        published = subprocess.run([*producer, "respond", *args, "--observed-at",
                            datetime.now(timezone.utc).isoformat(), "--ending-state", self.state],
                            input=json.dumps(facts).encode(), capture_output=True, timeout=5)
                        self.assertEqual(published.returncode, 0, published.stderr)
                        continue
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

    def restart_with_short_reads(self, transport, *, read_seconds=1.0):
        self.process.terminate()
        self.process.communicate(timeout=5)
        socket = self.home / "service.sock"
        socket.unlink(missing_ok=True)
        runner = RUNNER.replace("sys.argv = sys.argv[1:]", """
sys.path.insert(0, str(__import__('pathlib').Path(sys.argv[1]).resolve().parents[1]))
import muse_native_provider
muse_native_provider.READ_SECONDS = READ_SECONDS_TEST_VALUE
sys.argv = sys.argv[1:]
""".replace("READ_SECONDS_TEST_VALUE", repr(read_seconds)))
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
                                  ("orders", "cancel_prepare")):
            self.rpc({"operation": operation, "action": action}, ok=False)
        self.rpc({"operation": "cart", "action": "get", "response_view": "agent"}, ok=False)
        self.assertEqual(self.seen, before)

    def test_order_history_flows_through_cli_with_partial_scope_and_no_mutation(self):
        store = StateStore(self.home / "state", muse.load_home(self.home))
        before = store.read()
        for transport in ("browser_readonly", "browser_cart"):
            with self.subTest(transport=transport):
                self.restart_with_short_reads(transport)
                start = len(self.seen)
                result = self.rpc({"operation": "orders", "action": "list", "limit": 1,
                                   "response_view": "agent"})
                self.assertEqual(self.seen[start:], ["get_orders"])
                self.assertEqual(result["history_scope"], {"kind": "rendered_prefix", "page": 1,
                                                          "size": 1, "hasMore": True})
                self.assertEqual(result["backend_freshness"], "unverified")
                row = result["orders"]["items"][0]
                self.assertEqual(row["normalized_order_id"], "SYNTHETIC-A1")
                self.assertEqual(row["observed_status"], "Levert")
                self.assertEqual(row["delivery_display"], "Monday 12 October")
                self.assertIsNone(row["sum_display"])
                self.assertEqual(row["payment_status"], "unknown")
                self.assertEqual(row["tracking_status"], "not_read")
                self.assertIsNone(row["cancelled"])
                self.assertIn("unavailable", result["next"])
                for action in ("change_begin", "cancel_prepare", "cancel_confirm", "remove_prepare"):
                    self.rpc({"operation": "orders", "action": action, "order_id": "SYNTHETIC-A1"}, ok=False)
                self.assertEqual(self.seen[start:], ["get_orders"])
                self.assertEqual(store.read(), before)
                self.assertEqual((self.home / "muse-client.json").read_bytes(), self.marker)
                for path, original in self.history.items():
                    self.assertEqual(path.read_bytes(), original)
        requests = [read_json(path) for path in (self.broker / "requests").glob("*.json")]
        listed = [r for r in requests if r["payload"].get("tool") == "get_orders"]
        self.assertEqual(len(listed), 2)
        for request in listed:
            self.assertIn("never the entire historical account", request["payload"]["facts_contract"])
            self.assertFalse((self.broker / "consumed" / (request["request_id"] + ".json")).exists())

    def test_exact_order_cli_uses_finite_producer_and_preserves_unknowns(self):
        self.finite_producer = True
        store = StateStore(self.home / "state", muse.load_home(self.home))
        before = store.read()
        for transport in ("browser_readonly", "browser_cart"):
            self.restart_with_short_reads(transport, read_seconds=10.0)
            for view in ("full", "agent"):
                start = len(self.seen)
                result = self.rpc({"operation": "orders", "action": "get", "order_id": "SYNTHETIC-A1",
                                   "response_view": view})
                self.assertEqual(self.seen[start:], ["get_order", "order_tracking"])
                self.assertEqual(result["backend_freshness"], "unverified")
                if view == "full":
                    self.assertEqual(result["order"]["orderNumber"], "SYNTHETIC-A1")
                    self.assertNotIn("products", result["order"])
                    self.assertIsNone(result["tracking"]["status"])
                    self.assertEqual(result["order"]["payment_status"], "Betalt")
                    self.assertNotIn("total", result["order"])
                else:
                    self.assertEqual(result["order"]["normalized_order_id"], "SYNTHETIC-A1")
                    self.assertFalse(result["order_items"]["available"])
                    self.assertIsNone(result["order_items"]["total"])
                    self.assertEqual(result["order"]["tracking_status"], "unknown")
                    self.assertIsNone(result["order"]["cancelled"])
                self.assertIn("unverified", result["next"])
                self.assertEqual(store.read(), before)
                self.assertEqual((self.home / "muse-client.json").read_bytes(), self.marker)
                for path, original in self.history.items():
                    self.assertEqual(path.read_bytes(), original)
            self.transform = lambda tool, facts: ({**facts, "goods_complete": True, "items": [{
                "url": None, "name": "Synthetic product 250 ml", "quantity": -2,
                "price": "-12,00kr", "labels": ["Refundert", "Erstatning"]}]}
                if tool == "get_order" else facts)
            goods = self.rpc({"operation": "orders", "action": "get", "order_id": "SYNTHETIC-A1",
                              "response_view": "agent"})["order_items"]
            self.assertTrue(goods["available"])
            self.assertEqual(goods["items"][0]["quantity"], -2)
            self.assertIsNone(goods["items"][0]["product_id"])
            self.assertEqual(goods["items"][0]["status_labels"], ["Refundert", "Erstatning"])
            self.transform = lambda tool, facts: facts

    def test_exact_order_second_read_changed_account_latches(self):
        self.transform = lambda tool, facts: ({**facts, "account": {**ACCOUNT,
            "edit_urls": ["https://oda.com/no/account/delivery/edit/102/"]}}
            if tool == "order_tracking" else facts)
        self.rpc({"operation": "orders", "action": "get", "order_id": "SYNTHETIC-A1"}, ok=False)
        count = len(self.seen)
        self.assertEqual(self.rpc({"operation": "status"})["integration"]["status"], "unavailable")
        self.rpc({"operation": "orders", "action": "get", "order_id": "SYNTHETIC-A1"}, ok=False)
        self.assertEqual(len(self.seen), count)

    def test_exact_order_holds_custody_across_both_reads(self):
        waiting, release = threading.Event(), threading.Event()
        def hold_tracking(tool, facts):
            if tool == "order_tracking":
                waiting.set()
                self.assertTrue(release.wait(5))
            return facts
        self.transform = hold_tracking
        command = subprocess.Popen([sys.executable, "-I", "-B", str(ROOT / "cli.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "MEAL_CONCIERGE_SOCKET": str(self.home / "service.sock")})
        try:
            command.stdin.write(json.dumps({"operation": "orders", "action": "get",
                                          "order_id": "SYNTHETIC-A1"}).encode())
            command.stdin.close()
            self.assertTrue(waiting.wait(5))
            seen = list(self.seen)
            self.rpc({"operation": "cart", "action": "get"}, ok=False)
            self.assertEqual(self.seen, seen)
            self.rpc({"operation": "health"})
        finally:
            release.set()
            command.wait(timeout=5)
            command.stdout.close()
            command.stderr.close()
        self.assertEqual(command.returncode, 0)

    def test_exact_order_second_timeout_requires_actual_ending_without_replay(self):
        self.finite_producer = True
        self.restart_with_short_reads("browser_readonly", read_seconds=3.0)
        self.defer_tool = "order_tracking"
        self.rpc({"operation": "orders", "action": "get", "order_id": "SYNTHETIC-A1"}, ok=False)
        self.assertIsNotNone(self.deferred_record)
        record, facts = self.deferred_record
        first = [path for path in (self.broker / "responses").glob("*.json")
                 if read_json(path)["facts"].get("reference") == "SYNTHETIC-A1"]
        self.assertEqual(len(first), 1)
        original = first[0].read_bytes()
        seen = list(self.seen)
        self.rpc({"operation": "cart", "action": "get"}, ok=False)
        self.assertEqual(self.seen, seen)
        self.assertFalse((self.broker / "responses" / (record["request_id"] + ".json")).exists())
        ending = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "muse_browser_producer.py"),
            "end", "--directory", str(self.broker), "--request-id", record["request_id"],
            "--task-id", record["task_id"], "--observed-at", datetime.now(timezone.utc).isoformat(),
            "--ending-state", "completed"], input=json.dumps(facts).encode(), capture_output=True, timeout=5)
        self.assertEqual(ending.returncode, 0, ending.stderr)
        self.assertEqual(first[0].read_bytes(), original)
        self.assertFalse((self.broker / "responses" / (record["request_id"] + ".json")).exists())
        self.rpc({"operation": "cart", "action": "get"})
        self.assertEqual(self.seen.count("get_order"), 1)
        self.assertEqual(self.seen.count("order_tracking"), 1)

    def test_order_history_account_change_latches_before_later_read(self):
        self.transform = lambda tool, facts: {**facts, "account": {
            **ACCOUNT, "edit_urls": ["https://oda.com/no/account/delivery/edit/102/"]}}
        self.rpc({"operation": "orders", "action": "list"}, ok=False)
        count = len(self.seen)
        self.assertEqual(self.rpc({"operation": "status"})["integration"]["status"], "unavailable")
        self.rpc({"operation": "orders", "action": "list"}, ok=False)
        self.assertEqual(len(self.seen), count)

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
    def test_exact_detail_identity_and_unavailable_or_complete_goods(self):
        arguments = {"order_number": "SYNTHETIC-A1"}
        facts = observation("get_order", arguments)
        normalized = MuseNativeReadProvider._order_detail(facts, arguments, tracking=False)
        self.assertNotIn("products", normalized)
        self.assertEqual(normalized["payment_status"], "Betalt")
        self.assertNotIn("total", normalized)
        for changes in ({"reference": "SYNTHETIC-OTHER"},
                        {"url": "https://oda.com/no/account/orders/SYNTHETIC-OTHER/"},
                        {"url": facts["url"] + "?edit=true"}, {"items": []},
                        {"goods_complete": 1}, {"payment_status": False}):
            with self.subTest(changes=changes), self.assertRaises(HouseholdError):
                MuseNativeReadProvider._order_detail({**facts, **changes}, arguments, tracking=False)
        complete = {**facts, "goods_complete": True, "items": [{"url": None,
            "name": "Synthetic product 250 ml", "quantity": -2, "price": "-12,00kr",
            "labels": ["Refundert"]}]}
        row = MuseNativeReadProvider._order_detail(complete, arguments, tracking=False)["products"][0]
        self.assertIsNone(row["id"])
        self.assertEqual(row["quantity"], -2)
        self.assertEqual(row["status_labels"], ["Refundert"])
        self.assertEqual(MuseNativeReadProvider._order_detail(
            {**facts, "goods_complete": True, "items": []}, arguments, tracking=False)["products"], [])
        tracking = observation("order_tracking", arguments)
        self.assertIsNone(MuseNativeReadProvider._order_detail(tracking, arguments, tracking=True)["status"])
        with self.assertRaises(HouseholdError):
            MuseNativeReadProvider._order_detail({**tracking, "reference": "OTHER"}, arguments, tracking=True)

    def test_order_history_identity_prefix_and_unknown_fields(self):
        arguments = {"page": 1, "size": 2}
        facts = observation("get_orders", arguments)
        result = MuseNativeReadProvider._orders(facts, arguments)
        self.assertEqual(result["orders"][0]["orderNumber"], "SYNTHETIC-A1")
        self.assertIsNone(result["orders"][0]["sum_display"])
        self.assertNotIn("total", result["orders"][0])
        self.assertNotIn("payment_status", result["orders"][0])
        self.assertNotIn("tracking", result["orders"][0])
        self.assertEqual(result["rendered_row_count"], 1)
        self.assertTrue(result["history_scope"]["hasMore"])
        for change in ({"page": True}, {"page": 2}, {"size": 1}, {"hasMore": None},
                       {"url": "https://oda.com/no/account/orders/?page=2"},
                       {"orders": []}, {"orders": facts["orders"] * 2}):
            with self.subTest(change=change), self.assertRaises(HouseholdError):
                MuseNativeReadProvider._orders({**facts, **change}, arguments)
        empty = MuseNativeReadProvider._orders({**facts, "orders": [], "hasMore": False}, arguments)
        self.assertEqual(empty["orders"], [])
        card = facts["orders"][0]
        for change in ({"reference": "SYNTHETIC-B2"}, {"url": card["url"] + "edit/"},
                       {"url": "https://evil.invalid/no/account/orders/SYNTHETIC-A1/"},
                       {"status": False}, {"delivery_text": []}, {"total_text": 39.90},
                       {"payment_status": "paid"}):
            with self.subTest(change=change), self.assertRaises(HouseholdError):
                MuseNativeReadProvider._orders({**facts, "orders": [{**card, **change}]}, arguments)

    def test_terminal_status_preserves_unavailable_without_reprobing(self):
        class Provider:
            terminal_failure = None
            initial_timeout = False
            fail_on_probe = False

            def probe(self):
                if self.terminal_failure:
                    raise AssertionError("terminal native provider must not be reprobed")
                if self.fail_on_probe:
                    self.terminal_failure = "Oda native browser login is required; preserve the existing profile."
                    raise HouseholdError(self.terminal_failure)
                if self.initial_timeout:
                    self.initial_timeout = False
                    raise HouseholdError("Synthetic native observation expired")
                return {}

        for discovered_by_status in (False, True):
            with self.subTest(discovered_by_status=discovered_by_status), tempfile.TemporaryDirectory(prefix="nt-") as root:
                home, ops = Path(root) / "h", Path(root) / "ops"
                ops.mkdir(mode=0o700)
                muse.initialize(home, "oda", "Synthetic household", credential_name="custom.synthetic",
                                operation_directory=ops)
                provider = Provider()
                provider.initial_timeout = discovered_by_status
                app = muse.NativeReadMuseApplication(StateStore(home / "state", muse.load_home(home)),
                                                     provider, None, external_recipe_sources={})
                if discovered_by_status:
                    provider.fail_on_probe = True
                else:
                    provider.terminal_failure = "Oda native browser login is required; preserve the existing profile."
                for _ in range(2):
                    status = app.handle({"operation": "status"})
                    self.assertEqual(status["integration"], app.integration)
                    self.assertEqual(status["integration"]["status"], "unavailable")

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
            def validate(tool, arguments, facts, result, receipt):
                entered.set()
                if not release.wait(3):
                    raise AssertionError("synthetic validation barrier timed out")
                return MuseNativeReadProvider.validate_read_facts(
                    tool, arguments, facts, result, receipt)
            provider.validate_read_facts = validate
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
