from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
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
        self.calls = self.state / "foreground-calls"
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
        foreground.acknowledge(self.calls, original, "Fresh picker confirms the original slot is selected")
        self.assertIsNone(foreground.unresolved(self.calls))
        record = foreground.read_receipt(self.calls, original)
        self.assertNotIn("response", record)
        self.assertIn("original slot", record["reconciliation"])


if __name__ == "__main__":
    unittest.main()
