"""Review and reserve one exact delivery window through the attached Oda app."""
from datetime import timedelta
import hashlib
import hmac
import uuid
from zoneinfo import ZoneInfo

from core import contains_line_controls
import hosted_oda
from hosted_oda_menu import cart_amounts
from retail_mcp import retail_cart_delivery_matches_slot, retail_delivery_slot_date


BEFORE_WRITE = {"delivery_read", "delivery_review", "delivery_preflight"}
FINISHED = {"delivery_reserved", "delivery_observed", "delivery_already_selected", "delivery_inactive",
            "checkout_handoff", "checkout_cart_empty", "checkout_delivery_required"}
PENDING = {"delivery_dispatched", "delivery_readback"}
OBSERVATION_FIELDS = ("action", "frame_id", "plugin_id", "addresses", "address_observed_at",
                      "cart", "cart_observed_at", "slots", "slots_observed_at", "slot_arguments", "browser", "address_label")
OSLO = ZoneInfo("Europe/Oslo")


def begin(value, account):
    import hosted_oda_cart as journal

    slot_ref = value["slot_ref"]
    date = retail_delivery_slot_date(slot_ref)
    slot_id = int(slot_ref.rsplit(":", 1)[1])
    if not journal.provider_id(slot_id):
        raise ValueError("choose a dated Oda delivery reference from delivery.list")
    address = next(row["id"] for row in value["authorized_addresses"] if row["isSelected"])
    checkout = {"payment_method": value["payment_method"]} if value["action"] == "begin_checkout" else {}
    return {"account": account, "status": "delivery_read", "delivery": {
        "slot_ref": slot_ref, "slot_id": slot_id, "date": date, "address_id": address, **checkout}}


def frame(record, *, last_read=None, cutoff=None):
    import hosted_oda_cart as journal

    issued = journal.now()
    duration = timedelta(minutes=5)
    expires = issued + duration if last_read is None else min(last_read + timedelta(minutes=2), cutoff)
    if expires <= issued:
        raise ValueError("the Oda delivery quote or cutoff expired")
    delivery = record["delivery"]
    common = {"frame_id": uuid.uuid4().hex, "plugin_id": record["plugin_id"],
              "issued_at": issued.isoformat(), "expires_at": expires.isoformat()}
    address = {"delivery_address_id": delivery["address_id"]}
    if last_read is not None:
        arguments = {**address, "delivery_slot_id": delivery["slot_id"]}
        unattended = delivery["baseline"]["unattended"]
        if unattended is not None:
            arguments["is_unattended_delivery"] = unattended
        return {**common, "tool": "select_delivery_slot", "arguments": arguments}
    return {**common, "tools": [
        {"tool": "get_delivery_addresses", "arguments": {}},
        {"tool": "get_cart", "arguments": {}},
        {"tool": "get_delivery_slots", "arguments": {**address, "delivery_date": delivery["date"]}},
    ]}


def selected_slot(value):
    from hosted_oda_cart import provider_id

    if (not isinstance(value, dict) or value.keys() - {"id", "cutoffTime", "expireAt", "name"}
            or not provider_id(value.get("id"))):
        raise ValueError("Oda did not supply a structured selected slot")
    name = value.get("name")
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 200 or contains_line_controls(name)):
        raise ValueError("Oda selected delivery display changed")
    expires = value.get("expireAt")
    return {"slot_id": value["id"], "display": name, "cutoff_at": hosted_oda.response_time(value.get("cutoffTime")),
            "expires_at": hosted_oda.response_time(expires) if expires is not None else None}


