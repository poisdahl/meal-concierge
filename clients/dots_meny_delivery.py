"""One reviewed MENY delivery selection in the native cloud browser."""
from datetime import datetime, timedelta, timezone
import re
import time
import uuid

from clients import dots
from core import HouseholdError
from meny import (meny_delivery_list_price_display, meny_delivery_window_identity, meny_label_slot_ref,
                  meny_selected_delivery, normalize_delivery_slot_ref)


STATE_KEY = "native_delivery_selection"
PENDING = {"dispatching", "dispatched"}
EVIDENCE = {"evidence_kind": "host_attested_rendered_ui", "backend_freshness": "unverified"}
ORDINARY = {"pending_checkout", "pending_cart_change", "pending_cancellation", "order_change"}


def digest(value):
    return dots.digest(dots.encoded(value))


def validate(request):
    action = request["action"]
    fields = {"operation", "action"} | ({"slot_ref"} if action == "prepare" else {"review_id", "review_digest"})
    if action == "select":
        fields |= {"reservation_approved"}
    if action == "accept_current":
        fields |= {"owner_accepted_unknown", "original_executor_stopped"}
    dots.object_fields(request, fields, fields)
    if action == "prepare":
        if normalize_delivery_slot_ref(request["slot_ref"])[0] != request["slot_ref"]:
            raise ValueError("use the exact listed MENY window")
    elif (not isinstance(request["review_id"], str) or str(uuid.UUID(request["review_id"])) != request["review_id"]
            or not isinstance(request["review_digest"], str) or re.fullmatch(r"[a-f0-9]{64}", request["review_digest"]) is None):
        raise ValueError("use the exact original MENY delivery review and digest")
    if action == "select" and request["reservation_approved"] is not True:
        raise ValueError("the owner must approve this exact MENY reservation")
    if action == "accept_current" and (request["owner_accepted_unknown"] is not True or request["original_executor_stopped"] is not True):
        raise ValueError("archive only the exact owner-approved original attempt after its executor has stopped")


