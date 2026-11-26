"""Bound read-only responses supplied by the Dot's attached Oda plugin."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
import uuid

from agent_views import project_agent_result
from core import cart_contents, contains_line_controls
from product_observations import normalize_retail_product_search_batch
from service_common import require_provider_identity, safe_order_id


CART_REQUEST = {"operation": "cart", "action": "get", "response_view": "agent"}
TOOLS = {"cart": {"get": "get_cart"}, "catalog": {"products": "product_search", "recipes": "recipe_search"},
         "delivery": {"list": "get_delivery_slots"},
         "orders": {"list": "get_orders", "get": "get_order", "track": "order_tracking"}}
PAYLOAD_FIELDS = {tool: operation for operation, actions in TOOLS.items() for tool in actions.values()}
CART_FIELDS = {"groups", "productQuantityCount", "totalGrossAmount", "totalBreakdown", "url"}
CART_LINKS = {"https://oda.com/no/cart/", "https://oda.com/no/cart"}
BREAKDOWN_FIELDS = {"subtotal", "productDiscounts", "couponDiscount", "surcharge",
                    "bottleDeposit", "packaging", "deliveryFee", "total"}
EVIDENCE = "host_supplied_oda_mcp"
TRACKING_STATUSES = {"unpaid_order", "unpaid_order_change", "paid_and_modifiable", "paid_and_not_modifiable",
                     "picking", "shipped", "delivered", "failed", "cancelled"}


def tool_request(request):
    operation, action = request.get("operation"), request.get("action")
    tool = TOOLS.get(operation, {}).get(action) if isinstance(operation, str) and isinstance(action, str) else None
    if not tool or request.get("response_view") != "agent":
        raise ValueError("hosted Oda supports only cart get and the discovered read-only actions")
    extras = set(request) - {"operation", "action", "response_view"}
    if tool == "get_cart" and not extras:
        return tool, {}
    if tool == "get_delivery_slots" and extras <= {"date"}:
        return tool, {"delivery_date": observed_date(request["date"])} if "date" in request else {}
    if tool in {"get_order", "order_tracking"} and extras == {"order_id"}:
        return tool, {"order_number": safe_order_id(request["order_id"])}
    if tool == "get_orders" and extras <= {"limit"}:
        return tool, page_arguments(request)
    if operation == "catalog" and extras <= {"query", "limit"}:
        query = request.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 200 or not query.isprintable():
            raise ValueError("hosted Oda search requires a bounded text query")
        arguments = {"queries": [query.strip()]} if tool == "product_search" else {"query": query.strip()}
        return tool, {**arguments, **page_arguments(request)}
    raise ValueError("unexpected arguments for the hosted Oda read")


def page_arguments(request):
    limit = request.get("limit", 5)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("hosted Oda reads require a limit of 1 to 20")
    return {"page": 1, "size": limit}


def observed_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("hosted Oda reads require an ISO calendar date")
    return date.fromisoformat(value).isoformat()


def issue(plugin, tool, arguments):
    issued = datetime.now(timezone.utc)
    return {"read_id": str(uuid.uuid4()), "plugin_id": plugin, "provider": "oda", "tool": tool,
            "arguments": arguments, "issued_at": issued.isoformat(),
            "expires_at": (issued + timedelta(minutes=5)).isoformat()}


def arguments_digest(arguments):
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, allow_nan=False).encode()).hexdigest()


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
            or not isinstance(name, str) or not name.strip() or len(name) > 1_000 or contains_line_controls(name)):
        raise ValueError("hosted Oda cart product identity is unavailable")
    return {"product": {"id": product_id, "name": name}, "quantity": item.get("quantity"),
            "totalGrossAmount": money(item.get("totalGrossAmount"))}


def cart_view(cart):
    if (not isinstance(cart, dict) or set(cart) != CART_FIELDS or not isinstance(cart["groups"], list)
            or type(cart["productQuantityCount"]) is not int
            or not isinstance(cart["url"], str) or cart["url"] not in CART_LINKS):
        raise ValueError("incomplete hosted Oda cart result or unexpected Oda cart link")
    breakdown = cart["totalBreakdown"]
    if (not isinstance(breakdown, dict) or not BREAKDOWN_FIELDS <= breakdown.keys()
            or breakdown.keys() - BREAKDOWN_FIELDS - {"surchargeThresholdDescription"}):
        raise ValueError("incomplete or unrecognized hosted Oda cart breakdown")
    threshold = breakdown.get("surchargeThresholdDescription")
    if threshold is not None and (not isinstance(threshold, str) or len(threshold) > 1_000):
        raise ValueError("hosted Oda surcharge description is invalid")
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
    if not any(Decimal(amounts[key]) for key in BREAKDOWN_FIELDS - {"subtotal", "total"}):
        line_total = sum(Decimal(item["totalGrossAmount"]) for group in groups for item in group["items"])
        if line_total != Decimal(amounts["subtotal"]):
            raise ValueError("hosted Oda cart line totals do not match the unadjusted subtotal")
    view = project_agent_result("cart", "get", observed, limit=20)
    if view.get("cart_normalization") is not None or "amounts" not in view:
        raise ValueError("hosted Oda cart totals could not be verified")
    lines = view["lines"]
    lines["truncated"] = lines.pop("next_offset", None) is not None
    return {**view, "provider": "oda", "cart_url": cart["url"]}


def recipes(data, arguments):
    values = data.get("recipes") if isinstance(data, dict) else None
    if (not isinstance(values, list) or len(values) > arguments["size"]
            or type(data.get("hasMore")) is not bool):
        raise ValueError("hosted Oda recipe search scope changed")
    rows = []
    for recipe in values:
        recipe_id = recipe.get("id") if isinstance(recipe, dict) else None
        title = recipe.get("title") if recipe_id is not None else None
        if (type(recipe_id) is not int or not 0 < recipe_id <= 2**53 - 1
                or not isinstance(title, str) or not title.strip() or len(title) > 300 or contains_line_controls(title)):
            raise ValueError("hosted Oda recipe identity is unavailable")
        row = {"recipe_id": recipe_id, "name": title}
        url = recipe.get("url")
        if (isinstance(url, str) and len(url) <= 2083
                and re.fullmatch(rf"https://oda\.com/no/recipes/{recipe_id}(?:-[A-Za-z0-9._~-]+)?/?", url)):
            row["url"] = url
        rows.append(row)
    if len({row["recipe_id"] for row in rows}) != len(rows):
        raise ValueError("hosted Oda recipe search returned duplicate identities")
    return {"provider": "oda", "query": arguments["query"], "recipes": rows, "has_more": data["hasMore"]}


def catalog_view(tool, data, arguments, observed_at):
    if tool == "product_search":
        queries = arguments["queries"]
        view = normalize_retail_product_search_batch(data, queries, size=arguments["size"],
                                                    observed_at=observed_at)[queries[0]]
        view["scope"]["evidence"] = EVIDENCE
        # The hosted product search schema supplies no ingredient or allergen labels.
        for product in view["products"]:
            product.pop("dietary_evidence", None)
        return view
    return recipes(data, arguments)


def delivery_view(data, arguments):
    from retail_mcp import normalize_retail_delivery_slot, normalize_retail_delivery_slots

    observed = observed_date(data.get("deliveryDate") if isinstance(data, dict) else None)
    if "delivery_date" in arguments and observed != arguments["delivery_date"]:
        raise ValueError("hosted Oda delivery date changed")
    slots = data.get("slots")
    if not isinstance(slots, list) or len(slots) > 400:
        raise ValueError("hosted Oda delivery slot scope changed")
    seen = set()
    for raw in slots:
        slot = normalize_retail_delivery_slot(raw)
        reference = slot["slot_ref"]
        if (not isinstance(raw["price"], str) or reference.rsplit(":", 1)[0] != f"oda:{observed}"
                or reference in seen):
            raise ValueError("hosted Oda delivery slot identity changed")
        seen.add(reference)
    return normalize_retail_delivery_slots(data)


def order_row(order, *, include_products=False):
    if not isinstance(order, dict) or not isinstance(order.get("orderNumber"), str):
        raise ValueError("hosted Oda order identity is unavailable")
    order_id = safe_order_id(order["orderNumber"])
    total, products = order.get("grossAmount"), order.get("products")
    if (order.get("currency") != "NOK" or type(total) not in (int, float)
            or not 0 <= total <= 10**12 or not math.isfinite(total) or not isinstance(products, list)):
        raise ValueError("hosted Oda order amounts or lines are unavailable")
    summary = {"orderNumber": order_id, "deliveryDate": observed_date(order.get("deliveryDate")),
               "currency": "NOK", "grossAmount": total}
    if include_products:
        lines = [cart_line(line) for line in products]
        cart_contents({"items": lines}, require_quantities=True)
        summary["products"] = lines
    return summary


def orders_view(tool, data, arguments):
    if tool == "get_orders":
        orders = data.get("orders") if isinstance(data, dict) else None
        if (not isinstance(orders, list) or len(orders) > arguments["size"]
                or type(data.get("hasMore")) is not bool):
            raise ValueError("hosted Oda order list scope changed")
        rows = [order_row(order) for order in orders]
        if len({row["orderNumber"] for row in rows}) != len(rows):
            raise ValueError("hosted Oda returned duplicate order identities")
        result = project_agent_result("orders", "list", {"orders": rows}, limit=arguments["size"])
        displayed = result["orders"]
        displayed["truncated"] = displayed.pop("next_offset", None) is not None
        return {**result, "provider": "oda", "has_more": data["hasMore"]}
    expected = arguments["order_number"]
    require_provider_identity(data, expected, tracking=tool == "order_tracking")
    if data.get("orderNumber") != expected:
        raise ValueError("hosted Oda orderNumber does not match the original request")
    if tool == "get_order":
        result = project_agent_result("orders", "get", {"order": order_row(data, include_products=True)}, limit=20)
        items = result["order_items"]
        items["truncated"] = items.pop("next_offset", None) is not None
        return {**result, "provider": "oda"}
    status = data.get("status")
    if not isinstance(status, str) or status not in TRACKING_STATUSES:
        raise ValueError("hosted Oda tracking status is unavailable")
    return {"provider": "oda", "order_id": arguments["order_number"], "status": status}


def complete(frame, reply):
    common = {"read_id", "plugin_id", "tool", "observed_at"}
    tool = frame["tool"]
    field = PAYLOAD_FIELDS[tool]
    result_shapes = [common | {field, "arguments"}, common | {"error"}]
    if field == "cart":
        result_shapes.append(common | {field})
    if not isinstance(reply, dict) or set(reply) not in result_shapes:
        raise ValueError("expected one structured Oda read result or a hosted-unavailable ending")
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
    data = reply[field]
    if not isinstance(data, dict) or data.get("isError", False) is not False or "error" in data:
        raise ValueError("expected a successful structured Oda result")
    arguments = reply.get("arguments")
    if field == "cart" and "arguments" in reply and arguments != {}:
        raise ValueError("the hosted Oda cart read takes no arguments")
    if field != "cart" and (not isinstance(arguments, dict) or arguments_digest(arguments) != frame["arguments_sha256"]):
        raise ValueError("arguments do not match the original hosted Oda request")
    if field == "cart":
        result = cart_view(data)
    elif field == "catalog":
        result = catalog_view(tool, data, arguments, observation.isoformat())
    elif field == "delivery":
        result = delivery_view(data, arguments)
    else:
        result = orders_view(tool, data, arguments)
    return {"ok": True, "status": f"{field}_read", "observed_at": observation.isoformat(),
            "evidence": EVIDENCE, "result": result}
