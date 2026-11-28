"""Guarded Oda submissions in the Dot's native browser."""
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
CHECKOUT_RECOVERY_JOURNAL_VERSION = 5
RETIREMENT_JOURNAL_VERSION = 3
READABLE_JOURNAL_VERSIONS = (2, RETIREMENT_JOURNAL_VERSION, 4, CHECKOUT_RECOVERY_JOURNAL_VERSION)
NATIVE_START = {"begin_saved_card_checkout": "saved_card", "begin_vipps_checkout": "vipps"}
NATIVE_APPROVE = {"approve_saved_card_checkout": "saved_card", "approve_vipps_checkout": "vipps"}
RECOVERY_ACTIONS = {"defer_checkout", "recheck_checkout", "observe_checkout_order", "recheck_checkout_unsubmitted",
                    "observe_checkout_unsubmitted", "recheck_checkout_archive", "observe_checkout_archive", "observe_vipps_phone_request"}
ORDERS_ARGUMENTS = {"page": 1, "size": 20}
ACTIVE_ORDERS = {"paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered"}
INACTIVE_ORDERS = {"cancelled", "failed"}
PAID_METHOD_LABELS = {
    "betalt med kort": "card", "paid by card": "card", "paid with card": "card",
    "betalt med vipps": "vipps", "paid by vipps": "vipps", "paid with vipps": "vipps",
}


def native_method(delivery):
    if delivery.get("native_vipps"):
        return "vipps"
    return "saved_card" if delivery.get("native_saved_card") else None


def review_request(record):
    delivery = record["delivery"]
    method = native_method(delivery)
    addition = delivery.get("addition")
    url = addition_url(addition["order_id"]) if addition else CHECKOUT_URL
    return {"action": "review_vipps_addition" if addition else f"review_{method}_checkout", "url": url,
            "operation_id": record["operation_id"],
            "start_checkout_report": record["status"] == "checkout_preflight"}


def addition_url(order_id):
    return f"{CHECKOUT_URL}?orderNumber={order_id}"


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


def reviewed_payment(payment, control, method, total):
    from hosted_oda_cart import require_fields

    require_fields(payment, ("method", "selected", "masked_card") if method == "saved_card" else ("method", "selected"))
    if payment["method"] != method or payment["selected"] is not True:
        raise ValueError("verify the approved selected Oda payment method")
    result = {"payment_method": method}
    if method == "saved_card":
        masked_card = text(payment["masked_card"])
        if re.fullmatch(r"•••• [0-9]{4}", masked_card) is None:
            raise ValueError("the selected Oda merchant-saved card is unavailable")
        result["masked_card"] = masked_card
    require_fields(control, ("label", "enabled", "count"))
    label = text(control["label"])
    pattern = r"(?:Bekreft og betal|Confirm and pay) (.+)" if method == "saved_card" else r"Betal med Vipps(?: (.+))?"
    payable = re.fullmatch(pattern, label, re.IGNORECASE)
    if (control["enabled"] is not True or type(control["count"]) is not int or control["count"] != 1
            or payable is None or total <= 0
            or (payable[1] is not None and native_amount_minor("Total inkl. MVA", payable[1]) != total)):
        raise ValueError("the unique enabled Oda payment control does not match the exact payable review")
    return {**result, "control": label}


def checkout_rows_match(expected, rows):
    return (1 <= len(expected) <= 32 and isinstance(rows, list)
            and all(isinstance(row, dict) and row.keys() == {"product_id", "title", "subtitle", "quantity"} for row in rows)
            and checkout_lines_match(expected, rows))


