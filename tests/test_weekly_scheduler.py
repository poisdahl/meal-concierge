"""Actual Application/SDK scheduler recovery with synthetic provider boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
fixture = CORE / 'tests/test_meal_concierge.py'
spec = importlib.util.spec_from_file_location('weekly_fixtures', fixture)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from core import HouseholdError, StateStore
from service import Application, Server


class WeeklySchedulerTests(unittest.TestCase):
    def setUp(self):
        fixtures.FlowTests.setUp(self)
        self.clock = mock.patch('service.now', return_value=fixtures.ODA_FIXTURE_NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.owner = {'platform': 'native-fixture', 'scope': 'scope-one'}
        self.binding = {**self.owner, 'job_id': 'weekly-one'}

    tearDown = fixtures.FlowTests.tearDown

    def call(self, action, **kwargs):
        return self.app.handle({'operation': 'schedule', 'action': action, **kwargs})

    def owner_plan(self, owner=None, **kwargs):
        return self.call('owner_plan', scheduler={'owner': owner or self.owner,
            'inventory': {**self.owner, 'verified': True}, **kwargs})['owner']

    def weekly_plan(self, binding=None, **kwargs):
        return self.call('scheduler_plan', scheduler={'binding': binding or self.binding,
            'inventory': {**self.owner, 'verified': True, 'matching_jobs': 0}, **kwargs})

    def ack(self, plan, state='active'):
        return self.call('ack_scheduler', automation_digest=plan['automation_digest'], scheduler={
            'binding': plan['scheduler']['binding'], 'generation': plan['scheduler']['generation'],
            'previous_binding': plan['scheduler'].get('previous_binding'),
            'previous_job_removed': True, 'verified': True, 'state': state})

    def owner_ack(self, owner, bindings):
        return self.call('ack_owner', scheduler={'owner': owner['owner'], 'generation': owner['generation'],
            'inventory': {**owner['owner'], 'verified': True, 'bindings': bindings}})

    def active(self, *, automatic=False):
        self.call('update', changes={'enabled': True, 'mode': 'auto_checkout' if automatic else 'cart_ready',
                                    'auto_checkout': automatic, 'maximum_total': 100.0,
                                    'delivery': {'weekday': 'Saturday', 'strategy': 'cheapest'}})
        owner = self.owner_plan()
        plan = self.weekly_plan()
        self.ack(plan)
        self.owner_ack(owner, [self.binding])
        return plan['invocation']

    def auto(self, invocation):
        return self.app.handle({'operation': 'checkout', 'action': 'auto',
                               'occurrence': '2026-W36', 'scheduler': invocation})

    def unselected(self):
        self.oda.cart['delivery'] = None
        self.oda.delivery_slots['slots'] = [deepcopy(self.oda.delivery_slots['slots'][0])]
        self.oda.delivery_slots['slots'][0]['selected'] = False

    def email(self, action, order='test-order', **kwargs):
        return self.app.handle({'operation': 'email', 'action': action, 'provider': 'oda',
                                'order_id': order, **kwargs})

    def new_email(self, order='test-order'):
        with self.store.locked() as state:
            state['email_recipient'] = 'synthetic@example.test'
            state['menu'] = {'order_id': order, 'week': '2026-W36', 'dishes': [{
                'name': 'Frozen soup', 'ingredients': ['2 carrots'], 'steps': ['Boil.'],
                'source': {'publisher': 'Synthetic source', 'relationship': 'original'},
                'rights': {'credit': 'Frozen credit'}}]}
        return self.email('schedule', order, delivery_date='2026-09-05')

    def email_plan(self, order='test-order', binding=None, **kwargs):
        return self.email('scheduler_plan', order, scheduler={
            'binding': binding or {**self.owner, 'job_id': order},
            'inventory': {**self.owner, 'verified': True, 'matching_jobs': 0}, **kwargs})

    def email_ack(self, plan, order='test-order'):
        return self.email('ack_scheduler', order, automation_digest=plan['automation_digest'], scheduler={
            'binding': plan['scheduler']['binding'], 'generation': plan['scheduler']['generation'],
            'previous_binding': plan['scheduler'].get('previous_binding'), 'previous_job_removed': True,
            'verified': True, 'state': 'active'})

    def cleanup_email(self, plan, order='test-order'):
        return self.email('ack_cleanup', order, scheduler={
            'binding': plan['scheduler']['binding'], 'generation': plan['scheduler']['generation'],
            'previous_binding': plan['scheduler'].get('previous_binding'), 'previous_job_removed': True,
            'verified': True, 'state': 'removed'})

    def test_plan_ack_due_survive_restart_and_reject_legacy_or_wrong_identity(self):
        invocation = self.active()
        self.app = Application(StateStore(Path(self.temp.name), fixtures.CONFIG), self.oda, self.browser)
        self.assertEqual(self.call('due', scheduler=invocation)['occurrence'], '2026-W36')
        for mutation in ({'generation': 'old'}, {'binding': {**self.binding, 'scope': 'other'}},
                         {'binding': {**self.binding, 'job_id': 'other'}}):
            with self.assertRaises(HouseholdError):
                self.auto({**invocation, **mutation})
        with self.assertRaises(HouseholdError):
            self.call('set_cron_job', cron_job_id='escape')
        with self.assertRaises(HouseholdError):
            self.auto(None)
        self.assertFalse(self.oda.calls)
        result = self.auto(invocation)
        self.assertEqual(result['mode'], 'cart_ready')
        self.assertEqual(result['occurrence'], '2026-W36')

    def test_settings_pause_cannot_revive_old_generation(self):
        invocation = self.active()
        paused = self.call('pause_scheduler', scheduler=invocation)
        with self.assertRaises(HouseholdError):
            self.auto(invocation)
        self.call('disable')
        self.call('update', changes={'enabled': True})
        with self.assertRaises(HouseholdError):
            self.ack(paused)
        self.assertFalse(self.oda.calls)
        current = self.call('show')['schedule']['scheduler']
        plan = self.weekly_plan(generation=current['generation'])
        self.ack(plan)
        self.assertEqual(self.call('due', scheduler=plan['invocation'])['occurrence'], '2026-W36')

    def test_registration_and_owner_ack_loss_reuse_exact_plan(self):
        self.call('update', changes={'enabled': True, 'mode': 'cart_ready'})
        owner = self.owner_plan()
        self.assertEqual(self.owner_plan(), owner)
        plan = self.weekly_plan()
        self.assertEqual(self.weekly_plan(), plan)
        self.ack(plan); self.ack(plan)
        self.owner_ack(owner, [self.binding]); self.owner_ack(owner, [self.binding])
        self.assertFalse(self.oda.calls)

    def test_global_transfer_waits_for_exact_old_cleanup_and_rejects_old_worker(self):
        old = self.active()
        owner = self.call('show')['schedule']['scheduler_owner']
        target = {'platform': 'second-native-fixture', 'scope': 'second-scope'}
        transition = self.owner_plan(target, generation=owner['generation'])
        with self.assertRaises(HouseholdError):
            self.auto(old)
        binding = {**target, 'job_id': 'weekly-two'}
        plan = self.weekly_plan(binding, generation=old['generation'])
        self.assertEqual(plan['scheduler']['previous_binding'], self.binding)
        with self.assertRaises(HouseholdError):
            self.call('ack_scheduler', automation_digest=plan['automation_digest'], scheduler={
                'binding': binding, 'generation': plan['scheduler']['generation'], 'previous_binding': self.binding,
                'verified': True, 'state': 'active'})
        self.ack(plan)
        with self.assertRaises(HouseholdError):
            self.auto(plan['invocation'])
        with self.assertRaises(HouseholdError):
            self.owner_ack(transition, [binding, self.binding])
        self.owner_ack(transition, [binding])
        self.assertEqual(self.auto(plan['invocation'])['occurrence'], '2026-W36')
        with self.assertRaises(HouseholdError):
            self.auto(old)

    def test_expired_worker_cannot_overwrite_later_attempt(self):
        invocation = self.active()
        original = self.oda.call
        entered, release = threading.Event(), threading.Event()
        failures = []
        def delay(tool, args, **kwargs):
            if threading.current_thread().name == 'old-weekly' and tool == 'get_cart':
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test worker was not released')
            return original(tool, args, **kwargs)
        self.oda.call = delay
        def old_worker():
            try:
                self.auto(invocation)
            except HouseholdError as exc:
                failures.append(str(exc))
        thread = threading.Thread(target=old_worker, name='old-weekly')
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with mock.patch('service.now', return_value=fixtures.ODA_FIXTURE_NOW + timedelta(minutes=6)):
                self.assertEqual(self.auto(invocation)['mode'], 'cart_ready')
                later = deepcopy(self.store.read()['occurrences']['2026-W36'])
                release.set(); thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(self.store.read()['occurrences']['2026-W36'], later)
            self.assertTrue(failures)
        finally:
            release.set(); thread.join(5)

    def test_legacy_terminal_email_cleanup_needs_no_new_native_job(self):
        self.new_email()
        self.email('cancel_followup', owner_confirmed_cancelled=True)
        owner = self.owner_plan()
        receipt = {'generation': owner['generation'], 'state': 'removed',
                   'verified': True, 'inventory': {**self.owner, 'verified': True, 'matching_jobs': 0}}
        self.email('ack_cleanup', scheduler=receipt)
        self.email('ack_cleanup', scheduler=receipt)
        self.owner_ack(owner, [])
        self.assertNotIn('scheduler', self.store.read()['email_jobs'][0])
        self.assertFalse(self.email('automation_plan')['removals'])
        self.assertEqual(self.email('cancel_followup', owner_confirmed_cancelled=True)['automation_cleanup']['action'], 'none')

    def test_legacy_cleanup_and_adoption_require_original_inventory_scope(self):
        self.new_email()
        self.email('cancel_followup', owner_confirmed_cancelled=True)
        target = {'platform': 'second-native', 'scope': 'second-scope'}
        owner = self.owner_plan(target)
        receipt = {'generation': owner['generation'], 'state': 'removed', 'verified': True,
                   'inventory': {**target, 'verified': True, 'matching_jobs': 0}}
        with self.assertRaisesRegex(HouseholdError, 'original scheduler scope'):
            self.email('ack_cleanup', scheduler=receipt)
        with self.assertRaises(HouseholdError):
            self.owner_ack(owner, [])
        self.email('ack_cleanup', scheduler={**receipt, 'inventory': {**self.owner, 'verified': True, 'matching_jobs': 0}})
        self.new_email('new-in-target')
        new_binding = {**target, 'job_id': 'new-in-target'}
        with self.assertRaisesRegex(HouseholdError, 'original scheduler scope'):
            self.email_plan('new-in-target', binding=new_binding)
        plan = self.email_plan('new-in-target', binding=new_binding,
            inventory={**target, 'verified': True, 'matching_jobs': 0})
        self.email_ack(plan, 'new-in-target')
        self.owner_ack(owner, [new_binding])

    def test_reconcile_waits_for_live_selection_before_resolving(self):
        invocation = self.active()
        self.unselected()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.oda.call
        failures, results = [], []
        def accepted(tool, args, **kwargs):
            result = original(tool, args, **kwargs)
            if tool == 'select_delivery_slot':
                entered.set()
                if not release.wait(5):
                    raise AssertionError('selection test was not released')
                raise HouseholdError('accepted response lost')
            return result
        self.oda.call = accepted
        def worker():
            try:
                self.auto(invocation)
            except HouseholdError as exc:
                failures.append(str(exc))
        def reconcile():
            try:
                results.append(self.call('reconcile', occurrence='2026-W36'))
            finally:
                finished.set()
        selected = threading.Thread(target=worker)
        reader = threading.Thread(target=reconcile)
        selected.start()
        try:
            self.assertTrue(entered.wait(5))
            count = len(self.oda.calls)
            reader.start()
            self.assertFalse(finished.wait(0.1))
            self.assertEqual(len(self.oda.calls), count)
            self.assertEqual(self.store.read()['occurrences']['2026-W36']['delivery_effect']['state'], 'dispatching')
            owner = self.call('show')['schedule']['scheduler_owner']
            with self.assertRaisesRegex(HouseholdError, 'original dispatched'):
                self.owner_ack(owner, [self.binding])
        finally:
            release.set(); selected.join(5)
            if reader.ident:
                reader.join(5)
        self.assertFalse(selected.is_alive() or reader.is_alive())
        self.assertTrue(failures)
        self.assertTrue(results[0]['resolved'])
        self.assertEqual(sum(tool == 'select_delivery_slot' for tool, _ in self.oda.calls), 1)

    def test_reconcile_does_not_read_slots_during_protected_checkout(self):
        invocation = self.active(automatic=True)
        self.auto(invocation)
        with self.store.locked() as state:
            state['pending_checkout']['status'] = 'uncertain'
            row = state['occurrences']['2026-W36']
            row['delivery_effect'] = {'state': 'uncertain', 'attempt_id': row['attempt_id'],
                                      'provider': 'oda', 'slot_ref': self.oda.delivery_slots['slots'][0]['slot_ref']}
        before = len(self.oda.calls)
        with self.assertRaisesRegex(HouseholdError, 'protected checkout'):
            self.call('reconcile', occurrence='2026-W36')
        self.assertEqual(len(self.oda.calls), before)

    def test_manual_preparation_cannot_publish_over_a_newer_weekly_attempt(self):
        invocation = self.active()
        self.auto(invocation)
        original = self.oda.call
        def replace(tool, args, **kwargs):
            if tool == 'get_cart' and not getattr(replace, 'done', False):
                replace.done = True
                self.auto(invocation)
            return original(tool, args, **kwargs)
        self.oda.call = replace
        with self.assertRaisesRegex(HouseholdError, 'attempt is stale'):
            self.app.handle({'operation': 'checkout', 'action': 'prepare', 'occurrence': '2026-W36'})
        self.assertIsNone(self.store.read()['pending_checkout'])
        self.assertEqual(self.store.read()['occurrences']['2026-W36']['attempts'], 2)

    def test_pause_during_provider_lookup_prevents_selection_and_stale_status(self):
        invocation = self.active()
        self.unselected()
        original = self.oda.call
        def pause(tool, args, **kwargs):
            if tool == 'get_delivery_slots' and not getattr(pause, 'done', False):
                pause.done = True
                self.call('pause_scheduler', scheduler=invocation)
            return original(tool, args, **kwargs)
        self.oda.call = pause
        with self.assertRaises(HouseholdError):
            self.auto(invocation)
        self.assertFalse(any(tool == 'select_delivery_slot' for tool, _ in self.oda.calls))
        self.assertIsNone(self.store.read().get('delivery_selection'))
        self.assertEqual(self.store.read()['occurrences']['2026-W36']['status'], 'started')

    def test_lost_selection_blocks_retry_and_owner_until_original_read_reconciliation(self):
        invocation = self.active()
        self.unselected()
        original = self.oda.call
        def lose(tool, args, **kwargs):
            result = original(tool, args, **kwargs)
            if tool == 'select_delivery_slot':
                self.call('pause_scheduler', scheduler=invocation)
                raise HouseholdError('synthetic accepted selection response lost')
            return result
        self.oda.call = lose
        with self.assertRaisesRegex(HouseholdError, 'response lost'):
            self.auto(invocation)
        effect = self.store.read()['occurrences']['2026-W36']['delivery_effect']
        self.assertEqual(effect['state'], 'uncertain')
        current_owner = self.call('show')['schedule']['scheduler_owner']
        new = self.owner_plan({'platform': 'native-fixture', 'scope': 'scope-two'}, generation=current_owner['generation'])
        with self.assertRaisesRegex(HouseholdError, 'original dispatched'):
            self.owner_ack(new, [])
        with self.assertRaises(HouseholdError):
            self.app.handle({'operation': 'delivery', 'action': 'select', 'slot_ref': effect['slot_ref']})
        self.app = Application(StateStore(Path(self.temp.name), fixtures.CONFIG), self.oda, self.browser)
        self.assertTrue(self.call('reconcile', occurrence='2026-W36')['resolved'])
        self.assertIsNone(self.store.read().get('delivery_selection'))
        self.assertEqual(sum(tool == 'select_delivery_slot' for tool, _ in self.oda.calls), 1)
        self.assertEqual(self.store.read()['occurrences']['2026-W36']['delivery_effect']['attempt_id'], effect['attempt_id'])

    def test_accepted_selection_then_pause_preserves_evidence_not_provenance(self):
        invocation = self.active()
        self.unselected()
        original = self.oda.call
        def pause(tool, args, **kwargs):
            result = original(tool, args, **kwargs)
            if tool == 'select_delivery_slot':
                self.call('pause_scheduler', scheduler=invocation)
            return result
        self.oda.call = pause
        with self.assertRaises(HouseholdError):
            self.auto(invocation)
        row = self.store.read()['occurrences']['2026-W36']
        self.assertEqual(row['delivery_effect']['state'], 'resolved')
        self.assertEqual(row['status'], 'started')
        self.assertIsNone(self.store.read().get('delivery_selection'))

    def test_pause_after_prepare_and_at_final_callback_prevents_checkout(self):
        for phase in ('prepared', 'before_click'):
            with self.subTest(phase=phase):
                if phase == 'before_click':
                    self.tearDown(); self.setUp()
                invocation = self.active(automatic=True)
                prepared = self.auto(invocation)
                original = self.browser.submit_checkout
                if phase == 'prepared':
                    self.call('pause_scheduler', scheduler=invocation)
                else:
                    def submit(*args, **kwargs):
                        self.call('pause_scheduler', scheduler=invocation)
                        return original(*args, **kwargs)
                    self.browser.submit_checkout = submit
                with self.assertRaises(HouseholdError):
                    self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
                self.assertEqual(self.browser.checkout_clicks, 0)

    def test_manual_cart_ready_continuation_stays_manual_after_pause(self):
        invocation = self.active()
        ready = self.auto(invocation)
        self.call('pause_scheduler', scheduler=invocation)
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare', 'occurrence': ready['occurrence']})
        pending = self.store.read()['pending_checkout']
        self.assertFalse(pending['automatic_checkout'])
        self.assertTrue(pending['scheduler_context']['manual'])
        result = self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        self.assertTrue(result['confirmed'])

    def test_email_only_owner_and_new_email_during_handover(self):
        owner = self.owner_plan()
        self.new_email()
        with self.assertRaises(HouseholdError):
            self.owner_ack(owner, [])
        plan = self.email_plan()
        self.email_ack(plan)
        self.new_email('late-email')
        with self.assertRaises(HouseholdError):
            self.owner_ack(owner, [plan['scheduler']['binding']])
        late = self.email_plan('late-email')
        self.email_ack(late, 'late-email')
        self.owner_ack(owner, [plan['scheduler']['binding'], late['scheduler']['binding']])
        self.assertNotIn('scheduler', self.call('show')['schedule'])
        self.assertIsNone(self.call('show')['schedule'].get('cron_job_id'))

    def test_global_handover_fences_legacy_email(self):
        self.new_email()
        self.owner_plan()
        with self.assertRaisesRegex(HouseholdError, 'owner'):
            self.email('due')
        with self.assertRaises(HouseholdError):
            self.email('ack_automation', protocol=4)
        plan = self.email('automation_plan')
        self.assertFalse(plan['updates'])
        self.assertEqual(len(plan['scheduler_updates']), 1)

    def test_global_transfer_between_email_claim_and_dispatch_revokes_send(self):
        owner = self.owner_plan()
        self.new_email()
        self.oda.order_delivery = '2026-09-03'
        self.email('schedule', delivery_date='2026-09-03')
        plan = self.email_plan()
        self.email_ack(plan)
        self.owner_ack(owner, [plan['scheduler']['binding']])
        claim = self.email('due', scheduler=plan['invocation'])
        self.owner_plan({'platform': 'new-native', 'scope': 'new-scope'}, generation=owner['generation'])
        with self.assertRaisesRegex(HouseholdError, 'owner'):
            self.email('begin_send', claim_token=claim['claim_token'], scheduler=plan['invocation'])
        self.assertEqual(self.store.read()['email_jobs'][0]['status'], 'claimed')

    def test_sent_native_job_reservation_and_cleanup_replay(self):
        owner = self.owner_plan()
        self.new_email()
        plan = self.email_plan()
        self.email_ack(plan)
        self.owner_ack(owner, [plan['scheduler']['binding']])
        # The existing local-sender tests prove mark_sent. This fixture records
        # only terminal journal setup to isolate native cleanup and ID reuse.
        with self.store.locked() as state:
            state['email_jobs'][0]['status'] = 'sent'
        self.new_email('other')
        with self.assertRaisesRegex(HouseholdError, 'already bound'):
            self.email_plan('other', binding=plan['scheduler']['binding'])
        self.cleanup_email(plan)
        self.cleanup_email(plan)
        other = self.email_plan('other', binding=plan['scheduler']['binding'])
        self.cleanup_email(plan)  # Exact old receipt cannot release new row.
        self.assertEqual(self.store.read()['email_jobs'][1]['scheduler']['generation'], other['scheduler']['generation'])
        with self.assertRaises(HouseholdError):
            self.email('ack_cleanup', scheduler={**plan['scheduler'], 'generation': 'stale', 'state': 'removed', 'verified': True})

    def test_weekly_email_collisions_and_disabled_weekly_cleanup(self):
        invocation = self.active()
        self.new_email()
        with self.assertRaisesRegex(HouseholdError, 'already bound'):
            self.email_plan(binding=self.binding)
        email = self.email_plan()
        self.email_ack(email)
        disabled = self.call('disable')['automation_cleanup']
        self.ack(disabled, 'removed')
        owner = self.call('show')['schedule']['scheduler_owner']
        self.owner_ack(owner, [email['scheduler']['binding']])
        self.assertEqual(self.store.read()['email_jobs'][0]['status'], 'pending')
        self.assertEqual(self.call('show')['schedule']['scheduler_owner']['state'], 'active')

    def test_malformed_owner_and_inventory_never_fall_back(self):
        with self.assertRaises(HouseholdError):
            self.call('owner_plan', scheduler={'owner': self.owner})
        with self.store.locked() as state:
            state['schedule']['scheduler_owner'] = {}
        with self.assertRaisesRegex(HouseholdError, 'invalid managed'):
            self.auto(None)

    def test_window_boundaries_and_iso_year_use_original_occurrence(self):
        invocation = self.active()
        for instant in (datetime(2026, 9, 3, 12, 59, tzinfo=timezone.utc),
                        datetime(2026, 9, 3, 13, 30, tzinfo=timezone.utc)):
            with mock.patch('service.now', return_value=instant), self.assertRaises(HouseholdError):
                self.call('due', scheduler=invocation)
        with mock.patch('service.now', return_value=datetime(2027, 1, 1, 14, 5, tzinfo=timezone.utc)):
            self.call('update', changes={'weekday': 'Friday'})
            generation = self.call('show')['schedule']['scheduler']['generation']
            plan = self.weekly_plan(generation=generation)
            self.ack(plan)
            self.assertEqual(self.call('due', scheduler=plan['invocation'])['occurrence'], '2026-W53')

    def test_actual_sdk_and_cli_weekly_owner_plan_ack_due_and_auto(self):
        # A real native SDK bridge talks to the existing Server/Application.
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        path = Path(self.temp.name) / 's.sock'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path)); listener.listen(); listener.settimeout(0.1)
        stop = threading.Event()
        server = Server(path, os.getgid(), os.getuid(), self.app)
        def serve():
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                server._serve(connection)
        thread = threading.Thread(target=serve)
        thread.start()
        async def exercise():
            params = StdioServerParameters(command=sys.executable, args=['-B', '-I', str(CORE/'mcp_server.py')],
                env={'MEAL_CONCIERGE_SOCKET': str(path), 'HOME': self.temp.name, 'TMPDIR': self.temp.name}, cwd=self.temp.name)
            with open(Path(self.temp.name)/'bridge.log', 'w') as log:
                async with stdio_client(params, errlog=log) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=15) as session:
                        await session.initialize()
                        async def call(tool, **kwargs):
                            result = await session.call_tool('meal_concierge_'+tool, kwargs)
                            self.assertFalse(result.is_error, str(result))
                            return result.structured_content
                        await call('schedule', action='update', changes={'enabled': True, 'mode': 'cart_ready'})
                        owner = (await call('schedule', action='owner_plan', scheduler={
                            'owner': self.owner, 'inventory': {**self.owner, 'verified': True}}))['owner']
                        plan = await call('schedule', action='scheduler_plan', scheduler={'binding': self.binding,
                            'inventory': {**self.owner, 'verified': True, 'matching_jobs': 0}})
                        await call('schedule', action='ack_scheduler', automation_digest=plan['automation_digest'],
                            scheduler={'binding': self.binding, 'generation': plan['scheduler']['generation'],
                                       'previous_binding': None, 'state': 'active', 'verified': True})
                        await call('schedule', action='ack_owner', scheduler={'owner': self.owner,
                            'generation': owner['generation'], 'inventory': {**self.owner, 'verified': True, 'bindings': [self.binding]}})
                        due = await call('schedule', action='due', scheduler=plan['invocation'])
                        result = await call('checkout', action='auto', occurrence=due['occurrence'], scheduler=due['scheduler'])
                        self.assertEqual(result['mode'], 'cart_ready')
        try:
            asyncio.run(exercise())
            invocation = self.app._weekly_invocation(self.store.read()["schedule"])
            cli = subprocess.run([sys.executable, "-B", "-I", str(CORE / "cli.py")],
                input=json.dumps({"operation": "schedule", "action": "due", "scheduler": invocation}),
                text=True, capture_output=True, timeout=15, cwd=self.temp.name,
                env={"PATH": os.defpath, "HOME": self.temp.name, "TMPDIR": self.temp.name, "MEAL_CONCIERGE_SOCKET": str(path)})
            self.assertEqual(cli.returncode, 0, cli.stderr + cli.stdout)
            self.assertEqual(json.loads(cli.stdout)["result"]["occurrence"], "2026-W36")
        finally:
            stop.set(); thread.join(5); listener.close()
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
