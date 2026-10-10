"""Public producer CLI, literal JSON delivery and real one-use broker custody."""
from datetime import datetime, timezone
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError
from muse_browser import NativeBridge, claim_request, read_json
from muse_browser_producer import ProducerError, parse_json, publish, validate_native_read
from muse_native_provider import FACTS_CONTRACTS


SOURCE = Path(__file__).resolve().parents[1]
PRODUCER = SOURCE / "muse_browser_producer.py"
# Allow several real subprocesses to start while the source waiter is active;
# cloud startup time must not replace the publication-custody assertions.
WAITER_SECONDS = 30
FACTS = {
    "url": "https://oda.com/no/checkout/confirm/",
    "account": {"url": "https://oda.com/no/account/delivery/",
                "edit_urls": ["https://oda.com/no/account/delivery/edit/1/"]},
    "address": "Synthetic test address",
    "delivery_sections": ["Man 4. januar, 10:00 - 12:00"],
    "items": [{"product_id": None, "title": f"Synthetic blåbær {i}",
               "subtitle": "SYNTHETIC TRANSPORT ONLY ÆØÅ 日本語 " + "x" * 180,
               "quantity": 1} for i in range(30)],
    "warnings": [],
    "amount_rows": [{"label": "30 varer", "value": "10,00kr"},
                    {"label": "Delsum", "value": "10,00kr"},
                    {"label": "Total inkl. MVA", "value": "10,00kr"}],
    "payment": {"display": "SYNTHETIC — not a payment card", "selected": True},
    "submit_controls": [{"label": "SYNTHETIC — no purchase", "enabled": True}],
    "complete_sections": ["account", "items", "warnings", "amounts", "delivery", "payment", "submit"],
}
READ_PAYLOAD = {"tool": "get_orders", "arguments": {"page": 1, "size": 10},
                "facts_contract": FACTS_CONTRACTS["get_orders"]}
READ_FACTS = {
    "url": "https://oda.com/no/account/orders/", "signed_in": True, "complete": True,
    "account": FACTS["account"], "page": 1, "size": 10, "hasMore": True,
    "orders": [{"url": "https://oda.com/no/account/orders/SYNTHETIC-A1/",
                "reference": "SYNTHETIC-A1", "status": "Levert — syntetisk",
                "delivery_text": None, "total_text": "10,00 kr"}],
}


class InputBoundaryTests(unittest.TestCase):
    def test_invalid_json_and_nonobjects_have_only_fixed_error_categories(self):
        for data in (b'{"private":"DO-NOT-PRINT","private":1}', b'{"value":1e999}',
                     b'{"value":NaN}', b'{"value":"\\ud800"}', b"\xff", b"[]"):
            with self.subTest(data=data), self.assertRaises(ProducerError) as error:
                parse_json(data)
            self.assertEqual(error.exception.stage, "input")
            self.assertIn(error.exception.category, {"invalid_json", "facts_object_required"})
            self.assertNotIn("DO-NOT-PRINT", str(error.exception))

    def test_native_read_rejects_common_evidence_and_order_schema_without_repair(self):
        malformed = []
        for account in ({**FACTS["account"], "url": READ_FACTS["url"]},
                        {**FACTS["account"], "edit_urls": []}):
            malformed.append({**READ_FACTS, "account": account})
        malformed.append({**READ_FACTS, "orders": [{
            "reference": "SYNTHETIC-A1", "detail_href": "/no/account/orders/SYNTHETIC-A1/",
            "status": "Levert", "delivery": None, "total": "10,00 kr"}]})
        for facts in malformed:
            with self.subTest(facts=facts):
                before = deepcopy(facts)
                with self.assertRaises(HouseholdError):
                    validate_native_read({"operation": "provider_read", "payload": READ_PAYLOAD},
                                         {"facts": facts})
                self.assertEqual(facts, before)

    def test_failure_observations_remain_literal_for_consumer_handling(self):
        for facts in ({"signed_in": False, "complete": True},
                      {"signed_in": True, "complete": False}):
            before = deepcopy(facts)
            validate_native_read({"operation": "provider_read", "payload": READ_PAYLOAD},
                                 {"facts": facts})
            self.assertEqual(facts, before)


