"""Unbound days remain editable until a reviewed purchase anchors the calendar."""
import unittest
from copy import deepcopy
from datetime import date
from unittest import mock
import test_meal_concierge_planner as fixture
import menu_planning as mp
from planning_assessment import assess_menu
from service_common import menu_email_html


class RelativePlanningTests(unittest.TestCase):
    setUp = fixture.WeeklyPlannerTests.setUp
    tearDown = fixture.WeeklyPlannerTests.tearDown
    save_candidates = fixture.WeeklyPlannerTests.save_candidates
    plan = fixture.WeeklyPlannerTests.plan

    def saved(self, batch=False):
        with self.store.locked() as state:
            state['profile']['meals'].update(dinner_days=6 if batch else 2,
                dishes=2, batch_dishes=2 if batch else 0, meal_mode='batch' if batch else 'fresh',
                cook_days=['Monday','Thursday'], recurring_batch_accepted=True)
        result = self.plan({'candidates': self.save_candidates(2)})
        return self.app.handle({'operation':'menu','action':'save','planner_ref':result['save_ref']})['menu']

    def test_relative_plan_save_render_and_later_edit(self):
        menu = self.saved()
        self.assertEqual([s['day'] for s in menu['slots']], [1,2])
        self.assertFalse(any('date' in s for s in menu['slots']))
        self.assertIn('Dag 1', menu_email_html(menu))
        self.assertTrue(assess_menu(self.store.read())['ready'])
        with mock.patch.object(self.app, '_household_today', return_value=date(2027,1,20)):
            changed = self.app.handle({'operation':'menu','action':'edit_slots','menu_ref':mp.menu_ref(menu),
                'idempotency_key':'relative-move', 'edits':[{'action':'move','slot_id':menu['slots'][1]['slot_id'],'day':4}]})
        self.assertEqual([s['day'] for s in changed['menu']['slots']], [1,4])

    def test_relative_batch_keeps_preparations_and_quantities(self):
        menu = self.saved(batch=True)
        self.assertEqual([s['day'] for s in menu['slots']], list(range(1,7)))
        self.assertEqual([s['day'] for s in mp.preparation_slots(menu)], [1,4])
        self.assertEqual(len(menu['dishes']),2)
        self.assertIn('porsjoner Dag 1',menu_email_html(menu))
        self.assertTrue(assess_menu(self.store.read())['ready'])

    def test_checkout_binds_only_after_confirmation_and_replay_is_stable(self):
        import test_meal_concierge as retail
        from service import Application
        original = self.saved(batch=True)
        menu = self.app.handle({'operation':'menu','action':'add_slot','menu_ref':mp.menu_ref(original),
            'idempotency_key':'checkout-side','slot_input':{'day':2,'meal_type':'side','portions':2,
            'reference':original['slots'][0]['reference']}})['menu']
        self.app.handle({'operation':'menu','action':'lock','menu_ref':mp.menu_ref(menu),
            'slot_id':menu['slots'][0]['slot_id'],'locked':True})
        provider = retail.MutableFakeOda()
        browser = retail.FakeBrowser()
        browser.oda = provider
        app = Application(self.store, provider, browser)
        app._now = lambda: retail.ODA_FIXTURE_NOW
        provider._mutate_cart({'operations':[{'productId':'10','quantity':-1}]})
        sync = retail.CartPlanTests.sync(app, '10', quantity=1)
        self.assertTrue(sync['synced'])
        missing = app.handle({'operation':'checkout','action':'prepare'})
        self.assertEqual(missing['reason'], 'first_meal_date_required')
        self.assertIsNone(self.store.read().get('pending_checkout'))
        self.assertEqual(browser.checkout_clicks, 0)
        prepared = app.handle({'operation':'checkout','action':'prepare','first_meal_date':'2026-09-08'})
        self.assertIn('summary', prepared, prepared)
        self.assertEqual(prepared['summary']['menu_schedule']['first_meal_date'], '2026-09-08')
        self.assertTrue(mp.relative(self.store.read()['menu']))
        pending = deepcopy(self.store.read()['pending_checkout'])
        confirmed = app.handle({'operation':'checkout','action':'confirm','confirmation_id':prepared['confirmation_id']})
        self.assertTrue(confirmed.get('confirmed'), confirmed)
        self.assertEqual(browser.checkout_clicks, 1)
        once = self.store.read()
        bound = once['order_snapshots']['new-order']
        self.assertEqual(bound['slots'][0]['date'], '2026-09-08')
        self.assertEqual(bound['slots'][-1]['date'], '2026-09-13')
        self.assertEqual([s['slot_id'] for s in bound['slots']], [s['slot_id'] for s in menu['slots']])
        self.assertEqual(bound['replan_selection'], menu['replan_selection'])
        self.assertEqual(bound['revision'],menu['revision']+1)
        self.assertEqual(once['menu_planning']['locks'][mp.lock_key(bound)], [menu['slots'][0]['slot_id']])
        for slot in bound['slots']:
            owner = bound.get('slot_owners',{}).get(slot['slot_id'],bound['menu_id'])
            owned = once['recipe_usage'][owner]['slots']
            self.assertIn(slot,owned)
            self.assertEqual(sum(s['slot_id']==slot['slot_id'] for r in once['recipe_usage'].values() for s in r.get('slots',[])),1)
        self.assertIn('Tirsdag 8. september 2026',menu_email_html(bound))
        with self.store.locked() as state:
            app._record_order_snapshot(state, pending, 'new-order')
        twice = self.store.read()
        self.assertEqual(once['order_snapshots'],twice['order_snapshots'])
        self.assertEqual(once['order_snapshot_times'],twice['order_snapshot_times'])
        self.assertEqual(once.get('email'),twice.get('email'))
        self.assertEqual(once.get('email_jobs'),twice.get('email_jobs'))
        from service_common import menu_digest
        self.assertEqual(bound['digest'],menu_digest(bound))

    def test_replan_and_add_use_ordinals_after_calendar_time_passes(self):
        menu = self.saved()
        candidates = self.save_candidates(3)
        with mock.patch.object(self.app, '_household_today', return_value=date(2027,2,1)):
            prepared = self.app.handle({'operation':'menu','action':'replan_prepare','menu_ref':mp.menu_ref(menu),
                'remaining_days':[2], 'planner_input':{'candidates':[candidates[2]]}})
            self.assertIn('apply_arguments',prepared,prepared)
            changed = self.app.handle({'operation':'menu',**prepared['apply_arguments']})['menu']
            added = self.app.handle({'operation':'menu','action':'add_slot','menu_ref':mp.menu_ref(changed),
                'idempotency_key':'relative-side', 'slot_input':{'day':2,'meal_type':'side','portions':2,
                'reference':{k:v for k,v in candidates[0].items() if k!='facts'}}})['menu']
        self.assertEqual([s['day'] for s in added['slots']], [1,2,2])
        self.assertFalse(any('date' in s for s in added['slots']))

    def test_reject_mixed_or_invalid_ordinal_input(self):
        from core import HouseholdError
        for values in ({'days':[True]}, {'days':[0]}, {'days':[32]}, {'days':[1,'2026-10-18']},
                       {'planning_mode':'relative','week':'2026-W42'},
                       {'planning_mode':'relative','dates':['2026-10-18']}):
            with self.subTest(values=values), self.assertRaises(HouseholdError):
                self.app._effective_planner_request(values,self.store.read(),anchor_current_date=True)
        self.assertEqual(self.provider.calls,[])

    def test_calendar_gaps_year_boundary_and_absolute_storage(self):
        from core import HouseholdError
        from service_common import menu_digest
        menu = self.saved()
        menu['slots'][1]['day']=4
        menu['planning_scope']['days']=[1,4]
        menu['digest']=menu_digest(menu)
        delivery={'slot':{'start_at':'2026-12-30T22:00:00+00:00','end_at':'2026-12-31T01:00:00+00:00'}}
        bound=mp.bind_calendar(menu,'2026-12-31',delivery)
        self.assertEqual([s['date'] for s in bound['slots']],['2026-12-31','2027-01-03'])
        self.assertEqual(bound['digest'],menu_digest(bound))
        self.assertEqual(bound['dishes'],menu['dishes'])
        self.assertTrue(mp.within_storage(menu['slots'][0],menu['slots'][1],{'max_interval_days':3}))
        self.assertFalse(mp.within_storage(menu['slots'][0],menu['slots'][1],{'max_interval_days':2}))
        with self.assertRaises(HouseholdError):
            mp.within_storage(menu['slots'][0],menu['slots'][1],{'use_by_date':'2027-01-05'})

    def test_calendar_eligibility_blocks_recorded_and_mixed_history(self):
        from core import HouseholdError
        menu=self.saved()
        with self.store.locked() as state:
            state['menu_planning']['outcomes'][menu['slots'][0]['slot_id']]={'outcome':'cooked'}
        with self.assertRaisesRegex(HouseholdError,'recorded'):
            mp.require_calendar_eligible(self.store.read(),menu)
        with self.store.locked() as state:
            state['menu_planning']['outcomes'].clear()
            state['recipe_usage'][menu['menu_id']]['cooked_keys']=['historical-recipe']
        with self.assertRaisesRegex(HouseholdError,'mixed_history_owner'):
            mp.require_calendar_eligible(self.store.read(),menu)

    def test_nullable_storage_and_relative_count(self):
        import planner
        self.assertTrue(mp.within_storage({'date':'2026-10-12'}, {'date':'2026-10-14'},
            {'max_interval_days':None,'use_by_date':'2026-10-14'}))
        menu=self.saved()
        profile=deepcopy(self.store.read()['profile'])
        profile['meals']['dinner_days']=6
        assessment=planner.saved_menu_minimum_evaluation(menu,profile)
        self.assertTrue(assessment['complete_menu'])

    def test_new_relative_menu_starts_empty_cart_but_successor_does_not(self):
        import tempfile
        import test_meal_concierge as retail
        for successor in (False,True):
            with self.subTest(successor=successor), tempfile.TemporaryDirectory() as directory:
                store,provider,_,app,pid=retail.CartPlanTests.app(directory,'oda')
                retail.CartPlanTests.sync(app,pid)
                provider._mutate_cart({'operations':[{'productId':pid,'quantity':-2}]})
                with store.locked() as state:
                    old=deepcopy(state['menu'])
                    state['menu']={**old,'menu_id':'new-relative','digest':'b'*64,
                        'planning_scope':{'planning_mode':'relative','days':[1,2]}}
                    if successor:
                        state['menu']['supersedes']=mp.menu_ref(old)
                result=retail.CartPlanTests.sync(app,pid)
                self.assertEqual(result['synced'],not successor)

    def test_cooking_between_review_and_receipt_never_redates_history(self):
        import test_meal_concierge as retail
        from service import Application
        from core import HouseholdError
        menu=self.saved()
        provider=retail.MutableFakeOda(); browser=retail.FakeBrowser(); browser.oda=provider
        app=Application(self.store,provider,browser); app._now=lambda:retail.ODA_FIXTURE_NOW
        provider._mutate_cart({'operations':[{'productId':'10','quantity':-1}]})
        retail.CartPlanTests.sync(app,'10',quantity=1)
        app.handle({'operation':'menu','action':'lock','menu_ref':mp.menu_ref(menu),
            'slot_id':menu['slots'][0]['slot_id'],'locked':True})
        prepared=app.handle({'operation':'checkout','action':'prepare','first_meal_date':'2026-09-08'})
        pending=deepcopy(self.store.read()['pending_checkout'])
        app.handle({'operation':'recipes','action':'mark_cooked','menu_id':menu['menu_id'],
            'expected_revision':menu['revision'],'slot_id':menu['slots'][0]['slot_id'],'idempotency_key':'cook-before-confirm'})
        with self.assertRaisesRegex(HouseholdError,'recorded'):
            app.handle({'operation':'checkout','action':'confirm','confirmation_id':prepared['confirmation_id']})
        self.assertEqual(browser.checkout_clicks,0)
        with self.assertRaisesRegex(HouseholdError,'recorded'):
            app._recovery_dispatch_state_guard(self.store.read(),pending,{}, {}, '',None,claim=True)
        # An already-dispatched purchase can still be verified after this outcome.
        # Preserve success while refusing to redate cooking or enqueue recipes.
        with self.store.locked() as state:
            app._record_order_snapshot(state,pending,'already-dispatched')
        state=self.store.read()
        snapshot=state['order_snapshots']['already-dispatched']
        self.assertEqual(snapshot['calendar_status'],'needs_start_date_review')
        self.assertTrue(mp.relative(snapshot))
        self.assertEqual(state['menu_planning']['locks'][mp.lock_key(snapshot)],[menu['slots'][0]['slot_id']])
        self.assertEqual(snapshot['slots'],menu['slots'])
        self.assertFalse(any(j.get('order_id')=='already-dispatched' for j in state.get('email_jobs',[])))

    def test_first_meal_date_anchors_first_retained_day(self):
        menu=self.saved()
        menu=self.app.handle({'operation':'menu','action':'edit_slots','menu_ref':mp.menu_ref(menu),
            'idempotency_key':'shift-days','edits':[
                {'action':'move','slot_id':menu['slots'][0]['slot_id'],'day':3},
                {'action':'move','slot_id':menu['slots'][1]['slot_id'],'day':5}]})['menu']
        delivery={'slot':{'start_at':'2026-10-20T07:00:00+02:00','end_at':'2026-10-20T13:00:00+02:00'}}
        bound=mp.bind_calendar(menu,'2026-10-20',delivery)
        self.assertEqual([s['date'] for s in bound['slots']],['2026-10-20','2026-10-22'])
        self.assertEqual(bound['planning_scope']['dates'],['2026-10-20','2026-10-22'])
        menu=self.app.handle({'operation':'menu','action':'add_slot','menu_ref':mp.menu_ref(menu),
            'idempotency_key':'early-lunch','slot_input':{'day':1,'meal_type':'lunch','portions':1,
                'reference':menu['slots'][0]['reference']}})['menu']
        bound=mp.bind_calendar(menu,'2026-10-20',delivery)
        self.assertEqual(bound['planning_scope']['dates'],['2026-10-22','2026-10-24'])
        self.assertEqual(min(s['date'] for s in bound['slots']),'2026-10-20')
        with self.store.locked() as state:
            state['menu']=bound
        self.assertNotIn('dinner_day_coverage',str(assess_menu(self.store.read()).get('issues',[])))

    def test_purchase_does_not_order_retired_recipes_of_previous_owner(self):
        import test_meal_concierge as retail
        from service import Application
        original=self.saved()
        removed=original['slots'][0]['recipe_key']
        retained=original['slots'][1]['recipe_key']
        menu=self.app.handle({'operation':'menu','action':'edit_slots','menu_ref':mp.menu_ref(original),
            'idempotency_key':'remove-before-order','edits':[{'action':'remove','slot_id':original['slots'][0]['slot_id']}]})['menu']
        provider=retail.MutableFakeOda(); browser=retail.FakeBrowser(); browser.oda=provider
        app=Application(self.store,provider,browser); app._now=lambda:retail.ODA_FIXTURE_NOW
        provider._mutate_cart({'operations':[{'productId':'10','quantity':-1}]})
        retail.CartPlanTests.sync(app,'10',quantity=1)
        prepared=app.handle({'operation':'checkout','action':'prepare','first_meal_date':'2026-09-08'})
        confirmed=app.handle({'operation':'checkout','action':'confirm','confirmation_id':prepared['confirmation_id']})
        self.assertTrue(confirmed.get('confirmed'),confirmed)
        state=self.store.read()
        prior=state['recipe_usage'][original['menu_id']]
        self.assertEqual(prior['status'],'planned')
        self.assertFalse(prior.get('order_id'))
        self.assertFalse(prior.get('ordered_slot_ids'))
        current=state['recipe_usage'][menu['menu_id']]
        self.assertEqual(current['ordered_slot_ids'],[s['slot_id'] for s in menu['slots']])
        self.assertTrue(app._usage_summary(state,removed,'2026-W37')['eligible'])
        self.assertEqual(app._usage_summary(state,retained,'2026-W37')['blocked_by'][0]['status'],'ordered')

    def test_submit_returns_new_start_question_without_dispatch(self):
        import test_meal_concierge as retail
        from service import Application
        self.saved()
        provider=retail.MutableFakeOda(); browser=retail.FakeBrowser(); browser.oda=provider
        self.store.config['confirmation_policy']='standing'
        app=Application(self.store,provider,browser)
        app._now=lambda:retail.ODA_FIXTURE_NOW
        provider._mutate_cart({'operations':[{'productId':'10','quantity':-1}]})
        retail.CartPlanTests.sync(app,'10',quantity=1)
        app.handle({'operation':'checkout','action':'prepare','first_meal_date':'2026-09-08'})
        question={'status':'needs_input','reason':'first_meal_date_required','reprepared':True,'confirmed':False}
        with mock.patch.object(app,'_checkout_confirm',return_value=question):
            result=app.handle({'operation':'checkout','action':'submit','idempotency_key':'relative-submit'})
        self.assertEqual(result,question)
        self.assertEqual(browser.checkout_clicks,0)
