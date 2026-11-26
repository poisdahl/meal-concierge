from __future__ import annotations

import hashlib
from contextlib import nullcontext
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError, StateStore
import foreground
from service import Application


class ForegroundTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.calls = foreground.receipt_directory(self.state, create=True)
        self.build = {"source_commit": "a" * 40, "source_modified": False}

    def call(self, request, *, identifier=None, application=None):
        return foreground.call(self.calls, identifier or str(uuid.uuid4()), request, {},
                               application or self.application, self.build)

    def application(self):
        return Application(StateStore(self.state, {"household": "Dot fixture", "provider": "none"}), None, None)

    def result(self, request):
        response, code = self.call(request)
        self.assertEqual(code, 0, response)
        return response["result"]

    def test_no_store_recipe_menu_and_shopping_list_reopen(self):
        setup = self.result({"operation": "setup", "action": "apply", "keep_current": True})
        self.assertFalse(setup.get("configuration_required"))
        recipe = {"name": "Fixture oats", "portions": 2,
                  "ingredients": [{"raw": "100 g oats", "item": "oats", "quantity": 100,
                                   "unit": "g", "scalable": True}], "steps": ["Cook the oats."],
                  "source": {"kind": "user", "publisher": "Fixture", "relationship": "user_supplied"},
                  "rights": {"storage": "full", "credit": "Fixture"}}
        saved = self.result({"operation": "recipes", "action": "save", "recipe": recipe,
                             "idempotency_key": "foreground-fixture-recipe"})["recipe"]
        menu = {"week": "2026-W41", "dishes": [{"recipe_ref": {"id": saved["id"], "revision": saved["revision"]},
                                                "portions": 6}],
                "schedule": [{"day": "2026-10-06", "meal": recipe["name"], "portions": 6}]}
        request = {"operation": "menu", "action": "save", "menu": menu}
        identifier = str(uuid.uuid4())
        response, code = self.call(request, identifier=identifier)
        self.assertEqual(code, 0, response)

        reopened = self.result({"operation": "menu", "action": "get"})["menu"]
        self.assertEqual(reopened["dishes"][0]["ingredients"][0]["quantity"], 300)
        listed = self.result({"operation": "shopping_list", "action": "get"})
        self.assertEqual(listed["provider"], "none")
        self.assertEqual(listed["items"][0]["quantity"], {"numerator": 300, "denominator": 1})
        unavailable = Mock(side_effect=AssertionError("receipt must not open an Application"))
        completed, code = self.call(request, identifier=identifier, application=unavailable)
        self.assertEqual(code, 0, completed)
        self.assertEqual(completed, {**response, "replayed": True})
        with self.assertRaisesRegex(ValueError, "different input"):
            self.call({**request, "menu": {}}, identifier=identifier, application=unavailable)

    def test_lost_reply_fences_new_merchant_writes_across_application_restart(self):
        original = str(uuid.uuid4())
        request = {"operation": "delivery", "action": "select", "slot_ref": "fixture-slot"}
        dispatched = []

        def crash_after_dispatch(value):
            dispatched.append(value)
            raise KeyboardInterrupt

        broken = Mock()
        broken.handle.side_effect = crash_after_dispatch
        with self.assertRaises(KeyboardInterrupt):
            self.call(request, identifier=original, application=lambda: broken)

        fresh = Mock()
        fresh.handle.return_value = {"selected": "fixture-slot"}
        response, code = self.call(request, identifier=original, application=lambda: fresh)
        self.assertEqual((response["status"], code), ("incomplete", 1))
        response, code = self.call(request, application=lambda: fresh)
        self.assertEqual((response["status"], response["original_request_id"], code),
                         ("reconciliation_required", original, 1))
        fresh.handle.assert_not_called()
        self.assertEqual(dispatched, [request])

        verified, code = self.call({"operation": "delivery", "action": "list"}, application=lambda: fresh)
        self.assertEqual((verified["result"]["selected"], code), ("fixture-slot", 0))
        self.assertEqual(foreground.unresolved(self.calls), original)
        foreground.acknowledge(self.calls, original, verified["request_id"],
                               "Fresh picker confirms the original slot is selected")
        self.assertIsNone(foreground.unresolved(self.calls))
        record = foreground.read_receipt(self.calls, original)
        self.assertNotIn("response", record)
        self.assertIn("original slot", record["reconciliation"]["evidence"])

    def test_handled_error_after_delivery_dispatch_still_fences_new_writes(self):
        dispatch = Mock()
        dispatch.handle.side_effect = HouseholdError("store selected the window but readback was uncertain")
        request = {"operation": "delivery", "action": "select", "slot_ref": "original-slot"}
        response, code = self.call(request, application=lambda: dispatch)
        self.assertEqual((response["outcome"], code), ("unknown", 1))
        original = response["request_id"]
        historical, code = foreground.receipt_view(foreground.read_receipt(self.calls, original), original)
        self.assertEqual(historical["receipt_build"]["source_commit"], "a" * 40)
        blocked, code = self.call({**request, "slot_ref": "another-slot"}, application=lambda: dispatch)
        self.assertEqual((blocked["original_request_id"], code), (original, 1))
        dispatch.handle.assert_called_once()

    def test_history_never_restores_native_send_grants(self):
        app = Mock()
        app.handle.return_value = {"dispatch": True, "token": "original", "notice": {"dispatch": True}}
        request = {"operation": "recipe_delivery", "action": "begin"}
        original, code = self.call(request, application=lambda: app)
        self.assertTrue(original["result"]["dispatch"])
        saved, code = self.call(request, identifier=original["request_id"], application=lambda: app)
        self.assertEqual(code, 0)
        self.assertFalse(saved["result"]["dispatch"])
        self.assertFalse(saved["result"]["notice"]["dispatch"])
        self.assertEqual(saved["result"]["token"], "original")
        app.handle.assert_called_once()

    def test_receipt_refuses_a_replaced_ledger(self):
        shutil.rmtree(self.calls)
        self.calls.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "original foreground receipt ledger"):
            foreground.receipt_directory(self.state, create=True)

    def test_historical_file_receipt_checks_original_bytes(self):
        output = self.state / "menu.pdf"
        original = b"%PDF-original"
        output.write_bytes(original)
        identifier = str(uuid.uuid4())
        response = {"ok": True, "result": {"output_path": str(output), "bytes": len(original),
                                          "sha256": hashlib.sha256(original).hexdigest(), "sent": False}}
        record = {"response": response, "exit_code": 0, "build": self.build}
        historical, code = foreground.receipt_view(record, identifier)
        self.assertEqual((historical["result"], code), (response["result"], 0))
        output.write_bytes(b"%PDF-replaced")
        historical, code = foreground.receipt_view(record, identifier)
        self.assertEqual((historical["status"], code), ("output_unavailable", 1))

    def test_oda_readiness_requires_authenticated_cart_and_matching_browser(self):
        app = Mock()
        app.provider = "oda"
        app.integration = {"status": "ready"}
        app._browser_operation.return_value = nullcontext()
        app._cart_lines.side_effect = Application._cart_lines.__get__(app)
        app._product_id.side_effect = Application._product_id.__get__(app)
        app.provider_client.call.side_effect = [
            {"items": [], "count": 0},
            {"result": [{"address": "Fixture only", "isSelected": True}]},
        ]
        self.assertEqual(foreground.verify_store(app), {"provider": "oda", "authenticated_cart": True,
                                                        "checkout_account_matches_oauth": True})
        self.assertEqual([call.args[0] for call in app.provider_client.call.call_args_list],
                         ["get_cart", "get_delivery_addresses"])
        self.assertEqual(app.browser.verify_connection_account.call_args.args, ("Fixture only",))

        app.provider_client.call.side_effect = [{"items": [], "count": 0},
                                                {"result": [{"address": "Fixture only", "isSelected": True}]}]
        app.browser.verify_connection_account.side_effect = HouseholdError("browser belongs to another account")
        response, code = self.call({"operation": "store_check"}, application=lambda: app)
        self.assertEqual(code, 1)
        self.assertIn("another account", response["error"])

    def test_meny_readiness_rejects_incomplete_signed_in_cart(self):
        app = Mock()
        app.provider = "meny"
        app._browser_operation.return_value = nullcontext()
        app._cart_lines.return_value = ({}, {})
        app.provider_client.call.return_value = {"items": []}
        with self.assertRaisesRegex(HouseholdError, "complete quantities"):
            foreground.verify_store(app)
        self.assertEqual(app.browser.verify_order_change.call_args.args, (None, None))
        self.assertEqual([call.args[0] for call in app.provider_client.call.call_args_list], ["get_cart"])


if __name__ == "__main__":
    unittest.main()
