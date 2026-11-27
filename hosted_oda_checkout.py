"""One approved Oda saved-card submission in the Dot's native browser."""
from datetime import timedelta
import hmac
import re
import uuid
from zoneinfo import ZoneInfo

from checkout_identity import checkout_lines_match
from core import DELIVERY_SLOT_KEYS, money_cents
import hosted_oda
from oda_browser import CHECKOUT_URL, OdaBrowser, _oda_checkout_amounts_minor
from oda_checkout_observation import checkout_amounts, native_amount_minor, receipt_delivery_matches, text
from service_common import safe_order_id


BEFORE_WRITE = {"checkout_review", "checkout_preflight"}
PENDING = {"checkout_dispatched", "checkout_readback", "checkout_owner_pending", "checkout_unsubmitted_read", "checkout_archive_read"}
FINISHED = {"checkout_confirmed", "checkout_order_inactive", "checkout_not_submitted", "checkout_archived_unknown",
            "checkout_order_observed_changed", "checkout_order_resolved_by_merchant"}
CHECKOUT_RECOVERY_JOURNAL_VERSION = 2
RECOVERY_ACTIONS = {"defer_checkout", "recheck_checkout", "observe_checkout_order", "recheck_checkout_unsubmitted",
                    "observe_checkout_unsubmitted", "recheck_checkout_archive", "observe_checkout_archive"}
ORDERS_ARGUMENTS = {"page": 1, "size": 20}
ACTIVE_ORDERS = {"paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered"}
INACTIVE_ORDERS = {"cancelled", "failed"}


def review_request(record):
    return {"action": "review_saved_card_checkout", "url": CHECKOUT_URL, "operation_id": record["operation_id"],
            "start_checkout_report": record["status"] == "checkout_preflight"}


def delivery_matches(window, display, observed_at):
    slot = {field: window[field] for field in DELIVERY_SLOT_KEYS - {"selected"}}
    return receipt_delivery_matches({**slot, "selected": True}, display, hosted_oda.response_time(observed_at))


def order_slot_matches(window, display, observed):
    if not isinstance(display, str):
        return False
    if delivery_matches(window, display, observed.isoformat()):
        return True
    hours = re.fullmatch(r"(?:kl\.?\s*)?(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?", display.strip(), re.IGNORECASE)
    if not hours:
        return False
    start, end = (hosted_oda.response_time(window[key]).astimezone(ZoneInfo("Europe/Oslo")) for key in ("start_at", "end_at"))
    return (int(hours[1]), int(hours[2] or 0), int(hours[3]), int(hours[4] or 0)) == (start.hour, start.minute, end.hour, end.minute)


def verified_provider_result(value):
    if not isinstance(value, dict) or "error" in value or value.get("isError", False) is not False:
        raise ValueError("the original Oda provider observation returned an error")
    return value


def record_native_report(checkout, report_id):
    if report_id is None:
        return
    if (not isinstance(report_id, str) or not 1 <= len(report_id) <= 200 or not report_id.isprintable()
            or checkout.get("native_report_id", report_id) != report_id):
        raise ValueError("the accompanying native checkout report ID is invalid or changed")
    checkout["native_report_id"] = report_id


def products(cart):
    result = []
    for group in cart["groups"]:
        for item in group["items"]:
            product = item["product"]
            result.append({"product_id": str(product["id"]), "name": product["name"],
                           "description": product.get("description") or "", "brand": product.get("brand") or "",
                           "quantity": int(item["quantity"])})
    return result


def recent_orders(value, after, before):
    checked = hosted_oda.response_time(value["orders_observed_at"])
    if not after <= checked <= before:
        raise ValueError("the original Oda order-list observation is outside the checkout review")
    orders = verified_provider_result(value["existing_orders"])
    hosted_oda.orders_view("get_orders", orders, ORDERS_ARGUMENTS)
    return [order["orderNumber"] for order in orders["orders"]]


