"""Native probe decoding for the observed MCP JSON result envelopes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client_package_probe import (CART_TITLE, HERE, INSPECT_FILE_LIMIT, INSPECT_OUTPUT_LIMIT,
                                  call_payload, inspect_fixture, prepare, service)


class NativeCallPayloadTests(unittest.TestCase):
    def call(self, payload, **result):
        return {"status": "completed", "error": None, "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "structured_content": None, **result}}

    def test_captured_profile_and_cart_text_shapes(self):
        # Sanitized shapes from the native reconnect and lost-response probes.
        profile = {"profile": {"meals": {"portions": 3}}}
        reconciled = {"projection": "agent", "operation": "cart",
                      "action": "reconcile_change", "reconciled": True,
                      "cart_write_pending": False}
        cart = {"projection": "agent", "operation": "cart", "action": "get",
                "lines": {"items": [{"product_id": "10", "name": "Synthetic eggs",
                                     "quantity": 1, "price": 29.9}],
                          "total": 1, "offset": 0}}
        for payload in (profile, reconciled, cart):
            with self.subTest(payload=payload):
                self.assertEqual(call_payload(self.call(payload)), payload)

    def test_structured_only_and_matching_text(self):
        payload = {"confirmed": False}
        self.assertEqual(call_payload(self.call(payload, structured_content=payload)), payload)
        self.assertEqual(call_payload(self.call(payload, content=[], structured_content=payload)), payload)

    def test_rejects_failed_calls_and_tool_errors(self):
        for changes in ({"status": "failed"}, {"error": "requires approval"}):
            with self.subTest(changes=changes), self.assertRaises(AssertionError):
                call_payload({**self.call({"profile": {}}), **changes})
        with self.assertRaises(AssertionError):
            call_payload(self.call({"profile": {}}, is_error=True))

    def test_rejects_malformed_or_non_object_json(self):
        for text in ("not JSON", "[]", "null", "3"):
            with self.subTest(text=text), self.assertRaises((AssertionError, ValueError)):
                call_payload(self.call({}, content=[{"type": "text", "text": text}]))

    def test_rejects_missing_or_ambiguous_content(self):
        for content in ([], [{"type": "image", "data": "synthetic"}],
                        [{"type": "text", "text": "{}"}] * 2):
            with self.subTest(content=content), self.assertRaises(AssertionError):
                call_payload(self.call({}, content=content))
        with self.assertRaises(AssertionError):
            call_payload(self.call({"confirmed": False}, structured_content={"confirmed": True}))
        with self.assertRaises(AssertionError):
            call_payload(self.call({}, structured_content=[]))


class CartRecoveryFixtureTests(unittest.TestCase):
    def setUp(self):
        # Keep AF_UNIX paths below the macOS socket-path limit.
        self.temp = tempfile.TemporaryDirectory(prefix="mc-cart-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "fixture"
        self.calls = []

    def cli(self, action, *, operations=None, expected=0):
        request = {"operation": "cart", "action": action}
        if operations is not None:
            request["operations"] = operations
        raw = json.dumps(request)
        result = subprocess.run(
            [sys.executable, "-S", "-P", str(self.root / "code/current/cli.py")],
            input=raw, text=True, capture_output=True, timeout=15,
            env={"PATH": os.defpath, "MEAL_CONCIERGE_SOCKET": str(self.root / "service.sock")})
        self.calls.append({"stdin": raw, "stdout": result.stdout, "exit": result.returncode})
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def inspect(self):
        # -S proves this mode needs no MCP/site-packages or service connection.
        result = subprocess.run([sys.executable, "-S", "-P", str(HERE), "--inspect", str(self.root)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertLessEqual(len(result.stdout), INSPECT_OUTPUT_LIMIT)
        return json.loads(result.stdout)

    def test_cli_recovers_one_effect_after_controlled_restart(self):
        prepared = subprocess.run([sys.executable, "-I", "-B", str(HERE), "--prepare", str(self.root),
                                   "--case", "cart-recovery"], capture_output=True, timeout=30)
        self.assertEqual(prepared.returncode, 0, prepared.stderr.decode())
        initial = self.inspect()
        self.assertFalse(initial["state_present"])
        self.assertFalse((self.root / "state").exists())
        self.assertEqual(initial["fixture"]["effect_count"], 0)
        self.assertEqual(initial["starts"], [])
        with service(self.root) as first:
            cart = self.cli("get")["result"]
            self.assertEqual(cart["items"][0]["name"], CART_TITLE)
            self.assertEqual(cart["items"][0]["quantity"], 1)
            self.assertNotIn("_fixture", cart)
            failed = self.cli("change", operations=[{"product_id": "10", "quantity": 1}], expected=1)
            self.assertEqual(failed, {"ok": False, "error": "Provider reply unavailable; cart change outcome is unknown."})
            for action in ("get", "reconcile_change"):
                self.assertIn("temporarily unavailable", self.cli(action, expected=1)["error"])
            pending = self.inspect()
            self.assertEqual(pending["starts"][0]["pid"], first.pid)
            self.assertEqual(pending["pending_cart_change"], {
                "provider": "mathem", "before": {"10": 1}, "expected": {"10": 2},
                "order_change": None, "operations": [{"productId": 10, "quantity": 1}]})
            self.assertEqual([row["event"] for row in pending["dispatches"]],
                             ["attempt", "pre_effect", "effect_applied"])
            self.assertEqual(pending["dispatches"][1]["pending"], pending["pending_cart_change"])
            self.assertEqual(pending["dispatches"][1]["state_sha256"], pending["files"]["state/state.json"]["sha256"])
            self.assertEqual(pending["counts"]["errors"], 3)
        self.assertIsNotNone(first.poll(), "original process must exit before restart")
        with service(self.root) as second:
            restarted = self.inspect()
            self.assertEqual([row["pid"] for row in restarted["starts"]], [first.pid, second.pid])
            self.assertEqual(len({row["instance"] for row in restarted["starts"]}), 2)
            for key in ("household", "provider", "fixture", "cart", "pending_cart_change", "dispatches"):
                self.assertEqual(restarted[key], pending[key], key)
            for name in ("config.json", "synthetic-cart.json", "dispatch.jsonl"):
                self.assertEqual(restarted["files"][name], pending["files"][name])
            before = self.cli("get")["result"]
            self.assertTrue(before["cart_write_pending"])
            self.assertEqual(before["items"][0]["quantity"], 2)
            reconciled = self.cli("reconcile_change")["result"]
            self.assertTrue(reconciled["reconciled"])
            self.assertFalse(reconciled["cart_write_pending"])
            final_cart = self.cli("get")["result"]
            self.assertNotIn("cart_write_pending", final_cart)
            self.assertEqual([(row["product_id"], row["quantity"]) for row in final_cart["items"]], [(10, 2)])
            final = self.inspect()
            self.assertIsNone(final["pending_cart_change"])
            for key in ("cart_mutation_requests", "provider_attempts", "effects"):
                self.assertEqual(final["counts"][key], 1)
            self.assertEqual(final["fixture"]["effect_count"], 1)
            mutations = [json.loads(call["stdin"]) for call in self.calls
                         if json.loads(call["stdin"])["action"] == "change"]
            self.assertEqual(mutations, [{"operation": "cart", "action": "change",
                                         "operations": [{"product_id": "10", "quantity": 1}]}])
            self.assertEqual(final["counts"]["requests"], final["counts"]["responses"])
        self.assertIsNotNone(second.poll())
        third = subprocess.run([sys.executable, "-I", str(HERE), "--serve", str(self.root)],
                               capture_output=True, timeout=15)
        self.assertNotEqual(third.returncode, 0)
        self.assertIn(b"only one service restart", third.stderr)
        self.assertEqual(len(self.inspect()["starts"]), 2)

    def test_inspection_counts_a_rejected_extra_mutation_as_an_attempt(self):
        prepare(self.root, case="cart-recovery")
        with service(self.root):
            self.cli("change", operations=[{"productId": 10, "quantity": 1}], expected=1)
            denied = self.cli("change", operations=[{"productId": 11, "quantity": 5}], expected=1)
            self.assertIn("reconcile_change", denied["error"])
            report = self.inspect()
            self.assertEqual(report["counts"]["cart_mutation_requests"], 2)
            self.assertEqual(report["counts"]["provider_attempts"], 1)
            self.assertEqual(report["counts"]["effects"], 1)
            self.assertEqual(report["counts"]["errors"], 2)
            self.assertTrue(any(row["request"].get("operations") == [{"productId": 11, "quantity": 5}]
                                for row in report["requests"]))

    def test_inspection_refuses_oversized_evidence_without_creating_state(self):
        prepare(self.root, case="cart-recovery")
        path = self.root / "application.jsonl"
        path.write_bytes(b" " * (INSPECT_FILE_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "file exceeds byte limit"):
            inspect_fixture(self.root)
        self.assertEqual(path.stat().st_size, INSPECT_FILE_LIMIT + 1)
        self.assertFalse((self.root / "state").exists())


if __name__ == "__main__":
    unittest.main()
