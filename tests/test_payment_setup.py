"""Payment preferences through the ordinary first-run Application interface."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore
from service import Application


class UnconnectedProvider:
    def probe(self, **kwargs):
        raise HouseholdError("not connected")

    def call(self, *args, **kwargs):
        raise AssertionError("setup must not enter the store")


class PaymentSetupTests(unittest.TestCase):
    def app(self, root, provider="oda", **config):
        return Application(StateStore(root, {"household": "Synthetic", "provider": provider, **config}), UnconnectedProvider(), None)

    def test_one_setup_question_persists_payment_without_store_access(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = self.app(root)
            before = app.store.read()
            shown = app.handle({"operation": "setup", "action": "show"})
            self.assertEqual(shown["current"]["payment_choices"], ["saved_card", "vipps"])
            self.assertEqual(shown["current"]["checkout_payment"], {"method": "saved_card", "card_last4": None})
            applied = app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                                  "changes": {"checkout_payment": {"method": "vipps"}}})
            self.assertEqual(applied["current"]["checkout_payment"], {"method": "vipps", "card_last4": None})
            reopened = self.app(root)
            self.assertEqual(reopened.store.read()["checkout_payment"], applied["current"]["checkout_payment"])
            self.assertIsNone(reopened.handle({"operation": "setup", "action": "show"})["question"])
            self.assertTrue(reopened.handle({"operation": "setup", "action": "apply", "keep_current": True})["idempotent"])
            after = reopened.store.read()
            for key in before.keys() - {"setup", "checkout_payment"}:
                self.assertEqual(after[key], before[key], key)
            reopened.handle({"operation": "setup", "action": "apply", "keep_current": False,
                             "changes": {"checkout_payment": {"method": "saved_card", "card_last4": "0012"}}})
            self.assertEqual(self.app(root).store.read()["checkout_payment"]["card_last4"], "0012")

    def test_provider_defaults_and_unsupported_choices_are_atomic(self):
        for provider, default in [("oda", "saved_card"), ("mathem", "saved_card"), ("meny", "vipps")]:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                app = self.app(Path(temp), provider)
                self.assertEqual(app.store.read()["checkout_payment"]["method"], default)
                bad = [{"method": "bank_transfer"}, {"method": "saved_card", "card_last4": 1234},
                       {"method": "saved_card", "card_last4": "4111111111111111"},
                       {"method": "vipps", "card_last4": "1234"}, {"method": "vipps", "phone": "12345678"}]
                if provider != "oda":
                    bad.append({"method": "saved_card" if provider == "meny" else "vipps"})
                for payment in bad:
                    before = app.store.read()
                    with self.assertRaises(HouseholdError):
                        app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                                    "changes": {"people": 3, "checkout_payment": payment}})
                    self.assertEqual(app.store.read(), before)

    def test_additive_legacy_default_preserves_existing_operations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = self.app(root)
            path = root / "state.json"
            legacy = json.loads(path.read_text())
            legacy.pop("checkout_payment")
            legacy["pending_checkout"] = {"status": "uncertain", "confirmation_id": "original"}
            path.write_text(json.dumps(legacy))
            reopened = self.app(root).store.read()
            self.assertEqual(reopened.pop("checkout_payment"), {"method": "saved_card", "card_last4": None})
            self.assertEqual(reopened, legacy)
            before = app.store.read()
            with self.assertRaisesRegex(HouseholdError, "pending provider operation"):
                app.handle({"operation": "setup", "action": "apply", "keep_current": False,
                            "changes": {"checkout_payment": {"method": "vipps"}}})
            self.assertEqual(app.store.read(), before)

    def test_legacy_provider_fallback_uses_configured_store_on_every_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = self.app(root, "meny")
            legacy = app.store.read()
            legacy.pop("provider")
            legacy.pop("checkout_payment")
            (root / "state.json").write_text(json.dumps(legacy))
            for _ in range(2):
                reopened = self.app(root, "meny").store.read()
                self.assertEqual(reopened["provider"], "meny")
                self.assertEqual(reopened["checkout_payment"], {"method": "vipps", "card_last4": None})

    def test_initial_config_can_choose_vipps(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(Path(temp), checkout_payment={"method": "vipps"})
            self.assertEqual(app.handle({"operation": "setup", "action": "show"})["current"]["checkout_payment"],
                             {"method": "vipps", "card_last4": None})


# Synthetic rendering of the observed Oda confirm controls. The browser helper's
# actual JavaScript runs in Node; the only mutating controls count their clicks.
PAYMENT_DOM = r"""
const {script,c}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const clicks=[];
class E {
 constructor(tag,text='',children=[]){this.tag=tag;this.text=text;this.children=children;for(const x of children)x.parentElement=this;}
 get innerText(){return this.text||this.children.map(x=>x.innerText).join('\n');}
 getBoundingClientRect(){return {width:this.hidden?0:100,height:20};} getAttribute(){return null;}
 contains(n){return this===n||this.children.some(x=>x.contains(n));}
 matches(s){return s==='*'||s===this.tag||(s===`input[type="${this.type}"]`&&this.tag==='input');}
 querySelectorAll(s){return this.children.flatMap(x=>[...(s.split(',').some(y=>x.matches(y))?[x]:[]),...x.querySelectorAll(s)]);}
 querySelector(s){return this.querySelectorAll(s)[0]||null;}
 closest(s){return s.split(',').some(x=>this.matches(x))?this:this.parentElement?.closest(s)||null;}
 click(){clicks.push(this.id);if(this.type==='radio'&&!c.noEffect)radios.forEach(r=>r.checked=r===this);}
}
const radios=[],labels=[];
for(const [index,text] of (c.options||['Vipps','Nytt kort','•••• 1234']).entries()){
 const r=new E('input');r.type='radio';r.id=index;r.checked=(c.selected??0)===index;r.disabled=(c.disabled||[]).includes(index);r.hidden=(c.hidden||[]).includes(index);
 const label=new E('label','',[new E('span',text),r]);r.labels=[label];radios.push(r);labels.push(label);
}
const quantity=new E('input');quantity.type='number';quantity.value=c.quantity||1;
const item=new E('article','',[new E('p','Pasta'),new E('p','500 g, Sopps'),new E('label','Antall'),quantity]);
const delivery=new E('section','',[new E('h2','Vi leverer varene dine'),new E('p','12. september 09:00–12:00'),new E('p','Eksempelveien 1')]);
const rows=[['1 varer','26,50 kr'],['Delsum','26,50 kr'],['Levering',c.fee?'20,00 kr':'19,00 kr'],['Total inkl. MVA','45,50 kr']];
const summary=new E('section','',rows.map(parts=>new E('div','',parts.map(x=>new E('span',x)))));
const pay=new E('button',c.button||((c.selected??0)===0?'Betal med':'Bekreft og betal')+' 45,50 kr');pay.id='PAY';pay.disabled=!!c.payDisabled;
global.document=new E('document','',[new E('body','',[item,delivery,...labels,summary,pay])]);document.body=document.children[0];
global.getComputedStyle=e=>({display:e.hidden?'none':'block',visibility:'visible'});
global.location=new URL(c.url||'https://oda.com/no/checkout/confirm/');
process.stdout.write(JSON.stringify({result:JSON.parse(eval(script)),clicks,selected:radios.map(r=>r.checked)}));
"""


class PaymentBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node executes the observed payment control contract")

    def evaluate(self, script, **case):
        import subprocess
        completed = subprocess.run([self.node, "-e", PAYMENT_DOM], input=json.dumps({"script": script, "c": case}),
                                   text=True, capture_output=True, check=True, timeout=10)
        return json.loads(completed.stdout)

    def test_configured_selection_clicks_only_one_existing_radio(self):
        from oda_browser import _oda_checkout_payment_script, CHECKOUT_URL
        for payment, case, expected in [
            ({"method": "saved_card"}, {}, 2),
            ({"method": "vipps"}, {"selected": 2}, 0),
            ({"method": "saved_card", "card_last4": "5678"}, {"options": ["Vipps", "•••• 1234", "•••• 5678"]}, 2),
        ]:
            with self.subTest(payment=payment):
                selected = self.evaluate(_oda_checkout_payment_script(payment, select=True, expected_url=CHECKOUT_URL), **case)
                self.assertEqual(selected["clicks"], [expected])
                self.assertTrue(selected["result"]["selected"])
                reread = self.evaluate(_oda_checkout_payment_script(payment), **{**case, "selected": expected})
                self.assertTrue(reread["result"]["verified"])
                self.assertEqual(reread["clicks"], [])
        current = self.evaluate(_oda_checkout_payment_script({"method": "saved_card"}, select=True, expected_url=CHECKOUT_URL),
                                options=["Vipps", "•••• 1234", "•••• 5678"], selected=2)
        self.assertEqual(current["clicks"], [])
        self.assertEqual(current["result"]["payment_display"], "•••• 5678")

    def test_unavailable_ambiguous_or_wrong_page_never_selects_or_falls_back(self):
        from oda_browser import _oda_checkout_payment_script, CHECKOUT_URL
        cases = [({}, {"options": ["Vipps", "•••• 1234", "•••• 5678"]}),
                 ({"card_last4": "9999"}, {}),
                 ({"card_last4": "1234"}, {"options": ["Vipps", "•••• 1234", "•••• 1234"]}),
                 ({}, {"options": ["Vipps", "Nytt kort"]}),
                 ({}, {"options": ["Vipps", "Nytt kort •••• 1234"]}),
                 ({}, {"options": ["Vipps", "New card •••• 1234"], "selected": 1}),
                 ({}, {"hidden": [2]}), ({}, {"disabled": [2]}),
                 ({}, {"url": CHECKOUT_URL + "?orderNumber=123"}),
                 ({}, {"options": ["Vipps", "4111 1111 1111 1111"]}),
                 ({"method": "vipps"}, {"options": ["Vipps", "Vipps", "•••• 1234"]})]
        for payment, case in cases:
            with self.subTest(payment=payment, case=case):
                result = self.evaluate(_oda_checkout_payment_script({"method": "saved_card", **payment}, select=True, expected_url=CHECKOUT_URL), **case)
                self.assertFalse(result["result"]["verified"])
                self.assertEqual(result["clicks"], [])

    def test_navigator_selects_once_then_rechecks_and_submit_review_never_corrects(self):
        from oda_browser import OdaBrowser
        for no_effect in (False, True):
            browser = OdaBrowser.__new__(OdaBrowser)
            state = {"noEffect": no_effect}
            clicks = []
            def evaluate(script):
                result = self.evaluate(script, **state)
                clicks.extend(result["clicks"])
                state["selected"] = result["selected"].index(True)
                return result["result"]
            browser._eval = evaluate
            browser._settle = lambda _: None
            if no_effect:
                with self.assertRaisesRegex(HouseholdError, "navigation timed out"):
                    browser._advance_checkout_path(payment={"method": "saved_card"}, select_payment=True)
            else:
                browser._advance_checkout_path(payment={"method": "saved_card"}, select_payment=True)
            self.assertEqual(clicks, [2])
            state["selected"] = 0
            clicks.clear()
            with self.assertRaisesRegex(HouseholdError, "configured payment is not selected"):
                browser._advance_checkout_path(payment={"method": "saved_card"})
            self.assertEqual(clicks, [])

    def test_vipps_final_click_binds_method_card_and_amount_after_callback(self):
        from oda_browser import OdaBrowser, _oda_checkout_surface_script, CHECKOUT_URL
        from core import CheckoutPreconditionError
        expected = {"delivery_address": "Eksempelveien 1", "total_minor": 4550}
        amounts = {"product_subtotal": 26.5, "delivery_price": 19, "discounts": None, "deposits": None,
                   "bags": None, "other_fees": None, "provider_total": 45.5}
        for method, selected in [("vipps", 0), ("saved_card", 2)]:
            payment = {"method": method}
            script = _oda_checkout_surface_script(expected, payment)
            surface = self.evaluate(script, selected=selected)["result"]
            self.assertTrue(surface["masked_payment"] and surface["total_matches"])
            for change in [{}, {"selected": 2 if selected == 0 else 0}, {"fee": True},
                           {"quantity": 2}, {"payDisabled": True}, {"button": "Betal med 46,50 kr"},
                           *([{"options": ["Vipps", "Nytt kort", "•••• 5678"]}] if method == "saved_card" else [])]:
                with self.subTest(method=method, change=change):
                    browser = OdaBrowser.__new__(OdaBrowser)
                    browser._checkout_deadline = None
                    observed, callbacks = [], []
                    def evaluate(final):
                        self.assertEqual(callbacks, [True])
                        result = self.evaluate(final, **{"selected": selected, **change})
                        observed.append(result)
                        return result["result"]
                    browser._eval = evaluate
                    def submit():
                        browser._click_checkout_submit(4550, CHECKOUT_URL, lambda: callbacks.append(True),
                            expected_product_count=1, expected_amounts=amounts, review_surface=(script, surface))
                    if change:
                        with self.assertRaises(CheckoutPreconditionError):
                            submit()
                    else:
                        submit()
                    self.assertEqual(observed[0]["clicks"], [] if change else ["PAY"])


if __name__ == "__main__":
    unittest.main()
