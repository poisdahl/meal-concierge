"""Synthetic Mathem contract and household flow; no customer session or orders."""
from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
import asyncio
from datetime import datetime, timezone
import json
import shutil
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import httpx
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_meal_concierge as existing
from core import HouseholdError, StateStore, cart_summary
from retail_mcp import RetailMcpClient, REQUIRED_TOOLS, normalize_retail_delivery_slots, retail_delivery_slot_date
from product_observations import _mathem_ore, normalize_retail_product_search, parse_package
from recipe_sources import provider_recipe_candidates
from recipes import RecipeError, normalize_recipe
from service import Application, config, validate_schedule
from service_common import email_automation_key
from oda_browser import MathemBrowser, _mathem_receipt_address_script, oda_checkout_amount_minor, _oda_checkout_amount_script, _oda_checkout_amounts_minor, _mathem_checkout_account_script, checkout_delivery_matches, delivery_signature, _mathem_checkout_payment_script


PRODUCTS = {'result': [{'query': 'ägg', 'hasMore': False, 'products': [
    {'id': 10, 'name': 'Ägg', 'description': '6 st', 'price': '29,90 kr', 'availability': True},
]}]}
SLOTS = {'deliveryDate': '2026-09-12', 'slots': [{
    'id': 77, 'openDatetime': '2026-09-12T09:00:00+02:00',
    'closeDatetime': '2026-09-12T12:00:00+02:00', 'price': '19,90 kr',
    'isSelected': False, 'isFull': False, 'isUnavailable': False,
}]}



class SharedRetailCartNavigationTests(unittest.TestCase):
    def browser(self, cls):
        browser = cls.__new__(cls)
        browser._open = mock.Mock()
        browser._settle = mock.Mock()
        browser._invoke = mock.Mock()
        browser._cart_surface = mock.Mock(return_value={'action': 'continue'})
        browser._click_action = mock.Mock()
        return browser

    @unittest.skipUnless(shutil.which('node'), 'Node executes native destination transition')
    def test_mathem_destination_selects_radio_before_its_continuation_label(self):
        harness = r"""
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const node=text=>({innerText:text,disabled:false,checked:false,attrs:{},
 getAttribute(k){return this.attrs[k]??null;},setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];},
 getBoundingClientRect:()=>({width:10,height:10})});
const old=node('Lägg till i din nuvarande beställning 123456 Lör 12. sep 09:00 - 12:00 Exempelvägen 1');
const fresh=node('Skapa en ny beställning Du väljer leveranstid i nästa steg.');
for(const r of [old,fresh])r.labels=[r];old.checked=input.selected;fresh.checked=!input.selected;
const next=node(input.selected?'Fortsätt till betalning':'Fortsätt');
const buttons=input.duplicate?[next,node(next.innerText)]:[next];
const location={href:'https://www.mathem.se/se/checkout/modify/'};
const document={querySelector:()=>null,querySelectorAll:s=>s==='input[type="radio"]'?[old,fresh]:s==='button'?buttons:[]};
const getComputedStyle=()=>({display:'block',visibility:'visible'});
process.stdout.write(JSON.stringify({value:JSON.parse(eval(input.script)),
 radioMarked:[old,fresh].filter(r=>r.attrs['data-mathem-destination']).map(r=>r===old),
 nextMarked:buttons.filter(r=>r.attrs['data-mathem-destination-next']).length}));
"""
        expected = {'order_id': '123456', 'delivery_text': 'Lör 12. sep 09:00 - 12:00',
                    'delivery_address': 'Exempelvägen 1'}
        for existing_order, initially_existing, duplicate in ((False, True, False), (False, False, False),
                (True, False, False), (True, True, False), (False, True, True), (True, False, True)):
            with self.subTest(existing=existing_order, initial=initially_existing, duplicate=duplicate):
                browser = self.browser(MathemBrowser)
                selected = initially_existing
                observed = None
                calls = []
                def evaluate(script):
                    nonlocal observed
                    run = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps({
                        'script': script, 'selected': selected, 'duplicate': duplicate}),
                        text=True, capture_output=True, check=True, timeout=10)
                    observed = json.loads(run.stdout)
                    return observed['value']
                def click(action, selector):
                    nonlocal selected
                    self.assertEqual(action, 'click')
                    calls.append(selector)
                    if selector == '[data-mathem-destination="true"]':
                        self.assertEqual(observed['radioMarked'], [existing_order])
                        selected = existing_order
                    else:
                        self.assertEqual(selected, existing_order)
                        self.assertEqual(observed['nextMarked'], 1)
                browser._eval = evaluate
                browser._invoke = click
                if duplicate:
                    with self.assertRaises(HouseholdError):
                        browser._choose_checkout_destination(expected if existing_order else None)
                else:
                    browser._choose_checkout_destination(expected if existing_order else None)
                self.assertEqual(calls.count('[data-mathem-destination="true"]'), int(initially_existing != existing_order))
                self.assertEqual(calls.count('[data-mathem-destination-next="true"]'), int(not duplicate))

    def test_both_providers_use_one_cart_continue_after_reload(self):
        for cls, origin in ((existing.OdaBrowser, 'https://oda.com/no/'), (MathemBrowser, 'https://www.mathem.se/se/')):
            with self.subTest(provider=cls.checkout_provider):
                b = self.browser(cls)
                self.assertEqual(b._continue_checkout_cart(), 'continue')
                b._open.assert_called_once_with(origin + 'cart/')
                self.assertEqual(b._invoke.call_args_list, [mock.call('reload'), mock.call('snapshot')])
                b._click_action.assert_called_once_with('continue', mouse=True)

    @unittest.skipUnless(shutil.which('node'), 'Node needed for browser boundary fixture')
    def test_actual_cart_script_binds_each_origin_language_and_ambiguity(self):
        harness = r"""
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));let marked=[];
const node=()=>({tagName:input.full?'A':'BUTTON',innerText:input.label,href:input.cart,disabled:false,
 getAttribute:()=>null,setAttribute:(key,value)=>marked.push(value),removeAttribute:()=>{},
 getBoundingClientRect:()=>({width:10,height:10})});
const controls=Array.from({length:input.count},node);
const document={innerText:'Delsumma',querySelector:s=>s==='main'?document:(input.login?{}:null),
 querySelectorAll:s=>s==='button'?(input.full?[]:controls):s==='a'?(input.full?controls:[]):[]};
document.body=document;const location={href:input.url};
const getComputedStyle=()=>({display:'block',visibility:'visible'});
process.stdout.write(JSON.stringify({value:JSON.parse(eval(input.script)),marked}));
"""
        for cls, origin, label in ((existing.OdaBrowser, 'https://oda.com/no/', 'Fortsett'),
                                   (MathemBrowser, 'https://www.mathem.se/se/', 'Fortsätt')):
            b = cls.__new__(cls); b._eval = lambda script: script
            script = b._cart_surface()
            cases = [(origin+'cart/', label, 1, False, False, 'continue'),
                     ('https://wrong.example/se/cart/', label, 1, False, False, 'blocked'),
                     (origin+'cart/', label, 2, False, False, 'blocked'),
                     (origin+'cart/', label, 1, False, True, 'blocked')]
            if cls is MathemBrowser:
                cases.append((origin, 'Fortsätt till varukorgen', 1, True, False, 'full_cart'))
                cases.append((origin, label, 1, False, False, 'wait'))
            for url, text, count, full, login, action in cases:
                with self.subTest(provider=cls.checkout_provider, action=action, url=url):
                    run = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps({
                        'script': script, 'url': url, 'cart': origin+'cart/', 'label': text,
                        'count': count, 'full': full, 'login': login}), text=True, capture_output=True, check=True)
                    result = json.loads(run.stdout)
                    self.assertEqual(result['value']['action'], action)
                    self.assertEqual(len(result['marked']), 0 if action in {'blocked', 'wait'} else 1)

    def test_mathem_storefront_keeps_one_full_cart_then_one_continue(self):
        b = self.browser(MathemBrowser)
        b._open.side_effect = HouseholdError('cart redirected to storefront')
        b._eval = mock.Mock(return_value={'url': 'https://www.mathem.se/se/'})
        b._cart_surface.side_effect = [{'action': 'full_cart'}, {'action': 'full_cart'}, {'action': 'continue'}]
        self.assertEqual(b._continue_checkout_cart(), 'continue')
        self.assertEqual(b._click_action.call_args_list, [mock.call('full-cart'), mock.call('continue', mouse=True)])
        b._open.assert_called_once()

    def test_delayed_mathem_cart_and_modify_never_repeat_continue(self):
        for target in (None, {'checkout_url': MathemBrowser.checkout_url + '?orderNumber=123456'}):
            b = self.browser(MathemBrowser)
            b._continue_checkout_cart = mock.Mock(return_value='continue')
            b._eval = mock.Mock(side_effect=[{'action': 'https://www.mathem.se/se/cart/#continue'},
                {'action': 'https://www.mathem.se/se/cart/#continue'}, {'action': 'modify'},
                {'action': 'modify'}, {'action': 'ready'}])
            b._choose_checkout_destination = mock.Mock()
            b._navigate_to_checkout('123456' if target else None, expected=target)
            b._continue_checkout_cart.assert_called_once()
            b._choose_checkout_destination.assert_called_once_with(target)
            b._invoke.assert_not_called()
            b._open.assert_not_called()

    def test_lost_continue_expiry_and_ambiguous_cart_stop_without_retry(self):
        for failure in ('lost_continue', 'expiry', 'ambiguous', 'stale_full_cart'):
            b = self.browser(MathemBrowser)
            b._eval = mock.Mock(side_effect=AssertionError('no subsequent navigation'))
            if failure == 'lost_continue': b._click_action.side_effect = HouseholdError('mouse up response lost')
            elif failure == 'expiry': b._settle.side_effect = HouseholdError('checkout deadline reached')
            elif failure == 'ambiguous': b._cart_surface.return_value = {'action': 'blocked'}
            else: b._cart_surface.return_value = {'action': 'full_cart'}
            with self.subTest(failure=failure), self.assertRaises(HouseholdError): b._navigate_to_checkout()
            self.assertLessEqual(b._click_action.call_count, 1)
            b._eval.assert_not_called()

    def test_timeout_distinguishes_storefront_before_and_after_destination(self):
        for modified in (False, True):
            b = self.browser(MathemBrowser)
            b._continue_checkout_cart = mock.Mock(return_value='continue')
            states = ([{'action': 'modify', 'route': 'modify'}] if modified else [])
            b._eval = mock.Mock(side_effect=states + [{'action': 'wait', 'route': 'storefront'}] * 60)
            b._choose_checkout_destination = mock.Mock()
            with self.assertRaises(HouseholdError) as error:
                b._navigate_to_checkout('private-order', expected={'checkout_url': MathemBrowser.checkout_url+'?orderNumber=private-order'})
            message = str(error.exception)
            self.assertIn('cart:continue-dispatched', message)
            self.assertIn('storefront:wait', message)
            self.assertEqual('modify:destination-dispatched' in message, modified)
            self.assertNotIn('private-order', message)
            self.assertEqual(b._choose_checkout_destination.call_count, int(modified))
            b._invoke.assert_not_called()

class MathemDietaryDetailTests(unittest.TestCase):
    # Visible row labels from Mathem's public product 4694, 2026-09-07.
    HTML = ('<div>Ingredienser</div><div>PASTA AV DURUMVETE.</div>'
            '<div>Allergener</div><div>Spannmål som innehåller gluten</div>'
            '<div>Kan innehålla spår av</div><div>Sojabönor, Senap</div>'
            '<div>Tillverkningsland</div><div>Italien</div>').encode()

    def test_exact_mathem_detail_and_provider_redirect_binding(self):
        response = mock.Mock(status=301)
        connection = mock.Mock(); connection.getresponse.return_value = response
        with tempfile.TemporaryDirectory() as root:
            client = RetailMcpClient(root, provider='mathem')
            for location, accepted in [
                ('/se/products/4694-barilla-pasta-fusilli/', True),
                ('https://oda.com/no/products/4694-pasta/', False),
                ('/se/products/5627-pasta/', False),
                ('/se/products/4694-pasta/?unbound=1', False),
            ]:
                response.getheader.return_value = location
                with self.subTest(location=location), mock.patch('recipe_import_sources._PinnedConnection', return_value=connection) as connect, mock.patch('recipe_import_sources._get_bytes', return_value=(self.HTML, 'text/html')) as fetch:
                    result = client.product_dietary_evidence('4694')
                    connect.assert_called_once_with('www.mathem.se', 443, tls=True, public_only=True)
                    if accepted:
                        self.assertEqual(result['ingredients'], 'PASTA AV DURUMVETE.')
                        self.assertEqual(result['allergens'], 'Spannmål som innehåller gluten')
                        self.assertEqual(result['may_contain'], 'Sojabönor, Senap')
                        self.assertEqual(result['source_url'], 'https://www.mathem.se' + location)
                        fetch.assert_called_once()
                    else:
                        self.assertIn('unavailable', result); fetch.assert_not_called()

    def test_missing_duplicate_and_script_rows_do_not_become_evidence(self):
        from dietary_assessment import parse_retail_product_page
        raw = b'<script>Allergener\nmilk</script><div>Ingredienser</div><div>x</div><div>Ingredienser</div><div>y</div>'
        result = parse_retail_product_page(raw, 'https://www.mathem.se/se/products/4694/', provider='mathem')
        self.assertEqual(set(result), {'source_url'})


class SharedRetailBindingTests(unittest.TestCase):
    # Labels/origins were separately read from the two authenticated services
    # on 2026-09-08. Synthetic addresses here are never provider evidence.
    EXAMPLES = (
        ('oda', 'https://oda.com/no/', 'Total inkl. MVA', 'Eksempelveien 1', 'NOK'),
        ('mathem', 'https://www.mathem.se/se/', 'Totalt inkl. moms', 'Exempelvägen 8', 'SEK'),
    )

    @unittest.skipUnless(shutil.which('node'), 'Node required for DOM contract')
    def test_receipt_binding_has_exact_provider_order_address_and_structure(self):
        from oda_browser import _receipt_address_script
        harness = r"""
const {script,order,address,label,url,cases}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const results=[];
for(const c of cases){
 const el=(text,kind)=>({innerText:text,children:[],closest:q=>kind==='main'?(c.dialogMain?{}:null):(c.excludedAddress&&kind==='address'?{}:null),getBoundingClientRect:()=>({width:10,height:10})});
 const p=el(c.wrongAddress?'Other address':address,'address');if(c.hiddenAddress)p.hidden=true;
 const total=el(c.wrongLabel?'Other total':label,'total');
 const ps=c.duplicateAddress?[p,p]:[p], totals=c.duplicateTotal?[total,total]:[total];
 const main=el((c.wrongOrder?'prefix'+order:order)+' '+address+' '+label,'main');
 main.querySelectorAll=s=>s==='p'?ps:[...ps,...totals];
 global.getComputedStyle=e=>({display:e.hidden?'none':'block',visibility:'visible'});
 global.location={href:c.page||url};global.document={querySelector:()=>c.login||null,querySelectorAll:()=>c.duplicateMain?[main,main]:[main]};
 results.push(JSON.parse(eval(script)).address_verified);
}process.stdout.write(JSON.stringify(results));
"""
        for provider, store, label, address, _currency in self.EXAMPLES:
            with self.subTest(provider=provider):
                order = 'synthetic-123'; url = store + 'account/orders/' + order + '/'
                # Provider-like text inside data must survive interpolation.
                address += ' /se/ https://www.mathem.se Totalt inkl. moms ADDRESS TOTAL_LABEL'
                script = _receipt_address_script(order, address, provider=provider)
                cases = [{}, {'wrongAddress': True}, {'wrongOrder': True}, {'wrongLabel': True},
                         {'duplicateAddress': True}, {'duplicateTotal': True}, {'duplicateMain': True},
                         {'hiddenAddress': True}, {'excludedAddress': True}, {'dialogMain': True},
                         {'login': True}, {'page': url + '?other=1'},
                         {'page': url.replace('https://', 'https://wrong.example/') }]
                result = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps(
                    dict(script=script, order=order, address=address, label=label, url=url, cases=cases)),
                    text=True, capture_output=True, check=True, timeout=10)
                self.assertEqual(json.loads(result.stdout), [True] + [False] * (len(cases)-1))

    @unittest.skipUnless(shutil.which('node'), 'Node required for account contract')
    def test_account_reference_is_bound_to_its_provider_and_visible_edit_link(self):
        from oda_browser import _checkout_account_script
        harness = r"""
const {script,page,link}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
const results=[];
for(const [url,href,login] of [[page,link,false],[page,link.replace('/123/','/999/'),false],[page,link+'?other=1',false],[page,link,true],[page+'?other=1',link,false]]){
 global.location={href:url};global.document={querySelector:()=>login||null,querySelectorAll:()=>[{href,getBoundingClientRect:()=>({width:1,height:1})}]};results.push(JSON.parse(eval(script)).account_matches);
}process.stdout.write(JSON.stringify(results));
"""
        for provider, store, _label, _address, _currency in self.EXAMPLES:
            with self.subTest(provider=provider):
                result=subprocess.run([shutil.which('node'),'-e',harness],input=json.dumps({
                    'script': _checkout_account_script(123,provider=provider), 'page':store+'account/delivery/',
                    'link':store+'account/delivery/edit/123/'}),text=True,capture_output=True,check=True,timeout=10)
                self.assertEqual(json.loads(result.stdout),[True,False,False,False,False])

    def test_frozen_original_reference_survives_selection_change_but_not_account_change(self):
        from oda_browser import OdaBrowser
        import hashlib,time
        for provider,store,_label,address,currency in self.EXAMPLES:
            with self.subTest(provider=provider):
                native=(OdaBrowser if provider=='oda' else MathemBrowser).__new__(OdaBrowser if provider=='oda' else MathemBrowser)
                native.provider_client=mock.Mock(provider=provider)
                original={'id':123,'address':address,'isSelected':False}
                native.provider_client.call.return_value={'result':[original,{'id':456,'address':'Different selected address','isSelected':True}]}
                native._open=mock.Mock();native._settle=mock.Mock()
                native._eval=mock.Mock(side_effect=[{'address_verified':True},{'address_verified':False},{'account_matches':True}])
                order={'orderNumber':'synthetic-123','currency':currency}
                deadline=time.monotonic()+60
                binding=native._read_order_binding('synthetic-123',order,deadline=deadline)
                self.assertEqual(binding,{'account_reference_digest':hashlib.sha256(b'123').hexdigest(),'receipt_address':address})
                native._eval=mock.Mock(side_effect=[{'address_verified':True},{'account_matches':True}])
                self.assertEqual(native._read_order_binding('synthetic-123',order,deadline=deadline,expected_binding=binding),binding)
                self.assertEqual(native._open.call_args.args[0],store+'account/delivery/')
                native.provider_client.call.return_value={'result':[{**original,'id':789}]};native._eval.reset_mock()
                with self.assertRaisesRegex(HouseholdError,'original order account binding'):
                    native._read_order_binding('synthetic-123',order,deadline=deadline,expected_binding=binding)
                native._eval.assert_not_called()
                native.provider_client.provider='mathem' if provider=='oda' else 'oda';native.provider_client.call.reset_mock()
                with self.assertRaisesRegex(HouseholdError,'mismatched'):
                    native._read_order_binding('synthetic-123',order,deadline=deadline)
                native.provider_client.call.assert_not_called()

    def test_legacy_uncertain_change_without_binding_survives_restart_without_dispatch(self):
        from order_operations import OrderOperations
        for provider,*_ in self.EXAMPLES:
            with self.subTest(provider=provider),tempfile.TemporaryDirectory() as temp:
                settings={**existing.CONFIG,'provider':provider}
                store=StateStore(Path(temp),settings)
                pending={'status':'uncertain','confirmation_id':'legacy-change','order_change':{'order_id':'synthetic-123'}}
                with store.locked() as state: state['pending_checkout']=deepcopy(pending)
                before=store.path.read_bytes() if hasattr(store,'path') else (Path(temp)/'state.json').read_bytes()
                app=OrderOperations();app.provider=provider;app.store=StateStore(Path(temp),settings);app.browser=mock.Mock();app._orders=mock.Mock()
                with self.assertRaisesRegex(HouseholdError,'Original order account binding'):
                    app._order_change_reconcile(pending)
                self.assertEqual(app.store.read()['pending_checkout'],pending)
                self.assertEqual((Path(temp)/'state.json').read_bytes(),before)
                app._orders.assert_not_called();app.browser.submit_order_change.assert_not_called()

    def test_legacy_cancellation_review_cannot_reach_a_dispatch(self):
        from oda_browser import OdaBrowser, CancellationPreconditionError
        from contextlib import contextmanager
        @contextmanager
        def operation(_deadline):yield {'final_dispatched':False}
        browser=OdaBrowser.__new__(OdaBrowser);browser._cancellation_operation=operation
        browser._review_cancellation=mock.Mock();browser._invoke=mock.Mock()
        with self.assertRaises(CancellationPreconditionError):
            browser.submit_cancellation('synthetic-123',{}, {'available':True})
        browser._review_cancellation.assert_not_called();browser._invoke.assert_not_called()