def protect_owner_handoff(record, value, baseline, reservation, context, address, slot_time):
    from hosted_oda_cart import now

    observed = now()
    delivery = record["delivery"]
    window = reservation["window"]
    cutoff = hosted_oda.response_time(window["cutoff_at"])
    hold = hosted_oda.response_time(reservation["expires_at"]) if reservation["expires_at"] else cutoff
    expires = min(observed + timedelta(minutes=2), cutoff, hold, hosted_oda.response_time(window["start_at"]))
    if expires <= observed:
        raise ValueError("the Oda checkout window expired before owner handoff")
    delivery.update(browser_context=context, physical_address=address, reservation=reservation,
                    checkout={"review": {"delivery": reservation["window"], "products": products(value["cart"]), "cart": baseline,
                                         "payment_method": delivery["payment_method"], "payable_ore": baseline["amounts_ore"]["total"]},
                              "browser_id": value["browser"]["browser_id"], "tab_id": value["browser"]["tab_id"],
                              "attempt_id": record["operation_id"], "dispatch_issued_at": observed.isoformat(),
                              "dispatch_expires_at": expires.isoformat(),
                              "existing_orders": recent_orders(value, slot_time, observed)})
    record["status"] = "checkout_owner_pending"
    record.pop("frame")


def observe_review(record, value, baseline, reservation, context, address, slot_time):
    from hosted_oda_cart import now, require_fields

    delivery = record["delivery"]
    ready = record["status"] == "checkout_preflight"
    observed = value["checkout"]
    fields = ("url", "browser_id", "tab_id", "new_order", "exclusive_checkout", "address_label", "delivery", "rows", "amount_rows",
              "payment", "submit_control", "warnings", "observed_at")
    require_fields(observed, fields)
    timestamp = hosted_oda.response_time(observed["observed_at"])
    checked = now()
    if not (slot_time <= timestamp <= checked < hosted_oda.response_time(record["frame"]["expires_at"])):
        raise ValueError("the Oda browser checkout observation expired")
    window = reservation["window"]
    if (observed["url"] != CHECKOUT_URL or observed["new_order"] is not True or observed["exclusive_checkout"] is not True
            or any(observed[field] != value["browser"][field] for field in ("browser_id", "tab_id"))
            or text(observed["address_label"]) != text(value["address_label"])
            or not delivery_matches(window, observed["delivery"], observed["observed_at"])):
        raise ValueError("the Oda checkout must match the dedicated browser, address and window")
    if observed["warnings"] != []:
        raise ValueError("resolve any visible checkout warnings before submitting to Oda")

    expected = products(value["cart"])
    rows = observed["rows"]
    if (not 1 <= len(expected) <= 32 or not isinstance(rows, list)
            or any(not isinstance(row, dict) or row.keys() != {"product_id", "title", "subtitle", "quantity"}
                   for row in rows)
            or not checkout_lines_match(expected, rows)):
        raise ValueError("the Oda browser checkout product identity or quantity changed")
    payment = observed["payment"]
    require_fields(payment, ("method", "selected", "masked_card"))
    masked_card = text(payment["masked_card"])
    total = baseline["amounts_ore"]["total"]
    if payment["method"] != "saved_card" or payment["selected"] is not True or re.fullmatch(r"•••• [0-9]{4}", masked_card) is None:
        raise ValueError("the selected Oda merchant-saved card is unavailable")
    control = observed["submit_control"]
    require_fields(control, ("label", "enabled", "count"))
    label = text(control["label"])
    payable = re.fullmatch(r"(?:Bekreft og betal|Confirm and pay) (.+)", label, re.IGNORECASE)
    if (control["enabled"] is not True or type(control["count"]) is not int or control["count"] != 1
            or payable is None or native_amount_minor("Total inkl. MVA", payable[1]) != total or total <= 0):
        raise ValueError("the unique enabled Oda payment control does not show the exact payable amount")
    amounts = checkout_amounts(observed["amount_rows"], value["cart"]["productQuantityCount"], total)
    minor = _oda_checkout_amounts_minor(amounts)
    app = baseline["amounts_ore"]
    checkout_components = {"subtotal": minor["product_subtotal"], "deliveryFee": minor["delivery_price"] or 0,
                           "packaging": minor["bags"] or 0, "bottleDeposit": minor["deposits"] or 0,
                           "surcharge": sum((minor["other_fees"] or {}).values())}
    if (any(app[field] != amount for field, amount in checkout_components.items())
            or (minor["discounts"] or 0) != app["productDiscounts"] + app["couponDiscount"]):
        raise ValueError("the Oda checkout goods, discounts or fees differ from the attached app cart")
    snapshot = {"url": CHECKOUT_URL, "browser": context, "address": address, "delivery": window,
                "cart": baseline, "products": expected, "checkout_rows": rows, "amounts": amounts,
                "payable_ore": total, "payment_method": "saved_card", "masked_card": masked_card, "control": label}
    digest = hosted_oda.arguments_digest({"operation_id": record["operation_id"], "account": record["account"], "review": snapshot})
    slot_cutoff = hosted_oda.response_time(window["cutoff_at"])
    hold_end = hosted_oda.response_time(reservation["expires_at"]) if reservation["expires_at"] else slot_cutoff
    if not ready:
        expires = min(checked + timedelta(minutes=10), slot_cutoff, hold_end)
        delivery.update(browser_context=context, physical_address=address, reservation=reservation,
                        review_expires_at=expires.isoformat(), review_digest=digest,
                        checkout={"review": snapshot, "browser_id": observed["browser_id"], "tab_id": observed["tab_id"]})
        record["status"] = "checkout_review"
        record.pop("frame")
        return False

    if digest != delivery["review_digest"]:
        raise ValueError("the approved Oda checkout changed; prepare a new purchase review")
    existing_orders = recent_orders(value, slot_time, timestamp)
    address_time = hosted_oda.response_time(value["address_observed_at"])
    expires = min(timestamp + timedelta(seconds=45), address_time + timedelta(minutes=2), slot_cutoff, hold_end,
                  hosted_oda.response_time(delivery["review_expires_at"]))
    if expires <= checked:
        raise ValueError("the exact Oda checkout authorization expired before submission")
    checkout = delivery["checkout"]
    checkout.update(attempt_id=record["operation_id"], dispatch_issued_at=checked.isoformat(), dispatch_expires_at=expires.isoformat(),
                    existing_orders=existing_orders)
    record["status"] = "checkout_dispatched"
    record["frame"] = {"frame_id": uuid.uuid4().hex, "plugin_id": record["plugin_id"],
                       "issued_at": checked.isoformat(), "expires_at": expires.isoformat(),
                       "native_browser": {"action": "submit_saved_card_checkout", "url": CHECKOUT_URL,
                                          "browser_id": checkout["browser_id"], "tab_id": checkout["tab_id"],
                                          "attempt_id": checkout["attempt_id"], "control": label, "payable_ore": total,
                                          "review_digest": digest}}
    return True


