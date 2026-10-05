"""Oda MCP through Muse's protected Request helper and normal urllib routing.

Each operation owns one process. Its inherited flock survives service death;
normal cancellation kills and reaps that process before releasing custody.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import HouseholdError
from retail_mcp import ODA_ENDPOINT, REQUIRED_TOOLS, RetailMcpClient
from service_common import strict_json_loads

BODY_LIMIT = 2 * 1024 * 1024
LINE_LIMIT = 256 * 1024
INPUT_LIMIT = 65_536
CONNECTOR_NAME = re.compile(r"custom\.[a-z0-9][a-z0-9-]{0,99}")
ERRORS = {
    "authorization_required": "Oda authorization was rejected. Follow Muse's connected-provider recovery guidance before another attempt.",
    "deadline_reached": "Oda operation deadline reached; reconcile any pending cart change before continuing.",
    "parent_gone": "Oda operation stopped after its owning service exited.",
    "helper_unavailable": "Muse's protected Oda credential helper is unavailable.",
    "attachment_invalid": "Muse's protected Oda credential attachment is unavailable.",
    "redirect_refused": "Oda MCP redirect is unsupported.",
    "operation_rejected": "Oda rejected the operation.",
    "missing_tools": "Oda MCP lacks required operations.",
    "protocol_failed": "Oda MCP response contract changed.",
    "unavailable": "Muse Oda MCP is unavailable.",
}
RECOVERY_REQUIRED = frozenset({"authorization_required", "helper_unavailable", "attachment_invalid", "redirect_refused"})
HTTP_PHASES = frozenset({"initialize", "notifications/initialized", "tools/list", "tools/call"})


def http_diagnostic(value):
    """Keep only nonsecret protocol facts from the worker's error boundary."""
    if (not isinstance(value, dict) or set(value) != {"phase", "status", "session_assigned"}
            or not isinstance(value["phase"], str) or value["phase"] not in HTTP_PHASES
            or type(value["status"]) is not int or not 300 <= value["status"] <= 599
            or type(value["session_assigned"]) is not bool):
        return None
    return value


def error_message(reason, diagnostic=None):
    message = ERRORS.get(reason, ERRORS["unavailable"])
    if diagnostic is not None:
        message += (f" [phase={diagnostic['phase']}; HTTP {diagnostic['status']}; "
                    f"session_assigned={str(diagnostic['session_assigned']).lower()}]")
    return message


class Stopped(Exception):
    def __init__(self, reason, *, diagnostic=None):
        super().__init__(reason)
        self.diagnostic = http_diagnostic(diagnostic)


def private_operation_directory(path):
    if not isinstance(path, (str, Path)):
        raise HouseholdError("Muse Oda operation directory is required")
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise HouseholdError("Muse Oda operation directory must be absolute, private and owned, without symlinks")
    return path


class MuseProtectedMcpClient(RetailMcpClient):
    def __init__(self, operation_directory, credential_name):
        if not isinstance(credential_name, str) or CONNECTOR_NAME.fullmatch(credential_name) is None:
            raise HouseholdError("Muse requires a connected custom Oda credential name")
        self.operation_directory = private_operation_directory(operation_directory)
        self.credential_name = credential_name
        self.provider, self.label = "oda", "Oda"
        self._terminal_error = None
        self._terminal_http = None

    @property
    def terminal_failure(self):
        return error_message(self._terminal_error, self._terminal_http) if self._terminal_error else None

    def product_dietary_evidence(self, reference, *, deadline=None):
        # The ordinary fallback uses pinned direct sockets, outside Muse routing.
        return {"unavailable": "muse_public_product_detail_unavailable"}

    @contextmanager
    def _operation_lock(self):
        private_operation_directory(self.operation_directory)
        descriptor = os.open(self.operation_directory / ".oda-household.lock",
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise HouseholdError("Muse Oda operation lock must be a private owned file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise HouseholdError("another Oda operation is active") from None
            yield descriptor
        finally:
            # Never LOCK_UN: the child retains this same open file description.
            os.close(descriptor)

    def _worker_command(self, descriptor):
        return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), str(descriptor)]

    def _run(self, tool, arguments, timeout):
        if self.terminal_failure:
            raise HouseholdError(self.terminal_failure)
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
            raise HouseholdError(ERRORS["deadline_reached"])
        timeout = min(timeout, 90.0)
        cutoff = time.monotonic() + timeout
        try:
            data = json.dumps({"tool": tool, "arguments": arguments, "cutoff": cutoff,
                               "credential_name": self.credential_name,
                               "parent_pid": os.getpid()}, allow_nan=False).encode()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise HouseholdError("Muse Oda request must be bounded JSON") from None
        if len(data) > INPUT_LIMIT:
            raise HouseholdError("Muse Oda request exceeds the byte limit")
        with self._operation_lock() as descriptor:
            if self.terminal_failure:
                raise HouseholdError(self.terminal_failure)
            process = None
            try:
                process = subprocess.Popen(self._worker_command(descriptor), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(descriptor,))
                remaining = cutoff - time.monotonic()
                if remaining <= 0:
                    raise HouseholdError(ERRORS["deadline_reached"])
                output, _ = process.communicate(data, timeout=remaining)
                if len(output) > BODY_LIMIT + INPUT_LIMIT:
                    raise ValueError()
                envelope = strict_json_loads(output)
                if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
                    raise ValueError()
                if not envelope["ok"]:
                    reason = envelope.get("error")
                    if not isinstance(reason, str):
                        reason = "unavailable"
                    diagnostic = http_diagnostic(envelope.get("http"))
                    if reason in RECOVERY_REQUIRED:
                        self._terminal_error = reason
                        self._terminal_http = diagnostic
                    raise HouseholdError(error_message(reason, diagnostic))
                if process.returncode != 0 or not isinstance(envelope.get("result"), dict):
                    raise ValueError()
                return (envelope["result"] if tool is None else
                        self._normalize_result(tool, arguments, envelope["result"]))
            except subprocess.TimeoutExpired:
                raise HouseholdError(ERRORS["deadline_reached"]) from None
            except HouseholdError:
                raise
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                raise HouseholdError(ERRORS["unavailable"]) from None
            finally:
                if process is not None:
                    if process.poll() is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    process.wait()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Stopped("redirect_refused")


