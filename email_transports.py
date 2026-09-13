"""Email transports using connections owned by the host, never by the model.

No login wizard, credential store, automatic network retries or arbitrary URL
resolver. Gmail's service and granted scopes are supplied by its native helper.
"""
from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import valid_email_address

LIMIT = 20 * 1024 * 1024


class NotSent(RuntimeError):
    """Affirmative failure before submission, never a timeout after submission."""


def check_message(raw, binding, expected_sha=None):
    if not isinstance(raw, bytes) or not raw or len(raw) > LIMIT:
        raise ValueError("email must be nonempty and at most 20 MiB")
    if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("frozen email checksum changed")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if any(part.defects for part in message.walk()):
        raise ValueError("malformed email")
    for key, field in (("From", "sender"), ("To", "recipient")):
        headers = message.get_all(key, [])
        if len(headers) != 1 or str(headers[0]) != binding[field] or not valid_email_address(str(headers[0])):
            raise ValueError("email sender/recipient differs from the selected connection")
    if any(key.lower() in {"cc", "bcc", "sender", "reply-to", "return-path"} or key.lower().startswith("resent-")
           for key in message.keys()):
        raise ValueError("additional email routing headers are not permitted")
    ids = message.get_all("Message-ID", [])
    if len(ids) != 1 or not re.fullmatch(r"<[A-Za-z0-9_.-]+@meal-concierge\.local>", str(ids[0])):
        raise ValueError("email needs its frozen meal-concierge Message-ID")
    return message


def capability(sender, transport, evidence, *, reconciliation=False):
    if not valid_email_address(sender):
        raise ValueError("connection did not establish a valid sender")
    return {"sender": sender, "account": sender, "verified": True, "transport": transport,
            "evidence": evidence, "message_limit": LIMIT, "attachment_limit": 12 * 1024 * 1024,
            "pdf": True, "images": True, "reconciliation": reconciliation}


class GmailTransport:
    """Reusable Gmail implementation; the native integration owns OAuth/guarding."""
    def __init__(self, service, granted_scopes):
        self.service = service
        self.scopes = set(granted_scopes or [])

    def inspect(self):
        prefix = "https://www.googleapis.com/auth/gmail."
        if not self.scopes.intersection({prefix + "send", prefix + "modify", "https://mail.google.com/"}):
            raise ValueError("existing Gmail grant does not establish send permission; connect Gmail with send access")
        sender = self.service.users().getProfile(userId="me").execute(num_retries=0)["emailAddress"]
        can_read = bool(self.scopes.intersection({prefix + "readonly", prefix + "modify", "https://mail.google.com/"}))
        return capability(sender, "gmail", "gmail:profile-and-recorded-grant", reconciliation=can_read)

    def send(self, raw, binding):
        try:
            check_message(raw, binding)
            if self.inspect()["account"] != binding["account"]:
                raise ValueError("Gmail account changed; select the intended account explicitly")
        except Exception as exc:
            raise NotSent("Gmail pre-submission check failed") from exc
        try:
            result = self.service.users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(raw).decode()}).execute(num_retries=0)
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) in {400, 401, 403, 404, 413, 429}:
                raise NotSent("Gmail explicitly rejected the request") from exc
            raise
        message_id = result.get("id")
        if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id):
            raise RuntimeError("Gmail returned no usable receipt; reconcile this original attempt")
        return {"outcome": "accepted", "evidence": "gmail:message:" + message_id}

    def reconcile(self, raw, binding):
        message = check_message(raw, binding)
        actual = self.inspect()
        if actual["account"] != binding["account"]:
            raise ValueError("original Gmail account is unavailable; no account fallback")
        if actual["reconciliation"]:
            result = self.service.users().messages().list(userId="me", q="in:sent rfc822msgid:" + str(message["Message-ID"]), maxResults=10).execute(num_retries=0)
            for item in result.get("messages", []):
                found = self.service.users().messages().get(userId="me", id=item["id"], format="raw").execute(num_retries=0)
                candidate = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(found["raw"]))
                # Gmail may add/rewrite transport headers. Compare the actual
                # selected addresses and every decoded MIME leaf, not wire bytes.
                leaves = lambda m: [(p.get_content_type(), p.get_filename(), p.get_payload(decode=True)) for p in m.walk() if not p.is_multipart()]
                if ("SENT" in found.get("labelIds", []) and
                    all(candidate.get_all(k) == message.get_all(k) for k in ("From", "To", "Cc", "Bcc", "Subject", "Message-ID")) and
                    leaves(candidate) == leaves(message)):
                    return {"outcome": "accepted", "evidence": "gmail:message:" + item["id"]}
        return {"outcome": "unknown", "next": "No positive sent-mail evidence. Do not resend; inspect the original account."}