@unittest.skipUnless(sys.platform.startswith("linux"), "Muse cloud owner identity is Linux")
class ProducerCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="muse-producer-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.broker = self.root / "broker"
        self.broker.mkdir(mode=0o700)
        for name in ("requests", "claims", "consumed", "responses", "endings", "closed"):
            (self.broker / name).mkdir(mode=0o700)
        self.bridge = NativeBridge(self.broker, "synthetic-native-chain")
        self.results, self.errors = [], []

    def request(self, operation="checkout_review", payload=None, seconds=WAITER_SECONDS):
        def waiter():
            try:
                with self.bridge.custody():
                    self.results.append(self.bridge.request(operation, payload or {},
                                                           deadline=time.monotonic() + seconds))
            except HouseholdError as error:
                self.errors.append(error)
        self.worker = threading.Thread(target=waiter)
        self.worker.start()
        self.addCleanup(self.worker.join, WAITER_SECONDS + 2)
        cutoff = time.monotonic() + 2
        while time.monotonic() < cutoff:
            paths = list((self.broker / "requests").glob("*.json"))
            if paths:
                self.key = paths[0].stem
                self.marker = self.broker / "publications" / (self.key + ".json")
                return
            time.sleep(0.01)
        self.fail("real source waiter did not issue a request")

    def run_cli(self, command, data=b"", *, request_id=None, extra=()):
        args = [sys.executable, "-I", "-B", str(PRODUCER), command,
                "--directory", str(self.broker), "--request-id", request_id or self.key,
                "--task-id", self.bridge.task_id]
        if command in {"respond", "end"}:
            args += ["--observed-at", datetime.now(timezone.utc).isoformat(), "--ending-state", "completed"]
        return subprocess.run(args + list(extra), input=data, capture_output=True, timeout=5)

    def assert_failure(self, result, stage, category):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr), {"error": {"stage": stage, "category": category}})

    def test_wrong_exact_order_is_rejected_before_response_publication(self):
        payload = {"tool": "get_order", "arguments": {"order_number": "SYNTHETIC-A1"},
                   "facts_contract": FACTS_CONTRACTS["get_order"]}
        self.request("provider_read", payload)
        self.assertEqual(self.run_cli("claim").returncode, 0)
        facts = {"url": "https://oda.com/no/account/orders/SYNTHETIC-OTHER/",
            "signed_in": True, "complete": True, "account": FACTS["account"],
            "reference": "SYNTHETIC-OTHER", "status": "Levert", "delivery_text": None,
            "payment_status": None, "amount_rows": [], "goods_complete": False, "items": None}
        self.assert_failure(self.run_cli("respond", json.dumps(facts).encode()),
                            "publish", "invalid_native_read")
        self.assertTrue(self.marker.exists())
        self.assertEqual(list((self.broker / "responses").glob("*.json")), [])
        self.assert_failure(self.run_cli("respond", json.dumps(facts).encode()),
                            "publish", "invocation_consumed")
        self.assertEqual(self.run_cli("end", json.dumps(facts).encode()).returncode, 0)
        self.worker.join(WAITER_SECONDS + 2)
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.errors), 1)

    def test_real_cli_delivers_large_literal_unicode_payload_and_blocks_duplicate(self):
        self.request()
        self.assertEqual(self.run_cli("claim").returncode, 0)
        data = json.dumps(FACTS, ensure_ascii=False).encode()
        self.assertGreater(len(data), 8192)
        result = self.run_cli("respond", data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.worker.join(2)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(self.results, [FACTS])
        self.assertEqual(self.errors, [])
        original = (self.broker / "responses" / (self.key + ".json")).read_bytes()
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        self.assert_failure(self.run_cli("respond", data), "request", "custody_rejected")
        self.assertEqual((self.broker / "responses" / (self.key + ".json")).read_bytes(), original)
        self.assertEqual(len(list((self.broker / "responses").glob("*.json"))), 1)
        self.assertTrue((self.broker / "closed" / (self.key + ".json")).exists())

    def test_native_read_cli_preserves_valid_literal_facts(self):
        self.request("provider_read", READ_PAYLOAD)
        self.assertEqual(self.run_cli("claim").returncode, 0)
        result = self.run_cli("respond", json.dumps(READ_FACTS, ensure_ascii=False).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.worker.join(2)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(self.results, [READ_FACTS])
        self.assertEqual(self.errors, [])

    def test_signed_out_cli_preserves_failure_for_native_terminal_latch(self):
        self.request("provider_read", READ_PAYLOAD)
        self.assertEqual(self.run_cli("claim").returncode, 0)
        facts = {"signed_in": False, "complete": True}
        result = self.run_cli("respond", json.dumps(facts).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.worker.join(2)
        self.assertEqual(self.results, [facts])
        self.assertEqual(self.errors, [])

    def test_native_catalog_cli_uses_request_timestamps_and_preserves_display(self):
        payload = {"tool": "product_search", "arguments": {"queries": ["squash"], "page": 1, "size": 10},
                   "facts_contract": FACTS_CONTRACTS["product_search"]}
        facts = {"url": "https://oda.com/no/search/products/?q=squash", "signed_in": True,
                 "complete": True, "account": FACTS["account"], "query": "squash", "page": 1,
                 "size": 10, "hasMore": False, "products": [{
                     "url": "https://oda.com/no/products/29829-synthetic-squash/", "name": "Synthetic squash",
                     "description": "250 g", "price": "39,90 kr", "unitPrice": "159,60 kr",
                     "unitName": "kg", "availability": True}]}
        self.request("provider_read", payload)
        self.assertEqual(self.run_cli("claim").returncode, 0)
        result = self.run_cli("respond", json.dumps(facts).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.worker.join(2)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(self.results, [facts])
        self.assertEqual(self.errors, [])

    def test_invalid_native_read_consumes_attempt_and_generic_end_only_closes_custody(self):
        self.request("provider_read", READ_PAYLOAD, seconds=2)
        claim_request(self.broker, self.key, self.bridge.task_id)
        facts = {**READ_FACTS, "account": {**FACTS["account"], "edit_urls": []}}
        observed = datetime.now(timezone.utc).isoformat()
        with self.assertRaises(ProducerError) as error:
            publish(self.broker, self.key, self.bridge.task_id,
                    json.dumps(facts).encode(), observed, "completed")
        self.assertEqual((error.exception.stage, error.exception.category),
                         ("publish", "invalid_native_read"))
        self.assertTrue(self.marker.exists())
        self.assertEqual(list((self.broker / "responses").glob("*.json")), [])
        with self.assertRaises(ProducerError) as retry:
            publish(self.broker, self.key, self.bridge.task_id,
                    json.dumps(READ_FACTS).encode(), observed, "completed")
        self.assertEqual(retry.exception.category, "invocation_consumed")
        publish(self.broker, self.key, self.bridge.task_id,
                json.dumps(facts).encode(), observed, "completed", ending=True)
        self.worker.join(3)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(list((self.broker / "responses").glob("*.json")), [])
        self.assertTrue((self.broker / "endings" / (self.key + ".json")).exists())
        with self.bridge.custody():
            pass

    def test_refused_payload_cannot_be_corrected_or_moved_and_ending_is_custody_only(self):
        self.request()
        self.assertEqual(self.run_cli("claim").returncode, 0)
        self.assert_failure(self.run_cli("respond", b'{"private":"DO-NOT-PRINT","private":1}'),
                            "input", "invalid_json")
        self.assertTrue(self.marker.exists())
        self.assertEqual(list((self.broker / "responses").glob("*.json")), [])
        alternate = self.root / "alternate"
        alternate.mkdir(mode=0o700)
        self.assert_failure(self.run_cli("respond", b"{}", extra=("--out-root", str(alternate))),
                            "input", "arguments")
        self.assertEqual(list(alternate.iterdir()), [])
        self.assert_failure(self.run_cli("respond", b"{}"), "publish", "invocation_consumed")
        ending = self.run_cli("end", b'{"synthetic_ending":"incomplete native payload"}')
        self.assertEqual(ending.returncode, 0, ending.stderr)
        self.worker.join(WAITER_SECONDS + 2)
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.errors), 1)
        self.assertTrue((self.broker / "endings" / (self.key + ".json")).exists())
        self.assertEqual(list((self.broker / "responses").glob("*.json")), [])

    def test_stale_completion_is_not_restamped_and_consumes_publication(self):
        self.request()
        self.assertEqual(self.run_cli("claim").returncode, 0)
        args = [sys.executable, "-I", "-B", str(PRODUCER), "respond", "--directory", str(self.broker),
                "--request-id", self.key, "--task-id", self.bridge.task_id,
                "--observed-at", "2000-01-01T00:00:00+00:00", "--ending-state", "completed"]
        result = subprocess.run(args, input=b"{}", capture_output=True, timeout=5)
        self.assert_failure(result, "publish", "invalid_handoff")
        self.assertTrue(self.marker.exists())
        self.assertEqual(list((self.broker / "responses").glob("*.json")), [])
        self.assert_failure(self.run_cli("respond", b"{}"), "publish", "invocation_consumed")

    def test_actual_ending_prevents_later_response_to_still_live_waiter(self):
        self.request()
        self.assertEqual(self.run_cli("claim").returncode, 0)
        ending = self.run_cli("end", b'{"synthetic_ending":"browser refused incomplete observation"}')
        self.assertEqual(ending.returncode, 0, ending.stderr)
        self.assertTrue(self.worker.is_alive())
        self.assert_failure(self.run_cli("respond", json.dumps(FACTS).encode()),
                            "publish", "response_write_uncertain")
        self.worker.join(WAITER_SECONDS + 2)
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(list((self.broker / "responses").glob("*.json")), [])

    def test_argument_and_byte_limits_precede_path_writes(self):
        self.request()
        for data in (b"", b"x" * 65537):
            self.assert_failure(self.run_cli("respond", data), "input", "bounded_closed_stdin_required")
            self.assertFalse(self.marker.exists())
        self.assert_failure(self.run_cli("respond", b"{}", request_id="../outside"), "input", "request_identity")
        self.assertFalse((self.broker / "publications").exists())


if __name__ == "__main__":
    unittest.main()
