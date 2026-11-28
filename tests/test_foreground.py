from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import HouseholdError, StateStore
import foreground
import hosted_oda
import meny_public
import runtime_ownership
import service
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

    def test_public_meny_search_never_opens_the_household_or_replays_https(self):
        application = Mock(side_effect=AssertionError("store-free app should stay closed"))
        request = {"operation": "catalog", "action": "products", "provider": "meny",
                   "query": "havregryn", "response_view": "agent"}
        with patch.object(meny_public, "products", return_value=([], 0)) as get:
            current, code = self.call(request, application=application)
            self.assertEqual(code, 0)
            self.assertEqual(current["result"]["scope"], "public_catalog")
            replay, code = self.call(request, identifier=current["request_id"], application=application)
            self.assertEqual(code, 0)
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["status"], "historical_public_catalog")
            self.assertEqual(replay["result"], current["result"])
        get.assert_called_once_with("havregryn", 5)
        application.assert_not_called()

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
        self.build = {"source_commit": "a" * 40, "installed_runtime_sha256": "b" * 64,
                      "source_sha256": "d" * 64}
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

    def test_order_handoff_binds_exact_order_and_redacts_receipt(self):
        order_id = "ORDER-PRIVATE-FIXTURE"
        request = {"operation": "orders", "action": "change_handoff", "order_id": order_id,
                   "response_view": "agent"}
        opened, code = self.begin(request)
        self.assertEqual(code, 0, opened)
        frame = opened["hosted_request"]
        self.assertEqual(frame["additional_reads"][0]["arguments"], {"order_number": order_id})
        path = self.calls / f"{self.identifier}.json"
        self.assertNotIn(order_id, path.read_text())
        reply = {**{key: frame[key] for key in ("read_id", "plugin_id", "tool", "arguments")},
                 "observed_at": datetime.now(timezone.utc).isoformat(),
                 "orders": {"orderNumber": order_id, "status": "paid_and_modifiable"},
                 "order": {"orderNumber": order_id, "currency": "NOK", "grossAmount": 42, "deliveryDate": "2030-03-03", "products": []},
                 "addresses": [{"id": 7, "isSelected": True}],
                 "browser": {"url": f"https://oda.com/no/account/orders/{order_id}/", "order_number": order_id,
                             "browser_id": "fixture", "tab_id": "fixture-tab", "address_edit_path": "/no/account/delivery/edit/7/",
                             "total_ore": 4200, "delivery_date": "2030-03-03", "available_changes": ["items"]}}
        for key, value in (("address_edit_path", "/no/account/delivery/edit/8/"), ("total_ore", 4000)):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "exact order|amount or delivery date"):
                self.finish({**reply, "browser": {**reply["browser"], key: value}})
        current, code = self.finish(reply)
        self.assertEqual(code, 0, current)
        self.assertTrue(current["result"]["handoff_ready"])
        self.assertFalse(current["result"]["dispatch"])
        self.assertNotIn(order_id, path.read_text())

    def test_rejects_wrong_origin_build_and_invalid_cart_before_acceptance(self):
        reply = self.example_reply()
        for key, value in (("plugin_id", "plugin_other"), ("read_id", str(uuid.uuid4())), ("tool", "manipulate_cart")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "original hosted Oda request"):
                self.finish({**reply, key: value})
        with self.assertRaisesRegex(ValueError, "installed build"):
            self.finish(reply, build={**self.build, "installed_runtime_sha256": "c" * 64})
        with self.assertRaisesRegex(ValueError, "installed build"):
            self.finish(reply, build={**self.build, "source_sha256": "e" * 64})
        with self.assertRaisesRegex(ValueError, "installed build"):
            self.finish(reply, build={**self.build, "source_commit": "f" * 40})
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

    def test_expired_read_is_distinct_from_an_uncertain_mutation(self):
        reply = self.example_reply()
        record = foreground.read_receipt(self.calls, self.identifier)
        record["hosted_read"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        foreground.install.write_json(self.calls / f"{self.identifier}.json", record)
        ending, code = self.begin()
        self.assertEqual((code, ending["status"]), (1, "hosted_read_expired"))
        self.assertIn("new request ID", ending["recovery"])
        with self.assertRaisesRegex(ValueError, "expired"):
            self.finish(reply)

    def test_nested_cart_fields_and_inconsistent_amounts_do_not_record_a_read(self):
        reply = self.example_reply()
        cart = reply["cart"]
        item = cart["groups"][0]["items"][0]
        for patch in ({"totalGrossAmount": {"private": "marker"}}, {"totalGrossAmount": float("inf")},
                      {"product": {"id": 42, "name": {"private": "marker"}}},
                      {"product": {"id": 42, "name": "Synthetic\ncontrol"}},
                      {"quantity": float("inf")}, {"quantity": 3}):
            with self.subTest(patch=patch), self.assertRaises((ValueError, HouseholdError)):
                self.finish({**reply, "cart": {**cart, "groups": [{"items": [{**item, **patch}]}]}})
        with self.assertRaisesRegex(ValueError, "unrecognized hosted Oda cart breakdown"):
            self.finish({**reply, "cart": {**cart, "totalBreakdown": {}}})
        with self.assertRaisesRegex(ValueError, "unrecognized hosted Oda cart breakdown"):
            self.finish({**reply, "cart": {**cart, "totalBreakdown": {**cart["totalBreakdown"], "newFee": "3.00"}}})
        with self.assertRaisesRegex(ValueError, "line totals do not match"):
            too_low = {**cart["totalBreakdown"], "subtotal": "9.00", "total": "9.00"}
            self.finish({**reply, "cart": {**cart, "totalGrossAmount": "9.00", "totalBreakdown": too_low}})
        with self.assertRaisesRegex(ValueError, "cart totals could not be verified"):
            self.finish({**reply, "cart": {**cart, "totalBreakdown": {**cart["totalBreakdown"], "total": "9.00"}}})
        self.assertEqual(self.begin()[0]["status"], "incomplete")

    def test_bounded_cart_summary_reports_truncation_without_a_paging_token(self):
        reply = self.example_reply()
        cart = reply["cart"]
        item = cart["groups"][0]["items"][0]
        cart["groups"][0]["items"] = [item] * 21
        cart["productQuantityCount"] = 42
        cart["totalGrossAmount"] = "210.00"
        cart["totalBreakdown"].update(subtotal="210.00", total="210.00")
        result, code = self.finish(reply)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["count"], 42)
        lines = result["result"]["lines"]
        self.assertEqual(lines["total"], 21)
        self.assertTrue(lines["truncated"])
        self.assertGreater(len(lines["items"]), 0)
        self.assertNotIn("next_offset", lines)

    def test_cart_preserves_standard_unicode_spacing_in_product_names(self):
        reply = self.example_reply()
        name = "Fixture\u00a0product"
        reply["cart"]["groups"][0]["items"][0]["product"]["name"] = name
        result, code = self.finish(reply)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["lines"]["items"][0]["name"], name)

    def test_empty_cart_with_optional_provider_fields_completes(self):
        reply = self.example_reply()
        cart = reply["cart"]
        cart.update(groups=[], productQuantityCount=0, totalGrossAmount="0.00",
                    totalBreakdown={**dict.fromkeys(hosted_oda.BREAKDOWN_FIELDS, "0.00"),
                                    "surchargeThresholdDescription": "Omitted from the cart view"})
        with self.assertRaisesRegex(ValueError, "line totals do not match"):
            false_amounts = {**cart["totalBreakdown"], "subtotal": "9.00", "deliveryFee": "1.00", "total": "10.00"}
            self.finish({**reply, "cart": {**cart, "totalGrossAmount": "10.00", "totalBreakdown": false_amounts}})
        result, code = self.finish(reply)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["count"], 0)
        self.assertEqual(result["result"]["total"], 0)
        self.assertEqual(result["result"]["lines"]["items"], [])
        self.assertNotIn("Omitted from the cart view", json.dumps(result))

    def test_catalog_product_search_requires_the_exact_query_and_scope(self):
        request = {"operation": "catalog", "action": "products", "query": "rosenkål", "limit": 1,
                   "response_view": "agent"}
        pending, code = self.begin(request)
        self.assertEqual(code, 0)
        self.assertFalse(pending["catalog_read"])
        frame = pending["hosted_request"]
        self.assertEqual(frame["tool"], "product_search")
        self.assertEqual(frame["arguments"], {"queries": ["rosenkål"], "page": 1, "size": 1})
        with self.assertRaisesRegex(ValueError, "different input"):
            self.begin({**request, "query": "potet"})
        with self.assertRaisesRegex(ValueError, "different input"):
            self.begin()
        binding = {key: frame[key] for key in ("read_id", "plugin_id", "tool", "arguments")}
        binding["observed_at"] = datetime.now(timezone.utc).isoformat()
        item = {"id": 42, "name": "Synthetic sprouts", "description": "250 g", "price": "19.90",
                "availability": {"isAvailable": True}, "unitPrice": "79.60", "unitName": "kg",
                "allergen_free_from": ["peanut"]}
        batch = {"query": "rosenkål", "hasMore": True, "products": [item]}
        with self.assertRaisesRegex(ValueError, "arguments do not match"):
            self.finish({**binding, "arguments": {**frame["arguments"], "size": 2},
                         "catalog": {"result": [batch]}})
        for other in ({**batch, "query": "potet"}, {**batch, "products": [item, {**item, "id": 43}]}):
            with self.subTest(batch=other), self.assertRaises(HouseholdError):
                self.finish({**binding, "catalog": {"result": [other]}})
        with self.assertRaisesRegex(ValueError, "successful structured"):
            self.finish({**binding, "catalog": {"isError": True, "result": [batch]}})
        for field, value in (("description", "250 g\x1b[31m"), ("unitPrice", "79.60\u202e"), ("brand", "A\x00B")):
            changed = {**batch, "products": [{**item, field: value}]}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "display contains control text"):
                self.finish({**binding, "catalog": {"result": [changed]}})
        ending, code = self.finish({**binding, "catalog": {"result": [batch]}})
        self.assertEqual(code, 0, ending)
        self.assertEqual(ending["status"], "catalog_read")
        self.assertTrue(ending["result"]["scope"]["has_more"])
        self.assertEqual(ending["result"]["products"][0]["observed_at"], binding["observed_at"])
        self.assertEqual(ending["result"]["products"][0]["purchase_options"][0]["merchandise_ore"], 1990)
        self.assertNotIn("dietary_evidence", ending["result"]["products"][0])
        self.assertEqual(self.begin(request)[0]["status"], "historical_catalog_read")
        payload = (self.calls / f"{self.identifier}.json").read_text()
        self.assertNotIn("rosenkål", payload)
        self.assertNotIn("Synthetic sprouts", payload)

    def test_catalog_recipe_search_keeps_only_distinct_recipe_identity_and_shop_link(self):
        request = {"operation": "catalog", "action": "recipes", "query": "pasta", "limit": 2,
                   "response_view": "agent"}
        pending, _ = self.begin(request)
        frame = pending["hosted_request"]
        self.assertEqual(frame["tool"], "recipe_search")
        self.assertEqual(frame["arguments"], {"query": "pasta", "page": 1, "size": 2})
        binding = {key: frame[key] for key in ("read_id", "plugin_id", "tool", "arguments")}
        binding["observed_at"] = datetime.now(timezone.utc).isoformat()
        url = "https://oda.com/no/recipes/42-fixture/"
        title = "Synthetic\u00a0pasta"
        recipe = {"id": 42, "title": title, "url": url, "likeCount": 7, "isLikedByUser": True}
        with self.assertRaisesRegex(ValueError, "duplicate identities"):
            self.finish({**binding, "catalog": {"recipes": [recipe, recipe], "hasMore": False}})
        ending, code = self.finish({**binding, "catalog": {"hasMore": False, "recipes": [recipe,
            {**recipe, "id": 43, "url": "https://oda.com/no/re\ncipes/43-fixture/"}]}})
        self.assertEqual(code, 0, ending)
        self.assertEqual(ending["result"]["recipes"], [
            {"recipe_id": 42, "name": title, "url": url},
            {"recipe_id": 43, "name": title},
        ])

    def test_delivery_reads_validate_the_exact_date_without_granting_a_selection(self):
        day = "2027-01-04"
        request = {"operation": "delivery", "action": "list", "date": day, "response_view": "agent"}
        frame = self.begin(request)[0]["hosted_request"]
        self.assertEqual(frame["tool"], "get_delivery_slots")
        self.assertEqual(frame["arguments"], {"delivery_date": day})
        binding = {key: frame[key] for key in ("read_id", "plugin_id", "tool", "arguments")}
        binding["observed_at"] = datetime.now(timezone.utc).isoformat()
        with self.assertRaisesRegex(ValueError, "date changed"):
            self.finish({**binding, "delivery": {"deliveryDate": "2027-01-05", "slots": []}})
        with self.assertRaisesRegex(HouseholdError, "slot changed"):
            self.finish({**binding, "delivery": {"deliveryDate": day, "slots": [{"isFull": True}]}})
        slot = {"id": 22, "openDatetime": f"{day}T11:00:00+01:00", "closeDatetime": f"{day}T12:00:00+01:00",
                "price": "kr\u00a039", "isSelected": False, "isFull": False, "isUnavailable": False}
        ending, code = self.finish({**binding, "delivery": {"deliveryDate": day, "slots": [slot]}})
        self.assertEqual(code, 0, ending)
        self.assertEqual(ending["status"], "delivery_read")
        self.assertEqual(ending["result"]["slots"][0]["price_ore"], 3900)
        with self.assertRaisesRegex(ValueError, "read-only actions"):
            self.begin({**request, "action": "select"})

    def test_orders_read_only_actions_bind_an_exact_id_and_keep_details_out_of_history(self):
        common = {"operation": "orders", "response_view": "agent"}
        item = {"product": {"id": 42, "name": "Synthetic order contents"}, "quantity": 2,
                "totalGrossAmount": "10.00"}
        order = {"orderNumber": "12345", "deliveryDate": "2027-01-04", "currency": "NOK",
                 "grossAmount": 20.0, "products": [item], "private_extra": "Never displayed"}
        for action, tool, fields in (("list", "get_orders", {}), ("get", "get_order", {"order_id": "12345"}),
                                     ("track", "order_tracking", {"order_id": "12345"})):
            with self.subTest(action=action):
                self.identifier = str(uuid.uuid4())
                request = {**common, "action": action, **fields}
                frame = self.begin(request)[0]["hosted_request"]
                self.assertEqual(frame["tool"], tool)
                binding = {key: frame[key] for key in ("read_id", "plugin_id", "tool", "arguments")}
                binding["observed_at"] = datetime.now(timezone.utc).isoformat()
                path = self.calls / f"{self.identifier}.json"
                self.assertNotIn('"12345"', path.read_text())
                if action == "list":
                    payload = {"hasMore": False, "orders": [order]}
                else:
                    self.assertEqual(frame["arguments"], {"order_number": "12345"})
                    payload = order if action == "get" else {"orderNumber": "12345", "status": "picking"}
                    with self.assertRaises(HouseholdError):
                        self.finish({**binding, "orders": {**payload, "orderNumber": "34567"}})
                    with self.assertRaisesRegex(ValueError, "orderNumber does not match"):
                        self.finish({**binding, "orders": {**payload, "orderNumber": None, "id": "12345"}})
                ending, code = self.finish({**binding, "orders": payload})
                self.assertEqual(code, 0, ending)
                self.assertEqual(ending["status"], "orders_read")
                self.assertNotIn("Never displayed", json.dumps(ending))
                historical = self.begin(request)[0]
                self.assertEqual(historical["status"], "historical_orders_read")
                self.assertNotIn('"12345"', path.read_text())

    def test_installed_hosted_front_door_issues_completes_and_recovers(self):
        import hosted_oda_cart

        root = self.state.resolve()
        home, code_root = root / "home", root / "program"
        release = code_root / "fixture-release"
        venv = release / "venv"
        venv.mkdir(parents=True)
        home.mkdir(mode=0o700)
        state = home / "state"
        state.mkdir(mode=0o700)
        (code_root / "current").symlink_to(release, target_is_directory=True)
        config = home / "config.json"
        manifest = {"manager": "external", "home": str(home), "code_root": str(code_root), "release": str(release),
                    "hosted_oda_plugin": self.plugin, "runtime_identity": self.build,
                    "paths": {"state": str(state), "config": str(config), "socket": str(home / "service.sock")}}
        foreground.install.write_json(home / "runtime.json", manifest)
        foreground.install.write_json(code_root / "owner.json", {"home": str(home)})
        foreground.install.write_json(config, {"provider": "oda", "household": "Synthetic Dot",
                                               "hosted_oda_plugin": self.plugin})
        foreground.install.write_json(state / "state.json", {"fixture": "retain this state"})
        recipes = state / "recipes.sqlite3"
        fd = os.open(recipes, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(b"retained fixture database")

        def execute(action, body=b""):
            options = Namespace(home=home, action=action, request_id=self.identifier,
                                image_output=None, delivery_output=None)
            with patch.object(sys, "stdin", Namespace(buffer=io.BytesIO(body))):
                return foreground.execute(options)

        with (patch.object(foreground, "SOURCE", release), patch.object(sys, "prefix", str(venv)),
              patch.object(service, "BUILD_IDENTITY", {**self.build, "identity_status": "recorded"}),
              patch.object(runtime_ownership, "assert_no_legacy_service"),
              patch.object(hosted_oda_cart, "DOT_WORKSPACE", root),
              patch.object(foreground, "verify_release_layout"),
              patch.object(service, "build_application", side_effect=AssertionError("started a service"))):
            discovered, code = execute("tools")
            self.assertEqual((code, discovered["transport"]), (0, "hosted_mcp"))
            self.assertEqual(discovered["cart_actions"], ["get"])
            self.assertEqual(discovered["cart_write_command"], "hosted-cart")
            self.assertEqual(discovered["menu_shopping_start"], "begin_menu")
            self.assertEqual(discovered["menu_shopping_restore"], "restore_menu")
            with self.assertRaisesRegex(ValueError, "read-only actions"):
                execute("call", b'{"operation":"cart","action":"ensure"}')
            self.assertEqual(execute("receipt")[0]["status"], "not_found")
            frame = execute("call", b'{"operation":"cart","action":"get"}')[0]["hosted_request"]
            with self.assertRaisesRegex(ValueError, "size limit"):
                execute("complete-hosted", b" " * (foreground.cli.MAX_INPUT + 1))
            reply = {key: frame[key] for key in ("read_id", "plugin_id", "tool")}
            reply.update(observed_at=datetime.now(timezone.utc).isoformat(),
                         cart={"groups": [], "productQuantityCount": 0, "totalGrossAmount": "0.00",
                               "totalBreakdown": dict.fromkeys(hosted_oda.BREAKDOWN_FIELDS, "0.00"),
                               "url": "https://oda.com/no/cart/"})
            with self.assertRaisesRegex(ValueError, "takes no arguments"):
                execute("complete-hosted", json.dumps({**reply, "arguments": {"override": True}}).encode())
            reply["arguments"] = frame["arguments"]
            self.assertEqual(execute("complete-hosted", json.dumps(reply).encode())[0]["status"], "cart_read")
            historical, code = execute("receipt")
            self.assertEqual((code, historical["status"]), (0, "historical_cart_read"))
            self.assertNotIn("result", historical)
            begun, code = execute("hosted-cart", b'{"action":"begin","product_id":515,"isolated_new_cart":true,'
                                  b'"authorized_addresses":[{"id":7,"isSelected":true}]}')
            self.assertEqual((code, begun["status"], begun["dispatch"]), (0, "reading_baseline", False))
            stopped, code = execute("hosted-cart", b'{"action":"abort"}')
            self.assertEqual((code, stopped["status"], stopped["dispatch"]), (0, "aborted", False))
        self.assertEqual(recipes.read_bytes(), b"retained fixture database")

    def test_staged_hosted_oda_import_works_without_client_packages(self):
        root = self.state.resolve()
        staged = root / "staged"
        staged.mkdir()
        for module in foreground.SOURCE.glob("*.py"):
            shutil.copyfile(module, staged / module.name)
        result = subprocess.run([sys.executable, "-I", "-B", "-c",
                                 "import sys; sys.path.insert(0, sys.argv[1]); import hosted_oda_cart",
                                 str(staged)], cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
