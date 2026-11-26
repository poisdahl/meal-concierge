from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
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

from core import HouseholdError, StateStore
import foreground
import hosted_oda
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


class HostedOdaForegroundTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.calls = foreground.receipt_directory(self.state, create=True)
        self.build = {"source_commit": "a" * 40, "installed_runtime_sha256": "b" * 64}
        self.plugin = "plugin_fixture_oda"
        self.identifier = str(uuid.uuid4())

    def begin(self, request=hosted_oda.CART_REQUEST):
        return foreground.hosted_call(self.calls, self.identifier, request, self.plugin, self.build)

    def example_reply(self):
        frame, code = self.begin()
        self.assertEqual(code, 0, frame)
        self.assertFalse(frame["cart_read"])
        self.assertEqual(frame["hosted_request"]["arguments"], {})
        self.assertEqual(frame["hosted_request"]["tool"], "get_cart")
        reply = {key: frame["hosted_request"][key] for key in ("read_id", "plugin_id", "tool")}
        return {**reply, "observed_at": datetime.now(timezone.utc).isoformat(), "cart": {
            "groups": [{"items": [{"product": {"id": 42, "name": "Fixture product"}, "quantity": 2,
                                   "totalGrossAmount": "10.00"}]}],
            "productQuantityCount": 2, "totalGrossAmount": "10.00",
            "totalBreakdown": {"subtotal": "10.00", "productDiscounts": "0.00", "couponDiscount": "0.00",
                               "surcharge": "0.00", "bottleDeposit": "0.00", "packaging": "0.00",
                               "deliveryFee": "0.00", "total": "10.00"},
            "url": "https://oda.com/no/cart/",
        }}

    def finish(self, reply, *, build=None):
        return foreground.hosted_complete(self.calls, self.identifier, reply, self.plugin, build or self.build)

    def test_finish_returns_current_cart_and_replay_contains_only_metadata(self):
        reply = self.example_reply()
        replay, code = self.begin()
        self.assertEqual((replay["status"], code), ("incomplete", 1))
        self.assertNotIn("hosted_request", replay)
        result, code = self.finish(reply)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "cart_read")
        self.assertEqual(result["evidence"], "host_supplied_oda_mcp")
        self.assertEqual(result["result"]["count"], 2)
        self.assertEqual(result["result"]["total"], 10)
        self.assertEqual(result["result"]["cart_url"], reply["cart"]["url"])
        self.assertEqual(result["result"]["lines"]["items"][0]["price"], "10.00")
        with self.assertRaisesRegex(ValueError, "already ended; use receipt"):
            self.finish({**reply, "plugin_id": "plugin_other"})
        historical, code = self.begin()
        self.assertEqual((historical["status"], code), ("historical_cart_read", 0))
        self.assertNotIn("result", historical)
        self.assertNotIn("Fixture product", (self.calls / f"{self.identifier}.json").read_text())

    def test_rejects_wrong_origin_build_and_invalid_cart_before_acceptance(self):
        reply = self.example_reply()
        for key, value in (("plugin_id", "plugin_other"), ("read_id", str(uuid.uuid4())), ("tool", "manipulate_cart")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "original hosted Oda request"):
                self.finish({**reply, key: value})
        with self.assertRaisesRegex(ValueError, "installed build"):
            self.finish(reply, build={**self.build, "installed_runtime_sha256": "c" * 64})
        with self.assertRaisesRegex(ValueError, "expired|observation time"):
            self.finish({**reply, "observed_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()})
        with self.assertRaisesRegex(ValueError, "unexpected Oda cart link"):
            self.finish({**reply, "cart": {**reply["cart"], "url": "https://invalid.example/no/cart/"}})
        self.assertEqual(self.begin()[0]["status"], "incomplete")
        self.assertEqual(self.finish(reply)[1], 0)

    def test_unsupported_actions_and_unavailable_plugin_never_return_a_cart(self):
        with self.assertRaisesRegex(ValueError, "only cart get"):
            self.begin({"operation": "cart", "action": "ensure"})
        self.assertIsNone(foreground.read_receipt(self.calls, self.identifier))
        reply = self.example_reply()
        del reply["cart"]
        unavailable, code = self.finish({**reply, "error": "unavailable"})
        self.assertEqual((unavailable["status"], code), ("hosted_oda_unavailable", 1))
        self.assertNotIn("result", unavailable)

    def test_nested_cart_fields_and_inconsistent_amounts_do_not_record_a_read(self):
        reply = self.example_reply()
        cart = reply["cart"]
        item = cart["groups"][0]["items"][0]
        for patch in ({"totalGrossAmount": {"private": "marker"}}, {"totalGrossAmount": float("inf")},
                      {"product": {"id": 42, "name": {"private": "marker"}}},
                      {"quantity": float("inf")}, {"quantity": 3}):
            with self.subTest(patch=patch), self.assertRaises((ValueError, HouseholdError)):
                self.finish({**reply, "cart": {**cart, "groups": [{"items": [{**item, **patch}]}]}})
        with self.assertRaisesRegex(ValueError, "incomplete hosted Oda cart breakdown"):
            self.finish({**reply, "cart": {**cart, "totalBreakdown": {}}})
        with self.assertRaisesRegex(ValueError, "cart totals could not be verified"):
            self.finish({**reply, "cart": {**cart, "totalBreakdown": {**cart["totalBreakdown"], "subtotal": "9.00"}}})
        self.assertEqual(self.begin()[0]["status"], "incomplete")

    def test_bounded_cart_summary_reports_truncation_without_a_paging_token(self):
        reply = self.example_reply()
        cart = reply["cart"]
        item = cart["groups"][0]["items"][0]
        cart["groups"][0]["items"] = [item] * 21
        cart["productQuantityCount"] = 42
        result, code = self.finish(reply)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["count"], 42)
        lines = result["result"]["lines"]
        self.assertEqual(lines["total"], 21)
        self.assertTrue(lines["truncated"])
        self.assertGreater(len(lines["items"]), 0)
        self.assertNotIn("next_offset", lines)


if __name__ == "__main__":
    unittest.main()
