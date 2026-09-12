"""Operation-specific Oda browser fallback for protected order actions."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import calendar
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
import unicodedata
from urllib.parse import parse_qs, quote, urlsplit

from core import CancellationPreconditionError, CheckoutPreconditionError, HouseholdError, cart_summary, validate_delivery_slot


class OdaCheckoutMismatchError(HouseholdError):
    """The live checkout surface no longer matches the supplied cart snapshot."""


STORE_URL = "https://oda.com/no/"
CART_URL = "https://oda.com/no/cart/"
CHECKOUT_ENTRY_URL = "https://oda.com/no/checkout/"
CHECKOUT_MODIFY_URL = "https://oda.com/no/checkout/modify/"
RECOMMENDATIONS_URL = "https://oda.com/no/checkout/recommendations/"
CHECKOUT_URL = "https://oda.com/no/checkout/confirm/"
CHECKOUT_BROWSER_TIMEOUT = 90
CANCELLATION_BROWSER_TIMEOUT = 105
FINAL_CLICK_MARGIN = 15
DEFAULT_BROWSER_ARGS = "--disable-quic"
CANCELLATION_BROWSER_ARGS = "--disable-quic,--disable-http2,--blink-settings=imagesEnabled=false"
ODA_CHECKOUT_AMOUNT_LABELS = {
    "discounts": "Du sparer",
    "discounted_subtotal": "Delsum",
    "bags": "Leveringsemballasje",
    "delivery_price": "Levering",
    "other_fee": "Tillegg for mindre bestilling",
    "provider_total": "Total inkl. MVA",
}
MATHEM_CHECKOUT_AMOUNT_LABELS = {
    "discounted_subtotal": "Delsumma",
    "discounts": "Du sparar",
    "bags": "Lådor",
    "delivery_price": "Leverans",
    "delivery_discount": "Gratis leverans",
    "other_fee": "Avgift för liten varukorg",
    "provider_total": "Totalt inkl. moms",
}
# Observed product discounts and delivery credit are separate rows. Other
# discounts or deposit rows still require their own verified provider contract.
MATHEM_CHECKOUT_PRODUCT_LABEL = re.compile(r"(?:1 vara|(?:0|[2-9]|[1-9]\d{1,6}) varor)")
ODA_CHECKOUT_PRODUCT_LABEL = re.compile(r"(?:1 vare|(?:0|[2-9]|[1-9]\d{1,6}) varer)")
ODA_CHECKOUT_AMOUNT_KEYS = (
    "product_subtotal",
    "delivery_price",
    "discounts",
    "deposits",
    "bags",
    "other_fees",
    "provider_total",
)


def _checkout_amount_labels(provider: str) -> dict[str, str]:
    if provider == "oda":
        return ODA_CHECKOUT_AMOUNT_LABELS
    if provider == "mathem":
        return MATHEM_CHECKOUT_AMOUNT_LABELS
    raise HouseholdError("Unsupported checkout provider")


def oda_checkout_pay_request_id(value: Any) -> str:
    """Return the one completed Oda checkout-payment request."""

    if not isinstance(value, Mapping) or not isinstance(value.get("requests"), list):
        raise HouseholdError("Oda checkout payment request log changed")
    matches = []
    for request in value["requests"]:
        if not isinstance(request, Mapping):
            raise HouseholdError("Oda checkout payment request log changed")
        parsed = urlsplit(str(request.get("url") or ""))
        status = request.get("status")
        request_id = request.get("requestId")
        if (
            str(request.get("method") or "").upper() == "POST"
            and not isinstance(status, bool)
            and isinstance(status, int)
            and 200 <= status < 300
            and parsed.scheme == "https"
            and parsed.netloc == "oda.com"
            and re.fullmatch(r"/(?:no/)?api/v1/checkout/pay/", parsed.path) is not None
            and not parsed.query
            and not parsed.fragment
            and isinstance(request_id, str)
            and request_id
        ):
            matches.append(request_id)
    if len(matches) != 1:
        raise HouseholdError("Oda checkout payment response is missing or ambiguous; do not send payment")
    return matches[0]


def oda_checkout_pay_order_id(value: Any, gateway_url: str) -> str:
    """Bind a hosted payment redirect to Oda's exact checkout response order."""

    if not isinstance(value, Mapping) or not isinstance(value.get("responseBody"), str):
        raise HouseholdError("Oda checkout payment response changed; do not send payment")
    try:
        payload = json.loads(value["responseBody"])
    except json.JSONDecodeError as exc:
        raise HouseholdError("Oda checkout payment response changed; do not send payment") from exc
    params = payload.get("params") if isinstance(payload, Mapping) else None
    order_id = params.get("orderNumber") if isinstance(params, Mapping) else None
    if (
        not isinstance(payload, Mapping)
        or payload.get("type") != "payments-providers-vipps"
        or payload.get("url") != gateway_url
        or not isinstance(order_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", order_id) is None
    ):
        raise HouseholdError("Oda checkout payment response changed; do not send payment")
    return order_id


def oda_checkout_amount_minor(label: Any, amount_text: Any, *, provider: str = "oda") -> int:
    """Parse one observed retailer checkout label/value row in minor units."""

    labels = _checkout_amount_labels(provider)
    product_pattern = MATHEM_CHECKOUT_PRODUCT_LABEL if provider == "mathem" else ODA_CHECKOUT_PRODUCT_LABEL
    product_label = isinstance(label, str) and product_pattern.fullmatch(label) is not None
    if (label not in labels.values() and not product_label) or not isinstance(amount_text, str):
        raise HouseholdError("Oda checkout amount row changed")
    normalized = " ".join(unicodedata.normalize("NFC", amount_text).split())
    match = re.fullmatch(
        rf"([−-])?(\d+(?:[ .]\d{{3}})*),(\d{{2}}) (?:kr|{'SEK' if provider == 'mathem' else 'NOK'})",
        normalized,
        re.IGNORECASE,
    )
    if match is None:
        raise HouseholdError("Oda checkout amount row changed")
    minor = int(match[2].replace(" ", "").replace(".", "")) * 100 + int(match[3])
    signed = -minor if match[1] else minor
    discount = label in {labels.get("discounts"), labels.get("delivery_discount")}
    if (discount and signed > 0) or (not discount and signed < 0):
        raise HouseholdError("Oda checkout amount row changed")
    return signed


def _oda_checkout_amounts_minor(value: Any, *, provider: str = "oda") -> dict[str, Any]:
    labels = _checkout_amount_labels(provider)
    if not isinstance(value, Mapping) or set(value) != set(ODA_CHECKOUT_AMOUNT_KEYS):
        raise HouseholdError("Oda checkout amounts changed")
    result: dict[str, Any] = {}
    for key in ODA_CHECKOUT_AMOUNT_KEYS:
        amount = value.get(key)
        if key == "other_fees":
            if amount is None:
                result[key] = None
                continue
            expected_name = labels["other_fee"]
            if not isinstance(amount, Mapping) or set(amount) != {expected_name}:
                raise HouseholdError("Oda checkout amounts changed")
            amount = amount[expected_name]
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                raise HouseholdError("Oda checkout amounts changed")
            minor = round(float(amount) * 100)
            if not math.isfinite(float(amount)) or not math.isclose(float(amount) * 100, minor, abs_tol=1e-7) or minor < 0:
                raise HouseholdError("Oda checkout amounts changed")
            result[key] = {expected_name: minor}
            continue
        if amount is None:
            result[key] = None
            continue
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise HouseholdError("Oda checkout amounts changed")
        minor = round(float(amount) * 100)
        if (
            not math.isfinite(float(amount))
            or not math.isclose(float(amount) * 100, minor, abs_tol=1e-7)
            or (key == "discounts" and minor > 0)
            or (key != "discounts" and minor < 0)
        ):
            raise HouseholdError("Oda checkout amounts changed")
        result[key] = minor
    return result


def _oda_checkout_amount_script(
    expected_total: int,
    *,
    expected_product_count: int,
    expected_amounts: Mapping[str, Any] | None = None,
    expected_url: str | None = None,
    provider: str = "oda",
    vipps: bool = False,
    retry: bool = False,
    addition_retry: bool = False,
) -> str:
    """Build the shared read/final-click parser from observed retailer rows."""

    labels = _checkout_amount_labels(provider)
    click_mode = expected_amounts is not None or expected_url is not None
    if click_mode and (expected_amounts is None or expected_url is None):
        raise HouseholdError("Oda final checkout amount binding is incomplete")
    if isinstance(expected_product_count, bool) or not isinstance(expected_product_count, int) or not 0 < expected_product_count <= 1_000_000:
        raise HouseholdError("Oda checkout product count is invalid")
    script = r"""
(() => {
 const amountLabels=AMOUNT_LABELS;
 const productLabel=PRODUCT_LABEL;
 const knownLabels=[productLabel,...Object.values(amountLabels)];
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const parseAmount=value=>{const match=norm(value).match(/^([−-])?(\d+(?:[ .]\d{3})*),(\d{2}) (?:kr|CURRENCY_CODE)$/i);if(!match)return null;const minor=Number(match[2].replace(/[ .]/g,''))*100+Number(match[3]);return match[1]?-minor:minor;};
 const labelNodes=label=>[...document.querySelectorAll('*')].filter(visible).filter(node=>norm(node.innerText||'')===label).filter(node=>![...node.children].some(child=>visible(child)&&norm(child.innerText||'')===label));
 const rowState=label=>{
   if(!label)return {state:'absent',value:null,root:null};
   const labels=labelNodes(label);
   const candidates=[...document.querySelectorAll('div')].filter(visible).filter(root=>{
     const lines=(root.innerText||'').split(/\n+/).map(norm).filter(Boolean);
     if(lines.length!==2||lines[0]!==label||parseAmount(lines[1])===null)return false;
     const own=norm(root.innerText||'');
     return ![...root.children].some(child=>visible(child)&&norm(child.innerText||'')===own);
   });
   if(labels.length===0&&candidates.length===0)return {state:'absent',value:null,root:null};
   if(labels.length!==1||candidates.length!==1||!candidates[0].contains(labels[0]))return {state:'invalid',value:null,root:null};
   const lines=(candidates[0].innerText||'').split(/\n+/).map(norm).filter(Boolean);
   return {state:'value',value:parseAmount(lines[1]),root:candidates[0]};
 };
 const states={
   product_subtotal:rowState(productLabel),
   delivery_price:rowState(amountLabels.delivery_price),
   discounts:rowState(amountLabels.discounts),
   delivery_discount:rowState(amountLabels.delivery_discount),
   discounted_subtotal:rowState(amountLabels.discounted_subtotal),
   bags:rowState(amountLabels.bags),
   other_fee:rowState(amountLabels.other_fee),
   provider_total:rowState(amountLabels.provider_total),
 };
 // The current Mathem new-order overview can omit a free delivery row.
 // Retain that absence; the complete displayed arithmetic must still match.
 const required=[states.product_subtotal,states.provider_total,...(RETRY?[]:[states.discounted_subtotal])];
 let summaryRoot=null;
 if(required.every(row=>row.state==='value')){
   summaryRoot=required[0].root;
   while(summaryRoot&&!required.every(row=>summaryRoot.contains(row.root)))summaryRoot=summaryRoot.parentElement;
 }
 const knownRoots=Object.values(states).filter(row=>row.root).map(row=>row.root);
 const contained=Boolean(summaryRoot)&&Object.values(states).every(row=>row.state!=='value'||summaryRoot.contains(row.root));
 const currencyLine=line=>/(?:^|\s)(?:kr|CURRENCY_CODE)$/i.test(line);
 const unknownRows=summaryRoot?[...summaryRoot.querySelectorAll('*')].filter(visible).filter(root=>{
   if(knownRoots.some(known=>known.contains(root)))return false;
   const lines=(root.innerText||'').split(/\n+/).map(norm).filter(Boolean);
   if(!lines.some(currencyLine)||knownLabels.includes(lines[0]))return false;
   return ![...root.children].some(child=>visible(child)&&(child.innerText||'').split(/\n+/).map(norm).filter(Boolean).some(currencyLine));
 }):['missing-summary-root'];
 const amounts={
   product_subtotal:states.product_subtotal.value,
   delivery_price:states.delivery_price.value,
   discounts:states.discounts.state==='absent'&&states.delivery_discount.state==='absent'?null:(states.discounts.value||0)+(states.delivery_discount.value||0),
   deposits:null,
   bags:states.bags.value,
   other_fees:states.other_fee.state==='absent'?null:{[amountLabels.other_fee]:states.other_fee.value},
   provider_total:states.provider_total.value,
   ...(MATHEM_BREAKDOWN?{discount_breakdown:{product_discount:states.discounts.value,delivery_discount:states.delivery_discount.value}}:{}),
 };
 const optionalValid=[states.delivery_price,states.discounts,states.delivery_discount,states.bags,states.other_fee].every(row=>row.state!=='invalid');
 const signsValid=required.every(row=>row.value>=0)&&[states.delivery_price,states.bags,states.other_fee].every(row=>row.state!=='value'||row.value>=0)&&[states.discounts,states.delivery_discount].every(row=>row.state!=='value'||row.value<=0);
 const deliveryDiscountValid=states.delivery_discount.state==='absent'||-states.delivery_discount.value===states.delivery_price.value;
 const discountedValid=(RETRY&&states.discounted_subtotal.state==='absent')||states.discounted_subtotal.value===states.product_subtotal.value+(states.discounts.value||0);
 // On addition retry the overview gross total and the payment calculation are
 // separate merchant values. The final button is the calculated amount due.
 const totalValid=ADDITION_RETRY
   ? amounts.product_subtotal===TOTAL&&[states.delivery_price,states.discounts,states.delivery_discount,states.bags,states.other_fee].every(row=>row.state==='absent')
   : amounts.provider_total===amounts.product_subtotal+(amounts.discounts||0)+(amounts.delivery_price||0)+(amounts.bags||0)+(states.other_fee.value||0);
 const amountsValid=required.every(row=>row.state==='value')&&optionalValid&&signsValid&&deliveryDiscountValid&&contained&&discountedValid&&totalValid&&unknownRows.length===0&&(ADDITION_RETRY||amounts.provider_total===TOTAL);
 if(!CLICK_MODE&&!VERIFY_READ_PAYMENT)return JSON.stringify({amounts,amounts_valid:amountsValid});
 const expectedAmounts=EXPECTED_AMOUNTS;
 const money=value=>[...norm(value).matchAll(/\b(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|CURRENCY_CODE)\b/gi)].map(match=>Number(match[1].replace(/[ .]/g,''))*100+Number(match[2]));
 const labels=[...document.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>/^(FINAL_CONTROL)\s+\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|CURRENCY_CODE)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||''))).filter(x=>{const values=money(x.innerText||x.getAttribute('aria-label')||'');return values.length===1&&values[0]===TOTAL;});
 // Mathem's default cart overview and explicit new-order payment calculation
 // are separate sources. Do not offer a confirmation when they disagree.
 if(!CLICK_MODE)return JSON.stringify({amounts,amounts_valid:amountsValid&&labels.length===1});
 const canonical=v=>v&&typeof v==='object'?(Array.isArray(v)?v.map(canonical):Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])]))):v;
 const ready=location.href===EXPECTED_URL&&labels.length===1&&amountsValid&&JSON.stringify(canonical(amounts))===JSON.stringify(canonical(expectedAmounts));
 if(!ready)return JSON.stringify({clicked:false});
 labels[0].click();return JSON.stringify({clicked:true});
})()
"""
    return (
        script.replace(
            "AMOUNT_LABELS",
            json.dumps(labels, ensure_ascii=False, separators=(",", ":")),
        )
        .replace("TOTAL", json.dumps(expected_total))
        .replace("PRODUCT_LABEL",
            json.dumps("1 vara" if expected_product_count == 1 else f"{expected_product_count} varor")
            if provider == "mathem" else json.dumps("1 vare" if expected_product_count == 1 else f"{expected_product_count} varer")
        )
        .replace("CURRENCY_CODE", "SEK" if provider == "mathem" else "NOK")
        .replace("FINAL_CONTROL", "Bekräfta och betala" if provider == "mathem" else "Betal med" if vipps else "Bekreft og betal|Confirm and pay")
        .replace("CLICK_MODE", "true" if click_mode else "false")
        .replace("VERIFY_READ_PAYMENT", "true" if provider == "mathem" and not retry else "false")
        .replace("ADDITION_RETRY", "true" if addition_retry else "false")
        .replace("RETRY", "true" if retry else "false")
        .replace("MATHEM_BREAKDOWN", "true" if provider == "mathem" else "false")
        .replace(
            "EXPECTED_AMOUNTS",
            json.dumps(expected_amounts, ensure_ascii=False, separators=(",", ":")),
        )
        .replace("EXPECTED_URL", json.dumps(expected_url))
    )



def require_order_binding(value: Any) -> Mapping[str, Any]:
    # Persisted pre-upgrade operations may have no original account reference.
    # Never replace that missing evidence with today's selected address.
    if (not isinstance(value, Mapping)
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get("account_reference_digest") or "")) is None
            or not isinstance(value.get("receipt_address"), str) or not value["receipt_address"].strip()):
        raise HouseholdError("Original order account binding is unavailable; preserve any dispatched attempt and prepare a new review only before dispatch")
    return value


def _retail_store_url(provider: str) -> str:
    if provider not in {"oda", "mathem"}:
        raise HouseholdError("unsupported browser provider")
    return "https://oda.com/no/" if provider == "oda" else "https://www.mathem.se/se/"


def _checkout_account_script(address_id: Any, *, provider: str) -> str:
    """Bind the browser account to a selected MCP address without returning its ID.

    The authenticated account page exposes duplicate edit links for one address;
    matching the visible unique address reference is sufficient. Comparing only
    the human-readable address would accept another account at the same address.
    """
    if type(address_id) is not int or not 0 < address_id < 2**53:
        raise HouseholdError("Retail selected account address is unavailable")
    store_url = _retail_store_url(provider)
    origin = store_url.rsplit("/", 2)[0]
    script = r"""
(() => {
 const origin=ORIGIN;
 if(location.href!==ACCOUNT_URL||document.querySelector('input[type="password"]'))return JSON.stringify({account_matches:false});
 const visible=e=>{const style=getComputedStyle(e),r=e.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const expected=EDIT_PATH;
 const matches=[...document.querySelectorAll('a[href]')].filter(visible).filter(e=>{
  const url=new URL(e.href);
  return url.origin===origin&&url.pathname===expected&&!url.search&&!url.hash;
 });
 return JSON.stringify({account_matches:matches.length>0});
})()
"""
    values = {"ORIGIN": json.dumps(origin), "ACCOUNT_URL": json.dumps(store_url + "account/delivery/"),
              "EDIT_PATH": json.dumps("/" + store_url.rstrip("/").rsplit("/", 1)[1] + "/account/delivery/edit/" + str(address_id) + "/")}
    return re.sub(r"\b(?:ORIGIN|ACCOUNT_URL|EDIT_PATH)\b", lambda match: values[match[0]], script)


def _mathem_checkout_account_script(address_id: Any) -> str:
    return _checkout_account_script(address_id, provider="mathem")


def _mathem_checkout_payment_script(expected_url: str = "https://www.mathem.se/se/checkout/confirm/") -> str:
    """Read the selected saved-card identity from its own visible radio labels."""
    return r"""
(() => {
 if(location.href!==EXPECTED_URL)return JSON.stringify({verified:false});
 const visible=e=>{const style=getComputedStyle(e),r=e.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const selected=[...document.querySelectorAll('input[type="radio"]')].filter(e=>e.checked&&!e.disabled);
 if(selected.length!==1)return JSON.stringify({verified:false});
 const radio=selected[0];
 const labels=[...radio.labels].filter(visible).filter(label=>label.contains(radio)&&label.querySelectorAll('input[type="radio"]').length===1);
 const texts=labels.map(label=>label.innerText||'');
 const cards=[...new Set(texts.flatMap(text=>[...text.matchAll(/(?:[*•·xX]{2,}\s*|slutar på\s*)(\d{4})\b/gi)].map(match=>match[1])))];
 if(cards.length!==1||texts.some(text=>/(?:\d[ -]?){12,19}/.test(text)))return JSON.stringify({verified:false});
 return JSON.stringify({verified:true,payment_kind:'saved_card',payment_display:'•••• '+cards[0]});
})()
""".replace("EXPECTED_URL", json.dumps(expected_url))


