"""Narrow filesystem boundary checks; native parsing is tested in NanoClaw."""
import importlib.util
import json
import os
from pathlib import Path
import socket
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
                self.assertEqual({p.name for p in (output / "template/bridge").iterdir()}, {"mcp_server.py", "rpc_client.py", "cli.py"})
                self.assertEqual((output / "template/bridge/cli.py").read_bytes(), (package.SOURCE / "cli.py").read_bytes())
                self.assertEqual((output / "template/skills/meal-concierge/SKILL.md").read_bytes(), (package.SOURCE / "skill/SKILL.md").read_bytes())
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


if __name__ == "__main__":
    unittest.main()
