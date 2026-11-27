"""Read-only, exact merchant reviews for an owner's MENY checkout or order change."""
from datetime import datetime, timezone
import re

from clients import dots, dots_meny_delivery as delivery
from core import HouseholdError, money_cents
from meny import (CHECKOUT_URL, meny_delivery_window_identity, meny_selected_delivery,
                  normalize_checkout_payment_snapshot, normalize_product_ref)


def validate(request):
    fields = {"operation", "action"}
    if request["operation"] == "orders":
        dots.object_fields(request, fields | {"order_id"}, fields | {"order_id"})
        if not isinstance(request["order_id"], str) or re.fullmatch(r"[0-9]{1,20}", request["order_id"]) is None:
            raise ValueError("use the exact MENY order number")
    else:
        dots.object_fields(request, fields, fields)


def checkout(host):
    cart = host.call("get_cart", {})
    if not cart["items"]:
        return {"status": "checkout_cart_empty", "dispatch": False}
    selected = meny_selected_delivery(host.call("get_delivery_slots", {})["slots"])
    context = delivery.context(host)
    if selected is None:
        return {"status": "checkout_delivery_required", "dispatch": False}
    window = meny_delivery_window_identity(selected["display"])
    if (not context["delivery"] or not cart["delivery"]
            or window != meny_delivery_window_identity(context["delivery"])
            or window != meny_delivery_window_identity(cart["delivery"]["display"])):
        raise HouseholdError("the MENY checkout requires the same delivery in the picker, header and cart")
    if datetime.fromisoformat(selected["start_at"]) <= datetime.now(timezone.utc):
        raise HouseholdError("the MENY checkout window expired")
    quantities = {item["product_id"]: item["quantity"] for item in cart["items"]}
    if len(quantities) != len(cart["items"]):
        raise HouseholdError("the MENY cart contains ambiguous product quantities")
    result = host.exchange("review_checkout", {"url": CHECKOUT_URL, "payment_method": "vipps",
                           "window": selected, "address_sha256": context["address_sha256"],
                           "quantities": quantities})
    fields = {"url", "ordinary_cart", "address_sha256", "items", "payment"}
    dots.object_fields(result, fields, fields)
    if (result["url"] != CHECKOUT_URL or result["ordinary_cart"] is not True
            or result["address_sha256"] != context["address_sha256"]):
        raise HouseholdError("the exact MENY checkout account, address or cart mode changed")
    items = result["items"]
    if (not isinstance(items, dict) or any(normalize_product_ref(key) != key or type(quantity) is not int
                                          or quantity < 1 for key, quantity in items.items()) or items != quantities):
        raise HouseholdError("the MENY checkout product quantities changed")
    payment = normalize_checkout_payment_snapshot(result["payment"])
    if meny_delivery_window_identity(payment["delivery"]) != window:
        raise HouseholdError("the MENY checkout delivery window changed")
    total = money_cents(payment["total"])
    cart_total = money_cents(cart["total"])
    breakdown = payment.get("price_breakdown")
    return {"status": "owner_checkout_ready", "dispatch": False, "payment_status": "unverified",
            "currency": "NOK", "cart_total_ore": cart_total, "checkout_total_ore": total,
            "cart_to_checkout_difference_ore": total - cart_total, "reviewed_quantities": quantities,
            "price_breakdown": breakdown, "price_breakdown_scope": breakdown["status"] if breakdown else "unverified",
            "delivery": selected, "payment_method": "vipps", "checkout_url": CHECKOUT_URL,
            "next_control": "Til betaling", "completion": "in_shop_with_owner"}


def order(host, order_id):
    result = host.exchange("review_order_change", {"order_number": order_id})
    fields = {"source_origin", "source_path", "source_query_keys", "source_hash", "authenticated", "exact_order",
              "order_number", "archived", "available_changes"}
    dots.object_fields(result, fields, fields)
    paths = (f"/trumf-profil/nettbutikk/bestilling/{order_id}", f"/profil/nettbutikk/bestilling/{order_id}")
    if (result["source_origin"] != "https://meny.no" or result["source_path"] not in paths
            or result["authenticated"] is not True or result["exact_order"] is not True
            or result["order_number"] != order_id or type(result["archived"]) is not bool or result["source_hash"] != ""
            or result["source_query_keys"] not in (["archived", "mworderid"], [])
            or result["archived"] != bool(result["source_query_keys"])):
        raise HouseholdError("the rendered MENY page does not match the exact original order")
    controls = result["available_changes"]
    if (not isinstance(controls, list) or any(change not in ("items", "delivery", "cancel") for change in controls)
            or len(set(controls)) != len(controls) or (result["archived"] and controls)):
        raise HouseholdError("the eligible MENY order-change controls changed")
    return {"status": "owner_order_change_ready" if controls else "order_change_unavailable", "dispatch": False,
            "order_id": order_id, "available_changes": controls, "completion": "in_shop_with_owner" if controls else None}


def handle(host, store, request):
    state = store.read()
    if (any(state.get(key) for key in delivery.ORDINARY)
            or (state.get(delivery.STATE_KEY) or {}).get("status") in delivery.PENDING):
        raise HouseholdError("reconcile the original MENY operation before preparing checkout or an order change")
    result = checkout(host) if request["operation"] == "checkout" else order(host, request["order_id"])
    return {**result, **delivery.EVIDENCE, "observed_at": datetime.now(timezone.utc).isoformat()}