def _retail_addition_amount_script(expected: Mapping[str, Any], *, submit: bool = False, provider: str = "mathem") -> str:
    """Observed Oda/Mathem addition overview; charge only the reviewed delta.

    These four rows report the original order, added goods, amount due now and
    combined order. They do not supply the new-order fee/discount breakdown.
    """
    return r"""
(() => {
 const expected=EXPECTED;
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const failed=()=>JSON.stringify(SUBMIT?{clicked:false}:{amounts_valid:false});
 if(location.href!==expected.checkout_url||document.querySelector('input[type="password"]'))return failed();
 const money='([−-]?)(\\d+(?:[ .]\\d{3})*),(\\d{2})\\s*(?:kr|SEK)';
 const values={},nodes=[...document.querySelectorAll('*')].filter(visible);
 const specs=[['original','Ursprunglig beställning',true],['added','Varor tillagda i efterhand',true],['payable','Att betala nu',false],['combined','Totalsumma för beställning',true]].filter(([key])=>!expected.delivery_change||key!=='added');
 if(expected.delivery_change&&(expected.product_count!==0||nodes.some(e=>norm(e.innerText)==='Varor tillagda i efterhand')))return failed();
 for(const [key,label,counted] of specs){
  const labels=nodes.filter(e=>norm(e.innerText)===label).filter(e=>![...e.children].some(c=>visible(c)&&norm(c.innerText)===label));
  if(labels.length!==1)return failed();
  const pattern=new RegExp('^'+label+' '+(counted?'([1-9]\\d*) (vara|varor) ':'')+money+'$','i');
  let row=labels[0].parentElement,match=null;
  while(row&&row!==document.body){match=norm(row.innerText).match(pattern);if(match)break;row=row.parentElement;}
  if(!match)return failed();
  const offset=counted?3:1,unsigned=Number(match[offset+1].replace(/[ .]/g,''))*100+Number(match[offset+2]);
  const minor=match[offset]?-unsigned:unsigned;
  if(!Number.isSafeInteger(minor)||(minor<0&&(!expected.delivery_change||key!=='payable')))return failed();
  values[key+'_minor']=minor;
  if(counted){const count=Number(match[1]);if(!Number.isSafeInteger(count)||count>1000000||(count===1)!==(match[2]==='vara'))return failed();values[key+'_count']=count;}
 }
 // Delivery changes read the merchant's full new total independently of the
 // signed amount displayed as payable now. A negative adjustment does not
 // establish a bank refund; it is preserved exactly through the final click.
 const wanted=expected.delivery_change
  ? {original_minor:expected.original_minor,original_count:expected.original_count,combined_count:expected.original_count,...(expected.order_amounts||{})}
  : {original_minor:expected.original_minor,original_count:expected.original_count,added_minor:expected.total_minor,added_count:expected.product_count,payable_minor:expected.total_minor,combined_minor:expected.original_minor+expected.total_minor,combined_count:expected.original_count+expected.product_count};
 if(SUBMIT&&expected.delivery_change&&!expected.order_amounts)return failed();
 if(Object.keys(wanted).some(key=>values[key]!==wanted[key]))return failed();
 const controls=[...document.querySelectorAll('button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true').filter(e=>/^Bekräfta och betala\s/.test(norm(e.innerText||e.getAttribute('aria-label')||'')));
 if(controls.length!==1)return failed();
 const pay=norm(controls[0].innerText||controls[0].getAttribute('aria-label')||'').match(new RegExp('^Bekräfta och betala '+money+'$','i'));
 if(!pay||(pay[1]?-1:1)*(Number(pay[2].replace(/[ .]/g,''))*100+Number(pay[3]))!==values.payable_minor)return failed();
 if(SUBMIT){controls[0].click();return JSON.stringify({clicked:true});}
 return JSON.stringify({amounts_valid:true,order_amounts:values});
})()
""".replace("SUBMIT", "true" if submit else "false").replace(
        "Ursprunglig beställning", "Opprinnelig bestilling" if provider == "oda" else "Ursprunglig beställning",
    ).replace("Varor tillagda i efterhand", "Nye varer lagt til" if provider == "oda" else "Varor tillagda i efterhand").replace(
        "Att betala nu", "Å betale" if provider == "oda" else "Att betala nu",
    ).replace("Totalsumma för beställning", "Ny totalsum" if provider == "oda" else "Totalsumma för beställning").replace(
        "Bekräfta och betala", "Bekreft og betal" if provider == "oda" else "Bekräfta och betala",
    ).replace("SEK", "NOK" if provider == "oda" else "SEK").replace(
        "(vara|varor)", "(vare|varer)" if provider == "oda" else "(vara|varor)",
    ).replace("==='vara'", "==='vare'" if provider == "oda" else "==='vara'").replace("EXPECTED", json.dumps(dict(expected), ensure_ascii=False))

def _receipt_address_script(order_id: str, address: str, *, provider: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", order_id) is None or not address.strip():
        raise HouseholdError("Retail receipt identity is unavailable")
    url = _retail_store_url(provider) + "account/orders/" + quote(order_id, safe="") + "/"
    script = r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 if(location.href!==URL||document.querySelector('input[type="password"]'))return JSON.stringify({address_verified:false});
 const mains=[...document.querySelectorAll('main')].filter(visible).filter(e=>!e.closest('[role="dialog"]'));
 if(mains.length!==1)return JSON.stringify({address_verified:false});
 const main=mains[0],text=norm(main.innerText);
 const escaped=ORDER.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
 const orderMatches=new RegExp(`(?:^|[^A-Za-z0-9._:-])${escaped}(?=$|[^A-Za-z0-9._:-])`).test(text);
 const addressNodes=[...main.querySelectorAll('p')].filter(visible).filter(e=>!e.closest('li,article,[role="dialog"]')&&norm(e.innerText)===norm(ADDRESS));
 const totalNodes=[...main.querySelectorAll('*')].filter(visible).filter(e=>norm(e.innerText)===TOTAL_LABEL).filter(e=>![...e.children].some(c=>visible(c)&&norm(c.innerText)===TOTAL_LABEL));
 return JSON.stringify({address_verified:orderMatches&&addressNodes.length===1&&totalNodes.length===1});
})()
"""
    label = (ODA_CHECKOUT_AMOUNT_LABELS if provider == "oda" else MATHEM_CHECKOUT_AMOUNT_LABELS)["provider_total"]
    values = {"TOTAL_LABEL": json.dumps(label), "URL": json.dumps(url), "ORDER": json.dumps(order_id), "ADDRESS": json.dumps(address, ensure_ascii=False)}
    return re.sub(r"\b(?:URL|ORDER|ADDRESS|TOTAL_LABEL)\b", lambda match: values[match[0]], script)


def _mathem_receipt_address_script(order_id: str, address: str) -> str:
    return _receipt_address_script(order_id, address, provider="mathem")


def _oda_order_payment_state_script(order_id: str) -> str:
    """Read an exact Oda same-order retry offer without returning page text."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", order_id) is None:
        raise HouseholdError("invalid order identity")
    order_url = f"https://oda.com/no/account/orders/{order_id}/"
    receipt_path = f"/api/v1/orders/{order_id}/receipt"
    retry_path = "/no/checkout/retry/"
    return r"""
(() => {
 if(location.href!==ORDER_URL||document.querySelector('input[type="password"]'))return JSON.stringify({status:'unknown'});
 const norm=value=>(value||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const style=getComputedStyle(e),box=e.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0;};
 const headings=[...document.querySelectorAll('h1')].filter(visible).map(e=>norm(e.innerText)).filter(text=>text==='Betaling påbegynt');
 const receipts=[...document.querySelectorAll('a[href]')].filter(visible).filter(e=>{
  const url=new URL(e.href,location.href);
  return url.origin===location.origin&&url.pathname===RECEIPT_PATH&&!url.search&&!url.hash;
 });
 const retryCandidates=[...document.querySelectorAll('a[href]')].filter(visible).filter(e=>{
  const url=new URL(e.href,location.href);
  return norm(e.innerText||e.getAttribute('aria-label')||'')==='Betal'||
   url.origin===location.origin&&url.pathname===RETRY_PATH;
 });
 const retries=retryCandidates.filter(e=>{
  const url=new URL(e.href,location.href);
  return norm(e.innerText||e.getAttribute('aria-label')||'')==='Betal'&&
   url.origin===location.origin&&url.pathname===RETRY_PATH&&!url.hash&&
   [...url.searchParams.keys()].length===1&&url.searchParams.get('orderNumber')===ORDER_ID;
 });
 const paymentStarted=headings.length===1&&receipts.length===1;
 const retryable=paymentStarted&&retryCandidates.length===1&&retries.length===1;
 return JSON.stringify({status:retryable?'retry_available':paymentStarted&&retryCandidates.length===0?'payment_started':'unknown'});
})()
""".replace("ORDER_URL", json.dumps(order_url)).replace("RECEIPT_PATH", json.dumps(receipt_path)).replace(
        "RETRY_PATH", json.dumps(retry_path),
    ).replace("ORDER_ID", json.dumps(order_id))


def identity_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", value).lower()))


def product_identity(name: str, description: str, brand: str) -> str:
    name_tokens = list(identity_tokens(name))
    description_tokens = list(identity_tokens(description))
    brand_tokens = list(identity_tokens(brand))
    if brand_tokens and name_tokens[:len(brand_tokens)] == brand_tokens:
        name_tokens = name_tokens[len(brand_tokens):]
    for length in range(min(len(name_tokens) - 1, len(description_tokens)), 0, -1):
        suffix = name_tokens[-length:]
        repeated_at_start = description_tokens[:length] == suffix
        repeated_later = not any(token.isdigit() for token in suffix) and any(description_tokens[start:start + length] == suffix for start in range(1, len(description_tokens) - length + 1))
        if repeated_at_start or repeated_later:
            name_tokens = name_tokens[:-length]
            break
    return " ".join(name_tokens + description_tokens + brand_tokens)


def checkout_identity_tokens(value: str) -> tuple[str, ...]:
    tokens = identity_tokens(value)
    for length in range((len(tokens) - 1) // 2, 0, -1):
        if tokens[:length] == tokens[-length:]:
            return tokens[length:]
    return tokens


def checkout_lines_match(expected: list[Mapping[str, Any]], actual: Any) -> bool:
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    candidates = []
    for line in expected:
        wanted = checkout_identity_tokens(str(line.get("identity") or ""))
        matches = []
        for index, item in enumerate(actual):
            if not isinstance(item, Mapping) or isinstance(item.get("quantity"), bool) or not isinstance(item.get("quantity"), (int, float)):
                continue
            if not isinstance(item.get("text"), str):
                continue
            if item.get("quantity") != line.get("quantity"):
                continue
            if wanted == checkout_identity_tokens(item["text"]):
                matches.append(index)
        candidates.append(matches)

    def assign(line: int, used: set[int]) -> bool:
        return line == len(candidates) or any(index not in used and assign(line + 1, used | {index}) for index in candidates[line])

    return assign(0, set())


def delivery_signature(value: str, *, provider: str = "oda") -> tuple[int, int, int, int, int, str] | None:
    normalized = " ".join(unicodedata.normalize("NFC", value).lower().split())
    if provider not in {"oda", "mathem"}:
        return None
    conjunction = "och|till" if provider == "mathem" else "og|til"
    month_pattern = (
        r"jan(?:uari)?|feb(?:ruari)?|mar(?:s)?|apr(?:il)?|maj|jun(?:i)?|jul(?:i)?|aug(?:usti)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|dec(?:ember)?"
        if provider == "mathem" else
        r"jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?"
    )
    hours = re.findall(rf"\b(\d{{1,2}})(?::(\d{{2}}))?\s*(?:-|–|{conjunction})\s*(\d{{1,2}})(?::(\d{{2}}))?(?![:.]\d)\b", normalized)
    dates = re.findall(rf"\b(\d{{1,2}})\.?\s*({month_pattern})\b", normalized)
    if len(hours) != 1 or len(dates) != 1:
        return None
    start, start_minute, end, end_minute = hours[0]
    day, month = dates[0]
    start_hour, start_minute, end_hour, end_minute, day = int(start), int(start_minute or 0), int(end), int(end_minute or 0), int(day)
    month = month[:3]
    months = ("jan", "feb", "mar", "apr", "maj" if provider == "mathem" else "mai", "jun", "jul", "aug", "sep", "okt", "nov", "dec" if provider == "mathem" else "des")
    month_number = months.index(month) + 1
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        return None
    if (start_hour, start_minute) >= (end_hour, end_minute) or not 1 <= day <= calendar.monthrange(2024, month_number)[1]:
        return None
    return start_hour, start_minute, end_hour, end_minute, day, month


def _relative_delivery_text(value: str, *, provider: str, today: date) -> str | None:
    normalized = " ".join(unicodedata.normalize("NFC", value).lower().split())
    relative_pattern = r"\bi\s?(?:dag|morgon)\b" if provider == "mathem" else r"\bi (?:dag|morgen)\b"
    relative = re.findall(relative_pattern, normalized)
    if len(relative) != 1 or delivery_signature(normalized, provider=provider) is not None:
        return None
    day = today + timedelta(days=0 if relative[0].replace(" ", "") == "idag" else 1)
    months = (
        "jan", "feb", "mar", "apr", "maj" if provider == "mathem" else "mai", "jun",
        "jul", "aug", "sep", "okt", "nov", "dec" if provider == "mathem" else "des",
    )
    return re.sub(relative_pattern, f"{day.day} {months[day.month - 1]}", normalized)


def checkout_delivery_matches(expected: str, roots: Any, *, provider: str = "oda", today: date | None = None) -> bool:
    if not expected:
        return True
    zone = ZoneInfo("Europe/Stockholm" if provider == "mathem" else "Europe/Oslo")
    local_today = today or datetime.now(zone).date()
    relative_used = False
    expected_normalized = " ".join(unicodedata.normalize("NFC", expected).lower().split())
    expected_pattern = r"\bi\s?(?:dag|morgon)\b" if provider == "mathem" else r"\bi (?:dag|morgen)\b"
    if re.findall(expected_pattern, expected_normalized):
        relative_used = True
        expected = _relative_delivery_text(expected, provider=provider, today=local_today)
        if expected is None:
            return False
    signature = delivery_signature(expected, provider=provider)
    if signature is None or not isinstance(roots, list) or len(roots) != 1 or not isinstance(roots[0], str):
        return False
    observed = " ".join(unicodedata.normalize("NFC", roots[0]).lower().split())
    relative_pattern = expected_pattern
    if re.findall(relative_pattern, observed):
        relative_used = True
        # Retail checkout switches to relative dates at local midnight. Never
        # let a relative label override a numeric date or a second date label.
        observed = _relative_delivery_text(roots[0], provider=provider, today=local_today)
        if observed is None:
            return False
    if relative_used and today is None and datetime.now(zone).date() != local_today:
        return False
    return delivery_signature(observed, provider=provider) == signature


def cancellation_delivery_matches(expected: str, lines: Any, *, provider: str = "oda") -> bool:
    # Cancellation always requires a bound delivery; checkout also permits an
    # absent expectation for other flows. Share its provider-local date reader.
    return bool(expected) and checkout_delivery_matches(expected, lines, provider=provider)


def cancellation_total_matches(expected_minor: int, rows: Any, *, provider: str = "oda") -> bool:
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], str):
        return False
    normalized = " ".join(unicodedata.normalize("NFC", rows[0]).split())
    if provider not in {"oda", "mathem"}:
        return False
    currency, other_currency = ("SEK", "NOK") if provider == "mathem" else ("NOK", "SEK")
    if re.search(rf"\b{other_currency}\b", normalized, re.IGNORECASE):
        return False
    patterns = (
        rf"\b(\d+(?:[ .]\d{{3}})*),(\d{{2}})\s*(?:kr|{currency})\b",
        rf"\b(?:kr|{currency})[,\s]*(\d+(?:[ .]\d{{3}})*),(\d{{2}})\b",
    )
    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, normalized, re.IGNORECASE):
            values.append(int(match.group(1).replace(" ", "").replace(".", "")) * 100 + int(match.group(2)))
    return values == [expected_minor]


def clear_cancellation_cache(profile: Path | str) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise HouseholdError("Oda browser cache cannot be reset safely")
    for relative in ("Default/Cache", "Default/Code Cache", "Default/Service Worker"):
        parent, name = relative.split("/", 1)
        profile_fd = None
        parent_fd = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            profile_fd = os.open(Path(profile), flags)
            parent_fd = os.open(parent, flags, dir_fd=profile_fd)
            shutil.rmtree(name, dir_fd=parent_fd)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HouseholdError("Oda browser cache cannot be reset") from exc
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
            if profile_fd is not None:
                os.close(profile_fd)


def _oda_delivery_change_surface_script(expected_url: str) -> str:
    return r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const controls=[...document.querySelectorAll('button,a,[role="radio"]')].filter(enabled);
 const final=controls.filter(x=>/^(Bekreft og betal|Confirm and pay)(\b|\s)/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(location.href!==URL||final.length!==1||document.querySelector('input[type="password"]'))return JSON.stringify({action:'wait'});
 {
   const money=[...norm(final[0].innerText||final[0].getAttribute('aria-label')||'').matchAll(/(?:^|\s)([−-]?)(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|NOK)\b/gi)].map(m=>(m[1]?-1:1)*(Number(m[2].replace(/[ .]/g,''))*100+Number(m[3])));
   const roots=[...document.querySelectorAll('h1,h2,h3,h4')].filter(visible).filter(x=>norm(x.innerText)==='Vi leverer varene dine').map(x=>x.closest('section,article,.k-card')).filter(Boolean).map(x=>norm(x.innerText));
   const payment=JSON.parse(PAYMENT);
   return JSON.stringify({action:'ready',amounts:money,delivery_roots:roots,payment_display:payment.verified===true?payment.payment_display:null,submit_controls:final.length});
 }
})()
""".replace("URL", json.dumps(expected_url)).replace("PAYMENT", _oda_checkout_payment_script().strip())


def _oda_checkout_payment_script(payment: Mapping[str, Any] | None = None, *, select: bool = False, expected_url: str | None = None) -> str:
    """Read the configured method, or select its unique existing radio during prepare."""
    return r"""
(() => {
 const preference=PREFERENCE;
 URL_CHECK
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const style=getComputedStyle(e),r=e.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const radios=[...document.querySelectorAll('input[type="radio"]')];
 const observed=radios.some(visible),selected=radios.filter(e=>e.checked);
 const failure={verified:false,observed};
 const choices=radios.filter(r=>visible(r)&&!r.disabled&&r.getAttribute('aria-disabled')!=='true').map(radio=>{
   const labels=[...radio.labels].filter(visible).filter(label=>label.contains(radio)&&label.querySelectorAll('input[type="radio"]').length===1);
   const texts=labels.map(label=>norm(label.innerText||''));
   if(texts.some(text=>/(?:\d[ -]?){12,19}/.test(text)))return null;
   if(texts.some(text=>/nytt kort|new card|legg til(?: et)?(?: nytt)? kort|add(?: a)?(?: new)? card/i.test(text)))return null;
   const cards=[...new Set(texts.flatMap(text=>[...text.matchAll(/(?:[*•·xX]{2,}\s*|slutter på\s*|slutar på\s*|ending in\s*)(\d{4})\b/gi)].map(match=>match[1])))];
   const vipps=texts.some(text=>/^Vipps$/i.test(text));
   if(cards.length===1&&!vipps)return {radio,method:'saved_card',last4:cards[0],display:'•••• '+cards[0]};
   if(cards.length===0&&vipps)return {radio,method:'vipps',last4:null,display:'Vipps'};
   return null;
 }).filter(Boolean);
 const matching=choices.filter(c=>c.method===preference.method&&(!preference.card_last4||c.last4===preference.card_last4));
 if((preference.card_last4||preference.method==='vipps')&&matching.length!==1)return JSON.stringify(failure);
 const current=selected.length===1?matching.find(c=>c.radio===selected[0]):null;
 if(current){if(matching.filter(c=>c.display===current.display).length!==1)return JSON.stringify(failure);return JSON.stringify({verified:true,observed,payment_display:current.display});}
 SELECTION_ACTION
})()
""".replace("PREFERENCE", json.dumps(payment or {"method": "saved_card", "card_last4": None})).replace(
        "URL_CHECK", "if(location.href!==" + json.dumps(expected_url) + ")return JSON.stringify({verified:false,observed:true});" if select else "",
    ).replace("SELECTION_ACTION", "if(selected.length>1||matching.length!==1)return JSON.stringify(failure);matching[0].radio.click();return JSON.stringify({verified:false,observed,selected:true});" if select else "return JSON.stringify(failure);")


def _oda_vipps_gateway_script(
    expected_total: int,
    expected_phone: str,
    *,
    expected_url: str | None = None,
    require_hit: bool = False,
    hit_x: float = 0,
    hit_y: float = 0,
) -> str:
    """Verify the exact hosted Vipps request page without returning phone data."""

    return r"""
(() => {
 const norm=value=>(value||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false;}const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
 const represented=e=>{for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||(p!==e&&s.opacity==='0'))return false;}const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
 const enabled=e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true';
 document.querySelectorAll('[data-oda-household-vipps-next]').forEach(e=>e.removeAttribute('data-oda-household-vipps-next'));
 document.querySelectorAll('[data-oda-household-vipps-phone]').forEach(e=>e.removeAttribute('data-oda-household-vipps-phone'));
 const current=new URL(location.href),params=[...current.searchParams.entries()];
 const identity=current.protocol==='https:'&&current.host==='pay.vipps.no'&&!current.username&&!current.password&&!current.hash&&params.length===1&&params[0][0]==='token'&&params[0][1]!==''&&(!EXPECTED_URL||current.href===EXPECTED_URL);
 const roots=[...document.querySelectorAll('main,[role="main"]')].filter(visible);
 const root=roots.length===1?roots[0]:null;
 const text=norm(root?.innerText||'');
 const amounts=[...text.matchAll(/(?:\bNOK\s*(\d+(?:[ .]\d{3})*)[,.](\d{2})\b|\b(\d+(?:[ .]\d{3})*)[,.](\d{2})\s*(?:kr|NOK)\b)/gi)].map(m=>Number((m[1]||m[3]).replace(/[ .]/g,''))*100+Number(m[2]||m[4]));
 const merchant=/(?:^|\s)Oda(?:\s|$)/i.test(text);
 const amountBound=amounts.length>0&&amounts.every(value=>value===EXPECTED_TOTAL);
 const sent=identity&&merchant&&amountBound&&/We've sent a payment request to/i.test(text)&&/Open Vipps/i.test(text);
 const expired=identity&&merchant&&amountBound&&/(?:payment timed out|betalingen (?:har )?(?:utløpt|gått ut))/i.test(text);
 const phones=root?[...root.querySelectorAll('input[type="tel"][name="phone-number"]')].filter(visible):[];
 const national=phones.length===1?phones[0].value.replace(/\D/g,''):'';
 const remember=root?[...root.querySelectorAll('input[type="checkbox"]')].filter(represented):[];
 const buttons=root?[...root.querySelectorAll('button')].filter(enabled).filter(e=>norm(e.innerText||e.getAttribute('aria-label')||'')==='Next'):[];
 const fillable=identity&&!sent&&!expired&&/Continue to pay with Vipps/i.test(text)&&merchant&&amountBound&&phones.length===1&&!phones[0].disabled&&!phones[0].readOnly&&remember.length===1&&remember[0].checked===false&&buttons.length===1;
 const phoneMatches=fillable&&(national===EXPECTED_PHONE||national==='47'+EXPECTED_PHONE);
 const exact=fillable&&phoneMatches;
 if(fillable)phones[0].setAttribute('data-oda-household-vipps-phone','');
 const target=exact?buttons[0]:null;
 if(target)target.setAttribute('data-oda-household-vipps-next','');
 const hit=REQUIRE_HIT?document.elementFromPoint(HIT_X,HIT_Y):target;
 return JSON.stringify({identity,ready:Boolean(target&&hit&&(hit===target||target.contains(hit))),sent,expired,fillable,phone_matches:phoneMatches});
})()
""".replace("EXPECTED_TOTAL", str(expected_total)).replace("EXPECTED_PHONE", json.dumps(expected_phone)).replace(
        "EXPECTED_URL", json.dumps(expected_url),
    ).replace("REQUIRE_HIT", "true" if require_hit else "false").replace("HIT_X", json.dumps(hit_x)).replace("HIT_Y", json.dumps(hit_y))


def _oda_vipps_phone_fill_script(phone_number: str) -> str:
    """Fill the marked hosted field through stdin-backed evaluation, never argv."""

    return r"""
