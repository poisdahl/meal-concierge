#!/usr/bin/env python3
"""One JSON request on stdin -> one JSON result. Forward to an existing service.

This shell adapter has no native MCP approval UI. The caller must preserve the
shared skill's owner-authority rules. Never retry an uncertain mutation; use
the original attempt/ref/key to reconcile it. No service is started here.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpc_client import ServiceError, rpc


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('request exceeds the meal concierge size limit')
        request = json.loads(raw)
        if not isinstance(request, dict) or not isinstance(request.get('operation'), str):
            raise ValueError('request must be an object with a string operation')
        operation = request.pop('operation')
    except (ValueError, UnicodeError) as exc:
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
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
