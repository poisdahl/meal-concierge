"""Shared Unix JSON RPC transport; no agent SDK or MCP dependency."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from typing import Any


class ServiceError(RuntimeError):
    """An explicit service rejection, distinct from an uncertain transport loss."""

    def __init__(self, message: str, *, provider_failure: Any = None):
        super().__init__(message)
        self.provider_failure = normalize_provider_failure(provider_failure)


def normalize_provider_failure(value: Any) -> dict[str, str] | None:
    """Optional wire diagnostics contain closed facts, never provider text or authority."""
    if not isinstance(value, dict):
        return None
    classes = {
        "client_connect_failed", "client_send_failed", "client_read_failed",
        "client_invalid_response", "cdp_timeout_reported", "cdp_channel_closed_reported",
        "browser_unavailable_reported", "operation_timeout_reported", "adapter_exit_failed",
        "adapter_response_invalid", "adapter_rejected", "local_timeout", "local_spawn_failed",
    }
    phases = {
        "adapter_json_error": "adapter_invocation", "adapter_stderr": "adapter_invocation",
        "adapter_exit": "adapter_invocation", "invalid_stdout": "adapter_invocation",
        "local_timeout": "local_wait", "local_spawn": "local_spawn",
    }
    if (value.get("provider") != "meny"
            or not isinstance(value.get("class"), str) or value["class"] not in classes
            or not isinstance(value.get("source"), str) or value["source"] not in phases
            or value.get("phase") != phases[value["source"]]
            or value.get("daemon_request_ending") != "unknown"):
        return None
    return {key: value[key] for key in (
        "provider", "class", "source", "phase", "daemon_request_ending",
    )}


SOCKET = Path(os.environ.get("MEAL_CONCIERGE_SOCKET", "/run/meal-concierge/service.sock"))


def rpc_timeout(operation: str, arguments: dict[str, Any]) -> int:
    order_operation = operation == "orders"
    cart_change = operation == "cart" and arguments.get("action") != "get"
    delivery_operation = operation == "delivery"
    if operation in {"checkout", "recipe_pack"}:
        return 660
    if operation == "cart" and arguments.get("action") == "change":
        return 420
    return 300 if order_operation or cart_change or delivery_operation or operation == "products" else 120


def rpc(operation: str, **arguments: Any) -> dict[str, Any]:
    # Probe before dispatch: a pre-contract core must never receive a mutation.
    if operation != "health":
        rpc("health")
    request = {"operation": operation, **arguments, "contract": 1}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(rpc_timeout(operation, arguments))
        connection.connect(str(SOCKET))
        connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        data = b""
        while b"\n" not in data and len(data) <= 2 * 1024 * 1024:
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
    try:
        response = json.loads(data.split(b"\n", 1)[0])
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError("meal concierge service returned no valid response") from exc
    if response.get("ok") is not True:
        raise ServiceError(str(response.get("error") or "meal concierge operation failed"),
                           provider_failure=response.get("provider_failure"))
    if response.get("contract") != 1:
        raise ServiceError("incompatible bridge/core contract; upgrade the stopped service before attaching this client")
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