class CommandTransport:
    """Fixed host-configured command, JSON on stdin/stdout; never a shell string."""
    def __init__(self, config):
        command = config.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command) or not os.path.isabs(command[0]):
            raise ValueError("email command must be a configured absolute executable and argument list")
        self.command = command

    def call(self, action, **request):
        completed = subprocess.run(self.command, input=json.dumps({"action": action, **request}).encode(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=False)
        if completed.returncode or len(completed.stdout) > 16384:
            # Never expose command stderr: it may contain credential/provider data.
            raise RuntimeError("native email helper failed; check its connection and authorization")
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError("invalid native email helper response")
        return result

    def inspect(self):
        return self.call("inspect")

    def preflight(self, binding):
        result = self.call("check", binding=binding)
        if result.get("ready") is not True:
            raise NotSent("native email permission check failed for the selected recipient")

    def send(self, raw, binding):
        check_message(raw, binding)
        return self.call("send", binding=binding, message_base64=base64.b64encode(raw).decode())

    def reconcile(self, raw, binding):
        return self.call("reconcile", binding=binding, message_base64=base64.b64encode(raw).decode())


class SMTPTransport:
    """Explicit existing SMTP connection. Secret values are resolved from host env."""
    def __init__(self, config):
        self.config = config

    def account(self):
        endpoint = {k: self.config.get(k) for k in ("host", "port", "tls", "username", "sender")}
        return "smtp:" + hashlib.sha256(json.dumps(endpoint, sort_keys=True).encode()).hexdigest()

    def connect(self):
        c = self.config
        host, tls = c["host"], c.get("tls", "starttls")
        if tls not in {"starttls", "implicit", "none"} or (tls == "none" and host not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("SMTP requires TLS except for an explicit loopback test sink")
        factory = smtplib.SMTP_SSL if tls == "implicit" else smtplib.SMTP
        kwargs = {"context": ssl.create_default_context()} if tls == "implicit" else {}
        smtp = factory(host, int(c.get("port", 465 if tls == "implicit" else 587)), timeout=30, **kwargs)
        try:
            smtp.ehlo()
            if tls == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if c.get("username"):
                password = os.environ.get(c.get("password_env", ""))
                if not password:
                    raise ValueError("existing SMTP password environment is unavailable in this runtime")
                smtp.login(c["username"], password)
            elif tls != "none":
                raise ValueError("configure the existing SMTP username and password_env")
            return smtp
        except BaseException:
            smtp.close()
            raise

    def inspect(self):
        smtp = self.connect()
        size = smtp.esmtp_features.get("size", "")
        smtp.close()
        result = capability(self.config["sender"], "smtp", "smtp:connection-and-explicit-sender")
        result["account"] = self.account()
        if size.isdigit() and int(size) > 0:
            result["message_limit"] = min(LIMIT, int(size))
            result["attachment_limit"] = min(result["attachment_limit"], int(size) * 3 // 4)
        return result

    def send(self, raw, binding):
        try:
            check_message(raw, binding)
            if binding["sender"] != self.config["sender"] or binding["account"] != self.account():
                raise ValueError("configured SMTP sender changed")
            smtp = self.connect()
        except Exception as exc:
            raise NotSent("SMTP pre-submission check failed") from exc
        try:
            refused = smtp.sendmail(binding["sender"], [binding["recipient"]], raw)
            if refused:
                raise RuntimeError("SMTP refused recipient")
            return {"outcome": "accepted", "evidence": "smtp:DATA-accepted:sha256:" + hashlib.sha256(raw).hexdigest()}
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            raise NotSent("SMTP explicitly rejected the email") from exc
        finally:
            # A failing QUIT must not erase a positive DATA acceptance.
            smtp.close()

    def reconcile(self, raw, binding):
        check_message(raw, binding)
        return {"outcome": "unknown", "next": "SMTP has no sent-mail lookup. Check the original delivery with the mail provider; do not resend."}


def gmail_helper():
    """Optional adapter for an existing authorized-user Gmail grant.

    Run in the existing Google integration's Python environment. Never creates
    or rewrites credentials. Installations with their own write guard (Bob, for
    example) must use that guarded helper instead of this standalone adapter.
    """
    import argparse
    from pathlib import Path
    import sys
    parser = argparse.ArgumentParser(description=gmail_helper.__doc__)
    parser.add_argument("--gmail-credentials", required=True, type=Path)
    args = parser.parse_args()
    request = json.loads(sys.stdin.buffer.read(29 * 1024 * 1024))
    action = request.get("action")
    if action not in {"inspect", "check", "send", "reconcile"}:
        raise ValueError("unknown Gmail action")
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        saved = json.loads(args.gmail_credentials.read_text())
        scopes = saved.get("scopes", [])
        # Only Google's actual token endpoint; do not trust an arbitrary endpoint
        # embedded in a copied authorized-user file.
        credentials = Credentials(token=saved.get("token"), refresh_token=saved.get("refresh_token"),
                                  token_uri="https://oauth2.googleapis.com/token", client_id=saved.get("client_id"),
                                  client_secret=saved.get("client_secret"), scopes=scopes)
        if credentials.refresh_token:
            credentials.refresh(Request())
            scopes = credentials.granted_scopes or scopes
        transport = GmailTransport(build("gmail", "v1", credentials=credentials), scopes)
        actual = transport.inspect()
        if action == "inspect":
            print(json.dumps(actual))
            return
        binding = request["binding"]
        if any(actual[k] != binding[k] for k in ("account", "sender")):
            raise ValueError("original account unavailable")
        if action == "check":
            if not valid_email_address(binding["recipient"]):
                raise ValueError("invalid recipient")
            print(json.dumps({"ready": True}))
            return
        raw = base64.b64decode(request["message_base64"], validate=True)
        check_message(raw, binding)
    except Exception:
        if action in {"send", "check"}:
            print(json.dumps({"ready": False, "outcome": "not_sent", "evidence": "gmail:pre-submission-check-rejected"}))
            return
        raise RuntimeError("Existing Gmail login/send permission is unavailable; check the owning integration") from None
    try:
        result = transport.send(raw, binding) if action == "send" else transport.reconcile(raw, binding)
    except NotSent:
        result = {"outcome": "not_sent", "evidence": "gmail:submission-rejected"}
    print(json.dumps(result))


if __name__ == "__main__":
    gmail_helper()
