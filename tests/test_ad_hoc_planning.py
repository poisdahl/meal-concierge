"""Ad-hoc dates go through the ordinary plan/save/assessment path."""
import unittest
from copy import deepcopy
from datetime import date
from unittest import mock

import test_meal_concierge_planner as fixture
import planning_assessment
import planner
from core import DEFAULT_PROFILE
import batch_planning


class AdHocPlanningTests(unittest.TestCase):
    setUp = fixture.WeeklyPlannerTests.setUp
    tearDown = fixture.WeeklyPlannerTests.tearDown
    save_candidates = fixture.WeeklyPlannerTests.save_candidates
    plan = fixture.WeeklyPlannerTests.plan

    def test_cross_week_short_plan_saved_without_weekly_quotas(self):
        with self.store.locked() as state:
            state['profile']['diet']['minimum_fish_portions'] = 4
            state['profile']['diet']['leafy_green_days'] = [4, 5]
        result = self.plan({'dates': ['2026-10-04', '2026-10-05'], 'candidates': self.save_candidates(2)})
        saved = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_ref': result['save_ref']})['menu']
        self.assertEqual([s['date'] for s in saved['slots']], ['2026-10-04', '2026-10-05'])
        self.assertEqual(saved['planning_scope']['planning_mode'], 'ad_hoc')
        self.assertFalse(saved['weekly_plan_complete'])
        self.assertTrue(planning_assessment.assess_menu(self.store.read())['ready'])
        self.assertEqual(planner.saved_menu_minimum_evaluation(saved, self.store.read()['profile'])['enforced_status'], 'pass')
        self.assertEqual(self.provider.calls, [])

    def test_period_and_default_today(self):
        candidates = self.save_candidates(2)
        result = self.plan({'period': {'start_date': '2026-10-04', 'end_date': '2026-10-05'}, 'candidates': candidates})
        self.assertEqual(result['save_handoff']['request']['dates'], ['2026-10-04', '2026-10-05'])
        with mock.patch.object(self.app, '_household_today', return_value=date(2026, 10, 4)):
            state = self.store.read()
            state['profile']['meals']['dinner_days'] = 2
            state['profile']['meals']['dishes'] = 2
            effective = self.app._effective_planner_request({'candidates': candidates}, state, anchor_current_date=True)
        self.assertEqual(effective['dates'], ['2026-10-04', '2026-10-05'])

    def test_explicit_week_retains_calendar_boundary(self):
        with self.assertRaisesRegex(planner.PlannerError, 'belong'):
            self.plan({'week': '2026-W40', 'dates': ['2026-10-04', '2026-10-05'], 'candidates': self.save_candidates(2)})

    def test_accepted_batch_template_moves_relative_to_requested_dates(self):
        profile = deepcopy(DEFAULT_PROFILE)
        profile['meals'].update(meal_mode='batch', dishes=2, batch_dishes=2,
            cook_days=['Monday', 'Thursday'], recurring_batch_accepted=True)
        layout = batch_planning.recurring_layout(profile, ['2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07'], relative=True)
        self.assertEqual([s['source_date'] for s in layout['sources']], ['2026-10-04', '2026-10-07'])
        self.assertEqual(layout['sources'][0]['eating_dates'], ['2026-10-04', '2026-10-05', '2026-10-06'])
        self.assertEqual(layout['required_portions'], 8)
        self.assertFalse(layout['sources'][1]['batch'])

    def test_relative_batch_plan_saves_consumption_and_shopping_separately(self):
        with self.store.locked() as state:
            state['profile']['meals'].update(meal_mode='batch', dishes=2, batch_dishes=2,
                cook_days=['Monday', 'Thursday'], recurring_batch_accepted=True)
        result = self.plan({'period': {'start_date': '2026-10-04', 'end_date': '2026-10-07'},
                            'candidates': self.save_candidates(2)})
        saved = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_ref': result['save_ref']})['menu']
        self.assertEqual(len(saved['dishes']), 2)
        self.assertEqual(len(saved['slots']), 4)
        self.assertEqual(saved['planning_scope']['dates'], ['2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07'])
        self.assertTrue(planning_assessment.assess_menu(self.store.read())['ready'])

    def test_invalid_periods_fail_before_provider_reads(self):
        candidates = self.save_candidates(1)
        for period in ({'start_date': '2026-10-04', 'end_date': '2026-10-12'},
                       {'start_date': '2026-10-04', 'end_date': '2026-10-03'},
                       {'start_date': 'invalid', 'end_date': '2026-10-03'}):
            with self.assertRaises(planner.PlannerError):
                self.plan({'period': period, 'candidates': candidates})
        self.assertEqual(self.provider.calls, [])
