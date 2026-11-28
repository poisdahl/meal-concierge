"""Review and reserve one exact delivery window through the attached Oda app."""
from datetime import timedelta
import hashlib
import hmac
import uuid
from zoneinfo import ZoneInfo

from core import contains_line_controls
import hosted_oda
import hosted_oda_checkout as checkout
from hosted_oda_menu import cart_amounts
from retail_mcp import normalize_retail_delivery_slot, retail_cart_delivery_matches_slot, retail_delivery_slot_date


BEFORE_WRITE = {"delivery_read", "delivery_review", "delivery_preflight"} | checkout.BEFORE_WRITE
FINISHED = {"delivery_reserved", "delivery_observed", "delivery_already_selected", "delivery_inactive",
            "delivery_archived_unknown", "checkout_handoff", "checkout_cart_empty", "checkout_delivery_required"} | checkout.FINISHED
RESERVATION_PENDING = {"delivery_dispatched", "delivery_readback"}
PENDING = RESERVATION_PENDING | checkout.PENDING
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
    payment = {"payment_method": value["payment_method"]} if value["action"] == "begin_checkout" else {}
    if value.get("owner_handoff") is True:
        payment["owner_handoff"] = True
    if value["action"] == "begin_saved_card_checkout":
        payment = {"payment_method": "saved_card", "native_saved_card": True}
    elif value["action"] == "begin_vipps_checkout":
        payment = {"payment_method": "vipps", "native_vipps": True, "maximum_payable_ore": value["maximum_payable_ore"]}
    return {"account": account, "status": "delivery_read", "delivery": {
        "slot_ref": slot_ref, "slot_id": slot_id, "date": date, "address_id": address, **payment}}


def frame(record, *, last_read=None, cutoff=None):
    import hosted_oda_cart as journal

    issued = journal.now()
    duration = timedelta(minutes=5)
    expires = issued + duration if last_read is None else min(last_read + timedelta(minutes=2), cutoff)
    delivery = record["delivery"]
    if record["status"] in {"delivery_preflight", "delivery_dispatched", "checkout_preflight"}:
        expires = min(expires, hosted_oda.response_time(delivery["review_expires_at"]))
    if expires <= issued:
        raise ValueError("the Oda delivery quote or cutoff expired")
    common = {"frame_id": uuid.uuid4().hex, "plugin_id": record["plugin_id"],
              "issued_at": issued.isoformat(), "expires_at": expires.isoformat()}
    address = {"delivery_address_id": delivery["address_id"]}
    if last_read is not None:
        arguments = {**address, "delivery_slot_id": delivery["slot_id"]}
        unattended = delivery["baseline"]["unattended"]
        if unattended is not None:
            arguments["is_unattended_delivery"] = unattended
        return {**common, "tool": "select_delivery_slot", "arguments": arguments}
    tools = [{"tool": name, "arguments": {}} for name in ("get_delivery_addresses", "get_cart")]
    addition = delivery.get("addition")
    if addition:
        tools.extend({"tool": name, "arguments": {"order_number": addition["order_id"]}}
                     for name in ("get_order", "order_tracking"))
    else:
        tools.append({"tool": "get_delivery_slots", "arguments": {**address, "delivery_date": delivery["date"]}})
    if record["status"] == "checkout_preflight" or delivery.get("owner_handoff"):
        tools.append({"tool": "get_orders", "arguments": checkout.ORDERS_ARGUMENTS})
    if checkout.native_method(delivery):
        return {**common, "tools": tools, "native_browser": checkout.review_request(record)}
    return {**common, "tools": tools}


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


def inspect_cart(cart, *, allow_grouped=False):
    from hosted_oda_cart import cart_quantities

    required = hosted_oda.CART_FIELDS | {"deliverySlot", "isUnattendedDelivery", "deliveryAddress"}
    if not isinstance(cart, dict) or cart.keys() != required:
        raise ValueError("Oda delivery requires a complete cart and explicit slot and unattended values")
    view = hosted_oda.cart_view({field: cart[field] for field in hosted_oda.CART_FIELDS})
    ordinary_quantities = cart_quantities({**cart, "deliverySlot": None}) is not None
    if not ordinary_quantities and not allow_grouped:
        raise ValueError("Oda delivery requires complete ungrouped cart quantities")
    unattended = cart["isUnattendedDelivery"]
    if unattended is not None and type(unattended) is not bool:
        raise ValueError("Oda unattended delivery state is unavailable")
    lines = [hosted_oda.cart_line(line) for group in cart["groups"] for line in group["items"]]
    goods = hosted_oda.arguments_digest(sorted(lines, key=lambda line: line["product"]["id"]))
    slot = selected_slot(cart["deliverySlot"]) if cart["deliverySlot"] is not None else None
    baseline = {"goods_sha256": goods, "amounts_ore": cart_amounts(cart), "unattended": unattended,
                "ordinary_quantities": ordinary_quantities}
    return view, baseline, slot