def begin_addition(value, original, account, owner, plugin):
    if (not original or original["operation_id"] != value["base_operation_id"]
            or original["status"] != "checkout_confirmed" or original["owner"] != owner
            or original["plugin_id"] != plugin or not hmac.compare_digest(original["account"], account)
            or "addition" in original.get("delivery", {})):
        raise ValueError("start from the confirmed original Oda order in this household")
    delivery = original["delivery"]
    checkout = delivery["checkout"]
    review, result = checkout["review"], checkout["result"]
    if (result["amount_ore"] != review["payable_ore"]
            or not result.get("approved_purchase_matches_order", result.get("matches_handoff_cart"))):
        raise ValueError("the confirmed Oda order does not match the original review")
    return {"account": account, "status": "delivery_read", "delivery": {
        **{field: delivery[field] for field in ("slot_ref", "slot_id", "date", "address_id", "physical_address")},
        "native_vipps": True, "payment_method": "vipps", "maximum_payable_ore": value["maximum_payable_ore"],
        "addition": {"base_operation_id": value["base_operation_id"], "order_id": result["order_id"],
                     "product_id": value["product_id"], "base_total_ore": result["amount_ore"],
                     "base_products": review["products"], "window": review["delivery"]}}}


def addition_amounts(rows, *, base_count, original, added, limit):
    from hosted_oda_cart import require_fields

    labels = {"Opprinnelig bestilling": ("original", base_count), "Nye varer lagt til": ("added", 1),
              "Å betale": ("due", None), "Ny totalsum": ("revised", base_count + 1)}
    if not isinstance(rows, list) or len(rows) != len(labels):
        raise ValueError("verify all four visible Oda addition amounts")
    amounts = {}
    for row in rows:
        require_fields(row, ("label", "value", "count"))
        identity = labels.get(text(row["label"]))
        if (identity is None or identity[0] in amounts or row["count"] != identity[1]
                or (identity[1] is not None and type(row["count"]) is not int)):
            raise ValueError("the Oda addition amount labels or item counts changed")
        amounts[identity[0]] = native_amount_minor("Total inkl. MVA", row["value"])
    if (amounts["original"] != original or amounts["added"] != added or not 0 < added <= limit
            or not 0 < amounts["due"] <= limit or amounts["revised"] != original + amounts["due"]):
        raise ValueError("the Oda addition goods, exact revised total or incremental limit changed")
    return amounts