def context(host):
    value = host.exchange("delivery_context", {})
    fields = {"source_url", "authenticated", "ordinary_cart", "home_delivery", "address_sha256", "delivery"}
    dots.object_fields(value, fields, fields)
    if (value["source_url"] != "https://meny.no/varer"
            or any(value[field] is not True for field in ("authenticated", "ordinary_cart", "home_delivery"))
            or not isinstance(value["address_sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", value["address_sha256"]) is None):
        raise HouseholdError("verify the original MENY cart and its current home delivery address")
    if value["delivery"] is not None:
        meny_delivery_window_identity(value["delivery"])
    return value


def review_inputs(host, slot_ref, *, allow_inactive=False):
    window, _ = normalize_delivery_slot_ref(slot_ref)
    started = time.monotonic()
    # Read the entire picker so a different date's selection cannot be hidden.
    slots = host.call("get_delivery_slots", {})
    selected = meny_selected_delivery(slots["slots"])
    live = context(host)
    cart = host.call("get_cart", {})
    cart_delivery = (cart.get("delivery") or {}).get("display")
    expected_delivery = meny_delivery_window_identity(selected["display"]) if selected else None
    observed = [meny_delivery_window_identity(source) if source is not None else None
                for source in (live["delivery"], cart_delivery)]
    if observed[0] != expected_delivery or observed[1] not in (None, expected_delivery):
        raise HouseholdError("the MENY picker and current cart disagree about delivery")
    if cart["items"] and expected_delivery is not None and observed[1] is None:
        raise HouseholdError("the MENY cart did not confirm its selected delivery")
    goods = sorted(cart["items"], key=lambda item: item["product_id"])
    if len({item["product_id"] for item in goods}) != len(goods):
        raise HouseholdError("MENY delivery needs unambiguous product quantities")
    binding = {"address_sha256": live["address_sha256"], "goods_sha256": digest(goods),
               "product_subtotal": cart["product_subtotal"], "selected": selected["slot_ref"] if selected else None}
    target = next((row for row in slots["slots"] if row["slot_ref"] == window), None)
    if target is None or datetime.fromisoformat(target["start_at"]) <= datetime.now(timezone.utc):
        if allow_inactive:
            return None, binding, started
        raise HouseholdError("the exact MENY delivery window is unavailable")
    display = slots["display"][window]
    if meny_label_slot_ref(display)[0] != window:
        raise HouseholdError("the displayed MENY delivery date changed")
    price = meny_delivery_list_price_display(target, display)
    view = {"window": {key: val for key, val in target.items() if key != "selected"},
            "calendar_price_display": price,
            "calendar_price_scope": "estimate", "delivery_address_sha256": live["address_sha256"],
            "cart_count": cart["count"], "cart_total": cart["total"], "currency": "NOK"}
    return view, {**binding, "calendar_label": display}, started


def view(record):
    return {**{key: record[key] for key in ("status", "review_id", "review_digest", "review", "expires_at")},
            "dispatch": False, **EVIDENCE,
            **({"observed_at": record["observed_at"], "original_dispatch_outcome": record["dispatch_outcome"]}
               if "observed_at" in record else {})}


def readback(host, store, record, *, accept_current=False):
    window = record["review"]["window"]
    reviewed, current, _ = review_inputs(host, window["slot_ref"], allow_inactive=True)
    observed = datetime.now(timezone.utc)
    target_selected = current["selected"] == window["slot_ref"]
    same_cart = all(current[field] == record["binding"][field] for field in ("goods_sha256", "product_subtotal"))
    inactive = not target_selected and (accept_current or observed >= dots.timestamp(window["end_at"]))
    if (current["address_sha256"] != record["binding"]["address_sha256"]
            or (not inactive and (not target_selected or reviewed is None or not same_cart))):
        raise HouseholdError("the selected MENY window, address or original cart is unverified; reconcile without resend")
    observed_price = reviewed["calendar_price_display"] if reviewed else None
    price_matches = (current.get("calendar_label") == record["binding"]["calendar_label"]
                     and observed_price == record["review"]["calendar_price_display"])
    status = ("delivery_inactive" if inactive else
              "delivery_selection_observed" if price_matches else "delivery_price_drift_observed")
    record = {**record, "status": status, "observed_at": observed.isoformat(),
              "dispatch_outcome": "host_acknowledged" if record["status"] == "dispatched" else "unknown",
              "readback_calendar_price_display": observed_price, "original_goods_still_match": same_cart}
    with store.locked() as state:
        state[STATE_KEY] = record
    return {**view(record), "readback_calendar_price_display": record["readback_calendar_price_display"]}


def handle(host, store, request, command_id):
    action = request["action"]
    state = store.read()
    record = state.get(STATE_KEY)
    if any(state.get(key) for key in ORDINARY):
        raise HouseholdError("reconcile the original MENY cart, checkout or order before working on delivery")
    if action == "prepare":
        if record and record["status"] in PENDING:
            raise HouseholdError("reconcile the original MENY delivery attempt before preparing another window")
        review, binding, _ = review_inputs(host, request["slot_ref"])
        if binding["selected"] is not None:
            if binding["selected"] == review["window"]["slot_ref"]:
                return {"status": "delivery_already_selected", "review": review, "dispatch": False, **EVIDENCE}
            raise HouseholdError("change an already selected MENY window in the shop")
        if review["calendar_price_display"] == "pris ikke tilgjengelig":
            raise HouseholdError("MENY shows no calendar fee for this window; choose a priced window")
        record = {"status": "delivery_review", "review_id": command_id, "review": review, "binding": binding,
                  "browser_binding_sha256": digest(host.config["browser_binding"]),
                  "review_digest": digest({"review_id": command_id, "browser": host.config["browser_binding"],
                                           "review": review, "binding": binding}),
                  "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
        with store.locked() as current:
            current[STATE_KEY] = record
        return view(record)
    if (not record or any(record[field] != request[field] for field in ("review_id", "review_digest"))
            or record["browser_binding_sha256"] != digest(host.config["browser_binding"])):
        raise HouseholdError("use the exact original MENY delivery review and approval")
    if action in {"reconcile", "accept_current"}:
        if record["status"] in {"delivery_selection_observed", "delivery_price_drift_observed", "delivery_inactive"}:
            return {**view(record), "observation_scope": "historical",
                    "readback_calendar_price_display": record["readback_calendar_price_display"]}
        if record["status"] not in PENDING:
            raise HouseholdError("no dispatch to reconcile for this MENY delivery review")
        if datetime.now(timezone.utc) < dots.timestamp(record["dispatch_expires_at"]):
            raise HouseholdError("wait for the original MENY delivery dispatch frame to expire")
        return readback(host, store, record, accept_current=action == "accept_current")
    if record["status"] != "delivery_review" or datetime.now(timezone.utc) >= dots.timestamp(record["expires_at"]):
        raise HouseholdError("the approval expired or the original MENY delivery attempt needs reconciliation")
    slot_ref = record["review"]["window"]["slot_ref"]
    review, binding, started = review_inputs(host, slot_ref)
    if review != record["review"] or binding != record["binding"] or datetime.now(timezone.utc) >= dots.timestamp(record["expires_at"]):
        raise HouseholdError("MENY address, cart or calendar offer changed; prepare a new review")
    dispatched = host.select_delivery(record, started)
    if dispatched["status"] == "delivery_selection_not_dispatched":
        return view(dispatched)
    return readback(host, store, dispatched)
