"""Explicit synthetic provider harness for the actual Grok cloud client.

The production Application, Unix server, MCP bridge and RetailMcpClient run
unchanged. Only external OAuth/HTTP/browser outcomes are replaced. No TCP
connection can leave this process. This file is never a production fallback.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import patch
import uuid


def runtime():
    import mcp
    import mcp.types
    assert sys.version_info[:3] == (3, 12, 12)
    assert sys.flags.isolated and sys.prefix != sys.base_prefix
    assert importlib.metadata.version('mcp') == '2.1.1'
    assert importlib.metadata.version('mcp-types') == '2.1.1'
    assert all(Path(module.__file__).is_relative_to(sys.prefix) for module in (mcp, mcp.types))


def serve(source: Path, root: Path, sock: Path):
    runtime()
    sys.path[:0] = [str(source), str(source / 'tests')]
    import httpx2
    from core import StateStore
    from service import Application, Server, config
    from retail_mcp import REQUIRED_TOOLS, RetailMcpClient
    from runtime_ownership import ownership
    from test_meal_concierge import FakeOda, FakeBrowser

    def no_network(event, args):
        if event in {'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.sendto'}:
            raise AssertionError('SYNTHETIC probe forbids DNS and datagram traffic')
        if event in {'socket.connect', 'socket.bind'}:
            assert args[0].family == socket.AF_UNIX, 'SYNTHETIC probe forbids network'

    sys.addaudithook(no_network)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_path = root / 'config.json'
    if not config_path.exists():
        config_path.write_text(json.dumps({
            'household': 'MC09 SYNTHETIC ' + uuid.uuid4().hex,
            'instance': 'mc09', 'provider': 'oda', 'confirmation_policy': 'fresh',
            'primary_recipe_library_id': 'builtin',
            'recipe_libraries': [{'library_id': 'builtin', 'provider': 'builtin', 'read_only': False}],
        }))
    settings = config(config_path)
    assert settings['household'].startswith('MC09 SYNTHETIC ')
    assert settings['provider'] == 'oda'
    (root / 'tokens').mkdir(mode=0o700, exist_ok=True)
    guard = threading.RLock()

    def record(name, value):
        with guard, (root / name).open('a') as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')

    class Shop(FakeOda):
        def __init__(self):
            super().__init__()
            self.path = root / 'synthetic-provider.json'
            if self.path.exists():
                saved = json.loads(self.path.read_text())
            else:
                saved = {'orders': [{
                    'order_number': key, 'grossAmount': 35.0,
                    'deliveryDate': '2026-09-12',
                    'deliverySlotDisplay': 'Saturday 2026-09-12 09:00 - 12:00',
                    'deliveryAddress': 'Synthetic test address',
                    'products': [{'product': {'id': 10, 'name': 'Fullkornspasta'},
                                  'quantity': 1, 'totalGrossAmount': '35.00'}],
                } for key in ('mc09-original-order', 'mc09-unrelated-order')],
                    'tracking': {'mc09-original-order': 'paid_and_modifiable',
                                 'mc09-unrelated-order': 'paid_and_modifiable'}, 'cancellations': []}
                self.path.write_text(json.dumps(saved))
            self.saved = saved
            self.orders = saved['orders']

        def call(self, tool, arguments, **kwargs):
            record('provider.jsonl', {'tool': tool, 'arguments': arguments})
            if tool == 'order_tracking':
                return {'order_id': arguments['order_number'],
                        'status': self.saved['tracking'][arguments['order_number']]}
            return super().call(tool, arguments, **kwargs)

    shop = Shop()

    class Browser(FakeBrowser):
        def submit_cancellation(self, order_id, order, review, before_click=None, *, deadline=None):
            record('browser.jsonl', {'action': 'cancel-attempt', 'order_id': order_id})
            assert order_id == 'mc09-original-order', 'synthetic unrelated order must be preserved'
            if before_click:
                before_click()
            with guard:
                shop.saved['cancellations'].append(order_id)
                shop.saved['tracking'][order_id] = 'cancelled'
                shop.path.write_text(json.dumps(shop.saved))
            record('browser.jsonl', {'action': 'cancel', 'order_id': order_id})
            if (root / 'lose-cancel-response').exists():
                raise RuntimeError('synthetic acknowledgement lost after dispatch')

    browser = Browser()
    browser.oda = shop
    real_http = httpx2.AsyncClient

    def respond(request):
        assert str(request.url) == 'https://oda.com/mcp'
        message = json.loads(request.content)
        method = message['method']
        mode = (root / 'fault').read_text().strip() if (root / 'fault').exists() else ''
        record('http.jsonl', {'method': method, 'mode': mode})
        if method == 'initialize':
            if mode == 'timeout':
                raise httpx2.ReadTimeout('synthetic provider timeout')
            result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'MC09 SYNTHETIC documented Oda contract', 'version': '1'}}
        elif method == 'notifications/initialized':
            return httpx2.Response(202)
        elif method == 'tools/list':
            names = sorted(REQUIRED_TOOLS - ({'get_orders'} if mode == 'missing-capability' else set()))
            result = {'tools': [{'name': name, 'inputSchema': {'type': 'object'}} for name in names]}
        elif method == 'tools/call':
            params = message['params']
            if mode == 'long-call':
                (root / 'long-call-started').write_text('synthetic provider entered')
                time.sleep(35)
            value = shop.call(params['name'], params.get('arguments', {}))
            if mode == 'partial' and params['name'] == 'get_cart':
                value = {'items': []}
            result = {'content': [], 'structuredContent': value}
            if mode == 'malformed':
                result.pop('structuredContent')
        else:
            raise AssertionError(method)
        return httpx2.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': result})

    def synthetic_http(**kwargs):
        return real_http(transport=httpx2.MockTransport(respond), **kwargs)

    class ObservedApplication(Application):
        def handle(self, request):
            record('application.jsonl', {'phase': 'request', 'request': request})
            result = super().handle(request)
            record('application.jsonl', {'phase': 'result', 'request': request, 'result': result})
            return result

    with ownership(root / 'state', root / 'browser/profile', root / 'browser', root / 'browser/run'):
        with patch('provider_oauth.build_auth', return_value=httpx2.Auth()), patch.object(httpx2, 'AsyncClient', synthetic_http):
            app = ObservedApplication(StateStore(root / 'state', settings),
                                      RetailMcpClient(root / 'tokens'), browser)
            record('lifecycle.jsonl', {'pid': os.getpid(), 'start': uuid.uuid4().hex})
            Server(sock, os.getgid(), os.getuid(), app).run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--socket', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    serve(args.source.resolve(), args.root.resolve(), args.socket.resolve())