def browser_context(value, key, address_id):
    from hosted_oda_cart import require_fields

    browser = value["browser"]
    require_fields(browser, ("browser_id", "tab_id", "address_id", "new_cart", "cart_url"))
    if (browser["new_cart"] is not True or type(browser["address_id"]) is not int or browser["address_id"] != address_id
            or not isinstance(browser["cart_url"], str) or browser["cart_url"] not in hosted_oda.CART_LINKS
            or any(not isinstance(browser[field], str) or not 1 <= len(browser[field]) <= 150
                   or not browser[field].isprintable() for field in ("browser_id", "tab_id"))):
        raise ValueError("verify the original dedicated Oda browser, account and ordinary cart")
    label = value["address_label"]
    actual = value["cart"]["deliveryAddress"]
    if (not isinstance(label, str) or not 1 <= len(label) <= 500 or contains_line_controls(label)
            or (actual is not None and (not isinstance(actual, str) or " ".join(actual.split()) != " ".join(label.split())))
            or (value["cart"]["deliverySlot"] is not None and actual is None)):
        raise ValueError("the cart delivery address does not match the selected Oda address")
    address = physical_address_fingerprint(label, key)
    # The two allowlisted official cart links identify the same browser surface.
    return hosted_oda.arguments_digest({**browser, "cart_url": "https://oda.com/no/cart/"}), address


def physical_address_fingerprint(label, key):
    if not isinstance(label, str) or not 1 <= len(label) <= 500 or not label.strip() or contains_line_controls(label):
        raise ValueError("the Oda physical delivery address is unavailable")
    return hmac.new(bytes.fromhex(key), " ".join(label.split()).encode(), hashlib.sha256).hexdigest()


def inactive_readback(record, baseline, current, selected, observed, cart_date, *, address_changed):
    delivery = record["delivery"]
    boundary = hosted_oda.response_time(delivery["dispatch_expires_at"])
    offered = delivery["review"]["window"]
    expiry = delivery.get("ack_expiry") or offered["cutoff_at"]
    ended = (delivery.get("acknowledged") is False or observed >= hosted_oda.response_time(expiry)
             or "owner_accepted_unknown_at" in delivery)
    same_slot = current is not None and current["slot_id"] == delivery["slot_id"]
    if same_slot and current["display"] is not None:
        exact = {key: val for key, val in offered.items() if key != "cutoff_at"}
        same_slot = retail_cart_delivery_matches_slot(current, {**exact, "selected": True}, today=cart_date)
    if not same_slot and delivery["slot_id"] not in selected and observed >= boundary and ended:
        record["status"] = "delivery_archived_unknown" if "owner_accepted_unknown_at" in delivery else "delivery_inactive"
        delivery["reconciled_at"] = observed.isoformat()
        delivery["original_cart_still_matches"] = not address_changed and current is None and baseline == delivery["baseline"]
        delivery["observed_current_delivery"] = ({key: current[key] for key in ("slot_id", "display")}
                                                 if current is not None else None)
        delivery["observed_address_changed"] = address_changed
        record.pop("frame")
        return True
    return False


