"""Observable isolation, validation and lock behavior of state reads."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore


class StateReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(self.temp.name, {'household': 'synthetic', 'provider': 'oda'})

    def test_reads_are_detached_and_do_not_rewrite_or_retimestamp(self):
        with self.store.locked() as state:
            state['recipe_usage']['old'] = {'week': '2020-W01', 'status': 'cancelled'}
        before = self.store.path.read_bytes()
        modified = self.store.path.stat().st_mtime_ns
        first = self.store.read()
        first['recipe_usage']['old']['status'] = 'cooked'
        self.assertEqual(self.store.read()['recipe_usage']['old']['status'], 'cancelled')
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.path.stat().st_mtime_ns, modified)

    def test_read_rejects_nonfinite_json_and_overflow(self):
        for value in ('NaN', 'Infinity', '-Infinity', '1e999'):
            with self.subTest(value=value):
                self.store.path.write_text('{"bad":' + value + '}')
                with self.assertRaisesRegex(HouseholdError, 'invalid JSON'):
                    self.store.read()

    def test_writes_preserve_json_type_and_signed_zero_changes(self):
        for before, after in ((1, True), (1, 1.0), (0.0, -0.0)):
            with self.subTest(before=repr(before), after=repr(after)):
                with self.store.locked() as state:
                    state['synthetic_value'] = before
                with self.store.locked() as state:
                    state['synthetic_value'] = after
                self.assertEqual(json.dumps(self.store.read()['synthetic_value']), json.dumps(after))

    def test_failed_invalid_write_keeps_original_state(self):
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(HouseholdError, 'invalid JSON'):
            with self.store.locked() as state:
                state['synthetic_value'] = float('nan')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_read_waits_for_writer_and_returns_committed_state(self):
        script = '''import fcntl,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from core import StateStore
store=StateStore.__new__(StateStore)
store.path=Path(sys.argv[2]); store.lock_path=store.path.with_name('state.lock')
print('reading',flush=True)
print(json.dumps(store.read()['menu']),flush=True)
'''
        with self.store.locked() as state:
            proc = subprocess.Popen([sys.executable, '-I', '-B', '-c', script,
                                     str(Path(__file__).resolve().parents[1]), str(self.store.path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
            self.assertEqual(proc.stdout.readline().strip(), 'reading')
            self.assertIsNone(proc.poll())
            state['menu'] = {'name': 'newly committed'}
        output, errors = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, errors)
        self.assertEqual(json.loads(output), {'name': 'newly committed'})


if __name__ == '__main__':
    unittest.main()