def start_readback(record, value):
    from hosted_oda_cart import now, require_fields

    checkout = record["delivery"]["checkout"]
    issued = now()
    if "matched_order_id" not in checkout:
        require_fields(value, ("action", "confirmation"))
        confirmation = value["confirmation"]
        fields = ("browser_id", "tab_id", "attempt_id", "url", "order_number", "order_link", "observed_at", "native_report_id")
        owner_source = isinstance(confirmation, dict) and "owner_confirmed_original" in confirmation
        require_fields(confirmation, fields + ("owner_confirmed_original", "retirement") if owner_source else fields)
        order_id = safe_order_id(confirmation["order_number"])
        observed = hosted_oda.response_time(confirmation["observed_at"])
        success_url = "https://oda.com/no/checkout/success/"
        retry_url = f"https://oda.com/no/checkout/retry/?orderNumber={order_id}"
        receipt_url = OdaBrowser._order_url(order_id)
        if (not hosted_oda.response_time(checkout["dispatch_issued_at"]) <= observed <= issued
                or any(confirmation[field] != checkout[field] for field in ("browser_id", "attempt_id"))
                or order_id in checkout["existing_orders"]
                or confirmation["order_link"] not in (None, receipt_url)
                or (confirmation["url"] not in (f"{success_url}?orderNumber={order_id}", retry_url)
                    and (confirmation["url"] != success_url or confirmation["order_link"] != receipt_url))):
            raise ValueError("bind the original Oda confirmation link or number to its checkout attempt")
        if owner_source:
            tab = confirmation["tab_id"]
            if (record["delivery"].get("owner_handoff") is not True or confirmation["owner_confirmed_original"] is not True
                    or confirmation["url"] != f"{success_url}?orderNumber={order_id}"
                    or not isinstance(tab, str) or not 1 <= len(tab) <= 150 or not tab.isprintable()
                    or tab == checkout["tab_id"]):
                raise ValueError("use the owner's exact original Oda success link to bind this handed-off checkout")
            verify_retirement(checkout, confirmation["retirement"])
        elif confirmation["tab_id"] != checkout["tab_id"]:
            raise ValueError("bind the original Oda confirmation to the tab that made the attempt")
        record_native_report(checkout, confirmation["native_report_id"])
        checkout["matched_order_id"] = order_id
        checkout["confirmation_observed_at"] = observed.isoformat()
        if owner_source:
            checkout["confirmation_source"] = "owner_identified_original_success_link"
        elif confirmation["url"] == retry_url:
            checkout["confirmation_source"] = "merchant_retry_page"
        else:
            checkout["confirmation_source"] = "original_checkout_confirmation"
    else:
        require_fields(value, ("action", "native_report_id") if "native_report_id" in value else ("action",))
        record_native_report(checkout, value.get("native_report_id"))
        order_id = checkout["matched_order_id"]
    record["status"] = "checkout_readback"
    record["frame"] = {"frame_id": uuid.uuid4().hex, "plugin_id": record["plugin_id"], "issued_at": issued.isoformat(),
                       "expires_at": (issued + timedelta(minutes=5)).isoformat(), "order_id": order_id,
                       "tools": [{"tool": "get_delivery_addresses", "arguments": {}},
                                 *[{"tool": name, "arguments": {"order_number": order_id}}
                                   for name in ("get_order", "order_tracking")]]}