def observe(record, value, key):
    import hosted_oda_cart as journal

    status, pending = record["status"], record["frame"]
    delivery = record["delivery"]
    fields = OBSERVATION_FIELDS + (("checkout",) if checkout.native_method(delivery) else ())
    if status == "checkout_preflight" or delivery.get("owner_handoff"):
        fields += ("existing_orders", "orders_observed_at")
    journal.require_fields(value, fields)
    accepting = status == "delivery_readback" and "owner_accepted_unknown_at" in delivery
    address_time, cart_time = journal.bound_times(record, pending, value, key, "cart_observed_at", check_account=not accepting)
    slot_time = hosted_oda.response_time(value["slots_observed_at"])
    if not (cart_time <= slot_time <= journal.now() < hosted_oda.response_time(pending["expires_at"])
            and hosted_oda.arguments_digest(value["slot_arguments"]) == hosted_oda.arguments_digest(pending["tools"][2]["arguments"])):
        raise ValueError("the original dated Oda address and delivery observation changed")
    slots = value["slots"]
    normalized = hosted_oda.delivery_view(slots, value["slot_arguments"])
    view, baseline, current = inspect_cart(value["cart"], allow_grouped=status == "delivery_readback")
    context, address = browser_context(value, key, delivery["address_id"])
    physical_changed = "physical_address" in delivery and not hmac.compare_digest(delivery["physical_address"], address)
    address_set = journal.address_selection(value["addresses"])
    if accepting:
        original_addresses = [{"id": identity, "isSelected": identity == delivery["address_id"]}
                              for identity in address_set["ids"]]
        if (delivery["address_id"] not in address_set["ids"]
                or not hmac.compare_digest(record["account"], journal.account_fingerprint(original_addresses, key))
                or not hmac.compare_digest(delivery["browser_context"], context)):
            raise ValueError("verify the original Oda account, saved addresses and dedicated tab before archiving")
    if physical_changed and not accepting:
        raise ValueError("the reviewed Oda physical delivery address changed")
    selected = [raw["id"] for raw in slots["slots"] if raw["isSelected"]]
    cart_date = cart_time.astimezone(OSLO).date()
    payment = delivery.get("payment_method")
    if payment is not None and status == "delivery_read" and (value["cart"]["productQuantityCount"] == 0 or (current is None and not selected)):
        record["status"] = "checkout_cart_empty" if value["cart"]["productQuantityCount"] == 0 else "checkout_delivery_required"
        record.pop("frame")
        return False
    address_changed = physical_changed or address_set["selected"] != delivery["address_id"]
    if status == "delivery_readback" and inactive_readback(record, baseline, current, selected, address_time, cart_date,
                                                           address_changed=address_changed):
        return False
    if accepting and (address_changed or not hmac.compare_digest(record["account"], journal.account_fingerprint(value["addresses"], key))):
        raise ValueError("the original Oda selection remains unresolved on the changed address or account")
    if not baseline["ordinary_quantities"]:
        raise ValueError("the Oda delivery readback no longer contains the reviewed ordinary cart")
    if "browser_context" in delivery and not hmac.compare_digest(delivery["browser_context"], context):
        raise ValueError("the Oda checkout browser changed")
    slot = next((row for row in normalized["slots"] if row["slot_ref"] == delivery["slot_ref"]), None)
    raw = next((row for row in slots["slots"] if row["id"] == delivery["slot_id"]), None)
    if (slot is None and raw is not None and raw["isSelected"] and raw["isFull"] and not raw["isUnavailable"]
            and status in {"delivery_read", "delivery_readback", "checkout_preflight"}):
        # The window can be sold out to other shoppers while this cart holds it.
        slot = normalize_retail_delivery_slot(raw)
    if slot is None or slot["price_kind"] != "exact":
        raise ValueError("the exact Oda delivery slot or price is unavailable")
    cutoff = hosted_oda.response_time(raw.get("cutoffTime"))
    checked = journal.now()
    before_cutoff = checked < cutoff
    checking_existing = status in {"delivery_readback", "delivery_read"} and slot["selected"] and payment is None
    if not slot_time <= checked < hosted_oda.response_time(slot["start_at"]) or (not before_cutoff and not checking_existing):
        raise ValueError("the Oda delivery window or cutoff expired")
    offer = {key: val for key, val in slot.items() if key != "selected"}
    offer["cutoff_at"] = cutoff.isoformat()
    if status == "delivery_readback" or (status in {"delivery_read", "checkout_preflight"} and slot["selected"]):
        expiry_times = [hosted_oda.response_time(t) for t in (raw.get("expireAt"), delivery.get("ack_expiry")) if t is not None]
        if current is not None and current["expires_at"] is not None:
            expiry_times.append(current["expires_at"])
        expiry = min(expiry_times, default=None)
        if (selected != [delivery["slot_id"]] or current is None or current["cutoff_at"] != cutoff
                or current["display"] is None or (expiry is not None and expiry <= journal.now())
                or (not before_cutoff and expiry is None)
                or not retail_cart_delivery_matches_slot(current, slot, today=cart_date)
                or baseline["amounts_ore"]["deliveryFee"] > slot["price_ore"]):
            raise ValueError("fresh Oda reads do not establish the exact active reservation")
        reservation = {"window": offer, "expires_at": expiry.isoformat() if expiry is not None else None,
                       "booking_cutoff_passed": not before_cutoff, "cart": view, "observed_at": slot_time.isoformat()}
        if checkout.native_method(delivery):
            return checkout.observe_review(record, value, baseline, reservation, context, address, slot_time)
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
                                   "approval": "complete_in_shop" if delivery.get("owner_handoff") else "review_only",
                                   "payment_status": "unverified",
                                   "amount_scope": "attached_app_cart_only", "browser_checkout_review": "required"}
            if delivery.get("owner_handoff"):
                checkout.protect_owner_handoff(record, value, baseline, reservation, context, address, slot_time)
                return False
        else:
            record["status"] = "delivery_already_selected"
        delivery["reservation"] = reservation
        record.pop("frame")
        return False
    if current is not None or selected:
        raise ValueError("use the Oda shop to change an existing selection or order")
    if payment is not None:
        raise ValueError("the requested checkout window is not selected")
    if status == "delivery_read":
        review = {"window": offer, "cart": view, "unattended": baseline["unattended"]}
        expiry = min(journal.now() + timedelta(minutes=10), cutoff, hosted_oda.response_time(offer["start_at"]))
        delivery.update(baseline=baseline, browser_context=context, physical_address=address, review_expires_at=expiry.isoformat(),
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
    addition = "addition" in record["delivery"]
    if action == "observe_vipps_addition" and addition and status in {"delivery_read", "checkout_preflight"}:
        return checkout.observe_addition(record, value, key)
    if action == "observe_delivery" and not addition and status in {"delivery_read", "delivery_preflight", "delivery_readback", "checkout_preflight"}:
        return observe(record, value, key)
    if action == "request_vipps_phone" and status == "checkout_dispatched":
        return checkout.issue_vipps_request(record, value)
    if action == "observe_vipps_phone_request" and status in {"checkout_dispatched", "checkout_readback"}:
        checkout.observe_vipps_request(record, value)
        return False
    if action == "recheck_checkout" and status in checkout.PENDING:
        checkout.start_readback(record, value)
        return False
    if action == "defer_checkout" and status in checkout.PENDING:
        attempt = record["delivery"]["checkout"]
        if now() < checkout.retire_after(attempt, owner_handoff=record["delivery"].get("owner_handoff", False)):
            raise ValueError("wait for the original Oda checkout permit to expire before deferring maintenance")
        checkout.defer_recovery(record)
        return False
    if action == "recheck_checkout_unsubmitted" and status in {"checkout_dispatched", "checkout_owner_pending", "checkout_unsubmitted_read"}:
        checkout.start_unsubmitted_read(record, value)
        return False
    if action == "recheck_checkout_archive" and status in checkout.PENDING:
        checkout.start_unknown_archive(record, value)
        return False
    if (action, status) in {("observe_checkout_unsubmitted", "checkout_unsubmitted_read"),
                            ("observe_checkout_archive", "checkout_archive_read")}:
        checkout.observe_current_checkout(record, value, key)
        return False
    if action == "observe_checkout_order" and status == "checkout_readback" and "frame" in record:
        checkout.observe_order(record, value, key)
        return False
    if action == "approve_delivery" and status == "delivery_review" and value["review_digest"] == record["delivery"]["review_digest"]:
        record["status"] = "delivery_preflight"
    elif action in checkout.NATIVE_APPROVE and status == "checkout_review":
        delivery = record["delivery"]
        if (checkout.NATIVE_APPROVE[action] != checkout.native_method(delivery)
                or value["review_digest"] != delivery["review_digest"] or type(value["approved_total_ore"]) is not int
                or value["approved_total_ore"] != delivery["checkout"]["review"]["payable_ore"]):
            raise ValueError("obtain owner approval for the exact Oda payment review and total")
        record["status"] = "checkout_preflight"
    elif action == "acknowledge_delivery" and status == "delivery_dispatched":
        acknowledge(record, value)
        return False
    elif action == "recheck" and status in RESERVATION_PENDING:
        if status == "delivery_dispatched" and now() < hosted_oda.response_time(record["frame"]["expires_at"]):
            raise ValueError("wait for the original Oda dispatch frame to expire before reconciling")
        record["delivery"].pop("owner_accepted_unknown_at", None)
        record["status"] = "delivery_readback"
    elif action == "accept_delivery_current" and status in RESERVATION_PENDING:
        delivery = record["delivery"]
        if (value["review_digest"] != delivery["review_digest"] or value["owner_accepted_unknown"] is not True
                or value["original_executor_stopped"] is not True or now() < hosted_oda.response_time(delivery["dispatch_expires_at"])):
            raise ValueError("retire the original expired Oda dispatch and obtain owner approval to archive its unknown outcome")
        delivery["owner_accepted_unknown_at"] = now().isoformat()
        record["status"] = "delivery_readback"
    elif action == "retry_read" and status in {"delivery_read", "delivery_preflight", "checkout_preflight"}:
        pass
    elif action == "abort" and status in BEFORE_WRITE:
        record["status"] = "aborted"
        record.pop("frame", None)
        return False
    else:
        raise ValueError("use the exact Oda delivery review or reconcile the original reservation")
    record["frame"] = frame(record)
    return False
