import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
"""Reported outcomes survive the real recipe-selection/projection boundary."""
from copy import deepcopy
import unittest
import test_meal_concierge_feedback as fixtures


class ExperienceSelectionTests(unittest.TestCase):
    setUp = fixtures.FeedbackTests.setUp
    plan = fixtures.FeedbackTests.plan
    target = fixtures.FeedbackTests.target
    event = fixtures.FeedbackTests.event
    def test_exact_report_visible_in_search_detail_plan_and_undo(self):
        menu = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_handoff': self.plan()['save_handoff']})['menu']
        target = self.target(menu)
        before = self.store.read()['profile']
        event = self.event('experience', target=target, experience={'actual_active_minutes': 70, 'portion_fit': 'too_large'}, idempotency_key='slow')['event']
        self.assertIn('portion_context', event['binding'])
        recipe_id = target['reference']['recipe_ref']['id']
        def get():
            return self.app.handle({'operation':'recipes', 'action':'get', 'recipe_id':recipe_id, 'response_view':'agent'})
        detail = get()
        self.assertEqual(detail['household_experience']['report_count'], 1)
        self.assertEqual(detail['household_experience']['recent_reports'][0]['experience']['actual_active_minutes'], 70)
        search = self.app.handle({'operation':'recipes', 'action':'search', 'include_ineligible':True, 'response_view':'agent'})
        row = next(row for row in search['recipes'] if row['library_recipe_ref']['recipe_id'] == recipe_id)
        self.assertEqual(row['household_experience']['report_count'], 1)
        discovery = self.app.handle({'operation':'recipes', 'action':'discover', 'source':'internal', 'projection':'summary', 'response_view':'agent'})
        self.assertTrue(any(row.get('household_experience') for row in discovery['recipes']))
        self.assertTrue(self.plan()['household_experience'])
        self.assertEqual(self.store.read()['profile'], before)
        self.event('undo', event_id=event['event_id'], idempotency_key='undo-slow')
        self.assertNotIn('household_experience', get())

    def test_reports_do_not_transfer_to_a_different_revision(self):
        from planning_feedback import household_experience
        menu = self.app.handle({'operation':'menu','action':'save','planner_handoff':self.plan()['save_handoff']})['menu']
        target = self.target(menu)
        self.event('experience', target=target, experience={'leftover_portions':1}, idempotency_key='leftover')
        different = deepcopy(target['reference'])
        different['recipe_ref']['revision'] += 1
        self.assertIsNone(household_experience(self.store.read()['planning_feedback'], different))

    def test_fifty_candidate_summaries_stay_within_mcp_wire_budget(self):
        import json
        from agent_views import project_agent_result
        from planning_feedback import attach_experience
        rows=[]
        for index in range(50):
            recipe={'id':f'rec_{index}', 'revision':1, 'name':'Synthetic household dinner',
                    'source':{'kind':'user'}, 'times':{'active_minutes':20}}
            target={'reference':{'recipe_ref':{'id':recipe['id'],'revision':1}},
                    'recipe_key':f'bank:{recipe["id"]}', 'menu_ref':{'menu_id':'m'*30,'revision':1,'digest':'d'*64},'slot_id':'s'*32}
            events=[{'event_id':str(i),'kind':'experience','date':'2026-09-27',
                     'binding':{'target':target,'experience':{'actual_active_minutes':70,'portion_fit':'too_small','leftover_portions':0},
                                'portion_context':{'served_portions':2,'recipe_portions':2}}} for i in range(3)]
            attach_experience(recipe,events); rows.append(recipe)
        value=project_agent_result('recipes','search',{'recipes':rows})
        wire=json.dumps({'jsonrpc':'2.0','id':1,'result':{'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False,separators=(',',':'))}]}},ensure_ascii=False,separators=(',',':'))
        self.assertLess(len(wire),45000)
        self.assertEqual(len(value['recipes']),50)
