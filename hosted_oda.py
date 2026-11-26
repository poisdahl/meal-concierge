"""Normalize one cart response supplied by the Dot's attached Oda plugin."""
from datetime import datetime, timedelta, timezone
import re
import uuid

from agent_views import project_agent_result
from core import cart_contents


CART_REQUEST = {"operation": "cart", "action": "get", "response_view": "agent"}
CART_FIELDS = {"groups", "productQuantityCount", "totalGrossAmount", "totalBreakdown", "url"}
CART_LINKS = {"https://oda.com/no/cart/", "https://oda.com/no/cart"}
BREAKDOWN_FIELDS = {"subtotal", "productDiscounts", "couponDiscount", "surcharge",
                    "bottleDeposit", "packaging", "deliveryFee", "total"}
EVIDENCE = "host_supplied_oda_mcp"


def issue(plugin):
    issued = datetime.now(timezone.utc)
    return {"read_id": str(uuid.uuid4()), "plugin_id": plugin, "provider": "oda", "tool": "get_cart",
            "arguments": {}, "issued_at": issued.isoformat(),
            "expires_at": (issued + timedelta(minutes=5)).isoformat()}


def response_time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("hosted Oda observation requires its actual timezone-aware time")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("hosted Oda observation requires its actual timezone-aware time")
    return result


def money(value, *, signed=False):
    pattern = r"-?[0-9]{1,12}(?:[.,][0-9]{1,2})?" if signed else r"[0-9]{1,12}(?:[.,][0-9]{1,2})?"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("hosted Oda cart amount is invalid")
    return value.replace(",", ".")


def cart_line(item):
    product = item.get("product") if isinstance(item, dict) else None
    if not isinstance(product, dict):
        raise ValueError("hosted Oda cart product identity is unavailable")
    product_id, name = product.get("id"), product.get("name")
    if (type(product_id) is not int or not 0 < product_id <= 2**53 - 1
            or not isinstance(name, str) or not name.strip() or len(name) > 1_000):
        raise ValueError("hosted Oda cart product identity is unavailable")
    return {"product": {"id": product_id, "name": name}, "quantity": item.get("quantity"),
            "totalGrossAmount": money(item.get("totalGrossAmount"))}


def cart_view(cart):
    if (not isinstance(cart, dict) or set(cart) != CART_FIELDS or not isinstance(cart["groups"], list)
            or type(cart["productQuantityCount"]) is not int
            or not isinstance(cart["url"], str) or cart["url"] not in CART_LINKS):
        raise ValueError("incomplete hosted Oda cart result or unexpected Oda cart link")
    breakdown = cart["totalBreakdown"]
    if not isinstance(breakdown, dict) or not BREAKDOWN_FIELDS <= breakdown.keys():
        raise ValueError("incomplete hosted Oda cart breakdown")
    amounts = {key: money(breakdown[key], signed=key in {"productDiscounts", "couponDiscount"})
               for key in sorted(BREAKDOWN_FIELDS)}
    groups = []
    for group in cart["groups"]:
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            raise ValueError("hosted Oda cart group is invalid")
        groups.append({"items": [cart_line(item) for item in group["items"]]})
    observed = {"groups": groups, "totalGrossAmount": money(cart["totalGrossAmount"]),
                "totalBreakdown": amounts, "productQuantityCount": cart["productQuantityCount"]}
    cart_contents(observed, require_quantities=True)
    view = project_agent_result("cart", "get", observed, limit=20)
    if view.get("cart_normalization") is not None or "amounts" not in view:
        raise ValueError("hosted Oda cart totals could not be verified")
    lines = view["lines"]
    lines["truncated"] = lines.pop("next_offset", None) is not None
    return {**view, "provider": "oda", "cart_url": cart["url"]}


def complete(frame, reply):
    common = {"read_id", "plugin_id", "tool", "observed_at"}
    if not isinstance(reply, dict) or set(reply) not in (common | {"cart"}, common | {"error"}):
        raise ValueError("expected one structured Oda cart result or a hosted-unavailable ending")
    if any(reply[key] != frame[key] for key in ("read_id", "plugin_id", "tool")):
        raise ValueError("reply does not match the original hosted Oda request")
    observation = response_time(reply["observed_at"])
    if not response_time(frame["issued_at"]) <= observation <= datetime.now(timezone.utc) < response_time(frame["expires_at"]):
        raise ValueError("the original hosted Oda read expired or the observation time changed")
    if "error" in reply:
        if reply["error"] != "unavailable":
            raise ValueError("report only the fixed hosted Oda unavailable status")
        return {"ok": False, "status": "hosted_oda_unavailable",
                "next": "Check the original Oda plugin and authorization before asking for a new read."}
    return {"ok": True, "status": "cart_read", "observed_at": observation.isoformat(),
            "evidence": EVIDENCE, "result": cart_view(reply["cart"])}