class OdaFinalBindingTests(unittest.TestCase):
    def test_cancellation_binding_first_open_keeps_cancellation_launch_flags(self):
        from oda_browser import OdaBrowser, CANCELLATION_BROWSER_ARGS
        browser=OdaBrowser(instance='synthetic',binary='/synthetic/browser',executable='/synthetic/chromium',
            profile='/synthetic/profile',home='/synthetic/home',socket_directory='/synthetic/socket',uid=10001,gid=10002,
            provider_client=mock.Mock(provider='oda'))
        browser.provider_client.call.return_value={'result':[{'id':123,'address':'Eksempelveien 1'}]}
        browser._clear_cancellation_cache=mock.Mock()
        calls=[]
        def run(command, **kw):
            calls.append((command,kw['env']['AGENT_BROWSER_ARGS']))
            if 'open' in command:
                data={'url':command[-1]}
            elif 'eval' in command:
                key='address_verified' if 'address_verified' in kw['input'] else 'account_matches'
                data={'result':json.dumps({key:True})}
            else:data={}
            return SimpleNamespace(returncode=0,stdout=json.dumps({'success':True,'data':data}))
        with mock.patch('oda_browser.subprocess.run',side_effect=run):
            with browser._cancellation_operation():
                browser._read_order_binding('123456',{'orderNumber':'123456','currency':'NOK'},deadline=browser._cancellation_deadline)
        self.assertEqual(len([cmd for cmd,args in calls if 'open' in cmd]),2)
        self.assertTrue(all(args==CANCELLATION_BROWSER_ARGS for cmd,args in calls))

    def test_same_postal_address_changed_selected_reference_stops_new_checkout(self):
        from oda_browser import OdaBrowser
        from core import CheckoutPreconditionError
        import hashlib
        browser=OdaBrowser.__new__(OdaBrowser)
        review={'account_reference_digest':hashlib.sha256(b'123').hexdigest(),'surface':{}}
        browser.review_checkout=lambda cart:dict(review)
        browser._cart_expectation=lambda cart:{'delivery_address':'Eksempelveien 1','total_minor':100,'product_count':1}
        browser._account_reference=mock.Mock(return_value=789)
        browser._click_checkout_submit=mock.Mock()
        with self.assertRaisesRegex(CheckoutPreconditionError,'selected account changed'):
            browser._submit_checkout({},review)
        browser._click_checkout_submit.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'Node executes actual atomic checkout guards')
    def test_new_checkout_and_addition_recheck_entire_review_after_callback(self):
        from oda_browser import OdaBrowser, _oda_checkout_surface_script, CHECKOUT_URL
        from core import CheckoutPreconditionError
        import hashlib
        harness=r"""
const {script,change,url}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class E {
 constructor(tag,text='',children=[]){this.tag=tag;this.text=text;this.children=children;for(const c of children)c.parentElement=this;}
 get innerText(){return this.text||this.children.map(c=>c.innerText).join('\n');}
 getBoundingClientRect(){return {width:100,height:20};} getAttribute(){return null;}
 contains(n){return this===n||this.children.some(c=>c.contains(n));}
 matches(s){return s==='*'||s===this.tag||(s===`input[type="${this.type}"]`&&this.tag==='input');}
 querySelectorAll(s){return this.children.flatMap(c=>[...(s.split(',').some(x=>c.matches(x))?[c]:[]),...c.querySelectorAll(s)]);}
 querySelector(s){return this.querySelectorAll(s)[0]||null;}
 closest(s){return s.split(',').some(x=>this.matches(x))?this:this.parentElement?.closest(s)||null;}
 click(){this.clicks=(this.clicks||0)+1;}
}
global.getComputedStyle=()=>({display:'block',visibility:'visible'});global.location={href:change==='url'?url+'other':url};
const quantity=new E('input');quantity.type='number';quantity.value=change==='quantity'?2:1;
const item=new E('article','',[new E('p',change==='item'?'Ris':'Pasta'),new E('p','500 g, Sopps'),new E('label','Antall'),quantity]);
const delivery=new E('section','',[new E('h2','Vi leverer varene dine'),new E('p',change==='delivery'?'12. september 12:00–15:00':'12. september 09:00–12:00'),new E('p',['address','spoof'].includes(change)?'Annen vei 2':'Eksempelveien 1')]);
const rows=url.includes('orderNumber=')?[['Opprinnelig bestilling','1 vare','100,00 kr'],['Nye varer lagt til','1 vare','45,50 kr'],['Å betale',change==='amount'?'46,50 kr':'45,50 kr'],['Ny totalsum','2 varer','145,50 kr']]:[['1 vare','26,50 kr'],['Delsum','26,50 kr'],['Levering',change==='amount'?'20,00 kr':'19,00 kr'],['Total inkl. MVA','45,50 kr']];
const summary=new E('section','',rows.map(parts=>new E('div','',parts.map(x=>new E('span',x)))));
const pay=new E('button','Bekreft og betal 45,50 kr');pay.disabled=change==='disabled';
const card=new E('input');card.type='radio';card.checked=change!=='selection';
const cardLabel=new E('label','',[new E('span',change==='card'?'•••• 5678':'•••• 1234'),card]);card.labels=[cardLabel];
const vipps=new E('input');vipps.type='radio';vipps.checked=change==='selection';
const vippsLabel=new E('label','',[new E('span','Vipps'),vipps]);vipps.labels=[vippsLabel];
global.document=new E('document','',[new E('body','',[item,delivery,new E('p',change==='spoof'?'Eksempelveien 1':''),cardLabel,vippsLabel,summary,pay])]);document.body=document.children[0];
if(change==='login'){const old=document.querySelector.bind(document);document.querySelector=s=>s.includes('input[type="password"]')?{}:old(s);}
const result=JSON.parse(eval(script));process.stdout.write(JSON.stringify({result,clicks:pay.clicks||0}));
"""
        expected={'delivery_address':'Eksempelveien 1','delivery_text':'12. september 09:00–12:00','total_minor':4550,'product_count':1,'lines':[]}
        amounts={'product_subtotal':26.5,'delivery_price':19,'discounts':None,'deposits':None,'bags':None,'other_fees':None,'provider_total':45.5}
        binding={'account_reference_digest':hashlib.sha256(b'123').hexdigest(),'receipt_address':'Eksempelveien 1'}
        def evaluate(script,url,change=None):
            result=subprocess.run([shutil.which('node'),'-e',harness],input=json.dumps(dict(script=script,url=url,change=change)),capture_output=True,text=True,check=True,timeout=10)
            return json.loads(result.stdout)
        for addition in (False,True):
            url=CHECKOUT_URL+('?orderNumber=123456' if addition else '')
            surface=evaluate(_oda_checkout_surface_script(expected),url)['result']
            self.assertTrue(surface['authenticated'] and surface['address_matches'] and surface['total_matches'] and surface['masked_payment'])
            self.assertFalse(evaluate(_oda_checkout_surface_script(expected),url,'spoof')['result']['address_matches'])
            review={'surface':surface,'amounts':amounts,'binding':binding,'account_reference_digest':binding['account_reference_digest']}
            review=json.loads(json.dumps(review,sort_keys=True))
            for change in (None,'card','selection','quantity','item','address','spoof','delivery','amount','disabled','login','url'):
                with self.subTest(addition=addition,change=change):
                    browser=OdaBrowser.__new__(OdaBrowser);browser._checkout_deadline=None
                    browser._invoke=mock.Mock();browser._account_reference=lambda address:123
                    browser._cart_expectation=lambda cart:expected;browser._order_cart=lambda *args:{}
                    browser._addition_expectation=lambda *args:{**expected,'checkout_url':url,'original_minor':10000,'original_count':1}
                    browser.review_checkout=lambda cart:deepcopy(review)
                    browser.review_order_change=lambda *a,**kw:deepcopy(review)
                    callback=[];observed=[]
                    def final_eval(script):
                        self.assertEqual(callback,[True]);result=evaluate(script,url,change);observed.append(result);return result['result']
                    browser._eval=final_eval
                    def submit():
                        if addition:browser.submit_order_change({},'123456',{},review,lambda:callback.append(True))
                        else:browser._submit_checkout({},review,lambda:callback.append(True))
                    if change:
                        with self.assertRaises(CheckoutPreconditionError):submit()
                    else:submit()
                    self.assertEqual(observed,[{'result':{'clicked':change is None},'clicks':0 if change else 1}])

class SharedRetailOrderCountTests(unittest.TestCase):
    def test_current_order_shapes_use_product_quantities(self):
        # Independent authenticated 2026-09-08 reads from both MCP 1.1.0
        # servers omit productQuantityCount. These small synthetic examples
        # test the observed shape, not live purchase or service parity.
        from oda_browser import OdaBrowser
        samples = [(OdaBrowser, "NOK", [2.0, 1.0], 3),
                   (MathemBrowser, "SEK", [1.0, 3.0], 4)]
        for browser, currency, quantities, expected in samples:
            order = {"currency": currency, "products": [{"quantity": q} for q in quantities]}
            with self.subTest(provider=browser.checkout_provider):
                self.assertEqual(browser._order_product_count(order), expected)
                self.assertEqual(browser._order_product_count({**order, "productQuantityCount": expected}), expected)
                for count in (True, expected + 1, str(expected)):
                    with self.assertRaises(HouseholdError):
                        browser._order_product_count({**order, "productQuantityCount": count})
                for quantity in (True, 0, -1, 1.5, float("inf"), float("nan"), "2"):
                    with self.assertRaises(HouseholdError):
                        browser._order_product_count({"products": [{"quantity": quantity}]})
                with self.assertRaises(HouseholdError):
                    browser._order_product_count({"products": []})


class SharedRetailReconciliationTests(unittest.TestCase):
    @mock.patch("service.now", new=lambda: existing.ODA_FIXTURE_NOW)
    def test_checkout_receipt_fills_only_missing_address_with_frozen_account(self):
        for provider in ('oda','mathem'):
            for address_field in (None,'deliveryAddress','delivery_address'):
                with self.subTest(provider=provider,address_field=address_field):
                    case=existing.FlowTests() if provider=='oda' else MathemGuardedCheckoutTests()
                    case.setUp()
                    try:
                        app=case.app;browser=case.browser;shop=case.oda if provider=='oda' else case.shop
                        original_review=browser.review_checkout
                        browser.review_checkout=lambda cart,**kw:{**original_review(cart,**kw),'account_reference_digest':'a'*64}
                        original_submit=browser.submit_checkout
                        def submit(cart,review,before_click,**kw):
                            if provider=='oda': original_submit(cart,review,before_click,**kw)
                            else:
                                before_click();browser.checkout_clicks+=1;shop.orders.append(case.order())
                            order=shop.orders[-1];order.pop('deliveryAddress',None)
                            if address_field:order[address_field]='Wrong address'
                        browser.submit_checkout=submit
                        reader=mock.Mock(wraps=browser.read_order_binding);browser.read_order_binding=reader
                        prepared=app.handle({'operation':'checkout','action':'prepare'})
                        result=app.handle({'operation':'checkout','action':'confirm','confirmation_id':prepared['confirmation_id']})
                        self.assertEqual(result['confirmed'],address_field is None)
                        self.assertEqual(browser.checkout_clicks,1)
                        if address_field:
                            reader.assert_not_called();self.assertFalse(result['retry_allowed'])
                            before=case.store.read()['pending_checkout']
                            again=app.handle({'operation':'checkout','action':'reconcile','confirmation_id':prepared['confirmation_id']})
                            self.assertFalse(again['confirmed']);self.assertEqual(case.store.read()['pending_checkout']['confirmation_id'],before['confirmation_id'])
                        else:reader.assert_called_once()
                        self.assertEqual(browser.checkout_clicks,1)
                    finally:
                        if provider=='oda':case.tearDown()
                        else:case.doCleanups()

    def test_legacy_uncertain_cancellation_preserves_state_across_restart(self):
        for provider in ('oda','mathem'):
            with self.subTest(provider=provider),tempfile.TemporaryDirectory() as temp:
                from order_operations import OrderOperations
                from contextlib import nullcontext
                settings={**existing.CONFIG,'provider':provider}
                store=StateStore(Path(temp),settings)
                pending={'status':'uncertain','confirmation_id':'legacy-cancel','order_id':'123456','browser':{'available':True}}
                with store.locked() as state:state['pending_cancellation']=deepcopy(pending)
                before=(Path(temp)/'state.json').read_bytes()
                app=OrderOperations();app.provider=provider;app.store=StateStore(Path(temp),settings)
                app._browser_operation=lambda deadline:nullcontext();app._read_protected_result=lambda *a:None
                app._orders=mock.Mock(return_value={'order':{},'tracking':{'status':'cancelled'}});app.browser=mock.Mock()
                with self.assertRaisesRegex(HouseholdError,'Original order account binding'):
                    app._cancel_reconcile(confirmation_id='legacy-cancel')
                self.assertEqual((Path(temp)/'state.json').read_bytes(),before)
                app.browser.read_order_binding.assert_not_called();app.browser.submit_cancellation.assert_not_called()

    def test_delivery_reconciliation_binds_full_date_and_currency(self):
        from contextlib import contextmanager
        from order_operations import OrderOperations
        class Store:
            def __init__(self, pending): self.state = {"pending_checkout": deepcopy(pending)}
            @contextmanager
            def locked(self): yield self.state
        class Probe(OrderOperations):
            def _orders(self, request): return deepcopy(self.current)
            def _record_order_snapshot(self, *args): pass
            def _store_protected_result(self, *args, **kwargs): pass
        # Separate shapes/amounts/dates from each provider; synthetic adverse
        # outcomes never interrupt a real payment or change a retailer order.
        for provider, currency, day, display, total in [
            ("oda", "NOK", "2026-09-12", "Hjemlevering mellom kl 09 og 12, 12. sep", 948.05),
            ("mathem", "SEK", "2026-09-10", "Tor 10. sep 14:00 - 16:00", 582.51),
        ]:
            before = {"orderNumber": "synthetic-order", "currency": currency, "grossAmount": total,
                      "deliveryDate": "2026-09-13", "deliverySlotDisplay": "13. sep 14:00 - 16:00",
                      "deliveryAddressId": 7, "products": [{"product": {"id": 10}, "quantity": 2}]}
            requested = {"display": display, "slot": {"slot_ref": provider + ":" + day + ":77",
                         "provider_slot_id": 77, "start_at": day + ("T07:00:00Z" if provider == "oda" else "T12:00:00Z"),
                         "end_at": day + ("T10:00:00Z" if provider == "oda" else "T14:00:00Z"), "price_ore": 0,
                         "price_kind": "exact", "selected": True}}
            pending = {"confirmation_id": "synthetic-confirmation", "status": "uncertain",
                       "summary": {"items": [], "total": 0},
                       "order_change": {"order_id": "synthetic-order", "before": {"order": before},
                                        "requested_delivery": requested, "binding": {"account_reference_digest": "a" * 64, "receipt_address": "Synthetic"}}}
            for drift in ({}, {"currency": "SEK" if provider == "oda" else "NOK"},
                          {"currency": None}, {"deliveryDate": day.replace("2026", "2027")}):
                with self.subTest(provider=provider, drift=drift):
                    app = Probe(); app.provider = provider; app.store = Store(pending)
                    app.browser = mock.Mock()
                    app.current = {"order": {**before, "deliveryDate": day, "deliverySlotDisplay": display, **drift},
                                   "tracking": {"order_id": "synthetic-order", "status": "paid_and_modifiable"}}
                    result = app._order_change_reconcile(pending)
                    self.assertEqual(result["confirmed"], not drift)
                    self.assertFalse(result["retry_allowed"])
                    self.assertEqual(app.store.state["pending_checkout"] is None, not drift)
                    if drift: app.browser.read_order_binding.assert_not_called()



class OdaPreparedDeliveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node executes the actual final browser script')
    def test_confirmation_does_not_reselect_and_rechecks_delivery_card_after_callback(self):
        from oda_browser import OdaBrowser, _oda_delivery_change_surface_script, CHECKOUT_URL
        from core import CheckoutPreconditionError
        browser = OdaBrowser.__new__(OdaBrowser)
        browser._checkout_dispatch_tab = lambda: None
        browser._checkout_deadline = None
        browser._open_order = mock.Mock(side_effect=AssertionError('confirm must not navigate'))
        current_page = None
        def invoke(action, *args, **kwargs):
            nonlocal current_page
            self.assertEqual(action, 'close')  # Operation entry clears the page.
            current_page = None
        def open_review(url):
            nonlocal current_page
            self.assertEqual(url, CHECKOUT_URL + '?orderNumber=123456')
            self.assertIsNone(current_page)
            current_page = url
        browser._open = mock.Mock(side_effect=open_review)
        browser._settle = mock.Mock()
        browser._invoke = mock.Mock(side_effect=invoke)
        browser._expand_checkout_amount_summary = mock.Mock()
        amounts = {'product_subtotal': 0, 'delivery_price': 19, 'discounts': None,
                   'deposits': None, 'bags': None, 'other_fees': None, 'provider_total': 19}
        browser._read_checkout_amounts = mock.Mock(return_value=amounts)
        order = {'orderNumber': '123456', 'currency': 'NOK', 'grossAmount': '948.05',
                 'deliverySlotDisplay': 'Hjemlevering mellom kl 14 og 16, 13. sep',
                 'products': [{'quantity': 2}]}
        delivery = {'slot_id': 77, 'display': 'Hjemlevering mellom kl 09 og 12, 12. sep',
                    'slot': existing.FakeOda().delivery_slots['slots'][1]}
        # Synthetic Oda DOM exercises control flow and guards. It is not a
        # claim about the currently logged-out account's live payment layout.
        harness = r"""
const {script,change}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class E {
 constructor(tag,text='',children=[]){this.tag=tag;this.text=text;this.children=children;for(const c of children)c.parentElement=this;}
 get innerText(){return this.text||this.children.map(c=>c.innerText).join('\n');}
 getBoundingClientRect(){return {width:100,height:20};}
 getAttribute(){return null;}
 contains(n){return this===n||this.children.some(c=>c.contains(n));}
 matches(s){return s==='*'||s===this.tag||(s===`input[type="${this.type}"]`&&this.tag==='input');}
 querySelectorAll(s){return this.children.flatMap(c=>[...(s.split(',').some(x=>c.matches(x))?[c]:[]),...c.querySelectorAll(s)]);}
 querySelector(s){return this.querySelectorAll(s)[0]||null;}
 closest(s){return s.split(',').some(x=>this.matches(x))?this:this.parentElement?.closest(s)||null;}
 click(){this.clicks=(this.clicks||0)+1;}
}
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
global.location={href:'https://oda.com/no/checkout/confirm/?orderNumber='+(change==='order'?'999999':'123456')};
const delivery=new E('section','',[new E('h2','Vi leverer varene dine'),new E('p',change==='delivery'?'12. september 12:00–15:00':'12. september 09:00–12:00')]);
const rows=[['Opprinnelig bestilling','2 varer','948,05 kr'],['Å betale','19,00 kr'],['Ny totalsum','2 varer',change==='amount'?'967,06 kr':'967,05 kr']];
const summary=new E('section','',rows.map(parts=>new E('div','',parts.map(x=>new E('span',x)))));
const pay=new E('button','Bekreft og betal 19,00 kr');pay.disabled=change==='disabled';
const radio=new E('input');radio.type='radio';radio.checked=change!=='unselected';
const label=new E('label','',[new E('span',change==='card'?'•••• 5678':'•••• 1234'),radio]);radio.labels=[label];
global.document=new E('document','',[new E('body','',[delivery,new E('p','Unrelated card •••• 1234'),label,summary,pay])]);document.body=document.children[0];
if(change==='login'){const old=document.querySelector.bind(document);document.querySelector=s=>s==='input[type="password"]'?{}:old(s);}
const result=JSON.parse(eval(script));process.stdout.write(JSON.stringify({result,clicks:pay.clicks||0}));
"""
        def evaluate(script, change=None):
            result = subprocess.run([shutil.which('node'), '-e', harness],
                input=json.dumps({'script': script, 'change': change}), capture_output=True, text=True, check=True, timeout=10)
            return json.loads(result.stdout)
        surface = evaluate(_oda_delivery_change_surface_script(CHECKOUT_URL + '?orderNumber=123456'))['result']
        binding = {'account_reference_digest': 'a' * 64, 'receipt_address': 'Eksempelveien 1'}
        browser._read_order_binding = mock.Mock(return_value=binding)
        browser._eval = lambda script: evaluate(script)['result']
        review = browser._delivery_change_review('123456', order, delivery, surface, binding)
        # Real state serialization reorders dict keys. Values still match.
        review = json.loads(json.dumps(review, sort_keys=True))
        for drift in (None, 'order', 'delivery', 'card', 'amount', 'disabled', 'login', 'unselected'):
            with self.subTest(drift=drift):
                after_callback = False
                calls = []
                def before_click():
                    nonlocal after_callback
                    after_callback = True
                def evaluate_current(script):
                    self.assertEqual(current_page, CHECKOUT_URL + '?orderNumber=123456')
                    result = evaluate(script, drift if after_callback else None)
                    calls.append(result)
                    return result['result']
                browser._eval = evaluate_current
                if drift:
                    with self.assertRaises(CheckoutPreconditionError):
                        browser.submit_delivery_change('123456', order, delivery, review, before_click)
                else:
                    browser.submit_delivery_change('123456', order, delivery, review, before_click)
                self.assertEqual(sum(call['clicks'] for call in calls), 0 if drift else 1)
                self.assertTrue(after_callback)
                browser._open_order.assert_not_called()
                browser._open.assert_called_with(CHECKOUT_URL + '?orderNumber=123456')
        browser._eval = mock.Mock(return_value={'action': 'wait'})
        callback = mock.Mock()
        with self.assertRaises(CheckoutPreconditionError):
            browser.submit_delivery_change('123456', order, delivery, review, callback)
        callback.assert_not_called()
        browser._eval = mock.Mock(side_effect=[surface, {'amounts_valid': True, 'order_amounts': review['order_amounts']}, HouseholdError('lost response after final eval')])
        with self.assertRaises(HouseholdError) as lost:
            browser.submit_delivery_change('123456', order, delivery, review)
        self.assertNotIsInstance(lost.exception, CheckoutPreconditionError)


class MathemOrderIdentityTests(unittest.TestCase):
    def test_swedish_existing_order_addition_requires_exact_date_slot_goods_and_total(self):
        from service_common import oda_order_matches_addition
        before = {"currency": "SEK", "deliveryDate": "2026-05-09", "deliverySlotDisplay": "9 maj 14–16",
                  "grossAmount": "121.95", "products": [
                      {"product": {"id": 4694, "name": "Pasta"}, "quantity": 1, "totalGrossAmount": "15.95"}]}
        after = deepcopy(before)
        after.update(grossAmount="137.90", products=[
            {"product": {"id": 4694, "name": "Pasta"}, "quantity": 2, "totalGrossAmount": "31.90"}])
        additions = {"items": [{"product_id": "4694", "quantity": 1}], "total": "15.95"}
        self.assertTrue(oda_order_matches_addition(before, after, additions, provider="mathem"))
        self.assertFalse(oda_order_matches_addition(before, after, additions))
        for key, value in [("currency", "NOK"), ("currency", None), ("deliveryDate", "2026-05-10"), ("deliverySlotDisplay", "9 maj 16–18"),
                           ("grossAmount", "137.91"), ("products", before["products"])]:
            changed = {**after, key: value}
            with self.subTest(drift=key):
                self.assertFalse(oda_order_matches_addition(before, changed, additions, provider="mathem"))
        self.assertFalse(oda_order_matches_addition(before, before, additions, provider="mathem"))
        for currency in (None, "NOK"):
            self.assertFalse(oda_order_matches_addition({**before, "currency": currency}, after, additions, provider="mathem"))

    def test_cancellation_swedish_delivery_and_currency_are_provider_bound(self):
        from oda_browser import cancellation_delivery_matches, cancellation_total_matches
        self.assertTrue(cancellation_delivery_matches("9 december 14–16", ["9 dec 14:00 till 16:00"], provider="mathem"))
        self.assertFalse(cancellation_delivery_matches("9 december 14–16", ["9 dec 14:00 till 16:00"]))
        self.assertFalse(cancellation_delivery_matches("9 december 14–16", ["9 dec 14–16", "10 dec 14–16"], provider="mathem"))
        self.assertTrue(cancellation_total_matches(12195, ["Totalt inkl. moms 121,95 SEK"], provider="mathem"))
        for row in ["Totalt 121,95 NOK", "Totalt 121,95 kr NOK", "Totalt 121,94 SEK", "Totalt 121,95 SEK Avgift 10,00 SEK"]:
            with self.subTest(row=row):
                self.assertFalse(cancellation_total_matches(12195, [row], provider="mathem"))
        self.assertFalse(cancellation_total_matches(12195, ["Totalt 121,95 SEK"]))
        self.assertTrue(cancellation_total_matches(12195, ["Totalt 121,95 NOK"]))


