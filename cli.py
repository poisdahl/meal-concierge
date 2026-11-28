#!/usr/bin/env python3
"""One JSON request on stdin -> one JSON result. Forward to an existing service.

This shell adapter has no native MCP approval UI. The caller must preserve the
shared skill's owner-authority rules. Never retry an uncertain mutation; use
the original attempt/ref/key to reconcile it. No service is started here.
"""
from __future__ import annotations

import json
import argparse
import base64
import hashlib
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpc_client import ServiceError, rpc, host_email_sender


MAX_INPUT = 2 * 1024 * 1024


def read_request(stream):
    raw = stream.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError('request exceeds the meal concierge size limit')
    request = json.loads(raw)
    if not isinstance(request, dict) or not isinstance(request.get('operation'), str):
        raise ValueError('request must be an object with a string operation')
    return request


def output_options(parser):
    parser.add_argument("--image-output", type=Path, help="exclusive local output file for an exact managed recipe image read")
    parser.add_argument("--delivery-output", type=Path, help="exclusive local output file for one frozen delivery attachment; transfer only, never sends")


def perform(request, send, *, image_output=None, delivery_output=None):
    arguments = dict(request)
    operation = arguments.pop('operation')
    try:
        delivery_read = operation == "recipe_delivery" and arguments.get("action") == "read"
        if delivery_output is not None:
            if not delivery_read or image_output is not None:
                raise ValueError("--delivery-output requires an exact recipe_delivery read and no image-output")
            from delivery_transport import export_part
            return {"ok": True, "result": export_part(send, arguments.get("job_id"), arguments.get("part_id"), delivery_output)}, 0
        image_read = operation == "recipes" and arguments.get("action") == "cover_get"
        if image_read != (image_output is not None):
            raise ValueError("cover_get requires --image-output; that option is only for exact managed image reads")
        if image_output is not None and image_output.exists():
            raise ValueError("image output already exists; choose a new local file")
    except (ValueError, UnicodeError, OSError, ServiceError) as exc:
        return {'ok': False, 'error': str(exc), 'dispatched': False}, 2
    try:
        if operation == "email_sender":
            result = host_email_sender(send, **arguments)
        else:
            result = send(operation, **arguments)
    except ServiceError as exc:
        return {'ok': False, 'error': str(exc), **exc.outcome_fields(),
                **({'provider_failure': exc.provider_failure} if exc.provider_failure else {})}, 1
    except (OSError, RuntimeError, ValueError) as exc:
        return {'ok': False, 'error': str(exc), 'outcome': 'unknown',
                'recovery': 'Do not retry a mutation. Reconcile the original attempt/ref/key.'}, 1
    if image_output is not None:
        data = result.pop("image_base64", None)
        if data is not None:
            try:
                body = base64.b64decode(data, validate=True)
                if not body or len(body) > 1024 * 1024 or result.get("content_type") != "image/jpeg":
                    raise ValueError("invalid managed image response")
                descriptor = os.open(image_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(body)
                result["output_path"] = str(image_output.absolute())
                result["sha256"] = hashlib.sha256(body).hexdigest()
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc), "image_status": "local_output_failed"}, 1
    return {'ok': True, 'result': result}, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    output_options(parser)
    args = parser.parse_args()
    try:
        request = read_request(sys.stdin.buffer)
    except (ValueError, UnicodeError, OSError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'dispatched': False}))
        return 2
    result, code = perform(request, rpc, image_output=args.image_output, delivery_output=args.delivery_output)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
