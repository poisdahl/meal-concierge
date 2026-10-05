"""Retail browser transport failures stay bounded and do not replay actions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError
from meny import MenyClient, normalized_selected_meny_slot
from oda_browser import OdaBrowser, MathemBrowser
from test_meny_provider_diagnostics import fake_adapter


def browser_for(cls, binary):
    arguments = dict(instance='synthetic-audit', binary=binary, executable='/synthetic/chrome',
                     profile='/synthetic/profile', home='/synthetic/home',
                     socket_directory='/synthetic/socket', uid=os.getuid(), gid=os.getgid())
    if cls is MathemBrowser:
        arguments['provider_client'] = None
    return cls(**arguments)


class RetailBrowserTransportTests(unittest.TestCase):
    def test_nonobject_adapter_responses_are_controlled_failures_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            for cls in (OdaBrowser, MathemBrowser):
                for value in (None, [], 'synthetic-secret', 7, True):
                    for check in (True, False):
                        with self.subTest(provider=cls.__name__, value=value, check=check):
                            log = Path(directory) / 'calls'
                            log.write_text('')
                            binary = fake_adapter(directory, stdout=json.dumps(value), exit_code=0,
                                                  invocation_log=log)
                            browser = browser_for(cls, binary)
                            if check:
                                with self.assertRaisesRegex(HouseholdError, 'browser rejected the operation') as error:
                                    browser._invoke('eval', '--stdin', stdin='synthetic()', check=check)
                                self.assertNotIn('synthetic-secret', str(error.exception))
                            else:
                                self.assertEqual(browser._invoke('close', check=False), {})
                            self.assertEqual(log.read_text().splitlines(), ['invoked'])

    def test_native_launch_keeps_browser_identity_without_python_after_fork(self):
        for cls in (OdaBrowser, MathemBrowser):
            browser = browser_for(cls, '/synthetic/adapter')
            browser.uid, browser.gid = 10001, 10002
            for euid in (0, 1000):
                with self.subTest(provider=cls.__name__, euid=euid):
                    completed = subprocess.CompletedProcess([], 0, stdout='{"success":true,"data":{}}')
                    with mock.patch('oda_browser.os.geteuid', return_value=euid), \
                         mock.patch('oda_browser.subprocess.run', return_value=completed) as run:
                        browser._invoke('get', 'url')
                    kwargs = run.call_args.kwargs
                    self.assertNotIn('preexec_fn', kwargs)
                    if euid == 0:
                        self.assertEqual((kwargs['user'], kwargs['group'], kwargs['extra_groups']),
                                         (10001, 10002, []))
                    else:
                        self.assertTrue({'user', 'group', 'extra_groups'}.isdisjoint(kwargs))

    def test_threaded_success_and_timeout_do_not_retry_adapter(self):
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=1) as workers:
            for cls in (OdaBrowser, MathemBrowser):
                with self.subTest(provider=cls.__name__):
                    log = Path(directory) / 'calls'
                    log.write_text('')
                    binary = fake_adapter(directory, stdout='{"success":true,"data":{"ready":true}}',
                                          exit_code=0, invocation_log=log)
                    browser = browser_for(cls, binary)
                    self.assertEqual(workers.submit(browser._invoke, 'get', 'url').result(timeout=5), {'ready': True})
                    self.assertEqual(log.read_text().splitlines(), ['invoked'])
                    log.write_text('')
                    browser.binary = fake_adapter(directory, delay=2, invocation_log=log)
                    browser._checkout_deadline = time.monotonic() + 0.3
                    with self.assertRaisesRegex(HouseholdError, 'browser is unavailable'):
                        workers.submit(browser._invoke, 'eval', '--stdin', stdin='synthetic()').result(timeout=5)
                    self.assertEqual(log.read_text().splitlines(), ['invoked'])

    def test_meny_recovery_keeps_target_identity_without_python_after_fork(self):
        browser = browser_for(MenyClient, '/synthetic/adapter')
        browser.uid, browser.gid = 10001, 10002
        for euid in (0, 1000):
            with self.subTest(euid=euid), \
                 mock.patch('meny.os.geteuid', return_value=euid), \
                 mock.patch.object(browser, '_browser_daemon_executable', return_value=Path('/synthetic/daemon')), \
                 mock.patch('meny.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertTrue(browser._terminate_browser_session(time.monotonic() + 5))
            kwargs = run.call_args.kwargs
            self.assertNotIn('preexec_fn', kwargs)
            self.assertEqual(run.call_args.args[0][-4:-1],
                             ['/synthetic/socket/meal-concierge-meny-synthetic-audit.pid', '/synthetic/daemon', '10001'])
            if euid == 0:
                self.assertEqual((kwargs['user'], kwargs['group'], kwargs['extra_groups']), (10001, 10002, []))
            else:
                self.assertTrue({'user', 'group', 'extra_groups'}.isdisjoint(kwargs))


class MenyDeliveryYearTests(unittest.TestCase):
    @mock.patch('meny.datetime')
    def test_wrong_year_stops_before_select_dismiss_or_refresh(self, clock):
        clock.now.return_value = datetime(2026, 10, 5, tzinfo=ZoneInfo('Europe/Oslo'))
        label = '6. oktober klokka 16:00 til 18:00'
        for year in (2025, 2027):
            for selected, refresh in ((False, False), (True, False), (True, True)):
                with self.subTest(year=year, selected=selected, refresh=refresh):
                    browser = browser_for(MenyClient, '/synthetic/adapter')
                    browser._open_delivery_picker = mock.Mock()
                    browser._eval = mock.Mock(return_value={
                        'ready': True, 'identity': True, 'authenticated': True,
                        'already_selected': selected, 'refresh_available': refresh, 'label': label,
                        'refresh_slot': '6. oktober klokka 18:00 til 20:00',
                    })
                    browser._invoke = mock.Mock()
                    ref = f'meny:{year}-10-06T16:00/18:00'
                    with self.assertRaisesRegex(HouseholdError, 'delivery slot date changed'):
                        browser._select_delivery_slot(ref)
                    browser._invoke.assert_not_called()
                    with self.assertRaisesRegex(HouseholdError, 'could not be verified'):
                        normalized_selected_meny_slot(ref, label)

    @mock.patch('meny.datetime')
    def test_current_year_and_new_year_slots_remain_selectable(self, clock):
        for today, label, ref in (
            (datetime(2026, 10, 5), '6. oktober klokka 16:00 til 18:00', 'meny:2026-10-06T16:00/18:00'),
            (datetime(2026, 12, 31), '1. januar klokka 16:00 til 18:00', 'meny:2027-01-01T16:00/18:00'),
        ):
            with self.subTest(ref=ref):
                clock.now.return_value = today.replace(tzinfo=ZoneInfo('Europe/Oslo'))
                browser = browser_for(MenyClient, '/synthetic/adapter')
                browser._open_delivery_picker = mock.Mock()
                browser._eval = mock.Mock(return_value={
                    'ready': True, 'identity': True, 'authenticated': True,
                    'already_selected': True, 'refresh_available': False, 'label': label,
                })
                browser._invoke = mock.Mock()
                browser._wait_delivery_picker_closed = mock.Mock()
                self.assertEqual(browser._select_delivery_slot(ref)['selected']['slot_ref'], ref)
                browser._invoke.assert_called_once_with('click', '[data-meal-concierge-action="delivery-dismiss"]')


if __name__ == '__main__':
    unittest.main()
