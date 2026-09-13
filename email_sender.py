"""Host-side email setup and single-attempt execution for MCP and CLI clients.

The service owns menus, destinations and delivery journals. This small executor
owns the network call and a durable local receipt across a lost service ack.
The fixed host config references an existing sender; tool arguments never select
executables, credential files or SMTP endpoints.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import valid_email_address
from delivery_operations import capabilities, identifier
from delivery_transport import export_part
from email_transports import CommandTransport, SMTPTransport, check_message


def config_path():
    return Path(os.environ.get("MEAL_CONCIERGE_EMAIL_CONFIG", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "meal-concierge/email-sender.json"))


def _save(path, value):
    """Atomic, durable receipt; a successful network response must survive ack loss."""
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class EmailSender:
    def __init__(self, rpc, config, *, transport_factory=None):
        self.rpc, self.config = rpc, config
        identifier(config.get("runtime_id"), "runtime_id")
        rows = config.get("connections")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
            raise ValueError("configure one to eight existing email connections")
        self.connections = {}
        self.factory = transport_factory or self._transport
        for row in rows:
            key = identifier(row.get("id"), "connection_id")
            if key in self.connections or type(row.get("unattended", False)) is not bool:
                raise ValueError("email connection IDs must be unique and unattended a boolean")
            self.connections[key] = row
        self.directory = Path(config["receipt_dir"])
        if not self.directory.is_absolute():
            raise ValueError("email receipt_dir must be an absolute private host directory")

    @staticmethod
    def _transport(row):
        if row.get("type") == "command":
            return CommandTransport(row)
        if row.get("type") == "smtp":
            return SMTPTransport(row)
        raise ValueError("unsupported email connection; configure command or smtp")

    def inspect(self, connection_id):
        row = self.connections.get(connection_id)
        if row is None:
            raise ValueError("selected email connection is unavailable in this runtime")
        actual = self.factory(row).inspect()
        capabilities(actual, "email", {"sender": actual.get("sender"), "recipient": actual.get("sender")})
        account = actual.get("account")
        if not isinstance(account, str) or not account or len(account) > 512:
            raise ValueError("email connection must establish its actual account")
        return actual

    def status(self):
        selected = self.rpc("recipe_delivery", action="sender")
        connections = []
        for key, row in self.connections.items():
            try:
                actual = self.inspect(key)
                connections.append({"id": key, "status": "available", "sender": actual["sender"],
                                    "pdf": actual.get("pdf", False), "images": actual.get("images", False),
                                    "unattended": row.get("unattended", False), "reconciliation": actual.get("reconciliation", False)})
            except Exception:
                connections.append({"id": key, "status": "needs_user_action",
                                    "next": "Check this existing connection's login, send permission and availability in this runtime."})
        return {**selected, "connections": connections, "sent": False,
                "next": "Select sender, recipient and timing once with configure. No test email was sent."}

    def configure(self, request):
        key = request.get("connection_id")
        if key is None and len(self.connections) == 1:
            key = next(iter(self.connections))
        actual = self.inspect(key)
        recipient = request.get("recipient")
        if not valid_email_address(recipient):
            raise ValueError("select the exact email recipient")
        timing = request.get("timing", "on_request")
        binding = {"connection_id": key, "runtime_id": self.config["runtime_id"], "account": actual["account"],
                   "installation_id": self.rpc("recipe_delivery", action="sender")["installation_id"],
                   "sender": actual["sender"], "recipient": recipient, "capabilities": actual,
                   "unattended": self.connections[key].get("unattended", False), "timing": timing}
        if request.get("sender") is not None and request["sender"] != actual["sender"]:
            raise ValueError("selected sender does not match the actual connected account")
        transport = self.factory(self.connections[key])
        if hasattr(transport, "preflight"):
            transport.preflight(binding)
        self.rpc("recipe_delivery", action="configure", sender_binding=binding,
                 changes={"email": {"enabled": True}}, capabilities={"email": actual},
                 destinations={"email": {"sender": actual["sender"], "recipient": recipient}})
        return {"configured": True, "binding": binding, "sent": False,
                "next": "Connection saved. Existing jobs and paused work are unchanged; delivery-day email still uses its verified native schedule."}

    def transport(self, binding, *, inspect=True):
        if not binding or binding.get("runtime_id") != self.config["runtime_id"]:
            raise ValueError("original email runtime is unavailable; no automatic sender replacement")
        if binding.get("installation_id") != self.rpc("recipe_delivery", action="sender")["installation_id"]:
            raise ValueError("email binding belongs to a different household installation")
        key = binding["connection_id"]
        if key not in self.connections:
            raise ValueError("original email connection is unavailable")
        if inspect:
            actual = self.inspect(key)
            if any(actual.get(k) != binding[k] for k in ("account", "sender")):
                raise ValueError("email account changed; reconnect the originally selected account")
        transport = self.factory(self.connections[key])
        if inspect and hasattr(transport, "preflight"):
            transport.preflight(binding)
        return transport

    @contextmanager
    def locked(self, target):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if self.directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("email receipt directory must be private and owned by this runtime user")
        installation = self.rpc("recipe_delivery", action="sender")["installation_id"]
        key = hashlib.sha256(json.dumps({**target, "installation_id": installation}, sort_keys=True).encode()).hexdigest()
        fd = os.open(self.directory / (key + ".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield self.directory / (key + ".json"), self.directory / (key + ".eml")
        finally:
            os.close(fd)

    def ack(self, record):
        target = record["target"]
        outcome = record["outcome"]
        if target["kind"] == "menu":
            return self.rpc("recipe_delivery", action="reconcile", job_id=target["request_id"],
                            part_id=record["part_id"], token=record["token"], outcome=outcome, evidence=record.get("evidence"))
        return self.rpc("email", action="release" if outcome == "not_sent" else "reconcile_send", provider=target["provider"], order_id=target["order_id"],
                        claim_token=record["token"], send_outcome="sent" if outcome == "accepted" else outcome,
                        sender_receipt=record.get("evidence"))

    def recover(self, record, path, message_path):
        # Never repeat network submission, even if the native helper vanished.
        if not record.get("token") and record["target"]["kind"] == "menu":
            job = self.rpc("recipe_delivery", action="get", job_id=record["target"]["request_id"], part_id=record["part_id"])
            part = job["parts"][0]
            if part.get("executor_attempt") != record["executor_attempt"]:
                if part["status"] == "ready":
                    record.update(outcome="not_sent", evidence="executor:no-begin-acquired", acknowledged=True, token=None)
                    _save(path, record)
                    return {"sent": False, "outcome": "not_sent", "acknowledged": True,
                            "next": "No network send started. Use explicit retry for this same request."}
                return {"sent": False, "outcome": "unknown", "next": "This executor did not acquire the attempt. Inspect the original service attempt; do not resend."}
            record["token"] = part.get("token")
        if record.get("outcome") not in {"accepted", "not_sent"}:
            if record["phase"] != "network_started":
                record.update(outcome="not_sent", evidence="executor:durable-marker-before-network")
            else:
                raw = message_path.read_bytes()
                check_message(raw, record["binding"], record["sha256"])
                result = self.transport(record["binding"], inspect=False).reconcile(raw, record["binding"])
                self.record_result(record, result)
            _save(path, record)
        return self.finish(record, path)

    @staticmethod
    def record_result(record, result):
        outcome = result.get("outcome")
        evidence = result.get("evidence")
        if outcome not in {"accepted", "not_sent", "unknown"}:
            raise ValueError("sender returned no observable outcome")
        if outcome != "unknown" and (not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1024 or any(ord(c) < 32 for c in evidence)):
            raise ValueError("sender returned no bounded positive receipt")
        if (record.get("outcome"), record.get("evidence")) != (outcome, evidence):
            record.pop("acknowledged", None)
        record.update(outcome=outcome, evidence=evidence, next=result.get("next"))

    def finish(self, record, path):
        result = {"outcome": record["outcome"], "sent": record["outcome"] == "accepted", "recipient_read": "unknown",
                  "warnings": record.get("warnings", []), "retry_allowed": False}
        try:
            if not record.get("token") and not record.get("acknowledged"):
                raise ValueError("original begin token not yet established")
            if not record.get("acknowledged"):
                self.ack(record)
                record["acknowledged"] = True
                _save(path, record)
            result["acknowledged"] = True
        except Exception:
            result.update(acknowledged=False, next="Receipt retained. Repeat this same request to reconcile; do not create a replacement send.")
        if record["outcome"] == "unknown":
            result["next"] = record.get("next") or "Email may have been sent. Reconcile this same request; never resend blindly."
        return result

    def execute(self, request):
        action = request.get("action", "status")
        if action == "status":
            return self.status()
        if action == "configure":
            return self.configure(request)
        if action == "adopt_order":
            return self.rpc("email", action="bind_sender", provider=request.get("provider"), order_id=request.get("order_id"))
        if action not in {"send", "reconcile", "retry", "send_order", "reconcile_order", "retry_order"}:
            raise ValueError("unknown email sender action")
        order = action.endswith("order")
        target = ({"kind": "order", "provider": request.get("provider"), "order_id": request.get("order_id")}
                  if order else {"kind": "menu", "request_id": identifier(request.get("request_id"), "request_id")})
        with self.locked(target) as (path, message_path):
            if path.exists():
                record = json.loads(path.read_text())
                if not order and request.get("menu_ref") is not None and request["menu_ref"] != record.get("menu_ref"):
                    raise ValueError("request_id is bound to another saved menu")
                if not action.startswith("retry"):
                    return self.recover(record, path, message_path)
                if request.get("delivery_requested") is not True or record.get("outcome") != "not_sent":
                    raise ValueError("explicit retry requires affirmative no-send evidence for this original attempt")
                recovered = self.recover(record, path, message_path)
                if not recovered.get("acknowledged"):
                    return recovered
                if not order and record.get("token"):
                    self.rpc("recipe_delivery", action="retry", job_id=target["request_id"], part_id=record["part_id"], token=record["token"])
                archive = path.with_suffix("." + hashlib.sha256(str(record.get("token") or record["executor_attempt"]).encode()).hexdigest()[:24] + ".json")
                if message_path.exists():
                    os.replace(message_path, archive.with_suffix(".eml"))
                os.replace(path, archive)
            if action.startswith("reconcile"):
                raise ValueError("no local original attempt; inspect the service journal instead of sending")
            if order:
                bound = self.rpc("email", action="sender_binding", provider=target["provider"], order_id=target["order_id"])
                binding = bound["binding"]
                transport = self.transport(binding)
                if not binding["unattended"] or not self.connections[binding["connection_id"]].get("unattended"):
                    raise ValueError("original sender is not available for unattended execution")
                claimed = self.rpc("email", action="due", provider=target["provider"], order_id=target["order_id"], scheduler=request.get("scheduler"))
                if claimed.get("claim") is not True:
                    return claimed
                record = {"target": target, "binding": binding, "token": claimed["claim_token"], "phase": "before_network"}
                _save(path, record)
                begun = self.rpc("email", action="begin_send", provider=target["provider"], order_id=target["order_id"],
                                 claim_token=record["token"], scheduler=request.get("scheduler"), managed_sender=True)
                if begun.get("dispatch") is not True:
                    return begun
                if begun["sender_binding"] != binding:
                    raise ValueError("order email connection changed before dispatch")
                def read_order(_operation, **arguments):
                    return self.rpc("email", action="read_message", provider=target["provider"], order_id=target["order_id"],
                                    claim_token=record["token"], offset=arguments["offset"])
                exported = export_part(read_order, None, None, message_path)
                record["warnings"] = begun.get("warnings", [])
            else:
                if request.get("delivery_requested") is not True:
                    raise ValueError("send requires an explicit request to deliver this saved menu")
                job = self.rpc("recipe_delivery", action="lookup", job_id=target["request_id"])["job"]
                binding = job.get("sender_binding") if job else self.rpc("recipe_delivery", action="sender")["binding"]
                transport = self.transport(binding)
                if not job:
                    job = self.rpc("recipe_delivery", action="request", request_id=target["request_id"], delivery_requested=True,
                                   channel="email", menu_ref=request.get("menu_ref"), capabilities={"email": binding["capabilities"]},
                                   destinations={"email": {k: binding[k] for k in ("sender", "recipient")}})
                elif request.get("menu_ref") is not None and request["menu_ref"] != job["menu_ref"]:
                    raise ValueError("request_id is bound to another saved menu")
                parts = [p for p in job["parts"] if p["channel"] == "email" and p["kind"] == "email"]
                if len(parts) != 1:
                    return {"sent": False, "warnings": job["warnings"], "next": "No sendable email part; inspect the original delivery."}
                part = parts[0]
                if part["status"] != "ready":
                    return {"sent": part["status"] == "accepted", "outcome": part["status"], "next": "Inspect/reconcile the original service attempt; no new send."}
                record = {"target": target, "binding": binding, "menu_ref": job["menu_ref"], "part_id": part["id"],
                          "phase": "before_network", "warnings": job["warnings"], "executor_attempt": secrets.token_hex(24)}
                _save(path, record)
                exported = export_part(self.rpc, job["id"], part["id"], message_path)
                begun = self.rpc("recipe_delivery", action="begin", job_id=job["id"], part_id=part["id"], executor_attempt=record["executor_attempt"])
                if begun.get("dispatch") is not True:
                    path.unlink()
                    message_path.unlink()
                    return begun
                record["token"] = begun["token"]
                if begun["sha256"] != exported["sha256"] or begun["destination"] != {k: binding[k] for k in ("sender", "recipient")}:
                    raise ValueError("frozen dispatch differs from exported email")
            raw = message_path.read_bytes()
            check_message(raw, binding, exported["sha256"])
            with message_path.open("rb") as handle:
                os.fsync(handle.fileno())
            record.update(sha256=exported["sha256"], phase="network_started", outcome="unknown")
            _save(path, record)
            try:
                self.record_result(record, transport.send(raw, binding))
            except Exception as exc:
                from email_transports import NotSent
                if isinstance(exc, NotSent):
                    record.update(outcome="not_sent", evidence="transport:rejected-before-submission")
                else:
                    record.update(outcome="unknown", next="Native sender result is uncertain. Reconcile this same request; do not resend.")
            _save(path, record)
            return self.finish(record, path)


def email_sender(rpc, **request):
    path = config_path()
    if not path.exists():
        return {"status": "not_configured", "sent": False,
                "next": "Connect an existing sender using the email setup guide; no new mailbox is required.",
                "setup": "https://github.com/poisdahl/meal-concierge/blob/main/docs/recipe-delivery.md#email-connection-setup"}
    try:
        return EmailSender(rpc, json.loads(path.read_text())).execute(request)
    except BlockingIOError:
        return {"status": "busy", "sent": False, "next": "This original email attempt is running; check it again later."}


def main():
    """One optional host setup command; it never enables email or sends."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--connection-id", default="email")
    parser.add_argument("--unattended", action="store_true", help="existing connection is available to this same host's scheduled jobs")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--gmail-credentials", type=Path, help="existing Google authorized-user file; never copied or changed")
    choice.add_argument("--smtp-host")
    choice.add_argument("--command", nargs="+", help="existing guarded JSON sender executable and arguments")
    parser.add_argument("--gmail-python", help="existing Google integration's Python with google-auth and google-api-python-client")
    parser.add_argument("--smtp-sender")
    parser.add_argument("--smtp-username")
    parser.add_argument("--smtp-password-env")
    parser.add_argument("--smtp-port", type=int, default=587)
    parser.add_argument("--smtp-tls", choices=["starttls", "implicit"], default="starttls")
    args = parser.parse_args()
    path = config_path()
    if path.exists():
        parser.error("email host configuration already exists; inspect and explicitly edit the intended connection")
    row = {"id": args.connection_id, "unattended": args.unattended}
    if args.gmail_credentials:
        if not args.gmail_python or not Path(args.gmail_python).is_absolute() or not args.gmail_credentials.is_file():
            parser.error("Gmail needs its existing credential file and absolute --gmail-python environment")
        row.update(type="command", command=[args.gmail_python, "-I", str(Path(__file__).with_name("email_transports.py")),
                                            "--gmail-credentials", str(args.gmail_credentials.resolve())])
    elif args.command:
        row.update(type="command", command=args.command)
    else:
        row.update(type="smtp", host=args.smtp_host, sender=args.smtp_sender, username=args.smtp_username,
                   password_env=args.smtp_password_env, port=args.smtp_port, tls=args.smtp_tls)
    receipts = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "meal-concierge" / args.runtime_id / "email-receipts"
    config = {"runtime_id": args.runtime_id, "receipt_dir": str(receipts), "connections": [row]}
    runner = EmailSender(lambda *a, **kw: {}, config)
    actual = runner.inspect(args.connection_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive publication: a concurrent setup must not overwrite another one.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(config, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps({"connected": True, "sender": actual["sender"], "sent": False,
                      "next": "Ask Meal Concierge to configure the recipient and timing; no mailbox or timer was created."}))


if __name__ == "__main__":
    main()
