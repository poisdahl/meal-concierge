"""Household setup and elapsed recurrence, without retailer or sender effects."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import DEFAULT_PROFILE, HouseholdError, StateStore, due_recurring, pantry_review
from service import Application
from test_meal_concierge_planner import CONFIG, NoProviderCalls


class PantryElapsedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = Application(StateStore(self.root, CONFIG), NoProviderCalls(), object())
        fixed = patch.object(self.app, '_household_today', return_value=date(2026, 9, 27))
        fixed.start()
        self.addCleanup(fixed.stop)

    def test_setup_does_not_treat_suggested_basics_as_stock(self):
        self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})
        profile = self.app.handle({'operation': 'profile', 'action': 'show'})
        self.assertFalse(profile['pantry_review']['assumptions_accepted'])
        confirmed = self.app.handle({'operation': 'profile', 'action': 'review_pantry'})
        self.assertEqual(confirmed['pantry_review']['next_review_on'], '2026-11-22')
        self.assertFalse(confirmed['pantry_review']['review_due'])
        before = self.app.store.read()
        self.assertTrue(pantry_review(before['profile'], date(2026, 11, 23))['review_due'])
        self.assertEqual(self.app.store.read(), before)
        # An unanswered reminder is still due, and expanding the list needs consent.
        self.assertTrue(pantry_review(before['profile'], date(2026, 12, 1))['review_due'])
        self.app.store.update_profile({'pantry': {'assume': ['salt', 'butter']}})
        self.assertFalse(self.app.store.read()['profile']['pantry']['assumptions_accepted'])
        self.app.handle({'operation': 'profile', 'action': 'review_pantry', 'changes': {'assume': ['salt']}})
        self.app.handle({'operation': 'profile', 'action': 'reset', 'paths': ['pantry.assume']})
        self.assertFalse(self.app.store.read()['profile']['pantry']['assumptions_accepted'])

    def test_old_preferences_are_preserved_without_inventing_consent(self):
        state = self.app.store.read()
        for key in ('assumptions_accepted', 'review_interval_days', 'last_reviewed_on'):
            state['profile']['pantry'].pop(key)
        state['profile']['products']['organic'] = 'explicit household choice'
        state['recipe_delivery']['preferences']['email']['pdf'] = True
        self.app.store.path.write_text(json.dumps(state))
        reopened = StateStore(self.root, CONFIG).read()
        self.assertFalse(reopened['profile']['pantry']['assumptions_accepted'])
        self.assertEqual(reopened['profile']['products']['organic'], 'explicit household choice')
        self.assertTrue(reopened['recipe_delivery']['preferences']['email']['pdf'])

    def test_elapsed_due_is_sticky_and_edits_preserve_purchase_date(self):
        item = {'product_id': '123', 'product_name': 'Synthetic crispbread', 'quantity': 1,
                'schedule': {'unit': 'days', 'every': 21, 'anchor': '2026-09-01'}}
        self.app.handle({'operation': 'recurring', 'action': 'add', 'item': item})
        first = self.app._due_recurring(self.app.store.read(), date(2026, 9, 27))[0]
        later = self.app._due_recurring(self.app.store.read(), date(2026, 10, 8))[0]
        self.assertEqual(first['fulfillment_key'], later['fulfillment_key'])
        self.assertEqual(later['quantity'], 1)
        with self.app.store.locked() as state:
            state['recurring_items'][0]['last_fulfilled_on'] = '2026-09-27'
        item['quantity'] = 2
        self.app.handle({'operation': 'recurring', 'action': 'add', 'item': item})
        saved = self.app.store.read()['recurring_items'][0]
        self.assertEqual(saved['last_fulfilled_on'], '2026-09-27')
        self.assertFalse(due_recurring(saved, date(2026, 10, 17)))
        self.assertTrue(due_recurring(saved, date(2026, 10, 18)))
        self.assertTrue(due_recurring(saved, date(2026, 12, 31)))

    def test_defaults_overview_and_validation(self):
        self.assertEqual(DEFAULT_PROFILE['meals']['target_active_minutes'], [0, 45])
        self.assertEqual(DEFAULT_PROFILE['meals']['maximum_active_minutes'], 60)
        self.assertEqual(DEFAULT_PROFILE['products']['organic'], '')
        self.assertEqual(DEFAULT_PROFILE['products']['local'], '')
        overview = self.app.handle({'operation': 'profile', 'action': 'overview'})
        self.assertIn('overview_request', overview['guide'])
        self.assertFalse(overview['recipe_delivery']['preferences']['email']['pdf'])
        for changes in ({'review_interval_days': 0}, {'last_reviewed_on': '2026-99-99'}, {'assumptions_accepted': 'yes'}):
            with self.subTest(changes=changes), self.assertRaises(HouseholdError):
                self.app.store.update_profile({'pantry': changes})


if __name__ == '__main__':
    unittest.main()
