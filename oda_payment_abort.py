"""Abort one retained Oda card challenge through its issuer's native control."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
import re
import select
import shutil
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

from core import HouseholdError


_CARD_ABORT_SCRIPT = r"""
import {createInterface} from 'node:readline';
import {createHash} from 'node:crypto';
const input=createInterface({input:process.stdin})[Symbol.asyncIterator]();
const cfg=JSON.parse((await input.next()).value);
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
const require=value=>{if(!value)throw Error('Unverified card abort surface')};
const ws=new WebSocket(cfg.endpoint);
await new Promise((ok,bad)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',bad,{once:true})});
let id=0,parent,issuer;const pending=new Map();
ws.addEventListener('message',event=>{const m=JSON.parse(event.data),p=pending.get(m.id);if(!p)return;pending.delete(m.id);clearTimeout(p.timer);m.error?p.bad(Error('Browser request failed')):p.ok(m.result)});
const send=(method,params={},sessionId)=>new Promise((ok,bad)=>{const n=++id,timer=setTimeout(()=>{pending.delete(n);bad(Error('Browser timeout'))},5000);pending.set(n,{ok,bad,timer});ws.send(JSON.stringify({id:n,method,params,...(sessionId?{sessionId}:{})}))});
const evaluate=async(session,expression,returnByValue=true)=>{const v=await send('Runtime.evaluate',{expression,returnByValue,awaitPromise:true},session);require(!v.exceptionDetails);return v.result};
const visible=`e=>{for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false}const r=e.getBoundingClientRect();return r.width>0&&r.height>0}`;
const frameExpression=`(()=>{if(location.href!==${JSON.stringify(cfg.url)})return null;const visible=${visible};const c=[...document.querySelectorAll('.adyen-checkout__threeds2__challenge')].filter(visible);const f=c.length===1?[...c[0].querySelectorAll('iframe[name="threeDSIframe"]')].filter(visible):[];return f.length===1?f[0]:null})()`;
const observation=`(async()=>{
 const expected=${JSON.stringify(cfg)},u=new URL(location.href);
 if(u.origin!=='https://oda.com'||u.hash)return {status:'unknown'};
 const three=u.pathname==='/no/checkout/threeDS/'&&u.searchParams.size===1&&u.searchParams.get('paymentId')===expected.payment_id;
 const resultPage=['/no/checkout/retry/','/no/checkout/success/'].includes(u.pathname)&&u.searchParams.get('orderNumber')===expected.order_id&&
   (expected.order_change_id===null?u.searchParams.size===1:u.searchParams.size===2&&u.searchParams.get('orderChangeId')===expected.order_change_id);
 const inspection=expected.observe_only===true&&u.href===expected.url;
 if(!three&&!resultPage&&!inspection)return {status:'unknown'};
 const r=await fetch('/api/v1/payments/adyen/three-ds/'+expected.payment_id+'/',{method:'GET',credentials:'same-origin',redirect:'error',signal:AbortSignal.timeout(5000)});
 if(!r.ok||location.href!==u.href)return {status:'unknown'};
 const d=await r.json(),p=d.params;
 if(!p||typeof p!=='object'||Array.isArray(p))return {status:'unknown'};
 if(d.type==='payments-providers-adyen-three-ds'&&Object.keys(p).join(',')==='payment_id'&&String(p.payment_id)===expected.payment_id)return {status:'pending'};
 if(!['checkout-payment-retry','checkout-payment-success'].includes(d.type)||Object.keys(p).sort().join(',')!=='order_change_id,order_number'||p.order_number!==expected.order_id)return {status:'unknown'};
 if(expected.order_change_id===null?p.order_change_id!==null:!Number.isSafeInteger(p.order_change_id)||p.order_change_id<=0||String(p.order_change_id)!==expected.order_change_id)return {status:'unknown'};
 return {status:d.type==='checkout-payment-retry'?'closed':'paid',terminal_status:d.type,source:'oda_three_ds',payment_id:expected.payment_id,order_id:expected.order_id,...(expected.order_change_id===null?{}:{order_change_id:expected.order_change_id})};
})()`;
const observe=async()=>{try{return (await evaluate(parent,observation)).value||{status:'unknown'}}catch{return {status:'unknown'}}};
const terminal=r=>['closed','paid'].includes(r.status);
try{
 const pages=(await send('Target.getTargets')).targetInfos.filter(t=>t.type==='page'&&t.url===cfg.url);require(pages.length===1);
 parent=(await send('Target.attachToTarget',{targetId:pages[0].targetId,flatten:true})).sessionId;
 let state=await observe();
 if(terminal(state)||cfg.observe_only||cfg.prior_attempted||state.status!=='pending'){emit(terminal(state)?state:{status:'unknown'});}
 else{
  const frameId=async()=>{const obj=await evaluate(parent,frameExpression,false);require(obj.objectId);const d=await send('DOM.describeNode',{objectId:obj.objectId},parent);require(d.node.frameId);return d.node.frameId};
  const frame=await frameId();
  const targets=(await send('Target.getTargets')).targetInfos.filter(t=>t.type==='iframe'&&t.targetId===frame);require(targets.length===1);
  const issuerUrl=targets[0].url,origin=new URL(issuerUrl);
  require(origin.protocol==='https:'&&origin.hostname==='acs2.edb.com'&&!origin.port&&!origin.username&&!origin.password);
  issuer=(await send('Target.attachToTarget',{targetId:frame,flatten:true})).sessionId;
  const cancel=`(()=>{if(location.href!==${JSON.stringify(issuerUrl)}||document.readyState!=='complete')return null;const visible=${visible},norm=v=>(v||'').replace(/\\s+/g,' ').trim();const c=[...document.querySelectorAll('a[href]')].filter(e=>visible(e)&&e.getAttribute('aria-disabled')!=='true'&&norm(e.innerText)==='Avbryt');if(c.length!==1)return null;const u=new URL(c[0].href);return u.protocol==='https:'&&u.origin===location.origin&&!u.username&&!u.password?c[0]:null})()`;
  require((await evaluate(issuer,`Boolean(${cancel})`)).value===true);
  emit({ready:true,payment_id:cfg.payment_id,issuer_origin:origin.origin,issuer_url_digest:createHash('sha256').update(issuerUrl).digest('hex')});
  const command=await input.next();require(!command.done&&command.value==='abort_once');
  state=await observe();
  if(terminal(state)){emit(state);}
  else{
   require(state.status==='pending'&&await frameId()===frame);
   require((await evaluate(issuer,`(()=>{const control=${cancel};if(!control)return false;control.click();return true})()`)).value===true);
   const until=Date.now()+cfg.observe_ms;
   do{state=await observe();if(terminal(state))break;await new Promise(r=>setTimeout(r,250));}while(Date.now()<until);
   emit(terminal(state)?state:{status:'unknown'});
  }
 }
}catch{emit({status:'unknown'});}
finally{if(issuer)await send('Target.detachFromTarget',{sessionId:issuer}).catch(()=>{});if(parent)await send('Target.detachFromTarget',{sessionId:parent}).catch(()=>{});ws.close()}
process.exit(0);
"""


def _allowed_page(url, payment_id, order_id, order_change_id):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "oda.com" or parsed.fragment
            or parsed.username or parsed.password):
        return False
    params = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.path == "/no/checkout/threeDS/":
        return params == {"paymentId": [payment_id]}
    expected = {"orderNumber": [order_id]}
    if order_change_id is not None:
        expected["orderChangeId"] = [order_change_id]
    return parsed.path in {"/no/checkout/retry/", "/no/checkout/success/"} and params == expected


def abort_card_payment(browser, context, before_abort, *, order_id,
                       order_change_id=None, deadline=None, prior=None):
    """Only an exact native terminal response closes a durably fenced abort."""
    attempted = bool((prior or {}).get("cancel_attempted"))
    unknown = {"status": "unknown", "cancel_attempted": attempted}
    if (browser.checkout_provider != "oda" or not isinstance(context, dict)
            or set(context) != {"tab_id", "payment_id"}
            or not isinstance(context["tab_id"], str) or not context["tab_id"]
            or not isinstance(context["payment_id"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", context["payment_id"])
            or not isinstance(order_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", order_id)
            or order_change_id is not None and (not isinstance(order_change_id, str)
                or not re.fullmatch(r"[1-9][0-9]{0,15}", order_change_id))):
        return unknown
    end = deadline if deadline is not None else time.monotonic() + 45
    node = shutil.which("node")
    if node is None or end - time.monotonic() < 1:
        return unknown
    with browser._checkout_operation(end, preserve_session=True), ExitStack() as inspection:
        retained = browser._select_payment_tab(context["tab_id"])
        url = str(browser._invoke("get", "url").get("url") or "") if retained else ""
        observe_only = not _allowed_page(url, context["payment_id"], order_id, order_change_id)
        if observe_only:
            # A restart loses the issuer page, not the recorded native payment
            # identity. The caller verifies the frozen order/account/goods first.
            # Read its outcome in an inspection tab; never reconstruct a click.
            inspection.enter_context(browser._inspection_tab())
            url = browser._order_url(order_id)
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "oda.com" or parsed.fragment:
                return unknown
            browser._open(url)
            if browser._invoke("get", "url").get("url") != url:
                return unknown
        endpoint = browser._invoke("get", "cdp-url").get("cdpUrl")
        parsed = urlsplit(str(endpoint or ""))
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return unknown
        process = subprocess.Popen([node, "--input-type=module", "-e", _CARD_ABORT_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env={"PATH": os.environ.get("PATH", os.defpath)})
        def line():
            remaining = min(25, end - time.monotonic())
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise HouseholdError("Card abort observation timed out")
            result = json.loads(process.stdout.readline())
            if not isinstance(result, dict):
                raise HouseholdError("Card abort observation is invalid")
            return result
        try:
            process.stdin.write(json.dumps({"endpoint": endpoint, "url": url,
                "payment_id": context["payment_id"], "order_id": order_id,
                "order_change_id": order_change_id, "prior_attempted": attempted,
                "observe_only": observe_only,
                "observe_ms": max(0, min(15000, int((end-time.monotonic()-2)*1000)))})+"\n")
            process.stdin.flush()
            result = line()
            if result.get("ready"):
                if (observe_only or attempted or result.get("payment_id") != context["payment_id"]
                        or result.get("issuer_origin") != "https://acs2.edb.com"
                        or re.fullmatch(r"[0-9a-f]{64}", str(result.get("issuer_url_digest", ""))) is None
                        or browser._checkout_dispatch_tab() != context["tab_id"]
                        or browser._invoke("get", "url").get("url") != url):
                    return unknown
                browser._require_checkout_time(1)
                before_abort({"payment_id": context["payment_id"], "tab_id": context["tab_id"],
                    "issuer_origin": result["issuer_origin"], "issuer_url_digest": result["issuer_url_digest"]})
                attempted = True
                process.stdin.write("abort_once\n");process.stdin.flush()
                result = line()
            if (result.get("status") in {"closed", "paid"}
                    and result.get("payment_id") == context["payment_id"]
                    and result.get("order_id") == order_id
                    and result.get("order_change_id") == order_change_id
                    and result.get("source") == "oda_three_ds"
                    and result.get("terminal_status") == {
                        "closed": "checkout-payment-retry", "paid": "checkout-payment-success"}[result["status"]]):
                return {**result, "cancel_attempted": attempted}
            return {**unknown, "cancel_attempted": attempted}
        except (OSError, ValueError, HouseholdError):
            return {**unknown, "cancel_attempted": attempted}
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
