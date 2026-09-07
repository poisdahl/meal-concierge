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
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpc_client import ServiceError, rpc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-output", type=Path, help="exclusive local output file for an exact managed recipe image read")
    parser.add_argument("--delivery-output", type=Path, help="exclusive local output file for one frozen delivery attachment; transfer only, never sends")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('request exceeds the meal concierge size limit')
        request = json.loads(raw)
        if not isinstance(request, dict) or not isinstance(request.get('operation'), str):
            raise ValueError('request must be an object with a string operation')
        operation = request.pop('operation')
        delivery_read = operation == "recipe_delivery" and request.get("action") == "read"
        if args.delivery_output is not None:
            if not delivery_read or args.image_output is not None:
                raise ValueError("--delivery-output requires an exact recipe_delivery read and no image-output")
            from delivery_transport import export_part
            result = export_part(rpc, request.get("job_id"), request.get("part_id"), args.delivery_output)
            print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
            return 0
        image_read = operation == "recipes" and request.get("action") == "cover_get"
        if image_read != (args.image_output is not None):
            raise ValueError("cover_get requires --image-output; that option is only for exact managed image reads")
        if args.image_output is not None and args.image_output.exists():
            raise ValueError("image output already exists; choose a new local file")
    except (ValueError, UnicodeError, OSError, ServiceError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'dispatched': False}))
        return 2
    try:
        result = rpc(operation, **request)
    except ServiceError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 1
    except (OSError, RuntimeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'outcome': 'unknown',
                          'recovery': 'Do not retry a mutation. Reconcile the original attempt/ref/key.'}))
        return 1
    if args.image_output is not None:
        data = result.pop("image_base64", None)
        if data is not None:
            try:
                body = base64.b64decode(data, validate=True)
                if not body or len(body) > 1024 * 1024 or result.get("content_type") != "image/jpeg":
                    raise ValueError("invalid managed image response")
                descriptor = os.open(args.image_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(body)
                result["output_path"] = str(args.image_output.absolute())
            except (OSError, ValueError) as exc:
                print(json.dumps({"ok": False, "error": str(exc), "image_status": "local_output_failed"}))
                return 1
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
