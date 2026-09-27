"""Narrow filesystem boundary checks; native parsing is tested in NanoClaw."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("nanoclaw_package", Path(__file__).resolve().parents[1] / "clients/nanoclaw.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def test_narrow_template_and_socket_directory(self):
        with tempfile.TemporaryDirectory(prefix="mc08-", dir=os.environ.get("MC08_SCRATCH", "/tmp")) as directory:
            root = Path(directory)
            python = root / "python"
            (python / "bin").mkdir(parents=True)
            (python / "bin/python3.12").touch()
            site = root / "site"
            (site / "mcp").mkdir(parents=True)
            sockets = root / "socket"
            sockets.mkdir()
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(sockets / "service.sock"))
                (sockets / "service.sock.owner.lock").touch()
                output = root / "package"
                result = package.build(output, python, site, sockets)
                self.assertEqual(len(result["additionalMounts"]), 3)
                self.assertTrue(all(m["readonly"] for m in result["additionalMounts"]))
                self.assertEqual({p.name for p in (output / "template/bridge").iterdir()}, {"mcp_server.py", "rpc_client.py", "cli.py", "agent_views.py", "delivery_transport.py"})
                self.assertEqual((output / "template/bridge/cli.py").read_bytes(), (package.SOURCE / "cli.py").read_bytes())
                self.assertEqual((output / "template/skills/meal-concierge/SKILL.md").read_bytes(), (package.SOURCE / "skill/SKILL.md").read_bytes())
                for source in (package.SOURCE / "skill/references").rglob("*.md"):
                    self.assertEqual((output / "template/skills/meal-concierge" /
                                      source.relative_to(package.SOURCE / "skill")).read_bytes(), source.read_bytes())
                server = json.loads((output / "template/mcp.json").read_text())["mcpServers"]["meal_concierge"]
                self.assertEqual(server["command"], "env")
                self.assertIn("${PLUGIN_ROOT}/bridge/mcp_server.py", server["args"])
                with self.assertRaisesRegex(ValueError, "new directory"):
                    package.build(output, python, site, sockets)
                (sockets / "state.json").write_text('{"private": true}')
                with self.assertRaisesRegex(ValueError, "only service.sock"):
                    package.build(root / "unsafe", python, site, sockets)
                self.assertFalse((root / "unsafe").exists())
                (sockets / "state.json").unlink()
                (sockets / "service.sock.owner.lock").unlink()
                (sockets / "service.sock.owner.lock").symlink_to(root / "private")
                with self.assertRaisesRegex(ValueError, "empty regular file"):
                    package.build(root / "unsafe", python, site, sockets)


    def test_built_bridge_exports_pdf_and_reports_managed_email_unavailable(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        body = b"%PDF-1.4\nSynthetic frozen recipe attachment\n%%EOF"
        requests = []

        class Reader(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                requests.append(request)
                if request['operation'] == 'health':
                    result = {'status': 'ok'}
                else:
                    result = {'offset': 0, 'next_offset': None, 'bytes': len(body),
                              'sha256': hashlib.sha256(body).hexdigest(), 'content_type': 'application/pdf',
                              'filename': 'recipes.pdf', 'data_base64': base64.b64encode(body).decode()}
                self.wfile.write((json.dumps({'ok': True, 'contract': 1, 'result': result}) + '\n').encode())

        with tempfile.TemporaryDirectory(prefix='mc08-', dir='/tmp') as directory:
            root = Path(directory)
            python = root / 'python'
            (python / 'bin').mkdir(parents=True)
            (python / 'bin/python3.12').touch()
            site = root / 'site'
            (site / 'mcp').mkdir(parents=True)
            sockets = root / 'socket'
            sockets.mkdir()
            with socketserver.UnixStreamServer(str(sockets / 'service.sock'), Reader) as server:
                package.build(root / 'package', python, site, sockets)
                bridge = root / 'package/template/bridge'
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    env = {**os.environ, 'MEAL_CONCIERGE_SOCKET': str(sockets / 'service.sock')}
                    output = root / 'recipes.pdf'
                    result = subprocess.run([sys.executable, '-I', '-B', str(bridge / 'cli.py'),
                                             '--delivery-output', str(output)],
                                            input=json.dumps({'operation': 'recipe_delivery', 'action': 'read',
                                                              'job_id': 'original', 'part_id': 'pdf'}),
                                            env=env, text=True, capture_output=True, timeout=15)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(body, output.read_bytes())
                    self.assertFalse(json.loads(result.stdout)['result']['sent'])
                    self.assertEqual(['health', 'recipe_delivery'], [r['operation'] for r in requests])
                    self.assertEqual('original', requests[-1]['job_id'])
                    self.assertEqual('pdf', requests[-1]['part_id'])

                    async def check_mcp():
                        params = StdioServerParameters(command=sys.executable,
                            args=['-I', '-B', str(bridge / 'mcp_server.py')], env=env)
                        async with stdio_client(params) as (read, write):
                            async with ClientSession(read, write, read_timeout_seconds=10) as session:
                                await session.initialize()
                                for action in ('status', 'send_order', 'reconcile_order'):
                                    result = await session.call_tool('meal_concierge_email_sender', {'action': action})
                                    self.assertFalse(result.is_error, result)
                                    self.assertEqual('unavailable', result.structured_content['status'])
                                    self.assertFalse(result.structured_content['dispatched'])
                                    self.assertNotIn('sent', result.structured_content)
                    asyncio.run(check_mcp())
                    result = subprocess.run([sys.executable, '-I', '-B', str(bridge / 'cli.py')],
                        input=json.dumps({'operation': 'email_sender', 'action': 'status'}),
                        env=env, text=True, capture_output=True, timeout=15)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual('unavailable', json.loads(result.stdout)['result']['status'])
                    self.assertEqual(2, len(requests), 'unsupported email must not dispatch to the service')
                finally:
                    server.shutdown()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
