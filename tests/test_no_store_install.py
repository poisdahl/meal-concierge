"""Browser-free installation and exact runtime ownership boundaries."""
from __future__ import annotations

import io
import json
import os
from argparse import Namespace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
import runtime_ownership as runtime
import core


class NoStoreInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix='mc-none-', dir='/tmp'))
        self.root = Path(self.temp).resolve()
        self.home = self.root / 'home'
        self.code = self.root / 'code'

    def install_arguments(self):
        return ['install.py', 'install', '--manager', 'external', '--home', str(self.home),
                '--code-root', str(self.code), '--provider', 'none', '--household', 'Synthetic meals']

    def test_install_and_update_need_only_household_paths(self):
        with patch.object(sys, 'argv', self.install_arguments()), \
             patch.object(install, 'discover_browser_paths', side_effect=AssertionError('browser discovery')), \
             patch.object(install, 'publish') as publish:
            install.main()
        meta, _manifest, _home, settings = publish.call_args.args
        self.assertEqual(settings['provider'], 'none')
        self.assertEqual(set(meta['paths']), {'config', 'state', 'socket'})
        self.assertNotIn('browser_launch', meta)
        arguments = install.service_args(meta)
        self.assertFalse(any(value.startswith('--browser') or value == '--tokens' for value in arguments))

        install.write_json(self.home / 'config.json', settings)
        install.write_json(self.home / 'runtime.json', meta)
        with patch.object(sys, 'argv', ['install.py', 'update', '--home', str(self.home)]), \
             patch.object(install, 'discover_browser_paths', side_effect=AssertionError('browser discovery')), \
             patch.object(runtime, 'assert_no_legacy_service'), \
             patch.object(install, 'publish') as update:
            install.main()
        self.assertEqual(update.call_args.args[0]['paths'], meta['paths'])
        self.assertFalse((self.home / 'browser').exists())
        self.assertFalse((self.home / 'tokens').exists())

    def test_browser_check_reports_not_required(self):
        self.home.mkdir()
        config = self.home / 'config.json'
        install.write_json(config, {'household': 'Synthetic meals', 'provider': 'none'})
        install.write_json(self.home / 'runtime.json', {'paths': {'config': str(config)}})
        output = io.StringIO()
        with patch.object(sys, 'argv', ['install.py', 'check-browser', '--home', str(self.home)]), \
             patch.object(install, 'discover_browser_paths', side_effect=AssertionError('browser discovery')), \
             patch.object(sys, 'stdout', output):
            install.main()
        self.assertEqual(json.loads(output.getvalue().splitlines()[0]),
                         {'provider': 'none', 'browser_required': False})

    def test_legacy_default_and_connected_browser_validation_are_preserved(self):
        self.assertEqual(install.configured_provider({}), 'oda')
        self.assertEqual(install.configured_provider({'provider': 'MaThEm'}), 'mathem')
        with self.assertRaisesRegex(RuntimeError, 'provider must be'):
            install.configured_provider({'provider': 'unknown'})
        for provider in ('oda', 'meny', 'mathem'):
            with self.subTest(provider=provider), \
                 patch.object(sys, 'argv', [provider if value == 'none' else value for value in self.install_arguments()]), \
                 patch.object(install, 'discover_browser_paths', side_effect=RuntimeError('missing synthetic browser')), \
                 patch.object(install, 'publish') as publish:
                with self.assertRaisesRegex(RuntimeError, 'browser prerequisites'):
                    install.main()
                publish.assert_not_called()


class StateOnlyOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix='mc-none-owner-', dir='/tmp'))
        self.root = Path(self.temp).resolve()

    def process(self, *, state='other-state', config=None, profile=None):
        words = ['python', 'service.py', '--state', state, '--config', config or 'config.json']
        if profile is not None:
            words += ['--browser-profile', profile]
        process_id = str(os.getpid() + 100000)
        self.enterContext(patch.object(runtime.subprocess, 'run', return_value=subprocess.CompletedProcess(
            [], 0, stdout=process_id + ' python service.py --state fixture --config fixture\n')))
        self.enterContext(patch.object(runtime, 'process_arguments', return_value=(words, self.root)))

    def test_state_only_ownership_keeps_state_and_alias_locks(self):
        with patch.object(runtime, 'assert_no_legacy_service'):
            with runtime.ownership(self.root / 'state', None):
                alias = self.root / 'alias'
                alias.symlink_to(self.root / 'state')
                with self.assertRaisesRegex(RuntimeError, 'target already owned'):
                    with runtime.ownership(alias, None):
                        self.fail('duplicate acquired state')
                with runtime.ownership(self.root / 'other-state', None):
                    pass
        self.assertEqual({path.name for path in self.root.iterdir()}, {'state', 'other-state', 'alias'})

    def test_state_only_checks_legacy_state_even_without_browser_arguments(self):
        self.process(state='state')
        with self.assertRaisesRegex(RuntimeError, 'owns this target'):
            runtime.assert_no_legacy_service(self.root / 'state', None)

    def test_state_only_ignores_unrelated_legacy_implicit_browser(self):
        self.process()
        runtime.assert_no_legacy_service(self.root / 'state', None)

    def test_connected_owner_accepts_unrelated_explicit_no_store_process(self):
        config = self.root / 'config with spaces.json'
        config.write_text(json.dumps({'provider': 'none'}))
        self.process(config=config.name)
        runtime.assert_no_legacy_service(self.root / 'state', self.root / 'browser')

    def test_no_store_config_does_not_exempt_same_state(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps({'provider': 'none'}))
        self.process(state='state')
        with self.assertRaisesRegex(RuntimeError, 'owns this target'):
            runtime.assert_no_legacy_service(self.root / 'state', self.root / 'browser')

    def test_connected_owner_preserves_implicit_browser_guard(self):
        self.process()
        config = self.root / 'config.json'
        for text in ('{}', '{"provider":"oda"}', '{"provider":"meny"}', 'invalid', '[]'):
            with self.subTest(config=text):
                config.write_text(text)
                with self.assertRaisesRegex(RuntimeError, 'implicit browser profile'):
                    runtime.assert_no_legacy_service(self.root / 'state', self.root / 'browser')
        config.unlink()
        with self.assertRaisesRegex(RuntimeError, 'implicit browser profile'):
            runtime.assert_no_legacy_service(self.root / 'state', self.root / 'browser')

    def test_connected_owner_preserves_browser_target_overlap(self):
        self.process(profile='browser')
        with self.assertRaisesRegex(RuntimeError, 'owns this target'):
            runtime.assert_no_legacy_service(self.root / 'state', self.root / 'browser')


class ConnectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix='mc-connect-', dir='/tmp'))
        self.root = Path(self.temp).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        state = self.home / 'state'
        state.mkdir()
        self.original = {'provider': 'none', 'menu': {'name': 'Saved meal'}, 'preferences': ['vegetables']}
        install.write_json(state / 'state.json', self.original)
        (state / 'recipes.sqlite3').write_bytes(b'synthetic unchanged recipe bank')
        self.config = {'household': 'Synthetic meals', 'instance': 'fixture', 'provider': 'none'}
        install.write_json(self.home / 'config.json', self.config)
        self.meta = {'format': 1, 'home': str(self.home), 'code_root': str(self.root / 'code'),
                     'name': 'synthetic-none', 'manager': 'external', 'unit': None,
                     'runtime_path': os.defpath, 'release': str(self.root / 'code/release'),
                     'paths': {'config': str(self.home / 'config.json'), 'state': str(state),
                               'socket': str(self.home / 'run/service.sock'),
                               'maintenance': str(self.home / 'maintenance.json')}}
        install.write_json(self.home / 'runtime.json', self.meta)
        self.args = Namespace(provider='oda', config=None, state=None, socket=None,
                              tokens=None, browser_home=None, browser_profile=None,
                              browser_socket_directory=None, agent_browser=None, browser_executable=None,
                              browser_mode=None, browser_display=None, browser_xauthority=None)
        self.enterContext(patch.object(runtime, 'assert_no_legacy_service'))
        self.browser = self.enterContext(patch.object(install, 'browser_paths', return_value={
            'browser_binary': '/synthetic/agent-browser', 'browser_executable': '/synthetic/chromium'}))
        self.enterContext(patch.object(sys, 'stdout', io.StringIO()))

        def transition(state, provider):
            if state['provider'] != 'none':
                raise RuntimeError('state already connected')
            return {**state, 'provider': provider, 'checkout_payment': {'method': 'saved_card'}}
        # The core owns the pure state migration and its financial-history tests.
        self.transition = self.enterContext(patch.object(core, 'first_store_state', side_effect=transition, create=True))

    def connect(self):
        meta = json.loads((self.home / 'runtime.json').read_text())
        install.connect_store(meta, self.args, self.home)

    def test_first_connection_preserves_menu_bank_and_original_backup(self):
        self.connect()
        state = json.loads((self.home / 'state/state.json').read_text())
        self.assertEqual(state['provider'], 'oda')
        self.assertEqual(state['menu'], self.original['menu'])
        self.assertEqual((self.home / 'state/recipes.sqlite3').read_bytes(), b'synthetic unchanged recipe bank')
        self.assertEqual(json.loads((self.home / 'config.json').read_text())['provider'], 'oda')
        updated = json.loads((self.home / 'runtime.json').read_text())
        self.assertIn('browser_profile', updated['paths'])
        self.assertFalse((self.home / 'maintenance.json').exists())
        self.assertFalse((self.home / 'pending-store.json').exists())
        backups = list((self.home / 'backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads((backups[0] / 'state/state.json').read_text()), self.original)
        self.assertEqual(json.loads((backups[0] / 'config.json').read_text()), self.config)
        self.assertEqual(json.loads((backups[0] / 'runtime.json').read_text()), self.meta)
        self.transition.assert_called_once()

    def test_preflight_failure_and_established_store_leave_state_untouched(self):
        self.browser.side_effect = RuntimeError('missing browser')
        with self.assertRaisesRegex(RuntimeError, 'missing browser'):
            self.connect()
        self.assertEqual(json.loads((self.home / 'state/state.json').read_text()), self.original)
        self.assertFalse((self.home / 'backups').exists())
        self.browser.side_effect = None
        install.write_json(self.home / 'config.json', {**self.config, 'provider': 'mathem'})
        with self.assertRaisesRegex(RuntimeError, 'provider switching'):
            self.connect()
        self.assertEqual(json.loads((self.home / 'state/state.json').read_text()), self.original)

    def test_cli_connects_only_while_stopped_and_blocks_other_pending_actions(self):
        argv = ['install.py', 'connect-store', '--home', str(self.home), '--provider', 'oda']
        with runtime.ownership(self.home / 'state', None), patch.object(sys, 'argv', argv):
            with self.assertRaisesRegex(RuntimeError, 'target already owned'):
                install.main()
        self.assertFalse((self.home / 'backups').exists())
        self.interrupted_connection()
        with patch.object(sys, 'argv', ['install.py', 'update', '--home', str(self.home)]):
            with self.assertRaisesRegex(RuntimeError, 'resume connect-store'):
                install.main()
        with patch.object(sys, 'argv', argv):
            install.main()
        self.assertEqual(json.loads((self.home / 'config.json').read_text())['provider'], 'oda')

    def interrupted_connection(self):
        atomic = core._atomic_json
        def fail_config(path, value):
            if path == self.home / 'config.json':
                raise OSError('synthetic interrupted config publication')
            atomic(path, value)
        with patch.object(core, '_atomic_json', side_effect=fail_config):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.connect()
        self.assertTrue((self.home / 'maintenance.json').exists())
        self.assertTrue((self.home / 'pending-store.json').exists())
        self.assertEqual(json.loads((self.home / 'state/state.json').read_text())['provider'], 'oda')

    def test_retry_resumes_exact_target_after_partial_publication(self):
        self.interrupted_connection()
        self.connect()
        self.assertEqual(json.loads((self.home / 'config.json').read_text())['provider'], 'oda')
        self.assertEqual(len(list((self.home / 'backups').iterdir())), 1)
        self.transition.assert_called_once()
        self.assertFalse((self.home / 'maintenance.json').exists())

    def test_retry_recovers_runtime_replacement_with_lost_success(self):
        atomic = core._atomic_json
        def fail_after_runtime(path, value):
            atomic(path, value)
            if path == self.home / 'runtime.json':
                raise OSError('synthetic lost runtime success')
        with patch.object(core, '_atomic_json', side_effect=fail_after_runtime):
            with self.assertRaisesRegex(OSError, 'lost runtime success'):
                self.connect()
        self.connect()
        self.assertFalse((self.home / 'maintenance.json').exists())
        self.transition.assert_called_once()

    def test_retry_rejects_different_provider_and_unrelated_state_edit(self):
        self.interrupted_connection()
        self.args.provider = 'meny'
        with self.assertRaisesRegex(RuntimeError, 'original provider'):
            self.connect()
        self.args.provider = 'oda'
        changed = {**json.loads((self.home / 'state/state.json').read_text()), 'unrelated': 'keep'}
        install.write_json(self.home / 'state/state.json', changed)
        with self.assertRaisesRegex(RuntimeError, 'files changed'):
            self.connect()
        self.assertEqual(json.loads((self.home / 'state/state.json').read_text()), changed)
        self.assertTrue((self.home / 'maintenance.json').exists())

    def test_maintenance_retains_recovery_after_pending_marker_cleanup(self):
        self.interrupted_connection()
        (self.home / 'pending-store.json').unlink()
        self.connect()
        self.assertFalse((self.home / 'maintenance.json').exists())
        self.assertEqual(json.loads((self.home / 'config.json').read_text())['provider'], 'oda')

    def test_unrelated_maintenance_is_not_replaced(self):
        marker = {'candidate': 'other-release', 'reason': 'offline update'}
        install.write_json(self.home / 'maintenance.json', marker)
        with self.assertRaisesRegex(RuntimeError, 'existing maintenance'):
            self.connect()
        self.assertEqual(json.loads((self.home / 'maintenance.json').read_text()), marker)
        self.assertFalse((self.home / 'pending-store.json').exists())

    def test_retry_rejects_altered_staged_documents(self):
        self.interrupted_connection()
        journal = json.loads((self.home / 'pending-store.json').read_text())
        path = Path(journal['backup']) / 'connected-state.json'
        staged = json.loads(path.read_text())
        install.write_json(path, {**staged, 'unexpected': True})
        with self.assertRaisesRegex(RuntimeError, 'connection files changed'):
            self.connect()
        self.assertEqual(json.loads((self.home / 'config.json').read_text()), self.config)


if __name__ == '__main__':
    unittest.main()
