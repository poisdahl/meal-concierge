"""Real foreground core commands and synthetic native-host recovery."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
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

    def run_session(self, action, value, host=None):
        child = subprocess.Popen([sys.executable, "-I", "-B", str(SOURCE / "clients/dots_session.py"),
                                 action, "--root", str(self.root)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env={**os.environ, "HOME": str(self.home)})
        try:
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


if __name__ == "__main__":
    unittest.main()