class MathemCheckoutAmountTests(unittest.TestCase):
    # Redacted rows observed in Mathem's authenticated checkout on 2026-09-07.
    ROWS = [
        ["1 vara", "15,95 kr"], ["Delsumma", "15,95 kr"],
        ["Avgift för liten varukorg", "99,00 kr"], ["Lådor", "7,00 kr"],
        ["Leverans", "59,00 kr"], ["Gratis leverans", "−59,00 kr"],
        ["Totalt inkl. moms", "121,95 kr"],
    ]

    def test_observed_sek_rows_and_provider_isolation(self):
        self.assertEqual([oda_checkout_amount_minor(label, value, provider="mathem")
                          for label, value in self.ROWS],
                         [1595, 1595, 9900, 700, 5900, -5900, 12195])
        self.assertEqual(oda_checkout_amount_minor("2 varor", "31,90 SEK", provider="mathem"), 3190)
        for label, value in [("1 varor", "15,95 kr"), ("2 vara", "31,90 kr"),
                             ("1 vara", "15,95 NOK"), ("Delsum", "15,95 kr"),
                             ("Gratis leverans", "59,00 kr"), ("Leverans", "−59,00 kr"),
                             ("1 vara", "15.95 kr")]:
            with self.subTest(label=label, value=value), self.assertRaises(HouseholdError):
                oda_checkout_amount_minor(label, value, provider="mathem")
        with self.assertRaises(HouseholdError):
            oda_checkout_amount_minor("1 vara", "15,95 kr")
        expected = {"product_subtotal": 15.95, "delivery_price": 59.0, "discounts": -59.0,
                    "deposits": None, "bags": 7.0,
                    "other_fees": {"Avgift för liten varukorg": 99.0}, "provider_total": 121.95}
        minor = _oda_checkout_amounts_minor(expected, provider="mathem")
        self.assertEqual(minor["provider_total"], 12195)
        self.assertEqual(minor["other_fees"], {"Avgift för liten varukorg": 9900})
        with self.assertRaises(HouseholdError):
            _oda_checkout_amounts_minor(expected)

    def test_swedish_delivery_matches_observed_mcp_and_checkout(self):
        expected = "Hemleverans mellan 14 och 16, 9. sep"
        actual = ["Ons 9. september, 14:00-16:00"]
        self.assertTrue(checkout_delivery_matches(expected, actual, provider="mathem"))
        self.assertFalse(checkout_delivery_matches(expected, actual))
        for changed in (["Ons 9. september, 13:00-16:00"],
                        ["Tors 10. september, 14:00-16:00"], actual * 2):
            self.assertFalse(checkout_delivery_matches(expected, changed, provider="mathem"))
        self.assertEqual(delivery_signature("1 maj, 09–12", provider="mathem"), (9, 0, 12, 0, 1, "maj"))
        self.assertEqual(delivery_signature("1 december, 09 till 12", provider="mathem"), (9, 0, 12, 0, 1, "dec"))
        self.assertIsNone(delivery_signature("31 februari, 09–12", provider="mathem"))
        self.assertIsNone(delivery_signature("1 mai, 09–12", provider="mathem"))

    @unittest.skipUnless(shutil.which("node"), "Node executes delayed checkout expansion")
    def test_checkout_expansion_waits_for_visible_sections_and_never_reclicks(self):
        browser = MathemBrowser.__new__(MathemBrowser)
        scripts = {"fresh": browser._checkout_expand_script({"lines": [{}]}, set()),
                   "dispatched": browser._checkout_expand_script({"lines": [{}]}, {"Visa varor", "Visa sammanfattning"})}
        harness = r"""
const scripts=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.location={href:'https://www.mathem.se/se/checkout/confirm/'};
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
let clicks=0;
const node=text=>({innerText:text,textContent:text,labels:[{textContent:'Antal'}],disabled:false,getAttribute:()=>null,getBoundingClientRect:()=>({width:50,height:20}),click:()=>{clicks++;}});
let inputs=[],buttons=[],subtotal=[];
global.document={querySelectorAll:s=>s==='input[type="number"]'?inputs:s==='button'?buttons:subtotal};
const read=script=>({result:JSON.parse(eval(script)),clicks});
const output=[read(scripts.fresh)];
buttons=[node('Visa varor'),node('Visa sammanfattning')];output.push(read(scripts.fresh));
output.push(read(scripts.dispatched));
inputs=[node('')];subtotal=[node('Delsumma')];buttons=[];output.push(read(scripts.dispatched));
output.push(read(scripts.fresh));
inputs=[];subtotal=[];buttons=[node('Visa varor'),node('Visa varor'),node('Visa sammanfattning')];output.push(read(scripts.fresh));
location.href='https://www.mathem.se/se/cart/';output.push(read(scripts.fresh));
process.stdout.write(JSON.stringify(output));
"""
        run = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps(scripts),
                             text=True, capture_output=True, check=True, timeout=10)
        rows = json.loads(run.stdout)
        self.assertEqual(rows, [
            {'result': {'ready': False, 'clicked': []}, 'clicks': 0},
            {'result': {'ready': False, 'clicked': ['Visa varor', 'Visa sammanfattning']}, 'clicks': 2},
            {'result': {'ready': False, 'clicked': []}, 'clicks': 2},
            {'result': {'ready': True}, 'clicks': 2},
            {'result': {'ready': True}, 'clicks': 2},
            {'result': {'blocked': True}, 'clicks': 2},
            {'result': {'blocked': True}, 'clicks': 2},
        ])

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the payment check")
    def test_payment_binding_requires_selected_visible_saved_card(self):
        harness = r"""
const {script,cases}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.getComputedStyle=e=>({display:e.hidden?'none':'block',visibility:'visible'});
const result=[];
for(const test of cases){
 global.location={href:test.page||'https://www.mathem.se/se/checkout/confirm/'};
 const radios=test.radios.map(spec=>{
  const radio={checked:spec.checked,disabled:!!spec.disabled};
  radio.labels=spec.texts.map(text=>({innerText:text,hidden:!!spec.hidden,contains:node=>node===radio,querySelectorAll:()=>Array(spec.radioCount||1).fill(radio),getBoundingClientRect:()=>({width:20,height:20})}));
  return radio;
 });
 global.document={querySelectorAll:()=>radios};
 result.push(JSON.parse(eval(script)));
}
process.stdout.write(JSON.stringify(result));
"""
        card = {"checked": True, "texts": ["", "", "•••• 1234"]}
        cases = [
            {"radios": [card, {"checked": False, "texts": ["•••• 5678"]}]},
            {"radios": [{**card, "checked": False}]},
            {"radios": [card, card]},
            {"radios": [{**card, "hidden": True}]},
            {"radios": [{**card, "radioCount": 2}]},
            {"radios": [{**card, "texts": ["•••• 1234", "•••• 5678"]}]},
            {"radios": [{**card, "texts": ["Swish"]}]},
            {"radios": [card], "page": "https://www.mathem.se/se/account/payment/"},
        ]
        run = subprocess.run([shutil.which("node"), "-e", harness],
            input=json.dumps({"script": _mathem_checkout_payment_script(), "cases": cases}),
            capture_output=True, text=True, check=True, timeout=10)
        result = json.loads(run.stdout)
        self.assertEqual(result[0], {"verified": True, "payment_kind": "saved_card", "payment_display": "•••• 1234"})
        self.assertEqual(result[1:], [{"verified": False}] * 7)

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the account check")
    def test_account_binding_uses_selected_provider_reference(self):
        script = _mathem_checkout_account_script(123)
        harness = r"""
const {script,cases}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.getComputedStyle=e=>({display:e.hidden?'none':'block',visibility:'visible'});
const result=[];
for(const test of cases){
 global.location={href:test.page||'https://www.mathem.se/se/account/delivery/'};
 global.document={querySelector:()=>test.login||null,querySelectorAll:()=>test.links.map(href=>({href,hidden:!!test.hidden,getBoundingClientRect:()=>({width:10,height:10})}))};
 result.push(JSON.parse(eval(script)).account_matches);
}
process.stdout.write(JSON.stringify(result));
"""
        link = "https://www.mathem.se/se/account/delivery/edit/123/"
        cases = [
            {"links": [link, link]},
            {"links": [link.replace('/123/', '/456/')]},
            {"links": [link.replace('www.mathem.se', 'example.com')]},
            {"links": [link], "hidden": True},
            {"links": [link], "page": "https://www.mathem.se/se/account/"},
            {"links": [link], "login": True},
            {"links": [link+'?unverified=1']},
        ]
        run = subprocess.run([shutil.which("node"), "-e", harness],
            input=json.dumps({"script": script, "cases": cases}), text=True,
            capture_output=True, check=True, timeout=10)
        self.assertEqual(json.loads(run.stdout), [True, False, False, False, False, False, False])
        for invalid in (None, True, "123", 0, -1, 2**53):
            with self.subTest(invalid=invalid), self.assertRaises(HouseholdError):
                _mathem_checkout_account_script(invalid)

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the checkout parser")
    def test_checkout_parser_binds_rows_and_rejects_drift(self):
        cases = [{"name": "observed", "rows": self.ROWS, "valid": True}]
        for name, index, text in [("subtotal_contains_delivery_credit", 1, "−43,05 kr"),
                                  ("delivery_credit_changed", 5, "−58,00 kr"),
                                  ("fee_changed", 2, "100,00 kr"),
                                  ("foreign_currency", 0, "15,95 NOK")]:
            rows = deepcopy(self.ROWS)
            rows[index][1] = text
            cases.append({"name": name, "rows": rows, "valid": False})
        cases.extend([
            {"name": "duplicate_total", "rows": self.ROWS + [self.ROWS[-1]], "valid": False},
            {"name": "unknown_fee", "rows": self.ROWS + [["Ny avgift", "1,00 kr"]], "valid": False},
            {"name": "unreconciled_product_discount", "rows": self.ROWS + [["Du sparar", "−1,00 kr"]], "valid": False},
        ])
        amounts = {"product_subtotal": 1595, "delivery_price": 5900, "discounts": -5900,
                   "deposits": None, "bags": 700,
                   "other_fees": {"Avgift för liten varukorg": 9900}, "provider_total": 12195,
                   "discount_breakdown": {"product_discount": None, "delivery_discount": -5900}}
        # Actual 2026-09-10 overview omits a delivery row when no fee is shown.
        # Keep None, and still bind every displayed row and the exact total.
        absent_delivery = {key: None for key in amounts}
        absent_delivery.update(product_subtotal=1850, provider_total=1850,
                               discount_breakdown={"product_discount": None, "delivery_discount": None})
        for change in (None, "explicit_zero", "duplicate", "negative", "fee", "discount_without_delivery", "unknown"):
            rows = [["1 vara", "18,50 kr"], ["Delsumma", "18,50 kr"], ["Totalt inkl. moms", "18,50 kr"]]
            if change == "explicit_zero": rows.append(["Leverans", "0,00 kr"])
            if change == "duplicate": rows.extend([["Leverans", "0,00 kr"], ["Leverans", "0,00 kr"]])
            if change == "negative": rows.append(["Leverans", "−1,00 kr"])
            if change == "fee": rows.append(["Leverans", "1,00 kr"])
            if change == "discount_without_delivery": rows.append(["Gratis leverans", "−1,00 kr"])
            if change == "unknown": rows.append(["Okänd avgift", "1,00 kr"])
            cases.append({"name": f"absent_delivery_{change}", "rows": rows,
                "valid": change in {None, "explicit_zero"}, "click_valid": change is None,
                "total": 1850, "count": 1, "amounts": absent_delivery})
        cases.append({"name": "native_overview_and_payment_disagree", "rows": [
            ["1 vara", "18,50 kr"], ["Delsumma", "18,50 kr"], ["Totalt inkl. moms", "18,50 kr"]],
            "valid": False, "total": 1850, "button_total": 12450, "amounts": absent_delivery})
        cases.append({"name": "fully_displayed_new_order_fees", "rows": [
            ["1 vara", "18,50 kr"], ["Delsumma", "18,50 kr"], ["Leverans", "0,00 kr"],
            ["Avgift för liten varukorg", "99,00 kr"], ["Lådor", "7,00 kr"], ["Totalt inkl. moms", "124,50 kr"]],
            "valid": True, "total": 12450, "amounts": {**absent_delivery, "delivery_price": 0,
                "bags": 700, "other_fees": {"Avgift för liten varukorg": 9900}, "provider_total": 12450}})
        # Authenticated read-only observation on 2026-09-08; no inferred fees.
        discounted_rows = [["17 varor", "482,52 kr"], ["Du sparar", "−24,51 kr"],
            ["Delsumma", "458,01 kr"], ["Avgift för liten varukorg", "99,00 kr"],
            ["Lådor", "7,00 kr"], ["Leverans", "79,00 kr"],
            ["Gratis leverans", "−79,00 kr"], ["Totalt inkl. moms", "564,01 kr"]]
        discounted_amounts = {**amounts, "product_subtotal": 48252, "delivery_price": 7900,
                              "discounts": -10351, "provider_total": 56401,
                              "discount_breakdown": {"product_discount": -2451, "delivery_discount": -7900}}
        for change in (None, "discount", "subtotal", "duplicate", "unknown", "reallocated"):
            rows = deepcopy(discounted_rows)
            if change == "discount": rows[1][1] = "−24,50 kr"
            if change == "subtotal": rows[2][1] = "482,52 kr"
            if change == "duplicate": rows.append(rows[1])
            if change == "unknown": rows.append(["Rabattkod", "−1,00 kr"])
            if change == "reallocated":
                rows[1][1] = "−103,51 kr"
                rows[2][1] = "379,01 kr"
                rows = [row for row in rows if row[0] != "Gratis leverans"]
            cases.append({"name": f"observed_product_discount_{change}", "rows": rows,
                "valid": change in {None, "reallocated"}, "click_valid": change is None,
                "total": 56401, "count": 17, "amounts": discounted_amounts})
        for case in cases:
            total = case.get("total", 12195)
            count = case.get("count", 1)
            case["read"] = _oda_checkout_amount_script(total, expected_product_count=count, provider="mathem")
            case["click"] = _oda_checkout_amount_script(total, expected_product_count=count, provider="mathem",
                expected_amounts=case.get("amounts", amounts), expected_url="https://www.mathem.se/se/checkout/confirm/")
        harness = r"""
const payload=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class Element {
 constructor(tag,text='',children=[]){this.tag=tag;this.text=text;this.children=children;this.disabled=false;for(const child of children)child.parentElement=this;}
 get innerText(){return this.text||this.children.map(child=>child.innerText).join('\n');}
 getBoundingClientRect(){return {width:100,height:20};}
 getAttribute(){return null;}
 contains(node){return this===node||this.children.some(child=>child.contains(node));}
 querySelectorAll(tag){return this.children.flatMap(child=>[...(tag==='*'||child.tag===tag?[child]:[]),...child.querySelectorAll(tag)]);}
 click(){this.clicks=(this.clicks||0)+1;}
}
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
global.location={href:'https://www.mathem.se/se/checkout/confirm/'};
const output=[];
for(const test of payload.cases){
 const summary=new Element('section','',test.rows.map(([label,amount])=>new Element('div','',[new Element('span',label),new Element('span',amount)])));
 const button=new Element('button','Bekräfta och betala '+((test.button_total||test.total||12195)/100).toFixed(2).replace('.',',')+' kr');
 global.document=new Element('document','',[summary,button]);
 const read=JSON.parse(eval(test.read));
 const submit=JSON.parse(eval(test.click));
 output.push({name:test.name,valid:read.amounts_valid,clicked:submit.clicked,clicks:button.clicks||0,amounts:read.amounts});
}
process.stdout.write(JSON.stringify(output));
"""
        completed = subprocess.run([shutil.which("node"), "-e", harness],
            input=json.dumps({"cases": cases}),
            capture_output=True, text=True, timeout=10, check=True)
        observed = json.loads(completed.stdout)
        self.assertEqual(len(observed), len(cases))
        for expected, actual in zip(cases, observed):
            with self.subTest(case=expected["name"]):
                self.assertEqual(actual["valid"], expected["valid"])
                self.assertEqual(actual["clicked"], expected.get("click_valid", expected["valid"]))
                self.assertEqual(actual["clicks"], int(expected.get("click_valid", expected["valid"])))
        self.assertEqual(observed[0]["amounts"], amounts)
        self.assertEqual(observed[-6]["amounts"], discounted_amounts)
        native = MathemBrowser.__new__(MathemBrowser)
        native._eval = mock.Mock(return_value={"amounts": observed[-6]["amounts"], "amounts_valid": True})
        converted = native._read_checkout_amounts(56401, 17)
        self.assertEqual(converted["discount_breakdown"], {"product_discount": -24.51, "delivery_discount": -79.0})
        self.assertEqual(converted["discounts"], -103.51)

class MathemShop(existing.FakeOda):
    def __init__(self):
        super().__init__()
        self.slots = deepcopy(SLOTS)
        self.cart['items'] = []
        self.cart['subtotal'] = 0
        self.cart['delivery'] = None
        self.fail_after_write = False

    def call(self, tool, arguments, **kwargs):
        self.calls.append((tool, deepcopy(arguments)))
        if tool == 'product_search':
            return normalize_retail_product_search(PRODUCTS, provider='mathem')
        if tool == 'recipe_search':
            return {'recipes': [{'id': '42', 'name': 'Soppa', 'url': 'https://www.mathem.se/se/recipes/42-soppa/'}]}
        if tool == 'get_delivery_slots':
            return normalize_retail_delivery_slots(self.slots, provider='mathem')
        if tool == 'select_delivery_slot':
            assert type(arguments['delivery_slot_id']) is int
            assert arguments['delivery_slot_id'] == 77
            self.slots['slots'][0]['isSelected'] = True
            self.cart['delivery'] = {'slot_id': 77, 'display': 'Hemleverans 09–12, 12 sep'}
            return {'selected': True}
        if tool == 'manipulate_cart':
            for operation in arguments['operations']:
                assert type(operation['productId']) is int
                previous = next((p for p in self.cart['items'] if p['product_id'] == operation['productId']), None)
                if previous is None:
                    previous = {'product_id': operation['productId'], 'name': 'Ägg', 'quantity': 0, 'price': 29.9}
                    self.cart['items'].append(previous)
                previous['quantity'] += operation['quantity']
            self.cart['items'] = [p for p in self.cart['items'] if p['quantity']]
            self.cart['subtotal'] = sum(p['price'] * p['quantity'] for p in self.cart['items'])
            if self.fail_after_write:
                raise HouseholdError('Mathem response timed out after dispatch')
            return deepcopy(self.cart)
        self.calls.pop()
        return super().call(tool, arguments, **kwargs)


