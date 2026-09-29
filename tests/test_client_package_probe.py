"""Native probe decoding for the observed MCP JSON result envelopes."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client_package_probe import call_payload


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


if __name__ == "__main__":
    unittest.main()
