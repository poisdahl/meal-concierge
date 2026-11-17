"""Running program preparation and stopped application retain exact candidate identity."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_identity
import install
from core import StateStore
from recipes import RecipeStore


class ProgramPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mc-program-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'; self.home.mkdir()
        self.code = Path(self.temp.name) / 'code'; self.code.mkdir()
        self.release = self.code / ('release-' + 'a' * 32); self.release.mkdir()
        (self.release / 'venv/bin').mkdir(parents=True)
        (self.release / 'venv/bin/python').symlink_to(sys.executable)
        for path in ROOT.glob('*.py'):
            shutil.copyfile(path, self.release / path.name)
        shutil.copyfile(ROOT / 'runtime-requirements.txt', self.release / 'runtime-requirements.txt')
        shutil.copytree(ROOT / 'skill', self.release / 'skill')
        install.write_json(self.release / build_identity.MANIFEST,
                           build_identity.create(self.release, self.release, install.PYTHON))
        settings = {'household': 'Synthetic program preparation', 'provider': 'none'}
        install.write_json(self.home / 'config.json', settings)
        self.state = StateStore(self.home / 'state', settings)
        self.bank = RecipeStore(self.home / 'state/recipes.sqlite3', settings['household'])
        self.bank.search()
        self.meta = {'home': str(self.home), 'manager': 'external', 'name': 'synthetic-program',
                     'code_root': str(self.code), 'release': str(self.code / 'old'), 'runtime_path': '/usr/bin:/bin',
                     'paths': {'config': str(self.home / 'config.json'), 'state': str(self.home / 'state'),
                               'socket': str(self.home / 'service.sock')}}
        install.write_json(self.home / 'runtime.json', self.meta)
        install.write_json(self.code / 'owner.json', {'home': str(self.home)})

    def prepare(self):
        with patch.object(install, 'stage_release', return_value=self.release):
            return install.prepare_program(self.home, self.meta)

    def test_cli_preparation_while_external_run_holds_lifecycle_and_service_ownership(self):
        home = self.home.resolve()
        meta = {**self.meta, 'home': str(home)}
        install.write_json(self.home / 'runtime.json', meta)
        install.write_json(self.code / 'owner.json', {'home': str(home)})
        with install.file_lock(home / '.installer.lock'), install.data_ownership(meta), \
             patch.object(install, 'stage_release', return_value=self.release), \
             patch.object(sys, 'argv', ['install.py', 'prepare-program', '--home', str(home)]), \
             patch('sys.stdout', new_callable=io.StringIO) as report:
            install.main()
        self.assertEqual(json.loads(report.getvalue())['status'], 'prepared')

    def test_prepare_while_service_owns_data_and_offline_apply_never_downloads(self):
        before = {p.name: p.read_bytes() for p in (self.home / 'state/state.json', self.home / 'state/recipes.sqlite3')}
        with install.data_ownership(self.meta):
            prepared = self.prepare()
            self.assertFalse(prepared['service_stop_required'])
            with self.assertRaises(RuntimeError):
                install.publish(deepcopy(self.meta), self.home / 'runtime.json', self.home, None,
                                prepared_release=self.release)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.home / 'state/state.json', self.home / 'state/recipes.sqlite3')})
        candidate = install.read_program_preparation(self.home, self.meta, prepared['prepared_program'])
        with patch.object(install, 'stage_release', side_effect=AssertionError('offline download')), \
             patch.object(install, 'write_unit'):
            install.publish(deepcopy(self.meta), self.home / 'runtime.json', self.home, None,
                            prepared_release=candidate)
        applied = json.loads((self.home / 'runtime.json').read_text())
        self.assertEqual(applied['release'], str(self.release))
        self.assertEqual((self.code / 'current').resolve(), self.release.resolve())
        self.assertFalse((self.home / 'maintenance.json').exists())
        self.assertTrue(list((self.home / 'backups').glob('before-*/backup.json')))

    def test_changed_bytes_and_changed_installation_reject_preparation(self):
        prepared = self.prepare()['prepared_program']
        changed = deepcopy(self.meta); changed['release'] = '/different/runtime'
        with self.assertRaisesRegex(RuntimeError, 'changed installation'):
            install.read_program_preparation(self.home, changed, prepared)
        (self.release / 'core.py').write_text('# changed candidate')
        with self.assertRaisesRegex(RuntimeError, 'bytes changed'):
            install.read_program_preparation(self.home, self.meta, prepared)
        self.assertEqual(json.loads((self.home / 'runtime.json').read_text()), self.meta)

    def test_cli_applies_the_exact_prepared_program(self):
        prepared = self.prepare()['prepared_program']
        argv = ['install.py', 'update', '--home', str(self.home), '--prepared-program', prepared]
        # Normalize the fixture's /tmp alias exactly as main normalizes --home.
        meta = deepcopy(self.meta)
        meta['home'] = str(self.home.resolve())
        install.write_json(self.code / 'owner.json', {'home': str(self.home.resolve())})
        install.write_json(self.home / 'runtime.json', meta)
        # Changing home spelling changes the baseline; prepare against that exact manifest.
        with patch.object(install, 'stage_release', return_value=self.release):
            prepared = install.prepare_program(self.home.resolve(), meta)['prepared_program']
        argv[-1] = prepared
        with patch.object(sys, 'argv', argv), patch.object(install, 'write_unit'), \
             patch.object(install, 'stage_release', side_effect=AssertionError('prepared update downloaded')):
            with patch('sys.stdout', new_callable=io.StringIO):
                install.main()
        self.assertEqual(json.loads((self.home / 'runtime.json').read_text())['release'], str(self.release))

    def test_failed_compatibility_check_creates_no_authoritative_markers(self):
        state = json.loads(self.state.path.read_text())
        state['version'] = 999
        install.write_json(self.state.path, state)
        with self.assertRaisesRegex(RuntimeError, 'compatibility preflight failed'):
            self.prepare()
        for name in ('maintenance.json', 'pending-install.json', 'program-preparations'):
            self.assertFalse((self.home / name).exists())
        self.assertFalse((self.code / 'current').exists())

    def test_prepare_rejects_a_different_code_owner(self):
        install.write_json(self.code / 'owner.json', {'home': '/different/installation'})
        with patch.object(install, 'stage_release') as stage, self.assertRaisesRegex(RuntimeError, 'different installation'):
            install.prepare_program(self.home, self.meta)
        stage.assert_not_called()

    def test_missing_household_state_cannot_qualify_as_a_new_household(self):
        self.state.path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'compatibility preflight failed'):
            self.prepare()
        self.assertFalse(self.state.path.exists())
        self.assertFalse((self.home / 'program-preparations').exists())
        self.assertFalse((self.home / 'maintenance.json').exists())

    def test_preflight_accepts_a_bank_with_managed_cover_references_without_copying_media(self):
        from PIL import Image
        content = io.BytesIO()
        Image.new('RGB', (4, 4), 'green').save(content, format='PNG')
        cover = self.bank.assets.import_bytes(content.getvalue())
        self.bank.save({'schema_version': 2, 'name': 'Synthetic covered lentils', 'portions': 2,
            'ingredients': ['200 g lentils'], 'steps': ['Simmer.'],
            'source': {'kind': 'user', 'relationship': 'user_supplied'}, 'rights': {'storage': 'full'},
            'image': {'asset_id': cover, 'credit': 'Synthetic fixture'}})
        before = self.bank.assets.read(cover)
        self.assertEqual(self.prepare()['status'], 'prepared')
        self.assertEqual(self.bank.assets.read(cover), before)
        self.assertFalse(list(self.home.glob('.program-preflight-*')))

    def test_doctor_uses_only_allowlisted_technical_metadata(self):
        (self.code / 'current').symlink_to(self.release.name)
        self.meta['release'] = str(self.release)
        self.meta['sensitive'] = 'synthetic private marker'
        install.write_json(self.home / 'runtime.json', self.meta)
        with patch.object(install, 'health', side_effect=ConnectionRefusedError):
            result = install.doctor(self.home)
        self.assertEqual(result['socket_health'], 'unavailable')
        self.assertTrue(result['current_matches_release'])
        self.assertNotIn('synthetic private marker', json.dumps(result))
        self.assertNotIn('household', json.dumps(result))
        self.assertFalse(result['changed'])

    def test_retention_is_explicit_dry_run_and_protects_current_and_irregular_entries(self):
        root = self.home / 'recipe-preparations'; root.mkdir()
        baseline = install.collection_generation(self.meta)
        for letter, release in [('a', '/old/runtime'), ('b', self.meta['release'])]:
            directory = root / (letter * 64); directory.mkdir()
            (directory / 'archive.zip').write_bytes(b'Synthetic archive')
            install.write_json(directory / 'receipt.json', {'format': 1, 'expected': {'sha256': letter * 64},
                'prepared_at': (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
                'release': release, 'state': str((self.home / 'state').resolve()), 'baseline': baseline})
        (root / ('c' * 64)).symlink_to(root / ('a' * 64), target_is_directory=True)
        with patch.object(install.os, 'unlink', side_effect=AssertionError('preview deleted bytes')):
            result = install.preparation_retention_preview(self.home, self.meta)
        self.assertEqual([row['prepared'] for row in result['candidates']], ['a' * 64])
        self.assertEqual((result['deleted'], result['protected']), (0, 2))
        install.write_json(self.home / 'maintenance.json', {'reason': 'synthetic interruption'})
        with self.assertRaisesRegex(RuntimeError, 'interrupted maintenance'):
            install.preparation_retention_preview(self.home, self.meta)


if __name__ == '__main__':
    unittest.main()
