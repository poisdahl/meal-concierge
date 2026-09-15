"""Native Oda/Mathem order-scoped item removal. Quantities mean units remaining.

Reads use the storefront's same-origin API; the sole merchant write is the
visible confirmation button. An uncertain click is reconciled, never repeated.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re
import secrets
import time

from core import HouseholdError, cart_summary
from service_common import canonical, money_cents, require_provider_identity, safe_order_id


_PROVIDERS = {
    "oda": {
        "label": "Oda",
        "origin": "https://oda.com",
        "locale": "no",
        "currency": "NOK",
        "total_pattern": r"Totalt fjernet,? inkl\. MVA\s*[-−]\s*([\d\s]+[,.]\d{2})\s*kr",
        "button": "Fjern varer",
        "dialog_fragments": [
            "Er du sikker på at du vil fjerne disse varene fra bestillingen din?",
            "Prisen på de fjernede varene blir trukket fra totalen nederst på bestillingen.",
        ],
    },
    "mathem": {
        "label": "Mathem",
        "origin": "https://www.mathem.se",
        "locale": "se",
        "currency": "SEK",
        "total_pattern": r"Totalt borttaget,? inkl\. moms\s*[-−]\s*([\d\s]+[,.]\d{2})\s*kr",
        "button": "Ta bort varor",
        # Mathem and Oda share the removal component. These two distinct
        # Swedish phrases bind the final control to its removal dialog without
        # accepting a generic delete or cancellation dialog.
        "dialog_fragments": ["Är du säker", "Ta bort varor", "beställning"],
    },
}


def _spec(provider):
    try:
        return _PROVIDERS[provider]
    except KeyError:
        raise HouseholdError("Ordered-item removal is unavailable for this provider") from None


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _items(eligibility, provider="oda"):
    spec = _spec(provider)
    rows = {}
    groups = eligibility.get("items_groups")
    if not isinstance(groups, list):
        raise HouseholdError(f"{spec['label']} removable items are unavailable")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            raise HouseholdError(f"{spec['label']} removable item group is invalid")
        for row in group["items"]:
            if not isinstance(row, dict):
                raise HouseholdError(f"{spec['label']} removable item is invalid")
            if row.get("product_id") is None:
                continue
            pid = str(row["product_id"])
            qty = row.get("uncredited_quantity")
            if (not re.fullmatch(r"[1-9][0-9]*", pid) or pid in rows
                    or type(qty) is not int or qty < 0
                    or type(row.get("quantity")) is not int or row["quantity"] < qty
                    or row["quantity"] < 1 or row.get("currency") != spec["currency"]
                    or not isinstance(row.get("description"), str) or not row["description"].strip()
                    or money_cents(row.get("gross_amount")) is None):
                raise HouseholdError(f"{spec['label']} removable item identity or quantity is ambiguous")
            rows[pid] = row
    if (type(eligibility.get("total_uncredited_quantity")) is not int
            or eligibility["total_uncredited_quantity"] != sum(r["uncredited_quantity"] for r in rows.values())):
        raise HouseholdError(f"{spec['label']} remaining order quantity cannot be verified")
    return rows


def _intent(items, eligibility, provider="oda"):
    spec = _spec(provider)
    rows = _items(eligibility, provider)
    if not isinstance(items, list) or not items or len(items) > 200:
        raise HouseholdError("items must contain desired remaining quantities")
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise HouseholdError("removal item must be an object")
        pid, remaining = str(item.get("product_id", "")), item.get("quantity")
        if pid in seen or pid not in rows or type(remaining) is not int or remaining < 0:
            raise HouseholdError("removal product or remaining quantity is invalid")
        seen.add(pid)
        row = rows[pid]
        previous = row["uncredited_quantity"]
        if remaining > previous:
            raise HouseholdError("removal requires a lower remaining quantity; use order additions for increases")
        if remaining == previous:
            continue
        if row.get("eligible_for_removal") is not True:
            raise HouseholdError(f"{spec['label']} no longer permits removing this product")
        if row.get("entire_quantity_removal_only") is True and remaining != 0:
            raise HouseholdError(f"{spec['label']} only permits removing the entire quantity of this product")
        credit = int((Decimal(str(row["gross_amount"])) / row["quantity"] * (previous - remaining) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        result.append({"product_id": pid, "name": row["description"], "quantity": remaining,
                       "previous_quantity": previous, "removed_quantity": previous - remaining,
                       "unit_price_ore": float(Decimal(str(row["gross_amount"])) / row["quantity"] * 100),
                       "credit_ore": credit})
    if result and sum(r["removed_quantity"] for r in result) >= eligibility["total_uncredited_quantity"]:
        raise HouseholdError("Removing every remaining item is order cancellation; use the cancellation flow")
    return sorted(result, key=lambda r: r["product_id"])


def _source_script(order_id, provider="oda"):
    spec = _spec(provider)
    return """(async () => {
      // order-removal-source: read-only storefront routes
      if (location.origin !== %s) return JSON.stringify({ok:false});
      const id = %s;
      const paths = ['/api/v1/orders/'+id+'/items_eligible_for_removal/', '/api/v1/orders/'+id+'/'];
      const values = [];
      for (const path of paths) {
        const r = await fetch(path, {method:'GET', credentials:'same-origin', cache:'no-store'});
        if (!r.ok || new URL(r.url).origin !== location.origin) return JSON.stringify({ok:false});
        values.push(await r.json());
      }
      return JSON.stringify({ok:true, eligibility:values[0], details:values[1]});
    })()""" % (json.dumps(spec["origin"]), json.dumps(order_id))


def _ui_script(order_id, items, stage, product_id=None, provider="oda"):
    """Tag exactly one observed native control; never dispatch merchant actions in JS."""
    spec = _spec(provider)
    return r"""(() => {
      // order-removal-ui
      const orderId = ORDER_ID, items = ITEMS, stage = STAGE, productId = PRODUCT_ID, spec = SPEC;
      const norm = x => String(x || '').replace(/\s+/g,' ').trim();
      const visible = e => !!e && !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
      const enabled = e => !e.disabled && e.getAttribute('aria-disabled') !== 'true';
      const all = (s, root=document) => [...root.querySelectorAll(s)].filter(visible);
      const answer = (ok, extra={}) => JSON.stringify({ok,...extra});
      if (location.origin !== spec.origin || location.pathname !== '/'+spec.locale+'/account/orders/remove-items/'+orderId+'/') return answer(false);
      document.querySelectorAll('[data-meal-remove-control]').forEach(e=>e.removeAttribute('data-meal-remove-control'));
      const tag = candidates => {
        if (candidates.length !== 1 || !enabled(candidates[0])) return answer(false);
        candidates[0].setAttribute('data-meal-remove-control','true'); return answer(true);
      };
      if (stage === 'option') return tag(all('[role="option"]').filter(e => norm(e.textContent) === String(items.find(i=>i.product_id===productId).quantity)));
      const articles = all('article');
      for (const item of items) {
        const matched = articles.filter(a => all('h1',a).some(h=>norm(h.textContent) === norm(item.name)));
        if (matched.length !== 1) return answer(false);
        if (item.requested !== false && item.unit_price_ore !== undefined) {
          const prices = [...norm(matched[0].innerText).matchAll(/([\d\s]+[,.]\d{2})\s*kr/g)].map(m=>Math.round(Number(m[1].replace(/\s/g,'').replace(',','.'))*100));
          if (!prices.includes(Math.round(item.unit_price_ore))) return answer(false);
        }
        const controls = all('[role="combobox"][id="quantity-to-credit"]', matched[0]);
        if (controls.length !== 1 || (item.requested !== false && !enabled(controls[0]))) return answer(false);
        const expected = stage==='select' && item.product_id===productId ? item.previous_quantity : item.quantity;
        if (stage==='select' && item.product_id===productId) {
          if (norm(controls[0].textContent) !== String(item.previous_quantity) && norm(controls[0].textContent) !== String(item.quantity)) return answer(false);
          return tag(controls);
        }
        if (stage!=='select' && norm(controls[0].textContent)!==String(expected)) return answer(false);
      }
      if (all('[role="combobox"][id="quantity-to-credit"]').length !== items.length) return answer(false);
      if (stage === 'open' || stage === 'confirm') {
        const main = all('main'); if (main.length !== 1) return answer(false);
        const text = norm(main[0].innerText);
        const totals = [...text.matchAll(new RegExp(spec.total_pattern, 'g'))];
        const credit = items.reduce((s,i)=>s+i.credit_ore,0);
        if (totals.length!==1 || Math.round(Number(totals[0][1].replace(/\s/g,'').replace(',','.'))*100)!==credit) return answer(false);
        if (stage === 'open') return tag(all('button',main[0]).filter(e=>norm(e.textContent)===spec.button && !e.closest('[role="dialog"]')));
      }
      if (stage === 'confirm') {
        const dialogs = all('[role="dialog"]').filter(d => spec.dialog_fragments.every(fragment=>norm(d.innerText).includes(fragment)));
        if (dialogs.length !== 1) return answer(false);
        return tag(all('button',dialogs[0]).filter(e=>norm(e.textContent)===spec.button));
      }
      return answer(false);
    })()""".replace("ORDER_ID", json.dumps(order_id)).replace("ITEMS", json.dumps(items)).replace("STAGE", json.dumps(stage)).replace("PRODUCT_ID", json.dumps(product_id)).replace("SPEC", json.dumps(spec, ensure_ascii=False))


def _control(browser, pending, stage, product_id=None, *, click=True):
    provider = pending["provider"]
    spec = _spec(provider)
    requested = {r["product_id"]: r for r in pending["items"]}
    items = [{"product_id": pid, "name": row["description"], "quantity": row["uncredited_quantity"],
              "previous_quantity": row["uncredited_quantity"], "credit_ore": 0, "requested": False,
              **requested.get(pid, {})} for pid, row in _items(pending["before"]["eligibility"], provider).items()]
    for item in items:
        item["requested"] = item["product_id"] in requested
    script = _ui_script(pending["order_id"], items, stage, product_id, provider)
    for _ in range(12):
        if browser._eval(script).get("ok") is True:
            if click:
                browser._invoke("click", '[data-meal-remove-control="true"]')
            return
        browser._settle(.25)
    raise HouseholdError(f"{spec['label']} removal controls do not match the reviewed items or total")


def _form(browser, pending):
    spec = _spec(pending["provider"])
    browser._open(f"{spec['origin']}/{spec['locale']}/account/orders/remove-items/{pending['order_id']}/")
    for item in pending["items"]:
        _control(browser, pending, "select", item["product_id"])
        _control(browser, pending, "option", item["product_id"])
    _control(browser, pending, "open")
    _control(browser, pending, "confirm", click=False)


def _read(app, order_id, deadline, binding=None):
    spec = _spec(app.provider)
    order = app.provider_client.call("get_order", {"order_number": order_id}, deadline=deadline)
    require_provider_identity(order, order_id)
    current_binding = app.browser.read_order_binding(order_id, order, deadline=deadline, expected_binding=binding)
    if binding is not None and current_binding != binding:
        raise HouseholdError(f"{spec['label']} removal account binding changed")
    source = app.browser._eval(_source_script(order_id, app.provider))
    if source.get("ok") is not True:
        raise HouseholdError(f"{spec['label']} removal source is unavailable")
    eligibility, details = source.get("eligibility"), source.get("details")
    if not isinstance(eligibility, dict) or not isinstance(details, dict):
        raise HouseholdError(f"{spec['label']} removal source is invalid")
    summary = details.get("summary")
    if (not isinstance(summary, dict) or eligibility.get("order_number") != order_id
            or summary.get("order_number") != order_id or eligibility.get("currency") != spec["currency"]
            or summary.get("currency") != spec["currency"] or money_cents(summary.get("gross_amount")) is None
            or not summary.get("delivery")):
        raise HouseholdError(f"{spec['label']} removal order identity, delivery or total is unavailable")
    _items(eligibility, app.provider)
    cart = cart_summary(app.provider_client.call("get_cart", {}, deadline=deadline))
    return {"eligibility": eligibility, "details": details, "cart": cart, "binding": current_binding}


def _guard(state, pending=None):
    if state.get("pending_checkout") or state.get("pending_cancellation") or state.get("pending_cart_change"):
        raise HouseholdError("Resolve the pending checkout, cancellation or cart change before removing ordered items")
    if canonical(state.get("order_change")) != canonical(pending):
        raise HouseholdError("An order change is already active or the removal changed")


def _view(pending):
    spec = _spec(pending["provider"])
    return {"provider": pending["provider"], "order_id": pending["order_id"], "confirmation_id": pending["confirmation_id"],
            "status": pending["status"], "items": deepcopy(pending["items"]), "currency": spec["currency"],
            "expected_credit_ore": pending["credit_ore"], "original_total_ore": pending["total_ore"],
            "confirmation_required": pending["status"] == "prepared"}


def _delivery(source, provider="oda"):
    spec = _spec(provider)
    delivery = source["details"]["summary"]["delivery"]
    if not isinstance(delivery, dict) or not delivery.get("delivery_address") or not delivery.get("delivery_time"):
        raise HouseholdError(f"{spec['label']} removal delivery address and time are unavailable")
    return {key: delivery[key] for key in ("delivery_address", "delivery_time")}


def _reconcile(app, pending, deadline):
    current = _read(app, pending["order_id"], deadline, pending["before"]["binding"])
    before = pending["before"]
    expected = {pid: r["uncredited_quantity"] for pid, r in _items(before["eligibility"], app.provider).items()}
    for item in pending["items"]:
        expected[item["product_id"]] = item["quantity"]
    observed = {pid: r["uncredited_quantity"] for pid, r in _items(current["eligibility"], app.provider).items()}
    quantities_match = {k:v for k,v in expected.items() if v} == {k:v for k,v in observed.items() if v}
    summary = current["details"]["summary"]
    total = money_cents(summary["gross_amount"])
    matched = (quantities_match and total == pending["total_ore"] - pending["credit_ore"]
               and _delivery(current, app.provider) == _delivery(before, app.provider))
    with app.store.locked() as state:
        if canonical(state.get("order_change")) != canonical(pending):
            raise HouseholdError("The pending removal changed during reconciliation")
        if matched:
            result = {**_view(pending), "status": "removed", "removed": True, "confirmation_required": False,
                      "new_total_ore": total, "credit_ore": pending["total_ore"] - total,
                      "cart_unchanged": current["cart"] == before["cart"],
                      "payment_resolution": {"refund": "unknown", "source": "merchant_order_summary"}}
            app._store_protected_result(state, pending["confirmation_id"], "order_reduction", result,
                                        target_id=pending["order_id"], intent_signature=_digest(pending["items"]))
            state["order_change"] = None
            return result
        state["order_change"]["status"] = "uncertain"
        return {**_view(state["order_change"]), "removed": False, "reconciliation_required": True,
                "message": "The exact quantity and order-total change is not verified; do not repeat removal"}


def orders_remove(app, request):
    """Called by OrderOperations._orders for the three remove_* actions."""
    if app.provider not in {"oda", "mathem"} or app.browser is None:
        raise HouseholdError("Ordered-item removal requires a logged-in Oda or Mathem browser")
    spec = _spec(app.provider)
    action = request.get("action")
    deadline = min(time.monotonic() + 105, request.get("_deadline") or float("inf"))
    # Check protected state before any browser navigation or session closure;
    # a rejected removal must not destroy an outstanding payment page.
    with app._browser_operation(deadline), app.browser._checkout_operation(deadline, preserve_session=True):
        if action == "remove_prepare":
            order_id = safe_order_id(request.get("order_id"))
            key = app._idempotency_key(request.get("idempotency_key"), "order_reduction")
            with app.store.locked() as state:
                existing = app._protected_request(state, "order_reduction", key, target_id=order_id)
                if existing:
                    if existing.get("requested_items") != request.get("items"):
                        raise HouseholdError("idempotency_key was already used for different removal items")
                    result = app._read_protected_result(state, existing["confirmation_id"], "order_reduction")
                    if result:
                        return result
                    pending = deepcopy(state.get("order_change"))
                    if pending and pending.get("confirmation_id") == existing["confirmation_id"]:
                        if pending.get("requested_items") != request.get("items"):
                            raise HouseholdError("idempotency_key was already used for different removal items")
                        return _view(pending)
                _guard(state)
                token = secrets.token_urlsafe(24)
                reservation = {"kind": "reduction", "provider": app.provider, "order_id": order_id,
                               "confirmation_id": token, "status": "preparing", "started_at": app._now().isoformat()}
                state["order_change"] = reservation
            try:
                before = _read(app, order_id, deadline)
                _delivery(before, app.provider)
                options, status = before["details"].get("options"), before["details"]["summary"].get("status")
                if (not isinstance(options, dict) or not isinstance(status, dict)
                        or options.get("can_remove_from_order") is not True
                        or status.get("can_remove_from_order") is not True):
                    raise HouseholdError(f"{spec['label']} no longer permits removing items from this order")
                items = _intent(request.get("items"), before["eligibility"], app.provider)
                pending = {**reservation, "status": "prepared", "before": before, "items": items,
                           "requested_items": deepcopy(request["items"]), "credit_ore": sum(r["credit_ore"] for r in items),
                           "total_ore": money_cents(before["details"]["summary"]["gross_amount"])}
                if items:
                    _form(app.browser, pending)
                with app.store.locked() as state:
                    _guard(state, reservation)
                    state["order_change"] = pending
                    app._bind_protected_request(state, "order_reduction", key, token, target_id=order_id)
                    state["protected_requests"]["order_reduction:" + key]["requested_items"] = deepcopy(request["items"])
                    if not items:
                        result = {**_view(pending), "status": "already_done", "already_done": True,
                                  "removed": False, "confirmation_required": False,
                                  "new_total_ore": pending["total_ore"], "credit_ore": 0}
                        app._store_protected_result(state, token, "order_reduction", result, target_id=order_id,
                                                    intent_signature=_digest(request["items"]))
                        state["order_change"] = None
                        return result
                return _view(pending)
            except Exception:
                with app.store.locked() as state:
                    if state.get("order_change") == reservation:
                        state["order_change"] = None
                raise
        if action not in {"remove_confirm", "remove_reconcile"}:
            raise HouseholdError("unsupported order removal action")
        token = request.get("confirmation_id")
        if not isinstance(token, str) or not token:
            raise HouseholdError("removal confirmation_id is required")
        with app.store.locked() as state:
            result = app._read_protected_result(state, token, "order_reduction")
            if result:
                if request.get("order_id") is not None and request["order_id"] != result["order_id"]:
                    raise HouseholdError("Removal order_id changed")
                return result
            pending = deepcopy(state.get("order_change"))
            if not pending or pending.get("kind") != "reduction" or pending.get("confirmation_id") != token:
                raise HouseholdError("No matching prepared order removal")
            if request.get("order_id") is not None and request["order_id"] != pending["order_id"]:
                raise HouseholdError("Removal order_id changed")
            _guard(state, pending)
        if pending["status"] in {"clicking", "uncertain"}:
            return _reconcile(app, pending, deadline)
        if action == "remove_reconcile":
            return _view(pending)
        current = _read(app, pending["order_id"], deadline, pending["before"]["binding"])
        if _digest(current) != _digest(pending["before"]):
            raise HouseholdError("Order, removable quantities, account or cart changed; prepare a new removal")
        _form(app.browser, pending)
        # Recheck merchant facts after local form preparation and immediately
        # before journaling the one permitted native submission.
        source = app.browser._eval(_source_script(pending["order_id"], app.provider))
        if source != {"ok": True, "eligibility": current["eligibility"], "details": current["details"]}:
            raise HouseholdError(f"{spec['label']} order changed while preparing the removal confirmation")
        fresh_cart = cart_summary(app.provider_client.call("get_cart", {}, deadline=deadline))
        if fresh_cart != current["cart"]:
            raise HouseholdError("Cart changed while preparing the removal confirmation")
        _control(app.browser, pending, "confirm", click=False)
        with app.store.locked() as state:
            _guard(state, pending)
            state["order_change"]["status"] = "clicking"
            pending = deepcopy(state["order_change"])
        try:
            app.browser._invoke("click", '[data-meal-remove-control="true"]')
            app.browser._settle(.25)
            return _reconcile(app, pending, deadline)
        except Exception:
            with app.store.locked() as state:
                if state.get("order_change") == pending:
                    state["order_change"]["status"] = "uncertain"
            raise HouseholdError(f"{spec['label']} removal result is uncertain; use remove_reconcile without another click") from None