class ProviderLoginStatusTests(unittest.TestCase):
    def test_status_recovers_completed_login_without_service_restart(self):
        for provider in ('oda', 'mathem'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory(prefix='mc-login-') as temp:
                shop = MathemShop()
                calls = []
                result = {'error': provider.title() + ' login is required'}
                def probe():
                    calls.append('probe')
                    if result.get('error'):
                        raise HouseholdError(result['error'])
                    return {'protocol_version': 'synthetic', 'server': {'name': provider}, 'tool_count': 25}
                shop.probe = probe
                store = StateStore(Path(temp) / 'state', {**existing.CONFIG, 'provider': provider})
                app = Application(store, shop, None)
                self.assertEqual(app.handle({'operation': 'health'})['integration']['status'], 'awaiting_login')
                self.assertEqual(calls, ['probe'])
                before = store.read()
                result['error'] = 'provider is temporarily unavailable'
                self.assertEqual(app.handle({'operation': 'status'})['integration']['status'], 'unavailable')
                result.clear()  # The owner's native OAuth helper has completed.
                status = app.handle({'operation': 'status'})
                self.assertEqual(status['integration']['status'], 'ready')
                self.assertEqual(status['store_readiness']['connection_check']['status'], 'verified')
                self.assertEqual(app.handle({'operation': 'health'})['integration']['status'], 'ready')
                app.handle({'operation': 'status'})
                self.assertEqual(calls, ['probe', 'probe', 'probe'])
                self.assertEqual(shop.calls, [])
                self.assertEqual(store.read(), before)


class MathemFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {**existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}
        self.store = StateStore(self.root / 'state', self.settings)
        self.shop = MathemShop()
        self.app = Application(self.store, self.shop, None)

    def test_search_cart_delivery_and_manual_checkout(self):
        self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})
        search = self.app.handle({'operation': 'catalog', 'action': 'products', 'query': 'ägg'})
        self.assertEqual(search['provider'], 'mathem')
        self.assertEqual(search['products'][0]['provider'], 'mathem')
        cart = self.app.handle({'operation': 'cart', 'action': 'change', 'operations': [{'productId': '10', 'quantity': 2}]})
        self.assertEqual(cart_summary(cart)['items'][0]['quantity'], 2)
        slots = self.app.handle({'operation': 'delivery', 'action': 'list'})
        slot = slots['slots'][0]
        self.assertEqual(slot['slot_ref'], 'mathem:2026-09-12:77')
        self.assertEqual(slot['price_ore'], 1990)
        selected = self.app.handle({'operation': 'delivery', 'action': 'select', 'slot_ref': slot['slot_ref']})
        self.assertEqual(selected['provider'], 'mathem')
        self.assertEqual(self.store.read()['delivery_selection']['provider'], 'mathem')
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertTrue(prepared['manual_checkout_required'])
        self.assertFalse(prepared['confirmed'])
        self.assertEqual(prepared['currency'], 'SEK')
        self.assertEqual(prepared['summary']['total'], 59.8)
        self.assertIn('www.mathem.se', prepared['checkout_url'])
        self.assertIsNone(self.store.read()['pending_checkout'])
        self.assertIsNone(self.app.browser)

    def test_weekly_cart_ready_selects_delivery_and_hands_off_manual_payment(self):
        self.app.handle({'operation': 'schedule', 'action': 'update', 'changes': {
            'enabled': True, 'mode': 'cart_ready', 'auto_checkout': False,
            'delivery': {'weekday': 'Saturday', 'strategy': 'cheapest'},
        }})
        self.app.handle({'operation': 'schedule', 'action': 'set_cron_job', 'cron_job_id': 'test-cron'})
        with mock.patch('service.now', return_value=datetime(2026, 9, 10, 13, 5, tzinfo=timezone.utc)):
            result = self.app.handle({'operation': 'checkout', 'action': 'auto', 'occurrence': '2026-W37'})
        self.assertEqual(result['mode'], 'cart_ready')
        self.assertEqual(result['selected']['slot_ref'], 'mathem:2026-09-12:77')
        self.assertIsNone(self.store.read()['pending_checkout'])
        continuation = self.app.handle({'operation': 'checkout', 'action': 'prepare', 'occurrence': '2026-W37'})
        self.assertTrue(continuation['manual_checkout_required'])
        self.assertEqual(continuation['occurrence'], '2026-W37')

    def test_uncertain_cart_is_reconciled_without_second_dispatch(self):
        self.shop.fail_after_write = True
        with self.assertRaisesRegex(HouseholdError, 'timed out'):
            self.app.handle({'operation': 'cart', 'action': 'change', 'operations': [{'productId': 10, 'quantity': 1}]})
        with self.assertRaisesRegex(HouseholdError, 'reconcile_change'):
            self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.app.handle({'operation': 'cart', 'action': 'reconcile_change'})
        self.assertIsNone(self.store.read().get('pending_cart_change'))
        self.assertEqual(sum(tool == 'manipulate_cart' for tool, _ in self.shop.calls), 1)

    def test_protected_actions_never_use_oda_browser_or_create_attempts(self):
        before = self.store.read()
        for operation, actions in [('checkout', ['confirm', 'submit', 'reconcile']), ('orders', ['change_begin', 'cancel_prepare', 'cancel_submit', 'cancel_confirm', 'cancel_reconcile'])]:
            for action in actions:
                with self.subTest(operation=operation, action=action), self.assertRaisesRegex(HouseholdError, 'Mathem'):
                    self.app.handle({'operation': operation, 'action': action, 'order_id': '1', 'idempotency_key': 'test'})
        with self.assertRaisesRegex(HouseholdError, 'no matching order change'):
            self.app.handle({'operation': 'orders', 'action': 'change_abort', 'order_id': '1'})
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.shop.calls, [])
        with self.assertRaisesRegex(HouseholdError, 'maximum total'):
            validate_schedule({**before['schedule'], 'auto_checkout': True, 'maximum_total': None}, 'mathem')

    def test_mathem_defaults_recipes_and_provider_isolation(self):
        status = self.app.handle({'operation': 'status'})
        self.assertEqual(status['currency'], 'SEK')
        self.assertEqual(status['schedule']['timezone'], 'Europe/Stockholm')
        sources = status['recipe_sources']
        self.assertTrue(sources['mathem'])
        self.assertFalse(sources['oda'])
        self.assertFalse(sources['meny'])
        candidates = self.app._provider_recipe_candidates('mathem', 'soppa', 5)
        self.assertEqual(candidates[0]['language'], 'sv-SE')
        self.assertEqual(candidates[0]['source']['kind'], 'mathem')
        self.assertEqual(candidates[0]['rights']['storage'], 'link_only')
        self.assertEqual(provider_recipe_candidates('oda', self.shop.call('recipe_search', {}), 5), [])
        with self.assertRaisesRegex(HouseholdError, 'belongs to provider mathem'):
            StateStore(self.root / 'state', {**self.settings, 'provider': 'oda'})
        self.assertNotEqual(email_automation_key('oda', '123'), email_automation_key('mathem', '123'))
        order = self.app.handle({'operation': 'orders', 'action': 'get', 'order_id': '123'})
        self.assertEqual(order['tracking']['order_id'], '123')

    def test_existing_source_preferences_survive_upgrade(self):
        oda_root = self.root / 'oda'
        oda_store = StateStore(oda_root, existing.CONFIG)
        with oda_store.locked() as state:
            del state['profile']['recipes']['sources']['mathem']
            state['profile']['recipes']['sources']['oda'] = False
        upgraded = StateStore(oda_root, existing.CONFIG).read()
        self.assertFalse(upgraded['profile']['recipes']['sources']['mathem'])
        self.assertFalse(upgraded['profile']['recipes']['sources']['oda'])

    def test_mathem_config_and_strict_prices(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.settings))
        self.assertEqual(config(path)['provider'], 'mathem')
        for text, expected in [('29,90 kr', 2990), ('29.90', 2990), ('kr\u00a00', 0), ('0 kr', 0), ('SEK 12,50', 1250)]:
            self.assertEqual(_mathem_ore(text), expected)
        for text in ['Från 29,90 kr', 'ca 29,90 kr', '29,90 NOK', '-1 kr', '1 2 kr']:
            self.assertIsNone(_mathem_ore(text))
        self.assertEqual(parse_package('6 st', provider='mathem')['unit'], 'count')
        self.assertEqual(retail_delivery_slot_date('mathem:2026-09-12:77', provider='mathem'), '2026-09-12')
        with self.assertRaises(HouseholdError):
            retail_delivery_slot_date('oda:2026-09-12:77', provider='mathem')

    def test_malformed_shared_results_use_accurate_errors(self):
        with self.assertRaisesRegex(HouseholdError, 'Product search result changed'):
            normalize_retail_product_search({'result': []}, provider='mathem')
        malformed = deepcopy(SLOTS)
        malformed['slots'][0]['openDatetime'] = 'not-a-timestamp'
        with self.assertRaisesRegex(HouseholdError, 'Delivery slot timestamp changed'):
            normalize_retail_delivery_slots(malformed, provider='mathem')
        with self.assertRaisesRegex(HouseholdError, 'Delivery slot_ref is invalid'):
            retail_delivery_slot_date('oda:2026-09-12:77', provider='mathem')
        with self.assertRaisesRegex(HouseholdError, 'configured Mathem provider'):
            self.app.handle({'operation': 'product_favorites', 'action': 'add', 'product_id': '/varer/not-a-mathem-id', 'name': 'Synthetic'})
        with self.assertRaisesRegex(HouseholdError, 'Cart items are unavailable'):
            cart_summary({'provider': 'mathem', 'total': 0})

    def test_mathem_content_keeps_provider_recipe_storage_restriction(self):
        value = provider_recipe_candidates('mathem', self.shop.call('recipe_search', {}), 5)[0]
        value['rights']['storage'] = 'full'
        value['rights']['license'] = 'unknown'
        with self.assertRaises(RecipeError):
            normalize_recipe(value)


