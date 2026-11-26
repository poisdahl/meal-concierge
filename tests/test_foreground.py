from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import StateStore
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

    def test_lost_reply_is_inspectable_without_replaying_the_original_call(self):
        original = str(uuid.uuid4())
        request = {"operation": "recipe_delivery", "action": "begin", "job_id": "original-job", "part_id": "part"}
        entered = []

        def crash_after_dispatch(value):
            entered.append(value)
            raise KeyboardInterrupt

        broken = Mock()
        broken.handle.side_effect = crash_after_dispatch
        with self.assertRaises(KeyboardInterrupt):
            self.call(request, identifier=original, application=lambda: broken)

        fresh = Mock()
        response, code = self.call(request, identifier=original, application=lambda: fresh)
        self.assertEqual((response["status"], code), ("incomplete", 1))
        fresh.handle.assert_not_called()
        self.assertEqual(entered, [request])
        record = foreground.read_receipt(self.calls, original)
        self.assertNotIn("response", record)
        self.assertEqual(response["receipt_build"]["source_commit"], "a" * 40)
        self.assertEqual(response["original_target"], {"job_id": "original-job", "part_id": "part"})

    def test_store_dispatch_stops_before_application_or_intent(self):
        application = Mock()
        for request in ({"operation": "delivery", "action": "select", "slot_ref": "fixture-slot"},
                        {"operation": "checkout", "action": "submit"},
                        {"operation": "products", "action": "apply"},
                        {"operation": "cart", "action": "ensure"},
                        {"operation": "orders", "action": "change_begin"}):
            with self.subTest(request=request), self.assertRaisesRegex(ValueError, "public|record_ingredients"):
                self.call(request, application=application)
        application.assert_not_called()
        self.assertEqual([path.name for path in self.calls.iterdir()], ["ledger.json"])

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

    def test_existing_cover_receipt_can_use_the_managed_asset_digest(self):
        jpeg = self.state / "cover.jpg"
        body = b"immutable managed image fixture"
        jpeg.write_bytes(body)
        output = {"output_path": str(jpeg), "bytes": len(body), "image_status": "available", "content_type": "image/jpeg",
                  "image": {"asset_id": "sha256:" + hashlib.sha256(body).hexdigest()}}
        self.assertTrue(foreground.output_matches(output))
        jpeg.write_bytes(b"x" * len(body))
        self.assertFalse(foreground.output_matches(output))

    def test_discovery_and_planning_default_to_interactive(self):
        app = Mock()
        app.handle.return_value = {}
        for operation, action in (("recipes", "discover"), ("menu", "plan")):
            request = {"operation": operation, "action": action}
            self.call(request, application=lambda: app)
            app.handle.assert_called_with({**request, "interactive": True})
            self.call({**request, "interactive": False}, application=lambda: app)
            app.handle.assert_called_with({**request, "interactive": False})

    def test_startup_error_has_one_sanitized_cli_result(self):
        output, errors = io.StringIO(), io.StringIO()
        argv = ["foreground.py", "--home", str(self.state), "tools"]
        with (patch.object(sys, "argv", argv), patch.object(foreground.os, "umask"),
              patch.object(foreground, "execute", side_effect=SystemExit("fixture-secret")),
              redirect_stdout(output), redirect_stderr(errors)):
            self.assertEqual(foreground.main(), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertNotIn("fixture-secret", output.getvalue())
        self.assertEqual(errors.getvalue(), "")
        receipt = json.loads(output.getvalue())
        self.assertFalse(receipt["ok"])
        self.assertIn("install.sh doctor", receipt["error"])


if __name__ == "__main__":
    unittest.main()
