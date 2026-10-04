"""Real foreground core commands and synthetic native-host recovery."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import termios
import tempfile
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[1]
PRODUCT = "/varer/frokost/havregryn-1234567890123"


class DotsSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mc-dots-session-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "core"
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        self.binding = {"origin": "https://meny.no", "browser_id": "synthetic-cloud-browser",
                        "tab_id": "synthetic-owned-tab", "account_sha256": "a" * 64,
                        "cart_context_sha256": "b" * 64}
        self.config = {"household": "Synthetic native household", "browser_binding": self.binding,
                       "allow_cart_writes": True}
        self.quantity = 0
        self.writes = 0
        self.frames = []
        code, result = self.run_session("init", self.config)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["result"]["setup"]["configuration_required"])
        self.assertEqual(self.frames, [])  # Initialization never contacts a provider.
        code, result = self.call({"operation": "setup", "action": "apply", "keep_current": True})
        self.assertEqual(code, 0, result)

    def snapshot(self):
        items = ([{"product_id": PRODUCT, "name": "Synthetic oats", "quantity": self.quantity,
                   "price": 20.0}] if self.quantity else [])
        return {"authenticated": True, "ready": True, "root_count": 1, "items": items,
                "item_root_count": len(items), "control_count": len(items), "empty": not items,
                "total_count": int(bool(items)), "subtotal_count": int(bool(items)),
                "subtotal": self.quantity * 20.0 if items else None, "count": self.quantity,
                "total": self.quantity * 20.0, "delivery_count": 0, "delivery": None}

    def state(self):
        return json.loads((self.root / "state/state.json").read_bytes())

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def host(self, frame):
        self.assertEqual(frame["browser_binding"], self.binding)
        operation = frame["operation"]
        if operation == "verify_new_cart":
            result = {"authenticated": True, "new_cart": True}
        elif operation == "get_cart":
            result = self.snapshot()
        elif operation == "manipulate_cart":
            pending = self.state()["pending_cart_change"]  # Must precede stdout issuance.
            self.assertEqual(pending["before"], frame["before_quantities"])
            self.assertEqual(pending["expected"], frame["expected_quantities"])
            self.assertEqual(pending["operations"], frame["arguments"]["operations"])
            self.assertEqual(frame["effect"], "cart_write")
            for item in frame["arguments"]["operations"]:
                self.assertEqual(item["productId"], PRODUCT)
                self.quantity += item["quantity"]
            self.writes += 1
            result = {"dispatched": True}
        else:
            self.fail(f"unexpected provider effect {operation}")
        return {"reply_to": frame["call_id"], "browser_binding": self.binding,
                "observed_at": datetime.now(timezone.utc).isoformat(), "result": result}

    def run_session(self, action, value, host=None, environment=None):
        child = subprocess.Popen([sys.executable, "-I", "-B", str(SOURCE / "clients/dots_session.py"),
                                 action, "--root", str(self.root)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env={**os.environ, "HOME": str(self.home), **(environment or {})})
        try:
            self.assertTrue(select.select([child.stdout], [], [], 45)[0], "core readiness timeout")
            self.assertEqual(json.loads(child.stdout.readline()), {"kind": "core_ready", "input_max_bytes": 65536})
            child.stdin.write(json.dumps(value).encode() + b"\n")
            child.stdin.flush()
            while True:
                self.assertTrue(select.select([child.stdout], [], [], 45)[0], "core stdout timeout")
                line = child.stdout.readline()
                self.assertTrue(line, "core exited without a terminal result")
                frame = json.loads(line)
                if frame["kind"] == "core_result":
                    result = frame
                    break
                self.assertEqual(frame["kind"], "native_host_request")
                self.frames.append(frame)
                reply = (host or self.host)(frame)
                if reply is None:
                    child.stdin.close()  # Lost host reply, not a definite pre-click stop.
                else:
                    child.stdin.write(json.dumps(reply).encode() + b"\n")
                    child.stdin.flush()
            child.wait(timeout=10)
            self.assertEqual(child.stderr.read(), b"")
            return child.returncode, result
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr):
                if not stream.closed:
                    stream.close()

    def call(self, request, command_id=None, host=None):
        return self.run_session("call", {"request_id": command_id or str(uuid.uuid4()), "request": request}, host)

    def ensure(self, quantity=1):
        return {"operation": "cart", "action": "ensure",
                "requirements": [{"product_id": PRODUCT, "product_name": "Synthetic oats", "quantity": quantity}]}

    def test_core_recipe_menu_and_command_recovery_without_provider(self):
        recipe = {"name": "Synthetic oats", "portions": 2,
                  "ingredients": [{"raw": "100 g havregryn", "item": "havregryn", "quantity": 100,
                                   "unit": "g", "scalable": True}], "steps": ["Cook the oats."],
                  "source": {"kind": "user", "publisher": "Synthetic kitchen", "relationship": "user_supplied"},
                  "rights": {"storage": "full", "credit": "Synthetic test"}}
        command_id = str(uuid.uuid4())
        request = {"operation": "recipes", "action": "save", "recipe": recipe,
                   "idempotency_key": "synthetic-native-oats"}
        code, saved = self.call(request, command_id)
        self.assertEqual(code, 0, saved)
        before = self.files()
        code, cached = self.call(request, command_id)
        self.assertEqual(code, 0, cached)
        self.assertEqual(saved, cached)
        self.assertEqual(before, self.files())
        ref = saved["result"]["recipe"]
        menu = {"week": "2026-W41", "dishes": [{"recipe_ref": {"id": ref["id"], "revision": ref["revision"]},
                                                       "portions": 6}],
                "schedule": [{"day": "2026-10-06", "meal": recipe["name"], "portions": 6}]}
        code, result = self.call({"operation": "menu", "action": "save", "menu": menu})
        self.assertEqual(code, 0, result)
        code, result = self.call({"operation": "menu", "action": "get"})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["menu"]["dishes"][0]["ingredients"][0]["quantity"], 300)
        self.assertEqual(self.frames, [])

    def test_lost_write_reply_survives_restart_and_only_reconciles_original(self):
        def lose_write(frame):
            reply = self.host(frame)
            return None if frame["operation"] == "manipulate_cart" else reply
        command_id = str(uuid.uuid4())
        code, result = self.call(self.ensure(), command_id, lose_write)
        self.assertEqual(code, 1, result)
        self.assertEqual(self.quantity, 1)
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.state()["pending_cart_change"]["expected"], {PRODUCT: 1})
        before = self.files()
        count = len(self.frames)
        code, cached = self.call(self.ensure(), command_id)
        self.assertEqual(code, 1, cached)
        self.assertEqual(result, cached)
        self.assertEqual(before, self.files())
        self.assertEqual(len(self.frames), count)
        code, blocked = self.call(self.ensure(2))
        self.assertEqual(code, 1, blocked)
        self.assertEqual(self.writes, 1)
        code, recovered = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 0, recovered)
        self.assertTrue(recovered["result"]["reconciled"])
        self.assertNotIn("pending_cart_change", self.state())
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.quantity, 1)

    def test_multibatch_ensure_and_binding_drift_never_clear_pending(self):
        code, result = self.call(self.ensure(3))
        self.assertEqual(code, 0, result)
        self.assertEqual(self.quantity, 3)
        self.assertEqual(self.writes, 2)  # Existing core two-click batches, then one.
        def lose_write(frame):
            reply = self.host(frame)
            return None if frame["operation"] == "manipulate_cart" else reply
        code, result = self.call(self.ensure(4), host=lose_write)
        self.assertEqual(code, 1, result)
        pending = self.state()["pending_cart_change"]
        def wrong_identity(frame):
            reply = self.host(frame)
            reply["browser_binding"] = {**self.binding, "account_sha256": "c" * 64}
            return reply
        code, result = self.call({"operation": "cart", "action": "reconcile_change"}, host=wrong_identity)
        self.assertEqual(code, 1, result)
        self.assertEqual(self.state()["pending_cart_change"], pending)
        self.assertEqual(self.writes, 3)
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({**self.config, "browser_binding": {**self.binding, "account_sha256": "c" * 64}}))
        before = self.files()
        code, result = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 1, result)
        self.assertEqual(before, self.files())
        self.assertEqual(self.writes, 3)

    def test_missing_originals_conflicts_and_partial_command_never_initialize_or_replay(self):
        code, result = self.call({"operation": "checkout", "action": "confirm"})
        self.assertEqual(code, 1, result)
        command_id = str(uuid.uuid4())
        record = self.root / "calls" / command_id
        record.mkdir(mode=0o700)
        value = {"request_id": command_id, "request": {"operation": "health"}}
        import hashlib
        intent = {"input": value, "config_sha256": hashlib.sha256((self.root / "config.json").read_bytes()).hexdigest()}
        (record / "intent.json").write_text(json.dumps(intent, sort_keys=True, ensure_ascii=False))
        before = self.files()
        code, result = self.call(value["request"], command_id)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(before, self.files())
        code, result = self.call({"operation": "profile"}, command_id)
        self.assertEqual(code, 1, result)
        self.assertEqual(before, self.files())
        state = self.root / "state/state.json"
        state.rename(self.root / "retained-state.json")
        before = self.files()
        code, result = self.call({"operation": "health"})
        self.assertEqual(code, 1, result)
        self.assertFalse(state.exists())
        self.assertEqual(before, self.files())

    def test_native_target_cannot_be_adopted_by_a_second_household(self):
        def lose_write(frame):
            reply = self.host(frame)
            return None if frame["operation"] == "manipulate_cart" else reply
        code, result = self.call(self.ensure(), host=lose_write)
        self.assertEqual(code, 1, result)
        original = self.root
        before = self.files()
        frame_count = len(self.frames)
        self.root = original.parent / "second-core"
        code, result = self.run_session("init", self.config)
        self.assertEqual(code, 1, result)
        self.assertIn("another original core household", result["error"])
        self.assertFalse((self.root / "state/state.json").exists())
        self.assertEqual(len(self.frames), frame_count)
        self.root = original
        self.assertEqual(before, self.files())
        self.assertEqual(self.writes, 1)
        code, result = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 0, result)
        self.assertEqual(self.writes, 1)

    def test_shared_target_lock_stops_native_calls(self):
        registry = self.home / ".meal-concierge-dots-targets"
        lock = sorted(registry.glob("*.lock"))[0]
        with lock.open("r+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, result = self.call(self.ensure())
            self.assertEqual(code, 1, result)
            self.assertIn("target already owned", result["error"])
            self.assertNotIn("pending_cart_change", self.state())
            self.assertEqual(self.frames, [])
        code, result = self.call(self.ensure())
        self.assertEqual(code, 0, result)
        self.assertEqual(self.writes, 1)

    def test_explicit_shared_registry_survives_calls_and_refuses_adoption_or_replacement(self):
        # A separate synthetic host has no legacy registry; its HOME is unchanged
        # across every call, while the shared state scope is outside both cores.
        self.home = self.root.parent / "shared-host-home"
        self.home.mkdir(mode=0o700)
        self.root = self.root.parent / "shared-core"
        shared = self.root.parent / "shared-state"
        shared.mkdir(mode=0o700)
        registry = shared / ".meal-concierge-dots-targets"
        config = {**self.config, "allow_cart_writes": False, "target_registry": str(registry)}
        code, result = self.run_session("init", config)
        self.assertEqual(code, 0, result)
        owner_files = {p.name: p.read_bytes() for p in registry.glob("*.json")}
        self.assertEqual(len(owner_files), 2)
        original_config = (self.root / "config.json").read_bytes()
        code, result = self.call({"operation": "cart", "action": "get"})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["items"], [])
        self.assertEqual(self.writes, 0)
        self.assertFalse((self.home / ".meal-concierge-dots-targets").exists())
        self.assertEqual((self.root / "config.json").read_bytes(), original_config)
        self.assertEqual(owner_files, {p.name: p.read_bytes() for p in registry.glob("*.json")})
        code, result = self.run_session("call", {
            "request_id": str(uuid.uuid4()), "request": {"operation": "cart", "action": "get"}},
            environment={"XDG_STATE_HOME": str(shared / "unrelated-state-scope")})
        self.assertEqual(code, 0, result)
        self.assertFalse((shared / "unrelated-state-scope").exists())
        self.assertEqual(owner_files, {p.name: p.read_bytes() for p in registry.glob("*.json")})
        original_root = self.root
        self.root = original_root.parent / "second-shared-core"
        code, result = self.run_session("init", config)
        self.assertEqual(code, 1, result)
        self.assertIn("another original core household", result["error"])
        self.assertFalse((self.root / "state").exists())
        self.root = original_root
        frame_count = len(self.frames)
        lock = sorted(registry.glob("*.lock"))[0]
        with lock.open("r+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, result = self.call({"operation": "cart", "action": "get"})
            self.assertEqual(code, 1, result)
        self.assertEqual(len(self.frames), frame_count)
        retained = shared / "retained-original-registry"
        registry.rename(retained)
        code, result = self.call({"operation": "cart", "action": "get"})
        self.assertEqual(code, 1, result)
        self.assertFalse(registry.exists())
        self.assertEqual(len(self.frames), frame_count)
        self.assertEqual(owner_files, {p.name: p.read_bytes() for p in retained.glob("*.json")})

    def test_registry_selection_rejects_legacy_bypass_and_invalid_scopes_before_init(self):
        original_root = self.root
        original_files = self.files()
        shared = original_root.parent / "registry-validation"
        shared.mkdir(mode=0o700)
        invalid = ["relative/.meal-concierge-dots-targets", str(shared / "different-name"),
                   str(shared / ".meal-concierge-dots-targets")]
        for index, path in enumerate(invalid):
            self.root = original_root.parent / ("invalid-core-" + str(index))
            code, result = self.run_session("init", {**self.config, "target_registry": path})
            self.assertEqual(code, 1, result)
            self.assertFalse(self.root.exists())
        # An existing private core directory cannot serve as its own registry scope.
        self.root = original_root
        code, result = self.run_session("init", {
            **self.config, "target_registry": str(self.root / ".meal-concierge-dots-targets")})
        self.assertEqual(code, 1, result)
        self.assertIn("outside the core root", result["error"])
        self.assertEqual(self.files(), original_files)
        self.assertFalse((shared / ".meal-concierge-dots-targets").exists())

    def test_disabled_writes_do_not_create_an_uncertain_journal(self):
        self.root = self.root.parent / "read-only-core"
        config = {"household": "Synthetic read-only household"}
        code, result = self.run_session("init", config)
        self.assertEqual(code, 0, result)
        before = self.files()
        code, result = self.call(self.ensure())
        self.assertEqual(code, 1, result)
        self.assertIn("disabled", result["error"])
        self.assertEqual(before, self.files())
        self.assertNotIn("pending_cart_change", self.state())
        self.assertEqual(self.frames, [])

    def test_host_attestations_require_json_booleans(self):
        def numeric_auth(frame):
            reply = self.host(frame)
            if frame["operation"] == "verify_new_cart":
                reply["result"]["authenticated"] = 1
            return reply
        code, result = self.call(self.ensure(), host=numeric_auth)
        self.assertEqual(code, 1, result)
        self.assertEqual(self.writes, 0)
        self.assertNotIn("pending_cart_change", self.state())
        def numeric_dispatch(frame):
            reply = self.host(frame)
            if frame["operation"] == "manipulate_cart":
                reply["result"]["dispatched"] = 1
            return reply
        code, result = self.call(self.ensure(), host=numeric_dispatch)
        self.assertEqual(code, 1, result)
        self.assertEqual(self.writes, 1)
        self.assertIn("pending_cart_change", self.state())
        code, result = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 0, result)
        self.assertEqual(self.writes, 1)

    def test_read_first_policy_enables_original_household_without_rebinding(self):
        self.root = self.root.parent / "read-first-core"
        self.binding = {**self.binding, "tab_id": "synthetic-read-first-tab",
                        "account_sha256": "c" * 64, "cart_context_sha256": "d" * 64}
        config = {**self.config, "browser_binding": self.binding, "allow_cart_writes": False}
        code, result = self.run_session("init", config)
        self.assertEqual(code, 0, result)
        code, result = self.call({"operation": "setup", "action": "apply", "keep_current": True})
        self.assertEqual(code, 0, result)
        original_config = (self.root / "config.json").read_bytes()
        registry = self.home / ".meal-concierge-dots-targets"
        original_owners = {p.name: p.read_bytes() for p in registry.glob("*.json")}
        original_digest = self.state()["native_config_sha256"]
        code, result = self.call({"operation": "cart", "action": "get"})
        self.assertEqual(code, 0, result)
        before = self.files()
        code, result = self.call(self.ensure())
        self.assertEqual(code, 1, result)
        self.assertEqual(before, self.files())
        frames = list(self.frames)
        policy = {"operation": "native_cart_policy", "action": "set", "enabled": True,
                  "browser_binding": self.binding}
        command_id = str(uuid.uuid4())
        code, granted = self.call(policy, command_id)
        self.assertEqual(code, 0, granted)
        self.assertEqual(granted["result"], {"enabled": True})
        self.assertEqual(frames, self.frames)
        self.assertEqual((self.root / "config.json").read_bytes(), original_config)
        self.assertEqual({p.name: p.read_bytes() for p in registry.glob("*.json")}, original_owners)
        self.assertEqual(self.state()["native_config_sha256"], original_digest)
        code, result = self.call(self.ensure())
        self.assertEqual(code, 0, result)
        self.assertEqual(self.writes, 1)
        before = self.files()
        code, cached = self.call(policy, command_id)
        self.assertEqual(code, 0, cached)
        self.assertEqual(granted, cached)
        self.assertEqual(before, self.files())

    def test_disabled_policy_preserves_pending_reconciliation_and_cached_ending(self):
        def lose_write(frame):
            reply = self.host(frame)
            return None if frame["operation"] == "manipulate_cart" else reply
        command_id = str(uuid.uuid4())
        code, uncertain = self.call(self.ensure(), command_id, lose_write)
        self.assertEqual(code, 1, uncertain)
        pending = self.state()["pending_cart_change"]
        policy = {"operation": "native_cart_policy", "action": "set", "enabled": False,
                  "browser_binding": self.binding}
        code, result = self.call(policy)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.state()["pending_cart_change"], pending)
        code, result = self.call({**policy, "enabled": True})
        self.assertEqual(code, 1, result)
        self.assertIn("reconcile", result["error"])
        self.assertFalse(self.state()["native_cart_writes_enabled"])
        before = self.files()
        frames = list(self.frames)
        code, cached = self.call(self.ensure(), command_id)
        self.assertEqual(code, 1, cached)
        self.assertEqual(cached, uncertain)
        self.assertEqual(before, self.files())
        code, result = self.call(self.ensure(2))
        self.assertEqual(code, 1, result)
        self.assertEqual(before, self.files())
        self.assertEqual(self.frames, frames)
        code, result = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 0, result)
        self.assertNotIn("pending_cart_change", self.state())
        self.assertEqual(self.writes, 1)
        code, result = self.call({**policy, "enabled": True})
        self.assertEqual(code, 0, result)
        code, result = self.call({"operation": "native_cart_policy", "action": "show"})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"], {"enabled": True})

    def test_policy_rejects_wrong_binding_and_non_boolean_without_intent(self):
        policy = {"operation": "native_cart_policy", "action": "set", "enabled": False,
                  "browser_binding": self.binding}
        before = self.files()
        for request in ({**policy, "enabled": 1},
                        {**policy, "browser_binding": {**self.binding, "account_sha256": "c" * 64}}):
            code, result = self.call(request)
            self.assertEqual(code, 1, result)
            self.assertEqual(before, self.files())
        self.assertEqual(self.frames, [])

    def test_interactive_terminal_accepts_long_json_and_restores_input_mode(self):
        master, slave = os.openpty()
        original = termios.tcgetattr(slave)
        child = subprocess.Popen([sys.executable, "-I", "-B", str(SOURCE / "clients/dots_session.py"),
                                  "init", "--root", str(self.root.parent / "terminal-core")],
                                 stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env={**os.environ, "HOME": str(self.home)})
        try:
            self.assertTrue(select.select([child.stdout], [], [], 45)[0])
            self.assertEqual(json.loads(child.stdout.readline())["kind"], "core_ready")
            self.assertFalse(termios.tcgetattr(slave)[3] & (termios.ICANON | termios.ECHO))
            data = b" " * 5120 + b'{"household":"Synthetic terminal household"}\n'
            while data:
                data = data[os.write(master, data):]
            self.assertTrue(select.select([child.stdout], [], [], 45)[0])
            result = json.loads(child.stdout.readline())
            self.assertTrue(result["ok"], result)
            child.wait(timeout=10)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(child.stderr.read(), b"")
            self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            child.stdout.close()
            child.stderr.close()
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