(()=>{
 const PHONE=EXPECTED_PHONE;
 const current=new URL(location.href),params=[...current.searchParams.entries()];
 const identity=current.protocol==='https:'&&current.host==='pay.vipps.no'&&!current.username&&!current.password&&!current.hash&&params.length===1&&params[0][0]==='token'&&params[0][1]!=='';
 const fields=[...document.querySelectorAll('[data-oda-household-vipps-phone]')];
 const field=identity&&fields.length===1?fields[0]:null;
 if(!field||field.disabled||field.readOnly)return JSON.stringify({filled:false});
 const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;
 if(!setter)return JSON.stringify({filled:false});
 setter.call(field,PHONE);
 field.dispatchEvent(new Event('input',{bubbles:true}));
 field.dispatchEvent(new Event('change',{bubbles:true}));
 return JSON.stringify({filled:true});
})()
""".replace("EXPECTED_PHONE", json.dumps(phone_number))


def _oda_checkout_surface_script(
    expected: Mapping[str, Any],
    payment: Mapping[str, Any] | None = None,
    *,
    provider: str = "oda",
    summary_product_count: int | None = None,
) -> str:
    if summary_product_count is not None and (
        isinstance(summary_product_count, bool)
        or not isinstance(summary_product_count, int)
        or not 0 < summary_product_count <= 1_000_000
    ):
        raise HouseholdError("Oda checkout summary product count is invalid")
    summary_label = None
    if summary_product_count is not None:
        summary_label = (
            "1 vara" if summary_product_count == 1 else f"{summary_product_count} varor"
        ) if provider == "mathem" else (
            "1 vare" if summary_product_count == 1 else f"{summary_product_count} varer"
        )
    return r"""
