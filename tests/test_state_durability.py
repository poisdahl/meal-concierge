"""State commit failures must preserve evidence and stop checkout dispatch."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import core
from service import Application
from test_meal_concierge import CONFIG, FakeMeny


class AtomicStateDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        core._atomic_json(self.path, {"generation": 1})

    def assert_state(self, generation):
        self.assertEqual(json.loads(self.path.read_text()), {"generation": generation})
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_file_sync_replace_and_directory_sync_order(self):
        events = []
        fsync, replace = os.fsync, os.replace

        def sync(descriptor):
            events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
            fsync(descriptor)

        def rename(source, target):
            events.append("replace")
            replace(source, target)

        with mock.patch.object(os, "fsync", side_effect=sync), mock.patch.object(os, "replace", side_effect=rename):
            core._atomic_json(self.path, {"generation": 2})
        self.assertEqual(events, ["file", "replace", "directory"])
        self.assert_state(2)

    def test_file_sync_failure_preserves_old_state(self):
        with mock.patch.object(os, "fsync", side_effect=OSError("file sync failed")):
            with self.assertRaisesRegex(OSError, "file sync failed"):
                core._atomic_json(self.path, {"generation": 2})
        self.assert_state(1)

    def test_directory_open_failure_preserves_old_state(self):
        original_open = os.open

        def open_directory(path, flags, *args):
            if path == self.path.parent:
                raise OSError("directory open failed")
            return original_open(path, flags, *args)

        with mock.patch.object(os, "open", side_effect=open_directory):
            with self.assertRaisesRegex(OSError, "directory open failed"):
                core._atomic_json(self.path, {"generation": 2})
        self.assert_state(1)

    def test_replace_failure_closes_directory_and_preserves_old_state(self):
        with mock.patch.object(os, "replace", side_effect=OSError("replace failed")), mock.patch.object(os, "close", wraps=os.close) as close:
            with self.assertRaisesRegex(OSError, "replace failed"):
                core._atomic_json(self.path, {"generation": 2})
        self.assertEqual(close.call_count, 2)
        self.assert_state(1)

    def test_directory_sync_failure_keeps_visible_replacement_and_closes_descriptors(self):
        original_sync = os.fsync

        def sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("directory sync failed")
            original_sync(descriptor)

        with mock.patch.object(os, "fsync", side_effect=sync), mock.patch.object(os, "close", wraps=os.close) as close:
            with self.assertRaisesRegex(OSError, "directory sync failed"):
                core._atomic_json(self.path, {"generation": 2})
        self.assertEqual(close.call_count, 2)
        self.assert_state(2)


class CheckoutStateDurabilityTests(unittest.TestCase):
    def test_directory_sync_failures_do_not_dispatch_or_replay_checkout(self):
        for failing_status, expected_clicks in (("clicking", 0), ("awaiting_user_payment", 1)):
            with self.subTest(failing_status=failing_status), tempfile.TemporaryDirectory() as temp:
                config = {**CONFIG, "provider": "meny"}
                store = core.StateStore(Path(temp), config)
                provider = FakeMeny()
                app = Application(store, provider, provider)
                prepared = app.handle({"operation": "checkout", "action": "prepare"})
                original_sync = os.fsync
                failures = []

                def sync(descriptor):
                    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                        pending = json.loads(store.path.read_text()).get("pending_checkout") or {}
                        if pending.get("status") == failing_status:
                            failures.append(pending["confirmation_id"])
                            raise OSError("checkout directory sync failed")
                    original_sync(descriptor)

                request = {"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]}
                with mock.patch.object(os, "fsync", side_effect=sync):
                    with self.assertRaisesRegex(OSError, "checkout directory sync failed"):
                        app.handle(request)
                self.assertEqual(failures, [prepared["confirmation_id"]])
                self.assertEqual(provider.checkout_clicks, expected_clicks)
                # Reopen from disk; a lost durability acknowledgement is not
                # proof that the replacement did not occur or a retry is safe.
                reopened = core.StateStore(Path(temp), config)
                self.assertEqual(reopened.read()["pending_checkout"]["status"], failing_status)
                restarted = Application(reopened, provider, provider)
                with self.assertRaisesRegex(core.HouseholdError, "no fresh checkout confirmation"):
                    restarted.handle(request)
                with self.assertRaisesRegex(core.HouseholdError, "reconcile the pending checkout"):
                    restarted.handle({"operation": "checkout", "action": "prepare"})
                self.assertEqual(provider.checkout_clicks, expected_clicks)


if __name__ == "__main__":
    unittest.main()
