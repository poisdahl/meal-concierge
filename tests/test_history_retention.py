"""Retention follows last change and reference closure, never a count target."""
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import initial_state, StateStore
import history_retention as history

OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
TODAY = date(2026, 9, 28)
CONFIG = {'household': 'synthetic', 'provider': 'oda'}


def populated():
    state = initial_state(CONFIG)
    for key in ('old', 'other'):
        slot = {'slot_id': 'slot_' + key, 'recipe_key': 'shared_recipe', 'date': '2020-01-01'}
        menu = {'menu_id': key, 'revision': 1, 'digest': 'digest_' + key,
                'week': '2020-W01', 'slots': [slot], 'dishes': [], 'salads': []}
        state['menu_planning']['history'][key + ':1'] = menu
        state['menu_planning']['locks'][key + ':1'] = [slot['slot_id']]
        state['menu_planning']['retired'][key] = ['shared_recipe']
        state['menu_planning']['applied']['apply_' + key] = {k: menu[k] for k in ('menu_id', 'revision', 'digest')}
        state['recipe_usage'][key] = {'week': '2020-W01', 'status': 'cancelled', 'slots': [slot]}
    history.track_changes(state, now=OLD)
    return state


class RetentionTests(unittest.TestCase):
    def test_old_detached_components_archive_without_linking_shared_recipes(self):
        state = populated()
        state['menu'] = deepcopy(state['menu_planning']['history']['other:1'])
        plan = history.plan(state, today=TODAY)
        self.assertEqual(plan['remove']['recipe_usage'], ['old'])
        self.assertEqual(plan['eligible_records'], 5)
        after = history.compact(state, plan)
        self.assertEqual(after['menu'], state['menu'])
        self.assertEqual(after['recipe_usage'], {'other': state['recipe_usage']['other']})

    def test_unknown_legacy_history_gets_full_first_observation_window(self):
        state = populated()
        state.pop(history.CLOCKS)
        self.assertEqual(history.plan(state, today=TODAY)['eligible_records'], 0)
        history.track_changes(state, now=datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertEqual(history.plan(state, today=TODAY)['eligible_records'], 0)
        self.assertEqual(history.plan(state, today=date(2027, 9, 28))['eligible_records'], 10)

    def test_last_relevant_change_and_cooldown_override_old_menu_dates(self):
        state = populated()
        state['recipe_usage']['old']['cooked_keys'] = ['shared_recipe']
        # An untracked change is also retained until its clock is reestablished.
        self.assertEqual(history.plan(state, today=TODAY)['remove']['recipe_usage'], ['other'])
        history.track_changes(state, now=datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertEqual(history.plan(state, today=TODAY)['remove']['recipe_usage'], ['other'])
        state['profile']['recipes']['repeat_cooldown_weeks'] = 260
        self.assertEqual(history.plan(state, today=date(2028, 9, 28))['remove']['recipe_usage'], ['other'])
        self.assertEqual(history.plan(state, today=date(2032, 9, 28))['remove']['recipe_usage'], ['old', 'other'])

    def test_future_dates_and_malformed_clocks_are_retained(self):
        state = populated()
        state['recipe_usage']['old']['week'] = '2030-W01'
        history.track_changes(state, now=OLD)
        state[history.CLOCKS]['records']['recipe_usage']['other']['last_changed_at'] = 'unknown'
        self.assertEqual(history.plan(state, today=TODAY)['eligible_records'], 0)

    def test_external_journals_and_replay_fences_are_roots_even_when_terminal(self):
        roots = (
            ('pending_checkout', {'menu': {'menu_id': 'old'}}),
            ('pending_cancellation', {'target': {'menu_id': 'old'}}),
            ('order_change', {'menu_id': 'old'}),
            ('email_jobs', [{'status': 'sending', 'menu_snapshot': {'menu_id': 'old'}}]),
            ('recipe_delivery', {'jobs': {'delivery': {'menu': {'menu_id': 'old'}}}}),
            ('order_snapshots', {'order': {'menu_id': 'old'}}),
            ('protected_results', {'confirmation': {'status': 'retired', 'menu': {'menu_id': 'old'}}}),
            ('protected_requests', {'confirmation': {'menu_id': 'old'}}),
            ('recipe_usage_requests', {'replay': {'digest': '{"menu_id":"old"}'}}),
            ('planning_feedback', [{'target': {'menu_ref': {'menu_id': 'old'}}}]),
        )
        for key, value in roots:
            with self.subTest(root=key):
                state = populated()
                state[key] = value
                self.assertEqual(history.plan(state, today=TODAY)['remove']['recipe_usage'], ['other'])

    def test_supersedes_carried_slots_batch_outcomes_and_prepared_refs(self):
        state = populated()
        state['menu'] = {'menu_id': 'current', 'supersedes': {'menu_id': 'old', 'revision': 1, 'digest': 'digest_old'}}
        state['batch_outcomes']['sources']['slot_old'] = {'outcome': 'cooked'}
        state['batch_outcomes']['leftovers']['leftover'] = {'source_slot_id': 'slot_old', 'outcome': 'cooked'}
        history.track_changes(state, now=OLD)
        plan = history.plan(state, today=TODAY)
        self.assertEqual(plan['remove']['recipe_usage'], ['other'])
        self.assertEqual(plan['remove']['batch_outcomes/sources'], [])
        self.assertEqual(plan['remove']['batch_outcomes/leftovers'], [])
        state['menu_planning']['prepared']['replan_exact'] = {'state_digest': 'entire-history'}
        self.assertEqual(history.plan(state, today=TODAY)['eligible_records'], 0)

    def test_recovery_preserves_new_journals_and_refuses_conflicting_or_corrupt_archive(self):
        state = populated()
        plan = history.plan(state, today=TODAY)
        archive = history.archive(state, plan)
        after = history.compact(state, plan)
        after['pending_checkout'] = {'status': 'uncertain', 'confirmation_id': 'new-journal'}
        restored = history.recover(after, archive)
        self.assertEqual(restored['recipe_usage'], state['recipe_usage'])
        self.assertEqual(restored['pending_checkout'], after['pending_checkout'])
        self.assertEqual(history.recover(restored, archive), restored)
        corrupt = deepcopy(archive)
        corrupt['records']['recipe_usage']['old']['week'] = '2021-W01'
        with self.assertRaisesRegex(ValueError, 'integrity'):
            history.recover(after, corrupt)
        after['recipe_usage']['old'] = {'week': 'changed'}
        before = deepcopy(after)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            history.recover(after, archive)
        self.assertEqual(after, before)

    def test_state_transactions_track_changes_without_touching_frozen_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(directory, CONFIG)
            with store.locked() as state:
                state['recipe_usage']['old'] = {'week': '2020-W01', 'status': 'cancelled'}
                state['pending_checkout'] = {'status': 'uncertain', 'exact': [1, 2, 3]}
            before = store.read()
            with store.locked() as state:
                state['recipe_usage']['old']['cooked_keys'] = ['recipe']
            after = store.read()
            self.assertNotEqual(before[history.CLOCKS], after[history.CLOCKS])
            self.assertEqual(before['pending_checkout'], after['pending_checkout'])
            self.assertEqual(history.plan(after)['eligible_records'], 0)

    def test_capacity_is_not_an_archival_target(self):
        state = populated()
        state['recipe_usage'] = {f'menu_{i}': {'week': '2020-W01', 'status': 'cancelled'} for i in range(2001)}
        self.assertEqual(history.plan(state, today=TODAY)['remove']['recipe_usage'], [])


if __name__ == '__main__':
    unittest.main()
