"""Operation-specific Oda browser fallback for protected order actions."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
import calendar
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
import unicodedata
from urllib.parse import quote

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
ODA_CHECKOUT_PRODUCT_LABEL = re.compile(r"(?:0|[1-9]\d{0,6}) varer")
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
 const required=[states.product_subtotal,states.discounted_subtotal,states.delivery_price,states.provider_total];
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
 const optionalValid=[states.discounts,states.delivery_discount,states.bags,states.other_fee].every(row=>row.state!=='invalid');
 const signsValid=required.every(row=>row.value>=0)&&[states.bags,states.other_fee].every(row=>row.state!=='value'||row.value>=0)&&[states.discounts,states.delivery_discount].every(row=>row.state!=='value'||row.value<=0);
 const deliveryDiscountValid=states.delivery_discount.state==='absent'||-states.delivery_discount.value===states.delivery_price.value;
 const discountedValid=states.discounted_subtotal.value===states.product_subtotal.value+(states.discounts.value||0);
 const totalValid=amounts.provider_total===amounts.product_subtotal+(amounts.discounts||0)+(amounts.delivery_price||0)+(amounts.bags||0)+(states.other_fee.value||0);
 const amountsValid=required.every(row=>row.state==='value')&&optionalValid&&signsValid&&deliveryDiscountValid&&contained&&discountedValid&&totalValid&&unknownRows.length===0&&amounts.provider_total===TOTAL;
 if(!CLICK_MODE)return JSON.stringify({amounts,amounts_valid:amountsValid});
 const expectedAmounts=EXPECTED_AMOUNTS;
 const money=value=>[...norm(value).matchAll(/\b(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|CURRENCY_CODE)\b/gi)].map(match=>Number(match[1].replace(/[ .]/g,''))*100+Number(match[2]));
 const labels=[...document.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>/^(FINAL_CONTROL)\s+\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|CURRENCY_CODE)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||''))).filter(x=>{const values=money(x.innerText||x.getAttribute('aria-label')||'');return values.length===1&&values[0]===TOTAL;});
 const ready=location.href===EXPECTED_URL&&labels.length===1&&amountsValid&&JSON.stringify(amounts)===JSON.stringify(expectedAmounts);
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
            if provider == "mathem" else f"String({expected_product_count})+' varer'"
        )
        .replace("CURRENCY_CODE", "SEK" if provider == "mathem" else "NOK")
        .replace("FINAL_CONTROL", "Bekräfta och betala" if provider == "mathem" else "Bekreft og betal|Confirm and pay")
        .replace("CLICK_MODE", "true" if click_mode else "false")
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


def _mathem_addition_amount_script(expected: Mapping[str, Any], *, submit: bool = False) -> str:
    """Observed Swedish existing-order overview; charge only the reviewed delta.

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
 const money='(\\d+(?:[ .]\\d{3})*),(\\d{2})\\s*(?:kr|SEK)';
 const values={},nodes=[...document.querySelectorAll('*')].filter(visible);
 const specs=[['original','Ursprunglig beställning',true],['added','Varor tillagda i efterhand',true],['payable','Att betala nu',false],['combined','Totalsumma för beställning',true]].filter(([key])=>!expected.delivery_change||key!=='added');
 if(expected.delivery_change&&(expected.total_minor!==0||expected.product_count!==0||nodes.some(e=>norm(e.innerText)==='Varor tillagda i efterhand')))return failed();
 for(const [key,label,counted] of specs){
  const labels=nodes.filter(e=>norm(e.innerText)===label).filter(e=>![...e.children].some(c=>visible(c)&&norm(c.innerText)===label));
  if(labels.length!==1)return failed();
  const pattern=new RegExp('^'+label+' '+(counted?'([1-9]\\d*) (vara|varor) ':'')+money+'$','i');
  let row=labels[0].parentElement,match=null;
  while(row&&row!==document.body){match=norm(row.innerText).match(pattern);if(match)break;row=row.parentElement;}
  if(!match)return failed();
  const offset=counted?3:1,minor=Number(match[offset].replace(/[ .]/g,''))*100+Number(match[offset+1]);
  if(!Number.isSafeInteger(minor)||minor<0)return failed();
  values[key+'_minor']=minor;
  if(counted){const count=Number(match[1]);if(!Number.isSafeInteger(count)||count>1000000||(count===1)!==(match[2]==='vara'))return failed();values[key+'_count']=count;}
 }
 const wanted={original_minor:expected.original_minor,original_count:expected.original_count,...(expected.delivery_change?{}:{added_minor:expected.total_minor,added_count:expected.product_count}),payable_minor:expected.total_minor,combined_minor:expected.original_minor+expected.total_minor,combined_count:expected.original_count+expected.product_count};
 if(Object.keys(wanted).some(key=>values[key]!==wanted[key]))return failed();
 const controls=[...document.querySelectorAll('button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true').filter(e=>/^Bekräfta och betala\s/.test(norm(e.innerText||e.getAttribute('aria-label')||'')));
 if(controls.length!==1)return failed();
 const pay=norm(controls[0].innerText||controls[0].getAttribute('aria-label')||'').match(new RegExp('^Bekräfta och betala '+money+'$','i'));
 if(!pay||Number(pay[1].replace(/[ .]/g,''))*100+Number(pay[2])!==values.payable_minor)return failed();
 if(SUBMIT){controls[0].click();return JSON.stringify({clicked:true});}
 return JSON.stringify({amounts_valid:true,order_amounts:values});
})()
""".replace("EXPECTED", json.dumps(dict(expected), ensure_ascii=False)).replace("SUBMIT", "true" if submit else "false")

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


def checkout_delivery_matches(expected: str, roots: Any, *, provider: str = "oda") -> bool:
    if not expected:
        return True
    return isinstance(roots, list) and len(roots) == 1 and isinstance(roots[0], str) and delivery_signature(expected, provider=provider) is not None and delivery_signature(expected, provider=provider) == delivery_signature(roots[0], provider=provider)


def cancellation_delivery_matches(expected: str, lines: Any, *, provider: str = "oda") -> bool:
    signature = delivery_signature(expected, provider=provider)
    return signature is not None and isinstance(lines, list) and len(lines) == 1 and isinstance(lines[0], str) and delivery_signature(lines[0], provider=provider) == signature


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
   const money=[...norm(final[0].innerText||final[0].getAttribute('aria-label')||'').matchAll(/\b(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|NOK)\b/gi)].map(m=>Number(m[1].replace(/[ .]/g,''))*100+Number(m[2]));
   const roots=[...document.querySelectorAll('h1,h2,h3,h4')].filter(visible).filter(x=>norm(x.innerText)==='Vi leverer varene dine').map(x=>x.closest('section,article,.k-card')).filter(Boolean).map(x=>norm(x.innerText));
   const text=norm(document.body?.innerText||''),payment=text.match(/(?:[*•·xX]{2,}\s*|slutter på\s*|ending in\s*)(\d{4})\b/i);
   return JSON.stringify({action:'ready',amounts:money,delivery_roots:roots,payment_display:payment?`•••• ${payment[1]}`:null,submit_controls:final.length});
 }
})()
""".replace("URL", json.dumps(expected_url))


