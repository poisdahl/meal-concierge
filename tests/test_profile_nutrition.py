from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copy import deepcopy
import tempfile
import unittest

from core import HouseholdError, StateStore
from service import Application
from recipe_selection import context_queries
from test_meal_concierge_planner import CONFIG, NoProviderCalls, recipe, explicit_facts


class ProfileNutritionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def app(self, config=CONFIG, root=None):
        store = StateStore(root or self.root, config)
        return Application(store, NoProviderCalls(), object())

    def test_fresh_setup_exposes_editable_national_goal_without_numeric_quotas(self):
        for provider in ('oda', 'meny', 'mathem'):
            with self.subTest(provider=provider):
                app = self.app({**CONFIG, 'provider': provider}, self.root / provider)
                diet = app.handle({'operation': 'setup', 'action': 'show'})['current']['diet']
                self.assertEqual(diet['patterns'], ['National dietary guidelines for the household country'])
                self.assertEqual(diet['fish_grams_per_person'], [])
                self.assertEqual(diet['prioritise'], [])
                self.assertTrue(all(value == 0 for key, value in diet.items() if key.startswith('minimum_')))
                self.assertFalse(any(diet['plate'].values()))
                self.assertEqual(diet['nutrition'], '')
                app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': False,
                            'changes': {'diet': {'patterns': ['Swedish dietary guidelines'], 'fish_grams_per_person': [150, 300]}}})
                saved = app.store.read()['profile']['diet']
                self.assertEqual(saved['patterns'], ['Swedish dietary guidelines'])
                self.assertEqual(saved['fish_grams_per_person'], [150, 300])

    def test_existing_goals_survive_reopen_setup_and_unrelated_update(self):
        app = self.app()
        app.store.update_profile({'diet': {
            'patterns': ['Norwegian dietary guidelines', 'Mediterranean', 'MIND', 'DASH'],
            'prioritise': ['vegetables', 'whole grains', 'fish', 'legumes'],
            'fish_grams_per_person': [300, 450], 'minimum_fish_portions': 2,
            'minimum_legume_dinners': 1, 'minimum_wholegrain_or_potato_dinners': 2,
            'minimum_vegetable_types': 5,
            'plate': {'vegetables': 0.5, 'protein': 0.25, 'wholegrain_or_potato': 0.25},
            'nutrition': 'My chosen nutritional goals', 'avoid': ['cream'],
            'rules': [{'kind': 'never_buy', 'term': 'sour cream'}],
        }, 'products': {'processing': 'My saved processing preference'}})
        before = deepcopy(app.store.read()['profile'])
        app = self.app()
        app.handle({'operation': 'setup', 'action': 'rerun'})
        app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})
        app.handle({'operation': 'profile', 'action': 'update', 'changes': {'cuisine': {'base_style': 'Quick cooking'}}})
        after = app.store.read()['profile']
        self.assertEqual(after['diet'], before['diet'])
        self.assertEqual(after['products'], before['products'])

    def test_explicit_empty_goals_and_targeted_reset_preserve_exclusions(self):
        cfg = {**CONFIG, 'profile_overrides': {'diet': {'patterns': [], 'nutrition': '',
                'prioritise': [], 'fish_grams_per_person': [], 'avoid': ['cream'],
                'allergies_or_sensitivities': ['peanut']}}}
        app = self.app(cfg)
        app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})
        self.assertEqual(self.app(cfg).store.read()['profile']['diet']['patterns'], [])
        app.store.update_profile({'diet': {'patterns': ['Custom'], 'fish_grams_per_person': [0, 0]}})
        self.assertEqual(app.store.read()['profile']['diet']['fish_grams_per_person'], [0, 0])
        app.handle({'operation': 'profile', 'action': 'reset', 'paths': ['diet.patterns', 'diet.fish_grams_per_person']})
        diet = app.store.read()['profile']['diet']
        self.assertEqual(diet['patterns'], [])
        self.assertEqual(diet['fish_grams_per_person'], [])
        self.assertEqual(diet['avoid'], ['cream'])
        self.assertEqual(diet['allergies_or_sensitivities'], ['peanut'])
        for invalid in ([1], [5, 2], [False, 1], [-1, 2], 'none', [1, 2, 3]):
            with self.subTest(invalid=invalid), self.assertRaises(HouseholdError):
                app.store.update_profile({'diet': {'fish_grams_per_person': invalid}})
        self.assertEqual(app.store.read()['profile']['diet'], diet)

    def test_ranking_uses_saved_goals_without_intrinsic_food_group_bonus(self):
        app = self.app()
        app.store.update_profile({'diet': {'patterns': []}})
        candidates = []
        for index, ingredient in enumerate(('laks', 'bønner')):
            dish = recipe(ingredient, f'goal-{index}', ingredient=ingredient)
            dish['tags'] = ['fish'] if index == 0 else ['legumes']
            saved = app.handle({'operation': 'recipes', 'action': 'save', 'recipe': dish,
                                'idempotency_key': f'goal-{index}'})['recipe']
            candidates.append({'recipe_ref': {'id': saved['id'], 'revision': saved['revision']},
                               'facts': explicit_facts(dietary=['fish'] if index == 0 else ['legume'], complete=True)})
        def plan(items):
            return app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {
                'week': '2026-W37', 'dates': ['2026-09-07'], 'candidates': items}})['plan']['selection']
        neutral = [plan([candidate]) for candidate in candidates]
        self.assertEqual(neutral[0]['total_score'], neutral[1]['total_score'])
        for selection in neutral:
            self.assertNotIn('unsupported:diet.plate', selection['soft_relaxations'])
            reasons = selection['plan_reason_contributions'] + selection['slots'][0]['reason_contributions']
            self.assertFalse(any(r['code'].startswith(('dietary:', 'weekly_target:')) for r in reasons))
        for desired, tag in enumerate(('fish', 'legumes')):
            app.store.update_profile({'diet': {'prioritise': [tag]}})
            chosen = plan(candidates)
            self.assertEqual(chosen['slots'][0]['reference']['recipe_ref'], candidates[desired]['recipe_ref'])
        app.store.update_profile({'diet': {'prioritise': [], 'minimum_fish_portions': 1}})
        self.assertEqual(plan(candidates)['slots'][0]['reference']['recipe_ref'], candidates[0]['recipe_ref'])
        app.store.update_profile({'diet': {'minimum_fish_portions': 0, 'fish_grams_per_person': [0, 0]}})
        self.assertEqual(plan(candidates)['slots'][0]['reference']['recipe_ref'], candidates[1]['recipe_ref'])

    def test_discovery_queries_follow_preferences_without_diet_category_fallback(self):
        profile = self.app().store.read()['profile']
        for provider in ('oda', 'meny', 'mathem'):
            expected = ['middag', 'gryta', 'soppa', 'ugnsrätt'] if provider == 'mathem' else ['middag', 'gryte', 'suppe', 'ovnsrett']
            self.assertEqual(context_queries(profile, provider), expected)
        profile['diet']['prioritise'] = ['fish']
        profile['cuisine']['wanted'] = ['Thai']
        self.assertEqual(context_queries(profile, 'meny'), ['Thai', 'fish', 'middag', 'gryte', 'suppe', 'ovnsrett'])


if __name__ == '__main__':
    unittest.main()