def load_message(body):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Stopped("protocol_failed")
            result[key] = value
        return result
    # Strict numbers plus duplicate-key refusal at the external boundary.
    strict_json_loads(body)
    return json.loads(body, object_pairs_hook=unique)


def message_result(message, expected_id):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise Stopped("protocol_failed")
    if "method" in message:
        if "id" in message:
            raise Stopped("protocol_failed")
        return None
    if type(message.get("id")) is not int or message["id"] != expected_id or "error" in message:
        raise Stopped("protocol_failed")
    result = message.get("result")
    if not isinstance(result, dict):
        raise Stopped("protocol_failed")
    return result


def read_result(response, expected_id):
    kind = response.headers.get_content_type()
    if kind == "application/json":
        body = response.read(BODY_LIMIT + 1)
        if len(body) > BODY_LIMIT:
            raise Stopped("protocol_failed")
        result = message_result(load_message(body), expected_id)
        if result is None:
            raise Stopped("protocol_failed")
        return result
    if kind != "text/event-stream":
        raise Stopped("protocol_failed")
    used, events, data = 0, 0, []
    while True:
        line = response.readline(min(LINE_LIMIT + 1, BODY_LIMIT - used + 1))
        used += len(line)
        if used > BODY_LIMIT or len(line) > LINE_LIMIT or not line:
            raise Stopped("protocol_failed")
        line = line.rstrip(b"\r\n")
        if not line:
            if data:
                events += 1
                if events > 64:
                    raise Stopped("protocol_failed")
                result = message_result(load_message(b"\n".join(data)), expected_id)
                data = []
                if result is not None:
                    return result
        elif line.startswith(b"data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(b" ") else value)


def exchange(request, *, parent_pid):
    sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
    try:
        from dynamic_credentials import add_surrogate_to_request
    except Stopped:
        raise
    except Exception:
        raise Stopped("helper_unavailable") from None
    opener = build_opener(NoRedirect())  # Default ProxyHandler, TLS and environment.
    session_id, protocol, rpc_id = None, None, 0

    def alive():
        if os.getppid() != parent_pid:
            raise Stopped("parent_gone")
        if time.monotonic() >= request["cutoff"]:
            raise Stopped("deadline_reached")

    def post(method, params=None, *, notification=False, initialize=False):
        nonlocal session_id, rpc_id
        alive()
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            rpc_id += 1
            payload["id"] = rpc_id
        outgoing = Request(ODA_ENDPOINT, data=json.dumps(payload, allow_nan=False).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            method="POST")
        if protocol is not None:
            outgoing.add_header("MCP-Protocol-Version", protocol)
        if session_id is not None:
            outgoing.add_header("Mcp-Session-Id", session_id)
        try:
            add_surrogate_to_request(outgoing, credential_name=request["credential_name"],
                                     entry_name="access_token", allowed_hosts=["oda.com"])
        except Stopped:
            raise
        except Exception:
            raise Stopped("helper_unavailable") from None
        header = outgoing.get_header("Authorization")
        if (outgoing.full_url != ODA_ENDPOINT or outgoing.get_method() != "POST"
                or not isinstance(header, str) or not header.startswith("Bearer hsurr:")
                or len(header) > 16384 or "\r" in header or "\n" in header):
            raise Stopped("attachment_invalid")
        alive()
        try:
            response = opener.open(outgoing, timeout=min(35.0, max(0.001, request["cutoff"] - time.monotonic())))
        except HTTPError as error:
            code = error.code
            error.close()
            raise Stopped("authorization_required" if code in {401, 403} else
                          "redirect_refused" if 300 <= code < 400 else "unavailable",
                          diagnostic={"phase": method, "status": code,
                                      "session_assigned": session_id is not None}) from None
        except URLError:
            raise Stopped("unavailable") from None
        with response:
            if response.geturl() != ODA_ENDPOINT:
                raise Stopped("protocol_failed")
            bound = response.headers.get("Mcp-Session-Id")
            if bound is not None:
                if not bound or len(bound) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in bound):
                    raise Stopped("protocol_failed")
                if initialize:
                    session_id = bound
                elif bound != session_id:
                    raise Stopped("protocol_failed")
            version = response.headers.get("MCP-Protocol-Version")
            if version is not None and not initialize and version != protocol:
                raise Stopped("protocol_failed")
            if notification:
                if response.status != 202:
                    raise Stopped("protocol_failed")
                return None
            if response.status != 200:
                raise Stopped("protocol_failed")
            return read_result(response, rpc_id)

    initialized = post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "meal-concierge-muse", "version": "1"}}, initialize=True)
    protocol = initialized.get("protocolVersion")
    if protocol not in {"2025-03-26", "2025-06-18"}:
        raise Stopped("protocol_failed")
    post("notifications/initialized", notification=True)
    names, cursors, cursor = set(), set(), None
    for _ in range(10):
        page = post("tools/list", {} if cursor is None else {"cursor": cursor})
        tools = page.get("tools")
        if not isinstance(tools, list) or len(tools) > 256:
            raise Stopped("protocol_failed")
        for row in tools:
            name = row.get("name") if isinstance(row, dict) else None
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) is None or name in names:
                raise Stopped("protocol_failed")
            names.add(name)
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or cursor in cursors:
            raise Stopped("protocol_failed")
        cursors.add(cursor)
    else:
        raise Stopped("protocol_failed")
    if not REQUIRED_TOOLS <= names:
        raise Stopped("missing_tools")
    tool = request["tool"]
    if tool is None:
        return {"status": "ready", "protocol_version": protocol, "tools": sorted(names),
                "tool_count": len(names), "transport": "muse_protected_request"}
    if tool not in names:
        raise Stopped("missing_tools")
    result = post("tools/call", {"name": tool, "arguments": request["arguments"]})
    if result.get("isError") is True:
        raise Stopped("operation_rejected")
    if ("isError" in result and type(result["isError"]) is not bool
            or not isinstance(result.get("structuredContent"), dict)):
        raise Stopped("protocol_failed")
    return result["structuredContent"]