def retire_after(checkout, *, owner_handoff):
    expiry = hosted_oda.response_time(checkout["dispatch_expires_at"])
    if owner_handoff:
        return max(expiry, hosted_oda.response_time(checkout["dispatch_issued_at"]) + timedelta(minutes=2))
    return expiry


def defer_recovery(record):
    # Older installers understand the cart flag but cannot preserve this checkout.
    record.pop("maintenance_deferred", None)
    record["checkout_recovery_deferred"] = True


def verify_retirement(checkout, evidence):
    from hosted_oda_cart import require_fields

    require_fields(evidence, ("browser_id", "tab_id", "original_tab_retired", "owner_handoff_ended", "no_pending_bank_action"))
    if (any(evidence[field] != checkout[field] for field in ("browser_id", "tab_id"))
            or any(evidence[field] is not True for field in ("original_tab_retired", "owner_handoff_ended", "no_pending_bank_action"))):
        raise ValueError("verify the original checkout tab, any owner takeover and any bank action are retired")


def current_checkout_read(record, issued, status):
    record["status"] = status
    record["frame"] = {"frame_id": uuid.uuid4().hex, "plugin_id": record["plugin_id"], "issued_at": issued.isoformat(),
                       "expires_at": (issued + timedelta(minutes=5)).isoformat(),
                       "retirement_version": CHECKOUT_RECOVERY_JOURNAL_VERSION,
                       "tools": [{"tool": "get_delivery_addresses", "arguments": {}}, {"tool": "get_cart", "arguments": {}},
                                 {"tool": "get_orders", "arguments": ORDERS_ARGUMENTS}]}


def start_unsubmitted_read(record, value):
    from hosted_oda_cart import now, require_fields

    fields = ("action", "attempt_id", "reason", "original_executor_retired", "retirement", "owner_confirmed_no_submit")
    require_fields(value, fields + ("native_report_id",) if "native_report_id" in value else fields)
    delivery = record["delivery"]
    checkout = delivery["checkout"]
    issued = now()
    reason = value["reason"]
    reasons = ("host_blocked_before_click", "checkout_click_never_called") if delivery.get("native_saved_card") else (
        "owner_returned_without_submitting", "owner_handoff_never_opened")
    if (value["attempt_id"] != checkout["attempt_id"] or reason not in reasons or value["original_executor_retired"] is not True
            or issued < retire_after(checkout, owner_handoff=delivery.get("owner_handoff", False))
            or (reason == "owner_returned_without_submitting" and value["owner_confirmed_no_submit"] is not True)):
        raise ValueError("require a known pre-submit ending and retire the original Oda executor before reconciling")
    verify_retirement(checkout, value["retirement"])
    record_native_report(checkout, value.get("native_report_id"))
    checkout["unsubmitted_reason"] = reason
    current_checkout_read(record, issued, "checkout_unsubmitted_read")


