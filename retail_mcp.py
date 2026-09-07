"""Shared Oda/Mathem MCP transport, not an adapter for arbitrary stores.

Live initialize/tools-list establishes the available operations. The observed
Oda schema has provider-specific Mathem adaptations verified against selected
authenticated product/cart/delivery/order responses and synthetic tests. Endpoints, OAuth
registrations, prices and cart displays remain provider-specific.
"""

from __future__ import annotations

import asyncio
import argparse
from contextlib import contextmanager
from datetime import date, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from core import HouseholdError, cart_summary, validate_delivery_slot
from product_observations import _mathem_ore, normalize_retail_product_search


ODA_ENDPOINT = "https://oda.com/mcp"
SERVER_NAME = "oda-weekly"
MATHEM_ENDPOINT = "https://www.mathem.se/mcp"
REQUIRED_TOOLS = frozenset({
    "product_search", "recipe_search", "likely_to_buy", "get_cart",
    "manipulate_cart", "get_delivery_addresses", "get_delivery_slots",
    "select_delivery_slot", "get_orders", "get_order", "order_tracking",
})


RETAIL_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"
)
ODA_EXACT_PRICE = re.compile(r"kr\u00a0(0|[1-9]\d{0,6})")
ODA_SLOT_REF = re.compile(r"oda:(\d{4}-\d{2}-\d{2}):(0|[1-9]\d*)")
ODA_CART_DELIVERY = re.compile(
    r"Hjemlevering mellom kl (?P<start>[01]\d|2[0-3]) og (?P<end>[01]\d|2[0-3]), "
    r"(?P<day>0?[1-9]|[12]\d|3[01])\. (?P<month>jan|feb|mar|apr|mai|jun|jul|aug|sep|okt|nov|des)"
)
ODA_MONTHS = {
    name: index for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"),
        start=1,
    )
}