def worker_main():
    logging.disable(logging.CRITICAL)
    def expired(_signum, _frame):
        raise Stopped("deadline_reached")
    signal.signal(signal.SIGALRM, expired)
    # Bounds stdin/import/setup too, even if the service disappears before writing.
    signal.setitimer(signal.ITIMER_REAL, 90.0)
    try:
        descriptor = int(sys.argv[1])
        os.fstat(descriptor)  # Keep inherited custody until process exit.
        data = sys.stdin.buffer.read(INPUT_LIMIT + 1)
        if len(data) > INPUT_LIMIT:
            raise Stopped("protocol_failed")
        request = strict_json_loads(data)
        if (not isinstance(request, dict) or set(request) != {"tool", "arguments", "cutoff", "credential_name", "parent_pid"}
                or not isinstance(request["arguments"], dict)
                or request["tool"] is not None and not isinstance(request["tool"], str)
                or not isinstance(request["credential_name"], str)
                or CONNECTOR_NAME.fullmatch(request["credential_name"]) is None
                or type(request["parent_pid"]) is not int or request["parent_pid"] <= 1
                or type(request["cutoff"]) not in {int, float}
                or not math.isfinite(request["cutoff"])):
            raise Stopped("protocol_failed")
        remaining = request["cutoff"] - time.monotonic()
        if remaining <= 0:
            raise Stopped("deadline_reached")
        if remaining > 90:
            raise Stopped("protocol_failed")
        signal.setitimer(signal.ITIMER_REAL, remaining)
        result = {"ok": True, "result": exchange(request, parent_pid=request["parent_pid"])}
        output = json.dumps(result, allow_nan=False).encode()
        if len(output) > BODY_LIMIT + INPUT_LIMIT:
            raise Stopped("protocol_failed")
    except BaseException as error:
        reason = str(error) if isinstance(error, Stopped) and str(error) in ERRORS else "unavailable"
        result = {"ok": False, "error": reason}
        if isinstance(error, Stopped) and error.diagnostic is not None:
            result["http"] = error.diagnostic
        output = json.dumps(result).encode()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    sys.stdout.buffer.write(output + b"\n")
    return 0 if json.loads(output)["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(worker_main())