def observe_addition(record, value, key):
    from hosted_oda_cart import bound_times, cart_quantities, now, require_fields
    from hosted_oda_delivery import browser_context, inspect_cart

    ready = record["status"] == "checkout_preflight"
    fields = ("action", "frame_id", "plugin_id", "addresses", "address_observed_at", "cart", "cart_observed_at",
              "browser", "address_label", "order", "tracking", "order_browser", "order_observed_at", "checkout")
    require_fields(value, fields + (("existing_orders", "orders_observed_at") if ready else ()))
    pending = record["frame"]
    address_time, cart_time = bound_times(record, pending, value, key, "cart_observed_at")
    delivery = record["delivery"]
    target = delivery["addition"]
    window = target["window"]
    order_id = target["order_id"]
    observed = value["checkout"]
    require_fields(observed, ("url", "browser_id", "tab_id", "new_order", "exclusive_checkout", "order_number", "address_label",
                              "delivery", "rows", "amount_rows", "payment", "submit_control", "warnings", "observed_at"))
    order_time = hosted_oda.response_time(value["order_observed_at"])
    stamp = hosted_oda.response_time(observed["observed_at"])
    checked = now()
    cutoff = min(hosted_oda.response_time(window[field]) for field in ("cutoff_at", "start_at"))
    if not cart_time <= order_time <= stamp <= checked < min(cutoff, hosted_oda.response_time(pending["expires_at"])):
        raise ValueError("the original Oda order or addition review expired")
    _, baseline, slot = inspect_cart(value["cart"])
    context, address = browser_context(value, key, delivery["address_id"])
    order_browser = value["order_browser"]
    arguments = {"order_number": order_id}
    raw = verified_provider_result(value["order"])
    tracking = hosted_oda.orders_view("order_tracking", verified_provider_result(value["tracking"]), arguments)
    eligible = hosted_oda.order_change_handoff(tracking, raw, order_browser, value["addresses"])
    if not eligible["handoff_ready"] or "items" not in eligible["available_changes"]:
        raise ValueError("the exact original Oda order is not eligible for item additions")
    order = hosted_oda.order_row(raw, include_products=True)
    base_products = target["base_products"]
    base_quantities = {line["product_id"]: line["quantity"] for line in base_products}
    order_quantities = {str(line["product"]["id"]): line["quantity"] for line in order["products"]}
    if (not hmac.compare_digest(delivery["physical_address"], address)
            or order_browser["browser_id"] != value["browser"]["browser_id"]
            or money_cents(order["grossAmount"]) != target["base_total_ore"] or order["deliveryDate"] != delivery["date"]
            or len(order_quantities) != len(order["products"]) or order_quantities != base_quantities
            or (raw.get("deliverySlotDisplay") is not None and not order_slot_matches(window, raw["deliverySlotDisplay"], order_time))):
        raise ValueError("the original Oda items, price or delivery changed before the addition")
    url = addition_url(order_id)
    if (observed["url"] != url or observed["order_number"] != order_id or observed["new_order"] is not False
            or observed["exclusive_checkout"] is not True or observed["warnings"] != []
            or any(observed[field] != value["browser"][field] for field in ("browser_id", "tab_id"))
            or text(observed["address_label"]) != text(value["address_label"])
            or not delivery_matches(window, observed["delivery"], observed["observed_at"])):
        raise ValueError("verify the exact existing-order addition, address and delivery in the dedicated Oda tab")
    cart = value["cart"]
    added = products(cart)
    item = str(target["product_id"])
    if ((slot is not None and slot["slot_id"] != delivery["slot_id"])
            or cart_quantities({**cart, "deliverySlot": None}) != {item: 1} or not checkout_rows_match(added, observed["rows"])):
        raise ValueError("verify only one approved package in the Oda addition cart")
    package = next(line for group in cart["groups"] for line in group["items"])
    amounts = addition_amounts(observed["amount_rows"], base_count=sum(base_quantities.values()),
                               original=target["base_total_ore"], added=money_cents(hosted_oda.cart_line(package)["totalGrossAmount"]),
                               limit=delivery["maximum_payable_ore"])
    payment = reviewed_payment(observed["payment"], observed["submit_control"], "vipps", amounts["due"])
    combined = {line["product_id"]: dict(line) for line in base_products}
    combined[item] = {**added[0], "quantity": 1 + base_quantities.get(item, 0)}
    snapshot = {"url": url, "browser": context, "address": address, "delivery": window, "cart": baseline,
                "products": list(combined.values()), "checkout_rows": observed["rows"], "amounts": amounts,
                "order_id": order_id, "payable_ore": amounts["due"], "revised_total_ore": amounts["revised"],
                "maximum_payable_ore": delivery["maximum_payable_ore"], **payment}
    digest = hosted_oda.arguments_digest({"operation_id": record["operation_id"], "account": record["account"], "review": snapshot})
    if not ready:
        delivery.update(review_expires_at=min(checked + timedelta(minutes=10), cutoff).isoformat(), review_digest=digest,
                        checkout={"review": snapshot, "browser_id": observed["browser_id"], "tab_id": observed["tab_id"]})
        record["status"] = "checkout_review"
        record.pop("frame")
        return False
    existing_orders = recent_orders(value, order_time, stamp)
    expires = min(stamp + timedelta(seconds=45), address_time + timedelta(minutes=2), cutoff,
                  hosted_oda.response_time(delivery["review_expires_at"]))
    if digest != delivery["review_digest"] or expires <= checked:
        raise ValueError("the approved Oda addition changed or expired before submission")
    issue_native_submit(record, checked, expires, existing_orders)
    return True


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
    if not checkout_rows_match(expected, rows):
        raise ValueError("the Oda browser checkout product identity or quantity changed")
    method = native_method(delivery)
    total = baseline["amounts_ore"]["total"]
    if method == "vipps" and total > delivery["maximum_payable_ore"]:
        raise ValueError("the exact Oda payable exceeds the owner's Vipps limit")
    payment = reviewed_payment(observed["payment"], observed["submit_control"], method, total)
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
                "payable_ore": total, **payment}
    if method == "vipps":
        snapshot["maximum_payable_ore"] = delivery["maximum_payable_ore"]
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
    issue_native_submit(record, checked, expires, existing_orders)
    return True


