"""A vanished candidate cannot block maintenance; unreadable live owners can."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_ownership


class ProcessExitTests(unittest.TestCase):
    def check(self, gone):
        result = subprocess.CompletedProcess([], 0,
            stdout='12345 python service.py --state /synthetic/state --config /synthetic/config\n')
        error = subprocess.CalledProcessError(1, ['lsof'])
        with patch.object(runtime_ownership.subprocess, 'run', return_value=result), \
             patch.object(runtime_ownership, 'process_arguments', side_effect=error), \
             patch.object(runtime_ownership.os, 'kill', side_effect=gone) as probe:
            if isinstance(gone, ProcessLookupError):
                runtime_ownership.assert_no_legacy_service('/synthetic/state', None)
            else:
                with self.assertRaises(subprocess.CalledProcessError):
                    runtime_ownership.assert_no_legacy_service('/synthetic/state', None)
            probe.assert_called_once_with(12345, 0)

    def test_disappeared_pid_is_safe_to_skip(self):
        self.check(ProcessLookupError())

    def test_live_pid_with_failed_inspection_still_blocks(self):
        self.check(None)

    def test_permission_failure_still_blocks(self):
        self.check(PermissionError())

    def test_inspected_live_owner_still_blocks_matching_target(self):
        result = subprocess.CompletedProcess([], 0,
            stdout='12345 python service.py --state /synthetic/state --config /synthetic/config\n')
        with patch.object(runtime_ownership.subprocess, 'run', return_value=result), \
             patch.object(runtime_ownership, 'process_arguments', return_value=(
                 ['python', 'service.py', '--state', '/synthetic/state', '--config', '/synthetic/config'],
                 Path('/'))):
            with self.assertRaisesRegex(RuntimeError, 'owns this target'):
                runtime_ownership.assert_no_legacy_service('/synthetic/state', None)


if __name__ == '__main__':
    unittest.main()