def start_unknown_archive(record, value):
    from hosted_oda_cart import now, require_fields

    fields = ("action", "attempt_id", "owner_accepted_unknown", "reviewed_total_ore", "retirement")
    require_fields(value, fields + ("native_report_id",) if "native_report_id" in value else fields)
    delivery = record["delivery"]
    checkout = delivery["checkout"]
    issued = now()
    if (value["attempt_id"] != checkout["attempt_id"] or value["owner_accepted_unknown"] is not True
            or type(value["reviewed_total_ore"]) is not int or value["reviewed_total_ore"] != checkout["review"]["payable_ore"]
            or issued < hosted_oda.response_time(checkout["review"]["delivery"]["end_at"])
            or issued < retire_after(checkout, owner_handoff=delivery.get("owner_handoff", False))):
        raise ValueError("obtain explicit acceptance of this exact unknown Oda purchase after its original delivery window ends")
    verify_retirement(checkout, value["retirement"])
    record_native_report(checkout, value.get("native_report_id"))
    checkout["owner_accepted_unknown_at"] = issued.isoformat()
    current_checkout_read(record, issued, "checkout_archive_read")
    if "matched_order_id" in checkout:
        order_id = checkout["matched_order_id"]
        record["frame"]["order_id"] = order_id
        record["frame"]["tools"].extend({"tool": name, "arguments": {"order_number": order_id}}
                                          for name in ("get_order", "order_tracking"))


def original_account(record, addresses, key):
    from hosted_oda_cart import account_fingerprint, address_selection

    address_id = record["delivery"]["address_id"]
    roster = address_selection(addresses)["ids"]
    selection = [{"id": identity, "isSelected": identity == address_id} for identity in roster]
    if address_id not in roster or not hmac.compare_digest(account_fingerprint(selection, key), record["account"]):
        raise ValueError("verify the original Oda account and its delivery addresses before reading the receipt")


def retain_last_labeled_payment(checkout):
    if "last_labeled_payment" not in checkout:
        prior = checkout.get("result", {})
        prior = prior.get("prior_labeled_payment", prior)
        if (prior.get("payment_observation") in {"authorized", "paid", "negative"}
                or prior.get("payment_evidence_conflicting") is True):
            fields = ("payment_observation", "merchant_tracking_status", "payment_evidence_conflicting", "observed_at")
            checkout["last_labeled_payment"] = {field: prior[field] for field in fields}
    return checkout.get("last_labeled_payment")


def observe_current_checkout(record, value, key):
    from hosted_oda_cart import bound_times, require_fields, now
    from hosted_oda_delivery import inspect_cart

    pending = record["frame"]
    if pending.get("retirement_version") != CHECKOUT_RECOVERY_JOURNAL_VERSION:
        raise ValueError("reissue the original Oda checkout recovery with the current retirement evidence")
    archive = record["status"] == "checkout_archive_read"
    order_id = pending.get("order_id") if archive else None
    fields = ("action", "frame_id", "plugin_id", "addresses", "address_observed_at", "cart_observed_at", "cart", "existing_orders", "orders_observed_at")
    require_fields(value, fields + ("order", "tracking", "order_observed_at") if order_id else fields)
    _, cart_time = bound_times(record, pending, value, key, "cart_observed_at", check_account=not archive)
    if archive:
        original_account(record, value["addresses"], key)
    view, current, _ = inspect_cart(value["cart"], allow_grouped=archive)
    checkout = record["delivery"]["checkout"]
    orders = recent_orders(value, cart_time, now())
    if hosted_oda.response_time(value["orders_observed_at"]) >= hosted_oda.response_time(pending["expires_at"]):
        raise ValueError("the original Oda order-list observation expired")
    if archive:
        date = record["delivery"]["date"]
        prior_payment = retain_last_labeled_payment(checkout) or {}
        latest_payment = checkout.get("result", {})
        payment_outcome = {"authorized": "previously_reported_authorized", "paid": "previously_reported_paid",
                           "negative": "previously_reported_negative_or_pending"}.get(prior_payment.get("payment_observation"), "unknown")
        if (prior_payment.get("payment_evidence_conflicting")
                or latest_payment.get("payment_conflict_unresolved")):
            payment_outcome = "previously_conflicting_observations"
        elif latest_payment.get("payment_label_unresolved") or latest_payment.get("payment_observation") == "unrecognized":
            payment_outcome = "previously_unresolved_payment_label"
        exact_tracking = None
        detail = None
        if order_id:
            order_time = hosted_oda.response_time(value["order_observed_at"])
            if not hosted_oda.response_time(value["orders_observed_at"]) <= order_time <= now() < hosted_oda.response_time(pending["expires_at"]):
                raise ValueError("the exact linked Oda order was not read in the archive frame")
            arguments = {"order_number": order_id}
            if value["order"] is not None:
                raw_order = verified_provider_result(value["order"])
                hosted_oda.orders_view("get_order", raw_order, arguments)
                order = hosted_oda.order_row(raw_order, include_products=True)
                rows = order["products"]
                detail = {"amount_ore": money_cents(order["grossAmount"]), "currency": order["currency"],
                          "delivery_date": order["deliveryDate"], "item_count": len(rows), "items_truncated": len(rows) > 32,
                          "items": [{"product_id": item["product"]["id"], "quantity": item["quantity"]} for item in rows[:32]]}
            if value["tracking"] is not None:
                exact_tracking = hosted_oda.orders_view("order_tracking", verified_provider_result(value["tracking"]), arguments)["status"]
        checkout["archive_observation"] = {"cart_count": view["count"], "order_list_truncated": value["existing_orders"]["hasMore"],
                                           "new_order_refs_on_original_date": [row["orderNumber"] for row in value["existing_orders"]["orders"]
                                               if row["deliveryDate"] == date and row["orderNumber"] != order_id
                                               and row["orderNumber"] not in checkout["existing_orders"]],
                                           "observed_at": value["orders_observed_at"], "payment_outcome": payment_outcome}
        record["status"] = "checkout_archived_unknown"
        if order_id:
            checkout["archive_observation"].update(linked_order_ref=order_id, linked_merchant_tracking_status=exact_tracking,
                                                   linked_order_detail=detail)
            if detail is not None and exact_tracking in ACTIVE_ORDERS | INACTIVE_ORDERS:
                record["status"] = "checkout_order_resolved_by_merchant"
    elif (current["goods_sha256"] != checkout["review"]["cart"]["goods_sha256"]
            or set(orders) != set(checkout["existing_orders"])):
        raise ValueError("the original Oda cart or order history changed before a non-submission could be established")
    else:
        record["status"] = "checkout_not_submitted"
    record.pop("frame")