def issue_native_submit(record, checked, expires, existing_orders):
    delivery = record["delivery"]
    checkout = delivery["checkout"]
    review = checkout["review"]
    checkout.update(attempt_id=record["operation_id"], dispatch_issued_at=checked.isoformat(), dispatch_expires_at=expires.isoformat(),
                    existing_orders=existing_orders)
    action = f"submit_{native_method(delivery)}_checkout"
    if "addition" in delivery:
        checkout.update(matched_order_id=delivery["addition"]["order_id"], confirmation_source="original_existing_order")
        action = "submit_vipps_addition"
    record["status"] = "checkout_dispatched"
    record["frame"] = {"frame_id": uuid.uuid4().hex, "plugin_id": record["plugin_id"],
                       "issued_at": checked.isoformat(), "expires_at": expires.isoformat(),
                       "native_browser": {"action": action, "url": review["url"],
                                          "browser_id": checkout["browser_id"], "tab_id": checkout["tab_id"],
                                          "attempt_id": checkout["attempt_id"], "control": review["control"],
                                          "payable_ore": review["payable_ore"], "review_digest": delivery["review_digest"]}}


def issue_vipps_request(record, value):
    from hosted_oda_cart import match_frame, now, require_fields

    require_fields(value, ("action", "frame_id", "plugin_id", "merchant_clicked_at", "gateway"))
    pending = record["frame"]
    delivery = record["delivery"]
    checkout = delivery["checkout"]
    if (native_method(delivery) != "vipps" or "vipps_request" in checkout or record.get("checkout_recovery_deferred") is True
            or pending.get("native_browser", {}).get("action") not in {"submit_vipps_checkout", "submit_vipps_addition"}):
        raise ValueError("reconcile the original Oda Vipps click instead of issuing another phone request")
    match_frame(pending, value)
    gateway = value["gateway"]
    require_fields(gateway, ("attempt_id", "browser_id", "tab_id", "source_url", "origin", "path", "query_keys", "location_sha256",
                             "canonical_token_url_verified", "observed_at", "direct_transition", "merchant", "currency",
                             "visible_amounts_ore", "amount_ambiguous",
                             "recipient_verified", "request_already_sent", "control"))
    clicked = hosted_oda.response_time(value["merchant_clicked_at"])
    observed = hosted_oda.response_time(gateway["observed_at"])
    checked = now()
    review = checkout["review"]
    deadlines = [observed + timedelta(seconds=45), clicked + timedelta(minutes=2),
                 hosted_oda.response_time(review["delivery"]["cutoff_at"]), hosted_oda.response_time(delivery["review_expires_at"])]
    hold = delivery.get("reservation", {}).get("expires_at")
    if hold:
        deadlines.append(hosted_oda.response_time(hold))
    expires = min(deadlines)
    control = gateway["control"]
    require_fields(control, ("label", "enabled", "count"))
    amounts = gateway["visible_amounts_ore"]
    location = gateway["location_sha256"]
    if (not hosted_oda.response_time(pending["issued_at"]) <= clicked < hosted_oda.response_time(pending["expires_at"])
            or not clicked <= observed <= checked < expires
            or any(gateway[field] != checkout[field] for field in ("attempt_id", "browser_id", "tab_id"))
            or gateway["source_url"] != review["url"] or gateway["direct_transition"] is not True
            or gateway["origin"] != "https://pay.vipps.no" or gateway["path"] != "/" or gateway["query_keys"] != ["token"]
            or gateway["canonical_token_url_verified"] is not True
            or not isinstance(location, str) or re.fullmatch(r"[a-f0-9]{64}", location) is None
            or text(gateway["merchant"]).casefold() != "oda" or gateway["currency"] != "NOK"
            or not isinstance(amounts, list) or len(amounts) > 10 or ("addition" in delivery and not amounts)
            or any(type(amount) is not int or amount != review["payable_ore"] for amount in amounts)
            or gateway["amount_ambiguous"] is not False or gateway["recipient_verified"] is not True
            or gateway["request_already_sent"] is not False or type(control["count"]) is not int or control["count"] != 1
            or control["enabled"] is not True or text(control["label"]) not in {"Next", "Neste"}):
        raise ValueError("the exact Oda Vipps transition, amount, recipient or phone request control could not be verified")
    common = {"frame_id": uuid.uuid4().hex, "plugin_id": record["plugin_id"],
              "issued_at": checked.isoformat(), "expires_at": expires.isoformat()}
    checkout["vipps_request"] = {**common, "merchant_clicked_at": clicked.isoformat(), "location_sha256": location}
    record["frame"] = {**common, "native_browser": {"action": "send_vipps_phone_request", "origin": gateway["origin"],
                       "location_sha256": location, "control": text(control["label"]), "payable_ore": review["payable_ore"],
                       **{field: checkout[field] for field in ("attempt_id", "browser_id", "tab_id")}}}
    return True