def _oda_checkout_surface_script(expected: Mapping[str, Any]) -> str:
    return r"""
(() => {
 const expected=EXPECTED;
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const text=norm(document.body?.innerText||'');
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const labels=[...document.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>/^(Bekreft og betal|Confirm and pay)\s+\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|NOK)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 const login=!!document.querySelector('form[action*="login"],input[type="password"]');
 const unavailable=/ikke tilgjengelig|utsolgt|unavailable/i.test(text);
 const itemInputs=[...document.querySelectorAll('input[type="number"]')].filter(visible).filter(input=>/\bAntall\b/i.test(norm(input.closest('li,article')?.innerText||'')));
 const items=itemInputs.map(input=>{const root=input.closest('li,article');return {quantity:Number(input.value),text:norm([...(root?.querySelectorAll('p')||[])].filter(visible).slice(0,2).map(x=>x.innerText).join(' '))};});
 const money=value=>[...norm(value).matchAll(/\b(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|NOK)\b/gi)].map(match=>Number(match[1].replace(/[ .]/g,''))*100+Number(match[2]));
 const amounts=labels.length===1?money(labels[0].innerText||labels[0].getAttribute('aria-label')||''):[];
 const totalMatch=amounts.length===1&&amounts[0]===expected.total_minor;
 const deliveryRoots=[...document.querySelectorAll('h1,h2,h3,h4')].filter(visible).filter(x=>norm(x.innerText||'')==='Vi leverer varene dine').map(x=>x.closest('section,article,.k-card')).filter(Boolean);
 const escaped=norm(expected.delivery_address).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
 const addressMatch=deliveryRoots.length===1&&Boolean(expected.delivery_address)&&new RegExp(`(?:^|[\\s,:])${escaped}(?=$|[\\s,])`,'i').test(norm(deliveryRoots[0].innerText||''));
 const paymentMatch=text.match(/(?:[*•·xX]{2,}\s*|slutter på\s*|ending in\s*)(\d{4})\b/i);
 const maskedPayment=Boolean(paymentMatch) && !/(?:\d[ -]?){12,19}/.test(text);
 const paymentDisplay=maskedPayment?`•••• ${paymentMatch[1]}`:null;
 return JSON.stringify({url:location.href,authenticated:!login,available:!unavailable,items,total_matches:totalMatch,delivery_roots:deliveryRoots.map(root=>norm(root.innerText||'')),address_matches:addressMatch,masked_payment:maskedPayment,payment_display:paymentDisplay,submit_controls:labels.length});
})()
""".replace("EXPECTED", json.dumps(expected, ensure_ascii=False, separators=(",", ":")))


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

    def review_checkout(self, cart: Mapping[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        with self._checkout_operation(deadline):
            return self._review_checkout(cart)

    def _order_cart(self, cart, order_id, order, binding):
        binding = require_order_binding(binding)
        original = self._order_expectation(order_id, order)
        return {**cart, "deliverySlot": {"name": original["delivery_text"]}, "deliveryAddress": binding["receipt_address"]}

    def review_order_change(self, cart: Mapping[str, Any], order_id: str, order: Mapping[str, Any], *, deadline: float | None = None, expected_binding=None) -> dict[str, Any]:
        with self._checkout_operation(deadline):
            binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=expected_binding)
            bound_cart = self._order_cart(cart, order_id, order, binding)
            review = self._review_checkout(bound_cart, order_id=order_id, delivery_text=self._order_expectation(order_id, order)["delivery_text"])
            review["binding"] = binding
            return review

    def _review_checkout(self, cart: Mapping[str, Any], *, order_id: str | None = None, delivery_text: str | None = None) -> dict[str, Any]:
        expected = self._cart_expectation(cart)
        account_digest = self._verify_checkout_account(expected["delivery_address"]) if order_id is None else None
        if delivery_text is not None:
            expected["delivery_text"] = delivery_text
        if order_id is None:
            self._navigate_to_checkout()
        else:
            self._navigate_to_checkout(order_id)
        expanded = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const buttons=[...document.querySelectorAll('button')].filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Vis varene');
 if(buttons.length>1)return JSON.stringify({expanded:false});
 if(buttons.length===1)buttons[0].click();
 return JSON.stringify({expanded:true});
})()
""")
        if expanded != {"expanded": True}:
            raise HouseholdError("Oda checkout items cannot be reviewed")
        for _ in range(20):
            ready = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const show=[...document.querySelectorAll('button')].filter(visible).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Vis varene');
 const inputs=[...document.querySelectorAll('input[type="number"]')].filter(visible).filter(input=>/\bAntall\b/i.test(norm(input.closest('li,article')?.innerText||'')));
 return JSON.stringify({ready:show.length===0&&inputs.length===COUNT});
})()
""".replace("COUNT", str(len(expected["lines"]))))
            if ready == {"ready": True}:
                break
            self._settle(0.25)
        else:
            raise HouseholdError("Oda checkout items did not finish rendering")
        self._expand_checkout_amount_summary()
        script = _oda_checkout_surface_script(expected)
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
            raise HouseholdError("Oda saved payment card could not be verified; check Payment in your Oda account and complete any card entry there, then request a new checkout review")
        surface = dict(result)
        result["line_matches"] = checkout_lines_match(expected["lines"], result.pop("items"))
        result["delivery_matches"] = checkout_delivery_matches(expected["delivery_text"], result.pop("delivery_roots"))
        if not all(result[key] is True for key in ("authenticated", "available", "line_matches", "total_matches", "delivery_matches", "address_matches", "masked_payment")) or result["submit_controls"] != 1:
            raise OdaCheckoutMismatchError("Oda checkout does not match the reviewed cart")
        result["amounts"] = self._read_checkout_amounts(
            expected["total_minor"], expected["product_count"],
        )
        if re.fullmatch(r"•••• \d{4}", str(result.get("payment_display") or "")) is None:
            raise HouseholdError("Oda checkout payment identity is unavailable")
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
 const show=buttons.filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Vis oppsummering');
 const hide=buttons.filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Skjul oppsummering');
 if(show.length===1&&hide.length===0){show[0].click();return JSON.stringify({expanded:true});}
 return JSON.stringify({expanded:show.length===0&&hide.length===1});
})()
""")
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

    def _navigate_to_checkout(self, order_id: str | None = None) -> None:
        self._continue_checkout_cart()
        if order_id is None:
            self._advance_checkout_path()
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

    def _advance_checkout_path(self, order_id: str | None = None) -> None:
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
   // Oda defaults to an existing order here. Bind the new-order radio before advancing.
   const main=document.querySelector('main');
   if(ORDER!==null||!main||!visible(main)||dialogs.length||document.querySelector('input[type="password"]'))return JSON.stringify({action:'blocked'});
   const radios=[...main.querySelectorAll('input[type="radio"]')];
   const selected=radios.filter(x=>x.checked);
   const candidates=radios.filter(x=>enabled(x)&&[...x.labels].filter(label=>visible(label)&&label.contains(x)&&label.querySelectorAll('input[type="radio"]').length===1&&/^Lag en ny bestilling(?:\s|$)/.test(norm(label.innerText))).length===1);
   if(candidates.length!==1||selected.length!==1||radios.some(x=>!visible(x)))return JSON.stringify({action:'blocked'});
   const target=candidates[0];
   if(!target.checked){target.setAttribute('data-oda-household-action','new-order');return JSON.stringify({action:'new_order'});}
   const payment=[...main.querySelectorAll('button')].filter(enabled).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Gå til betaling');
   if(payment.length!==1)return JSON.stringify({action:'blocked'});
   payment[0].setAttribute('data-oda-household-action','payment');
   return JSON.stringify({action:'payment'});
 }
 if(confirmPage){
   const controls=[...document.querySelectorAll('button')].filter(enabled);
   const submit=controls.filter(x=>/^(Bekreft og betal|Legg inn bestilling|Confirm and pay|Place order)(\b|\s)/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
   if(submit.length===1){
     const expected=ORDER;
     const actual=new URL(location.href).searchParams.get('orderNumber');
     if((expected===null&&actual===null)||(expected!==null&&actual===expected))return JSON.stringify({action:'ready'});
     return JSON.stringify({action:'blocked'});
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
""".replace("STORE", json.dumps(STORE_URL)).replace("CART", json.dumps(CART_URL)).replace("CHECKOUT_ENTRY", json.dumps(CHECKOUT_ENTRY_URL)).replace("MODIFY", json.dumps(CHECKOUT_MODIFY_URL)).replace("RECOMMENDATIONS", json.dumps(RECOMMENDATIONS_URL)).replace("CHECKOUT", json.dumps(CHECKOUT_URL)).replace("ORDER", json.dumps(order_id))
        dispatched: set[str] = set()
        self._settle(10)
        for _ in range(30):
            action = self._eval(script).get("action")
            if action == "ready":
                return
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

    def submit_checkout(self, cart: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, deadline: float | None = None) -> None:
        with self._checkout_operation(deadline):
            self._submit_checkout(cart, review, before_click)

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
            self._click_checkout_submit(
                expected_cart["total_minor"],
                f"{CHECKOUT_URL}?orderNumber={order_id}",
                before_click,
                expected_product_count=expected_cart["product_count"],
                expected_amounts=review.get("amounts"),
                review_surface=(_oda_checkout_surface_script(expected_cart), review["surface"]),
            )

    def review_delivery_change(self, order_id: str, order: Mapping[str, Any], delivery: Mapping[str, Any], *, deadline: float | None = None, expected_binding=None) -> dict[str, Any]:
        with self._checkout_operation(deadline):
            binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=expected_binding)
            expected_order = self._order_expectation(order_id, order)
            expected_product_count = self._order_product_count(order)
            target = str(delivery.get("display") or "")
            signature = delivery_signature(target)
            if signature is None:
                raise HouseholdError("Oda delivery slot identity is unavailable")
            signature_value = [*signature[:5], {name: index for index, name in enumerate(("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"), 1)}[signature[5]]]
            self._open_order(order_id)
            opened = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 document.querySelectorAll('[data-oda-household-action]').forEach(x=>x.removeAttribute('data-oda-household-action'));
 const buttons=[...document.querySelectorAll('main button')].filter(enabled).filter(x=>/^(Endre levering(?:stid)?|Endre tidspunkt|Flytt levering)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(buttons.length!==1)return JSON.stringify({ready:false});
 buttons[0].setAttribute('data-oda-household-action','delivery-change-open');
 return JSON.stringify({ready:true});
})()
""")
            if opened != {"ready": True}:
                raise HouseholdError("Oda does not currently expose delivery changes for this order")
            self._invoke("click", '[data-oda-household-action="delivery-change-open"]')
            selected = False
            dispatched: set[str] = set()
            expected_url = f"{CHECKOUT_URL}?orderNumber={order_id}"
            for _ in range(50):
                surface = self._eval(r"""
