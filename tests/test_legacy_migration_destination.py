"""Legacy copies claim a fresh output and never overwrite an existing household."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import DEFAULT_SCHEDULE, HouseholdError, StateStore
import migrate as legacy

CONFIG = {"household": "Synthetic legacy copy", "provider": "oda"}
PRODUCT = {"product_id": "123", "product_name": "Synthetic oats", "quantity": 1}


def planning(schedule=None, *, present=True):
    row = deepcopy(PRODUCT)
    if present:
        row["schedule"] = deepcopy(schedule)
    return {"documents": {"recurring_items": {"items": [row]}}}


def tree_bytes(root):
    return {str(p.relative_to(root)): (stat.S_IMODE(p.lstat().st_mode), p.read_bytes())
            for p in root.rglob("*") if p.is_file()}


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 17, 12, tzinfo=ZoneInfo("Europe/Oslo")).astimezone(tz)


class LegacyDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.valid = planning({"unit": "weeks", "every": 2, "anchor": "2026-W45"})

    def test_existing_household_and_empty_directory_are_preserved_exactly(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                destination = self.root / ("populated" if populated else "empty")
                destination.mkdir(mode=0o750)
                if populated:
                    (destination / "state.json").write_bytes(b'{"version":999,"pending_checkout":"KEEP"}\n')
                    (destination / "state.lock").write_bytes(b"existing lock")
                    (destination / "state-v5.backup.json").write_bytes(b"preserved backup")
                before = tree_bytes(destination)
                mode = stat.S_IMODE(destination.stat().st_mode)
                with mock.patch("core.StateStore", side_effect=AssertionError("must not construct a store")), \
                        self.assertRaisesRegex(HouseholdError, "already exists"):
                    legacy.write_migration(destination, CONFIG, self.valid)
                self.assertEqual(tree_bytes(destination), before)
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), mode)

    def test_existing_and_dangling_symlinks_are_rejected_without_touching_targets(self):
        target = self.root / "target"
        target.mkdir(mode=0o750)
        mode = stat.S_IMODE(target.stat().st_mode)
        (target / "state.json").write_bytes(b"PRIVATE SYNTHETIC TARGET")
        before = tree_bytes(target)
        self.output.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(HouseholdError, "already exists"):
            legacy.write_migration(self.output, CONFIG, self.valid)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(tree_bytes(target), before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
        dangling = self.root / "dangling"
        missing = self.root / "missing"
        dangling.symlink_to(missing, target_is_directory=True)
        with self.assertRaisesRegex(HouseholdError, "already exists"):
            legacy.write_migration(dangling, CONFIG, self.valid)
        self.assertTrue(dangling.is_symlink())
        self.assertFalse(missing.exists())

    def test_competing_imports_can_claim_the_output_only_once(self):
        barrier = threading.Barrier(2)
        def attempt():
            barrier.wait(timeout=5)
            try:
                legacy.write_migration(self.output, CONFIG, self.valid)
            except HouseholdError as exc:
                return str(exc)
            return "created"
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(lambda _: attempt(), range(2)))
        self.assertEqual(outcomes.count("created"), 1)
        self.assertTrue(any("already exists" in outcome for outcome in outcomes))
        self.assertEqual(json.loads((self.output / "state.json").read_text())["household"], CONFIG["household"])
        self.assertEqual(list(self.output.iterdir()), [self.output / "state.json"])

    def test_failed_fresh_output_is_retained_and_rejects_replay(self):
        def interrupted(path, result):
            path.write_bytes(b"partial synthetic write")
            raise OSError("synthetic storage failure")
        with mock.patch.object(legacy, "_atomic_json", side_effect=interrupted), \
                self.assertRaisesRegex(HouseholdError, "retain the fresh output directory for recovery"):
            legacy.write_migration(self.output, CONFIG, self.valid)
        self.assertTrue(self.output.is_dir())
        self.assertEqual((self.output / "state.json").read_bytes(), b"partial synthetic write")
        with self.assertRaisesRegex(HouseholdError, "already exists"):
            legacy.write_migration(self.output, CONFIG, self.valid)
        self.assertEqual((self.output / "state.json").read_bytes(), b"partial synthetic write")

    def test_invalid_or_missing_recurring_schedule_rejects_before_creating_output(self):
        cases = [planning(present=False), planning(None), planning({}),
                 planning({"unit": "weeks", "anchor": "2026-W45"}),
                 planning({"every": 1, "anchor": "2026-W45"}),
                 planning({"unit": "weeks", "every": 0}), planning({"unit": "weeks", "every": True}),
                 planning({"unit": "weeks", "every": "2"}), planning({"unit": "years", "every": 1}),
                 planning({"unit": "weeks", "every": 1, "anchor": "2026-W99"}),
                 planning({"unit": "months", "every": 1, "anchor": "0000-01"}),
                 planning({"unit": "days", "every": 1, "anchor": "2026-02-30"}),
                 planning({"unit": "days", "every": 1, "anchor": "2026-11-17", "extra": "unknown"})]
        for index, source in enumerate(cases):
            with self.subTest(index=index):
                before = deepcopy(source)
                with self.assertRaisesRegex(HouseholdError, r"recurring_items\[0\].schedule is missing or invalid"):
                    legacy.write_migration(self.output, CONFIG, source)
                self.assertFalse(self.output.exists())
                self.assertEqual(source, before)

    def test_explicit_schedules_are_normalized_without_mutating_inputs_and_reopen(self):
        schedules = [{"unit": "weeks", "every": 1, "anchor": None},
                     {"unit": "days", "every": 21, "anchor": "20261117"},
                     {"unit": "weeks", "every": 2, "anchor": "2026-W45"},
                     {"unit": "months", "every": 3, "anchor": "2026-10"}]
        expected = ["2026-W47", "2026-11-17", "2026-W45", "2026-10"]
        for index, (schedule, anchor) in enumerate(zip(schedules, expected)):
            with self.subTest(schedule=schedule):
                source = planning(schedule)
                source["documents"]["favorites"] = {"items": [deepcopy(PRODUCT)]}
                delivery = deepcopy(DEFAULT_SCHEDULE["delivery"])
                delivery.pop("strategy", None)
                scheduler = {"schedules": [{"enabled": False, "timezone": "Europe/Oslo", "delivery": delivery}]}
                before = deepcopy((CONFIG, source, scheduler))
                destination = self.root / f"normalized-{index}"
                with mock.patch.object(legacy, "datetime", FixedDateTime):
                    result = legacy.write_migration(destination, CONFIG, source, scheduler)
                self.assertEqual((CONFIG, source, scheduler), before)
                self.assertEqual(result["recurring_items"][0]["schedule"]["anchor"], anchor)
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((destination / "state.json").stat().st_mode), 0o600)
                reopened = StateStore(destination, CONFIG).read()
                self.assertEqual(reopened["recurring_items"], result["recurring_items"])
                self.assertEqual(reopened["product_favorites"], result["product_favorites"])
                self.assertIsNone(reopened["menu"])
                self.assertIsNone(reopened["pending_checkout"])

    def test_cli_reports_missing_schedule_without_creating_output(self):
        config = self.root / "config.json"
        old = self.root / "planning.json"
        config.write_text(json.dumps(CONFIG))
        old.write_text(json.dumps(planning(present=False)))
        completed = subprocess.run([sys.executable, "-B", str(ROOT / "migrate.py"),
                                    "--config", str(config), "--old-planning", str(old),
                                    "--output-directory", str(self.output)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("explicit positive integer every", completed.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