def observe_vipps_request(record, value):
    from hosted_oda_cart import match_frame, now, require_fields

    require_fields(value, ("action", "frame_id", "plugin_id", "attempt_id", "browser_id", "tab_id", "location_sha256",
                           "clicked_at", "observed_at", "request_confirmation_visible", "recipient_verified"))
    checkout = record["delivery"]["checkout"]
    pending = checkout.get("vipps_request")
    if pending is None:
        raise ValueError("use the original Vipps phone request dispatch")
    match_frame(pending, value)
    clicked, observed = (hosted_oda.response_time(value[field]) for field in ("clicked_at", "observed_at"))
    retired = "original_tab_retired_at" in checkout
    retirement = checkout.get("original_tab_retired_at")
    if (any(value[field] != checkout[field] for field in ("attempt_id", "browser_id", "tab_id"))
            or value["location_sha256"] != pending["location_sha256"]
            or not hosted_oda.response_time(pending["issued_at"]) <= clicked < hosted_oda.response_time(pending["expires_at"])
            or not clicked <= observed <= min(now(), clicked + timedelta(minutes=2))
            or (retired and (retirement is None or observed >= hosted_oda.response_time(retirement)))
            or value["request_confirmation_visible"] is not True
            or value["recipient_verified"] is not True):
        raise ValueError("the phone request was not observed on the original Oda Vipps dispatch")
    pending.setdefault("observed_at", observed.isoformat())


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
            verify_retirement(checkout, confirmation["retirement"], issued)
        else:
            if confirmation["tab_id"] != checkout["tab_id"]:
                raise ValueError("bind the original Oda confirmation to the tab that made the attempt")
            if "original_tab_retired_at" in checkout:
                retirement = checkout["original_tab_retired_at"]
                if retirement is None and record["delivery"].get("native_saved_card") is True:
                    # Legacy recovery could not record retirement before the native permit expired.
                    retirement = checkout["dispatch_expires_at"]
                if retirement is None or observed >= hosted_oda.response_time(retirement):
                    raise ValueError("the original Oda checkout tab was retired before this confirmation")
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
    if "vipps_request" in checkout:
        return max(expiry, hosted_oda.response_time(checkout["vipps_request"]["expires_at"]))
    return expiry


def defer_recovery(record):
    # Older installers understand the cart flag but cannot preserve this checkout.
    record.pop("maintenance_deferred", None)
    record["checkout_recovery_deferred"] = True


def migrate_retirement(checkout, version):
    # Older journals did not preserve the first retirement time or the receipt's tab.
    if (version is None or version < RETIREMENT_JOURNAL_VERSION) and (
        any(field in checkout for field in ("unsubmitted_reason", "owner_accepted_unknown_at", "result"))
        or checkout.get("confirmation_source") == "owner_identified_original_success_link"
    ):
        checkout.setdefault("original_tab_retired_at", None)


def remember_retirement(checkout, observed):
    checkout.setdefault("original_tab_retired_at", observed.isoformat())


