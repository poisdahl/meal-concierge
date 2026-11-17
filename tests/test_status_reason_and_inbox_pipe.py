"""The agent sees why a store is unavailable, and a pipe in the pack inbox cannot hang the service."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_views import project_agent_result
from core import HouseholdError, StateStore
from service import Application
from test_managed_recipe_pack import Provider


class StatusReasonTests(unittest.TestCase):
    def test_status_view_carries_the_integration_message(self):
        integration = {"status": "unavailable", "provider": "oda",
                       "message": "Oda login is required: token expired"}
        view = project_agent_result("status", None, {"integration": integration})["integration"]
        self.assertEqual(view, integration)
        long = project_agent_result("status", None, {"integration": {**integration, "message": "x" * 1000}})
        self.assertEqual(long["integration"]["message"], {"excerpt": "x" * 400, "omitted_characters": 600})
        ready = project_agent_result("status", None, {"integration": {"status": "ready", "provider": "oda"}})
        self.assertEqual(ready["integration"], {"status": "ready", "provider": "oda"})


class InboxPipeTests(unittest.TestCase):
    def test_a_named_pipe_in_the_inbox_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox"
            inbox.mkdir(mode=0o700)
            store = StateStore(Path(root) / "state", {"household": "inbox-pipe-test", "provider": "oda"})
            app = Application(store, Provider(), None, recipe_pack_inbox=inbox)
            archive_id = "a" * 64 + ".zip"
            os.mkfifo(inbox / archive_id, 0o600)
            outcome = []

            def inspect():
                try:
                    outcome.append(app.handle({"operation": "recipe_pack", "action": "inspect", "archive_id": archive_id}))
                except HouseholdError as exc:
                    outcome.append(str(exc))

            worker = threading.Thread(target=inspect, daemon=True)
            worker.start()
            worker.join(10)
            blocked = worker.is_alive()
            if blocked:
                # Release the open so the temporary directory can be removed.
                os.close(os.open(inbox / archive_id, os.O_WRONLY | os.O_NONBLOCK))
                worker.join(10)
            self.assertFalse(blocked, "opening the inbox entry waited for a writer")
            self.assertEqual(outcome, ["selected local recipe pack is not a bounded regular file"])
            # The service still answers afterwards.
            self.assertEqual(app.handle({"operation": "recipe_pack", "action": "status"})["available"], True)


if __name__ == "__main__":
    unittest.main()