class MathemTransportTests(unittest.TestCase):
    def test_swedish_oil_query_preserves_bounds_and_rejects_wrong_response(self):
        arguments = {'queries': ['rapsolje'], 'page': 1, 'size': 5}
        response = normalize_retail_product_search(
            {'result': [{'query': 'rapsolja', 'products': [], 'hasMore': False}]}, provider='mathem')
        client = RetailMcpClient(self.__class__.__name__, provider='mathem')
        with mock.patch('retail_mcp.time.monotonic', return_value=100), mock.patch.object(client, '_run', return_value=deepcopy(response)) as run:
            result = client.call('product_search', arguments, deadline=112)
            run.assert_called_once_with('product_search', {'queries': ['rapsolja'], 'page': 1, 'size': 5}, 12)
        self.assertEqual(arguments, {'queries': ['rapsolje'], 'page': 1, 'size': 5})
        self.assertEqual(result['query'], 'rapsolje')
        self.assertEqual(result['scope']['provider_query'], 'rapsolja')
        for wrong in ('rapsolje', 'olje', None):
            with self.subTest(query=wrong), mock.patch.object(client, '_run', return_value={**response, 'query': wrong}) as run:
                with self.assertRaisesRegex(HouseholdError, 'query changed'):
                    client.call('product_search', arguments)
                self.assertEqual(run.call_count, 1)
        for provider, queries in [('oda', ['rapsolje']), ('mathem', ['olivolja']), ('mathem', ['rapsolje', 'salt'])]:
            client = RetailMcpClient(self.__class__.__name__, provider=provider)
            with self.subTest(provider=provider, queries=queries), mock.patch.object(client, '_run', return_value=deepcopy(response)) as run:
                result = client.call('product_search', {'queries': queries, 'size': 5})
                run.assert_called_once_with('product_search', {'queries': queries, 'size': 5}, 90.0)
                self.assertNotIn('provider_query', result['scope'])

    def test_observed_mathem_packages_reach_plan_without_inventing_payable(self):
        from product_planner import build_product_plan, menu_requirements
        for label, grams in [('Sverige, 500 g', 500), ('Sverige, 2 kg', 2000), ('Spanien, 250 g', 250)]:
            with self.subTest(label=label):
                self.assertEqual(parse_package(label, provider='mathem'), {
                    'quantity': {'numerator': grams, 'denominator': 1}, 'unit': 'g', 'item_count': 1})
                self.assertIsNone(parse_package(label, provider='oda'))
        for label in ['Sverige, ca 500 g', 'Sverige, 500 g/kg', 'Sverige, 500 g extra', 'Okänd, 500 g', 'Spanien, 250 ml', 'Sverige, 2 st', 'Sverige, 0 g']:
            self.assertIsNone(parse_package(label, provider='mathem'), label)
        menu = {'dishes': [{'shopping_requirements': [
            {'item': 'rapsolje', 'quantity': 20, 'unit': 'ml', 'scalable': True},
            {'item': 'morot', 'quantity': 400, 'unit': 'g', 'scalable': True}]}], 'salads': []}
        requirements, unresolved = menu_requirements(menu)
        self.assertEqual(unresolved, [])
        raw_products = {
            'rapsolja': {'id': 10008, 'name': 'Rapsolja', 'description': '1 l', 'price': '24.95', 'availability': True},
            'morot': {'id': 7664, 'name': 'Morötter', 'description': 'Sverige, 500 g', 'price': '12.95', 'availability': True},
        }
        def search(tool, arguments, timeout):
            self.assertEqual(tool, 'product_search')
            query = arguments['queries'][0]
            result = normalize_retail_product_search({'result': [{'query': query, 'products': [raw_products[query]], 'hasMore': False}]}, provider='mathem')
            result['scope']['requested_size'] = arguments['size']
            return result
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / 'state', {**existing.CONFIG, 'provider': 'mathem'})
            client = RetailMcpClient(temp, provider='mathem')
            app = Application(store, client, None)
            with mock.patch.object(client, '_run', side_effect=search) as run:
                observations = app._product_observations(menu, deadline=None)
                self.assertEqual(run.call_count, 2)
            approvals = [{'requirement_id': r['requirement_id'], 'candidate_refs': [10008 if r['identity'] == 'rapsolje' else 7664]} for r in requirements]
            common = dict(provider='mathem', binding={}, menu=menu, observations=observations, candidate_approvals=approvals, budget_ore=70000)
            self.assertEqual(build_product_plan(**common)['status'], 'needs_input')
            plan = build_product_plan(**common, price_mode='estimate')
            self.assertEqual(plan['status'], 'prepared', plan['unresolved_requirements'])
            self.assertEqual(plan['cost_status'], 'merchandise_estimate_only')
            self.assertEqual(plan['budget_status'], 'unverified')
            self.assertEqual(plan['totals']['merchandise_ore'], 3790)
            self.assertIsNone(plan['totals']['total_payable_ore'])
            self.assertIsNone(plan['totals']['mandatory_deposit_ore'])
            oil = next(r for r in plan['requirements'] if r['identity'] == 'rapsolje')
            self.assertEqual(oil['observation']['query'], 'rapsolje')
            self.assertEqual(oil['observation']['scope']['provider_query'], 'rapsolja')

    def test_oauth_endpoint_storage_and_result_use_mathem_identity(self):
        captured = {}
        supported_tools = set(REQUIRED_TOOLS)
        def oauth_provider(directory, server_name, label, endpoint):
            captured['oauth'] = (directory, server_name, label, endpoint)
            return SimpleNamespace()
        class Context:
            def __init__(self, *args, **kwargs):
                if 'event_hooks' in kwargs:
                    captured['redirect_check'] = kwargs['event_hooks']['response'][0]
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        class Session(Context):
            async def initialize(self):
                return SimpleNamespace(server_info={'name': 'Mathem MCP'}, protocol_version='2025-11-25')
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in supported_tools])
            async def call_tool(self, tool, arguments):
                captured['call'] = (tool, arguments)
                return SimpleNamespace(is_error=False, structured_content=PRODUCTS)
        @asynccontextmanager
        async def stream(url, **kwargs):
            captured['url'] = url
            yield (None, None, None)
        modules = {
            'httpx2': SimpleNamespace(AsyncClient=Context, Timeout=lambda *a, **k: None, URL=httpx.URL),
            'mcp': SimpleNamespace(ClientSession=Session),
            'mcp.client.streamable_http': SimpleNamespace(streamable_http_client=stream),
            'mcp.types': SimpleNamespace(LATEST_PROTOCOL_VERSION='2025-11-25'),
            'provider_oauth': SimpleNamespace(build_auth=oauth_provider),
        }
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(sys.modules, modules):
            client = RetailMcpClient(temp, provider='mathem')
            result = client.call('product_search', {'queries': ['ägg'], 'size': 5})
            self.assertEqual(result['provider'], 'mathem')
            self.assertEqual(captured['url'], 'https://www.mathem.se/mcp')
            self.assertEqual(captured['oauth'], (Path(temp), 'mathem-weekly', 'Mathem', captured['url']))
            self.assertTrue((Path(temp) / '.mathem-household.lock').exists())
            self.assertFalse((Path(temp) / '.oda-household.lock').exists())
            self.assertEqual(RetailMcpClient(temp).endpoint, 'https://oda.com/mcp')
            response = SimpleNamespace(is_redirect=True, next_request=SimpleNamespace(url=httpx.URL('https://oda.com/mcp')))
            with self.assertRaisesRegex(HouseholdError, 'redirect is unsupported'):
                asyncio.run(captured['redirect_check'](response))
            supported_tools.remove('get_cart')
            with self.assertRaisesRegex(HouseholdError, 'lacks required operations: get_cart'):
                client.probe()
            with mock.patch.object(modules['provider_oauth'], 'build_auth', side_effect=HouseholdError('Mathem login is required')):
                with self.assertRaisesRegex(HouseholdError, 'Mathem login is required'):
                    client.probe()

    def test_runtime_launcher_does_not_require_browser_for_mathem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = root / 'config.json'
            settings.write_text(json.dumps({**existing.CONFIG, 'provider': 'mathem'}))
            wrapper = root / 'python'
            wrapper.write_text(f"""#!{sys.executable}
import json, os, sys
if len(sys.argv) > 1 and sys.argv[1].endswith('/service.py'):
    print(json.dumps(sys.argv[1:]))
else:
    os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
""")
            wrapper.chmod(0o755)
            result = subprocess.run(['/bin/bash', str(existing.CORE / 'run-service.sh')],
                env={**os.environ, 'HERMES_PYTHON': str(wrapper), 'HERMES_HOME': str(root),
                     'MEAL_CONCIERGE_HOME': str(root / 'private'), 'MEAL_CONCIERGE_CONFIG': str(settings),
                     'MEAL_CONCIERGE_BROWSER_SOCKET_DIR': str(root / 'browser-socket'),
                     'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = json.loads(result.stdout)
            self.assertEqual(arguments[arguments.index('--tokens') + 1], str(root / 'mcp-tokens'))
            self.assertNotIn('--browser-binary', arguments)
            self.assertNotIn('--browser-executable', arguments)
            import service
            with mock.patch.object(sys, 'argv', ['service.py', *arguments[1:]]), mock.patch.object(service.RetailMcpClient, 'probe', return_value={}), mock.patch.object(service, 'OdaBrowser') as browser, mock.patch.object(service.Server, 'run') as run:
                service.main()
            browser.assert_not_called()
            run.assert_called_once()
            adapter = root / 'agent-browser'
            adapter.write_text('#!/bin/sh\nexit 0\n'); adapter.chmod(0o700)
            chrome = root / 'chromium'
            chrome.write_text('#!/bin/sh\nexit 0\n'); chrome.chmod(0o700)
            configured = subprocess.run(['/bin/bash', str(existing.CORE / 'run-service.sh')],
                env={**os.environ, 'HERMES_PYTHON': str(wrapper), 'HERMES_HOME': str(root),
                     'MEAL_CONCIERGE_HOME': str(root / 'private'), 'MEAL_CONCIERGE_CONFIG': str(settings),
                     'MEAL_CONCIERGE_BROWSER_SOCKET_DIR': str(root / 'browser-socket'),
                     'MEAL_CONCIERGE_AGENT_BROWSER': str(adapter), 'MEAL_CONCIERGE_BROWSER_EXECUTABLE': str(chrome),
                     'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
            self.assertEqual(configured.returncode, 0, configured.stderr)
            arguments = json.loads(configured.stdout)
            # Capture the actual Application constructed by the launcher, with
            # real provider/browser instances but no retailer or listener call.
            with mock.patch.object(sys, 'argv', ['service.py', *arguments[1:]]), \
                 mock.patch.object(service.RetailMcpClient, 'probe', return_value={}), \
                 mock.patch.object(service, 'Server') as server:
                service.main()
            app = server.call_args.args[3]
            self.assertIsInstance(app.browser, MathemBrowser)
            self.assertIs(app.browser.provider_client, app.provider_client)
            self.assertEqual(app.browser.profile, root / 'private/browser/profile')


class CompactProductApplyTests(unittest.TestCase):
    def setUp(self):
        from test_meal_concierge_products import ProductRuntimeTests
        fixture = ProductRuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.app, self.store, self.shop = fixture.app, fixture.store, fixture.provider
        plan = fixture.prepare(approve=True)
        self.arguments = {'operation': 'products', 'action': 'apply', 'menu_ref': fixture.menu_ref,
            'candidate_approvals': self.app._plan_approvals(plan), 'ingredient_decisions': plan['ingredient_decisions'],
            'budget_ore': plan['budget_ore'], 'price_mode': plan['price_mode'],
            'product_plan_digest': plan['product_plan_digest'], 'cart_change_requested': True}

    def test_compact_inputs_and_fresh_or_final_price_drift_cannot_write(self):
        before = len(self.shop.calls)
        self.assertFalse(self.app.handle({**self.arguments, 'cart_change_requested': False})['applied'])
        for value in (None, 'bad'):
            with self.assertRaises(HouseholdError):
                self.app.handle({**self.arguments, 'product_plan_digest': value})
        self.assertEqual(len(self.shop.calls), before)
        with self.assertRaises(HouseholdError):
            self.app.handle({**self.arguments, 'menu_ref': {**self.arguments['menu_ref'], 'digest': 'b' * 64}})
        for change in ({'candidate_approvals': []}, {'budget_ore': 1}, {'price_mode': 'estimate'}):
            with self.subTest(change=change):
                self.assertFalse(self.app.handle({**self.arguments, **change})['applied'])
        for prices in ([1000, 900], [1000, 1000, 900]):
            self.shop.search_prices, self.shop.search_count = prices, 1
            self.assertFalse(self.app.handle(self.arguments)['applied'])
        self.assertNotIn('manipulate_cart', [name for name, _ in self.shop.calls])

    def test_compact_apply_restart_and_lost_cart_response_preserve_single_write(self):
        original = self.shop.call
        def lost_response(tool, arguments, **kwargs):
            result = original(tool, arguments, **kwargs)
            if tool == 'manipulate_cart':
                raise HouseholdError('synthetic lost cart response after effect')
            return result
        self.shop.call = lost_response
        self.app.handle(self.arguments)
        restarted = Application(StateStore(self.store.path.parent, {
            'instance': 'test', 'household': 'Test', 'profile_overrides': {}}), self.shop, object())
        result = restarted.handle(self.arguments)
        self.assertTrue(result['cart_reconciliation_required'])
        kept = restarted.handle({'operation': 'cart', 'action': 'reconcile',
            'menu_ref': self.arguments['menu_ref'], 'cart_digest': result['cart_plan']['cart_digest'],
            'decision': 'keep_current'})
        self.assertTrue(kept['reconciled'])
        result = restarted.handle(self.arguments)
        self.assertTrue(result['applied'])
        self.assertEqual([name for name, _ in self.shop.calls].count('manipulate_cart'), 1)
        self.assertEqual(self.shop.cart['items'][0]['quantity'], 1)


class MathemGuardedCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mc50-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cart = {
            'items': [{'product': {'id': 4694, 'name': 'Pasta Fusilli', 'description': '500 g', 'brand': 'Barilla'}, 'quantity': 1, 'totalGrossAmount': 15.95}],
            'productQuantityCount': 1, 'totalGrossAmount': 121.95,
            'deliverySlot': {'id': 77, 'name': 'Hemleverans mellan 09 och 12, 12. sep'},
            'deliveryAddress': 'Exempelvägen 1',
        }
        self.amounts = {'product_subtotal': 15.95, 'delivery_price': 59.0, 'discounts': -59.0,
                        'deposits': None, 'bags': 7.0, 'other_fees': {'Avgift för liten varukorg': 99.0},
                        'provider_total': 121.95}
        self.shop = MathemShop()
        self.shop.cart = deepcopy(self.cart)
        self.shop.slots['slots'][0]['isSelected'] = True
        self.shop.slots['slots'][0]['price'] = '0,00 kr'
        self.store = StateStore(self.root / 'state', {**existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'})
        self.browser = existing.FakeBrowser()
        self.browser.receipt_address = 'Exempelvägen 1'
        self.browser.oda = self.shop
        self.browser.order_followup = mock.Mock(return_value={
            'deadline_text': 'Du har till och med 23:59 på 12. september att lägga till varor i din leverans.',
            'cancellation_deadline_text': 'Du kan avboka din beställning när som helst före lördag 12 september kl. 23:59',
            'cancellation_available': True})
        self.browser.receipt_address_matches = lambda order_id, address, **kw: order_id == "123456" and address == "Exempelvägen 1"
        self.discount_breakdown = {'product_discount': None, 'delivery_discount': -59.0}
        self.browser.review_checkout = lambda cart, **kw: {'payment_display': '•••• 1234', 'amounts': deepcopy(self.amounts), 'discount_breakdown': deepcopy(self.discount_breakdown)}
        self.app = Application(self.store, self.shop, self.browser)
        self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})

    def order(self):
        return {'orderNumber': '123456', 'currency': 'SEK', 'grossAmount': 121.95,
                'deliveryDate': '2026-09-12', 'deliverySlotDisplay': 'Lör 12. sep 09:00 - 12:00',
                'deliveryAddress': 'Exempelvägen 1', 'products': deepcopy(self.cart['items'])}

    @mock.patch.object(Application, '_now', return_value=datetime(2026, 9, 4, 13, 5, tzinfo=timezone.utc))
    def test_supplemental_checkout_preserves_unassessed_menu_after_lost_response(self, _clock):
        for provider_name in ('oda', 'mathem'):
            with self.subTest(provider=provider_name), tempfile.TemporaryDirectory() as directory:
                if provider_name == 'oda':
                    shop = existing.MutableFakeOda()
                    browser = existing.FakeBrowser(); browser.oda = shop
                    product_id = '10'
                else:
                    shop = MathemShop()
                    shop.cart = deepcopy(self.cart)
                    shop.cart['items'] = []
                    shop.cart['productQuantityCount'] = 0
                    shop.slots['slots'][0]['isSelected'] = True
                    shop.slots['slots'][0]['price'] = '0,00 kr'
                    browser = existing.FakeBrowser(); browser.oda = shop
                    browser.receipt_address = 'Exempelvägen 1'
                    amounts = {**self.amounts, 'product_subtotal': 29.9, 'provider_total': 135.9}
                    browser.review_checkout = lambda cart, **kw: {'payment_display': '•••• 1234',
                        'amounts': amounts, 'discount_breakdown': self.discount_breakdown}
                    product_id = '4694'
                shop.cart['items'] = []
                shop.cart['subtotal'] = 0
                config = {**existing.CONFIG, 'provider': provider_name, 'confirmation_policy': 'standing'}
                store = StateStore(Path(directory), config)
                app = Application(store, shop, browser)
                from test_meal_concierge_planner import recipe
                refs = []
                for i in range(7):
                    saved = app.handle({'operation': 'recipes', 'action': 'save',
                        'recipe': recipe('Saved dinner ' + str(i), 'saved-dinner-' + str(i)),
                        'idempotency_key': 'saved-dinner-' + str(i)})['recipe']
                    refs.append({'recipe_ref': {'id': saved['id'], 'revision': saved['revision']}})
                app.handle({'operation': 'profile', 'action': 'update', 'changes': {'recipes': {'sources': {
                    'oda': False, 'mathem': False, 'meny': False, 'themealdb': False, 'wikibooks': False}}}})
                plan = app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {
                    'week': '2026-W38', 'dates': [f'2026-09-{14+i}' for i in range(7)],
                    'portions': 2, 'candidates': refs}})['plan']
                menu = app.handle({'operation': 'menu', 'action': 'save', 'planner_ref': plan['save_ref']})['menu']
                self.assertEqual(len(menu['slots']), 7)
                app.handle({'operation': 'profile', 'action': 'update', 'changes': {'diet': {
                    'rules': [{'kind': 'allergy', 'term': 'sesam'}],
                    'uncertainty_permissions': [{'kind': 'allergy', 'term': 'sesam', 'product_ref': product_id,
                        'condition': 'unknown', 'accepted': True, 'notify': True}]}}})
                browser.order_followup = lambda *a, **kw: {'deadline_text': None,
                    'cancellation_deadline_text': None, 'cancellation_available': True}
                app.handle({'operation': 'cart', 'action': 'ensure', 'requirements': [
                    {'product_id': product_id, 'product_name': 'Separate groceries', 'quantity': 1}]})
                if provider_name == 'mathem':
                    shop.cart['totalGrossAmount'] = 135.9
                    shop.cart['productQuantityCount'] = 1
                prepared = app.handle({'operation': 'checkout', 'action': 'prepare'})
                self.assertEqual(prepared['summary']['menu_attribution'], 'cart_only')
                self.assertEqual(prepared['summary']['menu_coverage'], 'not_assessed')
                self.assertEqual(prepared['summary']['menu_shortfall'], [])
                before = store.read()
                def lost(cart, review, before_click, **kwargs):
                    before_click(); browser.checkout_clicks += 1
                    summary = store.read()['pending_checkout']['summary']
                    shop.orders.append({'orderNumber': '123456', 'currency': 'SEK' if provider_name == 'mathem' else 'NOK',
                        'grossAmount': summary['total'], 'deliveryDate': '2026-09-12' if provider_name == 'mathem' else '2026-09-05',
                        'deliverySlotDisplay': 'Lör 12. sep 09:00 - 12:00' if provider_name == 'mathem' else 'Lør 5. sep 09:00 - 12:00',
                        'deliveryAddress': browser.receipt_address,
                        'products': [{'product': {'id': int(product_id), 'name': 'Separate groceries'},
                                      'quantity': 1, 'totalGrossAmount': summary['total']}]})
                    raise HouseholdError('synthetic accepted order response lost')
                browser.submit_checkout = lost
                request = {'operation': 'checkout', 'action': 'submit', 'idempotency_key': 'separate-groceries'}
                notice = app.handle(request)['notice']
                self.assertTrue(notice['dispatch'])
                self.assertEqual(browser.checkout_clicks, 0)
                inbox = Path(directory) / 'notice.txt'
                inbox.write_text(notice['payload']['message'])
                self.assertEqual(inbox.read_text(), notice['payload']['message'])
                app.handle({'operation': 'checkout', 'action': 'notice_result', 'notice_token': notice['notice_token'],
                            'send_outcome': 'sent', 'sender_receipt': 'synthetic-local-inbox-verified'})
                with self.assertRaisesRegex(HouseholdError, 'response lost'):
                    app.handle(request)
                restarted = Application(StateStore(Path(directory), config), shop, browser)
                result = restarted.handle(request)
                self.assertTrue(result['confirmed'])
                self.assertEqual(result['menu_attribution'], 'cart_only')
                self.assertEqual(result['menu_coverage'], 'not_assessed')
                after = result['notice']
                self.assertEqual(after['phase'], 'after_reconciliation')
                inbox.write_text(after['payload']['message'])
                self.assertEqual(inbox.read_text(), after['payload']['message'])
                restarted.handle({'operation': 'checkout', 'action': 'notice_result', 'notice_token': after['notice_token'],
                    'send_outcome': 'sent', 'sender_receipt': 'synthetic-result-inbox-verified'})
                self.assertTrue(restarted.handle(request)['idempotent'])
                self.assertEqual(browser.checkout_clicks, 1)
                for key in ('menu', 'recipe_usage', 'cart_plan', 'order_snapshots'):
                    self.assertEqual(restarted.store.read()[key], before[key], key)

    def test_legacy_cart_only_receipt_does_not_attribute_a_newer_menu_plan(self):
        old_menu = existing.CartPlanTests.menu()
        old_plan = self.app._new_cart_plan(self.app._cart_menu_ref(old_menu), {}, {}, {}, {}, set())
        new_menu = existing.CartPlanTests.menu(revision=2, digest="b" * 64)
        with self.store.locked() as state:
            state['menu'] = new_menu
            state['cart_plan'] = self.app._new_cart_plan(self.app._cart_menu_ref(new_menu),
                {'4694': 1}, {'4694': 'Pasta'}, {'4694': 1}, {}, set())
            original = deepcopy(state)
            # Historical pending has no new attribution fields. Only its frozen
            # requirements can establish that a menu was part of the purchase.
            self.app._record_order_snapshot(state, {'menu': old_menu, 'cart_plan': old_plan}, '123456')
            self.assertEqual(state, original)

    def test_prepare_uses_final_amounts_and_masked_payment(self):
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertNotIn('manual_checkout_required', prepared)
        self.assertEqual(prepared['summary']['total'], 121.95)
        self.assertEqual(prepared['summary']['payment'], '•••• 1234')
        self.assertEqual(prepared['summary']['amounts'], self.amounts)
        self.assertEqual(self.store.read()['pending_checkout']['status'], 'awaiting_confirmation')
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_existing_order_binding_keeps_original_address_and_rejects_identity_drift(self):
        import hashlib
        import time
        native = MathemBrowser.__new__(MathemBrowser)
        original = {'id': 123, 'address': 'Exempelvägen 1', 'isSelected': False}
        other = {'id': 456, 'address': 'Annan väg 2', 'isSelected': True}
        native.provider_client = mock.Mock(provider='mathem')
        native.provider_client.call.return_value = {'result': [original, other]}
        native._open = mock.Mock()
        native._settle = mock.Mock()
        expected = {'account_reference_digest': hashlib.sha256(b'123').hexdigest(),
                    'receipt_address': 'Exempelvägen 1'}
        deadline = time.monotonic() + 60
        native._eval = mock.Mock(side_effect=[{'address_verified': True}, {'address_verified': False}, {'account_matches': True}])
        bound = native._read_order_binding('123456', self.order(), deadline=deadline)
        self.assertEqual(bound, expected)
        native.provider_client.call.assert_called_once_with('get_delivery_addresses', {}, deadline=deadline)
        native._eval = mock.Mock(side_effect=[{'address_verified': True}, {'account_matches': True}])
        self.assertEqual(native._read_order_binding('123456', self.order(), deadline=deadline,
                         expected_binding=bound), expected)
        self.assertEqual(native._open.call_args_list[-2:], [
            mock.call('https://www.mathem.se/se/account/orders/123456/'),
            mock.call('https://www.mathem.se/se/account/delivery/')])
        # Same postal address does not replace the frozen OAuth reference.
        native.provider_client.call.return_value = {'result': [{**original, 'id': 789}]}
        native._eval.reset_mock()
        with self.assertRaisesRegex(HouseholdError, 'original order account binding'):
            native._read_order_binding('123456', self.order(), deadline=deadline, expected_binding=bound)
        native._eval.assert_not_called()
        native.provider_client.call.return_value = {'result': [original]}
        for observations, message in (([{'address_verified': False}] * 20, 'receipt address'),
                ([{'address_verified': True}] + [{'account_matches': False}] * 20, 'original order account')):
            native._eval = mock.Mock(side_effect=observations)
            with self.assertRaisesRegex(HouseholdError, message):
                native._read_order_binding('123456', self.order(), deadline=deadline, expected_binding=bound)
        for delta in ({'currency': 'NOK'}, {'orderNumber': 'different'}):
            native.provider_client.call.reset_mock()
            with self.assertRaises(HouseholdError):
                native._read_order_binding('123456', {**self.order(), **delta}, deadline=deadline)
            native.provider_client.call.assert_not_called()
        native.provider_client.call.return_value = {'result': [original, original]}
        native._eval = mock.Mock(return_value={'address_verified': True})
        with self.assertRaisesRegex(HouseholdError, 'ambiguous'):
            native._read_order_binding('123456', self.order(), deadline=deadline)
        native.provider_client.call.return_value = {'result': [original]}
        native._eval = mock.Mock(side_effect=[{'address_verified': True}, {'account_matches': True}])
        with mock.patch('oda_browser.time.monotonic', return_value=deadline):
            with self.assertRaisesRegex(HouseholdError, 'deadline'):
                native._read_order_binding('123456', self.order(), deadline=deadline, expected_binding=bound)

    def test_free_delivery_keeps_product_discount_separate_in_protected_review(self):
        self.shop.cart['items'][0]['totalGrossAmount'] = 16.95
        self.amounts.update(product_subtotal=16.95, discounts=-60.0)
        self.discount_breakdown['product_discount'] = -1.0
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertEqual(prepared['summary']['amounts'], self.amounts)
        self.assertEqual(prepared['summary']['discount_breakdown'], self.discount_breakdown)
        self.assertEqual(prepared['summary']['delivery']['slot']['price_ore'], 0)
        self.assertEqual(prepared['summary']['total'], 121.95)
        self.assertEqual(self.browser.checkout_clicks, 0)
        # Equal aggregate discounts cannot establish a delivery credit.
        self.discount_breakdown.update(product_discount=-60.0, delivery_discount=None)
        with self.assertRaisesRegex(HouseholdError, 'delivery price disagrees'):
            self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_shared_order_reads_and_nested_cancellation_keep_original_deadline(self):
        import time
        deadline = time.monotonic() + 60
        self.shop.orders.append(self.order())
        original = self.shop.call
        deadlines = []
        def provider(tool, arguments, **kwargs):
            deadlines.append((tool, kwargs.get('deadline')))
            return original(tool, arguments, **kwargs)
        self.shop.call = provider
        self.app._orders({'action': 'get', 'order_id': '123456', '_deadline': deadline})
        self.app._orders({'action': 'list', '_deadline': deadline})
        self.assertEqual(deadlines, [('get_order', deadline), ('order_tracking', deadline), ('get_orders', deadline)])
        # Retain Oda's shared deadline path alongside Mathem's binding checks.
        self.shop.orders[0]['currency'] = 'NOK'
        store = StateStore(self.root / 'oda-deadline', {**existing.CONFIG, 'confirmation_policy': 'standing'})
        oda = Application(store, self.shop, self.browser)
        deadlines.clear()
        result = oda._orders({'action': 'cancel_submit', 'order_id': '123456',
            'idempotency_key': 'synthetic-deadline', '_deadline': deadline})
        self.assertTrue(result['cancelled'])
        self.assertTrue(deadlines)
        self.assertTrue(all(value == deadline for _, value in deadlines))
        self.assertEqual(self.browser.cancellation_review_deadlines, [deadline])
        self.assertEqual(self.browser.cancellation_submit_deadlines, [deadline])

    def test_mathem_cancellation_lost_response_reconciles_original_binding_once(self):
        self.shop.orders.append(self.order())
        binding = {'account_reference_digest': 'a' * 64, 'receipt_address': 'Exempelvägen 1'}
        self.browser.review_cancellation = lambda *args, **kw: {
            'available': True, 'binding': deepcopy(binding), 'consequence': 'Cannot be undone'}
        self.browser.read_order_binding = mock.Mock(return_value=deepcopy(binding))
        original = self.browser.submit_cancellation
        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise HouseholdError('synthetic lost response after cancellation')
        self.browser.submit_cancellation = lost_response
        request = {'action': 'cancel_submit', 'order_id': '123456', 'idempotency_key': 'cancel-own-order'}
        with self.assertRaises(HouseholdError):
            self.app._orders(request)
        self.assertEqual(self.store.read()['pending_cancellation']['status'], 'uncertain')
        self.assertEqual(self.browser.cancel_clicks, 1)
        restarted = Application(StateStore(self.store.path.parent, {
            **existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}), self.shop, self.browser)
        # Another shopping address never substitutes for the frozen receipt.
        self.shop.cart['deliveryAddress'] = 'Annan adress 2'
        self.browser.read_order_binding.side_effect = HouseholdError('original account unavailable')
        with self.assertRaises(HouseholdError):
            restarted._orders(request)
        self.assertEqual(self.store.read()['pending_cancellation']['status'], 'uncertain')
        self.browser.read_order_binding.side_effect = None
        result = restarted._orders(request)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['payment_resolution'], {'authorization_release': 'unknown', 'refund': 'unknown'})
        self.assertTrue(restarted._orders(request)['idempotent'])
        self.assertEqual(self.browser.cancel_clicks, 1)
        self.assertEqual(self.browser.read_order_binding.call_args.kwargs['expected_binding'], binding)

    def test_mathem_begin_ensure_and_retain_abort_preserve_order_and_cart(self):
        self.shop.orders.append(self.order())
        original_order = deepcopy(self.shop.orders)
        self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
        binding = {'account_reference_digest': 'a' * 64, 'receipt_address': 'Exempelvägen 1'}
        self.browser.read_order_binding = mock.Mock(return_value=binding)
        begun = self.app._orders({'action': 'change_begin', 'order_id': '123456'})
        self.assertTrue(begun['editing'])
        self.assertEqual(begun['provider'], 'mathem')
        self.assertEqual(self.store.read()['order_change']['binding'], binding)
        self.app._cart({'action': 'ensure', 'requirements': [{'product_id': 4694, 'product_name': 'Pasta Fusilli', 'quantity': 1}]})
        self.assertNotIn('manipulate_cart', [name for name, _ in self.shop.calls])
        self.shop.fail_after_write = True
        with self.assertRaises(HouseholdError):
            self.app._cart({'action': 'ensure', 'requirements': [{'product_id': 4694, 'product_name': 'Pasta Fusilli', 'quantity': 2}]})
        with self.assertRaisesRegex(HouseholdError, 'reconcile_change'):
            self.app._orders({'action': 'change_abort', 'order_id': '123456', 'retain_cart': True})
        self.app._cart({'action': 'reconcile_change'})
        self.assertEqual(self.store.read()['order_change']['expected_cart_quantities'], {'4694': 1})
        with self.assertRaisesRegex(HouseholdError, 'retain_cart'):
            self.app._orders({'action': 'change_abort', 'order_id': '123456'})
        self.assertTrue(self.app._orders({'action': 'change_abort', 'order_id': '123456', 'retain_cart': True})['cart_retained'])
        staged = deepcopy(self.shop.cart)
        review = self.app._orders({'action': 'change_begin', 'order_id': '123456'})
        self.assertTrue(review['cart_confirmation_required'])
        self.assertIsNone(self.store.read()['order_change'])
        self.app._orders({'action': 'change_begin', 'order_id': '123456', 'cart_digest': review['cart_digest']})
        self.assertEqual(self.store.read()['order_change']['starting_cart_quantities'], {'4694': 1})
        self.assertEqual(self.shop.orders, original_order)
        self.assertEqual(self.shop.cart, staged)
        self.assertEqual([name for name, _ in self.shop.calls].count('manipulate_cart'), 1)

    def test_new_card_dispatch_lost_before_observation_cannot_prepare_another_payment(self):
        def lost_payment(cart, review, before_click, **kwargs):
            before_click()
            self.browser.checkout_clicks += 1
            self.shop.orders.append(self.order())
            self.shop.tracking = 'unpaid_order'
            self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
            raise HouseholdError('lost before first payment observation')
        self.browser.submit_checkout = lost_payment
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        with self.assertRaisesRegex(HouseholdError, 'lost before first'):
            self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        pending = self.store.read()['pending_checkout']
        self.assertTrue(pending['authentication_unresolved'])
        self.assertNotIn('authentication_context', pending)
        self.browser.review_payment_recovery = mock.Mock(side_effect=AssertionError('must not navigate or retry'))
        restarted = Application(StateStore(self.store.path.parent, {
            **existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}), self.shop, self.browser)
        for action, args in [('reconcile', {'confirmation_id': prepared['confirmation_id']}), ('prepare', {'recovery': True})]:
            result = restarted.handle({'operation': 'checkout', 'action': action, **args})
            self.assertFalse(result['recovery_preparation_available'])
            self.assertEqual(result['authentication_status'], 'unavailable')
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.browser.review_payment_recovery.assert_not_called()

    def test_new_card_success_page_waits_for_merchant_confirmation_without_repayment(self):
        def payment_success_page(cart, review, before_click, **kwargs):
            before_click()
            self.browser.checkout_clicks += 1
            self.shop.orders.append(self.order())
            self.shop.tracking = 'unpaid_order'
            self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
            return None
        self.browser.submit_checkout = payment_success_page
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        result = self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['recovery_preparation_available'])
        self.assertTrue(self.store.read()['pending_checkout']['authentication_unresolved'])
        self.browser.review_payment_recovery = mock.Mock(side_effect=AssertionError('must not navigate or retry'))
        restarted = Application(StateStore(self.store.path.parent, {
            **existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}), self.shop, self.browser)
        with self.assertRaisesRegex(HouseholdError, 'no fresh checkout confirmation'):
            restarted.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        for action, args in [('reconcile', {'confirmation_id': prepared['confirmation_id']}), ('prepare', {'recovery': True})]:
            waiting = restarted.handle({'operation': 'checkout', 'action': action, **args})
            self.assertFalse(waiting['recovery_preparation_available'])
            self.assertFalse(waiting['retry_allowed'])
        self.browser.read_order_binding = mock.Mock(side_effect=lambda *a, expected_binding, **kw: expected_binding)
        self.shop.tracking = 'paid_and_modifiable'
        self.assertTrue(restarted.handle({'operation': 'checkout', 'action': 'reconcile',
                                        'confirmation_id': prepared['confirmation_id']})['confirmed'])
        self.assertIsNone(self.store.read()['pending_checkout'])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.browser.review_payment_recovery.assert_not_called()

    def test_original_addition_bank_challenge_is_persisted_and_reconciled_after_restart(self):
        self.shop.orders.append(self.order())
        self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
        binding = {'account_reference_digest': 'a' * 64, 'receipt_address': 'Exempelvägen 1'}
        self.browser.read_order_binding = mock.Mock(return_value=binding)
        self.app._orders({'action': 'change_begin', 'order_id': '123456'})
        self.app._cart({'action': 'change', 'operations': [{'productId': 4904, 'quantity': 1}]})
        amounts = {key: None for key in self.amounts}
        amounts.update(product_subtotal=29.9, provider_total=29.9)
        self.browser.review_order_change = mock.Mock(return_value={
            'payment_display': '•••• 1234', 'binding': binding, 'amounts': amounts,
            'order_amounts': {'original_minor': 12195, 'added_minor': 2990, 'payable_minor': 2990,
                             'combined_minor': 15185, 'original_count': 1, 'added_count': 1, 'combined_count': 2}})
        context = {'tab_id': 'owned', 'payment_id': '123456'}
        def challenge_payment(cart, order_id, order, review, before_click, **kwargs):
            before_click()
            self.browser.checkout_clicks += 1
            self.shop.tracking = 'unpaid_order_change'
            self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
            return {'authentication_context': context}
        self.browser.submit_order_change = challenge_payment
        self.browser.checkout_payment_authentication = mock.Mock(return_value={'active': True, 'challenge': True})
        self.browser.checkout_payment_failure = mock.Mock(return_value=None)
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        result = self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        self.assertTrue(result['authentication_required'])
        self.assertEqual(result['payment_method'], 'saved_card')
        self.assertFalse(result['retry_allowed'])
        self.assertFalse(result['recovery_preparation_available'])
        self.assertEqual(self.store.read()['pending_checkout']['authentication_context'], context)
        self.browser.read_order_binding.reset_mock()
        restarted = Application(StateStore(self.store.path.parent, {
            **existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}), self.shop, self.browser)
        self.browser.checkout_payment_authentication.return_value = None
        for action, arguments in [('reconcile', {'confirmation_id': prepared['confirmation_id']}),
                                  ('prepare', {'recovery': True})]:
            result = restarted.handle({'operation': 'checkout', 'action': action, **arguments})
            self.assertEqual(result['authentication_status'], 'unavailable')
            self.assertFalse(result['recovery_preparation_available'])
        self.browser.read_order_binding.assert_not_called()
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.shop.orders[0]['products'].append({'product_id': 4904, 'quantity': 1})
        self.shop.orders[0]['grossAmount'] = 151.85
        self.shop.tracking = 'paid_and_modifiable'
        result = restarted.handle({'operation': 'checkout', 'action': 'reconcile', 'confirmation_id': prepared['confirmation_id']})
        self.assertTrue(result['confirmed'])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertIsNone(self.store.read()['pending_checkout'])

    def test_original_addition_dispatch_retains_target_and_prepares_after_restart(self):
        self.shop.orders.append(self.order())
        self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
        binding = {'account_reference_digest': 'a' * 64, 'receipt_address': 'Exempelvägen 1'}
        self.browser.read_order_binding = mock.Mock(return_value=binding)
        self.app._orders({'action': 'change_begin', 'order_id': '123456'})
        self.app._cart({'action': 'change', 'operations': [{'productId': 4904, 'quantity': 1}]})
        amounts = {key: None for key in self.amounts}
        amounts.update(product_subtotal=29.9, provider_total=29.9)
        self.browser.review_order_change = mock.Mock(return_value={
            'payment_display': '•••• 1234', 'binding': binding, 'amounts': amounts,
            'order_amounts': {'original_minor': 12195, 'added_minor': 2990, 'payable_minor': 2990,
                             'combined_minor': 15185, 'original_count': 1, 'added_count': 1, 'combined_count': 2}})
        target = {'payment_failed': True, 'order_id': '123456', 'order_change_id': 'change-1'}
        def failed_payment(cart, order_id, order, review, before_click, **kwargs):
            before_click()
            self.browser.checkout_clicks += 1
            self.shop.tracking = 'unpaid_order_change'
            self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
            return deepcopy(target)
        self.browser.submit_order_change = failed_payment
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        result = self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        self.assertFalse(result['confirmed'])
        self.assertTrue(result['payment_failed'])
        self.assertTrue(result['recovery_preparation_available'])
        pending = self.store.read()['pending_checkout']
        self.assertEqual(pending['payment_failure'], target)
        self.assertEqual(pending['summary']['items'], prepared['summary']['items'])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(self.shop.orders, [self.order()])
        restarted = Application(StateStore(self.store.path.parent, {
            **existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}), self.shop, self.browser)
        def review_recovery(cart, order_id, *, payment, expected_binding, addition, **kwargs):
            self.assertEqual(order_id, target['order_id'])
            self.assertEqual(addition['order_change_id'], target['order_change_id'])
            self.assertEqual(expected_binding, binding)
            return {'order_id': order_id, 'binding': binding, 'payment_choice': payment,
                    'payment_display': '•••• 1234', 'amounts_minor': {
                        **_oda_checkout_amounts_minor(amounts, provider='mathem'), 'provider_total': 2991}}
        self.browser.review_payment_recovery = review_recovery
        before_calls = len(self.shop.calls)
        recovery = restarted.handle({'operation': 'checkout', 'action': 'prepare', 'recovery': True})
        self.assertEqual(recovery['summary']['total'], 29.90)
        self.assertEqual(recovery['summary']['merchant_summary_total'], 29.91)
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertNotIn('get_cart', [name for name, _ in self.shop.calls[before_calls:]])
        self.assertEqual({k: v for k, v in self.store.read()['pending_checkout'].items() if k != 'recovery'}, pending)

    def test_addition_prepare_freezes_original_delivery_populated_by_checkout(self):
        self.shop.orders.append(self.order())
        self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
        binding = {'account_reference_digest': 'a' * 64, 'receipt_address': 'Exempelvägen 1'}
        self.browser.read_order_binding = mock.Mock(return_value=binding)
        self.app._orders({'action': 'change_begin', 'order_id': '123456'})
        self.app._cart({'action': 'change', 'operations': [{'productId': 4904, 'quantity': 1}]})
        amounts = {key: None for key in self.amounts}
        amounts.update(product_subtotal=29.9, provider_total=29.9)
        slotless = deepcopy(self.shop.cart)
        def checkout_populates_delivery(*args, **kwargs):
            self.shop.cart['deliverySlot'] = deepcopy(self.cart['deliverySlot'])
            self.shop.cart['deliveryAddress'] = binding['receipt_address']
            return {'payment_display': '•••• 1234', 'binding': binding, 'amounts': amounts,
                    'order_amounts': {'original_minor': 12195, 'added_minor': 2990, 'payable_minor': 2990,
                                     'combined_minor': 15185, 'original_count': 1, 'added_count': 1, 'combined_count': 2}}
        def changed_checkout(mutate):
            def review(*args, **kwargs):
                result = checkout_populates_delivery(*args, **kwargs)
                mutate(self.shop.cart)
                return result
            return review
        # Only the independently bound original delivery may appear during review.
        changes = [lambda c: c.update(subtotal=30),
                   lambda c: c['items'][0].update(quantity=2),
                   lambda c: c.update(deliveryAddress='Annan väg 2'),
                   lambda c: c['deliverySlot'].update(name='Hemleverans mellan 14 och 16, 13. sep')]
        for mutate in changes:
            with self.subTest(mutate=mutate):
                self.shop.cart = deepcopy(slotless)
                self.browser.review_order_change = changed_checkout(mutate)
                with self.assertRaises(HouseholdError):
                    self.app.handle({'operation': 'checkout', 'action': 'prepare'})
                self.assertIsNone(self.store.read()['pending_checkout'])
                self.assertEqual(self.browser.checkout_clicks, 0)
        self.shop.cart = deepcopy(slotless)
        self.browser.review_order_change = checkout_populates_delivery
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertEqual(self.store.read()['pending_checkout']['cart'], self.shop.cart)
        result = self.app.handle({'operation': 'checkout', 'action': 'confirm',
                                  'confirmation_id': prepared['confirmation_id']})
        self.assertTrue(result['confirmed'])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(len(self.shop.orders), 1)
        self.assertEqual(self.shop.orders[0]['grossAmount'], 151.85)

    def test_mathem_addition_without_cart_slot_recovers_one_dispatch(self):
        self.shop.orders.append(self.order())
        self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
        binding = {'account_reference_digest': 'a' * 64, 'receipt_address': 'Exempelvägen 1'}
        self.browser.read_order_binding = mock.Mock(return_value=binding)
        self.app._orders({'action': 'change_begin', 'order_id': '123456'})
        self.app._cart({'action': 'change', 'operations': [{'productId': 4904, 'quantity': 1}]})
        amounts = {key: None for key in self.amounts}
        amounts.update(product_subtotal=29.9, provider_total=29.9)
        self.browser.review_order_change = mock.Mock(return_value={
            'payment_display': '•••• 1234', 'binding': binding, 'amounts': amounts,
            'order_amounts': {'original_minor': 12195, 'added_minor': 2990, 'payable_minor': 2990,
                             'combined_minor': 15185, 'original_count': 1, 'added_count': 1, 'combined_count': 2}})
        original = self.browser.submit_order_change
        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise HouseholdError('synthetic lost addition response after effect')
        self.browser.submit_order_change = lost_response
        self.browser.submit_checkout = mock.Mock(side_effect=AssertionError('must not create a new order'))
        prepared = self.app._checkout_prepare()
        self.assertEqual(prepared['summary']['delivery']['selection_origin'], 'existing_order')
        self.assertEqual(prepared['summary']['delivery']['address'], binding['receipt_address'])
        self.assertEqual(prepared['summary']['total'], 29.9)
        self.assertEqual(prepared['summary']['order_amounts']['combined_minor'], 15185)
        self.assertIsNone(self.store.read()['pending_checkout']['cart']['delivery'])
        request = {'operation': 'checkout', 'action': 'submit', 'idempotency_key': 'existing-addition'}
        with self.assertRaises(HouseholdError):
            self.app.handle(request)
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(len(self.shop.orders), 1)
        self.assertEqual(self.store.read()['pending_checkout']['status'], 'uncertain')
        restarted = Application(StateStore(self.store.path.parent, {
            **existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}), self.shop, self.browser)
        self.shop.tracking = 'unpaid_order_change'
        self.browser.read_order_binding.reset_mock()
        waiting = restarted.handle(request)
        self.assertFalse(waiting['confirmed'])
        self.assertEqual(waiting['tracking_status'], 'unpaid_order_change')
        self.assertFalse(waiting['retry_allowed'])
        self.browser.read_order_binding.assert_not_called()
        self.shop.tracking = 'paid_and_modifiable'
        self.browser.read_order_binding.side_effect = HouseholdError('original account unavailable')
        with self.assertRaises(HouseholdError): restarted.handle(request)
        self.assertEqual(self.store.read()['pending_checkout']['status'], 'uncertain')
        self.browser.read_order_binding.side_effect = None
        result = restarted.handle(request)
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['changed_existing_order'])
        self.assertTrue(restarted.handle(request)['idempotent'])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertNotIn('select_delivery_slot', [name for name, _ in self.shop.calls])

    def test_mathem_delivery_change_lost_response_retains_exact_order_and_year(self):
        before = self.order()
        before.update(deliveryDate='2026-09-13', deliverySlotDisplay='Sön 13. sep 09:00 - 12:00')
        self.shop.orders.append(before)
        self.shop.cart = {'items': [], 'subtotal': 0, 'delivery': None}
        binding = {'account_reference_digest': 'a' * 64, 'receipt_address': 'Exempelvägen 1'}
        self.browser.read_order_binding = mock.Mock(return_value=binding)
        original_review = self.browser.review_delivery_change
        self.browser.review_delivery_change = lambda *a, expected_binding=None, **kw: original_review(*a, **kw)
        self.app._orders({'action': 'change_begin', 'order_id': '123456'})
        selected = self.app._delivery({'action': 'select', 'slot_ref': 'mathem:2026-09-12:77'})
        self.assertEqual(selected['staged_for_order'], '123456')
        prepared = self.app._checkout_prepare()
        self.assertEqual(prepared['summary']['total'], 0)
        original_submit = self.browser.submit_delivery_change
        def lost(*a, **kw):
            original_submit(*a, **kw)
            raise HouseholdError('lost response after the one delivery confirmation')
        self.browser.submit_delivery_change = lost
        self.browser.submit_checkout = mock.Mock(side_effect=AssertionError('no new order'))
        request = {'operation': 'checkout', 'action': 'submit', 'idempotency_key': 'one-delivery-change'}
        with self.assertRaises(HouseholdError): self.app.handle(request)
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertNotIn('authentication_unresolved', self.store.read()['pending_checkout'])
        restarted = Application(StateStore(self.store.path.parent, {
            **existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}), self.shop, self.browser)
        self.browser.read_order_binding.reset_mock()
        for drift in ({'deliveryDate': '2027-09-12'}, {'grossAmount': 122.95}, {'currency': 'NOK'},
                      {'products': [{**before['products'][0], 'quantity': 2}]}):
            current = deepcopy(self.shop.orders[0]); self.shop.orders[0].update(drift)
            result = restarted.handle(request)
            self.assertFalse(result['confirmed'])
            self.assertFalse(result['retry_allowed'])
            self.browser.read_order_binding.assert_not_called()
            self.shop.orders[0] = current
        self.browser.read_order_binding.side_effect = HouseholdError('original receipt account unavailable')
        with self.assertRaises(HouseholdError): restarted.handle(request)
        self.browser.read_order_binding.side_effect = None
        self.assertTrue(restarted.handle(request)['confirmed'])
        self.assertTrue(restarted.handle(request)['idempotent'])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(len(self.shop.orders), 1)
        self.assertEqual([name for name, _ in self.shop.calls].count('select_delivery_slot'), 1)

    def test_mathem_cancellation_last_expiry_check_and_unknown_click_are_distinct(self):
        from core import CancellationPreconditionError
        import time
        native = MathemBrowser.__new__(MathemBrowser)
        native._checkout_deadline = None
        native._invoke = mock.Mock(return_value={})
        native.provider_client = self.shop
        self.shop.orders.append(self.order())
        review = {'available': True, 'binding': {'receipt_address': 'Exempelvägen 1'}, 'receipt': {}}
        native._review_mathem_cancellation = mock.Mock(return_value=review)
        native._eval = mock.Mock(side_effect=HouseholdError('lost final response'))
        callback = mock.Mock(side_effect=CancellationPreconditionError('expired during last provider reads'))
        with self.assertRaises(CancellationPreconditionError):
            native.submit_cancellation('123456', self.order(), review, callback, deadline=time.monotonic()+60)
        callback.assert_called_once()
        native._eval.assert_not_called()
        with self.assertRaises(HouseholdError) as raised:
            native.submit_cancellation('123456', self.order(), review, deadline=time.monotonic()+60)
        self.assertNotIsInstance(raised.exception, CancellationPreconditionError)
        native._eval.assert_called_once()

    @unittest.skipUnless(shutil.which('node'), 'Node executes the persisted cancellation final guard')
    def test_cancellation_final_receipt_survives_sorted_state_but_rejects_drift(self):
        from core import CancellationPreconditionError
        native = MathemBrowser.__new__(MathemBrowser)
        native._checkout_deadline = None
        native._invoke = mock.Mock()
        native.provider_client = self.shop
        self.shop.orders.append(self.order())
        receipt = {'available': True, 'delivery_lines': ['Lör 12. sep 09:00 - 12:00'],
                   'total_rows': ['Totalt inkl. moms 121,95 kr'], 'deadline_text': ['Före imorgon 23:59'],
                   'addition_deadline_text': ['Lägg till före imorgon 23:59']}
        review = {'available': True, 'binding': {'receipt_address': 'Exempelvägen 1'},
                  'receipt': receipt, 'consequence': 'Cannot be undone'}
        self.browser.review_cancellation = mock.Mock(return_value=deepcopy(review))
        self.app._orders({'action': 'cancel_prepare', 'order_id': '123456'})
        persisted = self.store.read()['pending_cancellation']['browser']
        self.assertEqual(persisted, review)
        self.assertNotEqual(list(persisted['receipt']), list(receipt))
        native._review_mathem_cancellation = mock.Mock(return_value=deepcopy(review))
        native._cancellation_dialog_script = lambda action: "(() => {clicks++;return JSON.stringify({clicked:true});})()"
        for drift in (None, {'total_rows': ['Totalt inkl. moms 121,96 kr']},
                      {'delivery_lines': ['Lör 12. sep 10:00 - 12:00']}, {'deadline_text': []}, {'unexpected': True}):
            actual = {**receipt, **(drift or {})}
            native._cancellation_receipt_script = lambda *a: 'JSON.stringify(' + json.dumps(actual) + ')'
            observed = []
            def evaluate(script):
                harness = "let clicks=0;const result=JSON.parse(eval(require('node:fs').readFileSync(0,'utf8')));process.stdout.write(JSON.stringify({result,clicks}));"
                run = subprocess.run([shutil.which('node'), '-e', harness], input=script, capture_output=True,
                                     text=True, check=True, timeout=10)
                value = json.loads(run.stdout); observed.append(value); return value['result']
            native._eval = evaluate
            if drift is None:
                native.submit_cancellation('123456', self.order(), persisted)
            else:
                with self.assertRaises(CancellationPreconditionError): native.submit_cancellation('123456', self.order(), persisted)
            self.assertEqual(observed[-1]['clicks'], 1 if drift is None else 0)

    def _automatic_dietary_fixture(self):
        self.app.handle({'operation': 'profile', 'action': 'update', 'changes': {'diet': {
            'rules': [{'kind': 'allergy', 'term': 'sesam'}],
            'uncertainty_permissions': [{'kind': 'allergy', 'term': 'sesam', 'product_ref': '4694',
                'condition': 'unknown', 'accepted': True, 'notify': True}]}}})
        from dietary_assessment import parse_retail_product_page
        self.shop.product_dietary_evidence = lambda ref, **kw: parse_retail_product_page(
            MathemDietaryDetailTests.HTML, f'https://www.mathem.se/se/products/{ref}-pasta/', provider='mathem')
        self.app.handle({'operation': 'schedule', 'action': 'update', 'changes': {
            'enabled': True, 'maximum_total': 150, 'auto_checkout': True,
            'delivery': {'weekday': 'Saturday', 'strategy': 'keep_selected'}}})
        self.app.handle({'operation': 'schedule', 'action': 'set_cron_job', 'cron_job_id': 'synthetic-mathem-weekly'})

    @mock.patch.object(Application, '_now', return_value=datetime(2026, 9, 10, 13, 5, tzinfo=timezone.utc))
    def test_expired_undispatched_interactive_review_allows_fresh_auto_review(self, _clock):
        self._automatic_dietary_fixture()
        old = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        request = {'operation': 'checkout', 'action': 'auto', 'occurrence': '2026-W37'}
        pending = self.store.read()['pending_checkout']
        # Neither a live interactive review, unknown expiry nor a possibly
        # dispatched checkout can be discarded by an automatic occurrence.
        for status, expiry in [('awaiting_confirmation', pending['expires_at']),
                               ('awaiting_confirmation', 'unknown'),
                               ('uncertain', '2026-09-10T13:04:00+00:00')]:
            with self.subTest(status=status, expiry=expiry):
                with self.store.locked() as state:
                    state['pending_checkout'] = {**deepcopy(pending), 'status': status, 'expires_at': expiry}
                before = self.store.read()['pending_checkout']
                with self.assertRaises(HouseholdError):
                    self.app.handle(request)
                self.assertEqual(self.store.read()['pending_checkout'], before)
                self.assertEqual(self.browser.checkout_clicks, 0)
        with self.store.locked() as state:
            state['pending_checkout'] = {**deepcopy(pending), 'expires_at': '2026-09-10T13:04:00+00:00'}
        fresh = self.app.handle(request)
        current = self.store.read()['pending_checkout']
        self.assertNotEqual(current['confirmation_id'], old['confirmation_id'])
        self.assertTrue(current['automatic_checkout'])
        self.assertEqual(current['occurrence'], '2026-W37')
        self.assertTrue(fresh['notification_required'])
        self.assertTrue(fresh['notice']['dispatch'])
        self.assertEqual(self.browser.checkout_clicks, 0)
        self.assertEqual(self.shop.orders, [])

    @mock.patch.object(Application, '_now', return_value=datetime(2026, 9, 10, 13, 5, tzinfo=timezone.utc))
    def test_mathem_auto_notice_lost_payment_and_result_replay(self, _clock):
        import hashlib
        self._automatic_dietary_fixture()
        request = {'operation': 'checkout', 'action': 'auto', 'occurrence': '2026-W37'}
        before = self.app.handle(request)
        notice = before['notice']
        self.assertTrue(before['notification_required']); self.assertTrue(notice['dispatch'])
        self.assertEqual(self.browser.checkout_clicks, 0)
        self.assertIn('4694', notice['payload']['message'])
        inbox = self.root / 'local-notice.txt'
        payload = notice['payload']['message'].encode()
        inbox.write_bytes(payload); self.assertEqual(inbox.read_bytes(), payload)
        receipt = 'synthetic-local:' + hashlib.sha256(inbox.read_bytes()).hexdigest()
        self.app.handle({'operation': 'checkout', 'action': 'notice_result', 'notice_token': notice['notice_token'],
                         'send_outcome': 'sent', 'sender_receipt': receipt})
        def lost(cart, review, before_click, **kwargs):
            before_click(); self.browser.checkout_clicks += 1
            self.shop.orders.append(self.order())
            raise HouseholdError('synthetic Mathem accepted with response lost')
        self.browser.submit_checkout = lost
        with self.assertRaisesRegex(HouseholdError, 'response lost'):
            self.app.handle(request)
        pending = self.store.read()['pending_checkout']
        self.assertEqual(pending['status'], 'uncertain')
        self.app = Application(StateStore(self.root / 'state', self.store.config), self.shop, self.browser)
        result = self.app.handle({'operation': 'checkout', 'action': 'reconcile', 'confirmation_id': pending['confirmation_id']})
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['payment'], {'provider_status': 'paid_and_modifiable', 'source': 'order_tracking',
                                           'authorization': 'unknown', 'charge': 'unknown'})
        after = result['notice']
        self.assertEqual(after['phase'], 'after_reconciliation')
        options = after['payload']['correction_options']
        self.assertEqual(options['edit_availability'], 'additions_only')
        self.assertIsNone(options['deadline'])
        self.assertEqual(options['deadline_status'], 'provider_reported_text')
        self.assertIn('12. september', options['deadline_text'])
        self.assertIn(options['deadline_text'], after['payload']['message'])
        self.assertIn('removal, replacement, refund and payment release are not promised', options['message'].casefold())
        self.assertIn('zero additional payment', options['message'])
        self.browser.order_followup.assert_called_once()
        self.app.handle({'operation': 'checkout', 'action': 'notice_result', 'notice_token': after['notice_token'],
                         'send_outcome': 'unknown', 'sender_receipt': 'synthetic-result-ack-lost'})
        replay = self.app.handle(request)
        self.assertTrue(replay['confirmed']); self.assertEqual(replay['payment'], result['payment'])
        self.assertEqual(replay['notice']['notice_token'], after['notice_token'])
        self.assertEqual(replay['notice']['payload'], after['payload'])
        self.browser.order_followup.assert_called_once()
        self.assertFalse(replay['notice']['dispatch']); self.assertFalse(replay['notice']['delivered'])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(len(self.shop.orders), 1)

    @mock.patch.object(Application, '_now', return_value=datetime(2026, 9, 10, 13, 5, tzinfo=timezone.utc))
    def test_mathem_auto_blocks_unverified_notice_and_changed_final_evidence(self, _clock):
        self._automatic_dietary_fixture()
        request = {'operation': 'checkout', 'action': 'auto', 'occurrence': '2026-W37'}
        notice = self.app.handle(request)['notice']
        for outcome in ('not_sent', 'unknown'):
            self.app.handle({'operation': 'checkout', 'action': 'notice_result', 'notice_token': notice['notice_token'],
                             'send_outcome': outcome, 'sender_receipt': 'synthetic:' + outcome})
            stopped = self.app.handle(request)
            self.assertFalse(stopped['notice']['dispatch']); self.assertFalse(stopped['notice']['delivered'])
            self.assertEqual(self.browser.checkout_clicks, 0)
        self.shop.product_dietary_evidence = lambda ref, **kw: {
            'source_url': f'https://www.mathem.se/se/products/{ref}-pasta/', 'allergens': 'Sesam'}
        changed = self.app.handle(request)
        self.assertTrue(changed['reprepared'])
        finding = changed['summary']['dietary_assessment']['findings'][0]
        self.assertEqual((finding['product_ref'], finding['condition']), ('4694', 'conflict'))
        self.assertTrue(self.app.handle(request)['dietary_review_required'])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_swedish_final_detail_preserves_dietary_categories(self):
        from dietary_assessment import parse_retail_product_page
        self.shop.product_dietary_evidence = lambda ref, **kw: parse_retail_product_page(
            MathemDietaryDetailTests.HTML, f'https://www.mathem.se/se/products/{ref}-pasta/', provider='mathem')
        cases = [
            ({'rules': [{'kind': 'preference', 'term': 'durumvete'}]}, 'preference', 'preference_deviation', False),
            ({'allergies_or_sensitivities': ['sesam']}, 'allergy_or_sensitivity', 'unknown', False),
            ({'rules': [{'kind': 'allergy', 'term': 'sesam'}]}, 'allergy', 'unknown', False),
            ({'rules': [{'kind': 'allergy', 'term': 'durumvete'}]}, 'allergy', 'conflict', True),
        ]
        for diet, kind, condition, blocked in cases:
            with self.subTest(kind=kind, condition=condition):
                self.app.handle({'operation': 'profile', 'action': 'update', 'changes': {
                    'diet': {'rules': [], 'allergies_or_sensitivities': [], 'uncertainty_permissions': [], **diet}}})
                prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
                finding, = prepared['summary']['dietary_assessment']['findings']
                self.assertEqual((finding['kind'], finding['condition'], finding['blocked']), (kind, condition, blocked))
                self.assertEqual(finding['product_ref'], '4694')
                self.assertEqual(finding['evidence']['source_url'], 'https://www.mathem.se/se/products/4694-pasta/')
                self.assertTrue(self.app.handle({'operation': 'checkout', 'action': 'confirm',
                    'confirmation_id': prepared['confirmation_id']})['dietary_review_required'])
                self.assertEqual(self.browser.checkout_clicks, 0)

    @mock.patch.object(Application, '_household_today', return_value=datetime(2026, 9, 10).date())
    @mock.patch.object(Application, '_now', return_value=datetime(2026, 9, 10, 13, 5, tzinfo=timezone.utc))
    def test_whole_week_shortfall_stays_visible_after_explicit_keep_current(self, _clock, _today):
        from test_meal_concierge_planner import recipe
        from product_planner import menu_requirements
        refs = []
        for index in range(7):
            value = recipe('Synthetic egg dinner ' + str(index), 'egg-week-' + str(index))
            value['ingredients'] = [{'item': 'ägg', 'quantity': 2, 'unit': 'stk', 'raw': '2 stk ägg', 'scalable': True}]
            saved = self.app.handle({'operation': 'recipes', 'action': 'save', 'recipe': value,
                                    'idempotency_key': 'egg-week-' + str(index)})['recipe']
            refs.append({'recipe_ref': {'id': saved['id'], 'revision': saved['revision']}})
        self.app.handle({'operation': 'profile', 'action': 'update', 'changes': {'recipes': {'sources': {
            'oda': False, 'mathem': False, 'meny': False, 'themealdb': False, 'wikibooks': False}}}})
        plan = self.app.handle({'operation': 'menu', 'action': 'plan', 'planner_input': {
            'week': '2026-W38', 'dates': [f'2026-09-{14+i}' for i in range(7)], 'portions': 2, 'candidates': refs}})['plan']
        menu = self.app.handle({'operation': 'menu', 'action': 'save', 'planner_ref': plan['save_ref']})['menu']
        self.assertEqual(len(menu['slots']), 7)
        menu_ref = {k: menu[k] for k in ('menu_id', 'revision', 'digest')}
        requirement, = menu_requirements(menu)[0]
        self.assertEqual(requirement['quantity'], {'numerator': 14, 'denominator': 1})
        self.shop.cart = {'items': [], 'subtotal': 0, 'deliveryAddress': 'Exempelvägen 1',
                          'delivery': {'slot_id': 77, 'display': 'Hemleverans mellan 09 och 12, 12. sep'}}
        original = self.shop.call
        def provider(tool, arguments, **kwargs):
            result = original(tool, arguments, **kwargs)
            if tool == 'product_search':
                result['scope']['requested_size'] = arguments['size']
            return result
        self.shop.call = provider
        prepared_products = self.app.handle({'operation': 'products', 'action': 'prepare', 'menu_ref': menu_ref,
            'candidate_approvals': [{'requirement_id': requirement['requirement_id'], 'candidate_refs': [10]}],
            'price_mode': 'estimate'})
        products = prepared_products['product_plan']
        self.assertEqual(products['totals']['package_count'], 3)
        arguments = prepared_products['apply_arguments']
        self.assertNotIn('product_plan', arguments)
        self.assertLess(len(json.dumps(arguments)), 2000)
        for invalid in (None, 'not-a-digest'):
            with self.subTest(invalid=invalid), self.assertRaises(HouseholdError):
                self.app.handle({'operation': 'products', **arguments,
                    'product_plan_digest': invalid, 'cart_change_requested': True})
        changed = self.app.handle({'operation': 'products', **arguments,
            'product_plan_digest': '0' * 64, 'cart_change_requested': True})
        self.assertFalse(changed['applied'])
        self.assertEqual(cart_summary(self.shop.cart)['items'], [])
        applied = self.app.handle({'operation': 'products', **arguments, 'cart_change_requested': True})
        self.assertTrue(applied['applied'])
        self.shop.cart['items'][0]['quantity'] = 2  # Synthetic external removal of one six-egg package.
        self.shop.cart['subtotal'] = 59.8
        stopped = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertTrue(stopped['cart_reconciliation_required'])
        missing, = [row for row in stopped['cart_plan']['items'] if row['missing_quantity']]
        self.assertEqual((missing['product_id'], missing['required_quantity'], missing['live_quantity'], missing['missing_quantity']), ('10', 3, 2, 1))
        kept = self.app.handle({'operation': 'cart', 'action': 'reconcile', 'menu_ref': menu_ref,
            'decision': 'keep_current', 'cart_digest': stopped['cart_plan']['cart_digest'], 'accept_missing_product_ids': ['10']})
        self.assertTrue(kept['reconciled'])
        self.browser.review_checkout = lambda cart, **kw: {'payment_display': '•••• 1234'}
        self.app.handle({'operation': 'schedule', 'action': 'update', 'changes': {
            'enabled': True, 'maximum_total': 150, 'auto_checkout': True,
            'delivery': {'weekday': 'Saturday', 'strategy': 'keep_selected'}}})
        self.app.handle({'operation': 'schedule', 'action': 'set_cron_job', 'cron_job_id': 'synthetic-egg-week'})
        automatic = self.app.handle({'operation': 'checkout', 'action': 'auto', 'occurrence': '2026-W37'})
        self.assertFalse(automatic['completed'])
        self.assertIn('required menu products are missing', automatic['reason'])
        self.assertEqual(self.browser.checkout_clicks, 0)
        with mock.patch.object(self.app, 'browser', None):
            manual = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertTrue(manual['manual_checkout_required'])
        self.assertEqual(manual['summary']['menu_shortfall'], automatic['summary']['menu_shortfall'])
        self.app.handle({'operation': 'profile', 'action': 'update', 'changes': {'diet': {
            'rules': [{'kind': 'preference', 'term': 'organic'}], 'uncertainty_permissions': [
                {'kind': 'preference', 'term': 'organic', 'product_ref': '10', 'condition': 'unknown',
                 'accepted': True, 'notify': True}]}}})
        self.shop.product_dietary_evidence = lambda *a, **kw: {'unavailable': 'synthetic'}
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertEqual(prepared['summary']['menu_attribution'], 'menu_bound')
        self.assertEqual(prepared['summary']['menu_shortfall'], [{'product_id': '10', 'name': 'Ägg',
            'required_quantity': 3, 'live_quantity': 2, 'missing_quantity': 1}])
        self.assertEqual(self.browser.checkout_clicks, 0)
        confirm = {'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']}
        notice = self.app.handle(confirm)['notice']
        self.assertIn('1 missing', notice['payload']['message'])
        inbox = self.root / 'shortfall-notice.txt'
        inbox.write_text(notice['payload']['message'])
        self.assertEqual(inbox.read_text(), notice['payload']['message'])
        self.app.handle({'operation': 'checkout', 'action': 'notice_result', 'notice_token': notice['notice_token'],
            'send_outcome': 'sent', 'sender_receipt': 'synthetic local inbox bytes verified'})
        def dispatch(cart, review, before_click, **kwargs):
            before_click()
            self.browser.checkout_clicks += 1
            self.shop.orders.append({**self.order(), 'grossAmount': 59.8,
                'products': [{'product': {'id': 10, 'name': 'Ägg'}, 'quantity': 2, 'totalGrossAmount': 59.8}]})
            raise HouseholdError('synthetic lost shortfall payment response')
        self.browser.submit_checkout = dispatch
        with self.assertRaisesRegex(HouseholdError, 'lost shortfall'):
            self.app.handle(confirm)
        result = self.app.handle({**confirm, 'action': 'reconcile'})
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['menu_shortfall'], prepared['summary']['menu_shortfall'])
        self.assertEqual(result['notice']['payload']['menu_shortfall'], result['menu_shortfall'])
        self.assertIn('1 missing', result['notice']['payload']['message'])
        restarted = Application(StateStore(self.root / 'state', self.store.config), self.shop, self.browser)
        replay = restarted.handle(confirm)
        self.assertEqual(replay['menu_shortfall'], result['menu_shortfall'])
        self.assertEqual(replay['notice']['payload'], result['notice']['payload'])
        self.assertFalse(replay['notice']['dispatch'])
        self.assertEqual(self.browser.checkout_clicks, 1)

    def test_final_mathem_product_conflict_and_substitution_scope(self):
        from dietary_assessment import parse_retail_product_page
        self.app.handle({'operation': 'profile', 'action': 'update', 'changes': {
            'diet': {'rules': [{'kind': 'never_buy', 'term': 'durumvete'}]}}})
        self.shop.product_dietary_evidence = lambda reference, **kw: parse_retail_product_page(
            MathemDietaryDetailTests.HTML, f'https://www.mathem.se/se/products/{reference}-pasta/', provider='mathem')
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        finding = prepared['summary']['dietary_assessment']['findings'][0]
        self.assertEqual((finding['product_ref'], finding['condition']), ('4694', 'conflict'))
        blocked = self.app.handle({'operation': 'checkout', 'action': 'confirm',
            'confirmation_id': prepared['confirmation_id'], 'dietary_review': [finding['finding_id']]})
        self.assertTrue(blocked['dietary_review_required']); self.assertEqual(self.browser.checkout_clicks, 0)
        self.app.handle({'operation': 'profile', 'action': 'update', 'changes': {'diet': {
            'rules': [{'kind': 'preference', 'term': 'sugar'}], 'uncertainty_permissions': [
                {'kind': 'preference', 'term': 'sugar', 'product_ref': '4694', 'condition': 'unknown', 'accepted': True, 'notify': True}]}}})
        self.shop.product_dietary_evidence = lambda reference, **kw: {'unavailable': 'synthetic_detail_unavailable'}
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        notice = self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})['notice']
        self.assertEqual(notice['phase'], 'before_dispatch')
        self.assertIn('4694', notice['payload']['message'])
        self.shop.cart['items'][0]['product']['id'] = 5627
        self.shop.cart['items'][0]['product']['name'] = 'Pasta Fusilli Glutenfri'
        changed = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        finding = changed['summary']['dietary_assessment']['findings'][0]
        self.assertEqual((finding['product_ref'], finding['condition']), ('5627', 'unknown'))
        stopped = self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': changed['confirmation_id']})
        self.assertTrue(stopped['dietary_review_required']); self.assertEqual(self.browser.checkout_clicks, 0)

    def test_lost_payment_response_reconciles_once(self):
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        def dispatch(cart, review, before_click, **kwargs):
            before_click()
            self.assertEqual(self.store.read()['pending_checkout']['status'], 'clicking')
            self.browser.checkout_clicks += 1
            self.shop.orders.append(self.order())
            raise HouseholdError('synthetic lost response after payment')
        self.browser.submit_checkout = dispatch
        with self.assertRaisesRegex(HouseholdError, 'lost response'):
            self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        self.assertEqual(self.store.read()['pending_checkout']['status'], 'uncertain')
        self.shop.tracking = 'unpaid'
        original_reader = self.browser.receipt_address_matches
        with mock.patch.object(self.browser, 'receipt_address_matches') as receipt:
            waiting = self.app.handle({'operation': 'checkout', 'action': 'reconcile', 'confirmation_id': prepared['confirmation_id']})
            self.assertFalse(waiting['confirmed'])
            self.assertFalse(waiting['retry_allowed'])
            self.assertEqual(waiting['tracking_status'], 'unpaid')
            self.assertEqual(waiting['payment']['charge'], 'unknown')
            receipt.assert_not_called()
        self.browser.receipt_address_matches = original_reader
        self.shop.tracking = 'paid_and_modifiable'
        self.shop.orders[0]['grossAmount'] = 121.96
        with mock.patch.object(self.browser, 'receipt_address_matches') as receipt:
            unrelated = self.app.handle({'operation': 'checkout', 'action': 'reconcile', 'confirmation_id': prepared['confirmation_id']})
            self.assertFalse(unrelated['confirmed'])
            receipt.assert_not_called()
        self.shop.orders[0]['grossAmount'] = 121.95
        result = self.app.handle({'operation': 'checkout', 'action': 'reconcile', 'confirmation_id': prepared['confirmation_id']})
        self.assertTrue(result['confirmed'])
        again = self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        self.assertTrue(again['confirmed'])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(again['payment'], result['payment'])
        self.assertEqual(result['payment'], {'provider_status': 'paid_and_modifiable',
            'source': 'order_tracking', 'authorization': 'unknown', 'charge': 'unknown'})

    def test_lost_submit_survives_restart_expiry_and_delayed_receipt(self):
        def dispatch(cart, review, before_click, **kwargs):
            before_click(); self.browser.checkout_clicks += 1
            raise HouseholdError('synthetic lost final response')
        self.browser.submit_checkout = dispatch
        with self.assertRaisesRegex(HouseholdError, 'lost final response'):
            self.app.handle({'operation': 'checkout', 'action': 'submit', 'idempotency_key': 'mathem-delayed'})
        pending = self.store.read()['pending_checkout']
        self.app = Application(StateStore(self.root / 'state', {**existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}), self.shop, self.browser)
        from datetime import timedelta
        after_expiry = datetime.fromisoformat(pending['expires_at']) + timedelta(hours=1)
        with mock.patch.object(self.app, '_now', return_value=after_expiry):
            unresolved = self.app.handle({'operation': 'checkout', 'action': 'submit', 'idempotency_key': 'mathem-delayed'})
            self.assertFalse(unresolved['confirmed']); self.assertFalse(unresolved['retry_allowed'])
            self.assertEqual(self.store.read()['pending_checkout']['status'], 'uncertain')
            self.shop.orders.append(self.order())
            self.browser.receipt_address_matches = lambda *a, **kw: False
            unresolved = self.app.handle({'operation': 'checkout', 'action': 'submit', 'idempotency_key': 'mathem-delayed'})
            self.assertFalse(unresolved['confirmed']); self.assertFalse(unresolved['retry_allowed'])
            self.browser.receipt_address_matches = lambda *a, **kw: True
            recovered = self.app.handle({'operation': 'checkout', 'action': 'submit', 'idempotency_key': 'mathem-delayed'})
            self.assertTrue(recovered['confirmed'])
            repeated = self.app.handle({'operation': 'checkout', 'action': 'submit', 'idempotency_key': 'mathem-delayed'})
            self.assertEqual(repeated['payment'], recovered['payment'])
        self.assertEqual(self.browser.checkout_clicks, 1)

    def test_changed_cart_inside_browser_review_stops_final_dispatch(self):
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        def dispatch(cart, review, before_click, **kwargs):
            self.shop.cart['items'][0]['quantity'] = 2
            before_click()
            self.browser.checkout_clicks += 1
        self.browser.submit_checkout = dispatch
        with self.assertRaisesRegex(HouseholdError, 'before the final click'):
            self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        self.assertEqual(self.browser.checkout_clicks, 0)
        self.assertIsNone(self.store.read()['pending_checkout'])

    def test_review_waits_for_complete_checkout_and_still_rejects_missing_or_drifted_fields(self):
        native = MathemBrowser.__new__(MathemBrowser)
        expected = native._cart_expectation(self.cart)
        ready = {'url': native.checkout_url, 'authenticated': True, 'available': True,
            'items': [{'quantity': line['quantity'], 'text': line['identity']} for line in expected['lines']],
            'delivery_roots': [expected['delivery_text']], 'address_matches': True,
            'payment_display': '•••• 1234', 'submit_controls': 1}
        native._account_reference = mock.Mock(return_value=123)
        native._open = mock.Mock()
        native._navigate_to_checkout = mock.Mock()
        native._settle = mock.Mock()
        native._read_checkout_amounts = mock.Mock(side_effect=lambda *a: {**deepcopy(self.amounts), 'discount_breakdown': deepcopy(self.discount_breakdown)})
        native._invoke = mock.Mock(side_effect=AssertionError('no payment dispatch during review'))
        for delta in ({'items': []}, {'delivery_roots': []}, {'payment_display': None}, {'submit_controls': 0}):
            with self.subTest(delayed=delta):
                native._eval = mock.Mock(side_effect=[{'account_matches': True}, {'ready': True},
                    {**ready, **delta}, ready])
                review = native._review_checkout(self.cart)
                self.assertEqual(review['payment_display'], '•••• 1234')
                self.assertEqual(review['amounts'], self.amounts)
        for delta in ({'payment_display': None}, {'submit_controls': 0}, {'address_matches': False},
                      {'items': []}, {'delivery_roots': []}):
            with self.subTest(persistent=delta):
                native._read_checkout_amounts.reset_mock()
                native._eval = mock.Mock(side_effect=[{'account_matches': True}, {'ready': True}]
                    + [{**ready, **delta}] * 20)
                with self.assertRaises(HouseholdError): native._review_checkout(self.cart)
                native._read_checkout_amounts.assert_not_called()
        native._invoke.assert_not_called()

    def test_slow_account_read_cannot_outlive_checkout_confirmation(self):
        import hashlib
        native = MathemBrowser.__new__(MathemBrowser)
        native._checkout_dispatch_tab = lambda: None
        native._checkout_deadline = None
        review = {**self.browser.review_checkout(self.cart),
                  'account_reference_digest': hashlib.sha256(b'123').hexdigest(),
                  'url': MathemBrowser.checkout_url, 'authenticated': True, 'available': True,
                  'items': [], 'delivery_roots': [], 'address_matches': True, 'submit_controls': 1}
        self.browser.review_checkout = lambda *a, **kw: deepcopy(review)
        native.review_checkout = self.browser.review_checkout
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        clock = [self.app._now()]
        expiry = datetime.fromisoformat(self.store.read()['pending_checkout']['expires_at'])
        def account_read(address):
            clock[0] = expiry
            return 123
        native._account_reference = account_read
        native._eval = mock.Mock(return_value={'clicked': True})
        self.browser.submit_checkout = lambda cart, review, before_click, **kw: native._submit_checkout(cart, review, before_click)
        with mock.patch.object(self.app, '_now', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(HouseholdError, 'confirmation expired before the final click'):
                self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        native._eval.assert_not_called()
        self.assertIsNone(self.store.read()['pending_checkout'])

    def test_unverified_browser_receipt_keeps_attempt_uncertain(self):
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.shop.orders.append(self.order())
        self.shop.orders[0].pop('deliveryAddress')
        with self.store.locked() as state:
            state['pending_checkout']['status'] = 'uncertain'
        self.browser.receipt_address_matches = lambda *a, **kw: False
        result = self.app.handle({'operation': 'checkout', 'action': 'reconcile', 'confirmation_id': prepared['confirmation_id']})
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['retry_allowed'])
        self.assertEqual(self.store.read()['pending_checkout']['status'], 'uncertain')
        self.browser.receipt_address_matches = lambda *a, **kw: True
        result = self.app.handle({'operation': 'checkout', 'action': 'reconcile', 'confirmation_id': prepared['confirmation_id']})
        self.assertTrue(result['confirmed'])
        self.assertEqual(self.browser.checkout_clicks, 0)

    @unittest.skipUnless(shutil.which('node'), 'Node executes receipt DOM verification')
    def test_receipt_address_is_scoped_to_the_exact_visible_order(self):
        harness = r"""
const {script,change}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.location={href:change==='page'?'https://www.mathem.se/se/account/orders/another/':'https://www.mathem.se/se/account/orders/ORDER-123/'};
global.getComputedStyle=e=>({display:e.hidden?'none':'block',visibility:'visible'});
const node=(text)=>({innerText:text,children:[],getBoundingClientRect:()=>({width:10,height:10}),closest:()=>null});
const address=node('ADDRESS Exempelvägen 1');if(change==='hidden')address.hidden=true;if(change==='product')address.closest=()=>({});
const total=node('Totalt inkl. moms');
const main=node((change==='id'?'ORDER-1234':'ORDER-123')+' Totalt inkl. moms');
main.querySelectorAll=s=>s==='p'?(change==='duplicate'?[address,address]:[address]):(change==='total'?[]:[total]);
if(change==='dialog')main.closest=()=>({});
global.document={querySelector:()=>change==='login'?{}:null,querySelectorAll:()=>[main]};
process.stdout.write(eval(script));
"""
        script = _mathem_receipt_address_script('ORDER-123', 'ADDRESS Exempelvägen 1')
        for change in (None, 'page', 'hidden', 'product', 'id', 'duplicate', 'total', 'dialog', 'login'):
            result = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps({'script': script, 'change': change}), capture_output=True, text=True, check=True, timeout=10)
            self.assertEqual(json.loads(result.stdout), {'address_verified': change is None})

    def test_swedish_receipt_requires_currency_address_and_exact_window(self):
        from service_common import order_matches_checkout
        summary = cart_summary(self.cart)
        self.assertTrue(order_matches_checkout(self.order(), summary, provider='mathem'))
        for change in ({'currency': 'NOK'}, {'deliveryAddress': None}, {'deliveryAddress': 'Annan väg 2'},
                       {'deliverySlotDisplay': 'Lör 12. sep 10:00 - 12:00'}, {'grossAmount': 122.95}):
            self.assertFalse(order_matches_checkout({**self.order(), **change}, summary, provider='mathem'))

    @unittest.skipUnless(shutil.which('node'), 'Node executes the actual final browser script')
    def test_addition_final_turn_binds_original_added_combined_and_exact_destination(self):
        from oda_browser import _retail_addition_amount_script
        from core import CheckoutPreconditionError
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._checkout_deadline = None
        binding = {'receipt_address': 'Exempelvägen 1', 'account_reference_digest': 'a' * 64}
        browser._invoke = mock.Mock(return_value={'tabs': []})
        order = self.order()
        order['grossAmount'] = 564.01
        order['products'][0]['quantity'] = 17
        cart = {**self.cart, 'totalGrossAmount': 18.5, 'deliverySlot': None}
        expected = browser._addition_expectation(cart, '123456', order, binding)
        harness = r"""
const {script,change}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class E {
 constructor(tag,text='',children=[]){this.tag=tag;this.tagName=tag.toUpperCase();this.text=text;this.children=children;for(const child of children)child.parentElement=this;}
 get innerText(){return this.text||this.children.map(e=>e.innerText).join('\n');}
 get textContent(){return this.innerText;}
 getBoundingClientRect(){return {width:100,height:20};}
 getAttribute(){return null;}
 contains(node){return this===node||this.children.some(e=>e.contains(node));}
 matches(s){return s==='*'||s===this.tag||(s===`input[type="${this.type}"]`&&this.tag==='input');}
 querySelectorAll(selector){return this.children.flatMap(e=>[...(selector.split(',').some(s=>e.matches(s))?[e]:[]),...e.querySelectorAll(selector)]);}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 closest(selector){return selector.split(',').some(s=>this.matches(s))?this:this.parentElement?.closest(selector)||null;}
 click(){this.clicks=(this.clicks||0)+1;}
}
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
global.location={href:'https://www.mathem.se/se/checkout/confirm/'+(change==='new_order'?'':'?orderNumber='+(change==='order'?'999999':'123456'))};
const quantity=new E('input');quantity.type='number';quantity.value=change==='quantity'?2:1;quantity.labels=[new E('label','Antal')];
const item=new E('article','',[new E('p','Pasta Fusilli'),new E('p','500 g, Barilla'),new E('label','Antal'),quantity]);
const delivery=new E('section','',[new E('h2','Vi levererar din beställning'),new E('p',change==='delivery'?'Lör 12. sep 10:00 - 12:00':'Lör 12. sep 09:00 - 12:00'),new E('p','Exempelvägen 1')]);
const radio=new E('input');radio.type='radio';radio.checked=true;
const label=new E('label','',[new E('span',change==='card'?'•••• 5678':'•••• 1234'),radio]);radio.labels=[label];
const rows=[['Ursprunglig beställning','17 varor',change==='original'?'564,02 kr':'564,01 kr'],['Varor tillagda i efterhand',change==='count'?'2 varor':'1 vara','18,50 kr'],['Att betala nu',change==='payable'?'582,51 kr':'18,50 kr'],['Totalsumma för beställning','18 varor',change==='combined'?'582,52 kr':'582,51 kr']];
if(change==='duplicate')rows.push(['Att betala nu','18,50 kr']);
if(change==='missing')rows.pop();
if(change==='currency')rows[0][2]='564,01 NOK';
const summary=new E('section','',rows.map(parts=>new E('div','',parts.map(text=>new E('span',text)))));
const pay=new E('button','Bekräfta och betala '+(change==='button'?'582,51':'18,50')+' kr');pay.disabled=change==='disabled';
global.document=new E('document','',[new E('body','',[item,delivery,label,summary,pay])]);document.body=document.children[0];
const result=JSON.parse(eval(script));process.stdout.write(JSON.stringify({result,clicks:pay.clicks||0}));
"""
        def evaluate(script, change=None, provider='mathem'):
            localized = harness
            if provider == 'oda':
                for before, after in [('https://www.mathem.se/se/', 'https://oda.com/no/'),
                        ('Antal', 'Antall'), ('Vi levererar din beställning', 'Vi leverer varene dine'),
                        ('Ursprunglig beställning', 'Opprinnelig bestilling'),
                        ('Varor tillagda i efterhand', 'Nye varer lagt til'), ('Att betala nu', 'Å betale'),
                        ('Totalsumma för beställning', 'Ny totalsum'), ('Bekräfta och betala', 'Bekreft og betal'),
                        ('varor', 'varer'), ('1 vara', '1 vare'), ('564,01 NOK', '564,01 SEK')]:
                    localized = localized.replace(before, after)
            value = subprocess.run([shutil.which('node'), '-e', localized], input=json.dumps({'script': script, 'change': change}),
                                   capture_output=True, text=True, check=True, timeout=10)
            return json.loads(value.stdout)
        read = evaluate(_retail_addition_amount_script(expected))
        self.assertEqual(read['clicks'], 0)
        self.assertTrue(read['result']['amounts_valid'])
        self.assertEqual(read['result']['order_amounts']['payable_minor'], 1850)
        self.assertEqual(read['result']['order_amounts']['combined_minor'], 58251)
        review = browser._checked_surface(expected, evaluate(browser._checkout_surface_script(expected))['result'])
        review.update(binding=binding, order_amounts=read['result']['order_amounts'])
        browser.review_order_change = lambda *a, **kw: deepcopy(review)
        for change in (None, 'new_order', 'order', 'quantity', 'delivery', 'card', 'original', 'count', 'payable', 'combined', 'duplicate', 'missing', 'currency', 'button', 'disabled'):
            with self.subTest(change=change):
                observed = []
                def final_eval(script):
                    value = evaluate(script, change); observed.append(value); return value['result']
                browser._eval = final_eval
                if change is None:
                    browser.submit_order_change(cart, '123456', order, review)
                else:
                    with self.assertRaises(CheckoutPreconditionError): browser.submit_order_change(cart, '123456', order, review)
                self.assertEqual(observed[-1]['clicks'], 0 if change else 1)
        browser._invoke = mock.Mock(return_value={'tabs': [{'tabId': 'owned', 'active': True}]})
        def captured_eval(script):
            if 'const containers=' in script:
                return {'url': 'https://www.mathem.se/se/checkout/retry/?orderNumber=123456&orderChangeId=change-1', 'failed': True}
            return evaluate(script)['result']
        browser._eval = captured_eval
        captured = browser.submit_order_change(cart, '123456', order, review)
        self.assertEqual(captured, {'payment_failed': True, 'order_id': '123456', 'order_change_id': 'change-1'})
        browser._invoke = mock.Mock(return_value={'tabs': []})
        def expired(): raise HouseholdError('expired before dispatch')
        browser._eval = mock.Mock()
        with self.assertRaises(CheckoutPreconditionError): browser.submit_order_change(cart, '123456', order, review, expired)
        browser._eval.assert_not_called()
        browser._eval = mock.Mock(side_effect=HouseholdError('lost browser reply after click'))
        with self.assertRaises(HouseholdError) as failure: browser.submit_order_change(cart, '123456', order, review)
        self.assertNotIsInstance(failure.exception, CheckoutPreconditionError)

        # The observed Norwegian addition uses the same four-row arithmetic,
        # and the real Oda submit method must check it in its final browser turn.
        from oda_browser import OdaBrowser, _oda_checkout_surface_script, ODA_CHECKOUT_AMOUNT_KEYS
        oda = OdaBrowser.__new__(OdaBrowser); oda._checkout_deadline = None
        oda._invoke = mock.Mock(return_value={})
        order['currency'] = 'NOK'
        expected = oda._addition_expectation(cart, '123456', order, binding)
        read = evaluate(_retail_addition_amount_script(expected, provider='oda'), provider='oda')
        self.assertTrue(read['result']['amounts_valid'])
        surface = evaluate(_oda_checkout_surface_script(expected), provider='oda')['result']
        self.assertTrue(surface['total_matches'])
        self.assertEqual(len(surface['items']), 1)
        amounts = {key: None for key in ODA_CHECKOUT_AMOUNT_KEYS}
        amounts.update(product_subtotal=18.5, provider_total=18.5)
        review = {'binding': binding, 'surface': surface, 'amounts': amounts,
                  'order_amounts': read['result']['order_amounts']}
        oda.review_order_change = lambda *a, **kw: deepcopy(review)
        for change in (None, 'new_order', 'order', 'quantity', 'delivery', 'card', 'original', 'count',
                       'payable', 'combined', 'duplicate', 'missing', 'currency', 'button', 'disabled'):
            with self.subTest(provider='oda', change=change):
                observed = []
                def final_eval(script):
                    value = evaluate(script, change, provider='oda'); observed.append(value); return value['result']
                oda._eval = final_eval
                if change is None:
                    oda.submit_order_change(cart, '123456', order, review)
                else:
                    with self.assertRaises(CheckoutPreconditionError): oda.submit_order_change(cart, '123456', order, review)
                self.assertEqual(observed[-1]['clicks'], 0 if change else 1)

    @unittest.skipUnless(shutil.which('node'), 'Node executes the actual final browser script')
    def test_free_delivery_final_turn_binds_original_goods_total_card_and_destination(self):
        from oda_browser import _retail_addition_amount_script
        from core import CheckoutPreconditionError
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._checkout_deadline = None
        binding = {'receipt_address': 'Exempelvägen 1', 'account_reference_digest': 'a' * 64}
        browser._invoke = mock.Mock()
        order = self.order()
        order['grossAmount'] = 582.51
        order['products'][0]['quantity'] = 18
        delivery = {'slot_id': 77, 'display': 'Lör 12. sep 09:00 - 12:00',
                    'slot': normalize_retail_delivery_slots({**SLOTS, 'slots': [{**SLOTS['slots'][0], 'price': '0,00 kr'}]}, provider='mathem')['slots'][0]}
        expected = browser._delivery_change_expectation('123456', order, delivery, binding)
        harness = r"""
const {script,change,pricing,provider}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class E {
 constructor(tag,text='',children=[]){this.tag=tag;this.tagName=tag.toUpperCase();this.text=text;this.children=children;for(const child of children)child.parentElement=this;}
 get innerText(){return this.text||this.children.map(e=>e.innerText).join('\n');}
 get textContent(){return this.innerText;}
 getBoundingClientRect(){return {width:100,height:20};}
 getAttribute(){return null;}
 contains(node){return this===node||this.children.some(e=>e.contains(node));}
 matches(s){return s==='*'||s===this.tag||(s===`input[type="${this.type}"]`&&this.tag==='input');}
 querySelectorAll(selector){return this.children.flatMap(e=>[...(selector.split(',').some(s=>e.matches(s))?[e]:[]),...e.querySelectorAll(selector)]);}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 closest(selector){return selector.split(',').some(s=>this.matches(s))?this:this.parentElement?.closest(selector)||null;}
 click(){this.clicks=(this.clicks||0)+1;}
}
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
global.location={href:'https://www.mathem.se/se/checkout/confirm/'+(change==='new_order'?'':'?orderNumber='+(change==='order'?'999999':'123456'))};
const quantity=new E('input');quantity.type='number';quantity.value=change==='quantity'?2:1;quantity.labels=[new E('label','Antal')];
const item=new E('article','',[new E('p','Pasta Fusilli'),new E('p','500 g, Barilla'),quantity]);
const delivery=new E('section','',[new E('h2','Vi levererar din beställning'),new E('p',change==='delivery'?'Lör 12. sep 10:00 - 12:00':'Lör 12. sep 09:00 - 12:00'),new E('p','Exempelvägen 1')]);
const radio=new E('input');radio.type='radio';radio.checked=true;
const label=new E('label','',[new E('span',change==='card'?'•••• 5678':'•••• 1234'),radio]);radio.labels=[label];
const amount=n=>(n/100).toFixed(2).replace('.',',');
const rows=[['Ursprunglig beställning',change==='count'?'19 varor':'18 varor',change==='original'?'582,52 kr':'582,51 kr'],['Att betala nu',change==='payable'?'582,51 kr':amount(pricing.payable)+' kr'],['Totalsumma för beställning','18 varor',change==='combined'?amount(pricing.final+1)+' kr':amount(pricing.final)+' kr']];
if(change==='added')rows.push(['Varor tillagda i efterhand','1 vara','18,50 kr']);
if(change==='duplicate')rows.push(['Att betala nu','0,00 kr']);
if(change==='missing')rows.pop();
if(change==='currency')rows[0][2]='582,51 NOK';
const summary=new E('section','',rows.map(parts=>new E('div','',parts.map(text=>new E('span',text)))));
const pay=new E('button','Bekräfta och betala '+(change==='button'?'582,51':amount(pricing.payable))+' kr');pay.disabled=change==='disabled';
global.document=new E('document','',[new E('body','',[...(change==='quantity'?[item]:[]),delivery,label,summary,pay])]);document.body=document.children[0];
if(provider==='oda'){
 location.href=location.href.replace('www.mathem.se/se','oda.com/no');
 const labels=[['Ursprunglig beställning','Opprinnelig bestilling'],['Att betala nu','Å betale'],['Totalsumma för beställning','Ny totalsum'],['Varor tillagda i efterhand','Nye varer lagt til'],['Bekräfta och betala','Bekreft og betal'],['varor','varer'],['vara','vare']];
 for(const e of document.querySelectorAll('*'))for(const [a,b] of labels)e.text=e.text.replaceAll(a,b);
}
const result=JSON.parse(eval(script));process.stdout.write(JSON.stringify({result,clicks:pay.clicks||0}));
"""
        def evaluate(script, change=None, pricing=None, provider="mathem"):
            value = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps({'script': script, 'change': change, 'pricing': pricing or {'final': 58251, 'payable': 0}, 'provider': provider}),
                                   capture_output=True, text=True, check=True, timeout=10)
            return json.loads(value.stdout)
        read = evaluate(_retail_addition_amount_script(expected))
        self.assertEqual(read['clicks'], 0)
        self.assertTrue(read['result']['amounts_valid'])
        self.assertEqual(read['result']['order_amounts']['payable_minor'], 0)
        self.assertEqual(read['result']['order_amounts']['combined_minor'], 58251)
        review = browser._checked_surface(expected, evaluate(browser._checkout_surface_script(expected))['result'])
        review.update(binding=binding, order_amounts=read['result']['order_amounts'])
        browser._read_order_binding = mock.Mock(return_value=deepcopy(binding))
        browser._open = mock.Mock()
        browser._read_delivery_change_review = lambda *a, **kw: deepcopy(review)
        for change in (None, 'new_order', 'order', 'added', 'quantity', 'delivery', 'card', 'original', 'count', 'payable', 'combined', 'duplicate', 'missing', 'currency', 'button', 'disabled'):
            with self.subTest(change=change):
                observed = []
                def final_eval(script):
                    value = evaluate(script, change); observed.append(value); return value['result']
                browser._eval = final_eval
                if change is None:
                    browser.submit_delivery_change('123456', order, delivery, review)
                else:
                    with self.assertRaises(CheckoutPreconditionError): browser.submit_delivery_change('123456', order, delivery, review)
                self.assertEqual(observed[-1]['clicks'], 0 if change else 1)
        def expired(): raise HouseholdError('expired before dispatch')
        browser._eval = mock.Mock()
        with self.assertRaises(CheckoutPreconditionError): browser.submit_delivery_change('123456', order, delivery, review, expired)
        browser._eval.assert_not_called()
        browser._eval = mock.Mock(side_effect=HouseholdError('lost browser reply after click'))
        with self.assertRaises(HouseholdError) as failure: browser.submit_delivery_change('123456', order, delivery, review)
        self.assertNotIsInstance(failure.exception, CheckoutPreconditionError)

        # Same, decreased and increased full totals use the same actual DOM
        # reader/final script for both retailers. Payment is a separate value.
        for provider in ('oda', 'mathem'):
            for final, payable in ((58251, 0), (57000, 0), (60000, 1749), (58251, 500)):
                with self.subTest(provider=provider, final=final, payable=payable):
                    pricing = {'final': final, 'payable': payable}
                    expectation = deepcopy(expected)
                    expectation['checkout_url'] = ('https://oda.com/no/' if provider == 'oda' else 'https://www.mathem.se/se/') + 'checkout/confirm/?orderNumber=123456'
                    observed = evaluate(_retail_addition_amount_script(expectation, provider=provider), pricing=pricing, provider=provider)
                    self.assertTrue(observed['result']['amounts_valid'])
                    values = observed['result']['order_amounts']
                    self.assertEqual((values['original_minor'], values['combined_minor'], values['payable_minor']), (58251, final, payable))
                    expectation['order_amounts'] = values
                    for drift in (None, 'combined', 'original', 'missing', 'added', 'button', 'duplicate'):
                        confirmed = evaluate(_retail_addition_amount_script(expectation, submit=True, provider=provider), drift, pricing, provider)
                        self.assertEqual(confirmed['clicks'], 1 if drift is None else 0)
                    unbound = {k: v for k, v in expectation.items() if k != 'order_amounts'}
                    self.assertEqual(evaluate(_retail_addition_amount_script(unbound, submit=True, provider=provider), pricing=pricing, provider=provider)['clicks'], 0)

    @unittest.skipUnless(shutil.which('node'), 'Node executes the observed collapsed summary')
    def test_zero_payable_collapsed_review_still_expands_original_order_amount(self):
        browser = MathemBrowser.__new__(MathemBrowser)
        expected = {'checkout_url': browser.checkout_url + '?orderNumber=123456',
                    'order_id': '123456', 'delivery_change': True, 'lines': []}
        script = browser._checkout_expand_script(expected, set())
        after = browser._checkout_expand_script(expected, {'Visa sammanfattning'})
        harness = r"""
const {script,after}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.location={href:'https://www.mathem.se/se/checkout/confirm/?orderNumber=123456'};
global.getComputedStyle=e=>({display:e.hidden?'none':'block',visibility:'visible'});
const node=t=>({innerText:t,getBoundingClientRect:()=>({width:10,height:10}),getAttribute:()=>null});
const original=node('Ursprunglig beställning');original.hidden=true;
const expand=node('Visa sammanfattning');let clicks=0;
expand.click=()=>{clicks++;original.hidden=false;expand.innerText='Dölj sammanfattning';};
const labels=[original,node('Att betala nu'),node('Totalsumma för beställning')];
global.document={querySelectorAll:s=>s==='*'?labels:s==='button'?[expand]:[]};
const first=JSON.parse(eval(script)),second=JSON.parse(eval(after));
process.stdout.write(JSON.stringify({first,second,clicks}));
"""
        result = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps({'script': script, 'after': after}),
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), {'first': {'ready': False, 'clicked': ['Visa sammanfattning']},
                                                    'second': {'ready': True}, 'clicks': 1})

    def test_checkout_destination_never_uses_default_existing_order_for_new_purchase(self):
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._settle = mock.Mock()
        browser._invoke = mock.Mock()
        browser._eval = mock.Mock(side_effect=[
            {'text': 'Skapa en ny beställning Du väljer leveranstid i nästa steg.', 'checked': False, 'selected_count': 1},
            {'text': 'Skapa en ny beställning Du väljer leveranstid i nästa steg.', 'checked': True, 'selected_count': 1}])
        browser._choose_checkout_destination(None)
        self.assertEqual(browser._invoke.call_args_list, [
            mock.call('click', '[data-mathem-destination="true"]'), mock.call('click', '[data-mathem-destination-next="true"]')])
        expected = {'order_id': '123456', 'delivery_text': 'Lör 12. sep 09:00 - 12:00', 'delivery_address': 'Exempelvägen 1'}
        for label in ('Lägg till i din nuvarande beställning 999999 Lör 12. sep 09:00 - 12:00 Exempelvägen 1',
                      'Lägg till i din nuvarande beställning 123456 Lör 12. sep 10:00 - 12:00 Exempelvägen 1',
                      'Lägg till i din nuvarande beställning 123456 Lör 12. sep 09:00 - 12:00 Annan adress 2'):
            browser._invoke.reset_mock()
            browser._eval.return_value = {'text': label, 'checked': True, 'selected_count': 1}
            browser._eval.side_effect = None
            with self.assertRaises(HouseholdError): browser._choose_checkout_destination(expected)
            browser._invoke.assert_not_called()

    def test_final_browser_turn_rejects_card_item_delivery_or_amount_drift(self):
        import hashlib
        from core import CheckoutPreconditionError
        browser = MathemBrowser.__new__(MathemBrowser)
        browser._checkout_dispatch_tab = lambda: None
        browser._checkout_deadline = None
        browser._account_reference = lambda address: 123
        expected = browser._cart_expectation(self.cart)
        harness = r"""
const {script,change}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class E {
 constructor(tag,text='',children=[]){this.tag=tag;this.tagName=tag.toUpperCase();this.text=text;this.children=children;for(const child of children)child.parentElement=this;}
 get innerText(){return this.text||this.children.map(e=>e.innerText).join('\n');}
 get textContent(){return this.innerText;}
 getBoundingClientRect(){return {width:100,height:20};}
 getAttribute(){return null;}
 contains(node){return this===node||this.children.some(e=>e.contains(node));}
 matches(s){return s==='*'||s===this.tag||(s===`input[type="${this.type}"]`&&this.tag==='input');}
 querySelectorAll(selector){return this.children.flatMap(e=>[...(selector.split(',').some(s=>e.matches(s))?[e]:[]),...e.querySelectorAll(selector)]);}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 closest(selector){return selector.split(',').some(s=>this.matches(s))?this:this.parentElement?.closest(selector)||null;}
 click(){this.clicks=(this.clicks||0)+1;}
}
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
global.location={href:'https://www.mathem.se/se/checkout/confirm/'};
const quantity=new E('input');quantity.type='number';quantity.value=change==='quantity'?2:1;quantity.labels=[new E('label','Antal')];
const item=new E('article','',[new E('p','Pasta Fusilli'),new E('p','500 g, Barilla'),quantity]);
const delivery=new E('section','',[new E('h2','Vi levererar din beställning'),new E('p',change==='delivery'?'Lör 12. sep 10:00 - 12:00':'Lör 12. sep 09:00 - 12:00'),new E('p',change==='spoof'?'Annan väg 2':'Exempelvägen 1')]);
const radio=new E('input');radio.type='radio';radio.checked=true;
const label=new E('label','',[new E('span',change==='card'?'•••• 5678':'•••• 1234'),radio]);radio.labels=[label];
const rows=[['1 vara','15,95 kr'],['Delsumma','15,95 kr'],['Avgift för liten varukorg',change==='amount'?'100,00 kr':'99,00 kr'],['Lådor','7,00 kr'],['Leverans','59,00 kr'],['Gratis leverans','−59,00 kr'],['Totalt inkl. moms','121,95 kr']];
const summary=new E('section','',rows.map(([a,b])=>new E('div','',[new E('span',a),new E('span',b)])));
const pay=new E('button','Bekräfta och betala 121,95 kr');
global.document=new E('document','',[new E('body','',[item,delivery,new E('p',change==='spoof'?'Exempelvägen 1':''),label,summary,pay])]);document.body=document.children[0];
const result=JSON.parse(eval(script));process.stdout.write(JSON.stringify({result,clicks:pay.clicks||0}));
"""
        def evaluate(script, change=None):
            value = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps({'script': script, 'change': change}),
                                   capture_output=True, text=True, check=True, timeout=10)
            return json.loads(value.stdout)
        self.assertFalse(evaluate(browser._checkout_surface_script(expected),'spoof')['result']['address_matches'])
        surface = evaluate(browser._checkout_surface_script(expected))['result']
        review = browser._checked_surface(expected, surface)
        review['account_reference_digest'] = hashlib.sha256(b'123').hexdigest()
        review['amounts'] = self.amounts
        review['discount_breakdown'] = self.discount_breakdown
        review = json.loads(json.dumps(review, sort_keys=True))
        browser.review_checkout = lambda cart: deepcopy(review)
        for change in (None, 'card', 'quantity', 'delivery', 'amount', 'spoof'):
            observed = []
            def final_eval(script):
                value = evaluate(script, change)
                observed.append(value)
                return value['result']
            browser._eval = final_eval
            journal = []
            if change is None:
                browser._submit_checkout(self.cart, review, lambda: journal.append('clicking'))
            else:
                with self.assertRaises(CheckoutPreconditionError):
                    browser._submit_checkout(self.cart, review, lambda: journal.append('clicking'))
            self.assertEqual(journal, ['clicking'])
            self.assertEqual(observed[0]['clicks'], 0 if change else 1)



if __name__ == '__main__':
    unittest.main()