def verify_retirement(checkout, evidence, observed):
    from hosted_oda_cart import require_fields

    require_fields(evidence, ("browser_id", "tab_id", "original_tab_retired", "owner_handoff_ended", "no_pending_bank_action"))
    if (any(evidence[field] != checkout[field] for field in ("browser_id", "tab_id"))
            or any(evidence[field] is not True for field in ("original_tab_retired", "owner_handoff_ended", "no_pending_bank_action"))):
        raise ValueError("verify the original checkout tab, any owner takeover and any bank action are retired")
    remember_retirement(checkout, observed)


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
    reasons = ("host_blocked_before_click", "checkout_click_never_called") if native_method(delivery) else (
        "owner_returned_without_submitting", "owner_handoff_never_opened")
    if (value["attempt_id"] != checkout["attempt_id"] or reason not in reasons or value["original_executor_retired"] is not True
            or "vipps_request" in checkout
            or issued < retire_after(checkout, owner_handoff=delivery.get("owner_handoff", False))
            or (reason == "owner_returned_without_submitting" and value["owner_confirmed_no_submit"] is not True)):
        raise ValueError("require a known pre-submit ending and retire the original Oda executor before reconciling")
    verify_retirement(checkout, value["retirement"], issued)
    record_native_report(checkout, value.get("native_report_id"))
    checkout["unsubmitted_reason"] = reason
    current_checkout_read(record, issued, "checkout_unsubmitted_read")
    if "addition" in delivery:
        add_linked_order_reads(record, checkout["matched_order_id"])


def add_linked_order_reads(record, order_id):
    record["frame"]["order_id"] = order_id
    record["frame"]["tools"].extend({"tool": name, "arguments": {"order_number": order_id}}
                                      for name in ("get_order", "order_tracking"))


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
    verify_retirement(checkout, value["retirement"], issued)
    record_native_report(checkout, value.get("native_report_id"))
    checkout["owner_accepted_unknown_at"] = issued.isoformat()
    current_checkout_read(record, issued, "checkout_archive_read")
    if "matched_order_id" in checkout:
        add_linked_order_reads(record, checkout["matched_order_id"])


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


def payment_conflicts_with_tracking(observation, tracking):
    return (observation == "negative" and tracking in ACTIVE_ORDERS
            or observation in {"authorized", "paid"} and tracking in {"unpaid_order", "unpaid_order_change"})


def recorded_payment_conflict(latest, prior):
    observation = latest.get("payment_observation")
    if observation in {"not_visible", "unrecognized"}:
        observation = prior.get("payment_observation")
    return (latest.get("payment_evidence_conflicting") is True
            or latest.get("payment_conflict_unresolved") is True
            or prior.get("payment_evidence_conflicting") is True
            or payment_conflicts_with_tracking(observation, latest.get("merchant_tracking_status")))


def observe_current_checkout(record, value, key):
    from hosted_oda_cart import bound_times, require_fields, now
    from hosted_oda_delivery import inspect_cart

    pending = record["frame"]
    if pending.get("retirement_version") != CHECKOUT_RECOVERY_JOURNAL_VERSION:
        raise ValueError("reissue the original Oda checkout recovery with the current retirement evidence")
    archive = record["status"] == "checkout_archive_read"
    order_id = pending.get("order_id")
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
        last_label = prior_payment.get("payment_observation")
        payment_outcome = {"authorized": "previously_reported_authorized", "paid": "previously_reported_paid",
                           "negative": "previously_reported_negative_or_pending"}.get(last_label, "unknown")
        if recorded_payment_conflict(latest_payment, prior_payment) or payment_conflicts_with_tracking(last_label, exact_tracking):
            payment_outcome = "previously_conflicting_observations"
        elif latest_payment.get("payment_label_unresolved") or latest_payment.get("payment_observation") == "unrecognized":
            payment_outcome = "previously_unresolved_payment_label"
        checkout["archive_observation"] = {"cart_count": view["count"], "order_list_truncated": value["existing_orders"]["hasMore"],
                                           "new_order_refs_on_original_date": [row["orderNumber"] for row in value["existing_orders"]["orders"]
                                               if row["deliveryDate"] == date and row["orderNumber"] != order_id
                                               and row["orderNumber"] not in checkout["existing_orders"]],
                                           "observed_at": value["orders_observed_at"], "payment_outcome": payment_outcome}
        record["status"] = "checkout_archived_unknown"
        if order_id:
            checkout["archive_observation"].update(linked_order_ref=order_id, linked_merchant_tracking_status=exact_tracking,
                                                   linked_order_detail=detail)
            if detail is not None and (exact_tracking in INACTIVE_ORDERS
                                       or ("addition" not in record["delivery"] and exact_tracking in ACTIVE_ORDERS)):
                record["status"] = "checkout_order_resolved_by_merchant"
    elif (current["goods_sha256"] != checkout["review"]["cart"]["goods_sha256"]
            or set(orders) != set(checkout["existing_orders"])):
        raise ValueError("the original Oda cart or order history changed before a non-submission could be established")
    else:
        if order_id:
            verify_unsubmitted_addition(record, value, pending)
        record["status"] = "checkout_not_submitted"
    record.pop("frame")