(() => {
 const expected=EXPECTED;
 const summaryLabel=SUMMARY_LABEL;
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const text=norm(document.body?.innerText||'');
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const labels=[...document.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>/^(FINAL_CONTROL)\s+\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|CURRENCY)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 const login=!!document.querySelector('form[action*="login"],input[type="password"]');
 const unavailable=/ikke tilgjengelig|utsolgt|unavailable/i.test(text);
 const itemInputs=[...document.querySelectorAll('input[type="number"]')].filter(visible).filter(input=>ITEM_MATCH);
 const items=itemInputs.map(input=>{const root=input.closest('li,article');return {quantity:Number(input.value),text:norm([...(root?.querySelectorAll('p')||[])].filter(visible).slice(0,2).map(x=>x.innerText).join(' '))};});
 const summaryCountNodes=summaryLabel===null?[]:[...document.querySelectorAll('*')].filter(visible).filter(node=>norm(node.innerText||'')===summaryLabel).filter(node=>![...node.children].some(child=>visible(child)&&norm(child.innerText||'')===summaryLabel));
 const summaryCountMatches=summaryLabel!==null&&itemInputs.length===0&&summaryCountNodes.length===1;
 const money=value=>[...norm(value).matchAll(/\b(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|CURRENCY)\b/gi)].map(match=>Number(match[1].replace(/[ .]/g,''))*100+Number(match[2]));
 const amounts=labels.length===1?money(labels[0].innerText||labels[0].getAttribute('aria-label')||''):[];
 const totalMatch=amounts.length===1&&amounts[0]===expected.total_minor;
 const deliveryRoots=[...document.querySelectorAll('h1,h2,h3,h4')].filter(visible).filter(x=>norm(x.innerText||'')===DELIVERY_HEADING).map(x=>x.closest('section,article,.k-card')).filter(Boolean);
 const escaped=norm(expected.delivery_address).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
 const addressMatch=deliveryRoots.length===1&&Boolean(expected.delivery_address)&&new RegExp(`(?:^|[\\s,:])${escaped}(?=$|[\\s,])`,'i').test(norm(deliveryRoots[0].innerText||''));
 const payment=JSON.parse(PAYMENT);
 const verifiedPayment=payment.verified===true;
 const paymentDisplay=verifiedPayment?payment.payment_display:null;
 return JSON.stringify({url:location.href,authenticated:!login,available:!unavailable,items,total_matches:totalMatch,delivery_roots:deliveryRoots.map(root=>norm(root.innerText||'')),address_matches:addressMatch,masked_payment:verifiedPayment,payment_display:paymentDisplay,submit_controls:labels.length,...(summaryLabel===null?{}:{summary_count_matches:summaryCountMatches})});
})()
""".replace("PAYMENT", _oda_checkout_payment_script(payment).strip()).replace("SUMMARY_LABEL", json.dumps(summary_label, ensure_ascii=False)).replace("CURRENCY", "SEK" if provider == "mathem" else "NOK").replace("DELIVERY_HEADING", json.dumps("Vi levererar din beställning" if provider == "mathem" else "Vi leverer varene dine")).replace("FINAL_CONTROL", "Bekräfta och betala" if provider == "mathem" else "Betal med" if payment and payment.get("method") == "vipps" else "Bekreft og betal|Confirm and pay").replace("ITEM_MATCH", "[...(input.labels||[])].some(label=>norm(label.textContent)==='Antal')" if provider == "mathem" else "/\\bAntall\\b/i.test(norm(input.closest('li,article')?.innerText||''))").replace("EXPECTED", json.dumps(expected, ensure_ascii=False, separators=(",", ":")))


_BANK_APP_CHOICE_SCRIPT = r"""
import {createInterface} from 'node:readline';
const input=createInterface({input:process.stdin})[Symbol.asyncIterator]();
const cfg=JSON.parse((await input.next()).value);
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
const require=value=>{if(!value)throw Error('Bank app choice unavailable')};
const ws=new WebSocket(cfg.endpoint);
await new Promise((ok,bad)=>{ws.addEventListener('open',ok,{once:true});ws.addEventListener('error',bad,{once:true})});
let id=0,parent,issuer;const pending=new Map();
ws.addEventListener('message',event=>{const m=JSON.parse(event.data),p=pending.get(m.id);if(!p)return;pending.delete(m.id);clearTimeout(p.timer);m.error?p.bad(Error('Browser request failed')):p.ok(m.result)});
const send=(method,params={},sessionId)=>new Promise((ok,bad)=>{const n=++id,timer=setTimeout(()=>{pending.delete(n);bad(Error('Browser timeout'))},5000);pending.set(n,{ok,bad,timer});ws.send(JSON.stringify({id:n,method,params,...(sessionId?{sessionId}:{})}))});
const evaluate=async(session,expression,returnByValue=true)=>{const v=await send('Runtime.evaluate',{expression,returnByValue},session);require(!v.exceptionDetails);return v.result};
const visible=`e=>{for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false}const r=e.getBoundingClientRect();return r.width>0&&r.height>0}`;
const frameExpression=`(()=>{if(location.href!==${JSON.stringify(cfg.url)})return null;const visible=${visible};const c=[...document.querySelectorAll('.adyen-checkout__threeds2__challenge')].filter(visible);const f=c.length===1?[...c[0].querySelectorAll('iframe[name="threeDSIframe"]')].filter(visible):[];return f.length===1?f[0]:null})()`;
try{
 const pages=(await send('Target.getTargets')).targetInfos.filter(t=>t.type==='page'&&t.url===cfg.url);require(pages.length===1);
 parent=(await send('Target.attachToTarget',{targetId:pages[0].targetId,flatten:true})).sessionId;
 const frameId=async()=>{const obj=await evaluate(parent,frameExpression,false);require(obj.objectId);const d=await send('DOM.describeNode',{objectId:obj.objectId},parent);require(d.node.frameId);return d.node.frameId};
 const frame=await frameId();
 const targets=(await send('Target.getTargets')).targetInfos.filter(t=>t.type==='iframe'&&t.targetId===frame);require(targets.length===1);
 const issuerUrl=targets[0].url;require(new URL(issuerUrl).protocol==='https:');
 issuer=(await send('Target.attachToTarget',{targetId:frame,flatten:true})).sessionId;
 const chooser=`(()=>{
  if(location.href!==${JSON.stringify(issuerUrl)}||document.readyState!=='complete')return null;
  const visible=${visible},norm=v=>(v||'').replace(/\\s+/g,' ').trim();
  if(document.querySelector('textarea,select,[contenteditable]:not([contenteditable="false"]),iframe,frame')||
     [...document.querySelectorAll('input')].some(e=>e.type!=='hidden'&&(e.type!=='checkbox'||visible(e))))return null;
  const controls=[...document.querySelectorAll('button,a[href],[role=\"button\"],[role=\"link\"]')].filter(visible);
  const buttons=controls.filter(e=>e.tagName==='BUTTON'&&!e.disabled&&e.getAttribute('aria-disabled')!=='true');
  const cancel=controls.filter(e=>e.tagName==='A'&&norm(e.innerText)==='Avbryt');
  const app=buttons.filter(e=>norm(e.innerText)==='Bank Norwegian Appen'),bankid=buttons.filter(e=>norm(e.innerText)==='BankID');
  const heading=[...document.querySelectorAll('h1,h2,h3')].filter(e=>visible(e)&&norm(e.innerText)==='Bekreft din identitet');
  if(cancel.length>1||controls.length!==2+cancel.length||buttons.length!==2||app.length!==1||bankid.length!==1||heading.length!==1||/utløpt|utgått|tidsavbrudd|tidsgräns|expired|timed out|avbrutt|cancelled|canceled/i.test(document.body.innerText))return null;
  return app[0];
 })()`;
 require((await evaluate(issuer,`Boolean(${chooser})`)).value===true);
 emit({ready:true});
 const authorization=await input.next();require(!authorization.done&&authorization.value==='choose_bank_app_once');
 require(await frameId()===frame);
 const result=await evaluate(issuer,`(()=>{const button=${chooser};if(!button)return false;button.click();return true})()`);
 require(result.value===true);emit({chosen:true});
}catch{emit({chosen:false});process.exitCode=1}
finally{if(issuer)await send('Target.detachFromTarget',{sessionId:issuer}).catch(()=>{});if(parent)await send('Target.detachFromTarget',{sessionId:parent}).catch(()=>{});ws.close()}
process.exit(process.exitCode||0);
"""


class OdaBrowser:
    checkout_provider = "oda"
    checkout_url = CHECKOUT_URL

    def __init__(
        self,
        *,
        instance: str,
        binary: Path | str,
        executable: Path | str,
        profile: Path | str,
        home: Path | str,
        socket_directory: Path | str,
        uid: int,
        gid: int,
        provider_client: Any = None,
        vipps_phone_number: str | None = None,
    ):
        self.provider_client = provider_client
        self.instance = instance
        self.binary = Path(binary)
        self.executable = Path(executable)
        self.profile = Path(profile)
        self.home = Path(home)
        self.socket_directory = Path(socket_directory)
        self.uid = uid
        self.gid = gid
        self.vipps_phone_number = vipps_phone_number
        self.session = f"oda-household-{instance}"
        self._checkout_deadline: float | None = None
        self._cancellation_deadline: float | None = None

    def _account_reference(self, expected_address: str) -> int:
        value = self._binding_client().call("get_delivery_addresses", {}, deadline=self._checkout_deadline)
        rows = value.get("result") if isinstance(value, Mapping) else None
        if not isinstance(rows, list):
            raise HouseholdError("Retail account addresses are unavailable")
        selected = [row for row in rows if isinstance(row, Mapping) and row.get("isSelected") is True]
        if len(selected) != 1:
            raise HouseholdError("Select one retailer delivery address before checkout")
        address = selected[0].get("address")
        normalize = lambda text: unicodedata.normalize("NFC", " ".join(text.split())).casefold()
        if not isinstance(address, str) or normalize(address) != normalize(expected_address):
            raise HouseholdError("Retail selected account address changed")
        reference = selected[0].get("id")
        _checkout_account_script(reference, provider=self.checkout_provider)
        return reference

    def receipt_address_matches(self, order_id, address, *, deadline=None):
        # Both MCP receipts omit the address. Read it only from the exact
        # order page, independently of the current cart or selected address.
        script = _receipt_address_script(order_id, address, provider=self.checkout_provider)
        with self._checkout_operation(deadline):
            self._open(self._order_url(order_id))
            for _ in range(20):
                if self._eval(script) == {"address_verified": True}:
                    return True
                self._settle(0.25)
        return False

    def _read_order_binding(self, order_id, order, *, deadline=None, expected_binding=None):
        """Bind the exact receipt to an OAuth address without selecting that address.

        The caller owns the browser operation. Recovery retains the original
        address reference even if another address is now selected for shopping.
        """
        if expected_binding is not None:
            require_order_binding(expected_binding)
        if deadline is not None and time.monotonic() >= deadline:
            raise HouseholdError("Retail order binding deadline reached")
        if order.get("currency") != ("SEK" if self.checkout_provider == "mathem" else "NOK"):
            raise HouseholdError("Retail order currency does not match the provider")
        if str(order.get("orderNumber") or order.get("order_number") or order.get("id") or "") != order_id:
            raise HouseholdError("Retail order identity changed")
        self._order_url(order_id)
        response = self._binding_client().call("get_delivery_addresses", {}, deadline=deadline)
        rows = response.get("result") if isinstance(response, Mapping) else None
        if not isinstance(rows, list) or not rows:
            raise HouseholdError("Retail order account addresses are unavailable")
        candidates = []
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("address"), str) or not row["address"].strip():
                raise HouseholdError("Retail order account address is incomplete")
            reference = row.get("id")
            account_script = _checkout_account_script(reference, provider=self.checkout_provider)
            binding = {"account_reference_digest": hashlib.sha256(str(reference).encode()).hexdigest(),
                       "receipt_address": " ".join(unicodedata.normalize("NFC", row["address"]).split())}
            if expected_binding is None or binding == expected_binding:
                candidates.append((binding, account_script))
        if not candidates or expected_binding is not None and len(candidates) != 1:
            raise HouseholdError("Retail original order account binding is unavailable")
        scripts = [_receipt_address_script(order_id, binding["receipt_address"], provider=self.checkout_provider) for binding, _ in candidates]
        self._open(self._order_url(order_id))
        for _ in range(20):
            matched = [candidate for candidate, script in zip(candidates, scripts, strict=True)
                       if self._eval(script) == {"address_verified": True}]
            if len(matched) > 1:
                raise HouseholdError("Retail order receipt address is ambiguous")
            if matched:
                break
            self._settle(0.25)
        else:
            raise HouseholdError("Retail order receipt address cannot be verified")
        binding, account_script = matched[0]
        self._open(_retail_store_url(self.checkout_provider) + "account/delivery/")
        for _ in range(20):
            if self._eval(account_script) == {"account_matches": True}:
                if deadline is not None and time.monotonic() >= deadline:
                    raise HouseholdError("Retail order binding deadline reached")
                return binding
            self._settle(0.25)
        raise HouseholdError("Retail browser and original order account do not match")

    def read_order_binding(self, order_id, order, *, deadline=None, expected_binding=None):
        with self._checkout_operation(deadline):
            return self._read_order_binding(order_id, order, deadline=deadline, expected_binding=expected_binding)

    def order_payment_state(self, order_id, *, deadline=None):
        """Read a fail-closed payment state from the exact Oda order page."""

        if self.checkout_provider != "oda":
            return {"status": "unknown"}
        with self._checkout_operation(deadline, preserve_session=True):
            self._open(self._order_url(order_id))
            script = _oda_order_payment_state_script(order_id)
            for _ in range(20):
                state = self._eval(script)
                if state.get("status") in {"retry_available", "payment_started"}:
                    return state
                self._settle(0.25)
        return {"status": "unknown"}

    def _binding_client(self):
        client = getattr(self, "provider_client", None)
        if client is None or getattr(client, "provider", self.checkout_provider) != self.checkout_provider:
            raise HouseholdError("Dedicated browser provider client is unavailable or mismatched")
        return client

    def _verify_checkout_account(self, address: str) -> str:
        reference = self._account_reference(address)
        self._open(_retail_store_url(self.checkout_provider) + "account/delivery/")
        for _ in range(20):
            if self._eval(_checkout_account_script(reference, provider=self.checkout_provider)) == {"account_matches": True}:
                self._require_checkout_time()
                return hashlib.sha256(str(reference).encode()).hexdigest()
            self._settle(0.25)
        raise HouseholdError("Log the dedicated browser into the same account as retailer OAuth")

    def review_checkout(self, cart: Mapping[str, Any], *, deadline: float | None = None, payment: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._checkout_operation(deadline):
            return self._review_checkout(cart, payment=payment, select_payment=True) if payment is not None else self._review_checkout(cart)

    def review_payment_recovery(self, cart, order_id, *, payment, expected_binding, deadline=None, addition=None):
        """Review the merchant's existing unpaid order, without recreating its cart."""
        with self._checkout_operation(deadline, preserve_session=True):
            binding = require_order_binding(expected_binding)
            self._order_url(order_id)
            if addition is not None and self.checkout_provider != "mathem":
                raise HouseholdError("Addition payment recovery is unavailable for this provider")
            bound_cart = self._order_cart(cart, order_id, addition["before"]["order"], binding) if addition else cart
            expected = self._cart_expectation(bound_cart)
            # Keep the original payment page intact while the merchant decides
            # whether this exact outstanding order has a retry review.
            label = "meal-concierge-payment-recovery"
            tabs = self._invoke("tab", "list").get("tabs", [])
            owned = [tab for tab in tabs if tab.get("label") == label]
            if len(owned) > 1:
                raise HouseholdError("The recovery browser tab is ambiguous")
            if owned:
                self._invoke("tab", owned[0]["tabId"])
            else:
                self._invoke("tab", "new", "--label", label, _retail_store_url(self.checkout_provider))
            if (expected["delivery_address"] != binding["receipt_address"]
                    or self._verify_checkout_account(binding["receipt_address"]) != binding["account_reference_digest"]):
                raise HouseholdError("Recovery account/address differs from the original checkout")
            url = _retail_store_url(self.checkout_provider) + "checkout/retry/?orderNumber=" + quote(order_id, safe="")
            if addition:
                url += "&orderChangeId=" + quote(addition["order_change_id"], safe="")
            self._open(url)
            # The retry heading renders before the asynchronous payment methods.
            selected = False
            for _ in range(60):
                choice = self._eval(_oda_checkout_payment_script(payment))
                if choice.get("verified"):
                    break
                if choice.get("observed") and not selected:
                    selected = True
                    choice = self._eval(_oda_checkout_payment_script(payment, select=True, expected_url=url))
                    if not (choice.get("selected") or choice.get("verified")):
                        raise HouseholdError("The original recovery payment method is unavailable or ambiguous")
                self._settle(0.5)
            else:
                raise HouseholdError("The merchant has no payable recovery review for this order")
            item_review = self._expand_checkout_items(
                len(expected["lines"]),
                # Oda's current retry page can omit product controls entirely.
                # That reduced review is safe only for Vipps: after this click,
                # the hosted-payment capture binds the exact response order
                # before it sends the phone request. A saved card can dispatch
                # immediately, so it must retain the detailed item review.
                allow_summary_only=(
                    self.checkout_provider == "oda" and payment.get("method") == "vipps"
                ),
                expected_product_count=expected["product_count"],
            )
            self._expand_checkout_amount_summary()
            summary_only = item_review == "summary"
            surface = self._eval(_oda_checkout_surface_script(
                expected,
                payment,
                provider=self.checkout_provider,
                summary_product_count=expected["product_count"] if summary_only else None,
            ))
            item_binding_matches = (
                surface.get("items") == [] and surface.get("summary_count_matches") is True
                if summary_only
                else checkout_lines_match(expected["lines"], surface.get("items"))
            )
            if (surface.get("url") != url or surface.get("submit_controls") != 1
                    or not all(surface.get(key) is True for key in (
                        "authenticated", "available", "total_matches", "address_matches", "masked_payment"))
                    or not item_binding_matches
                    or not checkout_delivery_matches(expected["delivery_text"], surface.get("delivery_roots"), provider=self.checkout_provider)):
                raise HouseholdError("The merchant recovery review differs from the original order")
            amounts = self._eval(_oda_checkout_amount_script(expected["total_minor"],
                expected_product_count=expected["product_count"], provider=self.checkout_provider, retry=True,
                addition_retry=bool(addition)))
            if amounts.get("amounts_valid") is not True:
                raise HouseholdError("The merchant recovery amounts differ from the original order")
            return {"order_id": order_id, "binding": dict(binding), "payment_choice": dict(payment),
                    "payment_display": surface["payment_display"], "surface": surface,
                    "amounts_minor": amounts["amounts"], "summary_only": summary_only}

    def submit_payment_recovery(self, cart, review, before_click, *, deadline=None, addition=None, before_vipps_request=None):
        with self._checkout_operation(deadline, preserve_session=True):
            if (review.get("summary_only") is True
                    and (self.checkout_provider != "oda"
                         or review.get("payment_choice", {}).get("method") != "vipps")):
                raise CheckoutPreconditionError(
                    "Summary-only payment recovery is available only for Oda/Vipps"
                )
            if (review.get("payment_choice", {}).get("method") == "vipps"
                    and re.fullmatch(r"\d{8}", str(self.vipps_phone_number or "")) is None):
                raise CheckoutPreconditionError("An exact private Vipps phone number is required before Oda checkout")
            try:
                current = self.review_payment_recovery(cart, review["order_id"],
                    payment=review["payment_choice"], expected_binding=review["binding"], deadline=deadline,
                    **({"addition": addition} if addition else {}))
                if current != dict(review):
                    raise HouseholdError("Recovery changed after its confirmation")
                bound_cart = self._order_cart(cart, review["order_id"], addition["before"]["order"], review["binding"]) if addition else cart
                expected = self._cart_expectation(bound_cart)
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                dispatch_tab = self._checkout_dispatch_tab()
                vipps = review["payment_choice"]["method"] == "vipps"
                if vipps:
                    self._invoke("network", "requests", "--clear")
                before_click()
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                surface = _oda_checkout_surface_script(
                    expected,
                    review["payment_choice"],
                    provider=self.checkout_provider,
                    summary_product_count=(
                        expected["product_count"] if review.get("summary_only") is True else None
                    ),
                ).strip()
                click = _oda_checkout_amount_script(expected["total_minor"],
                    expected_product_count=expected["product_count"], provider=self.checkout_provider,
                    expected_amounts=review["amounts_minor"], expected_url=review["surface"]["url"],
                    vipps=review["payment_choice"].get("method") == "vipps", retry=True,
                    addition_retry=bool(addition)).strip()
                script = ("(() => {const actual=JSON.parse(" + surface
                          + "),expected=" + json.dumps(review["surface"], ensure_ascii=False)
                          + ";const canonical=v=>v&&typeof v==='object'?(Array.isArray(v)?v.map(canonical):Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])]))):v;"
                          + "if(JSON.stringify(canonical(actual))!==JSON.stringify(canonical(expected)))return JSON.stringify({clicked:false});return " + click + ";})()")
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            if self._eval(script) != {"clicked": True}:
                raise CheckoutPreconditionError("Recovery changed before the final payment click")
            return self._capture_checkout_payment(dispatch_tab,
                authentication_expected=review["payment_choice"]["method"] == "saved_card",
                capture_failure=False,
                vipps_expected_total=(expected["total_minor"]
                                      if vipps else None),
                before_vipps_request=before_vipps_request)

    def _order_cart(self, cart, order_id, order, binding):
        binding = require_order_binding(binding)
        original = self._order_expectation(order_id, order)
        return {**cart, "deliverySlot": {"name": original["delivery_text"]}, "deliveryAddress": binding["receipt_address"]}

    def _addition_expectation(self, cart, order_id, order, binding):
        self._order_url(order_id)
        original = self._order_expectation(order_id, order)
        original_count = self._order_product_count(order)
        # An addition cart has no new delivery reservation. It inherits the
        # independently verified original receipt address and delivery window.
        bound = self._order_cart(cart, order_id, order, binding)
        expected = self._cart_expectation(bound)
        expected.update(order_id=order_id, checkout_url=self.checkout_url + "?orderNumber=" + quote(order_id, safe=""),
                        original_minor=original["total_minor"], original_count=original_count)
        if order.get("currency") != ("SEK" if self.checkout_provider == "mathem" else "NOK") or delivery_signature(expected["delivery_text"], provider=self.checkout_provider) is None:
            raise HouseholdError("Retail original order currency or delivery is unavailable")
        return expected

    def review_order_change(self, cart: Mapping[str, Any], order_id: str, order: Mapping[str, Any], *, deadline: float | None = None, expected_binding=None) -> dict[str, Any]:
        with self._checkout_operation(deadline):
            binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=expected_binding)
            bound_cart = self._order_cart(cart, order_id, order, binding)
            review = self._review_checkout(bound_cart, order_id=order_id, delivery_text=self._order_expectation(order_id, order)["delivery_text"],
                addition_expectation=self._addition_expectation(cart, order_id, order, binding))
            review["binding"] = binding
            return review

    def _expand_checkout_items(
        self,
        expected_line_count: int,
        *,
        allow_summary_only: bool = False,
        expected_product_count: int | None = None,
    ) -> str:
        if allow_summary_only and (
            isinstance(expected_product_count, bool)
            or not isinstance(expected_product_count, int)
            or not 0 < expected_product_count <= 1_000_000
        ):
            raise HouseholdError("Oda checkout summary product count is invalid")
        item_control_label = "Visa varor" if self.checkout_provider == "mathem" else "Vis varene"
        summary_control_label = (
            "Visa sammanfattning" if self.checkout_provider == "mathem" else "Vis oppsummering"
        ) if allow_summary_only else None
        summary_label = None
        if allow_summary_only:
            summary_label = (
                "1 vara" if expected_product_count == 1 else f"{expected_product_count} varor"
            ) if self.checkout_provider == "mathem" else (
                "1 vare" if expected_product_count == 1 else f"{expected_product_count} varer"
            )
        expanded = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const itemLabel=ITEM_CONTROL_LABEL,summaryLabel=SUMMARY_CONTROL_LABEL;
 const buttons=[...document.querySelectorAll('button')].filter(enabled);
 const itemControls=buttons.filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===itemLabel);
 const summaryControls=summaryLabel===null?[]:buttons.filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===summaryLabel);
 const inputs=[...document.querySelectorAll('input[type="number"]')].filter(visible).filter(input=>ITEM_MATCH);
 if(itemControls.length>1)return JSON.stringify({expanded:false,mode:null});
 if(inputs.length>0)return JSON.stringify({expanded:true,mode:'items'});
 if(itemControls.length===1){itemControls[0].click();return JSON.stringify({expanded:true,mode:'items'});}
 if(summaryLabel!==null){
   if(summaryControls.length>1)return JSON.stringify({expanded:false,mode:null});
   if(summaryControls.length===1)summaryControls[0].click();
   return JSON.stringify({expanded:true,mode:'summary'});
 }
 return JSON.stringify({expanded:true,mode:'items'});
})()
""".replace("ITEM_CONTROL_LABEL", json.dumps(item_control_label, ensure_ascii=False)).replace("SUMMARY_CONTROL_LABEL", json.dumps(summary_control_label, ensure_ascii=False)).replace("ITEM_MATCH", "[...(input.labels||[])].some(label=>norm(label.textContent)==='Antal')" if self.checkout_provider == "mathem" else "/\\bAntall\\b/i.test(norm(input.closest('li,article')?.innerText||''))"))
        if (expanded.get("expanded") is not True
                or expanded.get("mode") not in {"items", "summary"}):
            raise HouseholdError("Oda checkout items cannot be reviewed")
        mode = expanded["mode"]
        for _ in range(20):
            ready = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const mode=MODE,controlLabel=mode==='items'?ITEM_CONTROL_LABEL:SUMMARY_CONTROL_LABEL;
 const show=[...document.querySelectorAll('button')].filter(visible).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===controlLabel);
 const inputs=[...document.querySelectorAll('input[type="number"]')].filter(visible).filter(input=>ITEM_MATCH);
 const summaryLabel=SUMMARY_LABEL;
 const summaryNodes=summaryLabel===null?[]:[...document.querySelectorAll('*')].filter(visible).filter(node=>norm(node.innerText||'')===summaryLabel).filter(node=>![...node.children].some(child=>visible(child)&&norm(child.innerText||'')===summaryLabel));
 const ready=show.length===0&&(mode==='items'?inputs.length===COUNT:inputs.length===0&&summaryNodes.length===1);
 return JSON.stringify({ready,mode});
})()
""".replace("COUNT", str(expected_line_count)).replace("MODE", json.dumps(mode)).replace("ITEM_CONTROL_LABEL", json.dumps(item_control_label, ensure_ascii=False)).replace("SUMMARY_CONTROL_LABEL", json.dumps(summary_control_label, ensure_ascii=False)).replace("SUMMARY_LABEL", json.dumps(summary_label, ensure_ascii=False)).replace("ITEM_MATCH", "[...(input.labels||[])].some(label=>norm(label.textContent)==='Antal')" if self.checkout_provider == "mathem" else "/\\bAntall\\b/i.test(norm(input.closest('li,article')?.innerText||''))"))
            if ready.get("ready") is True and ready.get("mode") in {"items", "summary"}:
                return ready["mode"]
            self._settle(0.25)
        raise HouseholdError("Oda checkout items did not finish rendering")

    def _review_checkout(self, cart: Mapping[str, Any], *, order_id: str | None = None, delivery_text: str | None = None, payment: Mapping[str, Any] | None = None, select_payment: bool = False, addition_expectation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        expected = self._cart_expectation(cart)
        account_digest = self._verify_checkout_account(expected["delivery_address"]) if order_id is None else None
        if delivery_text is not None:
            expected["delivery_text"] = delivery_text
        if order_id is None:
            self._navigate_to_checkout(payment=payment, select_payment=select_payment) if payment is not None else self._navigate_to_checkout()
        else:
            self._navigate_to_checkout(order_id)
        self._expand_checkout_items(len(expected["lines"]))
        self._expand_checkout_amount_summary()
        script = _oda_checkout_surface_script(expected, payment)
        result = self._eval(script)
        required = {"url", "authenticated", "available", "items", "total_matches", "delivery_roots", "address_matches", "masked_payment", "payment_display", "submit_controls"}
        expected_url = CHECKOUT_URL if order_id is None else f"{CHECKOUT_URL}?orderNumber={order_id}"
        if set(result) != required or result["url"] != expected_url or type(result["submit_controls"]) is not int:
            raise HouseholdError("Oda checkout page changed")
        if result["authenticated"] is not True:
            raise HouseholdError("Oda browser login could not be verified; log the dedicated browser into the same intended account as Oda OAuth, then request a new checkout review")
        if result["available"] is not True:
            raise OdaCheckoutMismatchError("Oda checkout has unavailable items or details; review the current cart before checkout")
        if result["address_matches"] is not True:
            raise HouseholdError("Oda browser delivery address does not match the reviewed cart; check the intended account and address in Oda, then request a new checkout review")
        if result["masked_payment"] is not True:
            raise HouseholdError("Oda configured payment selection could not be verified; review checkout_payment in setup, then request a new checkout review")
        surface = dict(result)
        result["line_matches"] = checkout_lines_match(expected["lines"], result.pop("items"))
        result["delivery_matches"] = checkout_delivery_matches(expected["delivery_text"], result.pop("delivery_roots"))
        if not all(result[key] is True for key in ("authenticated", "available", "line_matches", "total_matches", "delivery_matches", "address_matches", "masked_payment")) or result["submit_controls"] != 1:
            raise OdaCheckoutMismatchError("Oda checkout does not match the reviewed cart")
        if addition_expectation is not None:
            amounts = self._eval(_retail_addition_amount_script(addition_expectation, provider=self.checkout_provider))
            if amounts.get("amounts_valid") is not True:
                raise OdaCheckoutMismatchError("Oda original, added and combined order amounts do not match")
            result["order_amounts"] = amounts["order_amounts"]
            result["amounts"] = {key: None for key in ODA_CHECKOUT_AMOUNT_KEYS}
            result["amounts"].update(product_subtotal=expected["total_minor"] / 100, provider_total=expected["total_minor"] / 100)
        else:
            result["amounts"] = self._read_checkout_amounts(
                expected["total_minor"], expected["product_count"],
            )
        if not (payment and payment.get("method") == "vipps" and result.get("payment_display") == "Vipps") and re.fullmatch(r"•••• \d{4}", str(result.get("payment_display") or "")) is None:
            raise HouseholdError("Oda checkout payment identity is unavailable")
        if payment is not None:
            result["payment_choice"] = dict(payment)
        result["surface"] = surface
        if account_digest is not None:
            result["account_reference_digest"] = account_digest
        return result

    def _expand_checkout_amount_summary(self) -> None:
        amounts_expanded = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const buttons=[...document.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true');
 const show=buttons.filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===SHOW_LABEL);
 const hide=buttons.filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===HIDE_LABEL);
 if(show.length===1&&hide.length===0){show[0].click();return JSON.stringify({expanded:true});}
 return JSON.stringify({expanded:show.length===0&&hide.length===1});
})()
""".replace("SHOW_LABEL", json.dumps("Visa sammanfattning" if self.checkout_provider == "mathem" else "Vis oppsummering")).replace("HIDE_LABEL", json.dumps("Dölj sammanfattning" if self.checkout_provider == "mathem" else "Skjul oppsummering")))
        if amounts_expanded != {"expanded": True}:
            raise HouseholdError("Oda checkout amount summary changed")
        self._settle(0.25)

    def _read_checkout_amounts(self, expected_total: int, expected_product_count: int) -> dict[str, Any]:
        amount_result = self._eval(_oda_checkout_amount_script(
            expected_total, expected_product_count=expected_product_count, provider=self.checkout_provider,
        ))
        if not isinstance(amount_result, Mapping) or set(amount_result) != {"amounts", "amounts_valid"} or amount_result["amounts_valid"] is not True:
            raise OdaCheckoutMismatchError("Oda checkout amount summary changed")
        raw_amounts = amount_result["amounts"]
        keys = set(ODA_CHECKOUT_AMOUNT_KEYS) | ({"discount_breakdown"} if self.checkout_provider == "mathem" else set())
        if not isinstance(raw_amounts, Mapping) or set(raw_amounts) != keys:
            raise HouseholdError("Oda checkout amounts changed")
        amounts: dict[str, Any] = {}
        for key in ODA_CHECKOUT_AMOUNT_KEYS:
            value = raw_amounts[key]
            if key == "other_fees" and isinstance(value, Mapping):
                amounts[key] = {str(name): minor / 100 for name, minor in value.items() if type(minor) is int}
                if len(amounts[key]) != len(value):
                    raise HouseholdError("Oda checkout amounts changed")
            elif value is None:
                amounts[key] = None
            elif type(value) is int:
                amounts[key] = value / 100
            else:
                raise HouseholdError("Oda checkout amounts changed")
        normalized_minor = _oda_checkout_amounts_minor(amounts, provider=self.checkout_provider)
        if normalized_minor["provider_total"] != expected_total:
            raise HouseholdError("Oda checkout amounts changed")
        if self.checkout_provider == "mathem":
            breakdown = raw_amounts["discount_breakdown"]
            if (
                not isinstance(breakdown, Mapping)
                or set(breakdown) != {"product_discount", "delivery_discount"}
                or any(value is not None and (type(value) is not int or value > 0) for value in breakdown.values())
                or sum(value or 0 for value in breakdown.values()) != (normalized_minor["discounts"] or 0)
            ):
                raise HouseholdError("Mathem checkout discount rows changed")
            amounts["discount_breakdown"] = {key: value / 100 if value is not None else None for key, value in breakdown.items()}
        return amounts

    def _navigate_to_checkout(self, order_id: str | None = None, *, payment: Mapping[str, Any] | None = None, select_payment: bool = False) -> None:
        self._continue_checkout_cart()
        if order_id is None:
            self._advance_checkout_path(payment=payment, select_payment=select_payment) if payment is not None else self._advance_checkout_path()
        else:
            self._advance_checkout_path(order_id)

    def _continue_checkout_cart(self) -> str:
        store_url = _retail_store_url(self.checkout_provider)
        for attempt in range(3):
            try:
                self._open(store_url + "cart/")
                break
            except HouseholdError:
                if self.checkout_provider == "mathem" and self._eval("JSON.stringify({url:location.href})") == {"url": store_url}:
                    break  # Mathem can initially expose the storefront cart panel.
                if attempt == 2:
                    raise
                try:
                    self.close()
                except HouseholdError:
                    pass
                self._settle(0.5 * (attempt + 1))
        self._settle(12)
        self._invoke("reload")
        self._invoke("snapshot")
        action = "wait"
        full_cart_dispatched = False
        for attempt in range(2):
            self._settle(5)
            for _ in range(5):
                surface = self._cart_surface()
                action = surface.get("action")
                if action == "full_cart" and self.checkout_provider == "mathem":
                    if not full_cart_dispatched:
                        full_cart_dispatched = True
                        self._click_action("full-cart")
                    action = "wait"
                    self._settle(1)
                    continue
                if action in {"continue", "blocked"}:
                    break
                self._settle(1)
            if action != "wait":
                break
            if attempt < 1:
                self._invoke("reload")
                self._invoke("snapshot")
        if action != "continue":
            raise HouseholdError("Oda cart cannot continue to checkout")
        self._click_action("continue", mouse=True)
        return "continue"

    def _cart_surface(self) -> dict[str, Any]:
        mathem = self.checkout_provider == "mathem"
        store_url = _retail_store_url(self.checkout_provider)
        return self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const unavailable=new RegExp(UNAVAILABLE_LABELS,'i');
 const storefrontLinkOnly=ALLOW_STOREFRONT_LINK;
 document.querySelectorAll('[data-oda-household-action]').forEach(x=>x.removeAttribute('data-oda-household-action'));
 if(![STORE,CART].includes(location.href))return JSON.stringify({action:'blocked'});
 if(document.querySelector('input[type="password"]'))return JSON.stringify({action:'blocked'});
 const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
 if(dialogs.some(root=>unavailable.test(norm(root.innerText||''))))return JSON.stringify({action:'blocked'});
 let roots=[];
 if(location.href===CART){
   const main=document.querySelector('main');
   roots=[main||document];
   if(unavailable.test(norm((main||document.body).innerText||'')))return JSON.stringify({action:'blocked'});
 }else{
   const candidates=dialogs.length?dialogs:(storefrontLinkOnly?[document]:[]);
   roots=candidates.filter(root=>{
     const text=norm(root.innerText||'');
     const next=[...root.querySelectorAll('button')].filter(visible).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===CONTINUE_LABEL);
     const full=[...root.querySelectorAll('a')].filter(visible).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===FULL_CART_LABEL&&x.href===CART);
     return full.length===1||(!storefrontLinkOnly&&next.length===1&&new RegExp(CART_CONTENTS,'i').test(text));
   });
 }
 if(roots.length>1)return JSON.stringify({action:'blocked'});
 if(roots.length===0)return JSON.stringify({action:'wait'});
 const root=roots[0];
 const next=storefrontLinkOnly&&location.href!==CART?[]:[...root.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===CONTINUE_LABEL);
 const full=[...root.querySelectorAll('a')].filter(visible).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===FULL_CART_LABEL&&x.href===CART);
 if(next.length>1||full.length>1)return JSON.stringify({action:'blocked'});
 if(next.length===1){next[0].setAttribute('data-oda-household-action','continue');return JSON.stringify({action:'continue'});}
 if(full.length===1){full[0].setAttribute('data-oda-household-action','full-cart');return JSON.stringify({action:'full_cart'});}
 return JSON.stringify({action:'wait'});
})()
""".replace("ALLOW_STOREFRONT_LINK", "true" if mathem else "false")
          .replace("CONTINUE_LABEL", json.dumps("Fortsätt" if mathem else "Fortsett"))
          .replace("FULL_CART_LABEL", json.dumps("Fortsätt till varukorgen" if mathem else "Gå til handlekurven"))
          .replace("UNAVAILABLE_LABELS", json.dumps("inte tillgänglig|slut i lager|unavailable" if mathem else "ikke tilgjengelig|utsolgt|unavailable"))
          .replace("CART_CONTENTS", json.dumps("Delsumma" if mathem else r"Delsum|Tøm handlekurv|Du har \d+ varer"))
          .replace("STORE", json.dumps(store_url))
          .replace("CART", json.dumps(store_url + "cart/")))

    def _click_action(self, action: str, *, mouse: bool = False) -> None:
        if action not in {"open-cart", "full-cart", "continue", "new-order", "previous-order", "payment", "recommendations"}:
            raise HouseholdError("invalid Oda browser action")
        selector = f'[data-oda-household-action="{action}"]'
        if not mouse:
            self._invoke("click", selector)
            return
        self._invoke("scrollintoview", selector)
        box = self._find_box(self._invoke("get", "box", selector))
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise HouseholdError("Oda cart control is not clickable")
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        clear = self._eval(r"""
(() => {
 const target=document.elementFromPoint(X,Y);
 return JSON.stringify({clear:!!target&&!!target.closest(SELECTOR)});
})()
""".replace("X", json.dumps(x)).replace("Y", json.dumps(y)).replace("SELECTOR", json.dumps(selector)))
        if clear != {"clear": True}:
            raise HouseholdError("Oda cart control is obscured")
        self._invoke("mouse", "move", str(round(x)), str(round(y)))
        self._invoke("mouse", "down")
        self._invoke("mouse", "up")

    @staticmethod
    def _find_box(value: Any) -> dict[str, float] | None:
        if isinstance(value, Mapping):
            if all(isinstance(value.get(key), (int, float)) for key in ("x", "y", "width", "height")):
                return {key: float(value[key]) for key in ("x", "y", "width", "height")}
            for child in value.values():
                if found := OdaBrowser._find_box(child):
                    return found
        elif isinstance(value, list):
            for child in value:
                if found := OdaBrowser._find_box(child):
                    return found
        return None

    def _advance_checkout_path(self, order_id: str | None = None, *, payment: Mapping[str, Any] | None = None, select_payment: bool = False) -> None:
        script = r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const unavailable=/ikke tilgjengelig|utsolgt|unavailable/i;
 document.querySelectorAll('[data-oda-household-action]').forEach(x=>x.removeAttribute('data-oda-household-action'));
 const confirmPage=location.origin==='https://oda.com'&&location.pathname==='/no/checkout/confirm/';
 if(![STORE,CART,CHECKOUT_ENTRY,MODIFY,RECOMMENDATIONS].includes(location.href)&&!confirmPage)return JSON.stringify({action:'blocked'});
 const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
 if(unavailable.test(norm(document.body?.innerText||''))||dialogs.some(root=>unavailable.test(norm(root.innerText||''))))return JSON.stringify({action:'blocked'});
 if(location.href===MODIFY){
   // Bind the requested destination; Oda initially selects an existing order.
   const main=document.querySelector('main');
   if(!main||!visible(main)||dialogs.length||document.querySelector('input[type="password"]'))return JSON.stringify({action:'blocked'});
   const radios=[...main.querySelectorAll('input[type="radio"]')];
   const selected=radios.filter(x=>x.checked);
   const existing=ORDER!==null;
   const candidates=radios.filter(x=>enabled(x)&&[...x.labels].filter(label=>{
     if(!visible(label)||!label.contains(x)||label.querySelectorAll('input[type="radio"]').length!==1)return false;
     const text=norm(label.innerText);
     return existing?/^Legg til i eksisterende bestilling(?:\s|$)/.test(text)&&text.split(/[^A-Za-z0-9_-]+/).filter(token=>token===ORDER).length===1:/^Lag en ny bestilling(?:\s|$)/.test(text);
   }).length===1);
   if(candidates.length!==1||selected.length!==1||radios.some(x=>!visible(x)))return JSON.stringify({action:'blocked'});
   const target=candidates[0];
   if(!target.checked){target.setAttribute('data-oda-household-action',existing?'previous-order':'new-order');return JSON.stringify({action:existing?'previous_order':'new_order'});}
   const payment=[...main.querySelectorAll('button')].filter(enabled).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')===(existing?'Gå til betaling':'Fortsett'));
   if(payment.length!==1)return JSON.stringify({action:'blocked'});
   payment[0].setAttribute('data-oda-household-action','payment');
   return JSON.stringify({action:'payment'});
 }
 if(confirmPage){
   const expected=ORDER,actual=new URL(location.href).searchParams.get('orderNumber');
   if(!((expected===null&&actual===null)||(expected!==null&&actual===expected)))return JSON.stringify({action:'blocked'});
   const payment=JSON.parse(PAYMENT);
   if(payment.observed&&!payment.verified)return JSON.stringify({action:'payment_required'});
   const controls=[...document.querySelectorAll('button')].filter(enabled);
   const submit=controls.filter(x=>/^(FINAL_CONTROL)(\b|\s)/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
   if(submit.length===1){
     if(payment.verified)return JSON.stringify({action:'ready'});
     return JSON.stringify({action:'wait'});
   }
   if(submit.length>1)return JSON.stringify({action:'blocked'});
   return JSON.stringify({action:'wait'});
 }
 if(location.href===CHECKOUT_ENTRY)return JSON.stringify({action:'wait'});
 if(location.href===RECOMMENDATIONS){
   const controls=[...document.querySelectorAll('a')].filter(enabled);
   const next=controls.filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Fortsett'&&x.href===CHECKOUT);
   if(next.length!==1)return JSON.stringify({action:'blocked'});
   next[0].setAttribute('data-oda-household-action','recommendations');
   return JSON.stringify({action:'recommendations'});
 }
 const main=document.querySelector('main');
 const candidates=dialogs.length?dialogs:(main&&visible(main)?[main]:[]);
 const roots=candidates.filter(root=>{
   const controls=[...root.querySelectorAll('button,a')].filter(enabled);
   const orderTokens=norm(root.innerText||'').split(/[^A-Za-z0-9_-]+/).filter(Boolean);
   const exactOrder=ORDER!==null&&orderTokens.filter(x=>x===ORDER).length===1;
   return controls.some(x=>{
     const label=norm(x.innerText||x.getAttribute('aria-label')||'');
     if(/^Legg til i forrige bestilling$/i.test(label)||label===ORDER)return exactOrder;
     return /^(Ny bestilling|Ny levering|separat(?: bestilling| levering)?|egen levering|Gå til betaling)$/i.test(label);
   });
 });
 if(roots.length>1)return JSON.stringify({action:'blocked'});
 if(roots.length===0)return JSON.stringify({action:'wait'});
 const controls=[...roots[0].querySelectorAll('button,a')].filter(enabled);
   const orderTokens=norm(roots[0].innerText||'').split(/[^A-Za-z0-9_-]+/).filter(Boolean);
   const exactOrder=ORDER!==null&&orderTokens.filter(x=>x===ORDER).length===1;
   const newOrder=controls.filter(x=>/^(Ny bestilling|Ny levering|separat(?: bestilling| levering)?|egen levering)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
   const previous=exactOrder?controls.filter(x=>/^Legg til i forrige bestilling$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')) || norm(x.innerText||x.getAttribute('aria-label')||'')===ORDER):[];
   const payment=controls.filter(x=>/^Gå til betaling$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
   if(ORDER===null && newOrder.length===1 && previous.length===0 && payment.length===0){newOrder[0].setAttribute('data-oda-household-action','new-order');return JSON.stringify({action:'new_order'});}
   if(ORDER===null && newOrder.length===0 && previous.length===0 && payment.length===1){payment[0].setAttribute('data-oda-household-action','payment');return JSON.stringify({action:'payment'});}
   if(ORDER!==null && newOrder.length===0 && previous.length===1 && payment.length===0){previous[0].setAttribute('data-oda-household-action','previous-order');return JSON.stringify({action:'previous_order'});}
   if(ORDER!==null && newOrder.length===0 && previous.length===0 && payment.length===1){payment[0].setAttribute('data-oda-household-action','payment');return JSON.stringify({action:'payment'});}
   return JSON.stringify({action:'blocked'});
})()
""".replace("STORE", json.dumps(STORE_URL)).replace("CART", json.dumps(CART_URL)).replace("CHECKOUT_ENTRY", json.dumps(CHECKOUT_ENTRY_URL)).replace("MODIFY", json.dumps(CHECKOUT_MODIFY_URL)).replace("RECOMMENDATIONS", json.dumps(RECOMMENDATIONS_URL)).replace("CHECKOUT", json.dumps(CHECKOUT_URL)).replace("PAYMENT", _oda_checkout_payment_script(payment).strip()).replace("FINAL_CONTROL", "Betal med" if payment and payment.get("method") == "vipps" else "Bekreft og betal|Legg inn bestilling|Confirm and pay|Place order").replace("ORDER", json.dumps(order_id))
        dispatched: set[str] = set()
        self._settle(10)
        for _ in range(30):
            action = self._eval(script).get("action")
            if action == "ready":
                return
            if action == "payment_required":
                if not select_payment:
                    raise HouseholdError("Oda configured payment is not selected; request a new checkout review")
                if "select-payment" in dispatched:
                    self._settle(0.5)
                    continue
                dispatched.add("select-payment")
                selected = self._eval(_oda_checkout_payment_script(payment, select=True, expected_url=CHECKOUT_URL))
                if selected.get("selected") is not True and selected.get("verified") is not True:
                    raise HouseholdError("Oda configured payment is unavailable or ambiguous; choose checkout_payment in setup, with card_last4 if several saved cards exist")
                self._settle(1)
                continue
            if action == "blocked":
                raise HouseholdError("Oda checkout navigation is ambiguous")
            if action in {"new_order", "previous_order", "payment", "recommendations"}:
                if action not in dispatched:
                    dispatched.add(action)
                    self._click_action(action.replace("_", "-"))
                    self._settle(10)
                else:
                    self._settle(0.5)
                continue
            self._settle(0.5)
        raise HouseholdError("Oda checkout navigation timed out")

    def _checkout_dispatch_tab(self):
        try:
            result = self._invoke("tab", "list")
        except HouseholdError:
            return None
        tabs = result.get("tabs") if isinstance(result, Mapping) else None
        if not isinstance(tabs, list):
            return None
        active = [tab.get("tabId") for tab in tabs if isinstance(tab, Mapping) and tab.get("active") is True]
        return active[0] if len(active) == 1 else None

    def _checkout_payment_observation(self, dispatch_tab):
        if dispatch_tab is None or self._checkout_dispatch_tab() != dispatch_tab:
            return None
        page = self._eval(r"""(() => {
 const visible=e=>{for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false;}const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
 const containers=[...document.querySelectorAll('.adyen-checkout__threeds2__challenge')].filter(visible);
 const frames=containers.length===1?[...containers[0].querySelectorAll('iframe[name="threeDSIframe"]')].filter(visible):[];
 return JSON.stringify({url:location.href,challenge:containers.length===1&&frames.length===1,
  failed:[...document.querySelectorAll('article')].some(e=>visible(e)&&/Din betalning gick inte igenom/i.test(e.innerText||''))});
})()""")
        parsed = urlsplit(page.get("url", ""))
        store = urlsplit(_retail_store_url(self.checkout_provider))
        if parsed.scheme != store.scheme or parsed.netloc != store.netloc:
            return None
        params = parse_qs(parsed.query, keep_blank_values=True)
        payment_id = params.get("paymentId", [])
        if (parsed.path == store.path + "checkout/threeDS/" and not parsed.fragment
                and set(params) == {"paymentId"} and len(payment_id) == 1
                and re.fullmatch(r"[1-9][0-9]{0,19}", payment_id[0])):
            page["authentication_context"] = {"tab_id": dispatch_tab, "payment_id": payment_id[0]}
        return page

    def checkout_payment_authentication(self, context, *, deadline=None):
        """Observe only the retained payment; never navigate to an auth URL."""
        if not isinstance(context, Mapping) or set(context) != {"tab_id", "payment_id"}:
            return None
        try:
            with self._checkout_operation(deadline, preserve_session=True):
                page = self._checkout_payment_observation(context["tab_id"])
        except HouseholdError:
            return None
        if not page or page.get("authentication_context") != dict(context):
            return None
        return {"active": True, "challenge": page.get("challenge") is True}

    def checkout_payment_failure(self, context, *, expected_order_id=None, deadline=None):
        """Resolve a late original failure through its retained native payment."""
        if self.checkout_provider != "mathem" or not isinstance(context, Mapping) or set(context) != {"tab_id", "payment_id"}:
            return None
        try:
            with self._checkout_operation(deadline, preserve_session=True):
                page = self._checkout_payment_observation(context["tab_id"])
                if not page or page.get("failed") is not True:
                    return None
                parsed = urlsplit(page["url"])
                store = urlsplit(_retail_store_url(self.checkout_provider))
                params = parse_qs(parsed.query, keep_blank_values=True)
                orders = params.get("orderNumber", [])
                changes = params.get("orderChangeId", [])
                addition = expected_order_id is not None
                if (parsed.scheme != store.scheme or parsed.netloc != store.netloc
                        or parsed.path != store.path + "checkout/retry/" or parsed.fragment
                        or set(params) != ({"orderNumber", "orderChangeId"} if addition else {"orderNumber"})
                        or len(orders) != 1
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", orders[0]) is None):
                    return None
                if addition and (orders != [expected_order_id] or len(changes) != 1
                        or re.fullmatch(r"[1-9][0-9]{0,15}", changes[0]) is None):
                    return None
                endpoint = "/api/v1/payments/adyen/three-ds/" + quote(str(context["payment_id"]), safe="") + "/"
                script = r"""(async()=>{
 const expected=EXPECTED;
 if(location.href!==expected.page)return JSON.stringify({});
 const response=await fetch(expected.endpoint,{method:'GET',credentials:'same-origin',redirect:'error'});
 if(!response.ok)return JSON.stringify({});
 const result=await response.json(),p=result.params;
 if(location.href!==expected.page||result.type!=='checkout-payment-retry'||!p||
    Object.keys(p).sort().join(',')!=='order_change_id,order_number'||
    typeof p.order_number!=='string'||p.order_number!==expected.order)return JSON.stringify({});
 if(expected.change===null){if(p.order_change_id!==null)return JSON.stringify({});}
 else if(!Number.isSafeInteger(p.order_change_id)||p.order_change_id<=0||
         String(p.order_change_id)!==expected.change)return JSON.stringify({});
 return JSON.stringify({payment_failed:true,order_id:expected.order,
  ...(expected.change===null?{}:{order_change_id:expected.change})});
})()""".replace("EXPECTED", json.dumps({"page": page["url"], "endpoint": endpoint, "order": orders[0],
                                        "change": changes[0] if addition else None}))
                result = self._eval(script)
                if self._checkout_dispatch_tab() != context["tab_id"]:
                    return None
                return result if result.get("payment_failed") is True else None
        except HouseholdError:
            return None

    def choose_checkout_bank_app(self, context, before_choice, *, deadline=None):
        """Choose the observed issuer's app method once, without reading inputs."""
        node = shutil.which("node")
        if node is None:
            return {"chosen": False}
        with self._checkout_operation(deadline, preserve_session=True):
            page = self._checkout_payment_observation(context["tab_id"])
            if not page or page.get("authentication_context") != context or page.get("challenge") is not True:
                return {"chosen": False}
            endpoint = self._invoke("get", "cdp-url").get("cdpUrl")
            parsed = urlsplit(str(endpoint or ""))
            if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                return {"chosen": False}
            # Native agent-browser 0.33.1 cannot select this OOPIF by CSS.
            # Attach directly to its DOM-bound target; never obtain an AX tree.
            process = subprocess.Popen(
                [node, "--input-type=module", "-e", _BANK_APP_CHOICE_SCRIPT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, env={"PATH": os.environ.get("PATH", os.defpath)},
            )
            try:
                process.stdin.write(json.dumps({"endpoint": endpoint, "url": page["url"]}) + "\n")
                process.stdin.flush()
                if not select.select([process.stdout], [], [], 10)[0]:
                    return {"chosen": False}
                if json.loads(process.stdout.readline()) != {"ready": True}:
                    return {"chosen": False}
                current = self._checkout_payment_observation(context["tab_id"])
                if not current or current.get("authentication_context") != context or current.get("challenge") is not True:
                    return {"chosen": False}
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                before_choice()
                output, _ = process.communicate("choose_bank_app_once\n", timeout=10)
                return {"chosen": process.returncode == 0 and json.loads(output) == {"chosen": True}}
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                raise HouseholdError("Bank app method selection could not be confirmed; reconcile the original payment") from exc
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def _complete_oda_vipps_request(self, dispatch_tab: str, expected_total: int, before_request=None) -> dict[str, Any]:
        """Send one verified hosted Vipps request for the already-created Oda order."""

        if self.checkout_provider != "oda" or re.fullmatch(r"\d{8}", str(self.vipps_phone_number or "")) is None:
            raise HouseholdError("An exact private Vipps phone number is required before Oda checkout")
        observed: dict[str, Any] = {}
        phone_filled = False
        for _ in range(40):
            if self._checkout_dispatch_tab() != dispatch_tab:
                raise HouseholdError("The Oda/Vipps payment tab changed; the outcome is uncertain; do not retry")
            observed = self._eval(_oda_vipps_gateway_script(expected_total, self.vipps_phone_number))
            if observed.get("sent") is True:
                raise HouseholdError("The Oda/Vipps request was already sent before its bound control was verified; reconcile the same order")
            if observed.get("fillable") is True and observed.get("phone_matches") is not True and not phone_filled:
                if self._eval(_oda_vipps_phone_fill_script(self.vipps_phone_number)) != {"filled": True}:
                    raise HouseholdError("The Oda/Vipps phone field changed before it could be filled")
                phone_filled = True
                self._settle(0.25)
                continue
            if observed.get("identity") is True and (observed.get("ready") is True or observed.get("expired") is True):
                break
            self._settle(0.25)
        if observed.get("expired") is True:
            raise HouseholdError("The Oda/Vipps payment expired before a mobile request was sent; reconcile the same order")
        if observed.get("identity") is not True or observed.get("ready") is not True:
            raise HouseholdError("The Oda/Vipps payment page could not be verified; the outcome is uncertain; do not retry")
        selector = "[data-oda-household-vipps-next]"
        gateway_url = str(self._invoke("get", "url").get("url") or "")
        parsed_gateway = urlsplit(gateway_url)
        gateway_params = parse_qs(parsed_gateway.query, keep_blank_values=True)
        if (parsed_gateway.scheme != "https" or parsed_gateway.hostname != "pay.vipps.no"
                or parsed_gateway.netloc != "pay.vipps.no"
                or set(gateway_params) != {"token"} or len(gateway_params["token"]) != 1
                or not gateway_params["token"][0] or parsed_gateway.fragment):
            raise HouseholdError("The exact Oda/Vipps transaction URL is unavailable; do not send or retry payment")
        payment_request_id = oda_checkout_pay_request_id(
            self._invoke("network", "requests", "--filter", "/checkout/pay/")
        )
        payment_response: Mapping[str, Any] = {}
        for attempt in range(8):
            response = self._invoke("network", "request", payment_request_id)
            payment_response = response if isinstance(response, Mapping) else {}
            if isinstance(payment_response.get("responseBody"), str):
                break
            if attempt < 7:
                self._settle(0.25)
        order_id = oda_checkout_pay_order_id(payment_response, gateway_url)
        self._invoke("scrollintoview", selector)
        box = self._find_box(self._invoke("get", "box", selector))
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise HouseholdError("The Oda/Vipps request control is not clickable; the outcome is uncertain; do not retry")
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        if (self._checkout_dispatch_tab() != dispatch_tab
                or self._eval(_oda_vipps_gateway_script(
                    expected_total, self.vipps_phone_number, expected_url=gateway_url,
                    require_hit=True, hit_x=x, hit_y=y,
                )) != {"identity": True, "ready": True, "sent": False, "expired": False,
                       "fillable": True, "phone_matches": True}):
            raise HouseholdError("The Oda/Vipps request control changed or is obscured; the outcome is uncertain; do not retry")
        self._require_checkout_time(FINAL_CLICK_MARGIN)
        request_context = {
            "tab_id": dispatch_tab,
            "expected_total": expected_total,
            "gateway_url_digest": hashlib.sha256(gateway_url.encode()).hexdigest(),
            "order_id": order_id,
        }
        if before_request:
            before_request(request_context)
        current_gateway_url = str(self._invoke("get", "url").get("url") or "")
        if (self._checkout_dispatch_tab() != dispatch_tab or current_gateway_url != gateway_url
                or self._eval(_oda_vipps_gateway_script(
                    expected_total, self.vipps_phone_number, expected_url=gateway_url,
                    require_hit=True, hit_x=x, hit_y=y,
                )) != {"identity": True, "ready": True, "sent": False, "expired": False,
                       "fillable": True, "phone_matches": True}):
            raise HouseholdError("The fenced Oda/Vipps request changed before Next; reconcile it without retrying")
        self._invoke("mouse", "move", str(round(x)), str(round(y)))
        self._invoke("mouse", "down")
        self._invoke("mouse", "up")
        for _ in range(40):
            if self._checkout_dispatch_tab() != dispatch_tab:
                break
            result = self._eval(_oda_vipps_gateway_script(
                expected_total, self.vipps_phone_number, expected_url=gateway_url,
            ))
            if result.get("sent") is True:
                return request_context
            self._settle(0.25)
        raise HouseholdError("Vipps did not confirm the Oda mobile payment request; the outcome is uncertain; do not retry")

    def checkout_vipps_request_state(self, context, *, deadline=None):
        """Observe the retained Oda/Vipps page without causing another request."""

        if (not isinstance(context, Mapping)
                or set(context) != {"tab_id", "expected_total", "gateway_url_digest", "order_id"}
                or not isinstance(context.get("tab_id"), str)
                or type(context.get("expected_total")) is not int
                or context["expected_total"] < 0
                or re.fullmatch(r"[0-9a-f]{64}", str(context.get("gateway_url_digest") or "")) is None
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", str(context.get("order_id") or "")) is None
                or re.fullmatch(r"\d{8}", str(self.vipps_phone_number or "")) is None):
            return {"status": "unknown"}
        with self._checkout_operation(deadline, preserve_session=True):
            if self._checkout_dispatch_tab() != context["tab_id"]:
                return {"status": "unknown"}
            try:
                current_url = str(self._invoke("get", "url").get("url") or "")
                if hashlib.sha256(current_url.encode()).hexdigest() != context["gateway_url_digest"]:
                    return {"status": "unknown"}
                observed = self._eval(_oda_vipps_gateway_script(
                    context["expected_total"], self.vipps_phone_number, expected_url=current_url,
                ))
            except HouseholdError:
                return {"status": "unknown"}
        if observed.get("sent") is True:
            return {"status": "sent"}
        if observed.get("expired") is True:
            return {"status": "expired"}
        return {"status": "unknown"}

    def _capture_checkout_payment(self, dispatch_tab, *, order_id=None, authentication_expected=True, capture_failure=True, vipps_expected_total=None, before_vipps_request=None):
        # Bind 3DS to this dispatch's tab and native payment identity. Only a
        # visible issuer challenge establishes user action. Continue observing
        # so a terminal failure still retains its ID.
        unresolved = {"authentication_unresolved": True} if authentication_expected else None
        if dispatch_tab is None:
            return unresolved
        if vipps_expected_total is not None:
            if type(vipps_expected_total) is not int or vipps_expected_total < 0:
                raise HouseholdError("The Oda/Vipps payment amount is invalid")
            context = self._complete_oda_vipps_request(dispatch_tab, vipps_expected_total, before_vipps_request)
            return {"vipps_request_sent": True, "vipps_request_context": context}
        context = None
        read_failures = 0
        store_path = urlsplit(_retail_store_url(self.checkout_provider)).path
        for _ in range(60):
            try:
                page = self._checkout_payment_observation(dispatch_tab)
                if not page:
                    break
                parsed = urlsplit(page["url"])
                if parsed.path == store_path + "checkout/success/":
                    return None
                observed = page.get("authentication_context")
                if observed:
                    if context is not None and context != observed:
                        break
                    context = observed
                # Recovery starts on a failure page that can remain visible
                # after the click, before the new payment redirects to 3DS.
                if (capture_failure and self.checkout_provider == "mathem"
                        and parsed.path == store_path + "checkout/retry/" and page.get("failed") is True):
                    params = parse_qs(parsed.query, keep_blank_values=True)
                    if order_id is None:
                        orders = params.get("orderNumber", [])
                        if (not parsed.fragment and set(params) == {"orderNumber"} and len(orders) == 1
                                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", orders[0])):
                            return {"payment_failed": True, "order_id": orders[0]}
                        break
                    change_ids = params.get("orderChangeId", [])
                    if (parsed.fragment or set(params) != {"orderNumber", "orderChangeId"}
                            or params["orderNumber"] != [order_id] or len(change_ids) != 1
                            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", change_ids[0]) is None):
                        break
                    return {"payment_failed": True, "order_id": order_id, "order_change_id": change_ids[0]}
                self._settle(0.5)
            except HouseholdError:
                read_failures += 1
                if read_failures > 1:
                    break
                try:
                    self._settle(0.5)
                except HouseholdError:
                    break
        return {"authentication_context": context} if context else unresolved

    def submit_checkout(self, cart: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, deadline: float | None = None, before_vipps_request=None):
        vipps = review.get("payment_choice", {}).get("method") == "vipps"
        with self._checkout_operation(deadline, preserve_session=vipps):
            return self._submit_checkout(cart, review, before_click, before_vipps_request=before_vipps_request)

    def submit_order_change(self, cart: Mapping[str, Any], order_id: str, order: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, deadline: float | None = None) -> None:
        with self._checkout_operation(deadline):
            try:
                binding = require_order_binding(review.get("binding"))
                current = self.review_order_change(cart, order_id, order, expected_binding=binding)
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            if current != dict(review):
                raise CheckoutPreconditionError("Oda order change changed after confirmation")
            expected_cart = self._cart_expectation(self._order_cart(cart, order_id, order, binding))
            return self._click_checkout_submit(
                expected_cart["total_minor"],
                f"{CHECKOUT_URL}?orderNumber={order_id}",
                before_click,
                expected_product_count=expected_cart["product_count"],
                expected_amounts=review.get("amounts"),
                review_surface=(_oda_checkout_surface_script(expected_cart), review["surface"]),
                addition_expectation=self._addition_expectation(cart, order_id, order, binding),
            )

    def _navigate_delivery_change(self, order_id, expected, slot):
        mathem = self.checkout_provider == "mathem"
        provider_name = "Mathem" if mathem else "Oda"
        home = _retail_store_url(self.checkout_provider)
        menu_label = "Visa möjliga åtgärder" if mathem else "Vis mulige handlinger"
        entry_label = "Ändra leveranstid" if mathem else "Endre leveringstid"
        dialog_label = "Ändra leveranstid" if mathem else "Bytt leveringstid"
        zone = ZoneInfo("Europe/Stockholm" if mathem else "Europe/Oslo")
        # The existing-order route can already retain the selected review. Open
        # its exact URL first; never use the new-order cart destination.
        self._open(expected["checkout_url"])
        for _ in range(20):
            surface = self._eval((self._checkout_surface_script(expected) if mathem else _oda_delivery_change_surface_script(expected["checkout_url"])))
            if checkout_delivery_matches(expected["delivery_text"], surface.get("delivery_roots"), provider=self.checkout_provider):
                return
            self._settle(0.25)
        self._open(home)
        menu_script = r"""(() => {
const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
if(location.href!==HOME)return JSON.stringify({opened:false});
const links=[...document.querySelectorAll('a')].filter(vis).filter(e=>e.href===ORDER_URL);if(links.length!==1)return JSON.stringify({opened:false});
const card=links[0].closest('article');if(!card)return JSON.stringify({opened:false});
const buttons=[...card.querySelectorAll('button')].filter(vis).filter(e=>!e.disabled&&norm(e.innerText||e.getAttribute('aria-label'))===MENU_LABEL&&e.getAttribute('aria-haspopup')==='menu'&&e.getAttribute('aria-expanded')==='false');
if(buttons.length!==1)return JSON.stringify({opened:false});buttons[0].setAttribute('data-retail-delivery-menu','');return JSON.stringify({opened:true});
})()""".replace("ORDER_URL", json.dumps(self._order_url(order_id))).replace("HOME", json.dumps(home)).replace("MENU_LABEL", json.dumps(menu_label))
        for _ in range(20):
            if self._eval(menu_script) == {"opened": True}:
                break
            self._settle(0.25)
        else:
            raise HouseholdError(f"{provider_name} does not expose delivery changes for this order")
        self._invoke("click", "[data-retail-delivery-menu]")
        entry_script = r"""(() => {
const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
if(location.href!==HOME)return JSON.stringify({bound:false});
const menus=[...document.querySelectorAll('[role="menu"]')].filter(vis);if(menus.length!==1)return JSON.stringify({bound:false});
const links=[...menus[0].querySelectorAll('a[role="menuitem"]')].filter(vis).filter(e=>norm(e.innerText)===ENTRY_LABEL);if(links.length!==1)return JSON.stringify({bound:false});
const u=new URL(links[0].href);if(u.origin!==ORIGIN||u.pathname!==CHECKOUT_PATH||u.hash||u.username||u.password||u.searchParams.get('orderNumber')!==ORDER_ID||JSON.stringify([...u.searchParams.keys()].sort())!==JSON.stringify(['modal','modal-id','modal-screen','orderNumber']))return JSON.stringify({bound:false});
links[0].setAttribute('data-retail-delivery-entry','');return JSON.stringify({bound:true,url:u.href});
})()""".replace("ORDER_ID", json.dumps(order_id)).replace("HOME", json.dumps(home)).replace("ENTRY_LABEL", json.dumps(entry_label)).replace("ORIGIN", json.dumps(home.rstrip("/").rsplit("/", 1)[0])).replace("CHECKOUT_PATH", json.dumps("/se/checkout/confirm/" if mathem else "/no/checkout/confirm/"))
        for _ in range(20):
            entry = self._eval(entry_script)
            if entry.get("bound"):
                break
            self._settle(0.25)
        else:
            raise HouseholdError(f"{provider_name} delivery-change destination cannot be bound to this order")
        self._invoke("click", "[data-retail-delivery-entry]")
        start = datetime.fromisoformat(slot["start_at"].replace("Z", "+00:00")).astimezone(zone)
        end = datetime.fromisoformat(slot["end_at"].replace("Z", "+00:00")).astimezone(zone)
        today = datetime.now(zone).date()
        if start.date() < today or (start.date() - today).days > 31 or start.minute or end.minute:
            raise HouseholdError(f"{provider_name} delivery date cannot be identified in the visible calendar")
        if start.date() == today:
            header = "i dag"
        elif (start.date() - today).days == 1:
            header = "i morgon" if mathem else "i morgen"
        else:
            weekdays = ("mån", "tis", "ons", "tors", "fre", "lör", "sön") if mathem else ("man", "tir", "ons", "tor", "fre", "lør", "søn")
            months = ("jan", "feb", "mars", "apr", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec") if mathem else ("jan", "feb", "mars", "apr", "mai", "juni", "juli", "aug", "sep", "okt", "nov", "des")
            header = (f"{weekdays[start.weekday()]} {start.day} {months[start.month - 1]}." if mathem else
                      f"{weekdays[start.weekday()]}. {start.day}. {months[start.month - 1]}.")
        slot_script = r"""(() => {
const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
if(location.href!==EXPECTED_URL)return JSON.stringify({ready:false});
const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(vis);if(dialogs.length!==1)return JSON.stringify({ready:false});const d=dialogs[0];
if(![...d.querySelectorAll('h1,h2,h3,h4')].some(e=>norm(e.innerText)===DIALOG_LABEL))return JSON.stringify({ready:false});
const tables=[...d.querySelectorAll('table')].filter(vis);if(tables.length!==1)return JSON.stringify({ready:false});const table=tables[0];
const headers=[...table.querySelectorAll('th')];const wanted=headers.filter(e=>norm(e.innerText)===HEADER);if(wanted.length!==1)return JSON.stringify({ready:false});
const index=wanted[0].cellIndex;const rows=[...table.querySelectorAll('tr')].filter(r=>r.cells.length>index&&norm(r.cells[0].innerText)===TIME_ROW);if(rows.length!==1)return JSON.stringify({ready:false});
const buttons=[...rows[0].cells[index].querySelectorAll('button')].filter(vis).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&new RegExp(PRICE_PATTERN).test(norm(e.innerText)));if(buttons.length!==1)return JSON.stringify({ready:false});
buttons[0].setAttribute('data-retail-delivery-slot','');return JSON.stringify({ready:true});
})()""".replace("EXPECTED_URL", json.dumps(entry["url"])).replace("HEADER", json.dumps(header)).replace("TIME_ROW", json.dumps(f"{start.hour:02d} - {end.hour:02d}")).replace("DIALOG_LABEL", json.dumps(dialog_label)).replace("PRICE_PATTERN", json.dumps(r"^\d+(?:[ .]\d{3})*(?:,\d{2})?\s*kr$" if mathem else r"^kr\s*\d+(?:[ .]\d{3})*(?:,\d{2})?$"))
        for _ in range(20):
            if self._eval(slot_script) == {"ready": True}:
                break
            self._settle(0.25)
        else:
            raise HouseholdError(f"{provider_name} does not expose that exact delivery window in the visible calendar")
        self._invoke("click", "[data-retail-delivery-slot]")
        # No repeated selection when a response is lost or the page fails to
        # advance. The caller must inspect the original selection before retry.
        for _ in range(30):
            if self._eval("JSON.stringify({ready:location.href===" + json.dumps(expected["checkout_url"]) + "&&!document.querySelector('[role=dialog]')})") == {"ready": True}:
                return
            self._settle(0.25)
        raise HouseholdError(f"{provider_name} delivery selection has not reached review; inspect it before selecting again")

    def review_delivery_change(self, order_id: str, order: Mapping[str, Any], delivery: Mapping[str, Any], *, deadline: float | None = None, expected_binding=None) -> dict[str, Any]:
        with self._checkout_operation(deadline):
            binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=expected_binding)
            expected = self._delivery_change_expectation(order_id, order, delivery, binding)
            self._navigate_delivery_change(order_id, expected, delivery["slot"])
            for _ in range(20):
                surface = self._eval(_oda_delivery_change_surface_script(expected["checkout_url"]))
                if surface.get("action") == "ready":
                    break
                self._settle(0.25)
            return self._delivery_change_review(order_id, order, delivery, surface, binding)

    def _delivery_change_expectation(self, order_id, order, delivery, binding):
        slot = validate_delivery_slot(delivery.get("slot"))
        if not slot["slot_ref"].startswith(self.checkout_provider + ":") or slot["provider_slot_id"] != delivery.get("slot_id"):
            raise HouseholdError("Delivery change does not identify the requested provider window")
        original = self._order_expectation(order_id, order)
        if (order.get("currency") != ("SEK" if self.checkout_provider == "mathem" else "NOK")
                or delivery_signature(str(delivery.get("display") or ""), provider=self.checkout_provider) is None):
            raise HouseholdError("Original order currency or requested delivery is unavailable")
        return {"order_id": order_id, "checkout_url": self.checkout_url + "?orderNumber=" + quote(order_id, safe=""),
                "lines": [], "product_count": 0, "original_minor": original["total_minor"],
                "original_count": self._order_product_count(order), "delivery_change": True,
                "delivery_text": delivery["display"], "delivery_address": binding["receipt_address"]}

    def _delivery_change_review(self, order_id, order, delivery, surface, binding):
        binding = require_order_binding(binding)
        expected_order = self._order_expectation(order_id, order)
        expected = self._delivery_change_expectation(order_id, order, delivery, binding)
        target = str(delivery.get("display") or "")
        if surface.get("action") != "ready":
            raise HouseholdError("Oda prepared delivery review is unavailable; prepare it again")
        amounts = surface.get("amounts")
        roots = surface.get("delivery_roots")
        payment_display = str(surface.get("payment_display") or "")
        if not isinstance(amounts, list) or len(amounts) != 1 or not checkout_delivery_matches(target, roots) or re.fullmatch(r"•••• \d{4}", payment_display) is None or surface.get("submit_controls") != 1:
            raise HouseholdError("Oda delivery change review does not match the requested slot")
        self._expand_checkout_amount_summary()
        observed = self._eval(_retail_addition_amount_script(expected, provider=self.checkout_provider))
        if observed.get("amounts_valid") is not True or observed["order_amounts"]["payable_minor"] != amounts[0]:
            raise HouseholdError("Oda delivery change has no verified original, final and payable totals")
        order_amounts = observed["order_amounts"]
        checkout_amounts = {key: None for key in ODA_CHECKOUT_AMOUNT_KEYS}
        checkout_amounts["provider_total"] = amounts[0] / 100
        summary = {
            "items": [],
            "count": 0,
            "total": amounts[0] / 100,
            "delivery": {"slot_id": delivery.get("slot_id"), "display": target, "address": binding["receipt_address"]},
            "payment": payment_display,
            "amounts": checkout_amounts,
            "order_amounts": order_amounts,
        }
        return {"binding": binding, "page_digest": hashlib.sha256(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), "summary": summary, "order_amounts": order_amounts, "target_order_id": order_id, "before_delivery": expected_order["delivery_text"], "surface": surface}

    def submit_delivery_change(self, order_id: str, order: Mapping[str, Any], delivery: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, deadline: float | None = None) -> None:
        with self._checkout_operation(deadline):
            try:
                binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=require_order_binding(review.get("binding")))
                expected_url = f"{CHECKOUT_URL}?orderNumber={order_id}"
                surface_script = _oda_delivery_change_surface_script(expected_url)
                # Each operation starts a fresh browser process. Reopen only
                # the retained review; never repeat order-menu or slot actions.
                self._open(expected_url)
                for _ in range(20):
                    surface = self._eval(surface_script)
                    if surface.get("action") == "ready":
                        break
                    self._settle(0.25)
                current = self._delivery_change_review(order_id, order, delivery, surface, binding)
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            if current != dict(review):
                raise CheckoutPreconditionError("Oda delivery change changed after confirmation")
            return self._click_checkout_submit(
                int(round(float(review["summary"]["total"]) * 100)),
                f"{CHECKOUT_URL}?orderNumber={order_id}",
                before_click,
                expected_product_count=self._order_product_count(order),
                expected_amounts=review["summary"].get("amounts"),
                review_surface=(surface_script, review["surface"]),
                addition_expectation={**self._delivery_change_expectation(order_id, order, delivery, binding),
                                      "order_amounts": review["order_amounts"]},
                authentication_expected=review["order_amounts"]["payable_minor"] > 0,
            )

    def _submit_checkout(self, cart: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, before_vipps_request=None) -> None:
        if (review.get("payment_choice", {}).get("method") == "vipps"
                and re.fullmatch(r"\d{8}", str(self.vipps_phone_number or "")) is None):
            raise CheckoutPreconditionError("An exact private Vipps phone number is required before Oda checkout")
        try:
            current = (self._review_checkout(cart, payment=review["payment_choice"], select_payment=True)
                       if "payment_choice" in review else self.review_checkout(cart))
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        if current != dict(review):
            raise CheckoutPreconditionError("Oda checkout changed after confirmation")
        expected_cart = self._cart_expectation(cart)
        try:
            reference = self._account_reference(expected_cart["delivery_address"])
            if hashlib.sha256(str(reference).encode()).hexdigest() != review.get("account_reference_digest"):
                raise HouseholdError("Oda selected account changed before the final click")
            self._require_checkout_time(FINAL_CLICK_MARGIN)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        return self._click_checkout_submit(
            expected_cart["total_minor"],
            CHECKOUT_URL,
            before_click,
            expected_product_count=expected_cart["product_count"],
            expected_amounts=review.get("amounts"),
            review_surface=(_oda_checkout_surface_script(expected_cart, review.get("payment_choice")), review["surface"]),
            authentication_expected=review.get("payment_choice", {}).get("method", "saved_card") == "saved_card",
            before_vipps_request=before_vipps_request,
        )

    def _click_checkout_submit(
        self,
        expected_total: int,
        expected_url: str,
        before_click: Callable[[], None] | None = None,
        *,
        expected_product_count: int,
        expected_amounts: Any,
        review_surface: tuple[str, Mapping[str, Any]] | None = None,
        addition_expectation: Mapping[str, Any] | None = None,
        authentication_expected: bool = True,
        before_vipps_request=None,
    ) -> None:
        try:
            self._require_checkout_time(FINAL_CLICK_MARGIN)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        dispatch_tab = self._checkout_dispatch_tab()
        vipps = review_surface is not None and review_surface[1].get("payment_display") == "Vipps"
        if vipps:
            self._invoke("network", "requests", "--clear")
        if before_click:
            try:
                before_click()
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
        try:
            self._require_checkout_time(FINAL_CLICK_MARGIN)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        if addition_expectation is not None:
            # Existing-order reviews bind their own original/final totals and
            # signed adjustment; new-order fee validation does not apply.
            script = _retail_addition_amount_script(addition_expectation, submit=True, provider=self.checkout_provider)
        else:
            try:
                amounts_minor = _oda_checkout_amounts_minor(expected_amounts)
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            script = _oda_checkout_amount_script(
                expected_total,
                expected_product_count=expected_product_count,
                expected_amounts=amounts_minor,
                expected_url=expected_url,
                vipps=vipps,
            )
        if review_surface is not None:
            surface_script, expected_surface = review_surface
            # The review and amount checks run in the same browser turn as the
            # only click, after before_click's fresh provider and expiry checks.
            script = ("(() => {const actual=JSON.parse(" + surface_script.strip()
                      + "),expected=" + json.dumps(expected_surface, ensure_ascii=False)
                      + ";if(Object.keys(actual).length!==Object.keys(expected).length||"
                      + "Object.keys(expected).some(k=>JSON.stringify(actual[k])!==JSON.stringify(expected[k])))"
                      + "return JSON.stringify({clicked:false});return " + script.strip() + ";})()")
        if self._eval(script) != {"clicked": True}:
            raise CheckoutPreconditionError("Oda checkout button changed before click")
        return self._capture_checkout_payment(
            dispatch_tab,
            authentication_expected=authentication_expected,
            vipps_expected_total=(expected_total if vipps else None),
            before_vipps_request=before_vipps_request,
        )

    def review_cancellation(self, order_id: str, order: Mapping[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        with self._cancellation_operation(deadline):
            return self._review_cancellation(order_id, order)

    def _review_cancellation(self, order_id: str, order: Mapping[str, Any], *, expected_binding=None) -> dict[str, Any]:
        binding = self._read_order_binding(order_id, order, deadline=self._cancellation_deadline, expected_binding=expected_binding)
        url = self._order_url(order_id)
        self._open_order(order_id)
        expected = self._order_expectation(order_id, order)
        for _ in range(20):
            result = self._eval(r"""
(() => {
 const expected=EXPECTED;
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const hasMoney=value=>/\b\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|NOK)\b/i.test(norm(value))||/\b(?:kr|NOK)[,\s]*\d+(?:[ .]\d{3})*,\d{2}\b/i.test(norm(value));
 if(location.href!==URL)return JSON.stringify({available:false,reason:'Ordredetaljene avviker'});
 const roots=[...document.querySelectorAll('main')].filter(visible);
 if(roots.length!==1)return JSON.stringify({available:false,reason:'Ordredetaljene avviker'});
 const root=roots[0];
 const lines=(root.innerText||'').split(/\n+/).map(norm).filter(Boolean);
 if(!lines.includes(`Bestilling ${expected.order_id}`)&&!lines.includes(`Order ${expected.order_id}`))return JSON.stringify({available:false,reason:'Ordredetaljene avviker'});
 const deliveryLines=lines.filter(x=>/\b\d{1,2}\.?\s*(?:jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\b/i.test(x)&&/\b\d{1,2}(?::\d{2})?\s*(?:-|–|og|til)\s*\d{1,2}(?::\d{2})?(?![:.]\d)\b/i.test(x));
 const totalPattern=/^(?:Total|Totalt)(?: inkl\.? MVA)?$/i;
 const totalLabels=[...root.querySelectorAll('*')].filter(visible).filter(x=>totalPattern.test(norm(x.innerText||''))).filter(x=>![...x.children].some(child=>visible(child)&&totalPattern.test(norm(child.innerText||''))));
 const totalRows=totalLabels.map(label=>{let row=label.parentElement;while(row&&row!==root){const text=norm(row.innerText||'');if(hasMoney(text))return text;row=row.parentElement;}return null;}).filter(Boolean);
 if(totalLabels.length!==1||totalRows.length!==1)return JSON.stringify({available:false,reason:'Ordredetaljene avviker'});
 document.querySelectorAll('[data-oda-household-cancel-review]').forEach(x=>x.removeAttribute('data-oda-household-cancel-review'));
 const buttons=[...root.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>/^(Kanseller bestillingen|Avbestill bestillingen|Cancel order)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(buttons.length!==1)return JSON.stringify({available:false,reason:'Oda tilbyr ikke avbestilling nå'});
 if(!Object.keys(buttons[0]).some(key=>key.startsWith('__reactProps')))return JSON.stringify({available:false,retry:true,reason:'Oda-siden er ikke klar'});
 buttons[0].setAttribute('data-oda-household-cancel-review','');
 const marked=[...document.querySelectorAll('[data-oda-household-cancel-review]')];
 if(marked.length!==1||marked[0]!==buttons[0]||!visible(marked[0])){buttons[0].removeAttribute('data-oda-household-cancel-review');return JSON.stringify({available:false,reason:'Oda tilbyr ikke avbestilling nå'});}
 return JSON.stringify({available:true,delivery_lines:deliveryLines,total_rows:totalRows});
})()
""".replace("EXPECTED", json.dumps(expected, ensure_ascii=False, separators=(",", ":"))).replace("URL", json.dumps(url)), browser_args=CANCELLATION_BROWSER_ARGS)
            if result.get("retry") is not True:
                break
            self._settle_cancellation(0.5)
        else:
            return {"available": False, "reason": "Oda-siden ble ikke ferdig lastet"}
        if (
            result.get("available") is not True
            or not cancellation_delivery_matches(expected["delivery_text"], result.get("delivery_lines"))
            or not cancellation_total_matches(expected["total_minor"], result.get("total_rows"))
        ):
            return {"available": False, "reason": str(result.get("reason") or "Oda tilbyr ikke avbestilling nå")}
        self._invoke("click", "[data-oda-household-cancel-review]", browser_args=CANCELLATION_BROWSER_ARGS)
        for _ in range(20):
            dialog = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
 if(dialogs.length!==1)return JSON.stringify({available:false,consequence:null});
 const root=dialogs[0];
 const text=norm(root.innerText||'');
 const final=[...root.querySelectorAll('button')].filter(enabled).filter(x=>/^(Kanseller bestillingen min|Avbestill bestillingen|Confirm cancellation)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 const dismiss=[...root.querySelectorAll('button')].filter(enabled).filter(x=>/^(Nei, ikke kanseller|Ikke avbestill|Do not cancel)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 document.querySelectorAll('[data-oda-household-cancel-dismiss]').forEach(x=>x.removeAttribute('data-oda-household-cancel-dismiss'));
 if(final.length===1&&dismiss.length===1&&final[0]!==dismiss[0])dismiss[0].setAttribute('data-oda-household-cancel-dismiss','');
 const marked=[...document.querySelectorAll('[data-oda-household-cancel-dismiss]')];
 const fees=(text.match(/[^.]{0,80}(?:gebyr|fee)[^.]{0,80}/i)||[])[0]||null;
 return JSON.stringify({available:marked.length===1&&marked[0]===dismiss[0]&&enabled(marked[0]),consequence:fees});
})()
""", browser_args=CANCELLATION_BROWSER_ARGS)
            if dialog.get("available") is True:
                break
            self._settle_cancellation(0.25)
        else:
            return {"available": False, "reason": "Oda tilbyr ikke avbestilling nå"}
        if dialog.get("available") is not True:
            return {"available": False, "reason": "Oda tilbyr ikke avbestilling nå"}
        self._invoke("click", "[data-oda-household-cancel-dismiss]", browser_args=CANCELLATION_BROWSER_ARGS)
        for _ in range(20):
            closed = self._eval(r"""
(() => {
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 return JSON.stringify({closed:![...document.querySelectorAll('[role="dialog"]')].some(visible)});
})()
""", browser_args=CANCELLATION_BROWSER_ARGS)
            if closed == {"closed": True}:
                break
            self._settle_cancellation(0.25)
        return {**dialog, "binding": binding} if closed == {"closed": True} else {"available": False, "reason": "Oda tilbyr ikke avbestilling nå"}

    def submit_cancellation(self, order_id: str, order: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, deadline: float | None = None) -> None:
        operation = None
        try:
            with self._cancellation_operation(deadline) as operation:
                self._submit_cancellation(order_id, order, review, operation, before_click)
        except HouseholdError as exc:
            if operation is None or operation["final_dispatched"] is not True:
                raise CancellationPreconditionError(str(exc)) from exc
            raise

    def _submit_cancellation(self, order_id: str, order: Mapping[str, Any], review: Mapping[str, Any], operation: dict[str, bool], before_click: Callable[[], None] | None = None) -> None:
        if self._review_cancellation(order_id, order, expected_binding=require_order_binding(review.get("binding"))) != dict(review):
            raise HouseholdError("Oda tilbyr ikke avbestilling nå")
        ready = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 if(location.href!==URL)return JSON.stringify({ready:false});
 const roots=[...document.querySelectorAll('main')].filter(visible);
 if(roots.length!==1)return JSON.stringify({ready:false});
 const lines=(roots[0].innerText||'').split(/\n+/).map(norm).filter(Boolean);
 if(!lines.includes(`Bestilling ${ORDER}`)&&!lines.includes(`Order ${ORDER}`))return JSON.stringify({ready:false});
 document.querySelectorAll('[data-oda-household-cancel-submit-open]').forEach(x=>x.removeAttribute('data-oda-household-cancel-submit-open'));
 const buttons=[...roots[0].querySelectorAll('button')].filter(enabled).filter(x=>/^(Kanseller bestillingen|Avbestill bestillingen|Cancel order)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(buttons.length!==1)return JSON.stringify({ready:false});
 buttons[0].setAttribute('data-oda-household-cancel-submit-open','');
 const marked=[...document.querySelectorAll('[data-oda-household-cancel-submit-open]')];
 return JSON.stringify({ready:marked.length===1&&marked[0]===buttons[0]&&enabled(marked[0])});
})()
""".replace("URL", json.dumps(self._order_url(order_id))).replace("ORDER", json.dumps(order_id)), browser_args=CANCELLATION_BROWSER_ARGS)
        if ready != {"ready": True}:
            raise HouseholdError("Oda cancellation control changed")
        self._invoke("click", "[data-oda-household-cancel-submit-open]", browser_args=CANCELLATION_BROWSER_ARGS)
        final = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
 if(dialogs.length!==1)return JSON.stringify({ready:false});
 const root=dialogs[0];
 const confirm=[...root.querySelectorAll('button')].filter(enabled).filter(x=>/^(Kanseller bestillingen min|Avbestill bestillingen|Confirm cancellation)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 const dismiss=[...root.querySelectorAll('button')].filter(enabled).filter(x=>/^(Nei, ikke kanseller|Ikke avbestill|Do not cancel)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 document.querySelectorAll('[data-oda-household-cancel-submit-final]').forEach(x=>x.removeAttribute('data-oda-household-cancel-submit-final'));
 if(confirm.length!==1||dismiss.length!==1||confirm[0]===dismiss[0])return JSON.stringify({ready:false});
 confirm[0].setAttribute('data-oda-household-cancel-submit-final','');
 const marked=[...document.querySelectorAll('[data-oda-household-cancel-submit-final]')];
 return JSON.stringify({ready:marked.length===1&&marked[0]===confirm[0]&&enabled(marked[0])});
})()
""", browser_args=CANCELLATION_BROWSER_ARGS)
        if final != {"ready": True}:
            raise HouseholdError("Oda cancellation confirmation changed")
        self._require_cancellation_time(FINAL_CLICK_MARGIN)
        if before_click:
            before_click()
        self._require_cancellation_time(FINAL_CLICK_MARGIN)
        operation["final_dispatched"] = True
        self._invoke("click", "[data-oda-household-cancel-submit-final]", browser_args=CANCELLATION_BROWSER_ARGS)

    def close(self) -> None:
        self._invoke("close", check=False)

    @contextmanager
    def _cancellation_operation(self, deadline: float | None = None):
        previous = getattr(self, "_cancellation_deadline", None)
        outer = previous is None
        if outer:
            self._cancellation_deadline = deadline if deadline is not None else time.monotonic() + CANCELLATION_BROWSER_TIMEOUT
        elif deadline is not None:
            self._cancellation_deadline = min(previous, deadline)
        operation = {"final_dispatched": False}
        started = False
        try:
            if outer:
                self._invoke("close", browser_args=CANCELLATION_BROWSER_ARGS)
                self._clear_cancellation_cache()
            started = True
            yield operation
        finally:
            try:
                if outer and started and operation["final_dispatched"] is not True:
                    self._invoke("close", browser_args=CANCELLATION_BROWSER_ARGS)
            finally:
                self._cancellation_deadline = previous

    def _clear_cancellation_cache(self) -> None:
        profile = getattr(self, "profile", None)
        if profile is None:
            return
        if os.geteuid() != 0 or self.uid == 0:
            clear_cancellation_cache(profile)
            return

        def drop_privileges() -> None:
            os.setgroups([])
            os.setgid(self.gid)
            os.setuid(self.uid)

        deadline = getattr(self, "_cancellation_deadline", None)
        remaining = 30.0 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise HouseholdError("Oda browser deadline reached")
        try:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--clear-cancellation-cache", str(profile)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(30.0, remaining),
                check=False,
                preexec_fn=drop_privileges,
                env={"LANG": "C.UTF-8", "PATH": os.environ.get("PATH", os.defpath)},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HouseholdError("Oda browser cache cannot be reset") from exc
        if completed.returncode != 0:
            raise HouseholdError("Oda browser cache cannot be reset")

    @contextmanager
    def _checkout_operation(self, deadline: float | None = None, *, preserve_session: bool = False):
        previous = getattr(self, "_checkout_deadline", None)
        if previous is None:
            self._checkout_deadline = deadline if deadline is not None else time.monotonic() + CHECKOUT_BROWSER_TIMEOUT
        elif deadline is not None:
            self._checkout_deadline = min(previous, deadline)
        try:
            if previous is None and not preserve_session:
                self._invoke("close", browser_args=DEFAULT_BROWSER_ARGS)
            yield
        finally:
            self._checkout_deadline = previous

    def _require_checkout_time(self, minimum: float = 0) -> None:
        deadline = getattr(self, "_checkout_deadline", None)
        if deadline is not None and deadline - time.monotonic() < minimum:
            raise HouseholdError("Oda checkout browser deadline reached")

    def _require_cancellation_time(self, minimum: float = 0) -> None:
        deadline = getattr(self, "_cancellation_deadline", None)
        if deadline is not None and deadline - time.monotonic() < minimum:
            raise HouseholdError("Oda cancellation browser deadline reached")

    def _settle(self, seconds: float) -> None:
        self._require_checkout_time(seconds)
        self._require_cancellation_time(seconds)
        time.sleep(seconds)

    def _settle_cancellation(self, seconds: float) -> None:
        self._require_cancellation_time(seconds)
        time.sleep(seconds)

    @staticmethod
    def _order_url(order_id: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", order_id) is None:
            raise HouseholdError("invalid order identity")
        return f"https://oda.com/no/account/orders/{order_id}/"

    def _open_order(self, order_id: str) -> None:
        expected = self._order_url(order_id)
        data = self._invoke("open", f"https://oda.com/no/orders/{order_id}/")
        if str(data.get("url") or "").rstrip("/") != expected.rstrip("/"):
            raise HouseholdError("Oda browser left the requested order page")

    @staticmethod
    def _order_expectation(order_id: str, order: Mapping[str, Any]) -> dict[str, Any]:
        actual_id = str(order.get("orderNumber") or order.get("order_number") or order.get("id") or "")
        if actual_id != order_id:
            raise HouseholdError("Oda order identity changed")
        total = order.get("grossAmount", order.get("subtotal", order.get("total")))
        if isinstance(total, bool):
            raise HouseholdError("Oda order total is unavailable")
        try:
            total_value = float(total)
            if not math.isfinite(total_value):
                raise ValueError
            total_minor = int(round(total_value * 100))
            total_text = f"{total_value:.2f}".replace(".", ",")
        except (TypeError, ValueError, OverflowError) as exc:
            raise HouseholdError("Oda order total is unavailable") from exc
        delivery = next((order[key] for key in ("deliverySlotDisplay", "deliveryDate", "delivery_date") if key in order), "")
        if not isinstance(delivery, str) or not delivery.strip():
            raise HouseholdError("Oda order delivery is unavailable")
        return {"order_id": order_id, "total_text": total_text, "total_minor": total_minor, "delivery_text": delivery}

    @staticmethod
    def _order_product_count(order: Mapping[str, Any]) -> int:
        products = order.get("products")
        if not isinstance(products, list) or not products:
            raise HouseholdError("Retail original order products are unavailable")
        quantities = [item.get("quantity") if isinstance(item, Mapping) else None for item in products]
        if any(isinstance(q, bool) or not isinstance(q, (int, float)) or not math.isfinite(q) or q != int(q) or not 0 < q <= 1000000 for q in quantities):
            raise HouseholdError("Retail original order quantities are unavailable")
        count = sum(int(q) for q in quantities)
        if count > 1_000_000:
            raise HouseholdError("Retail original order product count is unavailable")
        supplied = order.get("productQuantityCount", count)
        if isinstance(supplied, bool) or supplied != count:
            raise HouseholdError("Retail original order product count changed")
        return count

    @staticmethod
    def _cart_expectation(cart: Mapping[str, Any]) -> dict[str, Any]:
        summary = cart_summary(cart)
        if not summary["items"]:
            raise HouseholdError("cart is empty")
        raw_lines = []
        groups = cart.get("groups")
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, Mapping) and isinstance(group.get("items"), list):
                    raw_lines.extend(group["items"])
        elif isinstance(cart.get("items"), list):
            raw_lines = list(cart["items"])
        if len(raw_lines) != len(summary["items"]):
            raise HouseholdError("cart line identity is unavailable")
        lines = []
        for item, raw in zip(summary["items"], raw_lines, strict=True):
            name = item["name"].strip()
            quantity = item["quantity"]
            if not name or not isinstance(raw, Mapping):
                raise HouseholdError("cart line is incomplete")
            product = raw.get("product") if isinstance(raw.get("product"), Mapping) else raw
            raw_name = product.get("name")
            description = product.get("description", "")
            brand = product.get("brand", "")
            if brand is None:
                brand = ""
            if not isinstance(raw_name, str) or not isinstance(description, str) or not isinstance(brand, str):
                raise HouseholdError("cart line identity is invalid")
            if raw_name.strip() != name:
                raise HouseholdError("cart line identity changed")
            identity = product_identity(name, description.strip(), brand.strip())
            if not identity:
                raise HouseholdError("cart line identity is unavailable")
            lines.append({"name": name, "identity": identity, "quantity": quantity})
        product_count = sum(line["quantity"] for line in lines)
        provider_count = summary.get("count")
        if (
            isinstance(provider_count, bool)
            or not isinstance(provider_count, (int, float))
            or not math.isfinite(float(provider_count))
            or not float(provider_count).is_integer()
            or int(provider_count) != product_count
        ):
            raise HouseholdError("Oda cart product count changed")
        delivery = summary.get("delivery") or {}
        address = delivery.get("address")
        if not isinstance(address, str) or not address.strip():
            raise HouseholdError("Oda checkout delivery address is unavailable")
        return {
            "lines": lines,
            "product_count": product_count,
            "total_text": f"{summary['total']:.2f}".replace(".", ","),
            "total_minor": int(round(summary["total"] * 100)),
            "delivery_text": str(delivery.get("display") or ""),
            "delivery_address": unicodedata.normalize("NFC", " ".join(address.split())),
        }

    def _open(self, url: str) -> None:
        data = self._invoke("open", url)
        if str(data.get("url") or "").rstrip("/") != url.rstrip("/"):
            raise HouseholdError("Oda browser left the requested page")

    def _eval(self, script: str, *, browser_args: str = DEFAULT_BROWSER_ARGS) -> dict[str, Any]:
        data = self._invoke("eval", "--stdin", stdin=script, browser_args=browser_args)
        raw = data.get("result")
        if not isinstance(raw, str):
            raise HouseholdError("Oda browser returned no result")
        try:
            value = json.loads(raw)
            if isinstance(value, str):
                value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise HouseholdError("Oda browser returned malformed data") from exc
        if not isinstance(value, dict):
            raise HouseholdError("Oda browser result is invalid")
        return value

    def _invoke(self, *arguments: str, stdin: str | None = None, check: bool = True, browser_args: str = DEFAULT_BROWSER_ARGS) -> dict[str, Any]:
        # Shared binding may be the first open in a cold cancellation session.
        # Chromium's launch flags must match that operation from its first read.
        if getattr(self, "_cancellation_deadline", None) is not None and browser_args == DEFAULT_BROWSER_ARGS:
            browser_args = CANCELLATION_BROWSER_ARGS
        command = [str(self.binary), "--json", "--session", self.session, "--profile", str(self.profile), "--executable-path", str(self.executable), *arguments]
        environment = {
            "AGENT_BROWSER_ARGS": browser_args,
            "AGENT_BROWSER_DEFAULT_TIMEOUT": "60000",
            "AGENT_BROWSER_SOCKET_DIR": str(self.socket_directory),
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", os.defpath),
        }
        for name in ("AGENT_BROWSER_PROXY", "AGENT_BROWSER_PROXY_BYPASS"):
            if value := os.environ.get(name):
                environment[name] = value

        def drop_privileges() -> None:
            os.setgroups([])
            os.setgid(self.gid)
            os.setuid(self.uid)

        timeout = 90.0
        deadlines = [
            value
            for value in (getattr(self, "_checkout_deadline", None), getattr(self, "_cancellation_deadline", None))
            if value is not None
        ]
        if deadlines:
            deadline = min(deadlines)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HouseholdError("Oda browser deadline reached")
            timeout = min(timeout, max(0.1, remaining))
        try:
            completed = subprocess.run(
                command,
                input=stdin,
                stdin=subprocess.DEVNULL if stdin is None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
                check=False,
                preexec_fn=drop_privileges if os.geteuid() == 0 else None,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HouseholdError("Oda browser is unavailable") from exc
        if completed.returncode != 0:
            if not check:
                return {}
            raise HouseholdError("Oda browser operation failed")
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise HouseholdError("Oda browser response is malformed") from exc
        if envelope.get("success") is not True or not isinstance(envelope.get("data"), dict):
            if not check:
                return {}
            raise HouseholdError("Oda browser rejected the operation")
        return envelope["data"]


class MathemBrowser(OdaBrowser):
    """Observed Swedish saved-card checkout in an installation-owned browser.

    Uses the same native browser transport and deadline as Oda. Account identity
    is bound to the selected MCP address reference before each fresh review.
    Existing-order additions and cancellation bind the original receipt account.
    """
    checkout_provider = "mathem"
    checkout_url = "https://www.mathem.se/se/checkout/confirm/"

    def __init__(self, *, provider_client: Any, **arguments):
        super().__init__(provider_client=provider_client, **arguments)
        self.session = f"mathem-household-{self.instance}"


    def _review_checkout(self, cart, *, order_id=None, delivery_text=None):
        if order_id is not None or delivery_text is not None:
            raise HouseholdError("Mathem existing-order changes require the store website")
        expected = self._cart_expectation(cart)
        if delivery_signature(expected["delivery_text"], provider="mathem") is None:
            raise HouseholdError("Select a Mathem delivery window before checkout")
        account_digest = self._verify_checkout_account(expected["delivery_address"])
        self._navigate_to_checkout()
        result = self._review_mathem_surface(expected)
        result["account_reference_digest"] = account_digest
        result["amounts"] = self._read_checkout_amounts(expected["total_minor"], expected["product_count"])
        result["discount_breakdown"] = result["amounts"].pop("discount_breakdown")
        return result

    def _review_mathem_surface(self, expected):
        expanded = set()
        for _ in range(20):
            expansion = self._eval(self._checkout_expand_script(expected, expanded))
            if expansion.get("blocked"):
                raise HouseholdError("Mathem checkout expansion controls changed")
            if expansion.get("ready") is True:
                break
            expanded.update(expansion.get("clicked", []))
            self._settle(0.25)
        else:
            raise HouseholdError("Mathem checkout cannot be expanded for review")
        for attempt in range(20):
            surface = self._eval(self._checkout_surface_script(expected))
            try:
                result = self._checked_surface(expected, surface)
            except HouseholdError:
                if attempt == 19:
                    raise
            else:
                break
            self._settle(0.25)
        return result

    def _checkout_expand_script(self, expected, expanded):
        return r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 if(location.href!==URL)return JSON.stringify({blocked:true});
 const inputs=[...document.querySelectorAll('input[type="number"]')].filter(visible).filter(e=>[...e.labels].some(label=>norm(label.textContent)==='Antal'));
 const texts=[...document.querySelectorAll('*')].filter(visible).map(e=>norm(e.innerText));
 const subtotal=SUMMARY_LABELS.every(label=>texts.includes(label));
 if(inputs.length===LINE_COUNT&&subtotal)return JSON.stringify({ready:true});
 const already=ALREADY_EXPANDED;
 const buttons=[...document.querySelectorAll('button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true');
 const matches=['Visa varor','Visa sammanfattning'].map(label=>({label,found:buttons.filter(e=>norm(e.innerText||e.getAttribute('aria-label')||'')===label)}));
 if(matches.some(entry=>entry.found.length>1))return JSON.stringify({blocked:true});
 const clicked=[];
 for(const {label,found} of matches){
  if(found.length===1&&!already.includes(label)){found[0].click();clicked.push(label);}
 }
 return JSON.stringify({ready:false,clicked});
})()
""".replace("URL", json.dumps(expected.get("checkout_url", self.checkout_url))).replace("LINE_COUNT", str(len(expected["lines"]))).replace("ALREADY_EXPANDED", json.dumps(sorted(expanded))).replace("SUMMARY_LABELS", json.dumps(
            (["Ursprunglig beställning", "Att betala nu", "Totalsumma för beställning"]
             + ([] if expected.get("delivery_change") else ["Varor tillagda i efterhand"]))
            if expected.get("order_id") else ["Delsumma"]))

    def _checkout_surface_script(self, expected):
        return r"""
(() => {
 const expected=EXPECTED;
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const text=norm(document.body?.innerText||'');
 const inputs=[...document.querySelectorAll('input[type="number"]')].filter(visible).filter(e=>[...e.labels].some(label=>norm(label.textContent)==='Antal'));
 const items=inputs.map(input=>({quantity:Number(input.value),text:norm([...(input.closest('li,article')?.querySelectorAll('p')||[])].filter(visible).slice(0,2).map(e=>e.innerText).join(' '))}));
 const delivery=[...document.querySelectorAll('h1,h2,h3,h4')].filter(visible).filter(e=>norm(e.innerText)==='Vi levererar din beställning').map(e=>e.closest('section,article,.k-card')).filter(Boolean);
 const escaped=norm(expected.delivery_address).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
 const address_matches=!!expected.delivery_address&&delivery.length===1&&new RegExp(`(?:^|[\\s,:])${escaped}(?=$|[\\s,])`,'i').test(norm(delivery[0].innerText));
 const payment=JSON.parse(PAYMENT);
 const controls=[...document.querySelectorAll('button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true').filter(e=>/^Bekräfta och betala\s+PAYABLE_SIGN\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|SEK)$/i.test(norm(e.innerText||e.getAttribute('aria-label')||'')));
 return JSON.stringify({url:location.href,authenticated:!document.querySelector('input[type="password"]'),available:!/inte tillgänglig|slut i lager|unavailable/i.test(text),items,delivery_roots:delivery.map(e=>norm(e.innerText)),address_matches,payment_display:payment.verified===true?payment.payment_display:null,submit_controls:controls.length});
})()
""".replace("PAYMENT", _mathem_checkout_payment_script(expected.get("checkout_url", self.checkout_url)).strip()).replace("EXPECTED", json.dumps(expected, ensure_ascii=False)).replace("PAYABLE_SIGN", "[−-]?" if expected.get("delivery_change") else "")

    @staticmethod
    def _checked_surface(expected, surface):
        required = {"url", "authenticated", "available", "items", "delivery_roots", "address_matches", "payment_display", "submit_controls"}
        if not isinstance(surface, Mapping) or set(surface) != required or surface["url"] != expected.get("checkout_url", MathemBrowser.checkout_url):
            raise HouseholdError("Mathem checkout page changed")
        if not all(surface[key] is True for key in ("authenticated", "available", "address_matches")):
            raise OdaCheckoutMismatchError("Mathem account, address or availability does not match the reviewed cart")
        if not checkout_lines_match(expected["lines"], surface["items"]) or not checkout_delivery_matches(expected["delivery_text"], surface["delivery_roots"], provider="mathem"):
            raise OdaCheckoutMismatchError("Mathem checkout products or delivery changed")
        if type(surface["submit_controls"]) is not int or surface["submit_controls"] != 1 or re.fullmatch(r"•••• \d{4}", str(surface["payment_display"] or "")) is None:
            raise HouseholdError("Mathem checkout requires one selected saved card and one payment control")
        return dict(surface)

    def _navigate_to_checkout(self, order_id=None, *, expected=None):
        target_url = expected["checkout_url"] if order_id is not None else self.checkout_url
        # Navigation effects are dispatched once per observed route/control. A
        # lost response stops the operation; subsequent review starts afresh.
        action = self._continue_checkout_cart()
        assert action == "continue"
        dispatched = {"https://www.mathem.se/se/cart/#continue"}
        trace = ["cart:continue-dispatched"]
        for _ in range(60):
            state = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const routes=['https://www.mathem.se/se/','https://www.mathem.se/se/cart/','https://www.mathem.se/se/checkout/','https://www.mathem.se/se/checkout/recommendations/','https://www.mathem.se/se/checkout/confirm/','https://www.mathem.se/se/checkout/modify/',TARGET_URL];
 const route=['storefront','cart','checkout','recommendations','confirm','modify','target'][routes.indexOf(location.href)]||'other';
 const report=action=>JSON.stringify({action,route});
 if(!routes.includes(location.href)||document.querySelector('input[type="password"]'))return report('blocked');
 if(location.href===TARGET_URL)return report('ready');
 if(location.href===routes[4])return report('blocked');
 if(location.href===routes[5])return report('modify');
 document.querySelectorAll('[data-mathem-checkout-next]').forEach(e=>e.removeAttribute('data-mathem-checkout-next'));
 const controls=[...document.querySelectorAll('a,button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true');
 const full=controls.filter(e=>e.tagName==='A'&&norm(e.innerText)==='Fortsätt till varukorgen'&&e.href===routes[1]);
 const next=controls.filter(e=>norm(e.innerText)==='Fortsätt'&&(location.href===routes[1]&&e.tagName==='BUTTON'||location.href===routes[3]&&e.tagName==='A'&&e.href===routes[4]));
 const matches=full.length?full:next;
 if(matches.length>1)return report('blocked');
 if(matches.length===0)return report('wait');
 matches[0].setAttribute('data-mathem-checkout-next','true');
 return report(location.href+(full.length?'#full':'#continue'));
})()
""".replace("TARGET_URL", json.dumps(target_url)))
            action = state.get("action")
            # Only fixed route/action names enter diagnostics, never private
            # order query values, account data or arbitrary page text.
            route = state.get("route")
            if route not in {"storefront", "cart", "checkout", "recommendations", "confirm", "modify", "target", "other"}:
                route = "unknown"
            label = action if action in {"ready", "blocked", "modify", "wait"} else "control"
            observation = f"{route}:{label}"
            if trace[-1] != observation:
                trace.append(observation)
            if action == "ready":
                return
            if action == "blocked":
                raise HouseholdError("Mathem checkout navigation needs user attention")
            if action == "modify":
                if action not in dispatched:
                    dispatched.add(action)
                    self._choose_checkout_destination(expected if order_id else None)
                    trace.append("modify:destination-dispatched")
                self._settle(0.5)
                continue
            if action != "wait" and action not in dispatched:
                dispatched.add(action)
                self._invoke("click", '[data-mathem-checkout-next="true"]')
            self._settle(0.5)
        raise HouseholdError("Mathem checkout navigation did not finish; steps: " + ", ".join(trace))

    def _choose_checkout_destination(self, expected):
        # Mathem defaults to the existing order. A new checkout must explicitly
        # choose the new-order radio; an addition binds its exact existing order.
        selected_once = False
        for _ in range(20):
            choice = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 if(location.href!=='https://www.mathem.se/se/checkout/modify/'||document.querySelector('input[type="password"]'))return JSON.stringify({blocked:true});
 document.querySelectorAll('[data-mathem-destination],[data-mathem-destination-next]').forEach(e=>{e.removeAttribute('data-mathem-destination');e.removeAttribute('data-mathem-destination-next');});
 const radios=[...document.querySelectorAll('input[type="radio"]')].filter(e=>!e.disabled&&[...e.labels].some(visible));
 const rows=radios.map(e=>({element:e,text:norm([...e.labels].filter(visible).map(l=>l.innerText).join(' '))}));
 const wanted=rows.filter(r=>EXISTING?r.text.startsWith('Lägg till i din nuvarande beställning '):r.text==='Skapa en ny beställning Du väljer leveranstid i nästa steg.');
 if(wanted.length!==1)return JSON.stringify({waiting:true});
 const checked=wanted[0].element.checked,selected_count=radios.filter(e=>e.checked).length;
 wanted[0].element.setAttribute('data-mathem-destination','true');
 if(checked&&selected_count===1){
  const label=EXISTING?'Fortsätt till betalning':'Fortsätt';
  const next=[...document.querySelectorAll('button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&norm(e.innerText)===label);
  if(next.length!==1)return JSON.stringify({waiting:true});
  next[0].setAttribute('data-mathem-destination-next','true');
 }
 return JSON.stringify({text:wanted[0].text,checked,selected_count});
})()
""".replace("EXISTING", "true" if expected else "false"))
            if choice.get("blocked"):
                raise HouseholdError("Mathem checkout destination changed")
            if choice.get("waiting"):
                self._settle(0.25)
                continue
            if expected:
                text = choice.get("text", "")
                if (re.search(r"(?<![A-Za-z0-9._:-])" + re.escape(expected["order_id"]) + r"(?![A-Za-z0-9._:-])", text) is None
                    or not checkout_delivery_matches(expected["delivery_text"], [text], provider="mathem")
                    or unicodedata.normalize("NFC", " ".join(expected["delivery_address"].split())).casefold() not in text.casefold()):
                    raise HouseholdError("Mathem existing checkout destination does not match the original order")
            if choice.get("checked") is True and choice.get("selected_count") == 1:
                self._invoke("click", '[data-mathem-destination-next="true"]')
                return
            if not selected_once:
                selected_once = True
                self._invoke("click", '[data-mathem-destination="true"]')
            self._settle(0.25)
        raise HouseholdError("Mathem checkout destination could not be selected")

    def _submit_checkout(self, cart, review, before_click=None):
        try:
            current = self.review_checkout(cart)
            if current != dict(review):
                raise HouseholdError("Mathem checkout changed after confirmation")
            expected = self._cart_expectation(cart)
            self._require_checkout_time(FINAL_CLICK_MARGIN)
            reference = self._account_reference(expected["delivery_address"])
            if hashlib.sha256(str(reference).encode()).hexdigest() != review["account_reference_digest"]:
                raise HouseholdError("Mathem selected account changed before the final click")
            self._require_checkout_time(FINAL_CLICK_MARGIN)
            dispatch_tab = self._checkout_dispatch_tab()
            if before_click:
                before_click()
            self._require_checkout_time(FINAL_CLICK_MARGIN)
            # The final browser turn also binds items, delivery and the selected
            # card. The callback's MCP checks cannot leave those UI fields stale.
            surface_script = self._checkout_surface_script(expected).strip()
            expected_amounts = _oda_checkout_amounts_minor(review["amounts"], provider="mathem")
            expected_amounts["discount_breakdown"] = {
                key: round(review["discount_breakdown"][key] * 100) if review["discount_breakdown"][key] is not None else None
                for key in ("product_discount", "delivery_discount")
            }
            amount_script = _oda_checkout_amount_script(expected["total_minor"],
                expected_product_count=expected["product_count"], provider="mathem",
                expected_amounts=expected_amounts,
                expected_url=self.checkout_url).strip()
            wanted = {key: review[key] for key in ("url", "authenticated", "available", "items", "delivery_roots", "address_matches", "payment_display", "submit_controls")}
            script = "(() => {const actual=JSON.parse(" + surface_script + ");if(JSON.stringify(actual)!==JSON.stringify(" + json.dumps(wanted, ensure_ascii=False) + "))return JSON.stringify({clicked:false});return " + amount_script + ";})()"
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        if self._eval(script) != {"clicked": True}:
            raise CheckoutPreconditionError("Mathem checkout changed before the final click")
        return self._capture_checkout_payment(dispatch_tab)



    def review_order_change(self, cart, order_id, order, *, deadline=None, expected_binding=None):
        with self._checkout_operation(deadline):
            binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=expected_binding)
            expected = self._addition_expectation(cart, order_id, order, binding)
            self._navigate_to_checkout(order_id, expected=expected)
            result = self._review_mathem_surface(expected)
            amounts = self._eval(_retail_addition_amount_script(expected))
            if amounts.get("amounts_valid") is not True:
                raise HouseholdError("Mathem original, added and combined order amounts do not match")
            result.update(binding=binding, account_reference_digest=binding["account_reference_digest"],
                          order_amounts=amounts["order_amounts"])
            result["amounts"] = {key: None for key in ODA_CHECKOUT_AMOUNT_KEYS}
            result["amounts"].update(product_subtotal=expected["total_minor"] / 100, provider_total=expected["total_minor"] / 100)
            return result

    def submit_order_change(self, cart, order_id, order, review, before_click=None, *, deadline=None):
        with self._checkout_operation(deadline):
            try:
                current = self.review_order_change(cart, order_id, order,
                    deadline=self._checkout_deadline, expected_binding=review["binding"])
                if current != dict(review):
                    raise HouseholdError("Mathem addition changed after confirmation")
                expected = self._addition_expectation(cart, order_id, order, review["binding"])
                tabs = self._invoke("tab", "list").get("tabs", [])
                active = [tab["tabId"] for tab in tabs if tab.get("active") is True]
                dispatch_tab = active[0] if len(active) == 1 else None
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                if before_click:
                    before_click()
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                surface_script = self._checkout_surface_script(expected).strip()
                wanted = {key: review[key] for key in ("url", "authenticated", "available", "items", "delivery_roots", "address_matches", "payment_display", "submit_controls")}
                script = "(() => {const actual=JSON.parse(" + surface_script + ");if(JSON.stringify(actual)!==JSON.stringify(" + json.dumps(wanted, ensure_ascii=False) + "))return JSON.stringify({clicked:false});return " + _retail_addition_amount_script(expected, submit=True).strip() + ";})()"
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            # Transport failure here may follow the click. Leave it uncertain.
            if self._eval(script) != {"clicked": True}:
                raise CheckoutPreconditionError("Mathem addition changed before the final click")
            return self._capture_addition_failure(order_id, dispatch_tab)

    def _capture_addition_failure(self, order_id, dispatch_tab):
        return self._capture_checkout_payment(dispatch_tab, order_id=order_id)

    def review_delivery_change(self, order_id, order, delivery, *, deadline=None, expected_binding=None):
        with self._checkout_operation(deadline):
            binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=expected_binding)
            expected = self._delivery_change_expectation(order_id, order, delivery, binding)
            self._navigate_delivery_change(order_id, expected, delivery["slot"])
            return self._read_delivery_change_review(order_id, delivery, binding, expected)

    def _read_delivery_change_review(self, order_id, delivery, binding, expected):
        result = self._review_mathem_surface(expected)
        amounts = self._eval(_retail_addition_amount_script(expected))
        if amounts.get("amounts_valid") is not True:
            raise HouseholdError("Mathem delivery change has no verified original, final and payable totals")
        result.update(binding=binding, account_reference_digest=binding["account_reference_digest"],
                      target_order_id=order_id, order_amounts=amounts["order_amounts"])
        result["amounts"] = {key: None for key in ODA_CHECKOUT_AMOUNT_KEYS}
        payable = amounts["order_amounts"]["payable_minor"] / 100
        result["amounts"]["provider_total"] = payable
        result["summary"] = {"items": [], "count": 0, "total": payable,
                             "delivery": {"slot_id": delivery["slot_id"], "display": delivery["display"], "address": binding["receipt_address"]},
                             "payment": result["payment_display"], "order_amounts": amounts["order_amounts"]}
        return result

    def submit_delivery_change(self, order_id, order, delivery, review, before_click=None, *, deadline=None):
        with self._checkout_operation(deadline):
            try:
                binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=review["binding"])
                expected = self._delivery_change_expectation(order_id, order, delivery, binding)
                expected["order_amounts"] = review["order_amounts"]
                # Confirmation only rereads the prepared route. It never selects
                # another slot or replays a potentially uncertain reservation.
                self._open(expected["checkout_url"])
                current = self._read_delivery_change_review(order_id, delivery, binding, expected)
                if current != dict(review):
                    raise HouseholdError("Mathem delivery change changed after confirmation")
                dispatch_tab = self._checkout_dispatch_tab()
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                if before_click:
                    before_click()
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                surface_script = self._checkout_surface_script(expected).strip()
                wanted = {key: review[key] for key in ("url", "authenticated", "available", "items", "delivery_roots", "address_matches", "payment_display", "submit_controls")}
                script = "(() => {const actual=JSON.parse(" + surface_script + ");if(JSON.stringify(actual)!==JSON.stringify(" + json.dumps(wanted, ensure_ascii=False) + "))return JSON.stringify({clicked:false});return " + _retail_addition_amount_script(expected, submit=True).strip() + ";})()"
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            if self._eval(script) != {"clicked": True}:
                raise CheckoutPreconditionError("Mathem delivery changed before the final click")
            return self._capture_checkout_payment(dispatch_tab,
                authentication_expected=review["order_amounts"]["payable_minor"] > 0, capture_failure=False)



    @staticmethod
    def _order_url(order_id):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", order_id) is None:
            raise HouseholdError("invalid Mathem order identity")
        return "https://www.mathem.se/se/account/orders/" + quote(order_id, safe="") + "/"

    def _cancellation_receipt_script(self, order_id, binding):
        return r"""
(() => {
 const receipt=JSON.parse(RECEIPT);
 if(receipt.address_verified!==true)return JSON.stringify({available:false});
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const main=[...document.querySelectorAll('main')].filter(visible)[0];
 const lines=(main.innerText||'').split(/\n+/).map(norm).filter(Boolean);
 const labels=[...main.querySelectorAll('*')].filter(visible).filter(e=>norm(e.innerText)==='Totalt inkl. moms').filter(e=>![...e.children].some(c=>visible(c)&&norm(c.innerText)==='Totalt inkl. moms'));
 const money=/\b\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|SEK)\b/i;
 const totals=labels.map(e=>{let row=e.parentElement;while(row&&row!==main){if(money.test(norm(row.innerText)))return norm(row.innerText);row=row.parentElement;}return null;}).filter(Boolean);
 const buttons=[...main.querySelectorAll('button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&norm(e.innerText)==='Avbryt beställningen'&&e.getAttribute('aria-haspopup')==='dialog');
 return JSON.stringify({available:buttons.length===1,delivery_lines:lines.filter(t=>/\b\d{1,2}(?::\d{2})?\s*(?:-|–|och|till)\s*\d{1,2}(?::\d{2})?\b/i.test(t)),total_rows:totals,deadline_text:lines.filter(t=>/^Du kan avboka din beställning när som helst före /i.test(t)),addition_deadline_text:lines.filter(t=>/^Du har till och med .+ att lägga till varor i din leverans\./i.test(t))});
})()
""".replace("RECEIPT", _mathem_receipt_address_script(order_id, binding["receipt_address"]).strip())

    def order_followup(self, order_id, order, *, deadline=None):
        """Read current correction limits from the exact bound receipt, no clicks."""
        with self._checkout_operation(deadline):
            binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline)
            expected = self._order_expectation(order_id, order)
            self._open(self._order_url(order_id))
            for _ in range(20):
                receipt = self._eval(self._cancellation_receipt_script(order_id, binding))
                if (cancellation_delivery_matches(expected["delivery_text"], receipt.get("delivery_lines"), provider="mathem")
                    and cancellation_total_matches(expected["total_minor"], receipt.get("total_rows"), provider="mathem")):
                    additions = receipt.get("addition_deadline_text", [])
                    cancellations = receipt.get("deadline_text", [])
                    return {"deadline_text": additions[0] if len(additions) == 1 else None,
                            "cancellation_deadline_text": cancellations[0] if len(cancellations) == 1 else None,
                            "cancellation_available": receipt.get("available") is True}
                self._settle(0.25)
            raise HouseholdError("Mathem current correction limits could not be verified")

    def _review_mathem_cancellation(self, order_id, order, *, expected_binding=None):
        binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=expected_binding)
        expected = self._order_expectation(order_id, order)
        self._open(self._order_url(order_id))
        script = self._cancellation_receipt_script(order_id, binding)
        for _ in range(20):
            receipt = self._eval(script)
            if receipt.get("available") is True:
                break
            self._settle(0.25)
        if (receipt.get("available") is not True
                or not cancellation_delivery_matches(expected["delivery_text"], receipt.get("delivery_lines"), provider="mathem")
                or not cancellation_total_matches(expected["total_minor"], receipt.get("total_rows"), provider="mathem")):
            return {"available": False, "reason": "Mathem does not expose cancellation for this verified order"}
        # The observed opener explicitly declares a dialog. Never treat an
        # arbitrary cancel-labelled button as a non-final review control.
        opened = self._eval(r"""
(() => {
 const receipt=JSON.parse(RECEIPT);if(JSON.stringify(receipt)!==JSON.stringify(EXPECTED))return JSON.stringify({opened:false});
 const buttons=[...document.querySelectorAll('main button')].filter(e=>e.innerText.trim()==='Avbryt beställningen'&&e.getAttribute('aria-haspopup')==='dialog'&&e.getAttribute('aria-expanded')==='false'&&!e.disabled);
 if(buttons.length!==1)return JSON.stringify({opened:false});buttons[0].click();return JSON.stringify({opened:true});
})()
""".replace("RECEIPT", script.strip()).replace("EXPECTED", json.dumps(receipt, ensure_ascii=False)))
        if opened != {"opened": True}:
            raise HouseholdError("Mathem cancellation review control changed")
        for _ in range(20):
            dialog = self._eval(self._cancellation_dialog_script())
            if dialog.get("available") is True:
                return {"available": True, "binding": binding, "receipt": receipt, "consequence": dialog["consequence"]}
            self._settle(0.25)
        raise HouseholdError("Mathem cancellation dialog did not finish loading")

    @staticmethod
    def _cancellation_dialog_script(action=None):
        return r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
 if(dialogs.length!==1)return JSON.stringify({available:false});const d=dialogs[0];
 const buttons=[...d.querySelectorAll('button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true');
 const final=buttons.filter(e=>norm(e.innerText)==='Avboka min beställning');
 const dismiss=buttons.filter(e=>norm(e.innerText)==='Nej, avboka inte');
 const consequence=norm(d.innerText);
 if(final.length!==1||dismiss.length!==1||final[0]===dismiss[0]||consequence!=='Är du säker på att du vill avbryta din beställning? Du kan inte ångra detta Nej, avboka inte Avboka min beställning')return JSON.stringify({available:false});
 if(ACTION==='submit'){final[0].click();return JSON.stringify({clicked:true});}
 if(ACTION==='dismiss'){dismiss[0].click();return JSON.stringify({dismissed:true});}
 return JSON.stringify({available:true,consequence});
})()
""".replace("ACTION", json.dumps(action))

    def review_cancellation(self, order_id, order, *, deadline=None):
        with self._checkout_operation(deadline):
            review = self._review_mathem_cancellation(order_id, order)
            if review.get("available") is True:
                if self._eval(self._cancellation_dialog_script("dismiss")) != {"dismissed": True}:
                    raise HouseholdError("Mathem cancellation review could not be dismissed")
            return review

    def submit_cancellation(self, order_id, order, review, before_click=None, *, deadline=None):
        final_dispatched = False
        try:
            with self._checkout_operation(deadline):
                current = self._review_mathem_cancellation(order_id, order, expected_binding=review.get("binding"))
                if current != dict(review):
                    raise HouseholdError("Mathem cancellation changed after confirmation")
                # Fresh provider state and the original account binding precede
                # the last expiry check. A timeout after dispatch is uncertain.
                fresh = self.provider_client.call("get_order", {"order_number": order_id}, deadline=self._checkout_deadline)
                tracking = self.provider_client.call("order_tracking", {"order_number": order_id}, deadline=self._checkout_deadline)
                tracked_id = str(tracking.get("orderNumber") or tracking.get("order_number") or tracking.get("order_id") or tracking.get("id") or "")
                if fresh != dict(order) or tracked_id != order_id or tracking.get("status") != "paid_and_modifiable":
                    raise HouseholdError("Mathem order changed before cancellation")
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                if before_click:
                    before_click()
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                # StateStore sorts object keys. Compare the receipt fields,
                # retaining exact array/text equality, rather than key order.
                script = "(() => {const receipt=JSON.parse(" + self._cancellation_receipt_script(order_id, review["binding"]).strip() + ");const expected=" + json.dumps(review["receipt"], ensure_ascii=False) + ";if(Object.keys(receipt).length!==Object.keys(expected).length||Object.keys(expected).some(k=>JSON.stringify(receipt[k])!==JSON.stringify(expected[k])))return JSON.stringify({clicked:false});return " + self._cancellation_dialog_script("submit").strip() + ";})()"
                final_dispatched = True
                if self._eval(script) != {"clicked": True}:
                    final_dispatched = False
                    raise HouseholdError("Mathem cancellation changed before the final click")
        except HouseholdError as exc:
            if not final_dispatched:
                raise CancellationPreconditionError(str(exc)) from exc
            raise


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--clear-cancellation-cache":
        raise SystemExit(2)
    clear_cancellation_cache(sys.argv[2])
