"""Durable visible-browser launches survive installer and adapter restarts."""
from argparse import Namespace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_prerequisites import browser_launch_settings
from install import configured_browser_launch, service_args
from meny import MenyClient
from oda_browser import OdaBrowser


class BrowserLaunchTests(unittest.TestCase):
    def test_installer_retains_mode_display_and_authority_on_update_and_run(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / 'authority'
            auth.touch()
            args = Namespace(browser_mode='headed', browser_display=':2', browser_xauthority=str(auth))
            settings = configured_browser_launch(args, {})
            meta = {'browser_launch': settings, 'code_root': '/program', 'paths': {}}
            retained = configured_browser_launch(Namespace(browser_mode=None, browser_display=None, browser_xauthority=None), meta)
            self.assertEqual(retained, settings)
            command = service_args(meta)
            self.assertEqual(command[-6:], ['--browser-mode', 'headed', '--browser-display', ':2', '--browser-xauthority', str(auth)])
            reset = configured_browser_launch(Namespace(browser_mode='headless', browser_display=None, browser_xauthority=None), meta)
            self.assertEqual(reset, {'mode': 'headless'})

    def test_cold_launch_and_relaunch_keep_visible_mode_for_both_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / 'authority'
            auth.touch()
            for cls, module in ((MenyClient, 'meny'), (OdaBrowser, 'oda_browser')):
                for mode in ('headed', 'headless'):
                    for _restart in range(2):
                        with self.subTest(provider=module, mode=mode, restart=_restart):
                            client = cls(instance='synthetic', binary='/bin/agent-browser', executable='/bin/chrome',
                                         profile='/private/profile', home='/private/home', socket_directory='/private/socket',
                                         uid=os.getuid(), gid=os.getgid(), browser_mode=mode,
                                         browser_display=':2' if mode == 'headed' else None,
                                         browser_xauthority=str(auth) if mode == 'headed' else None)
                            completed = mock.Mock(returncode=0, stdout='{"success":true,"data":{}}')
                            with mock.patch(module + '.subprocess.run', return_value=completed) as run:
                                client._invoke('get', 'url')
                            self.assertEqual('--headed' in run.call_args.args[0], mode == 'headed')
                            env = run.call_args.kwargs['env']
                            self.assertEqual(env.get('DISPLAY'), ':2' if mode == 'headed' else None)
                            self.assertEqual(env.get('XAUTHORITY'), str(auth) if mode == 'headed' else None)

    def test_missing_display_fails_clearly_but_external_cdp_does_not_launch(self):
        with mock.patch('browser_prerequisites.sys.platform', 'linux'):
            with self.assertRaisesRegex(RuntimeError, 'requires --browser-display'):
                browser_launch_settings('headed')
            client = MenyClient(instance='synthetic', binary='/bin/agent-browser', executable='/bin/chrome',
                                profile='/private/profile', home='/private/home', socket_directory='/private/socket',
                                uid=os.getuid(), gid=os.getgid(), cdp='http://127.0.0.1:9224', browser_mode='headed')
        completed = mock.Mock(returncode=0, stdout='{"success":true,"data":{}}')
        with mock.patch('meny.subprocess.run', return_value=completed) as run:
            client._invoke('get', 'url')
        self.assertIn('--cdp', run.call_args.args[0])
        self.assertNotIn('--headed', run.call_args.args[0])
        self.assertNotIn('--profile', run.call_args.args[0])
        self.assertNotIn('DISPLAY', run.call_args.kwargs['env'])


if __name__ == '__main__':
    unittest.main()
