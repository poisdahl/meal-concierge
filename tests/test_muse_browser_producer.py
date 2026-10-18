"""Public producer CLI, literal JSON delivery and real one-use broker custody."""
from datetime import datetime, timezone, timedelta
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
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError
from muse_browser import NativeBridge, claim_request, read_json, native_authorization, durable_publish, digest, validate_response
from muse_browser_producer import ProducerError, parse_json, publish, read_task, validate_native_read
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
    def test_prepared_read_preserves_contract_account_and_deadline_without_observed_facts(self):
        record = {"request_id": "synthetic", "issued_at": "original-start",
                  "expires_at": "original-expiry",
                  "payload": {**READ_PAYLOAD, "accepted_account": READ_FACTS["account"]}}
        before = deepcopy(record)
        task = read_task(record)
        self.assertEqual(json.loads(task.split("\n", 1)[1]), record)
        self.assertEqual(record, before)
        for payload in ({**READ_PAYLOAD, "facts_contract": "Return a summary"},
                        {**READ_PAYLOAD, "tool": "checkout_delegate"},
                        {**READ_PAYLOAD, "accepted_account": {"url": "https://oda.com/no/", "edit_urls": []}}):
            with self.subTest(payload=payload), self.assertRaises(HouseholdError):
                read_task({**record, "payload": payload})

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

    def test_positive_native_read_requires_complete_accepted_account_set(self):
        accepted = deepcopy(FACTS["account"])
        accepted["edit_urls"].append("https://oda.com/no/account/delivery/edit/2/")
        record = {"operation": "provider_read", "payload": {**READ_PAYLOAD, "accepted_account": accepted}}
        for links in ([accepted["edit_urls"][0]], accepted["edit_urls"] +
                      ["https://oda.com/no/account/delivery/edit/3/"]):
            facts = {**READ_FACTS, "account": {**accepted, "edit_urls": links}}
            with self.subTest(links=links), self.assertRaises(HouseholdError):
                validate_native_read(record, {"facts": facts})
        facts = {**READ_FACTS, "account": {**accepted, "edit_urls": list(reversed(accepted["edit_urls"]))}}
        validate_native_read(record, {"facts": facts})
        self.assertEqual(record["payload"]["accepted_account"], accepted)


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
        before = set((self.broker / "requests").glob("*.json"))
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
            paths = [path for path in (self.broker / "requests").glob("*.json") if path not in before]
            if paths:
                self.key = paths[0].stem
                self.marker = self.broker / "publications" / (self.key + ".json")
                return
            time.sleep(0.01)
        self.fail("real source waiter did not issue a request")

    def run_cli(self, command, data=b"", *, request_id=None, extra=(), observed_at=None):
        args = [sys.executable, "-I", "-B", str(PRODUCER), command,
                "--directory", str(self.broker), "--request-id", request_id or self.key,
                "--task-id", self.bridge.task_id]
        if command in {"respond", "end"}:
            args += ["--observed-at", observed_at or datetime.now(timezone.utc).isoformat(), "--ending-state", "completed"]
        return subprocess.run(args + list(extra), input=data, capture_output=True, timeout=5)

    def assert_failure(self, result, stage, category):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr), {"error": {"stage": stage, "category": category}})

    def claim_read_cli(self, owner_pid=None):
        return subprocess.run([sys.executable, "-I", "-B", str(PRODUCER), "claim-read",
            "--directory", str(self.broker), "--task-id", self.bridge.task_id,
            "--owner-pid", str(os.getpid() if owner_pid is None else owner_pid)],
            input=b"", capture_output=True, timeout=5)

    def finish_read(self):
        if not (self.broker / "claims" / (self.key + ".json")).exists():
            self.assertEqual(self.run_cli("claim").returncode, 0)
        result = self.run_cli("respond", json.dumps(READ_FACTS).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.worker.join(2)
        self.assertFalse(self.worker.is_alive())

    def test_claim_read_selects_live_owner_and_delivers_whole_contract_to_real_waiter(self):
        payload = {**READ_PAYLOAD, "accepted_account": READ_FACTS["account"]}
        self.request("provider_read", payload)
        original = read_json(self.broker / "requests" / (self.key + ".json"))
        expired = {**original, "request_id": str(uuid.uuid4()),
                   "issued_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
                   "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()}
        durable_publish(self.broker / "requests" / (expired["request_id"] + ".json"), expired)
        claimed = self.claim_read_cli()
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        result = json.loads(claimed.stdout)
        self.assertEqual(result["request_id"], self.key)
        self.assertEqual(result["request_digest"], digest(original))
        self.assertEqual(result["expires_at"], original["expires_at"])
        task_record = json.loads(result["task"].split("\n", 1)[1])
        self.assertEqual(task_record, {key: original[key] for key in
                                    ("request_id", "issued_at", "expires_at", "payload")})
        self.assertIn("No prose", result["task"])
        self.assert_failure(self.claim_read_cli(), "request", "read_claim_rejected")
        self.assertFalse((self.broker / "claims" / (expired["request_id"] + ".json")).exists())
        self.finish_read()
        self.assertEqual(self.results, [READ_FACTS])
        self.assertEqual(self.errors, [])

    def test_claim_read_rejects_ambiguous_owner_start_and_incomplete_payload_without_claim(self):
        self.request("provider_read", READ_PAYLOAD)
        path = self.broker / "requests" / (self.key + ".json")
        original = read_json(path)
        duplicate = {**original, "request_id": str(uuid.uuid4())}
        other = self.broker / "requests" / (duplicate["request_id"] + ".json")
        durable_publish(other, duplicate)
        self.assert_failure(self.claim_read_cli(), "request", "read_claim_rejected")
        self.assertEqual(list((self.broker / "claims").iterdir()), [])
        other.unlink()
        self.assert_failure(self.claim_read_cli(owner_pid=0), "request", "read_claim_rejected")
        self.assert_failure(self.claim_read_cli(owner_pid=os.getpid() + 1000000), "request", "read_claim_rejected")
        for changed in ({**original, "owner_start": "0"},
                        {**original, "operation": "checkout_delegate"},
                        {**original, "task_id": "other-chain"},
                        {**original, "payload": {"tool": "get_cart", "arguments": {}}}):
            path.write_text(json.dumps(changed))
            self.assert_failure(self.claim_read_cli(), "request", "read_claim_rejected")
            self.assertEqual(list((self.broker / "claims").iterdir()), [])
        path.write_text(json.dumps(original))
        self.finish_read()

    def test_claim_read_does_not_skip_unresolved_prior_custody(self):
        self.request("provider_read", READ_PAYLOAD)
        original = read_json(self.broker / "requests" / (self.key + ".json"))
        old = {**original, "request_id": str(uuid.uuid4()),
               "issued_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
               "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()}
        request_path = self.broker / "requests" / (old["request_id"] + ".json")
        claim_path = self.broker / "claims" / request_path.name
        durable_publish(request_path, old)
        durable_publish(claim_path, {"request_digest": digest(old), "task_id": self.bridge.task_id})
        self.assert_failure(self.claim_read_cli(), "request", "read_claim_rejected")
        self.assertFalse((self.broker / "claims" / (self.key + ".json")).exists())
        claim_path.unlink()
        request_path.unlink()
        self.finish_read()

    def test_delegation_cli_requires_original_child_and_preserves_failed_publication_for_ending(self):
        payload = {"authorization": native_authorization(True),
                   "journal_binding": {"confirmation_id": "synthetic-original-confirmation",
                                       "journal_digest": "a" * 64}}
        self.request("checkout_delegate", payload)
        self.assertEqual(self.run_cli("claim").returncode, 0)
        self.assertEqual(self.run_cli("consume").returncode, 0)
        self.assertEqual(self.run_cli("bind", b'{"ok":true,"status":"accepted","browser_task_id":"synthetic-child:original"}').returncode, 0)
        observed = datetime.now(timezone.utc).isoformat()
        metadata = self.broker / "synthetic-completion.json"
        durable_publish(metadata, {"browser_task_id": "synthetic-child:wrong", "outcome": "completed",
                                   "completed_at_utc": observed})
        extra = ("--native-metadata", str(metadata))
        self.assert_failure(self.run_cli("respond", b'{"dispatch":"clicked_once"}',
                                        extra=extra, observed_at=observed), "request", "claim_rejected")
        self.assertTrue(self.marker.exists())
        original = self.broker / "synthetic-original-completion.json"
        durable_publish(original, {"browser_task_id": "synthetic-child:original", "outcome": "completed",
                                   "completed_at_utc": observed})
        self.assert_failure(self.run_cli("respond", b'{"dispatch":"clicked_once"}',
                          extra=("--native-metadata", str(original)), observed_at=observed),
                          "publish", "invocation_consumed")
        result = self.run_cli("end", b'{"dispatch":"clicked_once"}',
                             extra=("--native-metadata", str(original)), observed_at=observed)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Ending closes native custody; the original waiter still reaches its deadline.
        self.worker.join(WAITER_SECONDS + 2)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.errors), 1)
        ending = read_json(self.broker / "endings" / (self.key + ".json"))
        self.assertEqual(ending["native_completion"], read_json(original))

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

    def corrupt_read(self):
        self.request("provider_read", READ_PAYLOAD, seconds=0.5)
        record = read_json(self.broker / "requests" / (self.key + ".json"))
        child = "synthetic-browser-child"
        self.native_task = {"acceptance": {"ok": True, "status": "accepted", "browser_task_id": child},
                            "completion": {"browser_task_id": child, "outcome": "completed",
                                           "completed_at_utc": datetime.now(timezone.utc).isoformat()}}
        claim = self.broker / "claims" / (self.key + ".json")
        claim.write_text(json.dumps({"request_digest": digest(record), "task_id": child}))
        claim.chmod(0o660)
        response = self.broker / "responses" / (self.key + ".json")
        response.write_text(json.dumps({"manual_unusable_reply": READ_FACTS}))
        response.chmod(0o660)
        self.worker.join(2)
        self.assertFalse(self.worker.is_alive())
        self.assertEqual(self.results, [])
        while datetime.now(timezone.utc) < datetime.fromisoformat(record["expires_at"]):
            time.sleep(0.01)
        return record

    def close_corrupt(self, native_task=None):
        return self.run_cli("close-corrupt-read", json.dumps(native_task or self.native_task).encode())

    def test_corrupt_child_claim_closes_only_custody_and_preserves_every_original_byte(self):
        record = self.corrupt_read()
        originals = {kind: (self.broker / kind / (self.key + ".json")).read_bytes()
                     for kind in ("requests", "claims", "responses", "closed")}
        with self.assertRaises(HouseholdError):
            with self.bridge.custody():
                pass
        result = self.close_corrupt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"read_discarded_custody_closed": True})
        ending = read_json(self.broker / "endings" / (self.key + ".json"))
        self.assertNotIn("facts", ending)
        self.assertEqual(ending["native_task"], self.native_task)
        for kind, original in originals.items():
            self.assertEqual((self.broker / kind / (self.key + ".json")).read_bytes(), original)
        with self.assertRaises(HouseholdError):
            validate_response(record, ending, fresh=False)
        with self.bridge.custody():
            pass
        for operation in ("claim", "respond", "consume"):
            self.assertNotEqual(self.run_cli(operation, b"{}").returncode, 0)
        self.assertNotEqual(self.close_corrupt().returncode, 0)
        self.results.clear()
        self.errors.clear()
        self.request("provider_read", READ_PAYLOAD)
        self.assertEqual(self.run_cli("claim").returncode, 0)
        result = self.run_cli("respond", json.dumps(READ_FACTS).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.worker.join(2)
        self.assertEqual(self.results, [READ_FACTS])
        self.assertEqual(self.errors, [])

    def test_corrupt_read_closure_rejects_changed_claim_response_and_closed_records(self):
        self.corrupt_read()
        self.assertEqual(self.close_corrupt().returncode, 0)
        for kind in ("claims", "responses", "closed"):
            with self.subTest(kind=kind):
                path = self.broker / kind / (self.key + ".json")
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaises(HouseholdError):
                    with self.bridge.custody():
                        pass
                path.write_bytes(original)

    def test_corrupt_read_requires_terminal_matching_native_receipts(self):
        self.corrupt_read()
        for field, value in (("outcome", "waiting_for_information"),
                             ("browser_task_id", "synthetic-wrong-child"),
                             ("completed_at_utc", "2000-01-01T00:00:00Z")):
            native = deepcopy(self.native_task)
            native["completion"][field] = value
            with self.subTest(field=field):
                self.assert_failure(self.close_corrupt(native), "request", "corrupt_read_rejected")
        native = deepcopy(self.native_task)
        native["acceptance"]["status"] = "refused"
        self.assert_failure(self.close_corrupt(native), "request", "corrupt_read_rejected")
        self.assertFalse((self.broker / "endings" / (self.key + ".json")).exists())

    def test_corrupt_read_cannot_close_an_unclosed_or_unexpired_waiter(self):
        record = self.corrupt_read()
        closed = self.broker / "closed" / (self.key + ".json")
        original = closed.read_bytes()
        closed.unlink()
        self.assert_failure(self.close_corrupt(), "request", "corrupt_read_rejected")
        closed.write_bytes(original)
        closed.chmod(0o600)
        request = self.broker / "requests" / (self.key + ".json")
        record["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        request.write_text(json.dumps(record))
        self.assert_failure(self.close_corrupt(), "request", "corrupt_read_rejected")

    def test_corrupt_read_rejects_actions_consumed_bindings_and_valid_claims(self):
        record = self.corrupt_read()
        request = self.broker / "requests" / (self.key + ".json")
        original = request.read_bytes()
        for operation in ("cart_change", "delivery_select", "checkout_click", "cancellation_click",
                          "checkout_delegate", "cancellation_delegate", "checkout_review", "order_binding"):
            record["operation"] = operation
            request.write_text(json.dumps(record))
            with self.subTest(operation=operation):
                self.assert_failure(self.close_corrupt(), "request", "corrupt_read_rejected")
        request.write_bytes(original)
        for kind in ("consumed", "native-tasks"):
            root = self.broker / kind
            root.mkdir(mode=0o700, exist_ok=True)
            path = root / (self.key + ".json")
            durable_publish(path, {})
            self.assert_failure(self.close_corrupt(), "request", "corrupt_read_rejected")
            path.unlink()
        claim = self.broker / "claims" / (self.key + ".json")
        record = json.loads(original)
        claim.write_text(json.dumps({"request_digest": digest(record), "task_id": record["task_id"]}))
        claim.chmod(0o600)
        self.assert_failure(self.close_corrupt(), "request", "corrupt_read_rejected")
        end = self.run_cli("end", b'{"actual_ending":"synthetic completed read"}')
        self.assertEqual(end.returncode, 0, end.stderr)

    def test_another_unresolved_read_still_blocks_after_corrupt_read_closure(self):
        self.corrupt_read()
        self.assertEqual(self.close_corrupt().returncode, 0)
        # A real producer subprocess must start before the read expires.
        self.request("provider_read", READ_PAYLOAD, seconds=5)
        claimed = self.run_cli("claim")
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        self.worker.join(7)
        with self.assertRaises(HouseholdError):
            with self.bridge.custody():
                pass
        self.assertEqual(self.run_cli("end", b'{"actual_ending":"synthetic completed read"}').returncode, 0)
        with self.bridge.custody():
            pass

    def test_argument_and_byte_limits_precede_path_writes(self):
        self.request()
        for data in (b"", b"x" * 65537):
            self.assert_failure(self.run_cli("respond", data), "input", "bounded_closed_stdin_required")
            self.assertFalse(self.marker.exists())
        self.assert_failure(self.run_cli("respond", b"{}", request_id="../outside"), "input", "request_identity")
        self.assertFalse((self.broker / "publications").exists())


if __name__ == "__main__":
    unittest.main()