def payment_observation(label):
    if label is None:
        return "not_visible"
    label = text(label).casefold()
    if re.fullmatch(r"(?:betaling autorisert|payment authorized|betaling reservert|payment reserved)", label):
        return "authorized"
    if re.fullmatch(r"(?:betalt(?: med kort)?|paid(?: by card| with card)?|betaling gjennomført|payment complete)", label):
        return "paid"
    if re.fullmatch(r"(?:betaling(?:en)? (?:mislyktes|feilet|(?:er )?avvist|venter)|kortet ble avvist|venter på betaling|"
                    r"ikke betalt|ubetalt|(?:card|payment) declined|payment (?:failed|pending)|unpaid)", label):
        return "negative"
    return "unrecognized"


def observe_order(record, value, key):
    from hosted_oda_cart import bound_times, now, require_fields
    from hosted_oda_delivery import physical_address_fingerprint

    require_fields(value, ("action", "frame_id", "plugin_id", "addresses", "address_observed_at",
                           "observed_at", "browser", "order", "tracking"))
    pending = record["frame"]
    _, observed = bound_times(record, pending, value, key, "observed_at", check_account=False)
    order_id = pending["order_id"]
    delivery = record["delivery"]
    original_account(record, value["addresses"], key)
    checkout = delivery["checkout"]
    expected = checkout["review"]
    browser = value["browser"]
    require_fields(browser, ("url", "browser_id", "tab_id", "attempt_id", "order_number", "delivery", "payment_label",
                             "address_id", "address_label", "total_ore", "original_tab_retired"))
    tab = browser["tab_id"]
    retired = browser["original_tab_retired"]
    if (browser["url"] != OdaBrowser._order_url(order_id) or browser["order_number"] != order_id
            or any(browser[field] != checkout[field] for field in ("browser_id", "attempt_id"))
            or not isinstance(tab, str) or not 1 <= len(tab) <= 150 or not tab.isprintable()
            or type(retired) is not bool or retired != (tab != checkout["tab_id"])
            or type(browser["address_id"]) is not int or browser["address_id"] != delivery["address_id"]
            or not hmac.compare_digest(physical_address_fingerprint(browser["address_label"], key), delivery["physical_address"])
            or type(browser["total_ore"]) is not int
            or not delivery_matches(expected["delivery"], browser["delivery"], observed.isoformat())):
        raise ValueError("verify the exact original Oda shop confirmation and order on the bound native tab")
    arguments = {"order_number": order_id}
    raw_order = verified_provider_result(value["order"])
    hosted_oda.orders_view("get_order", raw_order, arguments)
    order = hosted_oda.order_row(raw_order, include_products=True)
    tracking = hosted_oda.orders_view("order_tracking", verified_provider_result(value["tracking"]), arguments)["status"]
    quantities = {str(item["product"]["id"]): item["quantity"] for item in order["products"]}
    total = money_cents(order["grossAmount"])
    if (len(quantities) != len(order["products"])
            or total != browser["total_ore"] or order["deliveryDate"] != delivery["date"]
            or (raw_order.get("deliverySlotDisplay") is not None
                and not order_slot_matches(expected["delivery"], raw_order["deliverySlotDisplay"], observed))):
        raise ValueError("the two Oda receipts do not establish the original order total and delivery")
    reviewed = {line["product_id"]: line["quantity"] for line in expected["products"]}
    items_match = quantities == reviewed
    observation = payment_observation(browser["payment_label"])
    paid = observation in {"authorized", "paid"}
    conflicting = (tracking in ACTIVE_ORDERS and observation == "negative"
                   or tracking in {"unpaid_order", "unpaid_order_change"} and paid)
    if observation in {"authorized", "paid", "negative"}:
        checkout["last_labeled_payment"] = {"payment_observation": observation, "merchant_tracking_status": tracking,
                                           "payment_evidence_conflicting": conflicting, "observed_at": observed.isoformat()}
        if not conflicting:
            checkout.pop("unclear_payment_label_at", None)
    elif observation == "unrecognized":
        checkout["unclear_payment_label_at"] = observed.isoformat()
    elif checkout.get("result", {}).get("payment_observation") == "unrecognized":
        checkout.setdefault("unclear_payment_label_at", checkout["result"]["observed_at"])
    prior_payment = retain_last_labeled_payment(checkout)
    exact_review = items_match and total == expected["payable_ore"]
    if delivery.get("owner_handoff"):
        amounts = {"app_cart_amount_ore": expected["payable_ore"], "matches_handoff_cart": exact_review}
    else:
        amounts = {"approved_amount_ore": expected["payable_ore"], "approved_items_match": items_match,
                   "approved_purchase_matches_order": exact_review}
    checkout["result"] = {"order_id": order_id, "amount_ore": total, "currency": "NOK", **amounts,
                          "original_reference_source": checkout.get("confirmation_source", "original_checkout_confirmation"),
                          "merchant_tracking_status": tracking, "merchant_reports_payment_authorized": observation == "authorized",
                          "payment_observation": observation, "payment_evidence_conflicting": conflicting,
                          "payment_label_unresolved": "unclear_payment_label_at" in checkout,
                          "observed_at": observed.isoformat()}
    if observation in {"not_visible", "unrecognized"} and prior_payment is not None:
        checkout["result"]["prior_labeled_payment"] = prior_payment
        label = prior_payment["payment_observation"]
        checkout["result"]["payment_conflict_unresolved"] = (
            prior_payment.get("payment_evidence_conflicting") is True
            or (label == "negative" and tracking in ACTIVE_ORDERS)
            or (label in {"authorized", "paid"} and tracking in {"unpaid_order", "unpaid_order_change"}))
    if not items_match:
        changes = sorted(product_id for product_id in reviewed.keys() | quantities.keys()
                         if reviewed.get(product_id) != quantities.get(product_id))
        checkout["result"]["quantity_changes"] = {
            "count": len(changes), "items": [{"product_id": product_id, "reviewed_quantity": reviewed.get(product_id, 0),
                                             "observed_quantity": quantities.get(product_id, 0)} for product_id in changes[:32]],
            "truncated": len(changes) > 32}
    release = now() >= retire_after(checkout, owner_handoff=delivery.get("owner_handoff", False))
    unresolved_payment = (conflicting or checkout["result"]["payment_label_unresolved"]
                          or checkout["result"].get("payment_conflict_unresolved") is True)
    if release and tracking in ACTIVE_ORDERS and not unresolved_payment:
        record["status"] = "checkout_confirmed" if exact_review else "checkout_order_observed_changed"
    elif release and tracking in INACTIVE_ORDERS:
        record["status"] = "checkout_order_inactive"
    record.pop("frame")
