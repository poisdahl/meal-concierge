"""Native payment replacement helpers; payment credentials stay in memory only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

from core import HouseholdError, cart_summary


# CDP observes the storefront's own polling rather than decoding or exporting
# payment tokens. The child survives navigation, so its final GET can establish
# the same request's outcome after the native Cancel handler returns to Oda.
_VIPPS_OBSERVER_SCRIPT = r"""
import {createInterface} from 'node:readline';
import {createHash} from 'node:crypto';
const input=createInterface({input:process.stdin})[Symbol.asyncIterator]();
const cfg=JSON.parse((await input.next()).value);
let finalOutcome=null;
const emit=v=>{if(v.ready||v.observing)process.stdout.write(JSON.stringify(v)+'\n');else finalOutcome=v};
const digest=v=>createHash('sha256').update(v).digest('hex');
const require=v=>{if(!v)throw Error('Unverified payment surface')};
const ws=new WebSocket(cfg.endpoint);
await new Promise((ok,bad)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',bad,{once:true})});
let seq=0,session,readSession,readTarget;const pending=new Map(),requests=new Map(),polls=new Map();
const statuses=new Set(['PENDING','SUBMITTED','ACCEPTED','REJECTED','FAILED','TIMEOUT','ERROR']);
const send=(method,params={},sessionId)=>new Promise((ok,bad)=>{
 const id=++seq,timer=setTimeout(()=>{pending.delete(id);bad(Error('Browser timeout'))},5000);
 pending.set(id,{ok,bad,timer});ws.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));
});
const candidate=url=>{try{const u=new URL(url);return u.protocol==='https:'&&u.hostname==='api.vipps.no'&&!u.port&&!u.username&&!u.password&&!u.hash}catch{return false}};
// Replay the observed request's routing/context headers as well as its auth.
// Values remain private child memory and are sent only to the identical
// observed URL; an unsuccessful replay never supplies terminal evidence.
const safeHeaders=headers=>Object.fromEntries(Object.entries(headers||{}).filter(([k])=>!k.startsWith(':')&&
 !['host','content-length','connection','accept-encoding'].includes(k.toLowerCase())));
const authorized=headers=>{const token=new URL(cfg.url).searchParams.get('token');return Boolean(token)&&Object.entries(headers).some(([k,v])=>k.toLowerCase()==='authorization'&&v==='Bearer '+token)};
const shape=url=>new URL(url).pathname.split('/').map(p=>['','dwo-api-application','v1','v2','deeplink','landingpage','landing-page','vipps-epayment-legacy-mobile-api','payment','payments','status','request','requests'].includes(p)?p:':id').join('/');
let paymentId=null,eventError=false,deleteHttp=null,phase='attach',readHttp=null,readJson=false,readStatus=null;
ws.addEventListener('message',async event=>{
 try{
  const m=JSON.parse(event.data),waiting=pending.get(m.id);
  if(waiting){pending.delete(m.id);clearTimeout(waiting.timer);m.error?waiting.bad(Error('Browser request failed')):waiting.ok(m.result);return}
  if(m.sessionId!==session)return;
  if(m.method==='Network.requestWillBeSent'){
   const r=m.params.request;if(!candidate(r.url))return;
   const headers=safeHeaders(r.headers);
   if(authorized(headers)&&['GET','DELETE'].includes(r.method))requests.set(m.params.requestId,{url:r.url,method:r.method,headers});
  }
  if(m.method==='Network.responseReceived'){
   const r=requests.get(m.params.requestId);if(!r)return;
   r.http=m.params.response.status;
   if(cfg.mode==='cancel'&&r.method==='GET'&&new URL(r.url).pathname===cfg.poll_path)polls.set(r.url,{...r,status:null});
   if(r.method==='DELETE'&&polls.has(r.url))deleteHttp=r.http;
  }
  if(m.method==='Network.loadingFinished'){
   const r=requests.get(m.params.requestId);if(!r||r.method!=='GET'||r.http!==200)return;
   if(cfg.mode==='cancel'&&new URL(r.url).pathname!==cfg.poll_path)return;
   const body=await send('Network.getResponseBody',{requestId:m.params.requestId},session);
   const value=JSON.parse(body.base64Encoded?Buffer.from(body.body,'base64').toString():body.body);
   if(value&&statuses.has(value.status))polls.set(r.url,{...r,status:value.status});
  }
 }catch{eventError=true}
});
const evaluate=async (expression, targetSession=session)=>{
 const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},targetSession);
 require(!result.exceptionDetails);return result.result.value;
};
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const prepareReader=async()=>{
 const created=await send('Target.createTarget',{url:'https://pay.vipps.no/',background:true});readTarget=created.targetId;
 readSession=(await send('Target.attachToTarget',{targetId:readTarget,flatten:true})).sessionId;
 const end=Date.now()+8000;
 while(Date.now()<end){if(await evaluate("location.origin==='https://pay.vipps.no'",readSession))return;await pause(100)}
 require(false);
};
const read=async poll=>{
 const value=await evaluate(`(async()=>{
  if(location.origin!=='https://pay.vipps.no')return {http:null,json:false,status:null};
  const r=await fetch(${JSON.stringify(poll.url)},{method:'GET',headers:${JSON.stringify({...poll.headers,'Cache-Control':'no-store',Pragma:'no-cache'})},redirect:'error',signal:AbortSignal.timeout(8000)});
  const json=!!r.headers.get('content-type')?.includes('application/json');
  if(r.status!==200||!json)return {http:r.status,json,status:null};
  const body=await r.json();return {http:r.status,json,status:typeof body?.status==='string'?body.status:null};
 })()`,readSession);
 readHttp=value.http;readJson=value.json;readStatus=statuses.has(value.status)?value.status:null;
 return readHttp===200&&readJson?readStatus:null;
};
const result=(status,poll,extra={})=>({status:status==='ACCEPTED'?'paid':['REJECTED','FAILED','TIMEOUT'].includes(status)?'closed':'unknown',
 terminal_status:status||'UNKNOWN',poll_url_digest:poll?digest(poll.url):null,...extra});
