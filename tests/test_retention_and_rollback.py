"""Disposable runtime transitions and exact, offline cleanup."""
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import install
from core import StateStore
import runtime_retention as retention
import runtime_rollback as reversal


class MaintenanceTransitionTests(unittest.TestCase):
    def setUp(self):
        from test_program_preparation import ProgramPreparationTests
        self.fixture = ProgramPreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home, self.meta, self.code = self.fixture.home, self.fixture.meta, self.fixture.code
        self.prior = self.code / ("release-" + "b" * 32)
        shutil.copytree(self.fixture.release, self.prior, symlinks=True)
        self.meta["release"] = str(self.prior)
        (self.code / "current").symlink_to(self.prior.name)
        install.write_json(self.home / "runtime.json", self.meta)

    def update(self):
        with patch.object(install, "write_unit"):
            install.publish(json.loads(json.dumps(self.meta)), self.home / "runtime.json", self.home, None,
                            prepared_release=self.fixture.release)
        return json.loads((self.home / "runtime.json").read_text())

    def test_changed_candidate_after_migration_is_never_selected(self):
        migrate = install.migrate
        def changed(release, meta):
            migrate(release, meta)
            (release / "staged_change.py").write_text("# inert post-migration change")
        with patch.object(install, "migrate", changed):
            with self.assertRaisesRegex(RuntimeError, "bytes changed"):
                self.update()
        self.assertEqual((self.code / "current").resolve(), self.prior.resolve())
        self.assertTrue((self.home / "pending-install.json").is_file())
        self.assertTrue((self.home / "maintenance.json").is_file())

    def test_rollback_pre_use_is_idempotent_and_does_not_start_service(self):
        meta = self.update()
        with patch.object(install, "write_unit"), patch.object(install, "lifecycle", side_effect=AssertionError("started")):
            self.assertEqual(reversal.rollback(self.home, meta)["status"], "rolled_back")
            self.assertEqual((self.code / "current").resolve(), self.prior.resolve())
            self.assertEqual(reversal.rollback(self.home, self.meta)["status"], "rolled_back")

    def test_previous_release_cannot_gain_a_package_after_rollback_was_armed(self):
        meta = self.update()
        unexpected = self.prior / "unexpected_package"
        unexpected.mkdir()
        (unexpected / "__init__.py").write_text("# inert fixture")
        with self.assertRaisesRegex(RuntimeError, "unexpected release entry"):
            reversal.rollback(self.home, meta)
        self.assertEqual((self.code / "current").resolve(), self.fixture.release.resolve())

    def test_even_an_empty_start_or_changed_state_refuses_rollback(self):
        meta = self.update()
        state = self.fixture.state.path
        before = state.read_bytes()
        state.write_bytes(before + b" ")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            reversal.rollback(self.home, meta)
        state.write_bytes(before)
        StateStore(state.parent, self.fixture.state.config)
        with self.assertRaisesRegex(RuntimeError, "may have started"):
            reversal.rollback(self.home, meta)
        self.assertEqual((self.code / "current").resolve(), self.fixture.release.resolve())

    def test_rollback_preserves_prior_journals_after_partial_restore_and_retry(self):
        previous_state = json.loads(self.fixture.state.path.read_text())
        previous_state["version"] = 13
        install.write_json(self.fixture.state.path, previous_state)
        prior_bytes = self.fixture.state.path.read_bytes()
        meta = self.update()
        with patch.object(install, "write_unit", side_effect=RuntimeError("interrupted switch")):
            with self.assertRaisesRegex(RuntimeError, "interrupted switch"):
                reversal.rollback(self.home, meta)
        self.assertEqual(self.fixture.state.path.read_bytes(), prior_bytes)
        with self.assertRaisesRegex(RuntimeError, "rollback is in progress"):
            StateStore(self.fixture.state.path.parent, self.fixture.state.config)
        with patch.object(install, "write_unit"):
            self.assertEqual(reversal.rollback(self.home, meta)["status"], "rolled_back")

    def test_interrupted_migrated_update_keeps_the_original_preimage_and_candidate(self):
        state = self.fixture.state.path
        self.meta['home'] = str(self.home.resolve())
        install.write_json(self.home/'runtime.json', self.meta)
        install.write_json(self.code/'owner.json', {'home':str(self.home.resolve())})
        original = json.loads(state.read_text())
        original['version'] = 13
        install.write_json(state, original)
        original_bytes = state.read_bytes()
        def interrupted(release, meta):
            migrate(release, meta)
            raise RuntimeError('parent interrupted after migration')
        migrate = install.migrate
        with patch.object(install, 'migrate', interrupted):
            with self.assertRaisesRegex(RuntimeError, 'parent interrupted'):
                self.update()
        pending = install.bounded_json(self.home / 'pending-install.json')['update_operation']
        self.assertEqual(pending['phase'], 'migrated')
        self.assertEqual(pending['previous_manifest'], self.meta)
        backup = Path(pending['backup'])
        self.assertEqual((backup / 'state/state.json').read_bytes(), original_bytes)
        self.assertNotEqual(state.read_bytes(), original_bytes)
        with patch.object(install,'stage_release',side_effect=AssertionError('new candidate')), \
             patch.object(install,'migrate',side_effect=AssertionError('repeated migration')), \
             patch.object(install,'backup',side_effect=AssertionError('new backup')), patch.object(install,'write_unit'):
            with patch.object(sys,'argv',['install.py','update','--home',str(self.home)]):
                install.main()
            current = install.bounded_json(self.home/'runtime.json')
            receipt = install.bounded_json(self.home/'rollback.json')
            self.assertEqual((receipt['guard']['operation'],receipt['backup']), (pending['id'],str(backup)))
            self.assertEqual(reversal.rollback(self.home, current)['status'], 'rolled_back')
        self.assertEqual(state.read_bytes(), original_bytes)
        self.assertEqual((self.code/'current').resolve(), self.prior.resolve())

    def test_completed_migration_with_later_work_can_only_recover_forward(self):
        with patch.object(install, 'write_unit', side_effect=RuntimeError('publication stopped')):
            with self.assertRaisesRegex(RuntimeError, 'publication stopped'):
                install.publish(json.loads(json.dumps(self.meta)), self.home/'runtime.json', self.home, None,
                                prepared_release=self.fixture.release)
        state = self.fixture.state.path
        changed = json.loads(state.read_text())
        changed['synthetic_local_record'] = 'retain'
        install.write_json(state, changed)
        with patch.object(install, 'write_unit'):
            install.publish(json.loads(json.dumps(self.meta)), self.home/'runtime.json', self.home, None)
        self.assertEqual(json.loads(state.read_text())['synthetic_local_record'], 'retain')
        with self.assertRaisesRegex(RuntimeError,'no pre-use rollback evidence'):
            reversal.rollback(self.home, install.bounded_json(self.home/'runtime.json'))
        self.assertEqual(len(list((self.home/'backups').iterdir())), 1)

    def test_legacy_pending_can_finish_forward_but_cannot_arm_rollback(self):
        install.write_json(self.home/'pending-install.json', self.meta)
        meta = self.update()
        self.assertEqual(meta['release'], str(self.fixture.release))
        self.assertFalse((self.home/'rollback.json').exists())
        with self.assertRaisesRegex(RuntimeError,'no pre-use rollback evidence'):
            reversal.rollback(self.home, meta)

    def test_partial_preimage_copy_resumes_without_discarding_unrelated_bytes(self):
        source = self.home / 'before'; source.write_bytes(b'original journal')
        current = self.home / 'current'; current.write_bytes(b'migrated journal')
        temp = reversal.staged_path(current, 'synthetic')
        temp.write_bytes(b'original'); temp.chmod(0o600)
        reversal.restore_file(source, current, reversal.content_hash(source), 'synthetic')
        self.assertEqual(current.read_bytes(), source.read_bytes())
        temp.write_bytes(b'other operation'); temp.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, 'unrelated bytes'):
            reversal.restore_file(source, current, reversal.content_hash(source), 'synthetic')
        self.assertEqual(temp.read_bytes(), b'other operation')

    def test_unreviewed_install_history_and_modified_previous_runtime_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "no pre-use rollback evidence"):
            reversal.rollback(self.home, self.meta)
        meta = self.update()
        (self.prior / "core.py").write_text("# replaced synthetic prior bytes")
        with self.assertRaisesRegex(RuntimeError, "previous program bytes changed"):
            reversal.rollback(self.home, meta)

    def test_health_binds_start_and_attach_to_the_complete_selected_runtime(self):
        selected = install.selected_identity(self.meta)
        expected = {key:selected[key] for key in ('source_sha256','installed_runtime_sha256')}
        with patch.object(install, 'health', return_value={**expected,'email_work_pending':False}):
            self.assertTrue(install.verify_serving(self.meta, selected))
            status = install.doctor(self.home)
            self.assertIs(status['serving_selected_runtime'], True)
            self.assertIs(status['email_work_pending'], False)
        with patch.object(install, 'health', return_value={**expected,'source_sha256':'0'*64}):
            with self.assertRaisesRegex(RuntimeError, 'serving process differs'):
                install.verify_serving(self.meta, selected)
        with patch.object(install, 'health', side_effect=ConnectionRefusedError):
            self.assertEqual(install.doctor(self.home)['email_work_pending'], 'unknown')

    def test_changed_prepared_dependency_or_interpreter_is_rejected(self):
        directory = self.fixture.release / "venv/lib/synthetic"
        directory.mkdir(parents=True)
        (directory / "module.py").write_text("# installed artifact")
        with self.assertRaisesRegex(RuntimeError, "bytes changed"):
            install.candidate_identity(self.fixture.release)

    def test_interrupted_local_import_stage_can_be_pruned_from_selected_release(self):
        stage = self.prior / ("recipe-pack-" + "a" * 32 + ".zip")
        stage.write_bytes(b"unused temporary ZIP")
        os.utime(stage, (0, 0))
        review = retention.public_preview(retention.inventory(self.home, self.meta, 1))
        self.assertEqual([item['kind'] for item in review['candidates']], ['failed_archive'])
        retention.apply(self.home, self.meta, 1, review['review'])
        self.assertFalse(stage.exists())
        self.assertTrue((self.prior / "core.py").is_file())

    def test_failed_cleanup_resumes_only_exact_remaining_entries(self):
        old = self.home / "recipe-preparations/.prepare-12345678"
        old.mkdir(parents=True)
        archive = old / ("recipe-pack-" + "1" * 32 + ".zip")
        archive.write_bytes(b"old stage")
        os.utime(old, (0, 0))
        digest = retention.public_preview(retention.inventory(self.home, self.meta, 1))["review"]
        original = retention._purge
        def interrupted(parent, name, rel, objects):
            if rel:
                original(parent, name, rel, objects)
                raise RuntimeError("interrupted")
            original(parent, name, rel, objects)
        with patch.object(retention, "_purge", interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                retention.apply(self.home, self.meta, 1, digest)
        self.assertFalse(old.exists())
        self.assertEqual(retention.apply(self.home, self.meta, 1, digest)["deleted"], 1)

    def test_real_backup_equivalence_across_descriptive_release_and_lifecycle(self):
        roots = self.home / 'backups'
        old, recent = (roots / ('before-' + token * 32) for token in 'ab')
        state = self.fixture.state.path.parent
        original = self.fixture.state.path.read_bytes()
        install.backup(self.meta, old)
        install.write_json(state / '.release-use.json', {'status':'possibly_used','operation':'f'*32,
                                                        'release':self.fixture.release.name})
        install.backup({**self.meta, 'release':str(self.fixture.release)}, recent)
        os.utime(old, (0, 0))
        preview = retention.public_preview(retention.inventory(self.home, self.meta, 1))
        self.assertEqual([r['id'] for r in preview['candidates']], [old.name])
        retention.apply(self.home, self.meta, 1, preview['review'])
        self.assertEqual((recent/'state/state.json').read_bytes(), original)
        self.assertEqual((recent/'state/recipes.sqlite3').read_bytes(), self.fixture.bank.path.read_bytes())
        self.assertTrue((state/'.release-use.json').exists())

    def test_backup_retention_keeps_unique_journals_and_exact_survivor(self):
        roots = self.home / "backups"
        for letter in "abc":
            path = roots / ("before-" + letter * 32)
            path.mkdir(parents=True)
            install.write_json(path / "backup.json", {"format": 1, "complete": True})
            (path / "state").mkdir()
            (path / "config.json").write_text("{}")
            (path / "state/state.json").write_text('{"pending":"original"}' if letter == "c" else '{}')
            os.utime(path, (0, 0))
        preview = retention.public_preview(retention.inventory(self.home, self.meta, 1))
        self.assertEqual([r['id'] for r in preview['candidates']], ["before-" + "a" * 32])
        self.assertEqual(retention.apply(self.home, self.meta, 1, preview['review'])["deleted"], 1)
        self.assertTrue((roots / ("before-" + "b" * 32)).is_dir())
        self.assertTrue((roots / ("before-" + "c" * 32) / "state/state.json").is_file())

    def test_retention_rechecks_bytes_and_protects_current_and_symlinks(self):
        root = Path(self.meta["paths"]["state"]) / ".recipe-pack-staging"; root.mkdir()
        old = root / ".incoming-abcd1234.zip"
        old.write_bytes(b"synthetic interrupted staging")
        os.utime(old, (0, 0))
        linked = root / ".incoming-other000.zip"
        linked.symlink_to(old)
        review = retention.public_preview(retention.inventory(self.home, self.meta, 2))
        self.assertEqual([row["id"] for row in review["candidates"]], [old.name])
        old.write_bytes(b"changed")
        os.utime(old, (0, 0))
        with self.assertRaisesRegex(RuntimeError, "changed"):
            retention.apply(self.home, self.meta, 2, review["review"])
        self.assertTrue(old.exists())
        digest = retention.public_preview(retention.inventory(self.home, self.meta, 2))["review"]
        self.assertEqual(retention.apply(self.home, self.meta, 2, digest)["deleted"], 1)
        self.assertTrue(retention.apply(self.home, self.meta, 2, digest)["replayed"])
        self.assertTrue(linked.is_symlink())
        self.assertFalse(old.exists())