def verify_unsubmitted_addition(record, value, pending):
    from hosted_oda_cart import now

    delivery = record["delivery"]
    target = delivery["addition"]
    checked = hosted_oda.response_time(value["order_observed_at"])
    arguments = {"order_number": target["order_id"]}
    raw = verified_provider_result(value["order"])
    hosted_oda.orders_view("get_order", raw, arguments)
    status = hosted_oda.orders_view("order_tracking", verified_provider_result(value["tracking"]), arguments)["status"]
    order = hosted_oda.order_row(raw, include_products=True)
    quantities = {str(line["product"]["id"]): line["quantity"] for line in order["products"]}
    if (not hosted_oda.response_time(value["orders_observed_at"]) <= checked <= now() < hosted_oda.response_time(pending["expires_at"])
            or status != "paid_and_modifiable" or len(quantities) != len(order["products"])
            or quantities != {line["product_id"]: line["quantity"] for line in target["base_products"]}
            or money_cents(order["grossAmount"]) != target["base_total_ore"] or order["deliveryDate"] != delivery["date"]
            or (raw.get("deliverySlotDisplay") is not None and not order_slot_matches(target["window"], raw["deliverySlotDisplay"], checked))):
        raise ValueError("the exact Oda order has not been verified unchanged and free of a pending addition")


def payment_observation(label):
    if label is None:
        return "not_visible"
    label = text(label).casefold()
    if re.fullmatch(r"(?:betaling autorisert|payment authorized|betaling reservert|payment reserved)", label):
        return "authorized"
    if label in PAID_METHOD_LABELS or label in {"betalt", "paid", "betaling gjennomført", "payment complete"}:
        return "paid"
    if re.fullmatch(r"(?:betaling(?:en)? (?:mislyktes|feilet|(?:er )?avvist|venter)|kortet ble avvist|venter på betaling|"
                    r"ikke betalt|ubetalt|(?:card|payment) declined|payment (?:failed|pending)|unpaid)", label):
        return "negative"
    return "unrecognized"