const chooser=`(()=>{
 if(location.href!==${JSON.stringify(cfg.url)})return null;
 const visible=e=>{for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.visibility==='hidden'||s.display==='none'||s.opacity==='0')return false}const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
 const norm=s=>(s||'').replace(/\\s+/g,' ').trim();
 const links=[...document.querySelectorAll('a.cancel-link[href]')].filter(e=>visible(e)&&e.getAttribute('aria-disabled')!=='true'&&
  (norm(e.innerText)==='Cancel the payment'&&e.getAttribute('aria-label')==='Cancel payment and return to the webshop'||
   norm(e.innerText)==='Avbryt betalingen'&&e.getAttribute('aria-label')==='Avbryt betalingen og gå tilbake til nettbutikken'));
 if(links.length!==1)return null;
 const u=new URL(links[0].href);if(u.protocol!=='https:'||u.hostname!=='oda.com'||u.port||u.username||u.password||u.hash||
  !/^\\/no\\/checkout\\/[1-9][0-9]*\\/[A-Za-z0-9_-]+\\/redirect-return\\/$/.test(u.pathname))return null;
 return links[0];
})()`;
try{
 const targets=(await send('Target.getTargets')).targetInfos.filter(t=>t.type==='page'&&t.url===cfg.url);require(targets.length===1);
 session=(await send('Target.attachToTarget',{targetId:targets[0].targetId,flatten:true})).sessionId;
 if(cfg.mode==='cancel'){
  phase='source_identity';
  paymentId=await evaluate(`(()=>{const link=${chooser};return link?new URL(link.href).pathname.split('/')[3]:null})()`);
  require(typeof paymentId==='string'&&/^[1-9][0-9]*$/.test(paymentId));
 }
 await send('Network.enable',{},session);
 phase='observe_poll';
 if(cfg.mode==='probe')emit({observing:true});
 const end=Date.now()+Math.min(cfg.observe_ms||15000,60000);
 while(Date.now()<end&&polls.size===0)await pause(100);
 require(polls.size===1&&!eventError);
 const poll=[...polls.values()][0];
 if(cfg.mode==='probe'){emit({observed:true,endpoint_shape:shape(poll.url),endpoint_digest:digest(poll.url),http:poll.http,status:poll.status,header_names:Object.keys(poll.headers).sort()});}
 else{
  // The permitted endpoint path comes from a real native observation,
  // never the unrelated mock endpoint present in the bundled gateway code.
  phase='endpoint';
  require(typeof cfg.poll_path==='string'&&new URL(poll.url).pathname===cfg.poll_path);
  phase='prepare_reader';
  await prepareReader();
  phase='initial_status';
  const state=await read(poll);
  if(['ACCEPTED','REJECTED','FAILED','TIMEOUT'].includes(state)){emit(result(state,poll,{cancel_attempted:false,payment_id:paymentId}));}
  else if(cfg.prior_attempted){emit(result(state,poll,{cancel_attempted:true,payment_id:paymentId}));}
  else{
   require(['PENDING','SUBMITTED'].includes(state));
   phase='cancel_surface';
   require(await evaluate(`Boolean(${chooser})`)===true);
   emit({ready:true,poll_url_digest:digest(poll.url),observed_status:state,payment_id:paymentId});
   phase='await_authorization';
   const command=await input.next();require(!command.done&&command.value==='cancel_once');
   phase='recheck_status';
   const latest=await read(poll);
   if(['ACCEPTED','REJECTED','FAILED','TIMEOUT'].includes(latest)){emit(result(latest,poll,{cancel_attempted:false,payment_id:paymentId}));}
   else{
    require(['PENDING','SUBMITTED'].includes(latest));
    phase='native_cancel';
    require(await evaluate(`(()=>{const link=${chooser};if(!link||new URL(link.href).pathname.split('/')[3]!==${JSON.stringify(paymentId)})return false;link.scrollIntoView({block:'center'});const r=link.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);if(!(hit===link||link.contains(hit)))return false;link.click();return true})()`)===true);
    phase='terminal_status';
    let terminal=null;const limit=Date.now()+Math.min(cfg.after_ms||15000,30000);
    while(Date.now()<limit){try{terminal=await read(poll)}catch{}if(['ACCEPTED','REJECTED','FAILED','TIMEOUT'].includes(terminal))break;await pause(500)}
    emit(result(terminal,poll,{cancel_attempted:true,delete_http_status:deleteHttp,payment_id:paymentId}));
   }
  }
 }
}catch{emit({status:'unknown',observation_failed:true,phase,poll_count:polls.size,network_observation_error:eventError,
 read_http_status:readHttp,read_content_json:readJson,read_status:readStatus,payment_id:paymentId,
 header_names:polls.size===1?Object.keys([...polls.values()][0].headers).sort():[]});process.exitCode=1}
