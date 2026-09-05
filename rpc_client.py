"""Shared Unix JSON RPC transport; no agent SDK or MCP dependency."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from typing import Any


class ServiceError(RuntimeError):
    """An explicit service rejection, distinct from an uncertain transport loss."""


SOCKET = Path(os.environ.get("MEAL_CONCIERGE_SOCKET", "/run/meal-concierge/service.sock"))


def rpc_timeout(operation: str, arguments: dict[str, Any]) -> int:
    order_operation = operation == "orders"
    cart_change = operation == "cart" and arguments.get("action") != "get"
    delivery_operation = operation == "delivery"
    if operation == "checkout":
        return 660
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
        raise ServiceError(str(response.get("error") or "meal concierge operation failed"))
    if response.get("contract") != 1:
        raise ServiceError("incompatible bridge/core contract; upgrade the stopped service before attaching this client")
    return response["result"]
