"""Review a staged MENY order edit, hand it to the owner, and read it back."""
from datetime import date, datetime, time, timedelta, timezone
import re
import uuid
from zoneinfo import ZoneInfo

from clients import dots, dots_meny_delivery as delivery
from core import HouseholdError, money_cents
from meny import (CHECKOUT_URL, MENY_MONTH_NAMES, MENY_WEEKDAY_NAMES, meny_delivery_window_identity,
                  normalize_checkout_payment_snapshot, normalize_delivery_slot_ref, normalize_product_ref)


STATE_KEY = "native_meny_edit"
ACTIONS = {"change_begin", "change_review", "change_approve", "change_reconcile", "change_discard"}
ACTIVE = {"edit_draft", "edit_review", "edit_owner_pending"}
UPDATE_CONTROL = "Send oppdatering"
READS_BY_ACTION = {
    "change_review": {"review_edit_checkout"},
    "change_approve": {"read_edit_receipt", "review_edit_checkout"},
    "change_reconcile": {"observe_edit_handoff", "read_edit_receipt"},
    "change_discard": {"observe_edit_handoff", "read_edit_receipt", "verify_observation_context", "get_cart"},
}


def active(state):
    return (state.get(STATE_KEY) or {}).get("status") in ACTIVE


def exact_fields(value, fields):
    dots.object_fields(value, fields, fields)


