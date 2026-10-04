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
        elif operation == "product_search":
            result = {"query": frame["arguments"]["queries"][0], "page": 1,
                      "requested_size": 5, "semantics": "bounded_relevance_ranked",
                      "authenticated": True, "ready": True, "heading_count": 1,
                      "products": [{"product_id": PRODUCT, "name": "Havregryn",
                                    "package": "100g", "price": "20,00 kr",
                                    "detail_price": "20,00 kroner.", "deposit_status": "none",
                                    "available": True}]}
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
                    if not child.stdin.closed:
                        child.stdin.close()  # Lost host reply, not a definite pre-click stop.
                elif not child.stdin.closed:
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

    def managed_request(self, host=None):
        recipe = {"name": "Synthetic oats", "portions": 2,
                  "ingredients": [{"raw": "100 g havregryn", "item": "havregryn", "quantity": 100,
                                   "unit": "g", "scalable": True}], "steps": ["Cook the oats."],
                  "source": {"kind": "user", "publisher": "Synthetic kitchen", "relationship": "user_supplied"},
                  "rights": {"storage": "full", "credit": "Synthetic test"}}
        code, saved = self.call({"operation": "recipes", "action": "save", "recipe": recipe,
                                 "idempotency_key": "synthetic-managed-oats"})
        self.assertEqual(code, 0, saved)
        ref = saved["result"]["recipe"]
        code, saved = self.call({"operation": "menu", "action": "save", "menu": {
            "week": "2026-W41", "dishes": [{"recipe_ref": {"id": ref["id"], "revision": ref["revision"]},
                                             "portions": 6}],
            "schedule": [{"day": "2026-10-06", "meal": recipe["name"], "portions": 6}]}})
        self.assertEqual(code, 0, saved)
        menu = self.state()["menu"]
        menu_ref = {key: menu[key] for key in ("menu_id", "revision", "digest")}
        request = {"operation": "products", "action": "prepare", "menu_ref": menu_ref}
        code, first = self.call(request, host=host)
        self.assertEqual(code, 0, first)
        requirement = first["result"]["product_plan"]["requirements"][0]["requirement_id"]
        request["candidate_approvals"] = [{"requirement_id": requirement, "candidate_refs": [PRODUCT]}]
        code, prepared = self.call(request, host=host)
        self.assertEqual(code, 0, prepared)
        self.assertEqual(prepared["result"]["product_plan"]["status"], "prepared", prepared)
        self.assertEqual(self.writes, 0)
        code, saved_plan = self.call({"operation": "products", "action": "get",
                                     "product_plan_ref": prepared["result"]["product_plan_ref"]})
        self.assertEqual(code, 0, saved_plan)
        self.prepared_ref = prepared["result"]["product_plan_ref"]
        return {"operation": "products", **prepared["result"]["apply_arguments"], "cart_change_requested": True}

    def personalized_host(self, frame):
        reply = self.host(frame)
        if frame["operation"] == "product_search":
            reply["result"].update(semantics="bounded_personalized", sort_label="Anbefalt for deg")
        return reply

    def test_personalized_search_preserves_scope_through_saved_plan_and_apply(self):
        request = self.managed_request(host=self.personalized_host)
        code, saved = self.call({"operation": "products", "action": "get",
                                 "product_plan_ref": self.prepared_ref})
        self.assertEqual(code, 0, saved)
        plan = saved["result"]
        self.assertEqual(plan["scope"]["search_semantics"], "bounded_personalized")
        scope = plan["requirements"][0]["search_scope"]
        self.assertEqual((scope["semantics"], scope["sort_label"], scope["requested_size"]),
                         ("bounded_personalized", "Anbefalt for deg", 5))
        code, applied = self.call(request, host=self.personalized_host)
        self.assertEqual(code, 0, applied)
        self.assertTrue(applied["result"]["applied"], applied)
        self.assertEqual((self.quantity, self.writes), (3, 2))

    def test_personalized_search_mode_drift_requires_review_without_writes(self):
        request = self.managed_request(host=self.personalized_host)
        code, result = self.call(request)  # Same product, now relevance-ranked.
        self.assertEqual(code, 0, result)
        self.assertFalse(result["result"]["applied"])
        self.assertEqual((self.quantity, self.writes), (0, 0))
        self.assertNotIn("pending_cart_change", self.state())

    def test_mixed_search_modes_are_reported_from_actual_observations(self):
        recipe = {"name": "Synthetic oats with milk", "portions": 2,
                  "ingredients": [{"raw": "100 g havregryn", "item": "havregryn", "quantity": 100,
                                   "unit": "g", "scalable": True},
                                  {"raw": "200 ml melk", "item": "melk", "quantity": 200,
                                   "unit": "ml", "scalable": True}], "steps": ["Cook the oats."],
                  "source": {"kind": "user", "publisher": "Synthetic kitchen", "relationship": "user_supplied"},
                  "rights": {"storage": "full", "credit": "Synthetic test"}}
        code, saved = self.call({"operation": "recipes", "action": "save", "recipe": recipe,
                                 "idempotency_key": "synthetic-mixed-search"})
        self.assertEqual(code, 0, saved)
        ref = saved["result"]["recipe"]
        code, saved = self.call({"operation": "menu", "action": "save", "menu": {
            "week": "2026-W41", "dishes": [{"recipe_ref": {"id": ref["id"], "revision": ref["revision"]},
                                             "portions": 6}],
            "schedule": [{"day": "2026-10-06", "meal": recipe["name"], "portions": 6}]}})
        self.assertEqual(code, 0, saved)
        menu = self.state()["menu"]
        reads = []
        def mixed(frame):
            if frame["operation"] == "product_search":
                reads.append(frame)
                if len(reads) == 1:
                    return self.personalized_host(frame)
            return self.host(frame)
        code, result = self.call({"operation": "products", "action": "prepare",
                                  "menu_ref": {key: menu[key] for key in ("menu_id", "revision", "digest")}}, host=mixed)
        self.assertEqual(code, 0, result)
        plan = result["result"]["product_plan"]
        self.assertEqual(len(reads), 2)
        self.assertEqual(plan["scope"]["search_semantics"], "mixed_bounded")
        self.assertEqual({row["observation"]["scope"]["semantics"] for row in plan["requirements"]},
                         {"bounded_personalized", "bounded_relevance_ranked"})
        self.assertEqual(self.writes, 0)

    def clear_request(self):
        code, cart = self.call({"operation": "cart", "action": "get"})
        self.assertEqual(code, 0, cart)
        return {"operation": "cart", "action": "clear", "cart_digest": cart["result"]["cart_digest"]}

    def test_clear_managed_cart_uses_batches_digest_and_cached_receipt_after_disable(self):
        self.assertEqual(self.call(self.managed_request())[0], 0)
        code, stale = self.call({"operation": "cart", "action": "clear", "cart_digest": "0" * 64})
        self.assertEqual(code, 1, stale)
        self.assertEqual((self.quantity, self.writes), (3, 2))
        request, command_id = self.clear_request(), str(uuid.uuid4())
        code, cleared = self.call(request, command_id)
        self.assertEqual(code, 0, cleared)
        self.assertTrue(cleared["result"]["cleared"])
        self.assertEqual((self.quantity, self.writes), (0, 4))
        self.assertNotIn("cart_plan", self.state())
        self.assertNotIn("pending_cart_change", self.state())
        self.assertEqual(self.call({"operation": "native_cart_policy", "action": "set", "enabled": False,
                                    "browser_binding": self.binding})[0], 0)
        before, frames = self.files(), list(self.frames)
        self.assertEqual(self.call(request, command_id), (0, cleared))
        self.assertEqual((self.files(), self.frames), (before, frames))
        code, blocked = self.call(request)
        self.assertEqual(code, 1, blocked)
        self.assertEqual((self.files(), self.frames), (before, frames))

    def test_clear_lost_complete_final_batch_reply_recovers_without_dispatch(self):
        self.assertEqual(self.call(self.managed_request())[0], 0)
        request = self.clear_request()
        def lose_final(frame):
            reply = self.host(frame)
            return None if frame["operation"] == "manipulate_cart" and self.quantity == 0 else reply
        self.call(request, host=lose_final)
        self.assertIn("pending_cart_change", self.state())
        self.assertEqual((self.quantity, self.writes), (0, 4))
        code, recovered = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 0, recovered)
        self.assertEqual((self.quantity, self.writes), (0, 4))
        self.assertNotIn("pending_cart_change", self.state())
        self.assertNotIn("cart_plan", self.state())

    def test_clear_partial_batch_loss_preserves_pending_and_never_resends(self):
        self.assertEqual(self.call(self.managed_request())[0], 0)
        request = self.clear_request()
        def partial(frame):
            if frame["operation"] == "manipulate_cart":
                self.quantity -= 1
                self.writes += 1
                return None
            return self.host(frame)
        self.call(request, host=partial)
        pending = self.state()["pending_cart_change"]
        self.assertEqual((self.quantity, self.writes), (2, 3))
        code, recovered = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 1, recovered)
        self.assertEqual(self.state()["pending_cart_change"], pending)
        code, blocked = self.call(request)
        self.assertEqual(code, 1, blocked)
        self.assertEqual((self.quantity, self.writes), (2, 3))
        self.assertEqual(self.state()["pending_cart_change"], pending)

    def test_managed_menu_uses_complete_search_and_verified_batches(self):
        request = self.managed_request()
        command_id = str(uuid.uuid4())
        code, applied = self.call(request, command_id)
        self.assertEqual(code, 0, applied)
        self.assertTrue(applied["result"]["applied"], applied)
        self.assertEqual((self.quantity, self.writes), (3, 2))
        state = self.state()
        self.assertNotIn("pending_cart_change", state)
        self.assertNotIn("managed_product_apply_fence", state)
        self.assertEqual(state["cart_plan"]["added_quantities"], {PRODUCT: 3})
        self.assertEqual(state["cart_plan"]["product_plan_digest"], request["product_plan_digest"])
        self.assertIn("product_plan_authority", state["cart_plan"])
        before, frames = self.files(), list(self.frames)
        code, cached = self.call(request, command_id)
        self.assertEqual((code, cached), (0, applied))
        self.assertEqual((self.files(), self.frames), (before, frames))

    def test_managed_lost_second_batch_reply_reconciles_without_double_allocation(self):
        request = self.managed_request()
        def lose_second(frame):
            reply = self.host(frame)
            return None if frame["operation"] == "manipulate_cart" and self.writes == 2 else reply
        code, result = self.call(request, host=lose_second)
        self.assertIn("pending_cart_change", self.state())
        self.assertEqual((self.quantity, self.writes), (3, 2))
        self.assertEqual(self.state()["pending_cart_change"]["native_managed"]["verified"], {PRODUCT: 2})
        code, recovered = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 0, recovered)
        self.assertEqual(self.writes, 2)
        state = self.state()
        self.assertNotIn("pending_cart_change", state)
        self.assertEqual(state["cart_plan"]["added_quantities"], {PRODUCT: 3})
        self.assertEqual(state["cart_plan"]["baseline_quantities"], {})
        self.assertEqual(state["cart_plan"]["status"], "needs_input")
        self.assertNotIn("product_plan_authority", state["cart_plan"])
        self.assertIn("managed_product_apply_fence", state)
        code, blocked = self.call(self.ensure(4))
        self.assertEqual(code, 1, blocked)
        self.assertEqual(self.writes, 2)

    def test_partial_managed_dispatch_protects_unconfirmed_units_as_baseline(self):
        request = self.managed_request()
        def partial(frame):
            if frame["operation"] == "manipulate_cart":
                self.assertEqual(self.state()["pending_cart_change"]["before"], {})
                self.quantity, self.writes = 1, self.writes + 1
                return None
            return self.host(frame)
        self.call(request, host=partial)
        code, recovered = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 0, recovered)
        state = self.state()
        self.assertEqual(state["cart_plan"]["added_quantities"], {})
        self.assertEqual(state["cart_plan"]["baseline_quantities"], {PRODUCT: 1})
        self.assertEqual(self.writes, 1)
        code, decision = self.call({"operation": "cart", "action": "reconcile", "decision": "keep_current",
                                    "menu_ref": state["cart_plan"]["menu_ref"],
                                    "cart_digest": state["cart_plan"]["pending_cart_digest"],
                                    "accept_missing_product_ids": [PRODUCT]})
        self.assertEqual(code, 0, decision)
        self.assertTrue(decision["result"]["reconciled"])
        self.assertEqual(self.writes, 1)

    def test_managed_boundaries_block_before_intent_or_dispatch(self):
        before = self.files()
        for request in ({"operation": "products", "action": "apply", "partial_apply": True},
                        {"operation": "cart", "action": "reconcile", "decision": "restore_missing"},
                        {"operation": "cart", "action": "reconcile", "decision": "keep_current", "exclude_product_ids": [PRODUCT]}):
            code, result = self.call(request)
            self.assertEqual(code, 1, result)
            self.assertEqual(self.files(), before)
        policy = {"operation": "native_cart_policy", "action": "set", "enabled": False,
                  "browser_binding": self.binding}
        self.assertEqual(self.call(policy)[0], 0)
        before, frames = self.files(), list(self.frames)
        code, result = self.call({"operation": "products", "action": "apply"})
        self.assertEqual(code, 1, result)
        self.assertEqual((self.files(), self.frames), (before, frames))

    def test_finalization_crash_preserves_only_exact_committed_plan(self):
        request = self.managed_request()
        journals = []
        def retain_journal(frame):
            reply = self.host(frame)
            if frame["operation"] == "manipulate_cart":
                journals.append(self.state()["pending_cart_change"])
            return reply
        code, applied = self.call(request, host=retain_journal)
        self.assertEqual(code, 0, applied)
        self.assertTrue(applied["result"]["applied"])
        completed = self.state()
        # Persisted final batch + committed core plan models process loss at
        # the narrow boundary before the adapter removes its journal.
        journal = journals[-1]
        journal["native_managed"]["verified"] = journal["expected"]
        for exact in (True, False):
            with self.subTest(exact_product_identity=exact):
                state = json.loads(json.dumps(completed))
                state["pending_cart_change"] = json.loads(json.dumps(journal))
                if not exact:
                    state["pending_cart_change"]["native_managed"]["product_plan_digest"] = "c" * 64
                (self.root / "state/state.json").write_text(json.dumps(state))
                code, read = self.call({"operation": "cart", "action": "get"})
                self.assertEqual(code, 0, read)
                self.assertTrue(read["result"]["cart_write_pending"])
                self.assertEqual(self.state()["pending_cart_change"], state["pending_cart_change"])
                code, recovered = self.call({"operation": "cart", "action": "reconcile_change"})
                self.assertEqual(code, 0, recovered)
                recovered_state = self.state()
                self.assertNotIn("pending_cart_change", recovered_state)
                self.assertEqual(recovered_state["cart_plan"]["added_quantities"], {PRODUCT: 3})
                self.assertEqual(self.writes, 2)
                if exact:
                    self.assertEqual(recovered_state["cart_plan"], completed["cart_plan"])
                    self.assertNotIn("managed_product_apply_fence", recovered_state)
                else:
                    self.assertEqual(recovered_state["cart_plan"]["status"], "needs_input")
                    self.assertNotIn("product_plan_authority", recovered_state["cart_plan"])
                    self.assertIn("managed_product_apply_fence", recovered_state)

    def test_native_search_rejects_selected_card_as_ranked_scope(self):
        request = self.managed_request()
        for invalid in ("selected_card", "personalized_missing_sort", "personalized_wrong_sort", PRODUCT + "?other=1", "/varer/../havregryn-1234567890123",
                        "/varer/%2e%2e/havregryn-1234567890123", "/varer/%bad%/havregryn-1234567890123"):
            with self.subTest(invalid_search_scope_or_path=invalid):
                def selected_card(frame):
                    reply = self.host(frame)
                    if frame["operation"] == "product_search":
                        if invalid.startswith("personalized_"):
                            reply["result"]["semantics"] = "bounded_personalized"
                            if invalid == "personalized_wrong_sort":
                                reply["result"]["sort_label"] = "Varegruppe"
                        elif invalid == "selected_card":
                            reply["result"]["semantics"] = invalid
                        else:
                            reply["result"]["products"][0]["product_id"] = invalid
                    return reply
                code, result = self.call({"operation": "products", "action": "prepare",
                                          "menu_ref": request["menu_ref"],
                                          "candidate_approvals": request["candidate_approvals"]}, host=selected_card)
                self.assertEqual(code, 0, result)
                plan = result["result"]["product_plan"]
                self.assertNotEqual(plan["status"], "prepared")
                self.assertIn("provider_search_unavailable_or_scope_changed",
                              [row["reason"] for row in plan["unresolved_requirements"]])
                self.assertEqual(self.writes, 0)

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

    def delivery_host(self, frame):
        if frame["operation"] != "get_delivery_slots":
            return self.host(frame)
        label = "fra 49 kr fra 49 kroner, 6. oktober klokka 09:00 til 12:00"
        return {"reply_to": frame["call_id"], "browser_binding": self.binding,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "result": {"authenticated": True, "ready": True,
                           "source_url": "https://meny.no/varer", "dialog_count": 1,
                           "slots": [{"slot_id": label, "display": label, "date": "2026-10-06",
                                      "start": "09:00", "end": "12:00", "selected": False}]}}

    def test_delivery_read_normalizes_labels_dates_and_from_price_without_writes(self):
        code, policy = self.call({"operation": "native_cart_policy", "action": "set", "enabled": False})
        self.assertEqual(code, 0, policy)
        before = self.state()
        code, result = self.call({"operation": "delivery", "action": "list",
                                 "dates": ["2026-10-06", "2026-10-07"]}, host=self.delivery_host)
        self.assertEqual(code, 0, result)
        slots = result["result"]["slots"]
        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertEqual(slot["slot_ref"], "meny:2026-10-06T09:00/12:00")
        self.assertEqual(slot["start_at"], "2026-10-06T09:00:00+02:00")
        self.assertEqual(slot["price_ore"], 4900)
        self.assertEqual(slot["price_kind"], "from")
        self.assertFalse(slot["selected"])
        self.assertEqual(result["result"]["price_display"][slot["slot_ref"]], "fra 49 kr")
        self.assertIn("6. oktober", result["result"]["display"][slot["slot_ref"]])
        for key in ("profile", "menu", "pending_cart_change", "cart_plan", "native_cart_writes_enabled"):
            self.assertEqual(before.get(key), self.state().get(key))
        self.assertEqual(self.writes, 0)
        self.assertTrue(all(f["effect"] == "read" for f in self.frames))

    def test_delivery_empty_requested_date_requires_a_complete_nonempty_picker(self):
        code, result = self.call({"operation": "delivery", "dates": ["2026-10-07"]}, host=self.delivery_host)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["slots"], [])
        self.assertEqual(result["result"]["display"], {})
        def empty(frame):
            reply = self.delivery_host(frame)
            if frame["operation"] == "get_delivery_slots":
                reply["result"]["slots"] = []
            return reply
        code, result = self.call({"operation": "delivery"}, host=empty)
        self.assertEqual(code, 1, result)
        self.assertEqual(self.writes, 0)

    def test_delivery_rejects_changed_route_auth_picker_or_binding(self):
        for field, value in (("source_url", "https://meny.no/trumf-profil"),
                             ("authenticated", False), ("ready", False),
                             ("dialog_count", True), ("dialog_count", 2)):
            with self.subTest(field=field, value=value):
                def changed(frame):
                    reply = self.delivery_host(frame)
                    if frame["operation"] == "get_delivery_slots":
                        reply["result"][field] = value
                    return reply
                code, result = self.call({"operation": "delivery"}, host=changed)
                self.assertEqual(code, 1, result)
        def wrong_binding(frame):
            reply = self.delivery_host(frame)
            if frame["operation"] == "get_delivery_slots":
                reply["browser_binding"] = {**self.binding, "account_sha256": "c" * 64}
            return reply
        code, result = self.call({"operation": "delivery"}, host=wrong_binding)
        self.assertEqual(code, 1, result)
        self.assertEqual(self.writes, 0)

    def test_delivery_rejects_conflicting_slot_facts_or_duplicate_refs(self):
        for change in ("date", "label", "duplicate", "selected"):
            with self.subTest(change=change):
                def changed(frame):
                    reply = self.delivery_host(frame)
                    if frame["operation"] == "get_delivery_slots":
                        slots = reply["result"]["slots"]
                        if change == "date":
                            slots[0]["date"] = "2026-10-07"
                        elif change == "label":
                            slots[0]["display"] = slots[0]["display"].replace("09:00", "10:00")
                        elif change == "selected":
                            slots[0]["selected"] = 1
                        else:
                            slots.append(dict(slots[0]))
                    return reply
                code, result = self.call({"operation": "delivery"}, host=changed)
                self.assertEqual(code, 1, result)
        self.assertEqual(self.writes, 0)

    def test_delivery_selection_and_address_override_stop_before_intent(self):
        for request in ({"operation": "delivery", "action": "select", "slot_ref": "anything"},
                        {"operation": "delivery", "action": "list", "address_id": "another"},
                        {"operation": "orders", "action": "list"}):
            before = self.files()
            frame_count = len(self.frames)
            code, result = self.call(request)
            self.assertEqual(code, 1, result)
            self.assertEqual(self.files(), before)
            self.assertEqual(len(self.frames), frame_count)

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