finally{await Promise.all([readTarget?send('Target.closeTarget',{targetId:readTarget}).catch(()=>{}):null,
 session?send('Target.detachFromTarget',{sessionId:session}).catch(()=>{}):null]);ws.close();
 if(finalOutcome)process.stdout.write(JSON.stringify(finalOutcome)+'\n')}
process.exit(process.exitCode||0);
"""


# Actual native GET200 SUBMITTED observation from the authorized Oda test.
_VIPPS_POLL_PATH = "/vipps-epayment-legacy-mobile-api/landing-page"


def _line(process, seconds):
    if not select.select([process.stdout], [], [], max(.01, seconds))[0]:
        raise HouseholdError("Payment status observation timed out")
    value = json.loads(process.stdout.readline())
    if not isinstance(value, dict):
        raise HouseholdError("Payment status observation was invalid")
    return value


def _native_vipps_terminal(browser, url, context):
    """Read retained native HTTP evidence without exporting its credentials."""
    from urllib.parse import parse_qs
    token = parse_qs(urlsplit(url).query).get("token", [])
    if len(token) != 1 or not token[0]:
        return None
    try:
        records = browser._invoke("network", "requests", "--filter", "api.vipps.no", "--method", "GET").get("requests")
        if not isinstance(records, list):
            return None
        def exact_api(row, path):
            if not isinstance(row, dict) or row.get("method") != "GET" or row.get("status") != 200:
                return False
            parsed = urlsplit(str(row.get("url") or ""))
            return parsed.scheme == "https" and parsed.netloc == "api.vipps.no" and parsed.path == path and not parsed.fragment
        validations = [row for row in records if exact_api(row, _VIPPS_POLL_PATH + "/validate-token")
                       and parse_qs(urlsplit(row["url"]).query).get("token") == token]
        if not validations:
            return None
        def latest(rows):
            if any(type(row.get("timestamp")) not in (int, float) for row in rows):
                raise ValueError("Native payment observation has no timestamp")
            return max(rows, key=lambda row: row["timestamp"])
        validation_row = latest(validations)
        validation = browser._invoke("network", "request", str(validation_row["requestId"]))
        if not exact_api(validation, _VIPPS_POLL_PATH + "/validate-token") or validation.get("url") != validation_row["url"]:
            return None
        claims = json.loads(validation.get("responseBody", ""))
        if (not isinstance(claims, dict) or type(claims.get("amount")) is not int
                or claims["amount"] != context["expected_total"] or claims.get("currency") != "NOK"):
            return None
        fallback = urlsplit(str(claims.get("fallback") or ""))
        match = re.fullmatch(r"/no/checkout/([1-9][0-9]*)/[A-Za-z0-9_-]+/redirect-return/", fallback.path)
        if fallback.scheme != "https" or fallback.netloc != "oda.com" or fallback.fragment or not match:
            return None
        def same_poll(row):
            if not exact_api(row, _VIPPS_POLL_PATH) or row.get("url") != claims.get("url"):
                return False
            headers = row.get("headers")
            return isinstance(headers, dict) and [v for k, v in headers.items() if k.lower() == "authorization"] == ["Bearer " + token[0]]
        polls = [row for row in records if same_poll(row)]
        if not polls:
            return None
        row = latest(polls)
        observed = browser._invoke("network", "request", str(row["requestId"]))
        if not same_poll(observed) or observed.get("requestId") != row["requestId"]:
            return None
        body = json.loads(observed.get("responseBody", ""))
        terminal = body.get("status") if isinstance(body, dict) else None
        if terminal not in {"ACCEPTED", "REJECTED", "FAILED", "TIMEOUT"}:
            return None
        return {"status": "paid" if terminal == "ACCEPTED" else "closed", "terminal_status": terminal,
                "source": "native_http200", "payment_id": match[1],
                "poll_url_digest": hashlib.sha256(row["url"].encode()).hexdigest(),
                "gateway_url_digest": context["gateway_url_digest"], "cancel_attempted": False}
    except (HouseholdError, KeyError, TypeError, ValueError):
        return None


def close_vipps_request(browser, context, before_cancel, *, deadline, prior=None):
    """Close only the retained request; callback must durably journal the click."""
    from oda_browser import _oda_vipps_gateway_script
    unknown = {"status": "unknown"}
    if (not isinstance(context, dict) or browser.checkout_provider != "oda"
            or type(context.get("expected_total")) is not int or context["expected_total"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(context.get("gateway_url_digest", "")))):
        return unknown
    with browser._checkout_operation(deadline, preserve_session=True):
        if not browser._select_payment_tab(context.get("tab_id")):
            return unknown
        current_url = str(browser._invoke("get", "url").get("url") or "")
        url = current_url
        if hashlib.sha256(url.encode()).hexdigest() != context["gateway_url_digest"]:
            history = browser._invoke("network", "requests", "--filter", "pay.vipps.no", "--method", "GET").get("requests")
            urls = {row.get("url") for row in history or [] if isinstance(row, dict)
                    and row.get("method") == "GET" and str(row.get("resourceType", "")).lower() == "document"
                    and isinstance(row.get("url"), str)
                    and hashlib.sha256(row["url"].encode()).hexdigest() == context["gateway_url_digest"]}
            if len(urls) != 1:
                return unknown
            url = urls.pop()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "pay.vipps.no" or parsed.username or parsed.password or parsed.port:
            return unknown
        terminal = _native_vipps_terminal(browser, url, context)
        if terminal:
            return {**terminal, "cancel_attempted": bool((prior or {}).get("cancel_attempted"))}
        if current_url != url:
            return unknown
        node = shutil.which("node")
        if not node:
            return unknown
        surface = browser._eval(_oda_vipps_gateway_script(context["expected_total"], browser.vipps_phone_number,
                                expected_url=url, allow_post_dispatch_ack=True, allow_source_bound_amountless=True))
        if not surface.get("identity") or not (surface.get("sent") or surface.get("expired") or surface.get("fillable")):
            return unknown
        endpoint = browser._invoke("get", "cdp-url").get("cdpUrl")
        parsed_endpoint = urlsplit(str(endpoint or ""))
        if parsed_endpoint.scheme != "ws" or parsed_endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return unknown
        process = subprocess.Popen([node, "--input-type=module", "-e", _VIPPS_OBSERVER_SCRIPT],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, env={"PATH": os.environ.get("PATH", os.defpath)})
        attempted = bool(prior and prior.get("cancel_attempted"))
        payment_id = (prior or {}).get("cancel_evidence", {}).get("payment_id")
        try:
            process.stdin.write(json.dumps({"endpoint": endpoint, "url": url, "mode": "cancel",
                "prior_attempted": attempted, "poll_path": _VIPPS_POLL_PATH,
                "observe_ms": 15000, "after_ms": 15000}) + "\n")
            process.stdin.flush()
            first = _line(process, min(25, deadline-time.monotonic()))
            if first.get("ready"):
                payment_id = first.get("payment_id")
                if attempted or browser._checkout_dispatch_tab() != context["tab_id"]:
                    return unknown
                latest = str(browser._invoke("get", "url").get("url") or "")
                if latest != url:
                    return unknown
                observed = browser._eval(_oda_vipps_gateway_script(context["expected_total"], browser.vipps_phone_number,
                                      expected_url=url, allow_post_dispatch_ack=True, allow_source_bound_amountless=True))
                if not observed.get("identity") or not (observed.get("sent") or observed.get("fillable")):
                    return unknown
                evidence = {"cancel_attempted": True, "gateway_url_digest": context["gateway_url_digest"],
                            "poll_url_digest": first["poll_url_digest"], "observed_status": first["observed_status"],
                            "payment_id": first["payment_id"]}
                before_cancel(evidence)
                attempted = True
                process.stdin.write("cancel_once\n"); process.stdin.flush()
                first = _line(process, min(25, deadline-time.monotonic()))
            return {**first, "cancel_attempted": attempted or first.get("cancel_attempted", False),
                    "gateway_url_digest": context["gateway_url_digest"], "payment_id": first.get("payment_id") or payment_id}
        except (OSError, ValueError, HouseholdError):
            return {**unknown, "cancel_attempted": attempted}
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def adopt_vipps_request(browser, cart, review, *, deadline, order_id):
    """Recover missing context only from a retained, provider-bound request.

    Old journals without a hosted tab and native validation response stay
    unresolved. An expired token or unpaid order is not closure evidence.
    """
    from oda_browser import _oda_vipps_gateway_script
    if browser.checkout_provider != "oda":
        return None
    expected = browser._cart_expectation(cart)
    with browser._checkout_operation(deadline, preserve_session=True):
        tabs = browser._invoke("tab", "list").get("tabs", [])
        hosted = [tab for tab in tabs if urlsplit(str(tab.get("url") or "")).netloc == "pay.vipps.no"]
        if len(hosted) != 1 or not browser._select_payment_tab(hosted[0].get("tabId")):
            return None
        url = str(browser._invoke("get", "url").get("url") or "")
        parsed = urlsplit(url)
        token = parse_qs(parsed.query).get("token")
        if (parsed.scheme != "https" or parsed.netloc != "pay.vipps.no" or parsed.path != "/"
                or parsed.fragment or not token or len(token) != 1 or not token[0]):
            return None
        records = browser._invoke("network", "requests", "--filter", "api.vipps.no", "--method", "GET").get("requests", [])
        validations = [row for row in records if row.get("method") == "GET" and row.get("status") == 200
                       and urlsplit(str(row.get("url") or "")).scheme == "https"
                       and urlsplit(str(row.get("url") or "")).netloc == "api.vipps.no"
                       and not urlsplit(row["url"]).fragment
                       and urlsplit(row["url"]).path == _VIPPS_POLL_PATH + "/validate-token"
                       and parse_qs(urlsplit(row["url"]).query).get("token") == token]
        if len(validations) != 1:
            return None
        observed = browser._invoke("network", "request", str(validations[0]["requestId"]))
        try:
            claims = json.loads(observed.get("responseBody", ""))
        except (TypeError, ValueError):
            return None
        if (observed.get("url") != validations[0]["url"] or observed.get("status") != 200
                or observed.get("method") != "GET" or observed.get("requestId") != validations[0]["requestId"]
                or not isinstance(claims, dict) or type(claims.get("amount")) is not int
                or claims["amount"] != expected["total_minor"] or claims.get("currency") != "NOK"):
            return None
        fallback = urlsplit(str(claims.get("fallback") or ""))
        match = re.fullmatch(r"/no/checkout/([1-9][0-9]*)/[A-Za-z0-9_-]+/redirect-return/", fallback.path)
        if fallback.scheme != "https" or fallback.netloc != "oda.com" or fallback.fragment or not match:
            return None
        surface = browser._eval(_oda_vipps_gateway_script(expected["total_minor"], browser.vipps_phone_number,
                                expected_url=url, allow_source_bound_amountless=True, allow_post_dispatch_ack=True))
        if not surface.get("identity") or not (surface.get("fillable") or surface.get("sent") or surface.get("expired")):
            return None
        # A native payment ID must resolve to the independently verified order;
        # equal amount alone cannot identify a payment after a lost handoff.
        with browser._inspection_tab():
            value = browser._eval(r"""(async()=>{
 if(location.origin!=='https://oda.com')return JSON.stringify({});
 const r=await fetch('/api/v1/checkout/payment/'+PAYMENT+'/retry/',{method:'GET',credentials:'same-origin',cache:'no-store',redirect:'error'});
 return r.ok?JSON.stringify({retry:await r.json()}):JSON.stringify({});
})()""".replace("PAYMENT", json.dumps(match[1]))).get("retry")
        params = value.get("params") if isinstance(value, dict) else None
        if (not isinstance(params, dict) or value.get("type") != "checkout-payment-retry"
                or params.get("order_number") != order_id or params.get("order_change_id") is not None):
            return None
        return {"tab_id": hosted[0]["tabId"], "expected_total": expected["total_minor"],
                "gateway_url_digest": hashlib.sha256(url.encode()).hexdigest(),
                "order_id": order_id if review.get("order_id") else None}


def _current_retry_target(browser, order_id):
    from urllib.parse import parse_qs
    url = str(browser._invoke("get", "url").get("url") or "")
    parsed = urlsplit(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "oda.com" or parsed.path != "/no/checkout/retry/"
            or parsed.fragment or set(params) != {"orderNumber", "orderChangeId"}
            or params["orderNumber"] != [order_id] or len(params["orderChangeId"]) != 1
            or not re.fullmatch(r"[1-9][0-9]{0,15}", params["orderChangeId"][0])):
        raise HouseholdError("Oda did not return the exact native order-addition retry target")
    return {"order_id": order_id, "order_change_id": params["orderChangeId"][0]}


def _verify_base(browser, order_id, before_order, deadline):
    from service_common import oda_order_matches_addition, require_provider_identity
    current = browser._binding_client().call("get_order", {"order_number": order_id}, deadline=deadline)
    require_provider_identity(current, order_id)
    if not oda_order_matches_addition(before_order, current, {"items": [], "total": 0}):
        raise HouseholdError("The original Oda order changed; reconcile the addition before replacing payment")


def _payment_retry(browser, payment_id, order_id):
    """Read the merchant retry target for the exact payment returned by Vipps."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(payment_id or "")):
        raise HouseholdError("The closed Vipps request has no exact Oda payment identity")
    script = r"""(async()=>{
 if(location.origin!=='https://oda.com')return JSON.stringify({});
 const response=await fetch('/api/v1/checkout/payment/'+PAYMENT+'/retry/',{method:'GET',credentials:'same-origin',cache:'no-store',redirect:'error'});
 if(!response.ok)return JSON.stringify({});
 return JSON.stringify({retry:await response.json()});
})()""".replace("PAYMENT", json.dumps(str(payment_id)))
    value = browser._eval(script).get("retry")
    params = value.get("params") if isinstance(value, dict) else None
    if (not isinstance(params, dict) or value.get("type") != "checkout-payment-retry"
            or params.get("order_number") != order_id
            or type(params.get("order_change_id")) is not int or params["order_change_id"] <= 0):
        raise HouseholdError("The closed Oda payment no longer resolves to this exact order addition")
    return {"order_id": order_id, "order_change_id": str(params["order_change_id"]), "payment_id": str(payment_id)}