def inspect_cart(cart):
    from hosted_oda_cart import cart_quantities

    required = hosted_oda.CART_FIELDS | {"deliverySlot", "isUnattendedDelivery", "deliveryAddress"}
    if not isinstance(cart, dict) or cart.keys() != required:
        raise ValueError("Oda delivery requires a complete cart and explicit slot and unattended values")
    view = hosted_oda.cart_view({field: cart[field] for field in hosted_oda.CART_FIELDS})
    if cart_quantities({**cart, "deliverySlot": None}) is None:
        raise ValueError("Oda delivery requires complete ungrouped cart quantities")
    unattended = cart["isUnattendedDelivery"]
    if unattended is not None and type(unattended) is not bool:
        raise ValueError("Oda unattended delivery state is unavailable")
    lines = [hosted_oda.cart_line(line) for group in cart["groups"] for line in group["items"]]
    goods = hosted_oda.arguments_digest(sorted(lines, key=lambda line: line["product"]["id"]))
    slot = selected_slot(cart["deliverySlot"]) if cart["deliverySlot"] is not None else None
    baseline = {"goods_sha256": goods, "amounts_ore": cart_amounts(cart), "unattended": unattended}
    return view, baseline, slot


def browser_context(value, key, address_id):
    from hosted_oda_cart import require_fields

    browser = value["browser"]
    require_fields(browser, ("browser_id", "tab_id", "address_id", "new_cart", "cart_url"))
    if (browser["new_cart"] is not True or type(browser["address_id"]) is not int or browser["address_id"] != address_id
            or browser["cart_url"] not in hosted_oda.CART_LINKS
            or any(not isinstance(browser[field], str) or not 1 <= len(browser[field]) <= 150
                   or not browser[field].isprintable() for field in ("browser_id", "tab_id"))):
        raise ValueError("verify the original dedicated Oda browser, account and ordinary cart")
    label = value["address_label"]
    actual = value["cart"]["deliveryAddress"]
    if (not isinstance(label, str) or not 1 <= len(label) <= 500 or contains_line_controls(label)
            or (actual is not None and (not isinstance(actual, str) or " ".join(actual.split()) != " ".join(label.split())))
            or (value["cart"]["deliverySlot"] is not None and actual is None)):
        raise ValueError("the cart delivery address does not match the selected Oda address")
    address = hmac.new(bytes.fromhex(key), " ".join(label.split()).encode(), hashlib.sha256).hexdigest()
    return hosted_oda.arguments_digest(browser), address


def inactive_readback(record, baseline, current, selected, observed, cart_date):
    delivery = record["delivery"]
    boundary = hosted_oda.response_time(delivery["dispatch_expires_at"])
    offered = delivery["review"]["window"]
    expiry = delivery.get("ack_expiry") or offered["cutoff_at"]
    ended = delivery.get("acknowledged") is False or observed >= hosted_oda.response_time(expiry)
    same_slot = current is not None and current["slot_id"] == delivery["slot_id"]
    if same_slot and current["display"] is not None:
        exact = {key: val for key, val in offered.items() if key != "cutoff_at"}
        same_slot = retail_cart_delivery_matches_slot(current, {**exact, "selected": True}, today=cart_date)
    if not same_slot and delivery["slot_id"] not in selected and observed >= boundary and ended:
        record["status"] = "delivery_inactive"
        delivery["reconciled_at"] = observed.isoformat()
        delivery["original_cart_still_matches"] = current is None and baseline == delivery["baseline"]
        record.pop("frame")
        return True
    return False


