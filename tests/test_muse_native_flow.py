"""Original-account startup and real shared custody for a composed native adapter."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError
from clients import muse
from muse_browser import MuseBrowser, claim_request, digest, read_json, respond_request
from muse_native_provider import MuseNativeReadProvider
from test_muse_client import wait_for
from test_muse_native_provider import ACCOUNT, observation


class NativeCompositionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-flow-")
        self.addCleanup(temporary.cleanup)
        self.ops = Path(temporary.name).resolve() / "ops"
        self.ops.mkdir(mode=0o700)
        self.broker = self.ops / "browser"
        self.broker.mkdir(mode=0o700)
        for name in ("requests", "claims", "consumed", "responses", "endings", "closed"):
            (self.broker / name).mkdir(mode=0o700)
        self.provider = MuseNativeReadProvider(self.ops, "synthetic-original-task", accepted_account=ACCOUNT)
        self.browser = MuseBrowser(self.broker, "synthetic-original-task", self.provider)

    def test_financial_adapter_borrows_native_operation_and_excludes_other_owner(self):
        competing = MuseNativeReadProvider(self.ops, "synthetic-original-task", accepted_account=ACCOUNT)
        with self.browser._custody():
            with self.provider.operation():
                self.assertTrue(self.provider._operation.held)
                with self.assertRaises(HouseholdError), competing.operation():
                    self.fail("another owner acquired the existing native custody")
        with competing.operation():
            self.assertFalse(self.provider._operation.held)
        with self.assertRaisesRegex(ValueError, "synthetic failure"), self.browser._custody():
            raise ValueError("synthetic failure")
        with competing.operation():
            self.assertFalse(self.provider._operation.held)

    def test_financial_adapter_cannot_borrow_another_task_chain(self):
        browser = MuseBrowser(self.broker, "synthetic-other-task", self.provider)
        with self.assertRaisesRegex(HouseholdError, "binding differ"), browser._custody():
            self.fail("another task chain borrowed provider custody")
        with self.provider.operation():
            pass

    def test_process_local_account_adoption_is_insufficient_for_financial_custody(self):
        provider = MuseNativeReadProvider(self.ops, "synthetic-original-task")
        browser = MuseBrowser(self.broker, "synthetic-original-task", provider)
        with self.assertRaisesRegex(HouseholdError, "original accepted account"), browser._custody():
            self.fail("unbound native provider admitted financial custody")

    def test_invalid_accepted_file_cannot_fall_back_to_first_observation(self):
        home = self.ops.parent / "h"
        muse.initialize(home, "oda", "Synthetic household", credential_name="custom.synthetic",
                        operation_directory=self.ops)
        marker = (home / "muse-client.json").read_bytes()
        accepted = self.ops.parent / "accepted.json"
        for invalid in (None, {}, {**ACCOUNT, "edit_urls": []}):
            accepted.write_text(json.dumps(invalid))
            accepted.chmod(0o600)
            result = subprocess.run([sys.executable, "-I", "-B", str(Path(muse.__file__)), "run",
                "--home", str(home), "--provider-transport", "browser_readonly",
                "--browser-directory", str(self.broker), "--browser-task-id", "synthetic-original-task",
                "--accepted-account", str(accepted)], capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse((home / "service.sock").exists())
            self.assertEqual(list((self.broker / "requests").glob("*.json")), [])
            self.assertEqual((home / "muse-client.json").read_bytes(), marker)

    def test_first_real_broker_observation_cannot_adopt_foreign_account(self):
        accepted = deepcopy(ACCOUNT)
        accepted["edit_urls"].append("https://oda.com/no/account/delivery/edit/202/")
        provider = MuseNativeReadProvider(self.ops, "synthetic-original-task", accepted_account=accepted)
        errors = []

        def produce():
            try:
                path = wait_for(lambda: next((self.broker / "requests").glob("*.json"), None))
                record = read_json(path)
                self.assertEqual(record["payload"]["accepted_account"], accepted)
                claim_request(self.broker, record["request_id"], record["task_id"])
                # Deliver a genuine positive observation of only the other account's set.
                # The ordinary consumer must reject it even without producer prevalidation.
                respond_request(self.broker, record["request_id"], record["task_id"], {
                    "request_id": record["request_id"], "request_digest": digest(record),
                    "task_id": record["task_id"], "observed_at": datetime.now(timezone.utc).isoformat(),
                    "task_state": "completed", "facts": observation("get_delivery_addresses", {})})
            except Exception as error:
                errors.append(error)

        # Only Linux has the cloud's process identity API. Other hosts exercise
        # the real broker and locks with a synthetic identity, without a browser/network.
        identity = (nullcontext() if sys.platform.startswith("linux") else
                    mock.patch("muse_browser.process_start", return_value="12345"))
        with identity:
            worker = threading.Thread(target=produce)
            worker.start()
            try:
                with self.assertRaisesRegex(HouseholdError, "account evidence changed"):
                    provider.probe()
            finally:
                worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            paths = list((self.broker / "requests").glob("*.json"))
            self.assertEqual(len(paths), 1)
            with self.assertRaisesRegex(HouseholdError, "account evidence changed"):
                provider.probe()
            self.assertEqual(list((self.broker / "requests").glob("*.json")), paths)
        self.assertEqual(provider._account_ids, {101, 202})


if __name__ == "__main__":
    unittest.main()
