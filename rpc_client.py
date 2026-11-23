"""Shared Unix JSON RPC transport; no agent SDK or MCP dependency."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
from typing import Any


class ServiceError(RuntimeError):
    """A service error response, optionally carrying an unresolved write outcome."""

    def __init__(self, message: str, *, provider_failure: Any = None, outcome: Any = None):
        super().__init__(message)
        self.provider_failure = normalize_provider_failure(provider_failure)
        self.outcome_unknown = outcome == "unknown"

    def outcome_fields(self) -> dict[str, str]:
        if not self.outcome_unknown:
            return {}
        return {"status": "outcome_unknown", "outcome": "unknown",
                "recovery": "Read status and reconcile the original attempt/ref/key before any further write; do not repeat the mutation."}


def normalize_provider_failure(value: Any) -> dict[str, Any] | None:
    """Optional wire diagnostics contain closed facts, never provider text or authority."""
    if not isinstance(value, dict):
        return None
    quantity_failures = {"quantity_not_settled", "quantity_unexpected"}
    cart_failures = quantity_failures | {"cart_not_settled"}
    classes = {
        "client_connect_failed", "client_send_failed", "client_read_failed",
        "client_invalid_response", "cdp_timeout_reported", "cdp_channel_closed_reported",
        "browser_unavailable_reported", "operation_timeout_reported", "adapter_exit_failed",
        "adapter_response_invalid", "adapter_rejected", "local_timeout", "local_spawn_failed",
    } | cart_failures
    phases = {
        "adapter_json_error": "adapter_invocation", "adapter_stderr": "adapter_invocation",
        "adapter_exit": "adapter_invocation", "invalid_stdout": "adapter_invocation",
        "local_timeout": "local_wait", "local_spawn": "local_spawn",
        "cart_readback": "post_click_readback",
    }
    if (value.get("provider") != "meny"
            or not isinstance(value.get("class"), str) or value["class"] not in classes
            or not isinstance(value.get("source"), str) or value["source"] not in phases
            or value.get("phase") != phases[value["source"]]
            or value.get("daemon_request_ending") != "unknown"):
        return None
    if (value["class"] in cart_failures) != (value["source"] == "cart_readback"):
        return None
    failure = {key: value[key] for key in (
        "provider", "class", "source", "phase", "daemon_request_ending",
    )}
    elapsed = value.get("elapsed_ms")
    if value["class"] in quantity_failures and type(elapsed) is int and 0 <= elapsed <= 3_600_000:
        failure["elapsed_ms"] = elapsed
    return failure


SOCKET = Path(os.environ.get("MEAL_CONCIERGE_SOCKET", "/run/meal-concierge/service.sock"))


def rpc_timeout(operation: str, arguments: dict[str, Any]) -> int:
    if operation == "shopping_list" and arguments.get("action") == "match":
        return rpc_timeout("products", {**arguments, "action": "prepare"})
    order_operation = operation == "orders"
    cart_operation = operation == "cart"
    delivery_operation = operation == "delivery"
    if operation in {"checkout", "recipe_pack"}:
        return 660
    if operation == "catalog" and arguments.get("action", "products") == "products":
        return 300
    if operation == "products":
        if arguments.get("action", "prepare") == "prepare":
            return 480
        if arguments.get("action") == "apply":
            return 660
    if operation == "cart" and arguments.get("action") == "change":
        return 420
    if operation == "delivery" and arguments.get("action") == "select":
        return 420
    if operation == "orders" and arguments.get("action") == "get":
        return 420
    return 300 if order_operation or cart_operation or delivery_operation or operation == "products" else 120


def merchant_mutation(operation, action):
    """Operations whose missing reply can hide a retailer-side effect."""
    actions = {
        "cart": {"change", "apply", "set", "update", "clear", "ensure", "sync", "reconcile", "weekly"},
        "products": {"apply"},
        "delivery": {"select"},
        "orders": {"cancel_confirm", "cancel_submit", "remove_confirm", "change_begin", "change_abort"},
        "checkout": {"confirm", "submit", "auto", "switch_payment", "authenticate", "abort_payment"},
    }
    return isinstance(operation, str) and isinstance(action, str) and action in actions.get(operation, ())


def rpc(operation: str, **arguments: Any) -> dict[str, Any]:
    # Probe before dispatch: a pre-contract core must never receive a mutation.
    admission_clock = None
    if operation != "health":
        probe = rpc("health")
        value = probe.get("admission_deadline_clock")
        if type(value) in {int, float} and math.isfinite(value):
            admission_clock = value
    request = {"operation": operation, **arguments, "contract": 1}
    if admission_clock is not None:
        # Use the service's own monotonic clock, so a container clock offset or
        # wall-clock adjustment cannot extend a queued request past its caller.
        request["request_not_after"] = admission_clock + rpc_timeout(operation, arguments)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(rpc_timeout(operation, arguments))
        connection.connect(str(SOCKET))
        frame = (json.dumps(request, ensure_ascii=False) + "\n").encode()
        try:
            connection.sendall(frame)
            data = b""
            while b"\n" not in data and len(data) <= 2 * 1024 * 1024:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
            body, terminator, _ = data.partition(b"\n")
            if not terminator or len(body) > 2 * 1024 * 1024:
                raise ValueError("incomplete or oversized service reply")
            response = json.loads(body)
            if not isinstance(response, dict):
                raise ValueError("expected a response object")
            if response.get("ok") is False:
                if not isinstance(response.get("error"), str) or not response["error"].strip() or "result" in response:
                    raise ValueError("invalid error receipt")
                if "contract" in response and (type(response["contract"]) is not int or response["contract"] != 1):
                    raise ValueError("incompatible error contract")
                if "outcome" in response and response["outcome"] != "unknown":
                    raise ValueError("invalid outcome")
                raise ServiceError(response["error"], provider_failure=response.get("provider_failure"),
                                   outcome=response.get("outcome"))
            if (response.get("ok") is not True or "error" in response or "outcome" in response
                    or type(response.get("contract")) is not int or response["contract"] != 1):
                raise ValueError("incompatible bridge/core contract")
            if not isinstance(response.get("result"), dict):
                raise ValueError("invalid or missing service result")
        except (OSError, ValueError, UnicodeError) as exc:
            if merchant_mutation(operation, arguments.get("action")):
                raise ServiceError("The merchant operation lost its service reply; read and reconcile the original attempt.",
                                   outcome="unknown") from exc
            raise ServiceError("meal concierge service returned no valid response or an incompatible bridge/core contract") from exc
    return response["result"]


def host_email_sender(service_rpc, **request):
    """Thin clients may lack the optional managed sender; native delivery stays usable."""
    try:
        from email_sender import email_sender
    except ModuleNotFoundError as exc:
        if exc.name != "email_sender":
            raise
        return {
            "status": "unavailable", "available": False, "dispatched": False,
            "next": "This client package has no managed email sender. Use an explicitly authorized "
                    "supported native email or chat destination through recipe_delivery, or a "
                    "client with the original managed sender. This capability result does not "
                    "establish the outcome of an earlier send; reconcile that original attempt "
                    "before retrying or changing delivery routes.",
        }
    return email_sender(service_rpc, **request)