def fingerprint(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def canonical_uuid(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def identity(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 220 or not value.isprintable()
            or value != " ".join(value.split())):
        raise HouseholdError("use the complete visible MENY product and package identity")
    return value.casefold()


def quantity(value):
    return type(value) is int and 1 <= value <= 1000


def amount(value):
    return type(value) is int and 1 <= value <= 100_000_000


def product_path(value):
    if normalize_product_ref(value) != value:
        raise HouseholdError("use the exact MENY product path")
    dots.public_path(value)
    return value


def validate(request):
    action = request["action"]
    fields = {"operation", "action"}
    if action == "change_begin":
        exact_fields(request, fields | {"order_id", "addition"})
        if not isinstance(request["order_id"], str) or re.fullmatch(r"[0-9]{1,20}", request["order_id"]) is None:
            raise ValueError("use the exact original MENY order number")
        addition = request["addition"]
        exact_fields(addition, {"product_id", "identity", "quantity"})
        product_path(addition["product_id"])
        identity(addition["identity"])
        if not quantity(addition["quantity"]):
            raise ValueError("the MENY addition needs an exact positive package quantity")
        return
    fields |= {"edit_id"}
    if action == "change_approve":
        fields |= {"review_id", "review_digest", "approved_reservation_ore"}
    if action == "change_discard":
        fields |= {"owner_confirmed_no_submit"}
    optional = {"accept_observed_digest"} if action == "change_reconcile" else set()
    dots.object_fields(request, fields | optional, fields)
    if not canonical_uuid(request["edit_id"]):
        raise ValueError("use the original MENY edit reference")
    if action == "change_approve" and (not canonical_uuid(request["review_id"])
            or not fingerprint(request["review_digest"]) or not amount(request["approved_reservation_ore"])):
        raise ValueError("use the exact owner-approved MENY edit review and reservation")
    if action == "change_discard" and request["owner_confirmed_no_submit"] is not True:
        raise ValueError("the owner must confirm no original edit submission or payment")
    if "accept_observed_digest" in request and not fingerprint(request["accept_observed_digest"]):
        raise ValueError("use the exact owner-accepted MENY receipt discrepancy")


def account_binding(host):
    return {key: value for key, value in host.config["browser_binding"].items() if key != "tab_id"}


def check_account(host, result):
    if (result["authenticated"] is not True
            or result["account_sha256"] != host.config["browser_binding"]["account_sha256"]):
        raise HouseholdError("the original authenticated MENY account changed")


def window(value, display):
    ref, _ = normalize_delivery_slot_ref(value)
    if ref != value:
        raise HouseholdError("use the exact dated MENY delivery window")
    day_text, span = ref[len("meny:"):].split("T", 1)
    day = date.fromisoformat(day_text)
    expected = f"{MENY_WEEKDAY_NAMES[day.weekday()]} {day.day}. {MENY_MONTH_NAMES[day.month - 1]} kl. {span.replace('/', '-')}"
    if meny_delivery_window_identity(display) != meny_delivery_window_identity(expected):
        raise HouseholdError("the MENY delivery window changed")
    return ref


def items_view(lines, count, *, quote=False):
    if (not isinstance(lines, list) or not 1 <= len(lines) <= 100
            or type(count) is not int or not 1 <= count <= 10000):
        raise HouseholdError("the complete MENY order lines are required")
    items, products = {}, set()
    for line in lines:
        fields = {"identity", "quantity"} | ({"product_id", "unit_price_ore"} if quote else set())
        exact_fields(line, fields)
        key = identity(line["identity"])
        if key in items or not quantity(line["quantity"]):
            raise HouseholdError("the MENY package identities or quantities are ambiguous")
        if quote:
            product = product_path(line["product_id"])
            if product in products or not amount(line["unit_price_ore"]):
                raise HouseholdError("the staged MENY product or price is ambiguous")
            products.add(product)
        items[key] = {**line, "identity": key}
    if sum(line["quantity"] for line in items.values()) != count:
        raise HouseholdError("the MENY order item count changed")
    return items


def receipt(host, order_id, *, phase):
    value = host.exchange("read_edit_receipt", {"order_id": order_id, "phase": phase})
    fields = {"url", "order_number", "authenticated", "account_sha256", "status"}
    terminal_read = phase in {"outcome", "accept_discrepancy"}
    closure_required = phase in {"discarded", "accept_discrepancy"}
    if value.get("status") == "cancelled" and terminal_read:
        exact_fields(value, fields)
    else:
        fields |= {"edit_code_sha256", "address_sha256", "slot_ref", "delivery_display", "reservation_ore",
                   "product_subtotal_ore", "items_complete", "item_count", "items"}
        if phase in {"original", "approval"}:
            fields.add("cutoff_at")
        if phase == "approval":
            fields |= {"receipt_browser_id", "receipt_tab_id"}
        if closure_required:
            fields |= {"original_handoff_ended", "no_payment_pending"}
        exact_fields(value, fields)
    check_account(host, value)
    if phase == "approval":
        binding = host.config["browser_binding"]
        tab = value["receipt_tab_id"]
        if (value["receipt_browser_id"] != binding["browser_id"] or not isinstance(tab, str)
                or not 1 <= len(tab) <= 150 or not tab.isprintable() or tab == binding["tab_id"]):
            raise HouseholdError("recheck the original MENY receipt in a separate tab of the same cloud browser")
    if value["order_number"] != order_id or value["url"] not in {
            f"https://meny.no/{prefix}profil/nettbutikk/bestilling/{order_id}" for prefix in ("", "trumf-")}:
        raise HouseholdError("the rendered MENY receipt is not the exact original order")
    if value["status"] == "cancelled" and terminal_read:
        return {"status": "cancelled"}
    delivered = terminal_read and value["status"] == "delivered"
    verified_code = fingerprint(value["edit_code_sha256"]) or terminal_read and value["edit_code_sha256"] is None
    if (not (delivered or value["status"] in {"editable", "confirmed"})
            or not verified_code or not fingerprint(value["address_sha256"])
            or value["items_complete"] is not True
            or not (amount(value["reservation_ore"]) or delivered and value["reservation_ore"] is None)
            or (value["product_subtotal_ore"] is not None and not amount(value["product_subtotal_ore"]))):
        raise HouseholdError("the original MENY order or reservation could not be verified")
    if closure_required and (
            value["original_handoff_ended"] is not True or value["no_payment_pending"] is not True):
        raise HouseholdError("the original MENY owner handoff or payment is still unresolved")
    slot_ref = window(value["slot_ref"], value["delivery_display"])
    result = {"order_id": order_id, "edit_code_sha256": value["edit_code_sha256"],
              "address_sha256": value["address_sha256"], "slot_ref": slot_ref,
              "reservation_ore": value["reservation_ore"], "product_subtotal_ore": value["product_subtotal_ore"],
              "items": items_view(value["items"], value["item_count"])}
    if delivered:
        result["status"] = "delivered"
    if phase in {"original", "approval"}:
        cutoff = dots.timestamp(value["cutoff_at"]).astimezone(timezone.utc)
        day, span = slot_ref[len("meny:"):].split("T", 1)
        start = datetime.combine(date.fromisoformat(day), time.fromisoformat(span.split("/")[0]), ZoneInfo("Europe/Oslo"))
        if value["status"] != "editable" or not datetime.now(timezone.utc) < cutoff < start:
            raise HouseholdError("the original MENY order is no longer editable or its cutoff is unverified")
        result["cutoff_at"] = cutoff.isoformat()
    return result


def same_original_receipt(original, observed, *, check_cutoff):
    optional = {"product_subtotal_ore"}
    if not check_cutoff:
        optional.add("cutoff_at")
    return (all(observed[key] == value for key, value in original.items() if key not in optional)
            and (original["product_subtotal_ore"] is None or observed["product_subtotal_ore"] is None
                 or observed["product_subtotal_ore"] == original["product_subtotal_ore"]))


def quote(host, record):
    original, addition = record["original"], record["addition"]
    value, observed = host.exchange("review_edit_checkout", {"order_id": original["order_id"],
                                    "edit_code_sha256": original["edit_code_sha256"], "addition": addition},
                                    observation_time=True)
    exact_fields(value, {"url", "authenticated", "account_sha256", "edit_code_sha256", "address_sha256",
                         "slot_ref", "ordinary_cart", "abort_available", "submit_label", "items_complete",
                         "item_count", "items", "payment"})
    check_account(host, value)
    if (value["url"] != CHECKOUT_URL or value["ordinary_cart"] is not False or value["abort_available"] is not True
            or value["submit_label"] != UPDATE_CONTROL or value["items_complete"] is not True
            or any(value[key] != original[key] for key in ("edit_code_sha256", "address_sha256", "slot_ref"))):
        raise HouseholdError("the original MENY edit, address or update control changed")
    items = items_view(value["items"], value["item_count"], quote=True)
    added = identity(addition["identity"])
    expected = {key: line["quantity"] for key, line in original["items"].items()}
    expected[added] = expected.get(added, 0) + addition["quantity"]
    if ({key: line["quantity"] for key, line in items.items()} != expected
            or items[added]["product_id"] != addition["product_id"]):
        raise HouseholdError("the staged MENY basket does not match the original order and exact addition")
    payment = normalize_checkout_payment_snapshot(value["payment"])
    window(original["slot_ref"], payment["delivery"])
    breakdown = payment.get("price_breakdown") or {}
    if breakdown.get("status") != "itemized":
        raise HouseholdError("the complete itemized MENY edit reservation is required")
    package_total = sum(line["quantity"] * line["unit_price_ore"] for line in items.values())
    subtotal = breakdown["product_subtotal_ore"]
    if (package_total != subtotal
            and not (breakdown["discounts_already_in_subtotal"] is True
                     and package_total + breakdown["discounts_ore"] == subtotal)):
        raise HouseholdError("the MENY package prices do not match the reviewed goods subtotal")
    return {"items": [items[key] for key in sorted(items)], "price_breakdown": breakdown,
            "price_rows": sorted(payment["price_rows"][1]), "reservation_ore": money_cents(payment["total"])}, observed


def output(record, **extra):
    original = record["original"]
    return {"status": record["status"], "edit_id": record["edit_id"], "order_id": original["order_id"],
            "slot_ref": original["slot_ref"], "cutoff_at": original["cutoff_at"],
            "original_reservation_ore": original["reservation_ore"],
            "original_product_subtotal_ore": original["product_subtotal_ore"], "dispatch": False,
            **delivery.EVIDENCE, **{key: record[key] for key in ("quote", "review_id", "review_digest", "expires_at",
                                                                "receipt_product_subtotal_ore", "discrepancy")
                                    if key in record}, **extra}


def replace(store, original, update):
    with store.locked() as state:
        if state.get(STATE_KEY) != original:
            raise HouseholdError("the original MENY edit record changed")
        state[STATE_KEY] = update


def save_quote(store, record, reviewed, command_id):
    updated = {**record, "status": "edit_review", "quote": reviewed, "review_id": command_id}
    updated["expires_at"] = min(datetime.now(timezone.utc) + timedelta(minutes=5),
                                dots.timestamp(record["original"]["cutoff_at"])).isoformat()
    updated["review_digest"] = delivery.digest({"edit_id": record["edit_id"], "quote": reviewed,
                                               "review_id": command_id, "tab": record["tab"]})
    replace(store, record, updated)
    return updated


def handle(host, store, request, command_id):
    action = request["action"]
    state = store.read()
    record = state.get(STATE_KEY)
    if (any(state.get(key) for key in delivery.ORDINARY)
            or (state.get(delivery.STATE_KEY) or {}).get("status") in delivery.PENDING):
        raise HouseholdError("reconcile the original MENY operation before working on an order edit")
    if action == "change_begin":
        if active(state):
            raise HouseholdError("resolve the original MENY order edit before starting a new one")
        host.verify_original_account()
        if host.call("get_cart", {})["items"]:
            raise HouseholdError("keep the ordinary MENY cart; an isolated empty cart is required")
        original = receipt(host, request["order_id"], phase="original")
        started = {"status": "edit_draft", "edit_id": command_id, "original": original,
                   "addition": request["addition"], "account": account_binding(host),
                   "tab": host.config["browser_binding"]["tab_id"]}
        replace(store, record, started)
        return output(started, addition=started["addition"])
    if not record or request["edit_id"] != record["edit_id"] or account_binding(host) != record["account"]:
        raise HouseholdError("use the exact original MENY edit and account")
    if action in {"change_reconcile", "change_discard"}:
        return reconcile(host, store, record, action, request.get("accept_observed_digest"))
    if record["status"] not in {"edit_draft", "edit_review"}:
        raise HouseholdError("reconcile the original MENY owner handoff without submitting again")
    if (host.config["browser_binding"]["tab_id"] != record["tab"]
            or datetime.now(timezone.utc) >= dots.timestamp(record["original"]["cutoff_at"])):
        raise HouseholdError("the original MENY edit tab or cutoff changed")
    if action == "change_approve" and (record["status"] != "edit_review"
            or any(request[key] != record[key] for key in ("review_id", "review_digest"))
            or request["approved_reservation_ore"] != record["quote"]["reservation_ore"]
            or datetime.now(timezone.utc) >= dots.timestamp(record["expires_at"])):
        raise HouseholdError("the exact MENY quote approval expired or changed")
    if action == "change_approve":
        original = record["original"]
        observed = receipt(host, original["order_id"], phase="approval")
        if not same_original_receipt(original, observed, check_cutoff=True):
            raise HouseholdError("the committed original MENY order changed; do not submit its stale edit")
    reviewed, observed = quote(host, record)
    if datetime.now(timezone.utc) >= dots.timestamp(record["original"]["cutoff_at"]):
        raise HouseholdError("the original MENY edit cutoff passed during review")
    if action == "change_review" or reviewed != record["quote"]:
        updated = save_quote(store, record, reviewed, command_id)
        return output(updated, approval_required=True, quote_changed=action == "change_approve")
    now = datetime.now(timezone.utc)
    expires = dots.timestamp(record["expires_at"])
    if now >= expires:
        raise HouseholdError("the exact MENY quote approval expired during review")
    admit_by = min(observed + timedelta(minutes=1), expires)
    if now >= admit_by:
        raise HouseholdError("the fresh MENY checkout observation expired before handoff")
    pending = {**record, "status": "edit_owner_pending", "owner_handoff_admit_by": admit_by.isoformat()}
    replace(store, record, pending)
    return output(pending, owner_handoff_ready=True, next_control=UPDATE_CONTROL,
                  owner_handoff_admit_by=admit_by.isoformat())


def observe_handoff(host, record):
    order_id = record["original"]["order_id"]
    reserve = (record.get("quote") or {}).get("reservation_ore")
    value, observed = host.exchange("observe_edit_handoff", {"order_id": order_id, "reservation_ore": reserve},
                                    observation_time=True)
    exact_fields(value, {"status", "origin", "path", "order_id", "reservation_ore"})
    status = value["status"]
    original_tab = host.config["browser_binding"]["tab_id"] == record["tab"]
    if status == "payment_pending":
        if (not original_tab or record["status"] != "edit_owner_pending" or value["order_id"] is not None
                or value["origin"] != "https://api.vipps.no" or value["path"] != "/dwo-api-application/v1/deeplink/vippsgateway"
                or value["reservation_ore"] != reserve):
            raise HouseholdError("the visible Vipps page is unverified; preserve the original MENY checkout")
    elif status == "edit_confirmed":
        if (not original_tab or record["status"] != "edit_owner_pending" or value["order_id"] != order_id
                or value["origin"] != "https://meny.no" or value["path"] not in {
                    "/kassen/bekreftelse", f"/trumf-profil/nettbutikk/bestilling/{order_id}",
                    f"/profil/nettbutikk/bestilling/{order_id}"} or value["reservation_ore"] is not None):
            raise HouseholdError("the current MENY confirmation does not identify the original edit")
    elif (status != "ready_for_readback" or value["origin"] != "https://meny.no"
            or not isinstance(value["path"], str) or not value["path"].startswith("/")
            or value["order_id"] is not None or value["reservation_ore"] is not None):
        raise HouseholdError("the current checkout state is unverified; preserve the original MENY checkout")
    return status, observed


def reconcile_discrepancy(store, record, observed, accepted, *, items_match, amounts_match):
    original = record["original"]
    changed_goods = observed["items"] != original["items"]
    changed_reservation = observed["reservation_ore"] is not None and observed["reservation_ore"] != original["reservation_ore"]
    if ("confirmation_observed_at" not in record
            or not (changed_goods or changed_reservation or observed.get("status") == "delivered")):
        return output(record, observed=False, items_match=items_match, amounts_match=amounts_match,
                      discrepancy_current=False)
    outcome = {"items": [observed["items"][key] for key in sorted(observed["items"])],
               "reservation_ore": observed["reservation_ore"],
               "product_subtotal_ore": observed["product_subtotal_ore"],
               "delivered": observed.get("status") == "delivered"}
    digest = delivery.digest({"edit_id": record["edit_id"], "review_digest": record["review_digest"],
                              "outcome": outcome})
    previous = record.get("discrepancy")
    discrepancy = {"digest": digest, **outcome}
    updated = {**record, "discrepancy": discrepancy}
    if accepted is not None and previous == discrepancy and accepted == digest:
        updated["status"] = "edit_acknowledged_with_drift"
        replace(store, record, updated)
        return output(updated, observed=False, discrepancy_current=True, discrepancy_accepted=True,
                      original_confirmation_seen=True, items_match=items_match, amounts_match=amounts_match)
    if previous != discrepancy:
        replace(store, record, updated)
    return output(updated, observed=False, discrepancy_current=True, discrepancy_acceptance_required=True,
                  items_match=items_match, amounts_match=amounts_match)


def reconcile(host, store, record, action, accepted):
    if record["status"] not in ACTIVE:
        return output(record, observation_scope="historical")
    original = record["original"]
    if (action == "change_discard" and record["status"] == "edit_owner_pending"
            and datetime.now(timezone.utc) < dots.timestamp(record["owner_handoff_admit_by"])):
        raise HouseholdError("wait for the original MENY handoff admission to expire before discard")
    handoff, observed_at = observe_handoff(host, record)
    if handoff == "payment_pending":
        return output(record, observed=False, owner_payment_pending=True)
    if action == "change_discard":
        if handoff == "edit_confirmed" or "confirmation_observed_at" in record:
            raise HouseholdError("the original MENY order has a confirmation; reconcile it instead")
        host.verify_original_account()
        if host.call("get_cart", {})["items"]:
            raise HouseholdError("the original ordinary MENY cart has not been restored")
        restored = receipt(host, original["order_id"], phase="discarded")
        if not same_original_receipt(original, restored, check_cutoff=False):
            raise HouseholdError("the untouched original MENY receipt could not be verified")
        ending = "edit_discarded"
    else:
        if handoff == "edit_confirmed" and "confirmation_observed_at" not in record:
            confirmed = {**record, "confirmation_observed_at": observed_at.isoformat()}
            replace(store, record, confirmed)
            record = confirmed
        phase = "accept_discrepancy" if accepted is not None else "outcome"
        observed = receipt(host, original["order_id"], phase=phase)
        if observed.get("status") == "cancelled":
            ending = "edit_original_order_cancelled"
        else:
            if record["status"] != "edit_owner_pending":
                raise HouseholdError("no original MENY owner handoff to reconcile")
            if (any(observed[key] != original[key] for key in ("address_sha256", "slot_ref"))
                    or (observed["edit_code_sha256"] != original["edit_code_sha256"]
                        and "confirmation_observed_at" not in record)):
                raise HouseholdError("the exact original MENY order, address or window changed during readback")
            reviewed = record["quote"]
            items_match = ({key: line["quantity"] for key, line in observed["items"].items()}
                           == {line["identity"]: line["quantity"] for line in reviewed["items"]})
            subtotal = observed["product_subtotal_ore"]
            amounts_match = (observed["reservation_ore"] == reviewed["reservation_ore"]
                             and (subtotal is None or subtotal == reviewed["price_breakdown"]["product_subtotal_ore"]))
            if not items_match or not amounts_match:
                return reconcile_discrepancy(store, record, observed, accepted,
                                             items_match=items_match, amounts_match=amounts_match)
            ending = "edit_observed"
    updated = {**record, "status": ending}
    updated.pop("discrepancy", None)
    if ending == "edit_observed":
        updated["receipt_product_subtotal_ore"] = subtotal
    replace(store, record, updated)
    return output(updated, observed=ending == "edit_observed",
                  original_confirmation_seen="confirmation_observed_at" in updated)
