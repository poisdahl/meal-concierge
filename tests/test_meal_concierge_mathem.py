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
            {"name": "unverified_product_discount", "rows": self.ROWS + [["Du sparar", "−1,00 kr"]], "valid": False},
        ])
        read_script = _oda_checkout_amount_script(12195, expected_product_count=1, provider="mathem")
        amounts = {"product_subtotal": 1595, "delivery_price": 5900, "discounts": -5900,
                   "deposits": None, "bags": 700,
                   "other_fees": {"Avgift för liten varukorg": 9900}, "provider_total": 12195}
        click_script = _oda_checkout_amount_script(12195, expected_product_count=1, provider="mathem",
            expected_amounts=amounts, expected_url="https://www.mathem.se/se/checkout/confirm/")
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
 const button=new Element('button','Bekräfta och betala 121,95 kr');
 global.document=new Element('document','',[summary,button]);
 const read=JSON.parse(eval(payload.read));
 const submit=JSON.parse(eval(payload.click));
 output.push({name:test.name,valid:read.amounts_valid,clicked:submit.clicked,clicks:button.clicks||0,amounts:read.amounts});
}
process.stdout.write(JSON.stringify(output));
"""
        completed = subprocess.run([shutil.which("node"), "-e", harness],
            input=json.dumps({"cases": cases, "read": read_script, "click": click_script}),
            capture_output=True, text=True, timeout=10, check=True)
        observed = json.loads(completed.stdout)
        self.assertEqual(len(observed), len(cases))
        for expected, actual in zip(cases, observed):
            with self.subTest(case=expected["name"]):
                self.assertEqual(actual["valid"], expected["valid"])
                self.assertEqual(actual["clicked"], expected["valid"])
                self.assertEqual(actual["clicks"], int(expected["valid"]))
        self.assertEqual(observed[0]["amounts"], amounts)

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
        for operation, actions in [('checkout', ['confirm', 'submit', 'reconcile']), ('orders', ['change_begin', 'change_abort', 'cancel_prepare', 'cancel_submit', 'cancel_confirm', 'cancel_reconcile'])]:
            for action in actions:
                with self.subTest(operation=operation, action=action), self.assertRaisesRegex(HouseholdError, 'Mathem'):
                    self.app.handle({'operation': operation, 'action': action, 'order_id': '1', 'idempotency_key': 'test'})
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
        self.browser.oda = self.shop
        self.browser.receipt_address_matches = lambda order_id, address, **kw: order_id == "123456" and address == "Exempelvägen 1"
        self.browser.review_checkout = lambda cart, **kw: {'payment_display': '•••• 1234', 'amounts': deepcopy(self.amounts)}
        self.app = Application(self.store, self.shop, self.browser)
        self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})

    def order(self):
        return {'orderNumber': '123456', 'currency': 'SEK', 'grossAmount': 121.95,
                'deliveryDate': '2026-09-12', 'deliverySlotDisplay': 'Lör 12. sep 09:00 - 12:00',
                'deliveryAddress': 'Exempelvägen 1', 'products': deepcopy(self.cart['items'])}

    def test_prepare_uses_final_amounts_and_masked_payment(self):
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertNotIn('manual_checkout_required', prepared)
        self.assertEqual(prepared['summary']['total'], 121.95)
        self.assertEqual(prepared['summary']['payment'], '•••• 1234')
        self.assertEqual(prepared['summary']['amounts'], self.amounts)
        self.assertEqual(self.store.read()['pending_checkout']['status'], 'awaiting_confirmation')
        self.assertEqual(self.browser.checkout_clicks, 0)

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
        result = self.app.handle({'operation': 'checkout', 'action': 'reconcile', 'confirmation_id': prepared['confirmation_id']})
        self.assertTrue(result['confirmed'])
        again = self.app.handle({'operation': 'checkout', 'action': 'confirm', 'confirmation_id': prepared['confirmation_id']})
        self.assertTrue(again['confirmed'])
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

    def test_slow_account_read_cannot_outlive_checkout_confirmation(self):
        import hashlib
        native = MathemBrowser.__new__(MathemBrowser)
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
    def test_final_browser_turn_rejects_card_item_delivery_or_amount_drift(self):
        import hashlib
        from core import CheckoutPreconditionError
        browser = MathemBrowser.__new__(MathemBrowser)
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
const delivery=new E('section','',[new E('h2','Vi levererar din beställning'),new E('p',change==='delivery'?'Lör 12. sep 10:00 - 12:00':'Lör 12. sep 09:00 - 12:00'),new E('p','Exempelvägen 1')]);
const radio=new E('input');radio.type='radio';radio.checked=true;
const label=new E('label','',[new E('span',change==='card'?'•••• 5678':'•••• 1234'),radio]);radio.labels=[label];
const rows=[['1 vara','15,95 kr'],['Delsumma','15,95 kr'],['Avgift för liten varukorg',change==='amount'?'100,00 kr':'99,00 kr'],['Lådor','7,00 kr'],['Leverans','59,00 kr'],['Gratis leverans','−59,00 kr'],['Totalt inkl. moms','121,95 kr']];
const summary=new E('section','',rows.map(([a,b])=>new E('div','',[new E('span',a),new E('span',b)])));
const pay=new E('button','Bekräfta och betala 121,95 kr');
global.document=new E('document','',[new E('body','',[item,delivery,label,summary,pay])]);document.body=document.children[0];
const result=JSON.parse(eval(script));process.stdout.write(JSON.stringify({result,clicks:pay.clicks||0}));
"""
        def evaluate(script, change=None):
            value = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps({'script': script, 'change': change}),
                                   capture_output=True, text=True, check=True, timeout=10)
            return json.loads(value.stdout)
        surface = evaluate(browser._checkout_surface_script(expected))['result']
        review = browser._checked_surface(expected, surface)
        review['account_reference_digest'] = hashlib.sha256(b'123').hexdigest()
        review['amounts'] = self.amounts
        browser.review_checkout = lambda cart: deepcopy(review)
        for change in (None, 'card', 'quantity', 'delivery', 'amount'):
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