def observe_order(record, value, key):
    from hosted_oda_cart import bound_times, now, require_fields
    from hosted_oda_delivery import physical_address_fingerprint

    fields = ("action", "frame_id", "plugin_id", "addresses", "address_observed_at", "observed_at", "browser", "order", "tracking")
    require_fields(value, fields + ("retirement",) if "retirement" in value else fields)
    pending = record["frame"]
    _, observed = bound_times(record, pending, value, key, "observed_at", check_account=False)
    order_id = pending["order_id"]
    delivery = record["delivery"]
    original_account(record, value["addresses"], key)
    checkout = delivery["checkout"]
    expected = checkout["review"]
    browser = value["browser"]
    addition = "addition" in delivery
    fields = ("url", "browser_id", "tab_id", "attempt_id", "order_number", "delivery", "payment_label",
              "address_id", "address_label", "total_ore", "original_tab_retired")
    require_fields(browser, fields + (("rows",) if addition else ()))
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
    if "original_tab_retired_at" in checkout and not retired:
        raise ValueError("use a replacement tab after the original Oda checkout tab was retired")
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
    if addition and not checkout_rows_match(products({"groups": [{"items": raw_order["products"]}]}), browser["rows"]):
        raise ValueError("the Oda browser and official order receipt show different items or quantities")
    label = browser["payment_label"]
    observation = payment_observation(label)
    # The base order's authorization does not prove payment for an addition.
    # Keep negative or unclear receipt evidence so it can still block completion.
    if addition and observation in {"authorized", "paid"}:
        observation = "not_visible"
    method = PAID_METHOD_LABELS.get(text(label).casefold()) if observation == "paid" else None
    requested_method = "card" if expected["payment_method"] == "saved_card" else expected["payment_method"]
    if method is not None:
        previous_method = checkout.get("merchant_payment_evidence")
        if previous_method is not None and previous_method["merchant_payment_method"] != method:
            checkout["conflicting_merchant_payment_evidence"] = previous_method
        checkout["merchant_payment_evidence"] = {
            "merchant_payment_method": method, "payment_method_observed_at": observed.isoformat(),
        }
    method_evidence = dict(checkout.get("merchant_payment_evidence", {}))
    method_matches = None
    if method_evidence:
        conflict = checkout.get("conflicting_merchant_payment_evidence")
        if conflict is not None:
            method_evidence["conflicting_merchant_payment_evidence"] = conflict
        method_matches = conflict is None and method_evidence["merchant_payment_method"] == requested_method
        method_evidence["payment_method_matches_review"] = method_matches
    if retired:
        remember_retirement(checkout, observed)
    conflicting = payment_conflicts_with_tracking(observation, tracking)
    previous_payment = checkout.get("result", {})
    if observation in {"authorized", "paid", "negative"}:
        checkout["last_labeled_payment"] = {"payment_observation": observation, "merchant_tracking_status": tracking,
                                           "payment_evidence_conflicting": conflicting, "observed_at": observed.isoformat()}
        if not conflicting:
            checkout.pop("unclear_payment_label_at", None)
    elif observation == "unrecognized":
        checkout["unclear_payment_label_at"] = observed.isoformat()
    elif previous_payment.get("payment_observation") == "unrecognized":
        checkout.setdefault("unclear_payment_label_at", previous_payment["observed_at"])
    prior_payment = retain_last_labeled_payment(checkout)
    exact_review = items_match and total == expected.get("revised_total_ore", expected["payable_ore"])
    if delivery.get("owner_handoff"):
        amounts = {"app_cart_amount_ore": expected["payable_ore"], "matches_handoff_cart": exact_review}
    else:
        amounts = {"approved_amount_ore": expected["payable_ore"], "approved_items_match": items_match,
                   "approved_purchase_matches_order": exact_review and method_matches is not False}
        if addition:
            amounts["reviewed_revised_total_ore"] = expected["revised_total_ore"]
            amounts["payment_evidence_scope"] = "order_revision_only"
    checkout["result"] = {"order_id": order_id, "amount_ore": total, "currency": "NOK", **amounts, **method_evidence,
                          "original_reference_source": checkout.get("confirmation_source", "original_checkout_confirmation"),
                          "merchant_tracking_status": tracking, "merchant_reports_payment_authorized": observation == "authorized",
                          "payment_observation": observation, "payment_evidence_conflicting": conflicting,
                          "payment_label_unresolved": "unclear_payment_label_at" in checkout,
                          "observed_at": observed.isoformat()}
    if observation in {"not_visible", "unrecognized"}:
        prior = prior_payment or {}
        payment_conflict_unresolved = (recorded_payment_conflict(previous_payment, prior)
                                       or payment_conflicts_with_tracking(prior.get("payment_observation"), tracking))
        if prior_payment is not None:
            checkout["result"]["prior_labeled_payment"] = prior_payment
        if prior_payment is not None or payment_conflict_unresolved:
            checkout["result"]["payment_conflict_unresolved"] = payment_conflict_unresolved
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
    if release and tracking in ACTIVE_ORDERS and not unresolved_payment and (not addition or exact_review and method_matches is not False):
        record["status"] = ("checkout_confirmed" if exact_review and method_matches is not False
                            else "checkout_order_observed_changed")
    elif release and tracking in INACTIVE_ORDERS:
        if native_method(delivery) != "vipps":
            record["status"] = "checkout_order_inactive"
        elif retired and "retirement" in value:
            verify_retirement(checkout, value["retirement"], observed)
            checkout["vipps_inactive_reconciled_at"] = observed.isoformat()
            record["status"] = "checkout_order_inactive"
    record.pop("frame")
