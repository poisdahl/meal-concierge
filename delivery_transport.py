"""Transfer exact frozen delivery bytes over the ordinary private RPC socket.

Local native hosts use export_part then their actual attachment/sender tool.
Exporting bytes is not sending a chat message or email and creates no receipt.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path


def export_part(rpc, job_id, part_id, output):
    output = Path(output)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            offset, expected, checksum = 0, None, hashlib.sha256()
            while True:
                result = rpc("recipe_delivery", action="read", job_id=job_id, part_id=part_id, offset=offset)
                metadata = (result.get("bytes"), result.get("sha256"), result.get("content_type"), result.get("filename"))
                if expected is None:
                    expected = metadata
                if metadata != expected or result.get("offset") != offset:
                    raise ValueError("frozen attachment changed during transfer")
                data = base64.b64decode(result["data_base64"], validate=True)
                if not data or len(data) > 128 * 1024 or offset + len(data) > expected[0]:
                    raise ValueError("invalid frozen attachment chunk")
                stream.write(data)
                checksum.update(data)
                offset += len(data)
                if result["next_offset"] is None:
                    break
                if result["next_offset"] != offset:
                    raise ValueError("invalid next frozen attachment offset")
            if offset != expected[0] or checksum.hexdigest() != expected[1]:
                raise ValueError("frozen attachment checksum does not match")
        return {"output_path": str(output.absolute()), "bytes": offset, "sha256": expected[1],
                "content_type": expected[2], "filename": expected[3], "sent": False}
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def send_smtp(message_bytes, smtp):
    """Concrete SMTP transfer for an already connected/authenticated native sender.

    The caller must begin its original email part first, and reconcile any
    exception as unknown unless the server proves rejection before acceptance.
    No login, recipient lookup, URL fetch, rerouting or retries happen here.
    """
    from email import policy
    from email.parser import BytesParser
    message = BytesParser(policy=policy.default).parsebytes(message_bytes)
    sender, recipient = str(message["From"]), str(message["To"])
    from core import valid_email_address
    if not valid_email_address(sender) or not valid_email_address(recipient):
        raise ValueError("frozen email must have exact sender and recipient")
    refused = smtp.sendmail(sender, [recipient], message_bytes)
    if refused:
        raise RuntimeError("SMTP recipient was refused; retain the server response for reconciliation")
    return {"outcome": "accepted", "recipient_read": "unknown",
            "evidence": "smtp:DATA-accepted:sha256:" + hashlib.sha256(message_bytes).hexdigest()}
