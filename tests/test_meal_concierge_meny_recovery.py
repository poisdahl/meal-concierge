"""Exercise the actual recovery subprocess against task-owned Linux processes."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meny import MenyClient


@unittest.skipUnless(sys.platform == "linux" and Path("/proc/self/exe").exists(), "Linux process descriptors are required")
class MenyRecoveryProcessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mc43-pidfd-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.sleep = shutil.which("sleep")
        self.assertIsNotNone(self.sleep)
        self.process = subprocess.Popen([self.sleep, "30"], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_owned_process)
        self.client = MenyClient(instance="synthetic-pidfd", binary=self.sleep,
                                 executable=self.sleep, profile=self.directory, home=self.directory,
                                 socket_directory=self.directory, uid=os.getuid(), gid=os.getgid())
        self.pid_file = self.directory / f"{self.client.session}.pid"
        self.pid_file.write_text(str(self.process.pid))
        self.pid_file.chmod(0o600)

    def stop_owned_process(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)

    def terminate(self):
        return self.client._terminate_browser_session(time.monotonic() + 2)

    def test_matching_owned_daemon_is_signaled_through_its_process_descriptor(self):
        self.assertTrue(self.terminate())
        self.assertEqual(self.process.wait(timeout=2), -signal.SIGTERM)

    def test_other_executable_is_not_signaled(self):
        other = shutil.which("true")
        self.assertIsNotNone(other)
        self.client.binary = Path(other)
        self.assertFalse(self.terminate())
        self.assertIsNone(self.process.poll())

    def test_symlink_pid_file_is_not_followed(self):
        target = self.directory / "other.pid"
        self.pid_file.rename(target)
        self.pid_file.symlink_to(target)
        self.assertFalse(self.terminate())
        self.assertIsNone(self.process.poll())

    def test_malformed_or_out_of_bounds_pid_never_signals(self):
        for value in ("not-a-pid", "0", "-1", "100000000"):
            with self.subTest(value=value):
                self.pid_file.write_text(value)
                self.assertFalse(self.terminate())
                self.assertIsNone(self.process.poll())

    def test_other_owner_is_not_signaled(self):
        self.client.uid = os.getuid() + 1
        self.assertFalse(self.terminate())
        self.assertIsNone(self.process.poll())

    def test_expired_deadline_never_signals(self):
        self.assertFalse(self.client._terminate_browser_session(time.monotonic() - 1))
        self.assertIsNone(self.process.poll())

    def test_exited_daemon_cannot_be_signaled_again(self):
        self.stop_owned_process()
        self.assertFalse(self.terminate())


if __name__ == "__main__":
    print(f"native pidfd bindings: open={hasattr(os, 'pidfd_open')} signal={hasattr(signal, 'pidfd_send_signal')}", flush=True)
    unittest.main()