def observe(record, value, key):
    import hosted_oda_cart as journal

    journal.require_fields(value, OBSERVATION_FIELDS)
    status, pending = record["status"], record["frame"]
    address_time, cart_time = journal.bound_times(record, pending, value, key, "cart_observed_at")
    slot_time = hosted_oda.response_time(value["slots_observed_at"])
    if not (cart_time <= slot_time <= journal.now() < hosted_oda.response_time(pending["expires_at"])
            and hosted_oda.arguments_digest(value["slot_arguments"]) == hosted_oda.arguments_digest(pending["tools"][-1]["arguments"])):
        raise ValueError("the original dated Oda address and delivery observation changed")
    slots = value["slots"]
    normalized = hosted_oda.delivery_view(slots, value["slot_arguments"])
    delivery = record["delivery"]
    view, baseline, current = inspect_cart(value["cart"])
    context, address = browser_context(value, key, delivery["address_id"])
    if "physical_address" in delivery and not hmac.compare_digest(delivery["physical_address"], address):
        raise ValueError("the reviewed Oda physical delivery address changed")
    selected = [raw["id"] for raw in slots["slots"] if raw["isSelected"]]
    cart_date = cart_time.astimezone(OSLO).date()
    payment = delivery.get("payment_method")
    if payment is not None and (value["cart"]["productQuantityCount"] == 0 or (current is None and not selected)):
        record["status"] = "checkout_cart_empty" if value["cart"]["productQuantityCount"] == 0 else "checkout_delivery_required"
        record.pop("frame")
        return False
    if status == "delivery_readback" and inactive_readback(record, baseline, current, selected, address_time, cart_date):
        return False
    if "browser_context" in delivery and not hmac.compare_digest(delivery["browser_context"], context):
        raise ValueError("the Oda checkout browser changed")
    slot = next((row for row in normalized["slots"] if row["slot_ref"] == delivery["slot_ref"]), None)
    if slot is None or slot["price_kind"] != "exact":
        raise ValueError("the exact Oda delivery slot or price is unavailable")
    raw = next(row for row in slots["slots"] if row["id"] == delivery["slot_id"])
    cutoff = hosted_oda.response_time(raw.get("cutoffTime"))
    if not slot_time <= journal.now() < min(cutoff, hosted_oda.response_time(slot["start_at"])):
        raise ValueError("the Oda delivery window or cutoff expired")
    offer = {key: val for key, val in slot.items() if key != "selected"}
    offer["cutoff_at"] = cutoff.isoformat()
    if status == "delivery_readback" or (status == "delivery_read" and slot["selected"]):
        expiry_times = [hosted_oda.response_time(t) for t in (raw.get("expireAt"), delivery.get("ack_expiry")) if t is not None]
        if current is not None and current["expires_at"] is not None:
            expiry_times.append(current["expires_at"])
        expiry = min(expiry_times, default=None)
        if (selected != [delivery["slot_id"]] or current is None or current["cutoff_at"] != cutoff
                or current["display"] is None or (expiry is not None and expiry <= journal.now())
                or not retail_cart_delivery_matches_slot(current, slot, today=cart_date)
                or baseline["amounts_ore"]["deliveryFee"] > slot["price_ore"]):
            raise ValueError("fresh Oda reads do not establish the exact active reservation")
        if status == "delivery_readback":
            before = delivery["baseline"]
            old_amounts, new_amounts = before["amounts_ore"], baseline["amounts_ore"]
            if (offer != delivery["review"]["window"] or before["goods_sha256"] != baseline["goods_sha256"]
                    or before["unattended"] != baseline["unattended"]
                    or any(old_amounts[field] != new_amounts[field] for field in old_amounts.keys() - {"deliveryFee", "total"})
                    or new_amounts["total"] - old_amounts["total"] != new_amounts["deliveryFee"] - old_amounts["deliveryFee"]):
                raise ValueError("Oda reservation readback differs from the reviewed goods, prices or window")
            record["status"] = "delivery_reserved" if delivery.get("acknowledged") else "delivery_observed"
        elif payment is not None:
            record["status"] = "checkout_handoff"
            delivery["handoff"] = {"url": view["cart_url"], "requested_payment_method": payment,
                                   "approval": "complete_in_shop", "payment_status": "unverified",
                                   "amount_scope": "attached_app_cart_only", "browser_checkout_review": "required"}
        else:
            record["status"] = "delivery_already_selected"
        delivery["reservation"] = {"window": offer, "expires_at": expiry.isoformat() if expiry is not None else None,
                                   "cart": view, "observed_at": slot_time.isoformat()}
        record.pop("frame")
        return False
    if current is not None or selected:
        raise ValueError("use the Oda shop to change an existing selection or order")
    if payment is not None:
        raise ValueError("the requested checkout window is not selected")
    if status == "delivery_read":
        review = {"window": offer, "cart": view, "unattended": baseline["unattended"]}
        delivery.update(baseline=baseline, browser_context=context, physical_address=address,
                        review=review, review_digest=hosted_oda.arguments_digest({
            "operation_id": record["operation_id"], "account": record["account"], "baseline": baseline,
            "browser_context": context, "physical_address": address, "review": review}))
        record["status"] = "delivery_review"
        record.pop("frame")
        return False
    if offer != delivery["review"]["window"] or baseline != delivery["baseline"]:
        raise ValueError("the approved Oda cart, window or fee changed; prepare a new review")
    record["status"] = "delivery_dispatched"
    record["frame"] = frame(record, last_read=address_time, cutoff=min(cutoff, hosted_oda.response_time(slot["start_at"])))
    delivery["dispatch_expires_at"] = record["frame"]["expires_at"]
    return True


