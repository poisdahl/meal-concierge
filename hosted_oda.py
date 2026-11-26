"""Normalize one cart response supplied by the Dot's attached Oda plugin."""
from datetime import datetime, timedelta, timezone
import re
import uuid

from agent_views import project_agent_result
from core import cart_contents


CART_REQUEST = {"operation": "cart", "action": "get", "response_view": "agent"}
CART_FIELDS = {"groups", "productQuantityCount", "totalGrossAmount", "totalBreakdown", "url"}
CART_LINKS = {"https://oda.com/no/cart/", "https://oda.com/no/cart"}
EVIDENCE = "host_attested_oda_mcp"


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
    cart = reply["cart"]
    if (not isinstance(cart, dict) or set(cart) != CART_FIELDS or not isinstance(cart["groups"], list)
            or type(cart["productQuantityCount"]) is not int or not isinstance(cart["totalBreakdown"], dict)
            or not isinstance(cart["totalGrossAmount"], str)
            or not re.fullmatch(r"[0-9]{1,12}(?:[.,][0-9]{1,2})?", cart["totalGrossAmount"])
            or not isinstance(cart["url"], str) or cart["url"] not in CART_LINKS):
        raise ValueError("incomplete hosted Oda cart result or unexpected Oda cart link")
    items = cart_contents(cart, require_quantities=True)["items"]
    if any(not item["product_id"].isascii() or not item["product_id"].isdecimal()
           or item["product_id"].startswith("0") or not item["name"].strip() for item in items):
        raise ValueError("hosted Oda cart product identity is unavailable")
    view = project_agent_result("cart", "get", cart)
    if view.get("cart_normalization") is not None or "total" not in view:
        raise ValueError("hosted Oda cart totals could not be verified")
    return {"ok": True, "status": "cart_read", "observed_at": observation.isoformat(),
            "evidence": EVIDENCE, "result": {**view, "provider": "oda", "cart_url": cart["url"]}}
