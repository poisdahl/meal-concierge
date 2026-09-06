"""Configuration translation through the shipped command's actual stdin path."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


SCRIPT = Path(__file__).parents[1] / "clients/openclaw.py"


class OpenClawConfigurationTests(unittest.TestCase):
    def attachment(self):
        return {"command": "/private/Meal Concierge/current/venv/bin/python",
                "args": ["-I", "/private/Meal Concierge/current/mcp_server.py"],
                "env": {"MEAL_CONCIERGE_SOCKET": "/private/household/service.sock"},
                "skill": "/private/Meal Concierge/current/skill/SKILL.md"}

    def run_adapter(self, value):
        return subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(value),
                              capture_output=True, text=True, check=False)

    def test_attach_output_preserves_paths_and_has_no_service_or_approval_override(self):
        attachment = self.attachment()
        result = self.run_adapter(attachment)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        server = value["mcp"]["servers"]["meal-concierge"]
        for key in ("command", "args", "env"):
            self.assertEqual(server[key], attachment[key])
        self.assertNotIn("codex", server)
        self.assertEqual(set(value), {"mcp", "skills"})
        self.assertEqual(value["skills"]["load"]["extraDirs"],
                         ["/private/Meal Concierge/current/skill"])

    def test_credential_bearing_environment_is_not_projected(self):
        attachment = self.attachment()
        attachment["env"]["API_TOKEN"] = "synthetic-test-value"
        result = self.run_adapter(attachment)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("synthetic-test-value", result.stderr)

    def test_relative_socket_and_nonobject_input_fail(self):
        attachment = self.attachment()
        attachment["env"]["MEAL_CONCIERGE_SOCKET"] = "relative.sock"
        for value in (attachment, []):
            with self.subTest(value=value):
                result = self.run_adapter(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