def acknowledge(record, value):
    import hosted_oda_cart as journal

    journal.require_fields(value, ("action", "frame_id", "plugin_id", "tool", "arguments", "dispatched_at",
                                  "observed_at", "result_source", "is_error", "slot"))
    pending = record["frame"]
    journal.match_frame(pending, value)
    dispatched = hosted_oda.response_time(value["dispatched_at"])
    observed = hosted_oda.response_time(value["observed_at"])
    if (value["tool"] != pending["tool"] or value["result_source"] != "original_attached_mutation_response"
            or hosted_oda.arguments_digest(value["arguments"]) != hosted_oda.arguments_digest(pending["arguments"])
            or not hosted_oda.response_time(pending["issued_at"]) <= dispatched < hosted_oda.response_time(pending["expires_at"])
            or not dispatched <= observed <= journal.now() or type(value["is_error"]) is not bool):
        raise ValueError("the response does not belong to the original Oda delivery dispatch")
    delivery = record["delivery"]
    if value["is_error"]:
        if value["slot"] is not None:
            raise ValueError("a provider error does not carry a successful slot response")
    else:
        selected = selected_slot(value["slot"])
        reviewed = delivery["review"]["window"]
        slot = {key: val for key, val in reviewed.items() if key != "cutoff_at"}
        slot["selected"] = True
        if (selected["slot_id"] != delivery["slot_id"]
                or selected["cutoff_at"] != hosted_oda.response_time(reviewed["cutoff_at"])
                or (selected["display"] is not None and not retail_cart_delivery_matches_slot(
                    selected, slot, today=observed.astimezone(OSLO).date()))):
            raise ValueError("the original Oda response does not match the approved window")
        if selected["expires_at"] is not None:
            delivery["ack_expiry"] = selected["expires_at"].isoformat()
    delivery["acknowledged"] = not value["is_error"]
    delivery["response_sha256"] = hosted_oda.arguments_digest(value["slot"])
    record["status"] = "delivery_readback"
    record["frame"] = frame(record)


def advance(record, value, key):
    from hosted_oda_cart import now

    action, status = value["action"], record["status"]
    if action == "observe_delivery" and status in {"delivery_read", "delivery_preflight", "delivery_readback"}:
        return observe(record, value, key)
    if action == "approve_delivery" and status == "delivery_review" and value["review_digest"] == record["delivery"]["review_digest"]:
        record["status"] = "delivery_preflight"
    elif action == "acknowledge_delivery" and status == "delivery_dispatched":
        acknowledge(record, value)
        return False
    elif action == "recheck" and status in PENDING:
        if status == "delivery_dispatched" and now() < hosted_oda.response_time(record["frame"]["expires_at"]):
            raise ValueError("wait for the original Oda dispatch frame to expire before reconciling")
        record["status"] = "delivery_readback"
    elif action == "retry_read" and status in {"delivery_read", "delivery_preflight"}:
        pass
    elif action == "abort" and status in BEFORE_WRITE:
        record["status"] = "aborted"
        record.pop("frame", None)
        return False
    else:
        raise ValueError("use the exact Oda delivery review or reconcile the original reservation")
    record["frame"] = frame(record)
    return False
