"""Optional native-agent journeys against an isolated, network-denied household.

Run with the pinned runtime: python -I tests/household_conversation_probe.py
--root /absolute/fresh/scratch. Requires an authenticated Claude Code client.
Prompts name household goals, never tool calls. Assertions inspect saved outcomes.
"""
from copy import deepcopy
from datetime import date, timedelta
import argparse
import json
from pathlib import Path
import sys
import time

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
import client_package_probe as probe
from core import StateStore
from recipes import RecipeStore
from service import Application, config


class OfflineStore:
    def probe(self):
        return {'protocol_version':'2025-11-25','server':{'name':'synthetic'},'tool_count':25}
    def call(self, *args, **kwargs):
        raise AssertionError('fixture preparation must not shop')


def fixture(root):
    probe.prepare(root)
    store = StateStore(root / 'state', config(root / 'config.json'))
    app = Application(store, OfflineStore(), None)
    app.handle({'operation':'setup','action':'apply','keep_current':True})
    with store.locked() as state:
        state['profile']['recipes']['sources'] = {key:key == 'internal' for key in state['profile']['recipes']['sources']}
        state['profile']['recipes']['web_search']['enabled'] = False
        state['profile']['recipes']['repeat_cooldown_weeks'] = 0
    refs = []
    bank = RecipeStore(root / 'state/recipes.sqlite3',store.config['household'])
    for name, minutes, protein in [('Lentil carrot skillet',20,'cooked lentils'),('Bean and carrot stew',25,'cooked beans'),('Chickpea carrot pasta',30,'cooked chickpeas')]:
        recipe = {'name':name,'language':'en','portions':2,'categories':['dinner'],
                  'times':{'active_minutes':minutes},
                  'ingredients':[{'item':item,'quantity':quantity,'unit':'g','raw':f'{quantity} g {item}','scalable':True}
                                 for item,quantity in [('carrots',300),(protein,400),('wholegrain pasta',160)]],
                  'steps':['Dice the carrots. Simmer in water until tender, about 12 minutes. Cook the pasta according to its packet. Heat the drained cooked pulses with the carrots, then combine with the drained pasta and serve.'],
                  'source':{'kind':'user','relationship':'user_supplied','publisher':'Synthetic test'},
                  'rights':{'storage':'full','credit':'Synthetic fixture'}}
        result = bank.save(recipe)
        refs.append({'recipe_ref':{'id':result['id'],'revision':result['revision']}})
    plan = app.handle({'operation':'menu','action':'plan','planner_input':{'planning_mode':'ad_hoc','dates':[date.today().isoformat()],'selection_mode':'agent','portions':2,'candidates':[refs[0]]}})['plan']
    app.handle({'operation':'menu','action':'save','planner_ref':plan['save_ref']})
    return store.read()['profile'], refs, [bank.get(ref['recipe_ref']['id']) for ref in refs]


def run(root):
    profile, refs, recipes = fixture(root)
    original_command = probe.claude_command
    def command(*args, **kwargs):
        result = original_command(*args, **kwargs)
        result[result.index('--max-turns')+1] = '20'
        return result
    probe.claude_command = command
    def state():
        return json.loads((root / 'state/state.json').read_text())
    def native(name,prompt,**kwargs):
        start = len(probe.records(root)); clock = time.monotonic()
        events = probe.native(root,'claude-code',plugin,name,prompt,**kwargs)
        calls = probe.records(root)[start:]
        probe.append(root / 'journey-metrics.jsonl', {'case':name,'calls':len(calls),'seconds':round(time.monotonic()-clock,2),
            'errors':sum('error' in call for call in calls), 'conversation_log':str(root / (name+'.jsonl'))})
        return events
    with probe.service(root):
        plugin = probe.build('claude-code',root,root / 'claude-package')
        native('report','The lentil carrot skillet in my saved plan took 70 minutes of active work, and the portions were too small. Please remember that experience for future meal choices.')
        reports = [event for event in state()['planning_feedback'] if event['kind']=='experience']
        assert len(reports)==1 and reports[0]['binding']['experience']=={'actual_active_minutes':70,'portion_fit':'too_small'}, reports
        assert state()['profile']==profile
        dates = [(date.today()+timedelta(days=i)).isoformat() for i in (1,2)]
        planning_start = len(probe.records(root))
        native('quick-plan',f'Plan and save two different dinners for two people on {dates[0]} and {dates[1]}, using the carrots we already have. Use my saved recipes and keep active cooking within 30 minutes. Just plan for now; no shopping. You may replace the previous draft. Choose suitable meals without asking me to choose.')
        menu = state()['menu']
        assert [slot['date'] for slot in menu['slots']]==dates
        assert {slot['reference']['recipe_ref']['id'] for slot in menu['slots']}=={ref['recipe_ref']['id'] for ref in refs[1:]}, menu
        assert all('carrot' in json.dumps(recipe['ingredients']) for recipe in menu['dishes'])
        assert any('household_experience' in json.dumps(call.get('result',{})) for call in probe.records(root)[planning_start:] if call['request']['operation']=='recipes')
        assert not any(call['request']['operation'] in {'products','catalog','cart','checkout','orders','email'} for call in probe.records(root))
        native('portion-change','Please change both dinners in my current saved plan to three portions each for guests. Keep the same dates and dishes. This is just for this plan; keep my usual household settings and do not shop.')
        assert all(slot['portions']==3 for slot in state()['menu']['slots'])
        assert [(slot['date'],slot['reference']) for slot in state()['menu']['slots']]==[(slot['date'],slot['reference']) for slot in menu['slots']]
        assert not any(call['request']['operation'] in {'products','catalog','cart','checkout','orders','email'} for call in probe.records(root))
        assert state()['profile']==profile
        bank=RecipeStore(root / 'state/recipes.sqlite3',state()['household'])
        assert [bank.get(ref['recipe_ref']['id']) for ref in refs]==recipes
        assert all(event['kind']=='experience' for event in state()['planning_feedback'])
    # A separate fresh conversation must reconcile the existing interrupted write.
    probe.write_json(root / 'synthetic-cart.json', {'items':[{'product_id':99,'name':'Existing groceries','quantity':2,'price':10}], 'subtotal':20,'delivery':None})
    class Disconnect(Exception):
        pass
    with probe.service(root):
        (root / 'hold-response').touch()
        def interrupt(_events):
            if (root / 'dispatch.jsonl').exists():
                raise Disconnect()
        try:
            native('add-groceries','Add one unit of the store product with ID 10 to my cart. Keep all existing groceries. Do not order or pay.',on_events=interrupt)
        except Disconnect:
            pass
        else:
            raise AssertionError('no interrupted dispatch observed')
        (root / 'release-response').touch()
        probe.wait_for(lambda:any(call['request']['operation']=='cart' and call['request'].get('action')=='change' for call in probe.records(root)))
        native('resume','The previous chat was interrupted while adding one unit of product 10 to my cart. Please check what actually happened and report the cart contents. Preserve the existing groceries; do not add it twice, order or pay.')
        cart=json.loads((root / 'synthetic-cart.json').read_text())
        assert {(row['product_id'],row['quantity']) for row in cart['items']}=={(99,2),(10,1)}
        assert len((root / 'dispatch.jsonl').read_text().splitlines())==1
        assert not state().get('pending_cart_change')
        assert not state().get('pending_checkout') and not state().get('order_snapshots')
        assert state()['profile']==profile
    print(json.dumps({'journeys':'passed','live_store_access':False,'root':str(root)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    args=parser.parse_args()
    assert args.root.is_absolute()
    probe.attestation()
    run(args.root)