def _cart_digest(cart):
    from service_common import canonical
    return hashlib.sha256(canonical(cart_summary(cart)).encode()).hexdigest()


def _card_failure_retry(browser, context, order_id, order_change_id, deadline):
    """Re-read the terminal native pay response without leaving its retained tab."""
    previous = browser._checkout_dispatch_tab()
    try:
        failure = browser.checkout_payment_failure(context, expected_order_id=order_id, deadline=deadline)
    finally:
        if previous is not None and not browser._select_payment_tab(previous):
            raise HouseholdError("The retained Oda payment review tab is unavailable")
    if (not isinstance(failure, dict) or failure.get("payment_failed") is not True
            or failure.get("order_id") != order_id or failure.get("order_change_id") != order_change_id
            or not isinstance(order_change_id, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", order_change_id)):
        raise HouseholdError("The native card failure no longer resolves to this exact order addition")
    return {"order_id": order_id, "order_change_id": order_change_id, "card_failure_context": dict(context)}


def verify_oda_addition_retry(browser, order_id, cart, before_order, binding, target, *, deadline):
    """Read only; verify the same payment target immediately before card dispatch."""
    from oda_browser import require_order_binding
    require_order_binding(binding)
    if not isinstance(target, dict) or target.get("cart_digest") != _cart_digest(cart):
        raise HouseholdError("The retained Oda addition goods changed")
    _verify_base(browser, order_id, before_order, deadline)
    native = _current_retry_target(browser, order_id)
    observed = (_card_failure_retry(browser, target["card_failure_context"], order_id, target.get("order_change_id"), deadline)
                if target.get("card_failure_context") else _payment_retry(browser, target.get("payment_id"), order_id))
    if any(target.get(k) != v for source in (native, observed) for k, v in source.items()):
        raise HouseholdError("The retained Oda addition retry target changed")


def prepare_oda_addition_retry(browser, order_id, cart, before_order, binding, *, deadline, closure, retained_target=None):
    """Resolve the same closed payment with a read; no goods transition is needed."""
    from oda_browser import require_order_binding
    from service_common import safe_order_id
    order_id = safe_order_id(order_id)
    require_order_binding(binding)
    if (not isinstance(closure, dict) or closure.get("status") != "closed"
            or closure.get("terminal_status") not in {"REJECTED", "FAILED", "TIMEOUT"}):
        raise HouseholdError("The original Vipps request is not proven closed")
    with browser._checkout_operation(deadline, preserve_session=True):
        _verify_base(browser, order_id, before_order, deadline)
        with browser._inspection_tab():
            browser._read_order_binding(order_id, before_order, deadline=deadline, expected_binding=binding)
            pair = (_card_failure_retry(browser, closure["card_failure_context"], order_id, closure.get("order_change_id"), deadline)
                    if closure.get("source") == "native_payment_failure" and closure.get("card_failure_context")
                    else _payment_retry(browser, closure.get("payment_id"), order_id))
        target = {**pair, "cart_digest": _cart_digest(cart)}
        if retained_target is not None and target != retained_target:
            raise HouseholdError("The retained Oda addition retry target changed")
        if pair.get("card_failure_context"):
            # Reloading would discard the exact native response. The retained
            # tab already has the verified retry route; recovery gets its own tab.
            if not browser._select_payment_tab(pair["card_failure_context"]["tab_id"]):
                raise HouseholdError("The retained Oda card failure tab is unavailable")
        else:
            browser._open("https://oda.com/no/checkout/retry/?orderNumber=" + order_id + "&orderChangeId=" + pair["order_change_id"])
        verify_oda_addition_retry(browser, order_id, cart, before_order, binding, target, deadline=deadline)
        return target