def _retail_timestamp(value: Any) -> tuple[str, datetime]:
    if not isinstance(value, str) or len(value) > 64 or RETAIL_TIMESTAMP.fullmatch(value) is None:
        raise HouseholdError("Delivery slot timestamp changed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HouseholdError("Delivery slot timestamp changed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HouseholdError("Delivery slot timestamp changed")
    return value, parsed


def _oda_price(value: Any) -> tuple[int | None, str]:
    if not isinstance(value, str) or len(value) > 64:
        return None, "unavailable"
    match = ODA_EXACT_PRICE.fullmatch(value)
    if match is None:
        return None, "unavailable"
    return int(match[1]) * 100, "exact"


def retail_delivery_slot_date(value: Any, *, provider: str = "oda") -> str:
    pattern = ODA_SLOT_REF if provider == "oda" else re.compile(r"mathem:(\d{4}-\d{2}-\d{2}):(0|[1-9]\d*)")
    match = pattern.fullmatch(str(value or ""))
    if match is None:
        raise HouseholdError("Delivery slot_ref is invalid")
    try:
        return date.fromisoformat(match[1]).isoformat()
    except ValueError as exc:
        raise HouseholdError("Delivery slot_ref is invalid") from exc


def oda_cart_delivery_window(value: Any, *, today: date | None = None) -> dict[str, Any]:
    """Parse the exact selected-delivery display observed in an Oda cart."""

    if not isinstance(value, Mapping):
        raise HouseholdError("Oda selected cart delivery changed")
    slot_id = value.get("slot_id")
    display = value.get("display")
    if isinstance(slot_id, bool) or not isinstance(slot_id, int) or slot_id < 0:
        raise HouseholdError("Oda selected cart delivery changed")
    if not isinstance(display, str) or len(display.encode("utf-8")) > 200:
        raise HouseholdError("Oda selected cart delivery changed")
    match = ODA_CART_DELIVERY.fullmatch(display)
    if match is None:
        raise HouseholdError("Oda selected cart delivery changed")
    current = today or datetime.now(ZoneInfo("Europe/Oslo")).date()
    month = ODA_MONTHS[match["month"]]
    day = int(match["day"])
    year = current.year + (1 if (month, day) < (current.month, current.day) else 0)
    try:
        delivery_date = date(year, month, day)
    except ValueError as exc:
        raise HouseholdError("Oda selected cart delivery changed") from exc
    return {
        "slot_id": slot_id,
        "date": delivery_date.isoformat(),
        "start": f"{int(match['start']):02d}:00",
        "end": f"{int(match['end']):02d}:00",
    }


def retail_cart_delivery_matches_slot(
    cart_delivery: Any,
    slot_value: Any,
    *,
    today: date | None = None,
    provider: str = "oda",
) -> bool:
    """Bind the selected cart to a slot using each supported store's contract."""

    slot = validate_delivery_slot(slot_value)
    if provider == "mathem":
        return (
            isinstance(cart_delivery, Mapping)
            and type(cart_delivery.get("slot_id")) is int
            and cart_delivery["slot_id"] == slot["provider_slot_id"]
        )
    start = datetime.fromisoformat(slot["start_at"].replace("Z", "+00:00")).astimezone(
        ZoneInfo("Europe/Oslo")
    )
    end = datetime.fromisoformat(slot["end_at"].replace("Z", "+00:00")).astimezone(
        ZoneInfo("Europe/Oslo")
    )
    cart_window = oda_cart_delivery_window(cart_delivery, today=today or start.date())
    return (
        cart_window["slot_id"] == slot["provider_slot_id"]
        and cart_window["date"] == start.date().isoformat()
        and cart_window["start"] == start.strftime("%H:%M")
        and cart_window["end"] == end.strftime("%H:%M")
    )


def normalize_retail_delivery_slot(value: Any, *, provider: str = "oda") -> dict[str, Any]:
    """Normalize the shared Oda/Mathem slot fields with provider-specific prices."""

    required = {
        "id", "openDatetime", "closeDatetime", "price",
        "isSelected", "isFull", "isUnavailable",
    }
    if not isinstance(value, Mapping) or not required.issubset(value):
        raise HouseholdError("Delivery slot changed")
    provider_id = value.get("id")
    if isinstance(provider_id, bool) or not isinstance(provider_id, int) or provider_id < 0:
        raise HouseholdError("Delivery slot id changed")
    if not all(isinstance(value.get(field), bool) for field in ("isSelected", "isFull", "isUnavailable")):
        raise HouseholdError("Delivery slot availability changed")
    start_at, start = _retail_timestamp(value.get("openDatetime"))
    end_at, end = _retail_timestamp(value.get("closeDatetime"))
    if end <= start:
        raise HouseholdError("Delivery slot must end after it starts")
    price_ore, price_kind = _oda_price(value.get("price"))
    if provider == "mathem":
        price_ore = _mathem_ore(value.get("price"))
        price_kind = "exact" if price_ore is not None else "unavailable"
    delivery_date = start.astimezone(ZoneInfo("Europe/Stockholm" if provider == "mathem" else "Europe/Oslo")).date().isoformat()
    return validate_delivery_slot({
        "slot_ref": f"{provider}:{delivery_date}:{provider_id}",
        "provider_slot_id": provider_id,
        "start_at": start_at,
        "end_at": end_at,
        "price_ore": price_ore,
        "price_kind": price_kind,
        "selected": value["isSelected"],
    })


def normalize_retail_delivery_slots(value: Any, *, provider: str = "oda") -> dict[str, Any]:
    """Return selectable slots from the shared Oda/Mathem delivery-date response."""

    if not isinstance(value, Mapping) or set(value) != {"deliveryDate", "slots"}:
        raise HouseholdError("Delivery slots changed")
    try:
        delivery_date = date.fromisoformat(str(value.get("deliveryDate") or ""))
    except ValueError as exc:
        raise HouseholdError("Delivery date changed") from exc
    raw_slots = value.get("slots")
    if not isinstance(raw_slots, list):
        raise HouseholdError("Delivery slots changed")
    slots = []
    for raw in raw_slots:
        if not isinstance(raw, Mapping):
            raise HouseholdError("Delivery slot changed")
        if not all(isinstance(raw.get(field), bool) for field in ("isSelected", "isFull", "isUnavailable")):
            raise HouseholdError("Delivery slot availability changed")
        if raw["isFull"] or raw["isUnavailable"]:
            continue
        slot = normalize_retail_delivery_slot(raw, provider=provider)
        start = datetime.fromisoformat(slot["start_at"].replace("Z", "+00:00"))
        if start.astimezone(ZoneInfo("Europe/Stockholm" if provider == "mathem" else "Europe/Oslo")).date() != delivery_date:
            raise HouseholdError("Delivery slot date changed")
        slots.append(slot)
    references: dict[str, dict[str, Any]] = {}
    for slot in slots:
        previous = references.get(slot["slot_ref"])
        if previous is not None and previous != slot:
            raise HouseholdError("Provider returned conflicting delivery slot ids")
        references[slot["slot_ref"]] = slot
    if sum(slot["selected"] for slot in references.values()) > 1:
        raise HouseholdError("Selected delivery slot is ambiguous")
    return {
        "provider": provider,
        "delivery_date": delivery_date.isoformat(),
        "slots": list(references.values()),
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _json_value(dump(mode="json", by_alias=True, exclude_none=True))
    raise HouseholdError("MCP returned an unsupported value")


class RetailMcpClient:
    def product_dietary_evidence(self, reference, *, deadline=None):
        if self.provider != 'oda':
            return {'unavailable': 'provider_product_detail_not_verified'}
        from dietary_assessment import read_oda_product_evidence
        return read_oda_product_evidence(reference, deadline)

    def __init__(self, token_directory: Path | str, *, provider: str = "oda"):
        if provider not in {"oda", "mathem"}:
            raise HouseholdError("MCP provider must be oda or mathem")
        self.provider = provider
        self.label = "Mathem" if provider == "mathem" else "Oda"
        self.endpoint = MATHEM_ENDPOINT if provider == "mathem" else ODA_ENDPOINT
        self.server_name = "mathem-weekly" if provider == "mathem" else SERVER_NAME
        self.token_directory = Path(token_directory)

    def probe(self) -> dict[str, Any]:
        return self._run(None, {}, 90.0)

    def call(self, tool: str, arguments: Mapping[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        if not isinstance(tool, str) or not tool:
            raise HouseholdError(f"{self.label} tool is missing")
        timeout = 90.0 if deadline is None else deadline - time.monotonic()
        if timeout <= 0:
            raise HouseholdError(f"{self.label} operation deadline reached")
        return self._run(tool, dict(arguments), min(timeout, 90.0))

    def _run(self, tool: str | None, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.token_directory.is_dir():
            raise HouseholdError(f"{self.label} login is required")
        with self._lock():
            try:
                return asyncio.run(self._run_async(tool, arguments, timeout))
            except HouseholdError:
                raise
            except Exception as exc:
                # AnyIO groups transport failures; retain our actionable auth
                # status without exposing third-party response/exception text.
                errors = [exc]
                while errors:
                    error = errors.pop(0)
                    if isinstance(error, HouseholdError):
                        raise error from None
                    if isinstance(error, BaseExceptionGroup):
                        errors.extend(error.exceptions)
                raise HouseholdError(f"{self.label} MCP is unavailable") from None

    async def _run_async(self, tool: str | None, arguments: dict[str, Any], timeout: float, *, auth=None) -> dict[str, Any]:
        try:
            import httpx2 as httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            from provider_oauth import build_auth
        except ImportError as exc:
            raise HouseholdError("Meal Concierge MCP runtime is unavailable") from exc

        provider = auth or build_auth(self.token_directory, self.server_name, self.label, self.endpoint)
        async def reject_redirect(response: Any) -> None:
            # httpx response hooks run before next_request is populated. Never
            # forward credential-bearing POST bodies to a redirect destination.
            if response.is_redirect:
                raise HouseholdError(f"{self.label} MCP redirect is unsupported")

        async with httpx.AsyncClient(
            auth=provider,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, read=min(60.0, timeout)),
            event_hooks={"response": [reject_redirect]},
        ) as client:
            async with streamable_http_client(self.endpoint, http_client=client, terminate_on_close=False) as streams:
                async with ClientSession(streams[0], streams[1], read_timeout_seconds=timeout) as session:
                    async with asyncio.timeout(timeout):
                        initialized = await session.initialize()
                        tools: list[Any] = []
                        cursor: str | None = None
                        for _ in range(10):
                            page = await (session.list_tools(cursor=cursor) if cursor else session.list_tools())
                            tools.extend(page.tools)
                            cursor = getattr(page, "next_cursor", getattr(page, "nextCursor", None))
                            if not cursor:
                                break
                        names = {str(getattr(item, "name", "")) for item in tools}
                        missing = sorted(REQUIRED_TOOLS - names)
                        if missing:
                            raise HouseholdError(f"{self.label} MCP lacks required operations: " + ", ".join(missing))
                        server_info = _json_value(getattr(initialized, "server_info", None))
                        protocol = str(getattr(initialized, "protocol_version", ""))
                        status = {
                            "status": "ready",
                            "protocol_version": protocol,
                            "server": server_info,
                            "tool_count": len(names),
                            "tools": sorted(names),
                        }
                        if tool is None:
                            return status
                        if tool not in names:
                            raise HouseholdError(f"{self.label} does not expose {tool}")
                        result = await session.call_tool(tool, arguments)
        if bool(getattr(result, "is_error", getattr(result, "isError", False))):
            raise HouseholdError(f"{self.label} rejected the operation")
        structured = getattr(result, "structured_content", getattr(result, "structuredContent", None))
        value = _json_value(structured)
        if not isinstance(value, dict):
            raise HouseholdError(f"{self.label} returned no structured result")
        if tool == "get_delivery_slots":
            return normalize_retail_delivery_slots(value, provider=self.provider)
        if tool == "product_search":
            normalized = normalize_retail_product_search(value, provider=self.provider)
            requested_size = arguments.get("size")
            if isinstance(requested_size, int) and not isinstance(requested_size, bool):
                normalized["scope"]["requested_size"] = requested_size
            return normalized
        return value

    @contextmanager
    def _lock(self):
        path = self.token_directory / f".{self.provider}-household.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise HouseholdError(f"another {self.label} operation is active") from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Oda/Mathem MCP startup/preflight check")
    parser.add_argument("--provider", choices=("oda", "mathem"), default="oda")
    parser.add_argument("--tokens", type=Path, required=True)
    parser.add_argument("--state", action="store_true", help="also read a secret-free cart/order baseline")
    args = parser.parse_args()
    client = RetailMcpClient(args.tokens, provider=args.provider)
    probe = client.probe()
    output: dict[str, Any] = {
        "status": probe["status"],
        "protocol_version": probe["protocol_version"],
        "server": probe["server"],
        "tool_count": probe["tool_count"],
    }
    if args.state:
        cart = cart_summary(client.call("get_cart", {}))
        orders = client.call("get_orders", {"page": 1, "size": 20})
        encoded = json.dumps({"cart": cart, "orders": orders}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        values = orders.get("orders") if isinstance(orders.get("orders"), list) else []
        latest_status = None
        if values:
            latest = values[0]
            order_id = str(latest.get("orderNumber") or latest.get("order_number") or "") if isinstance(latest, Mapping) else ""
            if order_id:
                latest_status = client.call("order_tracking", {"order_number": order_id}).get("status")
        output["baseline"] = {
            "digest": hashlib.sha256(encoded).hexdigest(),
            "cart_lines": len(cart["items"]),
            "cart_count": cart["count"],
            "cart_total": cart["total"],
            "delivery_selected": bool((cart.get("delivery") or {}).get("display")),
            "orders_returned": len(values),
            "latest_order_status": latest_status,
        }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