(() => {
 const wanted=SIGNATURE;
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const months={jan:1,januar:1,feb:2,februar:2,mar:3,mars:3,apr:4,april:4,mai:5,jun:6,juni:6,jul:7,juli:7,aug:8,august:8,sep:9,september:9,okt:10,oktober:10,nov:11,november:11,des:12,desember:12};
 const signature=text=>{const value=norm(text).toLocaleLowerCase('nb-NO'),d=value.match(/\b(\d{1,2})\.?\s*(jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\b/i),t=value.match(/\b(\d{1,2})(?::(\d{2}))?\s*(?:-|–|og|til)\s*(\d{1,2})(?::(\d{2}))?\b/i);return d&&t?[Number(t[1]),Number(t[2]||0),Number(t[3]),Number(t[4]||0),Number(d[1]),months[d[2]]]:null};
 document.querySelectorAll('[data-oda-household-action]').forEach(x=>x.removeAttribute('data-oda-household-action'));
 const controls=[...document.querySelectorAll('button,a,[role="radio"]')].filter(enabled);
 const review=JSON.parse(REVIEW);if(review.action==='ready')return JSON.stringify(review);
 const slots=controls.filter(x=>{const found=signature(x.innerText||x.getAttribute('aria-label')||'');return found&&JSON.stringify(found)===JSON.stringify(wanted)});
 if(!SELECTED){if(slots.length!==1)return JSON.stringify({action:'wait'});slots[0].setAttribute('data-oda-household-action','delivery-change-slot');return JSON.stringify({action:'slot'});}
 const next=controls.filter(x=>/^(Fortsett|Bekreft(?: levering)?|Gå til betaling)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(next.length!==1)return JSON.stringify({action:'wait'});
 next[0].setAttribute('data-oda-household-action','delivery-change-next');
 return JSON.stringify({action:'next'});
})()
""".replace("REVIEW", _oda_delivery_change_surface_script(expected_url).strip()).replace("SIGNATURE", json.dumps(signature_value)).replace("URL", json.dumps(expected_url)).replace("SELECTED", "true" if selected else "false"))
                action = surface.get("action")
                if action == "ready":
                    return self._delivery_change_review(order_id, order, delivery, surface, binding)
                if action == "slot":
                    if "slot" in dispatched:
                        raise HouseholdError("Oda delivery slot selection did not advance")
                    dispatched.add("slot")
                    self._invoke("click", '[data-oda-household-action="delivery-change-slot"]')
                    selected = True
                elif action == "next":
                    if "next" in dispatched:
                        raise HouseholdError("Oda delivery change payment step did not advance")
                    dispatched.add("next")
                    self._invoke("click", '[data-oda-household-action="delivery-change-next"]')
                self._settle(0.5)
            raise HouseholdError("Oda delivery change navigation timed out")

    def _delivery_change_review(self, order_id, order, delivery, surface, binding):
        binding = require_order_binding(binding)
        expected_order = self._order_expectation(order_id, order)
        expected_product_count = self._order_product_count(order)
        target = str(delivery.get("display") or "")
        if surface.get("action") != "ready":
            raise HouseholdError("Oda prepared delivery review is unavailable; prepare it again")
        amounts = surface.get("amounts")
        roots = surface.get("delivery_roots")
        payment_display = str(surface.get("payment_display") or "")
        if not isinstance(amounts, list) or len(amounts) != 1 or not checkout_delivery_matches(target, roots) or re.fullmatch(r"•••• \d{4}", payment_display) is None or surface.get("submit_controls") != 1:
            raise HouseholdError("Oda delivery change review does not match the requested slot")
        self._expand_checkout_amount_summary()
        checkout_amounts = self._read_checkout_amounts(
            amounts[0], expected_product_count,
        )
        summary = {
            "items": [],
            "count": 0,
            "total": amounts[0] / 100,
            "delivery": {"slot_id": delivery.get("slot_id"), "display": target, "address": binding["receipt_address"]},
            "payment": payment_display,
            "amounts": checkout_amounts,
        }
        return {"binding": binding, "page_digest": hashlib.sha256(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), "summary": summary, "target_order_id": order_id, "before_delivery": expected_order["delivery_text"], "surface": surface}

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
            self._click_checkout_submit(
                int(round(float(review["summary"]["total"]) * 100)),
                f"{CHECKOUT_URL}?orderNumber={order_id}",
                before_click,
                expected_product_count=self._order_product_count(order),
                expected_amounts=review["summary"].get("amounts"),
                review_surface=(surface_script, review["surface"]),
            )

    def _submit_checkout(self, cart: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None) -> None:
        try:
            current = self.review_checkout(cart)
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
        self._click_checkout_submit(
            expected_cart["total_minor"],
            CHECKOUT_URL,
            before_click,
            expected_product_count=expected_cart["product_count"],
            expected_amounts=review.get("amounts"),
            review_surface=(_oda_checkout_surface_script(expected_cart), review["surface"]),
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
    ) -> None:
        try:
            self._require_checkout_time(FINAL_CLICK_MARGIN)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        if before_click:
            try:
                before_click()
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
        try:
            self._require_checkout_time(FINAL_CLICK_MARGIN)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        try:
            amounts_minor = _oda_checkout_amounts_minor(expected_amounts)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        script = _oda_checkout_amount_script(
            expected_total,
            expected_product_count=expected_product_count,
            expected_amounts=amounts_minor,
            expected_url=expected_url,
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
    def _checkout_operation(self, deadline: float | None = None):
        previous = getattr(self, "_checkout_deadline", None)
        if previous is None:
            self._checkout_deadline = deadline if deadline is not None else time.monotonic() + CHECKOUT_BROWSER_TIMEOUT
        elif deadline is not None:
            self._checkout_deadline = min(previous, deadline)
        try:
            if previous is None:
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
        data = self._invoke("open", f"https://oda.com/no/orders/{order_id}/", browser_args=CANCELLATION_BROWSER_ARGS)
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
 const controls=[...document.querySelectorAll('button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true').filter(e=>/^Bekräfta och betala\s+\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|SEK)$/i.test(norm(e.innerText||e.getAttribute('aria-label')||'')));
 return JSON.stringify({url:location.href,authenticated:!document.querySelector('input[type="password"]'),available:!/inte tillgänglig|slut i lager|unavailable/i.test(text),items,delivery_roots:delivery.map(e=>norm(e.innerText)),address_matches,payment_display:payment.verified===true?payment.payment_display:null,submit_controls:controls.length});
})()
""".replace("PAYMENT", _mathem_checkout_payment_script(expected.get("checkout_url", self.checkout_url)).strip()).replace("EXPECTED", json.dumps(expected, ensure_ascii=False))

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
 const next=[...document.querySelectorAll('button')].filter(visible).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&norm(e.innerText)==='Fortsätt till betalning');
 if(wanted.length!==1||next.length!==1)return JSON.stringify({waiting:true});
 wanted[0].element.setAttribute('data-mathem-destination','true');next[0].setAttribute('data-mathem-destination-next','true');
 return JSON.stringify({text:wanted[0].text,checked:wanted[0].element.checked,selected_count:radios.filter(e=>e.checked).length});
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
        if order.get("currency") != "SEK" or delivery_signature(expected["delivery_text"], provider="mathem") is None:
            raise HouseholdError("Mathem original order currency or delivery is unavailable")
        return expected

    def review_order_change(self, cart, order_id, order, *, deadline=None, expected_binding=None):
        with self._checkout_operation(deadline):
            binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=expected_binding)
            expected = self._addition_expectation(cart, order_id, order, binding)
            self._navigate_to_checkout(order_id, expected=expected)
            result = self._review_mathem_surface(expected)
            amounts = self._eval(_mathem_addition_amount_script(expected))
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
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                if before_click:
                    before_click()
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                surface_script = self._checkout_surface_script(expected).strip()
                wanted = {key: review[key] for key in ("url", "authenticated", "available", "items", "delivery_roots", "address_matches", "payment_display", "submit_controls")}
                script = "(() => {const actual=JSON.parse(" + surface_script + ");if(JSON.stringify(actual)!==JSON.stringify(" + json.dumps(wanted, ensure_ascii=False) + "))return JSON.stringify({clicked:false});return " + _mathem_addition_amount_script(expected, submit=True).strip() + ";})()"
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            # Transport failure here may follow the click. Leave it uncertain.
            if self._eval(script) != {"clicked": True}:
                raise CheckoutPreconditionError("Mathem addition changed before the final click")

    def _navigate_delivery_change(self, order_id, expected, slot):
        # The existing-order route can already retain the selected review. Open
        # its exact URL first; never use the new-order cart destination.
        self._open(expected["checkout_url"])
        for _ in range(20):
            surface = self._eval(self._checkout_surface_script(expected))
            if checkout_delivery_matches(expected["delivery_text"], surface.get("delivery_roots"), provider="mathem"):
                return
            self._settle(0.25)
        self._open("https://www.mathem.se/se/")
        menu_script = r"""(() => {
const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
if(location.href!=='https://www.mathem.se/se/')return JSON.stringify({opened:false});
const links=[...document.querySelectorAll('a')].filter(vis).filter(e=>e.href===ORDER_URL);if(links.length!==1)return JSON.stringify({opened:false});
const card=links[0].closest('article');if(!card)return JSON.stringify({opened:false});
const buttons=[...card.querySelectorAll('button')].filter(vis).filter(e=>!e.disabled&&norm(e.innerText||e.getAttribute('aria-label'))==='Visa möjliga åtgärder'&&e.getAttribute('aria-haspopup')==='menu'&&e.getAttribute('aria-expanded')==='false');
if(buttons.length!==1)return JSON.stringify({opened:false});buttons[0].setAttribute('data-mathem-delivery-menu','');return JSON.stringify({opened:true});
})()""".replace("ORDER_URL", json.dumps(self._order_url(order_id)))
        for _ in range(20):
            if self._eval(menu_script) == {"opened": True}:
                break
            self._settle(0.25)
        else:
            raise HouseholdError("Mathem does not expose delivery changes for this order")
        self._invoke("click", "[data-mathem-delivery-menu]")
        entry_script = r"""(() => {
const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
if(location.href!=='https://www.mathem.se/se/')return JSON.stringify({bound:false});
const menus=[...document.querySelectorAll('[role="menu"]')].filter(vis);if(menus.length!==1)return JSON.stringify({bound:false});
const links=[...menus[0].querySelectorAll('a[role="menuitem"]')].filter(vis).filter(e=>norm(e.innerText)==='Ändra leveranstid');if(links.length!==1)return JSON.stringify({bound:false});
const u=new URL(links[0].href);if(u.origin!=='https://www.mathem.se'||u.pathname!=='/se/checkout/confirm/'||u.hash||u.username||u.password||u.searchParams.get('orderNumber')!==ORDER_ID||JSON.stringify([...u.searchParams.keys()].sort())!==JSON.stringify(['modal','modal-id','modal-screen','orderNumber']))return JSON.stringify({bound:false});
links[0].setAttribute('data-mathem-delivery-entry','');return JSON.stringify({bound:true,url:u.href});
})()""".replace("ORDER_ID", json.dumps(order_id))
        for _ in range(20):
            entry = self._eval(entry_script)
            if entry.get("bound"):
                break
            self._settle(0.25)
        else:
            raise HouseholdError("Mathem delivery-change destination cannot be bound to this order")
        self._invoke("click", "[data-mathem-delivery-entry]")
        start = datetime.fromisoformat(slot["start_at"].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Stockholm"))
        end = datetime.fromisoformat(slot["end_at"].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Stockholm"))
        today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
        if start.date() < today or (start.date() - today).days > 31 or start.minute or end.minute:
            raise HouseholdError("Mathem delivery date cannot be identified in the visible calendar")
        if start.date() == today:
            header = "i dag"
        elif (start.date() - today).days == 1:
            header = "i morgon"
        else:
            weekdays = ("mån", "tis", "ons", "tors", "fre", "lör", "sön")
            months = ("jan", "feb", "mars", "apr", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec")
            header = f"{weekdays[start.weekday()]} {start.day} {months[start.month - 1]}."
        slot_script = r"""(() => {
const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();const vis=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
if(location.href!==EXPECTED_URL)return JSON.stringify({ready:false});
const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(vis);if(dialogs.length!==1)return JSON.stringify({ready:false});const d=dialogs[0];
if(![...d.querySelectorAll('h1,h2,h3,h4')].some(e=>norm(e.innerText)==='Ändra leveranstid'))return JSON.stringify({ready:false});
const tables=[...d.querySelectorAll('table')].filter(vis);if(tables.length!==1)return JSON.stringify({ready:false});const table=tables[0];
const headers=[...table.querySelectorAll('th')];const wanted=headers.filter(e=>norm(e.innerText)===HEADER);if(wanted.length!==1)return JSON.stringify({ready:false});
const index=wanted[0].cellIndex;const rows=[...table.querySelectorAll('tr')].filter(r=>r.cells.length>index&&norm(r.cells[0].innerText)===TIME_ROW);if(rows.length!==1)return JSON.stringify({ready:false});
const buttons=[...rows[0].cells[index].querySelectorAll('button')].filter(vis).filter(e=>!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&norm(e.innerText)==='0 kr');if(buttons.length!==1)return JSON.stringify({ready:false});
buttons[0].setAttribute('data-mathem-delivery-slot','');return JSON.stringify({ready:true});
})()""".replace("EXPECTED_URL", json.dumps(entry["url"])).replace("HEADER", json.dumps(header)).replace("TIME_ROW", json.dumps(f"{start.hour:02d} - {end.hour:02d}"))
        for _ in range(20):
            if self._eval(slot_script) == {"ready": True}:
                break
            self._settle(0.25)
        else:
            raise HouseholdError("Mathem does not expose that exact free delivery window in the visible calendar")
        self._invoke("click", "[data-mathem-delivery-slot]")
        # No repeated selection when a response is lost or the page fails to
        # advance. The caller must inspect the original selection before retry.
        for _ in range(30):
            if self._eval("JSON.stringify({ready:location.href===" + json.dumps(expected["checkout_url"]) + "&&!document.querySelector('[role=dialog]')})") == {"ready": True}:
                return
            self._settle(0.25)
        raise HouseholdError("Mathem delivery selection has not reached review; inspect it before selecting again")

    def _delivery_change_expectation(self, order_id, order, delivery, binding):
        slot = validate_delivery_slot(delivery.get("slot"))
        if slot["price_kind"] != "exact" or slot["price_ore"] != 0 or slot["provider_slot_id"] != delivery.get("slot_id"):
            raise HouseholdError("Mathem automated delivery changes currently require an exact free window")
        original = self._order_expectation(order_id, order)
        count = self._order_product_count(order)
        if order.get("currency") != "SEK" or delivery_signature(str(delivery.get("display") or ""), provider="mathem") is None:
            raise HouseholdError("Mathem original order currency or requested delivery is unavailable")
        return {"order_id": order_id, "checkout_url": self.checkout_url + "?orderNumber=" + quote(order_id, safe=""),
                "lines": [], "product_count": 0, "total_minor": 0, "total_text": "0,00",
                "original_minor": original["total_minor"], "original_count": count, "delivery_change": True,
                "delivery_text": delivery["display"], "delivery_address": binding["receipt_address"]}

    def review_delivery_change(self, order_id, order, delivery, *, deadline=None, expected_binding=None):
        with self._checkout_operation(deadline):
            binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=expected_binding)
            expected = self._delivery_change_expectation(order_id, order, delivery, binding)
            self._navigate_delivery_change(order_id, expected, delivery["slot"])
            return self._read_delivery_change_review(order_id, delivery, binding, expected)

    def _read_delivery_change_review(self, order_id, delivery, binding, expected):
        result = self._review_mathem_surface(expected)
        amounts = self._eval(_mathem_addition_amount_script(expected))
        if amounts.get("amounts_valid") is not True:
            raise HouseholdError("Mathem delivery change must retain original goods and total with zero payable")
        result.update(binding=binding, account_reference_digest=binding["account_reference_digest"],
                      target_order_id=order_id, order_amounts=amounts["order_amounts"])
        result["amounts"] = {key: None for key in ODA_CHECKOUT_AMOUNT_KEYS}
        result["amounts"]["provider_total"] = 0.0
        result["summary"] = {"items": [], "count": 0, "total": 0.0,
                             "delivery": {"slot_id": delivery["slot_id"], "display": delivery["display"], "address": binding["receipt_address"]},
                             "payment": result["payment_display"], "order_amounts": amounts["order_amounts"]}
        return result

    def submit_delivery_change(self, order_id, order, delivery, review, before_click=None, *, deadline=None):
        with self._checkout_operation(deadline):
            try:
                binding = self._read_order_binding(order_id, order, deadline=self._checkout_deadline, expected_binding=review["binding"])
                expected = self._delivery_change_expectation(order_id, order, delivery, binding)
                # Confirmation only rereads the prepared route. It never selects
                # another slot or replays a potentially uncertain reservation.
                self._open(expected["checkout_url"])
                current = self._read_delivery_change_review(order_id, delivery, binding, expected)
                if current != dict(review):
                    raise HouseholdError("Mathem delivery change changed after confirmation")
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                if before_click:
                    before_click()
                self._require_checkout_time(FINAL_CLICK_MARGIN)
                surface_script = self._checkout_surface_script(expected).strip()
                wanted = {key: review[key] for key in ("url", "authenticated", "available", "items", "delivery_roots", "address_matches", "payment_display", "submit_controls")}
                script = "(() => {const actual=JSON.parse(" + surface_script + ");if(JSON.stringify(actual)!==JSON.stringify(" + json.dumps(wanted, ensure_ascii=False) + "))return JSON.stringify({clicked:false});return " + _mathem_addition_amount_script(expected, submit=True).strip() + ";})()"
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            if self._eval(script) != {"clicked": True}:
                raise CheckoutPreconditionError("Mathem delivery changed before the final click")



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
