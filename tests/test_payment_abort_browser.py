"""Execute the retained-card abort protocol against a synthetic issuer and CDP."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError
from oda_payment_abort import _CARD_ABORT_SCRIPT, _allowed_page, abort_card_payment


FIXTURE = r'''
import vm from 'node:vm';
const test=TEST;
let clicks=0,reads=0;
const parentUrl=test.parentUrl||'https://oda.com/no/checkout/threeDS/?paymentId=123';
const issuerUrl='https://acs2.edb.com/synthetic/DO_NOT_EMIT';
const page=new URL(test.parentUrl||parentUrl),issuerLocation=new URL(issuerUrl);
const fixtureRect=()=>({width:40,height:20});
const frame={getBoundingClientRect:fixtureRect};
const challenge={getBoundingClientRect:fixtureRect,querySelectorAll:()=>test.missingFrame?[]:[frame]};
const cancel={innerText:test.label||'Avbryt',href:test.wrongLink?'https://other.invalid/cancel':issuerUrl+'/cancel',
 getAttribute:()=>null,getBoundingClientRect:fixtureRect,
 click(){clicks++;if(test.redirect)page.href='https://oda.com/no/checkout/retry/?orderNumber=order-1';}};
const document={readyState:'complete',querySelectorAll:s=>s==='.adyen-checkout__threeds2__challenge'?[challenge]:s==='a[href]'?(test.duplicate?[cancel,cancel]:[cancel]):[]};
const fetch=async(url,options)=>{
 if(url!=='/api/v1/payments/adyen/three-ds/123/'||options.method!=='GET'||options.credentials!=='same-origin')throw Error('Unexpected payment request');
 const statuses=test.statuses||['pending','pending','closed'];
 const status=statuses[Math.min(reads++,statuses.length-1)];
 if(test.changeDuringRead)page.href='https://oda.com/no/checkout/threeDS/?paymentId=999';
 return {ok:!test.httpError,json:async()=>status==='pending'?{type:'payments-providers-adyen-three-ds',params:{payment_id:test.wrongPayment?'999':'123'}}:
 {type:status==='closed'?'checkout-payment-retry':status==='paid'?'checkout-payment-success':'unknown',params:{order_number:test.wrongOrder?'order-2':'order-1',order_change_id:test.wrongChange?7:null}}};
};
globalThis.WebSocket=class {
 constructor(){this.listeners={};queueMicrotask(()=>this.fire('open',{}))}
 addEventListener(k,f){(this.listeners[k]??=[]).push(f)}
 fire(k,v){for(const f of this.listeners[k]||[])f(v)}
 async send(raw){const m=JSON.parse(raw);let result={};
  try{
   if(m.method==='Target.getTargets')result={targetInfos:[{type:'page',url:parentUrl,targetId:'parent'},
     {type:'iframe',url:test.wrongIssuer?'https://other.invalid/':issuerUrl,targetId:test.wrongFrame?'other-frame':'frame'}]};
   if(m.method==='Target.attachToTarget')result={sessionId:m.params.targetId};
   if(m.method==='DOM.describeNode')result={node:{frameId:'frame'}};
   if(m.method==='Runtime.evaluate'){
    const value=await vm.runInNewContext(m.params.expression,{document,location:m.sessionId==='frame'?issuerLocation:page,URL,AbortSignal,fetch,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})});
    result={result:m.params.returnByValue?{value}:{objectId:value?'frame-object':undefined}};
   }
   queueMicrotask(()=>this.fire('message',{data:JSON.stringify({id:m.id,result})}));
  }catch{queueMicrotask(()=>this.fire('message',{data:JSON.stringify({id:m.id,error:{message:'Synthetic failure'}})}))}
 }
 close(){}
};
process.on('exit',()=>process.stderr.write(JSON.stringify({clicks,reads})));
'''


class CardAbortScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('Node is needed for the retained payment protocol')

    def run_protocol(self, **case):
        script = FIXTURE.replace('TEST', json.dumps(case)) + _CARD_ABORT_SCRIPT
        cfg = {'endpoint': 'ws://localhost/synthetic',
               'url': case.get('parentUrl', 'https://oda.com/no/checkout/threeDS/?paymentId=123'),
               'payment_id': '123', 'order_id': 'order-1', 'order_change_id': None,
               'prior_attempted': case.get('prior_attempted', False),
               'observe_only': case.get('observe_only', False), 'observe_ms': 1}
        result = subprocess.run([self.node, '--input-type=module', '-e', script],
                                input=json.dumps(cfg)+'\nabort_once\n', text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('DO_NOT_EMIT', result.stdout)
        return [json.loads(row) for row in result.stdout.splitlines()], json.loads(result.stderr)

    def test_native_terminal_on_unchanged_issuer_page_closes_after_one_abort(self):
        events, metrics = self.run_protocol()
        self.assertTrue(events[0]['ready'])
        self.assertEqual(events[-1]['status'], 'closed')
        self.assertEqual(events[-1]['payment_id'], '123')
        self.assertEqual(events[-1]['order_id'], 'order-1')
        self.assertEqual(metrics['clicks'], 1)

    def test_merchant_redirect_alone_does_not_establish_failure(self):
        events, metrics = self.run_protocol(statuses=['pending'], redirect=True)
        self.assertEqual(events[-1]['status'], 'unknown')
        self.assertEqual(metrics['clicks'], 1)

    def test_terminal_approval_races_never_become_abort_success(self):
        for statuses, clicks in ((['paid'], 0), (['pending','paid'], 0),
                                 (['pending','pending','paid'], 1)):
            with self.subTest(statuses=statuses):
                events, metrics = self.run_protocol(statuses=statuses)
                self.assertEqual(events[-1]['status'], 'paid')
                self.assertEqual(metrics['clicks'], clicks)

    def test_replayed_attempt_observes_without_clicking(self):
        for status, outcome in [('pending','unknown'), ('closed','closed')]:
            events, metrics = self.run_protocol(prior_attempted=True, statuses=[status])
            self.assertEqual(events[-1]['status'], outcome)
            self.assertEqual(metrics['clicks'], 0)

    def test_wrong_or_ambiguous_surface_never_clicks(self):
        cases = [{'wrongIssuer':True}, {'wrongFrame':True}, {'missingFrame':True},
                 {'wrongLink':True}, {'duplicate':True}, {'label':'Forsøk igjen'},
                 {'wrongPayment':True}, {'changeDuringRead':True}, {'httpError':True}]
        for case in cases:
            with self.subTest(case=case):
                events, metrics = self.run_protocol(**case)
                self.assertEqual(events[-1]['status'], 'unknown')
                self.assertEqual(metrics['clicks'], 0)

    def test_terminal_evidence_must_name_exact_order_and_change(self):
        for key in ['wrongOrder','wrongChange']:
            events, metrics = self.run_protocol(statuses=['closed'], **{key:True})
            self.assertEqual(events[-1]['status'], 'unknown')
            self.assertEqual(metrics['clicks'], 0)

    def test_inspection_after_restart_reads_terminal_only_and_never_aborts(self):
        cases = [({'statuses':['closed']},'closed'), ({'statuses':['paid']},'paid'),
                 ({'statuses':['pending']},'unknown'), ({'httpError':True},'unknown'),
                 ({'statuses':['closed'],'wrongOrder':True},'unknown'),
                 ({'statuses':['closed'],'wrongChange':True},'unknown')]
        for case, expected in cases:
            with self.subTest(case=case):
                events, metrics = self.run_protocol(parentUrl='https://oda.com/no/account/orders/order-1/',
                                                    observe_only=True, **case)
                self.assertEqual(events[-1]['status'], expected)
                self.assertEqual(metrics['clicks'], 0)
                self.assertEqual(len(events), 1)


class RetainedBrowser:
    checkout_provider = 'oda'
    lost_tab = False
    url = 'https://oda.com/no/checkout/threeDS/?paymentId=123'

    @contextmanager
    def _checkout_operation(self, deadline, preserve_session=False):
        assert preserve_session
        yield

    def _select_payment_tab(self, tab):
        return tab == 'owned' and not self.lost_tab

    @contextmanager
    def _inspection_tab(self):
        assert self.lost_tab
        yield

    def _order_url(self, order_id):
        return 'https://oda.com/no/account/orders/'+order_id+'/'

    def _open(self, url):
        assert self.lost_tab and url == self._order_url('order-1')
        self.url = url

    def _invoke(self, command, field):
        if command != 'get':
            raise AssertionError('Browser must not navigate or close')
        return {'url':self.url} if field == 'url' else {'cdpUrl':'ws://localhost/synthetic'}

    def _checkout_dispatch_tab(self):
        return 'owned'

    def _require_checkout_time(self, minimum):
        pass


class CardAbortFenceTests(unittest.TestCase):
    def run_abort(self, callback, *, lost_tab=False, case=None, **kwargs):
        if not shutil.which('node'):
            self.skipTest('Node is required')
        browser = RetainedBrowser()
        browser.lost_tab = lost_tab
        case = case or {}
        if lost_tab:
            case['parentUrl'] = browser._order_url('order-1')
        script = FIXTURE.replace('TEST', json.dumps(case)) + _CARD_ABORT_SCRIPT
        with patch('oda_payment_abort._CARD_ABORT_SCRIPT', script):
            return abort_card_payment(browser, {'tab_id':'owned','payment_id':'123'},
                                      callback, order_id='order-1', **kwargs)

    def test_durable_callback_precedes_native_abort(self):
        proofs = []
        result = self.run_abort(proofs.append)
        self.assertEqual(result['status'], 'closed')
        self.assertTrue(result['cancel_attempted'])
        self.assertEqual(len(proofs), 1)
        self.assertEqual(proofs[0]['payment_id'], '123')
        self.assertEqual(proofs[0]['tab_id'], 'owned')
        self.assertNotIn('DO_NOT_EMIT', json.dumps(proofs))

    def test_failed_fence_does_not_dispatch_abort(self):
        def fail(_):
            raise HouseholdError('Journal unavailable')
        result = self.run_abort(fail)
        self.assertEqual(result, {'status':'unknown','cancel_attempted':False})

    def test_replay_never_invokes_fence_again(self):
        result = self.run_abort(lambda _:self.fail('Duplicate abort'), prior={'cancel_attempted':True})
        self.assertEqual(result, {'status':'unknown','cancel_attempted':True})

    def test_lost_tab_observation_cannot_invoke_abort_callback(self):
        for status, expected in [('pending','unknown'), ('closed','closed'), ('paid','paid')]:
            with self.subTest(status=status):
                result = self.run_abort(lambda _:self.fail('Lost tab must never click'),
                                        lost_tab=True, case={'statuses':[status]})
                self.assertEqual(result['status'], expected)
                self.assertFalse(result['cancel_attempted'])

    def test_page_identity_rejects_duplicate_or_changed_parameters(self):
        for url in ['https://oda.com/no/checkout/threeDS/?paymentId=123&paymentId=123',
                    'https://oda.com/no/checkout/retry/?orderNumber=other',
                    'https://oda.com/no/checkout/retry/?orderNumber=order-1&orderChangeId=9',
                    'https://oda.com.evil/no/checkout/threeDS/?paymentId=123']:
            with self.subTest(url=url):
                self.assertFalse(_allowed_page(url,'123','order-1',None))


if __name__ == '__main__':
    unittest.main()
