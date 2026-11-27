"""Real foreground core commands and synthetic native-host recovery."""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import termios
import tempfile
import unittest
import uuid
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
PRODUCT = "/varer/frokost/havregryn-1234567890123"
EDIT_PRODUCT = "/varer/middag/pasta-1234567890222"


class DotsSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mc-dots-session-")
        self.addCleanup(self.temp.cleanup)
        temp_root = Path(self.temp.name).resolve()
        self.root = temp_root / "core"
        self.home = temp_root / "home"
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

    def host(self, frame, *, product_window_minimum=110):
        self.assertEqual(frame["browser_binding"], self.binding)
        operation = frame["operation"]
        lifetime = (datetime.fromisoformat(frame["expires_at"])
                    - datetime.fromisoformat(frame["emitted_at"])).total_seconds()
        self.assertGreater(lifetime, 0)
        if operation == "product_search":
            self.assertGreater(lifetime, product_window_minimum)
            self.assertLessEqual(lifetime, 120)
        else:
            self.assertLessEqual(lifetime, 60)
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

    def run_session(self, action, value, host=None, environment=None, launch_code=None):
        launch = (["-c", launch_code] if launch_code is not None
                  else [str(SOURCE / "clients/dots_session.py")])
        child = subprocess.Popen([sys.executable, "-I", "-B", *launch,
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

    def inspect_cart(self):
        before = self.files()
        result = subprocess.run([sys.executable, "-I", "-B", str(SOURCE / "clients/dots_session.py"),
                                 "inspect-cart", "--root", str(self.root)],
                                check=True, capture_output=True)
        self.assertEqual(self.files(), before)
        return json.loads(result.stdout)["result"]

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

    def timed_managed_apply(self, enclosing_limit=None, request=None):
        request = request or self.managed_request()
        # Only this child gets a controlled monotonic clock. The real CLI,
        # correlated stdin/stdout replies, planner and pending journal still run.
        launch_code = f"""
import sys, time
sys.path.insert(0, {str(SOURCE)!r})
from clients import dots_session as client
original_monotonic = time.monotonic
elapsed = 0
time.monotonic = lambda: original_monotonic() + elapsed
frames = {{}}
cart_reads = 0
original_emit = client.emit
def emit(value):
    global cart_reads
    if value['kind'] == 'native_host_request':
        operation = value['operation']
        if operation == 'get_cart':
            cart_reads += 1
        cost = (100 if operation == 'product_search' else
                40 if operation == 'verify_new_cart' else
                55 if operation == 'get_cart' and cart_reads <= 3 else 1)
        frames[value['call_id']] = cost
    original_emit(value if value['kind'] == 'core_ready'
                  else {{**value, 'test_elapsed_seconds': elapsed}})
client.emit = emit
original_line = client.Input.line
def line(self, deadline):
    global elapsed
    value = original_line(self, deadline)
    elapsed += frames.pop(value.get('reply_to'), 0)
    if time.monotonic() >= deadline:
        raise client.HouseholdError('controlled host reply deadline reached')
    return value
client.Input.line = line
if {enclosing_limit!r} == 'application_default':
    original_application = client.Application
    def application(*args, **kwargs):
        kwargs.pop('products_apply_budget_seconds')
        return original_application(*args, **kwargs)
    client.Application = application
elif {enclosing_limit!r} == 'native_240':
    original_application = client.application
    def application(root, config, reader, command_id, deadline, **kwargs):
        return original_application(root, config, reader, command_id,
                                    min(deadline, time.monotonic() + 240), **kwargs)
    client.application = application
raise SystemExit(client.main())
"""
        self.frames = []
        return self.run_session("call", {"request_id": str(uuid.uuid4()), "request": request},
                                launch_code=launch_code)

    def test_native_apply_finishes_after_240_seconds_with_bounded_frames(self):
        code, result = self.timed_managed_apply()
        self.assertEqual(code, 0, result)
        self.assertTrue(result["result"]["applied"], result)
        writes = [frame for frame in self.frames if frame["effect"] == "cart_write"]
        self.assertGreater(writes[0]["test_elapsed_seconds"], 240)
        self.assertLess(result["test_elapsed_seconds"], 600)
        self.assertEqual((self.quantity, self.writes), (3, 2))
        state = self.state()
        self.assertEqual(state["cart_plan"]["added_quantities"], {PRODUCT: 3})
        self.assertNotIn("pending_cart_change", state)

    def test_default_application_budget_defers_slow_meny_write_to_a_full_budget(self):
        code, result = self.timed_managed_apply("application_default")
        self.assertEqual(code, 0, result)
        first = result["result"]
        # Reads left less than the MENY cart-write reserve: no write is started.
        self.assertFalse(first["applied"])
        self.assertEqual(first["status"], "validating", first)
        self.assertEqual(first["validation_progress"]["pending_cart_write"], 1)
        self.assertFalse(any(frame["effect"] == "cart_write" for frame in self.frames))
        self.assertEqual((self.quantity, self.writes), (0, 0))
        self.assertNotIn("pending_cart_change", self.state())
        # The continuation reuses the completed reads and writes with a full budget.
        code, result = self.timed_managed_apply(
            "application_default", request={"operation": "products", **first["continue_arguments"]})
        self.assertEqual(code, 0, result)
        self.assertTrue(result["result"]["applied"], result)
        self.assertNotIn("product_search", [frame["operation"] for frame in self.frames])
        self.assertEqual((self.quantity, self.writes), (3, 2))
        self.assertNotIn("pending_cart_change", self.state())

    def test_earlier_native_deadline_stops_slow_apply_before_write(self):
        code, result = self.timed_managed_apply("native_240")
        self.assertEqual(code, 0, result)
        self.assertFalse(result["result"]["applied"])
        self.assertEqual(result["result"]["status"], "rejected_before_write")
        self.assertFalse(result["result"]["cart_write_pending"])
        self.assertEqual(result["result"]["reason"], "cart_read_failed_before_first_batch")
        self.assertFalse(any(frame["effect"] == "cart_write" for frame in self.frames))
        self.assertGreater(result["test_elapsed_seconds"], 240)
        self.assertEqual((self.quantity, self.writes), (0, 0))
        self.assertNotIn("pending_cart_change", self.state())

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

    def two_product_request(self, cache=None):
        milk = "/varer/meieri/melk-1234567890124"
        quantities, searches = {}, []
        cache = cache if cache is not None else {}
        def host(frame):
            operation = frame["operation"]
            if operation in {"verify_new_cart", "product_search"}:
                reply = self.host(frame)
                if operation == "product_search":
                    if cache.get("cached") is not None:
                        work = self.state()["menu_planning"]["prepared"][cache["validation_ref"]]["validation_work"]
                        self.assertEqual(next(iter(work["searches"].values()))["observed_at"], cache["cached"]["observed_at"])
                    query = frame["arguments"]["queries"][0]
                    searches.append(query)
                    if "melk" in query.casefold():
                        reply["result"]["products"] = [{"product_id": milk, "name": "Melk",
                            "package": "200ml", "price": "10,00 kr", "detail_price": "10,00 kroner.",
                            "deposit_status": "none", "available": True}]
                return reply
            self.assertEqual(frame["browser_binding"], self.binding)
            self.assertLessEqual((datetime.fromisoformat(frame["expires_at"])
                                - datetime.fromisoformat(frame["emitted_at"])).total_seconds(), 60)
            if operation == "get_cart":
                result = self.snapshot()
                items = [{"product_id": product, "name": "Melk" if product == milk else "Havregryn",
                          "quantity": quantity, "price": quantity * (10 if product == milk else 20)}
                         for product, quantity in quantities.items()]
                result.update(items=items, item_root_count=len(items), control_count=len(items), empty=not items,
                              total_count=int(bool(items)), subtotal_count=int(bool(items)),
                              subtotal=sum(item["price"] for item in items) if items else None,
                              total=sum(item["price"] for item in items), count=sum(quantities.values()))
            else:
                self.assertEqual(operation, "manipulate_cart")
                pending = self.state()["pending_cart_change"]
                self.assertEqual(frame["before_quantities"], quantities)
                self.assertEqual(pending["before"], quantities)
                self.assertEqual(pending["operations"], frame["arguments"]["operations"])
                for item in frame["arguments"]["operations"]:
                    self.assertIn(item["productId"], (PRODUCT, milk))
                    self.assertEqual(item["quantity"], 1)
                    quantities[item["productId"]] = quantities.get(item["productId"], 0) + 1
                self.assertEqual(frame["expected_quantities"], quantities)
                self.writes += 1
                result = {"dispatched": True}
            return {"reply_to": frame["call_id"], "browser_binding": self.binding,
                    "observed_at": datetime.now(timezone.utc).isoformat(), "result": result}
        recipe = {"name": "Synthetic oats and milk", "portions": 2,
                  "ingredients": [{"raw": "100 g havregryn", "item": "havregryn", "quantity": 100,
                                   "unit": "g", "scalable": True},
                                  {"raw": "200 ml melk", "item": "melk", "quantity": 200,
                                   "unit": "ml", "scalable": True}], "steps": ["Cook the oats."],
                  "source": {"kind": "user", "publisher": "Synthetic kitchen", "relationship": "user_supplied"},
                  "rights": {"storage": "full", "credit": "Synthetic test"}}
        code, saved = self.call({"operation": "recipes", "action": "save", "recipe": recipe,
                                 "idempotency_key": "synthetic-two-product-apply"})
        self.assertEqual(code, 0, saved)
        ref = saved["result"]["recipe"]
        code, saved = self.call({"operation": "menu", "action": "save", "menu": {
            "week": "2026-W41", "dishes": [{"recipe_ref": {"id": ref["id"], "revision": ref["revision"]},
                                             "portions": 2}],
            "schedule": [{"day": "2026-10-06", "meal": recipe["name"], "portions": 2}]}})
        self.assertEqual(code, 0, saved)
        menu = self.state()["menu"]
        request = {"operation": "products", "action": "prepare",
                   "menu_ref": {key: menu[key] for key in ("menu_id", "revision", "digest")}}
        code, first = self.call(request, host=host)
        self.assertEqual(code, 0, first)
        request["candidate_approvals"] = [{"requirement_id": row["requirement_id"],
            "candidate_refs": [row["observation"]["products"][0]["product_ref"]]}
            for row in first["result"]["product_plan"]["requirements"]]
        code, ready = self.call(request, host=host)
        self.assertEqual(code, 0, ready)
        self.assertEqual(ready["result"]["product_plan"]["status"], "prepared", ready)
        apply = {"operation": "products", **ready["result"]["apply_arguments"], "cart_change_requested": True}
        return apply, host, quantities, searches, milk

    def test_two_product_apply_continues_one_search_with_cached_time_and_expiry(self):
        cache = {}
        apply, host, quantities, searches, milk = self.two_product_request(cache)
        searches.clear()
        command_id = str(uuid.uuid4())
        code, partial = self.call(apply, command_id, host)
        self.assertEqual(code, 0, partial)
        self.assertEqual(partial["result"]["status"], "validating")
        self.assertFalse(partial["result"]["applied"])
        self.assertFalse(partial["result"]["cart_changed"])
        self.assertEqual(len(searches), 1)
        self.assertEqual((quantities, self.writes), ({}, 0))
        self.assertIsNone(self.state().get("pending_cart_change"))
        validation_ref = partial["result"]["product_plan_ref"]
        cache["validation_ref"] = validation_ref
        work = self.state()["menu_planning"]["prepared"][validation_ref]["validation_work"]
        cached = next(iter(work["searches"].values()))
        cache["cached"] = cached
        self.assertEqual(len(work["searches"]), 1)
        frames = len(self.frames)
        self.assertEqual(self.call(apply, command_id, host), (0, partial))
        self.assertEqual(len(self.frames), frames)
        code, applied = self.call({"operation": "products", **partial["result"]["continue_arguments"]}, host=host)
        self.assertEqual(code, 0, applied)
        self.assertTrue(applied["result"]["applied"], applied)
        self.assertEqual(len(searches), 2)
        self.assertNotEqual(searches[0], searches[1])
        cache["cached"] = None
        self.assertEqual(quantities, {PRODUCT: 1, milk: 1})
        self.assertEqual(self.state()["cart_plan"]["added_quantities"], quantities)
        self.assertIsNone(self.state().get("pending_cart_change"))
        # A failed first reply consumes the allowance too; no second search is emitted.
        def failed(frame):
            reply = host(frame)
            if frame["operation"] == "product_search":
                reply.pop("result")
                reply["error"] = "synthetic read failed"
            return reply
        frame_count = len(self.frames)
        code, fresh = self.call(apply, host=failed)
        self.assertEqual(code, 0, fresh)
        self.assertEqual(fresh["result"]["status"], "validating")
        self.assertEqual(fresh["result"]["validation_progress"]["pending_search_count"], 2)
        self.assertEqual(sum(f["operation"] == "product_search" for f in self.frames[frame_count:]), 1)
        code, fresh = self.call({"operation": "products", **fresh["result"]["continue_arguments"]}, host=host)
        self.assertEqual(code, 0, fresh)
        self.assertEqual(fresh["result"]["status"], "validating")
        # An expired saved cycle cannot dispatch its cached facts.
        state = self.state()
        work = state["menu_planning"]["prepared"][fresh["result"]["product_plan_ref"]]["validation_work"]
        work["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        (self.root / "state/state.json").write_text(json.dumps(state))
        writes, reads = self.writes, len(searches)
        code, restarted = self.call({"operation": "products", **fresh["result"]["continue_arguments"]}, host=host)
        self.assertEqual(code, 0, restarted)
        self.assertEqual(restarted["result"]["status"], "validating")
        self.assertTrue(restarted["result"]["validation_progress"]["restarted"])
        self.assertEqual(len(searches), reads + 1)
        self.assertEqual(self.writes, writes)
        self.assertIsNone(self.state().get("pending_cart_change"))

    def restore_partial_two_product_cart(self, lose_reply=False):
        apply, host, quantities, searches, milk = self.two_product_request()
        def finish(request, operator=host):
            for _ in range(3):
                code, result = self.call(request, host=operator)
                if result.get("result", {}).get("status") != "validating":
                    return code, result
                args = result["result"]["continue_arguments"]
                if request.get("restore_missing"):
                    # Recover a lost validating reply through the real get path.
                    code, fetched = self.call({"operation": "products", "action": "get",
                                              "product_plan_ref": result["result"]["product_plan_ref"]})
                    self.assertEqual(code, 0, fetched)
                    self.assertEqual(fetched["result"]["continue_arguments"], args)
                    args = fetched["result"]["continue_arguments"]
                    self.assertTrue(args["restore_missing"])
                    self.assertEqual(args["cart_digest"], request["cart_digest"])
                request = {"operation": "products", **args}
            self.fail("two-product validation failed to finish")
        def partial(frame):
            if frame["operation"] == "manipulate_cart":
                self.assertEqual(frame["expected_quantities"], {PRODUCT: 1, milk: 1})
                # The host finishes both clicks. An independent cart edit then
                # removes one product before the verification read.
                reply = host(frame)
                quantities.pop(PRODUCT)
                return reply
            return host(frame)
        code, first = finish(apply, partial)
        self.assertEqual(code, 0, first)
        self.assertFalse(first["result"]["applied"])
        self.assertIsNotNone(self.state().get("pending_cart_change"))
        code, recovered = self.call({"operation": "cart", "action": "reconcile_change"}, host=host)
        self.assertEqual(code, 0, recovered)
        state = self.state()
        self.assertEqual(state["cart_plan"]["baseline_quantities"], {milk: 1})
        self.assertEqual(state["cart_plan"]["added_quantities"], {})
        digest = state["cart_plan"]["pending_cart_digest"]
        code, decision = self.call({"operation": "cart", "action": "reconcile", "decision": "keep_current",
                                   "menu_ref": state["cart_plan"]["menu_ref"], "cart_digest": digest}, host=host)
        self.assertEqual(code, 0, decision)
        code, ordinary = finish(apply)
        self.assertEqual(code, 0, ordinary)
        self.assertTrue(ordinary["result"]["applied"])
        self.assertEqual((quantities, self.writes), ({milk: 1}, 1))
        restoration = {**apply, "restore_missing": True, "cart_digest": digest}
        code, stale = finish({**restoration, "cart_digest": "0" * 64})
        self.assertEqual(code, 1, stale)
        self.assertEqual((quantities, self.writes), ({milk: 1}, 1))
        def changed(frame):
            reply = host(frame)
            if frame["operation"] == "product_search":
                reply["result"]["products"][0].update(price="999,00 kr", detail_price="999,00 kroner.")
            return reply
        code, drift = finish(restoration, changed)
        self.assertEqual(code, 0, drift)
        self.assertFalse(drift["result"]["applied"])
        self.assertEqual((quantities, self.writes), ({milk: 1}, 1))
        writes = []
        def restore_host(frame):
            if frame["operation"] == "manipulate_cart":
                writes.append(frame)
                self.assertEqual(frame["arguments"]["operations"], [{"productId": PRODUCT, "quantity": 1}])
                self.assertEqual(frame["before_quantities"], {milk: 1})
            reply = host(frame)
            return None if lose_reply and frame["operation"] == "manipulate_cart" else reply
        code, restored = finish(restoration, restore_host)
        self.assertEqual(code, 0, restored)
        self.assertEqual(len(writes), 1)
        self.assertEqual((quantities, self.writes), ({milk: 1, PRODUCT: 1}, 2))
        if lose_reply:
            self.assertFalse(restored["result"]["applied"])
            self.assertIsNotNone(self.state().get("pending_cart_change"))
            code, recovered = self.call({"operation": "cart", "action": "reconcile_change"}, host=host)
            self.assertEqual(code, 1, recovered)
            self.assertIn("pending_cart_change", self.state())
            self.assertEqual(self.writes, 2)
            return
        else:
            self.assertTrue(restored["result"]["applied"])
        self.assertIsNone(self.state().get("pending_cart_change"))
        self.assertEqual(self.state()["cart_plan"]["baseline_quantities"], {milk: 1})
        self.assertEqual(self.state()["cart_plan"]["added_quantities"], {PRODUCT: 1})

    def test_explicit_restore_missing_preserves_partial_baseline_and_keep_current(self):
        self.restore_partial_two_product_cart()

    def test_restore_missing_lost_reply_reconciles_without_replay_or_double_attribution(self):
        self.restore_partial_two_product_cart(lose_reply=True)

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

    def test_clear_lost_final_batch_reply_stays_fenced_with_empty_cart(self):
        self.assertEqual(self.call(self.managed_request())[0], 0)
        request = self.clear_request()
        def lose_final(frame):
            reply = self.host(frame)
            return None if frame["operation"] == "manipulate_cart" and self.quantity == 0 else reply
        self.call(request, host=lose_final)
        self.assertIn("pending_cart_change", self.state())
        self.assertEqual((self.quantity, self.writes), (0, 4))
        code, recovered = self.call({"operation": "cart", "action": "reconcile_change"})
        self.assertEqual(code, 1, recovered)
        self.assertIn("still uncertain", recovered["error"])
        self.assertEqual((self.quantity, self.writes), (0, 4))
        self.assertIn("pending_cart_change", self.state())
        self.assertIn("cart_plan", self.state())

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
        self.assertEqual(code, 1, recovered)
        self.assertIn("still uncertain", recovered["error"])
        self.assertEqual(self.writes, 2)
        state = self.state()
        self.assertEqual(state["pending_cart_change"]["native_managed"]["verified"], {PRODUCT: 2})
        self.assertNotIn("dispatch_finished", state["pending_cart_change"])
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
        self.assertEqual(code, 1, recovered)
        self.assertIn("still uncertain", recovered["error"])
        state = self.state()
        self.assertEqual(state["cart_plan"]["added_quantities"], {})
        self.assertIn("pending_cart_change", state)
        self.assertEqual(self.writes, 1)
        code, decision = self.call({"operation": "cart", "action": "reconcile", "decision": "keep_current",
                                    "menu_ref": state["cart_plan"]["menu_ref"],
                                    "cart_digest": state["cart_plan"]["pending_cart_digest"],
                                    "accept_missing_product_ids": [PRODUCT]})
        self.assertEqual(code, 1, decision)
        self.assertIn("pending_cart_change", self.state())
        self.assertEqual(self.writes, 1)

    def test_managed_boundaries_block_before_intent_or_dispatch(self):
        before, frames = self.files(), list(self.frames)
        for request in ({"operation": "cart", "action": "ensure", "_restore_missing_cart_digest": "a" * 64},
                        {"operation": "products", "action": "apply", "_restore_missing_cart_digest": "a" * 64},
                        {"operation": "products", "action": "apply", "partial_apply": True},
                        {"operation": "products", "action": "apply", "restore_missing": "true", "cart_digest": "a" * 64},
                        {"operation": "products", "action": "apply", "restore_missing": True},
                        {"operation": "products", "action": "apply", "cart_digest": "a" * 64},
                        {"operation": "products", "action": "prepare", "restore_missing": True, "cart_digest": "a" * 64},
                        {"operation": "cart", "action": "reconcile", "decision": "restore_missing"},
                        {"operation": "cart", "action": "reconcile", "decision": "keep_current", "exclude_product_ids": [PRODUCT]}):
            code, result = self.call(request)
            self.assertEqual(code, 1, result)
            self.assertEqual((self.files(), self.frames), (before, frames))
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

    def test_native_catalog_product_and_recipe_search_are_bound_reads(self):
        code, products = self.call({"operation": "catalog", "action": "products", "query": "havregryn"},
                                   host=lambda frame: self.host(frame, product_window_minimum=100))
        self.assertEqual(code, 0, products)
        self.assertEqual(products["result"]["products"][0]["product_id"], PRODUCT)
        self.assertEqual(products["result"]["scope"]["backend_freshness"], "unverified")
        self.assertEqual(products["result"]["scope"]["evidence_kind"], "host_attested_rendered_ui")
        self.assertEqual(self.frames[-1]["effect"], "read")

        request = {"operation": "catalog", "action": "recipes", "query": "tomatsuppe", "limit": 2}
        found = {"source_url": "https://meny.no/sok?query=tomatsuppe&expanded=recipes", "query": "tomatsuppe",
                 "authenticated": True, "ready": True, "heading_count": 1, "displayed_count": 1,
                 "empty": False, "recipes": [{"recipe_id": "/oppskrifter/suppe/tomatsuppe", "name": "Tomatsuppe"}]}

        def host(frame, result=found):
            self.assertEqual((frame["operation"], frame["effect"]), ("recipe_search", "read"))
            self.assertEqual(frame["arguments"], {"query": "tomatsuppe", "page": 1, "size": 2})
            return {"reply_to": frame["call_id"], "browser_binding": self.binding,
                    "observed_at": datetime.now(timezone.utc).isoformat(), "result": result}

        code, recipes = self.call(request, host=host)
        self.assertEqual(code, 0, recipes)
        self.assertEqual(recipes["result"]["scope"]["backend_freshness"], "unverified")
        self.assertEqual(recipes["result"]["recipes"][0]["recipe_url"], "https://meny.no/oppskrifter/suppe/tomatsuppe")
        for changed in ({"authenticated": False}, {"recipes": [], "empty": True},
                        {"source_url": "https://meny.no/sok?query=other&expanded=recipes"}):
            code, rejected = self.call(request, host=lambda frame: host(frame, {**found, **changed}))
            self.assertEqual(code, 1, rejected)
            self.assertIn("search scope", rejected["error"])
        unusable = {**found, "recipes": [{"recipe_id": "/oppskrifter/tomatsuppe%3F", "name": "Tomatsuppe"}]}
        code, rejected = self.call(request, host=lambda frame: host(frame, unusable))
        self.assertEqual(code, 1, rejected)
        self.assertIn("exact recipe search path", rejected["error"])
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

        code, listed = self.call({"operation": "shopping_list", "action": "get"})
        self.assertEqual(code, 0, listed)
        self.assertEqual(listed["result"]["items"][0]["quantity"], {"numerator": 300, "denominator": 1})
        self.assertEqual(self.frames, [])
        match = {"operation": "shopping_list", "action": "match", "list_ref": listed["result"]["list_ref"]}
        code, stale = self.call({**match, "list_ref": {}})
        self.assertEqual(code, 1, stale)
        self.assertIn("shopping list changed", stale["error"])
        self.assertEqual(self.frames, [])
        code, matched = self.call(match)
        self.assertEqual(code, 0, matched)
        self.assertIn(matched["result"]["matching_status"], {"prepared", "needs_input"})
        self.assertEqual(matched["result"]["product_result"]["menu_ref"], listed["result"]["menu_ref"])
        self.assertFalse(matched["result"]["cart_changed"])
        self.assertTrue(any(frame["operation"] == "product_search" for frame in self.frames))
        self.assertTrue(all(frame["effect"] == "read" for frame in self.frames))
        self.assertEqual(self.writes, 0)

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
        self.assertEqual(self.inspect_cart(), {"pending": True, "writes_enabled": True,
                                              "dispatch_evidence": "frame_prepared", "core_acknowledged": False})
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
        self.assertEqual(code, 1, recovered)
        self.assertIn("still uncertain", recovered["error"])
        self.assertIn("pending_cart_change", self.state())
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.quantity, 1)

    def test_saved_host_ack_recovers_managed_batch_after_restart_with_original_id(self):
        request = self.managed_request()
        command_id = str(uuid.uuid4())
        launch = f"""
import sys
sys.path.insert(0, {str(SOURCE)!r})
from clients import dots_session
original = dots_session.publish_native_record
def fail_after_ack(path, record):
    original(path, record)
    if path.name == "ack.json":
        raise OSError("fixture interrupted after saving the original host acknowledgement")
dots_session.publish_native_record = fail_after_ack
raise SystemExit(dots_session.main())
"""
        code, result = self.run_session("call", {"request_id": command_id, "request": request},
                                        launch_code=launch)
        self.assertEqual(code, 1, result)
        self.assertEqual((self.quantity, self.writes), (2, 1))
        self.assertEqual(self.inspect_cart(), {"pending": True, "writes_enabled": True,
                                              "dispatch_evidence": "host_acknowledged", "core_acknowledged": False})
        code, result = self.call({"operation": "cart", "action": "get"})
        self.assertEqual(code, 0, result)
        operation_id = result["result"]["operation_id"]
        reconcile = {"operation": "cart", "action": "reconcile_change", "operation_id": operation_id}
        code, result = self.call({**reconcile, "operation_id": "wrong-operation"})
        self.assertEqual(code, 1, result)
        self.assertFalse(self.inspect_cart()["core_acknowledged"])

        def changed_tab(frame):
            reply = self.host(frame)
            reply["browser_binding"] = {**self.binding, "tab_id": "synthetic-different-tab"}
            return reply

        code, result = self.call(reconcile, host=changed_tab)
        self.assertEqual(code, 1, result)
        self.assertTrue(self.inspect_cart()["core_acknowledged"])
        self.frames.clear()
        code, result = self.call(reconcile)
        self.assertEqual(code, 0, result)
        self.assertEqual([frame["operation"] for frame in self.frames], ["verify_new_cart", "get_cart"])
        self.assertFalse(self.inspect_cart()["pending"])
        self.assertEqual((self.quantity, self.writes), (2, 1))

    def test_unchanged_cart_cannot_retire_a_native_write_without_its_ending(self):
        def lose_reply_before_click(frame):
            if frame["operation"] == "manipulate_cart":
                return None
            return self.host(frame)

        command_id = str(uuid.uuid4())
        code, original = self.call(self.ensure(), command_id, lose_reply_before_click)
        self.assertEqual(code, 1, original)
        pending = self.state()["pending_cart_change"]
        # Older installations kept the pending cart but had no dispatch audit.
        shutil.rmtree(self.root / "state/native-cart-dispatch")
        self.assertEqual(self.inspect_cart(), {"pending": True, "writes_enabled": True,
                                              "dispatch_evidence": "no_frame_record", "core_acknowledged": False})
        self.frames.clear()
        code, readback = self.call({"operation": "cart", "action": "get"})
        self.assertEqual(code, 0, readback)
        shown = readback["result"]
        self.assertEqual(shown["count"], 0)
        self.assertEqual(shown["operation_id"], pending["operation_id"])
        self.assertTrue(shown["cart_write_pending"])
        self.assertNotIn("accept_current_available", shown)
        code, check = self.call({"operation": "cart", "action": "reconcile_change",
                                 "operation_id": pending["operation_id"]})
        self.assertEqual(code, 1, check)
        self.assertIn("the original dispatch ending and bound readback", check["error"])
        code, blocked = self.call({"operation": "cart", "action": "accept_current",
                                   "operation_id": pending["operation_id"], "cart_digest": shown["cart_digest"]})
        self.assertEqual(code, 1, blocked)
        self.assertEqual(self.call(self.ensure(2))[0], 1)
        self.assertEqual([frame["operation"] for frame in self.frames],
                         ["get_cart", "verify_new_cart", "get_cart"])
        self.assertEqual(self.call(self.ensure(), command_id), (1, original))
        for request in ({"operation": "orders"}, {"operation": "orders", "action": "get", "order_id": "123456"},
                        {"operation": "catalog", "action": "products", "query": "havregryn"}):
            with self.subTest(request=request):
                frame_count = len(self.frames)
                code, blocked = self.call(request)
                self.assertEqual(code, 1, blocked)
                self.assertIn("a cart write is uncertain", blocked["error"])
                self.assertEqual(len(self.frames), frame_count)
        self.assertEqual(self.state()["pending_cart_change"], pending)
        self.assertEqual(self.writes, 0)

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
        lifetime = (datetime.fromisoformat(frame["expires_at"])
                    - datetime.fromisoformat(frame["emitted_at"])).total_seconds()
        self.assertGreater(lifetime, 110)
        self.assertLessEqual(lifetime, 120)
        label = "fra 49 kr fra 49 kroner, 6. oktober klokka 09:00 til 12:00"
        return {"reply_to": frame["call_id"], "browser_binding": self.binding,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "result": {"authenticated": True, "ready": True,
                           "source_url": "https://meny.no/varer", "dialog_count": 1,
                           "slots": [{"slot_id": label, "display": label, "date": "2026-10-06",
                                      "start": "09:00", "end": "12:00", "selected": False}]}}

    def rendered_orders_host(self, frame, *, binding=None, max_lifetime=60):
        self.assertEqual(frame["browser_binding"], binding or self.binding)
        self.assertEqual(frame["effect"], "read")
        self.assertLessEqual((datetime.fromisoformat(frame["expires_at"])
                            - datetime.fromisoformat(frame["emitted_at"])).total_seconds(), max_lifetime)
        identity = frame["arguments"].get("order_number", "123456")
        path = f"/trumf-profil/nettbutikk/bestilling/{identity}"
        location = {"origin": "https://meny.no", "path": path,
                    "query_keys": ["archived", "mworderid"], "hash": ""}
        result = {"authenticated": True, "authenticated_count": 1, "ready": True,
                  "main_count": 1, "heading_count": 1, "source_origin": "https://meny.no"}
        if frame["operation"] == "get_orders":
            result.update(source_path="/trumf-profil/nettbutikk", source_query_keys=[],
                          source_hash="#/bestillinger", heading="Bestillinger fra de siste 6 måneder",
                          table_count=1, columns=["BESTILLINGSKODE", "STATUS", "UTLEVERING", "TID", "SUM"],
                          rendered_row_count=1, rows_complete=True, pagination_count=0,
                          orders=[{"order_number": identity, "cell_count": 5, "links": [location, dict(location)],
                                   "status_marker": "LEVERT", "delivery_display": "Synthetic delivery",
                                   "time_display": "Synthetic time", "sum_display": "80,00 kr"}])
        else:
            self.assertEqual(frame["operation"], "get_order")
            result.update(source_path=path, source_query_keys=location["query_keys"], source_hash="",
                          heading="BESTILLING SYNTHETIC", order_number=identity, status_markers=["Levert"],
                          item_heading="Bestilte varer (40)", item_count=40, item_table_count=1,
                          item_columns=["VARE", "MENGDE"], item_rows_complete=True,
                          products=[{"name": f"Synthetic item {i}", "quantity": 2} for i in range(18)]
                                   + [{"name": "Synthetic last item", "quantity": 4}],
                          amounts={"Betalt beløp (kort)": "80,00 kr"})
        return {"reply_to": frame["call_id"], "browser_binding": binding or self.binding,
                "observed_at": datetime.now(timezone.utc).isoformat(), "result": result}

    def test_rendered_orders_keep_qualification_through_core_agent_pages_and_receipts(self):
        code, policy = self.call({"operation": "native_cart_policy", "action": "set", "enabled": False,
                                 "browser_binding": self.binding})
        self.assertEqual(code, 0, policy)
        before = self.state()
        request = {"operation": "orders"}
        command_id = str(uuid.uuid4())
        code, listed = self.call(request, command_id, self.rendered_orders_host)
        self.assertEqual(code, 0, listed)
        self.assertEqual(listed["result"]["history_scope"], "rendered_last_six_months")
        self.assertEqual(len(listed["result"]["orders"]), 1)  # Two consistent links are one order.
        frames = len(self.frames)
        self.assertEqual(self.call(request, command_id, self.rendered_orders_host), (0, listed))
        self.assertEqual(len(self.frames), frames)
        code, detail = self.call({"operation": "orders", "action": "get", "order_id": "123456"},
                                 host=self.rendered_orders_host)
        self.assertEqual(code, 0, detail)
        for value in (listed["result"], listed["result"]["orders"][0], detail["result"],
                      detail["result"]["order"], detail["result"]["tracking"]):
            self.assertEqual(value["evidence_kind"], "host_attested_rendered_ui")
            self.assertEqual(value["backend_freshness"], "unverified")
        order = detail["result"]["order"]
        self.assertEqual(order["receipt_layout"], "unclassified")
        self.assertIsNone(order["order_total"])
        self.assertEqual(order["payment_status"], "unknown")
        self.assertNotIn("grossAmount", order)
        self.assertEqual(order["amount_displays"], {"Betalt beløp (kort)": "80,00 kr"})
        code, projected = self.call({"operation": "orders", "action": "get", "order_id": "123456",
                                    "response_view": "agent", "view_offset": 18, "view_limit": 1},
                                    host=self.rendered_orders_host)
        self.assertEqual(code, 0, projected)
        view = projected["result"]
        self.assertEqual(view["order_items"]["items"][0]["quantity"], 4)
        self.assertEqual(view["order_items"]["backend_freshness"], "unverified")
        self.assertEqual(view["order"]["receipt_layout"], "unclassified")
        self.assertEqual(view["order"]["tracking"]["backend_freshness"], "unverified")
        self.assertIn("unverified", view["next"])
        for key in ("profile", "menu", "pending_cart_change", "cart_plan", "native_cart_writes_enabled"):
            self.assertEqual(before.get(key), self.state().get(key))
        self.assertEqual(self.writes, 0)

    def test_rendered_orders_reject_missing_scope_identity_and_incomplete_goods(self):
        cases = [("list", {"orders": [], "rendered_row_count": 0}),
                 ("list", {"rows_complete": False}), ("list", {"pagination_count": 1}),
                 ("list", {"heading": "Loading"}), ("list", {"authenticated_count": 2}),
                 ("get", {"order_number": "987654"}), ("get", {"source_query_keys": ["edit"]}),
                 ("get", {"item_count": 41, "item_heading": "Bestilte varer (41)"}),
                 ("get", {"item_rows_complete": False}), ("get", {"status_markers": ["Levert", "Bekreftet"]}),
                 ("get", {"amounts": {"Reservert beløp": "80,00 kr"}})]
        for action, changes in cases:
            with self.subTest(action=action, changes=changes):
                def changed(frame):
                    reply = self.rendered_orders_host(frame)
                    reply["result"].update(changes)
                    return reply
                request = {"operation": "orders", "action": action}
                if action == "get":
                    request["order_id"] = "123456"
                code, result = self.call(request, host=changed)
                self.assertEqual(code, 1, result)
        def conflicting_link(frame):
            reply = self.rendered_orders_host(frame)
            reply["result"]["orders"][0]["links"][1]["path"] = "/trumf-profil/nettbutikk/bestilling/987654"
            return reply
        self.assertEqual(self.call({"operation": "orders"}, host=conflicting_link)[0], 1)
        self.assertEqual(self.writes, 0)
        self.assertIsNone(self.state().get("pending_cart_change"))

    def current_order_layout_host(self, frame, **kwargs):
        response = self.rendered_orders_host(frame, **kwargs)
        details = response["result"]
        if frame["operation"] == "get_orders":
            details["columns"].append("")
            details["table_class"] = "ws-order-history-table"
            details["orders"][0].update(cell_count=6, receipt_cell_class="ws-order-history-table__receipt",
                                        receipt_label="Se kvittering", same_archived_url=True)
        else:
            details.update(source_archived=True, order_number_label="Ordrenummer", heading_matches_pickup_code=True,
                           item_table_class="ws-order-details-cart", item_disclosure_expanded=True)
        return response

    def prepare_legacy_pending_cart(self, *, command_id=None, dispatched=False, retain_dispatch=False):
        def lose_reply(frame):
            if frame["operation"] == "manipulate_cart":
                if dispatched:
                    self.host(frame)
                return None
            return self.host(frame)

        code, lost = self.call(self.ensure(), command_id, host=lose_reply)
        self.assertEqual(code, 1, lost)
        # This fixture represents source from before it could mark new native writes.
        saved = self.state()
        saved.pop("native_cart_dispatch_protocol")
        (self.root / "state" / "state.json").write_text(json.dumps(saved))
        if not retain_dispatch:
            shutil.rmtree(self.root / "state/native-cart-dispatch")
        code, disabled = self.call({"operation": "native_cart_policy", "action": "set", "enabled": False,
                                    "browser_binding": self.binding})
        self.assertEqual(code, 0, disabled)
        self.frames.clear()
        return self.inspect_cart()

    def replacement_observation_host(self, frame, *, tab_id="synthetic-observation-tab"):
        target = {"origin": self.binding["origin"], "browser_id": self.binding["browser_id"],
                  "tab_id": tab_id}
        identity = {key: self.binding[key] for key in ("account_sha256", "cart_context_sha256")}
        self.assertEqual(frame["browser_binding"], target)
        self.assertEqual(frame["effect"], "read")
        self.assertEqual(frame["scope"], "current_account_observation")
        self.assertNotIn("before_quantities", frame)
        self.assertNotIn("expected_quantities", frame)
        if frame["operation"] == "verify_observation_context":
            return {"reply_to": frame["call_id"], "browser_binding": target,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "result": {**identity, "authenticated": True, "new_cart": True}}
        if frame["operation"] == "get_cart":
            reply = {"reply_to": frame["call_id"], "browser_binding": target,
                     "observed_at": datetime.now(timezone.utc).isoformat(),
                     "result": {"source_url": "https://meny.no/varer", "new_cart": True, "snapshot": self.snapshot()}}
        else:
            reply = self.current_order_layout_host(frame, binding=target, max_lifetime=120)
        reply["result"] = {"verified_context": {**identity, "account_verified_before_and_after": True},
                           "observation": reply["result"]}
        return reply

    def observe_current(self, request, *, tab_id="synthetic-observation-tab", host=None):
        return self.run_session("observe", {"tab_id": tab_id, "request": request},
                                host or self.replacement_observation_host)

    def test_replacement_observation_preserves_the_original_cart_and_orders_guard(self):
        status = self.prepare_legacy_pending_cart()
        self.assertEqual(status, {"pending": True, "writes_enabled": False,
                                  "dispatch_evidence": "no_frame_record", "core_acknowledged": False,
                                  "legacy_reset_digest": status["legacy_reset_digest"]})
        before = self.files()
        registry = self.home / ".meal-concierge-dots-targets"
        owners = {path.name: path.read_bytes() for path in registry.glob("*.json")}
        code, cart = self.observe_current({"operation": "cart", "action": "get"})
        self.assertEqual(code, 0, cart)
        self.assertEqual(cart["result"]["cart"]["count"], 0)
        self.assertNotIn("cart_digest", cart["result"])
        code, orders = self.observe_current({"operation": "orders", "action": "list", "include_first_receipt": True})
        self.assertEqual(code, 0, orders)
        result = orders["result"]
        self.assertEqual(result["history"]["rendered_row_count"], 1)
        self.assertEqual(result["first_receipt"]["order_number"], result["history"]["orders"][0]["order_number"])
        self.assertEqual(result["first_receipt"]["receipt_layout"], "archived")
        for observation in (cart["result"], result):
            self.assertEqual(observation["pending_cart_guard"],
                             {key: value for key, value in status.items() if key != "legacy_reset_digest"})
            self.assertEqual(observation["observation_scope"], "current_account")
            self.assertEqual(observation["backend_freshness"], "unverified")
        self.assertEqual([frame["operation"] for frame in self.frames],
                         ["verify_observation_context", "get_cart", "verify_observation_context", "get_orders", "get_order"])
        self.assertEqual(self.frames[-2]["arguments"], {"page": 1, "size": 1})
        self.assertEqual(self.files(), before)
        self.assertEqual({path.name: path.read_bytes() for path in registry.glob("*.json")}, owners)
        self.assertEqual(self.inspect_cart(), status)
        code, denied = self.call({"operation": "orders", "action": "list"})
        self.assertEqual(code, 1, denied)
        self.assertIn("a cart write is uncertain", denied["error"])
        self.assertEqual(self.writes, 0)

    def reset_request(self, status):
        return {"tab_id": "synthetic-observation-tab", "legacy_reset_digest": status["legacy_reset_digest"],
                "accept_unknown_dispatch": True, "original_executor_retired": True}

    def run_reset(self, request, host=None, **kwargs):
        return self.run_session("reset-legacy-cart", request, host or self.replacement_observation_host, **kwargs)

    def test_legacy_reset_reopens_household_and_archives_a_dispatched_lost_reply(self):
        old_apply = self.managed_request()
        old_product_plan_ref = self.prepared_ref
        old_id = str(uuid.uuid4())
        status = self.prepare_legacy_pending_cart(command_id=old_id, dispatched=True)
        self.assertEqual((self.quantity, self.writes), (1, 1))
        self.quantity = 0  # A later cart change makes the current empty cart ambiguous about the old attempt.
        old_state = self.state()
        before = self.files()
        registry = self.home / ".meal-concierge-dots-targets"
        owners = {path.name: path.read_bytes() for path in registry.glob("*.json")}
        request = self.reset_request(status)
        old_policy_id = str(uuid.uuid4())
        old_policy = {"operation": "native_cart_policy", "action": "set", "enabled": False,
                      "browser_binding": self.binding}
        _, old_policy_result = self.call(old_policy, old_policy_id)
        incomplete_id = str(uuid.uuid4())
        self.call({"operation": "health"}, incomplete_id)
        (self.root / "calls" / incomplete_id / "result.json").unlink()
        code, reset = self.run_reset(request)
        self.assertEqual(code, 0, reset)
        self.assertEqual(reset["result"]["status"], "archived_unknown")
        self.assertEqual(reset["result"]["historical_dispatch"], "unknown")
        self.assertFalse(reset["result"]["writes_enabled_at_reset"])
        self.assertEqual([frame["operation"] for frame in self.frames], ["verify_observation_context", "get_cart"])
        self.assertEqual(self.writes, 1)
        after = self.state()
        self.assertNotIn("pending_cart_change", after)
        self.assertEqual(after["native_cart_legacy_reset"]["pending_cart_change"], old_state["pending_cart_change"])
        self.assertNotEqual(after["native_config_sha256"], old_state["native_config_sha256"])
        self.assertNotEqual(after["product_selection_generation"], old_state.get("product_selection_generation"))
        self.assertEqual(after["menu"], old_state["menu"])
        self.assertEqual(after["profile"], old_state["profile"])
        self.assertNotIn(self.prepared_ref, after["menu_planning"]["prepared"])
        current = self.files()
        for name, data in before.items():
            if name != "state/state.json":
                self.assertEqual(current[name], data, name)
        current_owners = {path.name: path.read_bytes() for path in registry.glob("*.json")}
        self.assertEqual(len(current_owners), len(owners) + 1)
        self.assertEqual({name: current_owners[name] for name in owners}, owners)
        self.frames.clear()
        self.assertEqual(self.call(old_policy, old_policy_id), (0, old_policy_result))
        old_result = json.loads(before[f"calls/{old_id}/result.json"])
        self.assertEqual(self.call(self.ensure(), old_id), (1, {"kind": "core_result", **old_result}))
        self.assertEqual(self.call({"operation": "health"}, incomplete_id)[1]["status"], "incomplete")
        self.assertEqual(self.run_reset(request)[1]["result"], {**reset["result"], "idempotent": True})
        self.assertEqual(self.run_reset({**request, "tab_id": "another-tab"})[0], 1)
        self.assertEqual(self.call({**old_policy, "enabled": True})[0], 1)
        self.assertEqual(self.call(self.ensure())[0], 1)
        cleanup = {"operation": "cart", "action": "reconcile_change"}
        self.assertEqual(self.call(cleanup)[1]["result"],
                         {"reconciled": False, "cart_write_pending": False, "status": "no_pending"})
        code, unknown = self.call({**cleanup, "operation_id": old_state["pending_cart_change"]["operation_id"]})
        self.assertEqual(code, 0, unknown)
        self.assertEqual(unknown["result"], {"reconciled": False, "cart_write_pending": False,
                                            "status": "archived_unknown", "historical_dispatch": "unknown"})
        for wrong in (old_id, request["legacy_reset_digest"]):
            code, unmatched = self.call({"operation": "cart", "action": "reconcile_change", "operation_id": wrong})
            self.assertEqual(code, 1, unmatched)
            self.assertIn("no current or recorded MENY cart operation", unmatched["error"])
        self.assertEqual(self.frames, [])
        self.binding = {**self.binding, "tab_id": request["tab_id"]}
        cart_command = str(uuid.uuid4())
        cart_request = {"operation": "cart", "action": "get"}
        code, empty = self.call(cart_request, cart_command)
        self.assertEqual(code, 0, empty)
        self.assertEqual(empty["result"]["count"], 0)
        self.frames.clear()
        self.assertEqual(self.call(cart_request, cart_command), (0, empty))
        self.assertEqual(self.call({**old_policy, "enabled": True, "browser_binding": self.binding})[0], 0)
        self.assertEqual(self.call(old_apply)[0], 1)
        self.assertEqual(self.call({"operation": "products", "action": "apply", "cart_change_requested": True,
                                    "product_plan_ref": old_product_plan_ref,
                                    "product_plan_digest": old_apply["product_plan_digest"]})[0], 1)
        self.assertEqual(self.frames, [])
        menu = after["menu"]
        prepare = {"operation": "products", "action": "prepare",
                   "menu_ref": {key: menu[key] for key in ("menu_id", "revision", "digest")},
                   "candidate_approvals": old_apply["candidate_approvals"]}
        code, fresh = self.call(prepare)
        self.assertEqual(code, 0, fresh)
        code, applied = self.call({"operation": "products", **fresh["result"]["apply_arguments"],
                                   "cart_change_requested": True})
        self.assertEqual(code, 0, applied)
        self.assertTrue(applied["result"]["applied"])
        self.assertEqual((self.quantity, self.writes), (3, 3))

    def test_legacy_reset_refuses_unsafe_baselines_without_removing_the_guard(self):
        status = self.prepare_legacy_pending_cart()
        request = self.reset_request(status)
        for changed in ({"legacy_reset_digest": "0" * 64}, {"accept_unknown_dispatch": False},
                        {"original_executor_retired": False}, {"tab_id": self.binding["tab_id"]}):
            self.assertEqual(self.run_reset({**request, **changed})[0], 1)
        self.assertEqual(self.frames, [])
        before = self.files()
        for unsafe in ("wrong_account", "expired", "changed_tab", "edit", "delivery", "nonempty"):
            with self.subTest(unsafe=unsafe):
                self.quantity = int(unsafe == "nonempty")

                def host(frame):
                    reply = self.replacement_observation_host(frame)
                    if frame["operation"] == "get_cart":
                        observation = reply["result"]["observation"]
                        if unsafe == "delivery":
                            observation["snapshot"].update(delivery_count=1, delivery={"display": "I dag, 18:00 - 20:00"})
                        elif unsafe == "edit":
                            observation["new_cart"] = False
                        elif unsafe == "expired":
                            reply["observed_at"] = frame["expires_at"]
                        elif unsafe == "changed_tab":
                            reply["browser_binding"] = self.binding
                        elif unsafe == "wrong_account":
                            reply["result"]["verified_context"]["account_sha256"] = "c" * 64
                    return reply

                self.assertEqual(self.run_reset(request, host)[0], 1)
                self.assertEqual(self.files(), before)
                self.assertEqual(self.inspect_cart(), status)
        self.assertEqual(self.writes, 0)

    def test_legacy_reset_retries_before_and_after_atomic_publication(self):
        self.prepare_legacy_pending_cart()
        for failure in ("owner_link", "before_state", "after_replace"):
            with self.subTest(failure=failure):
                status = self.inspect_cart()
                request = self.reset_request(status)
                before = self.state()
                launch_code = f"""
import stat, sys
sys.path.insert(0, {str(SOURCE)!r})
import core
from clients import dots_session as client
original = core._atomic_json
original_link = client.os.link
original_replace = client.os.replace
original_fsync = client.os.fsync
failure = {failure!r}
renamed_state = False
def link(source, target, **kwargs):
    if failure == 'owner_link' and target.parent.name == '.meal-concierge-dots-targets':
        raise OSError('simulated interruption during owner publication')
    return original_link(source, target, **kwargs)
def replace(source, target):
    global renamed_state
    original_replace(source, target)
    renamed_state = failure == 'after_replace' and target.name == 'state.json'
def fsync(descriptor):
    if renamed_state and stat.S_ISDIR(client.os.fstat(descriptor).st_mode):
        raise OSError('simulated interruption before state directory sync')
    return original_fsync(descriptor)
def interrupted(path, value):
    if failure == 'before_state' and path.name == 'state.json' and 'native_cart_legacy_reset' in value:
        raise OSError('simulated interruption during state publication')
    original(path, value)
client.os.link = link
client.os.replace = replace
client.os.fsync = fsync
core._atomic_json = interrupted
raise SystemExit(client.main())
"""
                code, stopped = self.run_reset(request, launch_code=launch_code)
                self.assertEqual(code, 1, stopped)
                self.assertIn("simulated interruption", stopped["error"])
                if failure != "after_replace":
                    self.assertEqual(self.state(), before)
                    self.assertEqual(self.inspect_cart(), status)
                if failure == "before_state":
                    self.frames.clear()
                    self.assertEqual(self.run_reset({**request, "tab_id": "different-tab"})[0], 1)
                    self.assertEqual(self.call(self.ensure())[0], 1)
                    self.assertEqual(self.frames, [])
                if failure == "after_replace":
                    self.frames.clear()
                    fail_sync = f"""
import sys
sys.path.insert(0, {str(SOURCE)!r})
from clients import dots_session as client
def fail_sync(path):
    raise OSError('cannot confirm state durability')
client.sync_native_record = fail_sync
raise SystemExit(client.main())
"""
                    failed_code, failed_retry = self.run_reset(request, launch_code=fail_sync)
                    self.assertEqual(failed_code, 1, failed_retry)
                    self.assertIn("cannot confirm state durability", failed_retry["error"])
                    code, retry = self.run_reset(request)
                    self.assertEqual(code, 0, retry)
                    self.assertTrue(retry["result"]["idempotent"])
                    self.assertEqual(self.frames, [])
        self.assertEqual(self.writes, 0)

    def test_legacy_reset_detects_missing_intent_or_explicit_write_policy(self):
        prior = self.state()
        request = self.reset_request(self.prepare_legacy_pending_cart())
        self.assertEqual(self.run_reset(request)[0], 0)
        self.frames.clear()
        cart = {"operation": "cart", "action": "get"}
        state_file = self.root / "state" / "state.json"
        saved = self.state()
        state_file.write_text(json.dumps(prior))  # Restore state alone while leaving the durable reset intent.
        code, declined = self.call(self.ensure())
        self.assertEqual(code, 1, declined)
        self.assertIn("legacy reset must be recovered", declined["error"])
        code, declined = self.run_reset(request)
        self.assertEqual(code, 1, declined)
        self.assertIn("archive is missing", declined["error"])
        state_file.write_text(json.dumps({key: value for key, value in saved.items()
                                          if key != "native_cart_writes_enabled"}))
        self.assertEqual(self.call(cart)[0], 1)
        state_file.write_text(json.dumps(saved))
        (self.root / "state" / "native-cart-legacy-reset-intent.json").unlink()
        self.assertEqual(self.call(cart)[0], 1)
        self.assertEqual(self.run_reset(request)[0], 1)
        self.assertEqual(self.frames, [])

    def test_legacy_reset_requires_exact_cart_batch_and_no_unattributed_dispatch_record(self):
        status = self.prepare_legacy_pending_cart(retain_dispatch=True)
        self.assertEqual(status["dispatch_evidence"], "frame_prepared")
        state = self.state()
        state_file = self.root / "state" / "state.json"
        pending = state["pending_cart_change"]
        state_file.write_text(json.dumps({**state, "pending_cart_change": {**pending, "cart_only": True}}))
        self.assertEqual(self.inspect_cart()["dispatch_evidence"], "no_frame_record")
        self.assertNotIn("legacy_reset_digest", self.inspect_cart())
        shutil.rmtree(self.root / "state" / "native-cart-dispatch")
        for changed in ({"operations": [{}]}, {"expected": {}}, {"stopped_before_next_click": True},
                        {"clear_requested": True, "clear_id": "b" * 32}, {"cart_only": 1},
                        {"before": {**pending["before"], "invalid": 1},
                         "expected": {**pending["expected"], "invalid": 1}}):
            with self.subTest(changed=changed):
                state_file.write_text(json.dumps({**state, "pending_cart_change": {**pending, **changed}}))
                self.assertNotIn("legacy_reset_digest", self.inspect_cart())
        self.assertEqual(self.frames, [])

    def test_legacy_reset_reconcile_retry_reports_a_recorded_cart_drift(self):
        request = self.reset_request(self.prepare_legacy_pending_cart())
        self.assertEqual(self.run_reset(request)[0], 0)
        self.binding = {**self.binding, "tab_id": request["tab_id"]}
        self.assertEqual(self.call({"operation": "native_cart_policy", "action": "set", "enabled": True,
                                    "browser_binding": self.binding})[0], 0)

        def drift_after_dispatch(frame):
            result = self.host(frame)
            if frame["operation"] == "manipulate_cart":
                self.quantity += 1  # A later cart change obscures the result of the acknowledged batch.
            return result

        self.assertEqual(self.call(self.ensure(), host=drift_after_dispatch)[0], 1)
        operation_id = self.state()["pending_cart_change"]["operation_id"]
        reconcile = {"operation": "cart", "action": "reconcile_change", "operation_id": operation_id}
        code, first = self.call(reconcile)
        self.assertEqual(code, 0, first)
        self.assertEqual(first["result"]["status"], "needs_input")
        self.assertFalse(first["result"]["reconciled"])
        self.frames.clear()
        code, again = self.call(reconcile)
        self.assertEqual(code, 0, again)
        self.assertEqual(again["result"], {"reconciled": False, "cart_write_pending": False,
                                           "status": "completed_with_cart_drift"})
        self.assertEqual(self.frames, [])
        self.assertEqual(self.writes, 1)

    def test_legacy_reset_refuses_a_new_write_even_if_dispatch_files_are_missing(self):
        def lose_reply(frame):
            return None if frame["effect"] == "cart_write" else self.host(frame)

        self.assertEqual(self.call(self.ensure(), host=lose_reply)[0], 1)
        shutil.rmtree(self.root / "state" / "native-cart-dispatch")
        self.assertEqual(self.call({"operation": "native_cart_policy", "action": "set", "enabled": False,
                                    "browser_binding": self.binding})[0], 0)
        self.assertEqual(self.state()["native_cart_dispatch_protocol"], 1)
        status = self.inspect_cart()
        self.assertEqual(status["dispatch_evidence"], "no_frame_record")
        self.assertNotIn("legacy_reset_digest", status)

    def host_pre_click_stop(self, frame):
        if frame["effect"] != "cart_write":
            return self.host(frame)
        result = {"dispatched": False, "stopped_before_first_click": True, "no_pending_clicks": True}
        self.assertEqual(frame["stop_before_first_click_result"], result)
        self.assertEqual(frame["operation_id"], self.state()["pending_cart_change"]["operation_id"])
        return {"reply_to": frame["call_id"], "browser_binding": self.binding,
                "observed_at": datetime.now(timezone.utc).isoformat(), "result": result}

    def test_native_cart_stop_before_click_preserves_original_quantities_and_receipt(self):
        code, ended = self.call(self.ensure(), host=self.host_pre_click_stop)
        self.assertEqual(code, 0, ended)
        self.assertFalse(ended["result"]["ensured"])
        self.assertEqual(ended["result"]["status"], "not_dispatched")
        self.assertNotIn("pending_cart_change", self.state())
        operation = ended["result"]["operation_id"]
        self.assertEqual(self.state()["cart_change_receipts"][operation]["status"], "not_dispatched")
        self.frames.clear()
        code, known = self.call({"operation": "cart", "action": "reconcile_change", "operation_id": operation})
        self.assertEqual(code, 0, known)
        self.assertEqual(known["result"], {"reconciled": False, "cart_write_pending": False, "status": "not_dispatched"})
        self.assertEqual(self.call({"operation": "cart", "action": "reconcile_change"})[1]["result"],
                         {"reconciled": False, "cart_write_pending": False, "status": "no_pending"})
        self.assertEqual(self.frames, [])
        self.assertEqual((self.quantity, self.writes), (0, 0))

    def test_native_cart_stop_must_be_timely_and_certain(self):
        def late(frame):
            reply = self.host_pre_click_stop(frame)
            if frame["effect"] == "cart_write":
                reply["observed_at"] = frame["expires_at"]
            return reply

        self.assertEqual(self.call(self.ensure(), host=late)[0], 1)
        self.assertEqual(self.inspect_cart()["dispatch_evidence"], "frame_prepared")
        self.assertIn("pending_cart_change", self.state())
        self.assertEqual((self.quantity, self.writes), (0, 0))

    def test_native_cart_stop_durable_before_interruption_reconciles_without_frames(self):
        launch_code = f"""
import sys
sys.path.insert(0, {str(SOURCE)!r})
from clients import dots_session as client
publish = client.publish_native_record
def interrupted(path, record):
    publish(path, record)
    if record.get('outcome') == 'not_dispatched':
        raise OSError('interrupted after the original pre-click ending')
client.publish_native_record = interrupted
raise SystemExit(client.main())
"""
        code, stopped = self.run_session("call", {"request_id": str(uuid.uuid4()), "request": self.ensure()},
                                         self.host_pre_click_stop, launch_code=launch_code)
        self.assertEqual(code, 1, stopped)
        self.assertEqual(self.inspect_cart()["dispatch_evidence"], "host_stopped_before_click")
        operation = self.state()["pending_cart_change"]["operation_id"]
        self.frames.clear()
        code, bad = self.call({"operation": "cart", "action": "reconcile_change", "operation_id": "different"})
        self.assertEqual(code, 1, bad)
        self.assertEqual(self.inspect_cart()["dispatch_evidence"], "host_stopped_before_click")
        fail_sync = f"""
import sys
sys.path.insert(0, {str(SOURCE)!r})
from clients import dots_session as client
def fail(path):
    raise OSError('could not sync original ending')
client.sync_native_record = fail
raise SystemExit(client.main())
"""
        request = {"operation": "cart", "action": "reconcile_change", "operation_id": operation}
        code, unsynced = self.run_session("call", {"request_id": str(uuid.uuid4()), "request": request},
                                         launch_code=fail_sync)
        self.assertEqual(code, 1, unsynced)
        self.assertIn("could not sync original ending", unsynced["error"])
        self.assertIn("pending_cart_change", self.state())
        code, recovered = self.call(request)
        self.assertEqual(code, 0, recovered)
        self.assertEqual(recovered["result"], {"reconciled": False, "cart_write_pending": False,
                                               "status": "not_dispatched"})
        self.assertEqual(self.state()["cart_change_receipts"][operation]["status"], "not_dispatched")
        self.assertNotIn("pending_cart_change", self.state())
        self.assertEqual(self.frames, [])
        self.assertEqual((self.quantity, self.writes), (0, 0))

    def test_native_cart_stop_after_prior_batch_reports_partial_clear(self):
        self.quantity = 3
        code, before = self.call({"operation": "cart", "action": "get"})
        self.assertEqual(code, 0, before)

        def after_first_batch(frame):
            return self.host_pre_click_stop(frame) if self.writes else self.host(frame)

        code, stopped = self.call({"operation": "cart", "action": "clear", "cart_digest": before["result"]["cart_digest"]},
                                  host=after_first_batch)
        self.assertEqual(code, 0, stopped)
        self.assertFalse(stopped["result"]["cleared"])
        self.assertEqual(stopped["result"]["status"], "partially_applied")
        self.assertEqual(stopped["result"]["batch_status"], "not_dispatched")
        self.assertEqual(stopped["result"]["completed_batches"], 1)
        self.assertEqual((self.quantity, self.writes), (1, 1))

    def test_native_cart_stop_result_lost_after_core_commit_is_recoverable_by_frame_operation(self):
        launch_code = f"""
import sys
sys.path.insert(0, {str(SOURCE)!r})
from clients import dots_session as client
publish = client.dots.exclusive_json
def interrupted(path, record, maximum):
    if path.name == 'result.json':
        raise OSError('interrupted before publishing the command result')
    publish(path, record, maximum)
client.dots.exclusive_json = interrupted
raise SystemExit(client.main())
"""
        original_id = str(uuid.uuid4())
        code, stopped = self.run_session("call", {"request_id": original_id, "request": self.ensure()},
                                         self.host_pre_click_stop, launch_code=launch_code)
        self.assertEqual(code, 1, stopped)
        operation = self.frames[-1]["operation_id"]
        self.assertNotIn("pending_cart_change", self.state())
        self.frames.clear()
        self.assertEqual(self.call(self.ensure(), original_id)[1]["status"], "incomplete")
        code, receipt = self.call({"operation": "cart", "action": "reconcile_change", "operation_id": operation})
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["result"]["status"], "not_dispatched")
        self.assertFalse(receipt["result"]["reconciled"])
        self.assertEqual(self.frames, [])
        self.assertEqual((self.quantity, self.writes), (0, 0))

    def test_native_cart_accept_current_archives_original_removed_test_unit_as_unknown(self):
        reset = self.reset_request(self.prepare_legacy_pending_cart())
        self.assertEqual(self.run_reset(reset)[0], 0)
        legacy_archive = self.state()["native_cart_legacy_reset"]
        self.binding = {**self.binding, "tab_id": reset["tab_id"]}
        policy = {"operation": "native_cart_policy", "action": "set", "browser_binding": self.binding}
        self.assertEqual(self.call({**policy, "enabled": True})[0], 0)
        self.quantity = 1
        code, initial = self.call({"operation": "cart", "action": "get"})
        self.assertEqual(code, 0, initial)
        original_id = str(uuid.uuid4())
        clear = {"operation": "cart", "action": "clear", "cart_digest": initial["result"]["cart_digest"],
                 "response_view": "agent", "view_offset": 0, "view_limit": 2, "view_section": "items"}

        def stop_with_original_generic_error(frame):
            if frame["effect"] != "cart_write":
                return self.host(frame)
            return {"reply_to": frame["call_id"], "browser_binding": self.binding,
                    "observed_at": datetime.now(timezone.utc).isoformat(), "error": "synthetic readiness failure"}

        code, failed = self.call(clear, original_id, stop_with_original_generic_error)
        self.assertEqual(code, 1, failed)
        self.assertIn("native host operation failed", failed["error"])
        self.assertEqual(self.call({**policy, "enabled": False})[0], 0)
        pending = self.state()["pending_cart_change"]
        self.quantity = 0  # Separate, owner-authorized UI cleanup, outside Meal Concierge.
        code, fresh = self.call({"operation": "cart", "action": "get"})
        self.assertEqual(code, 0, fresh)
        request = {"operation": "cart", "action": "accept_current", "operation_id": pending["operation_id"],
                   "original_request_id": original_id, "cart_digest": fresh["result"]["cart_digest"],
                   "accept_unknown_dispatch": True, "original_executor_retired": True}

        def observed(frame):
            return self.replacement_observation_host(frame, tab_id=self.binding["tab_id"])

        self.frames.clear()
        self.assertEqual(self.call(request, host=observed)[0], 1)  # Still within the original write window.
        self.assertEqual(self.frames, [])
        frame_path, = (self.root / "state" / "native-cart-dispatch").glob("*/frame.json")
        frame = json.loads(frame_path.read_bytes())
        now = datetime.now(timezone.utc)
        frame.update(emitted_at=(now - timedelta(minutes=2)).isoformat(),
                     expires_at=(now - timedelta(minutes=1)).isoformat())
        frame_path.write_text(json.dumps(frame))  # The later recovery runs after the original frame expired.
        originals = self.files()
        for changed in ({"original_request_id": str(uuid.uuid4())}, {"operation_id": "different"},
                        {"accept_unknown_dispatch": False}, {"original_executor_retired": False}):
            self.assertEqual(self.call({**request, **changed}, host=observed)[0], 1)
            self.assertEqual(self.frames, [])
        self.quantity = 1
        self.assertEqual(self.call(request, host=observed)[0], 1)
        self.assertEqual(self.state()["pending_cart_change"], pending)
        self.quantity = 0
        self.frames.clear()
        request_id = str(uuid.uuid4())
        code, accepted = self.call(request, request_id, observed)
        self.assertEqual(code, 0, accepted)
        self.assertEqual(accepted["result"]["historical_dispatch"], "unknown")
        self.assertEqual([frame["operation"] for frame in self.frames], ["verify_observation_context", "get_cart"])
        self.assertFalse(self.inspect_cart()["writes_enabled"])
        self.assertNotIn("pending_cart_change", self.state())
        self.assertEqual(self.state()["native_cart_legacy_reset"], legacy_archive)
        archived = self.state()["cart_change_receipts"][pending["operation_id"]]
        self.assertEqual(archived["original_operation"], pending)
        self.assertEqual(archived["dispatch_outcome"], "unknown")
        self.assertEqual(archived["native_owner_review"]["frame"], frame)
        for name, contents in originals.items():
            if name != "state/state.json":
                self.assertEqual(self.files()[name], contents, name)
        self.frames.clear()
        self.assertEqual(self.call(clear, original_id), (1, failed))
        self.assertEqual(self.call(request, request_id), (0, accepted))
        code, recovered = self.call({"operation": "cart", "action": "reconcile_change",
                                     "operation_id": pending["operation_id"]})
        self.assertEqual(code, 0, recovered)
        self.assertEqual(recovered["result"], {"status": "owner_accepted_cart", "historical_dispatch": "unknown",
                                              "cart_write_pending": False, "reconciled": False})
        self.assertEqual(self.call({"operation": "cart", "action": "reconcile_change"})[1]["result"],
                         {"reconciled": False, "cart_write_pending": False, "status": "no_pending"})
        self.assertEqual(self.frames, [])
        self.assertEqual(self.writes, 0)

    def test_legacy_reset_refuses_dispatch_evidence_and_an_owned_replacement(self):
        status = self.prepare_legacy_pending_cart(retain_dispatch=True)
        self.assertEqual(status["dispatch_evidence"], "frame_prepared")
        self.assertNotIn("legacy_reset_digest", status)
        saved = self.state()
        attempt, = (self.root / "state" / "native-cart-dispatch").iterdir()
        (attempt / "frame.json").unlink()
        (attempt / "ack.json").write_text("{}")
        self.assertEqual(self.inspect_cart()["dispatch_evidence"], "no_frame_record")
        self.assertNotIn("legacy_reset_digest", self.inspect_cart())
        shutil.rmtree(attempt.parent)
        request = self.reset_request(self.inspect_cart())
        attempt.mkdir(parents=True, mode=0o700)
        (attempt / "ack.json").write_text("{}")
        self.assertEqual(self.run_reset(request)[0], 1)
        shutil.rmtree(attempt.parent)
        state_file = self.root / "state" / "state.json"
        state_file.write_text(json.dumps({**saved, "pending_checkout": {"status": "awaiting_confirmation"}}))
        self.assertEqual(self.run_reset(request)[0], 1)
        state_file.write_text(json.dumps(saved))
        root = self.root
        self.root = root.with_name("other-core")
        try:
            binding = {**self.binding, "tab_id": request["tab_id"], "account_sha256": "c" * 64}
            self.assertEqual(self.run_session("init", {**self.config, "browser_binding": binding})[0], 0)
        finally:
            self.root = root
        code, rejected = self.run_reset(request)
        self.assertEqual(code, 1, rejected)
        self.assertIn("already belongs", rejected["error"])
        self.assertEqual(self.frames, [])
        self.assertEqual(self.state(), saved)

    def test_replacement_observation_rejects_privilege_and_context_drift(self):
        cart = {"operation": "cart", "action": "get"}
        self.assertEqual(self.observe_current(cart)[0], 1)  # An ordinary household uses call.
        self.assertEqual(self.frames, [])
        status = self.prepare_legacy_pending_cart()
        before = self.files()
        for request in (self.ensure(), {"operation": "cart", "action": "accept_current"},
                        {"operation": "cart", "action": "reconcile_change"},
                        {"operation": "orders", "action": "get", "order_id": "123456"},
                        {"operation": "orders", "action": "list", "include_first_receipt": 1}):
            with self.subTest(request=request):
                self.assertEqual(self.observe_current(request)[0], 1)
        self.assertEqual(self.observe_current(cart, tab_id=self.binding["tab_id"])[0], 1)
        self.assertEqual(self.frames, [])

        def wrong_before_read(frame):
            reply = self.replacement_observation_host(frame)
            reply["result"]["account_sha256"] = "c" * 64
            return reply

        code, refused = self.observe_current({"operation": "orders", "action": "list"}, host=wrong_before_read)
        self.assertEqual(code, 1, refused)
        self.assertNotIn("result", refused)
        self.assertEqual([frame["operation"] for frame in self.frames], ["verify_observation_context"])
        for invalid in ("account", "cart", "lifetime", "tab", "context_not_verified", "order_edit", "oversized_total"):
            with self.subTest(invalid=invalid):
                def wrong(frame):
                    reply = self.replacement_observation_host(frame)
                    if frame["operation"] == "verify_observation_context":
                        return reply
                    context = reply["result"]["verified_context"]
                    if invalid in {"account", "cart"}:
                        context["account_sha256" if invalid == "account" else "cart_context_sha256"] = "c" * 64
                    elif invalid == "lifetime":
                        reply["observed_at"] = frame["expires_at"]
                    elif invalid == "tab":
                        reply["browser_binding"] = self.binding
                    elif invalid == "context_not_verified":
                        context["account_verified_before_and_after"] = 1
                    elif invalid == "order_edit":
                        reply["result"]["observation"]["new_cart"] = False
                    else:
                        reply["result"]["observation"]["snapshot"]["total"] = 10**400
                    return reply

                code, output = self.observe_current(cart, host=wrong)
                self.assertEqual(code, 1, output)
                self.assertNotIn("result", output)
                if invalid == "oversized_total":
                    self.assertIn("numbers exceed the supported range", output["error"])
        for invalid in ("another_order", "unclassified_receipt"):
            with self.subTest(invalid=invalid):
                def wrong_detail(frame):
                    reply = self.replacement_observation_host(frame)
                    if frame["operation"] == "get_order":
                        detail = reply["result"]["observation"]
                        if invalid == "another_order":
                            detail["order_number"] = "987654"
                        else:
                            for key in ("source_archived", "order_number_label", "heading_matches_pickup_code",
                                        "item_table_class", "item_disclosure_expanded"):
                                del detail[key]
                    return reply

                code, output = self.observe_current({"operation": "orders", "action": "list", "include_first_receipt": True},
                                                    host=wrong_detail)
                self.assertEqual(code, 1, output)
                self.assertNotIn("result", output)
        self.assertEqual(self.inspect_cart(), status)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.writes, 0)

    def test_replacement_observation_refuses_owned_tab_and_conflicting_checkout_without_frames(self):
        self.prepare_legacy_pending_cart()
        root = self.root
        self.root = root.with_name("other-core")
        try:
            binding = {**self.binding, "tab_id": "synthetic-observation-tab", "account_sha256": "c" * 64}
            code, result = self.run_session("init", {**self.config, "browser_binding": binding})
            self.assertEqual(code, 0, result)
        finally:
            self.root = root
        cart = {"operation": "cart", "action": "get"}
        before = self.files()
        code, owned = self.observe_current(cart)
        self.assertEqual(code, 1, owned)
        self.assertIn("already belongs", owned["error"])
        self.assertEqual(self.files(), before)
        state_file = root / "state/state.json"
        state_file.write_text(json.dumps({**self.state(), "pending_checkout": {"status": "awaiting_confirmation"}}))
        before = self.files()
        code, protected = self.observe_current(cart, tab_id="synthetic-unused-tab")
        self.assertEqual(code, 1, protected)
        self.assertIn("pending ordinary MENY cart", protected["error"])
        self.assertEqual(self.frames, [])
        self.assertEqual(self.files(), before)

    def test_current_receipt_layout_stays_a_read_only_visible_window(self):
        code, listed = self.call({"operation": "orders"}, host=self.current_order_layout_host)
        self.assertEqual(code, 0, listed)
        self.assertEqual(listed["result"]["history_scope"], "rendered_last_six_months")
        self.assertEqual(listed["result"]["orders"][0]["history_scope"], "rendered_last_six_months")
        self.assertEqual(listed["result"]["backend_freshness"], "unverified")
        code, detail = self.call({"operation": "orders", "action": "get", "order_id": "123456"},
                                 host=self.current_order_layout_host)
        self.assertEqual(code, 0, detail)
        self.assertEqual(detail["result"]["order"]["productQuantityCount"], 40)
        self.assertEqual(detail["result"]["order"]["receipt_layout"], "archived")
        self.assertEqual(detail["result"]["order"]["payment_status"], "unknown")
        self.assertEqual(self.writes, 0)

    def canonical_order_layout_host(self, frame):
        reply = self.rendered_orders_host(frame)
        reply["result"].update(source_query_keys=[], account_sha256=self.binding["account_sha256"],
                               order_number_label="Ordrenummer", item_disclosure_expanded=True,
                               status_markers=["Bestillingen kan oppdateres frem til mandag kl. 20:00"],
                               delivery_display="tirsdag 8. des. kl. 08:00 - 12:00",
                               amounts={"Sum varer": "80,00 kr", "Reservert beløp": "125,00 kr"})
        return reply

    def test_canonical_meny_order_is_current_read_only_and_replays_as_historical(self):
        request = {"operation": "orders", "action": "get", "order_id": "123456", "response_view": "agent"}
        command_id = str(uuid.uuid4())
        before = self.state()
        code, result = self.call(request, command_id, self.canonical_order_layout_host)
        self.assertEqual(code, 0, result)
        view = result["result"]
        self.assertEqual(view["observation_scope"], "fresh")
        self.assertEqual(view["order"]["receipt_layout"], "current")
        self.assertEqual(view["order"]["tracking"]["status"], "confirmed")
        self.assertEqual(view["order"]["tracking"]["backend_freshness"], "unverified")
        self.assertEqual(view["order"]["amount_displays"]["Reservert beløp"], "125,00 kr")
        self.assertEqual(view["order"]["payment_status"], "unknown")
        self.assertIsNone(view["order"]["order_total"])
        self.assertEqual(view["order"]["delivery_display"], "tirsdag 8. des. kl. 08:00 - 12:00")
        self.assertEqual(view["order"]["observed_at"], view["order_items"]["observed_at"])
        self.assertNotIn("account_sha256", view["order"])
        code, cached = self.call(request, command_id, self.canonical_order_layout_host)
        self.assertEqual(code, 0, cached)
        self.assertEqual(cached["result"], {**view, "observation_scope": "historical"})
        self.assertEqual([frame["operation"] for frame in self.frames], ["get_order"])
        self.assertEqual(self.state(), before)

    def test_canonical_meny_order_rejects_wrong_account_route_label_or_incomplete_items(self):
        request = {"operation": "orders", "action": "get", "order_id": "123456"}
        cases = ({"account_sha256": "f" * 64}, {"order_number": "987654"},
                 {"source_path": "/profil/nettbutikk/bestilling/987654"},
                 {"source_query_keys": ["archived"]}, {"order_number_label": "Bestillingskode"},
                 {"item_rows_complete": False}, {"item_disclosure_expanded": False},
                 {"status_markers": ["Bekreftet", "Bestillingen er kansellert"]})
        for changes in cases:
            with self.subTest(changes=changes):
                def changed(frame):
                    reply = self.canonical_order_layout_host(frame)
                    reply["result"].update(changes)
                    return reply

                code, refused = self.call(request, host=changed)
                self.assertEqual(code, 1, refused)
        self.assertEqual(self.writes, 0)

    def test_canonical_meny_cancellation_needs_a_positive_status_and_preserves_unknown_items(self):
        request = {"operation": "orders", "action": "get", "order_id": "123456", "response_view": "agent"}

        def receipt(frame):
            reply = self.canonical_order_layout_host(frame)
            reply["result"].update(status_markers=markers, source_path="/profil/nettbutikk/bestilling/123456")
            return reply

        for markers in ([], ["Ikke kansellert"], ["Kanseller bestilling"]):
            code, result = self.call(request, host=receipt)
            self.assertEqual(code, 0, result)
            self.assertEqual(result["result"]["order"]["observed_status"], "unknown")
            self.assertIsNone(result["result"]["order"]["cancelled"])

        def confirmation(frame):
            reply = receipt(frame)
            fields = ("item_heading", "item_count", "item_table_count", "item_columns", "item_rows_complete",
                      "products", "item_disclosure_expanded")
            for key in fields:
                del reply["result"][key]
            return reply

        code, missing = self.call(request, host=confirmation)
        self.assertEqual(code, 1, missing)
        markers = ["Bestillingen er kansellert."]
        code, cancelled = self.call(request, host=confirmation)
        self.assertEqual(code, 0, cancelled)
        view = cancelled["result"]
        self.assertTrue(view["order"]["cancelled"])
        self.assertEqual(view["order"]["tracking"]["status"], "cancelled")
        self.assertEqual(view["order"]["payment_status"], "unknown")
        self.assertFalse(view["order_items"]["available"])
        self.assertIsNone(view["order_items"]["total"])

    def test_rendered_orders_require_a_positive_status_in_older_layouts(self):
        for host in (self.rendered_orders_host, self.current_order_layout_host):
            for marker, expected in (("Ikke kansellert", "unknown"), ("Kansellert bestilling", "cancelled")):
                with self.subTest(layout=host.__name__, marker=marker):
                    def status(frame):
                        reply = host(frame)
                        if frame["operation"] == "get_orders":
                            reply["result"]["orders"][0]["status_marker"] = marker
                        else:
                            reply["result"]["status_markers"] = [marker]
                        return reply

                    code, history = self.call({"operation": "orders"}, host=status)
                    self.assertEqual(code, 0, history)
                    self.assertEqual(history["result"]["orders"][0]["status"], expected)
                    code, detail = self.call({"operation": "orders", "action": "get", "order_id": "123456"}, host=status)
                    self.assertEqual(code, 0, detail)
                    self.assertEqual(detail["result"]["tracking"]["status"], expected)

    def test_current_receipt_layout_rejects_a_different_link_or_closed_disclosure(self):
        cases = (("list", "row", {"same_archived_url": False}), ("list", "row", {"receipt_label": "Endre"}),
                 ("list", "row", {"cell_count": 7}), ("list", "result", {"table_class": "other"}),
                 ("list", "result", {"heading": "Unrelated fixture heading"}),
                 ("get", "result", {"item_disclosure_expanded": False}), ("get", "result", {"source_archived": False}),
                 ("get", "result", {"order_number_label": "Bestillingskode"}),
                 ("get", "result", {"heading": "BESTILLING other", "heading_matches_pickup_code": False}))
        for action, target, changes in cases:
            with self.subTest(action=action, changes=changes):
                def changed(frame):
                    reply = self.current_order_layout_host(frame)
                    result = reply["result"]
                    destination = result["orders"][0] if target == "row" else result
                    destination.update(changes)
                    return reply
                request = {"operation": "orders", "action": action}
                if action == "get":
                    request["order_id"] = "123456"
                code, result = self.call(request, host=changed)
                self.assertEqual(code, 1, result)
        self.assertEqual(self.writes, 0)

    def test_native_orders_protected_actions_and_invalid_inputs_stop_before_intent(self):
        before = self.files()
        requests = [{"operation": "orders", "action": action, "order_id": "123456"}
                    for action in ("change_begin", "cancel_prepare", "cancel_confirm", "remove_prepare")]
        requests += [{"operation": "orders", "action": "get", "order_id": identity}
                     for identity in ("", "../123", "synthetic", 123456)]
        requests += [{"operation": "orders", "limit": limit} for limit in (0, 6, True)]
        for request in requests:
            with self.subTest(request=request):
                code, result = self.call(request, host=self.rendered_orders_host)
                self.assertEqual(code, 1, result)
                self.assertEqual(self.files(), before)
                self.assertEqual(self.frames, [])

    def test_delivery_read_normalizes_labels_dates_and_from_price_without_writes(self):
        code, policy = self.call({"operation": "native_cart_policy", "action": "set", "enabled": False,
                                 "browser_binding": self.binding})
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

    def test_delivery_longer_read_window_rejects_stale_or_future_observations(self):
        for invalid in ("stale", "future"):
            with self.subTest(observation=invalid):
                def changed(frame):
                    reply = self.delivery_host(frame)
                    if frame["operation"] == "get_delivery_slots":
                        boundary = frame["emitted_at"] if invalid == "stale" else frame["expires_at"]
                        offset = -1 if invalid == "stale" else 1
                        reply["observed_at"] = (datetime.fromisoformat(boundary)
                                                + timedelta(seconds=offset)).isoformat()
                    return reply
                code, result = self.call({"operation": "delivery"}, host=changed)
                self.assertEqual(code, 1, result)
                self.assertIn("reply identity or lifetime changed", result["error"])
                self.assertNotIn("pending_cart_change", self.state())
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
                        {"operation": "orders", "action": "change_begin", "order_id": "123456"}):
            before = self.files()
            frame_count = len(self.frames)
            code, result = self.call(request)
            self.assertEqual(code, 1, result)
            self.assertEqual(self.files(), before)
            self.assertEqual(len(self.frames), frame_count)

    def test_rebind_retired_browser_preserves_household_and_blocks_old_target(self):
        tab = "synthetic-observation-tab"
        before = self.state()
        request = {"tab_id": tab, "previous_tab_id": self.binding["tab_id"], "original_tab_retired": True}

        def wrong_account(frame):
            reply = self.replacement_observation_host(frame)
            reply["result"]["account_sha256"] = "f" * 64
            return reply

        code, rejected = self.run_session("rebind-tab", request, wrong_account)
        self.assertEqual(code, 1, rejected)
        self.assertEqual(self.state(), before)
        code, rebound = self.run_session("rebind-tab", request, self.replacement_observation_host)
        self.assertEqual(code, 0, rebound)
        self.assertEqual(rebound["result"]["status"], "browser_rebound")
        self.assertFalse(rebound["result"]["writes_enabled"])
        after = self.state()
        self.assertEqual(after["native_tab_rebind"]["previous_tab"], self.binding["tab_id"])
        self.assertEqual(after["profile"], before["profile"])
        self.assertNotEqual(after["product_selection_generation"], before.get("product_selection_generation"))
        self.assertNotEqual(after["native_config_sha256"], before["native_config_sha256"])
        self.binding = {**self.binding, "tab_id": tab}
        read = {"operation": "cart", "action": "get"}
        identifier = str(uuid.uuid4())
        code, result = self.call(read, identifier)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.frames[-1]["browser_binding"]["tab_id"], tab)
        next_tab = "synthetic-third-tab"
        code, latest = self.run_session("rebind-tab", {**request, "tab_id": next_tab, "previous_tab_id": tab},
                                        lambda frame: self.replacement_observation_host(frame, tab_id=next_tab))
        self.assertEqual(code, 0, latest)
        self.assertEqual(self.call(read, identifier), (0, result))
        code, history = self.run_session("rebind-tab", request)
        self.assertEqual(code, 0, history)
        self.assertEqual(history["result"]["observation_scope"], "historical")
        code, rejected = self.run_session("rebind-tab", {**request, "previous_tab_id": next_tab})
        self.assertEqual(code, 1, rejected)
        self.assertEqual(self.state()["native_tab_rebind"]["tab_id"], next_tab)

    def test_legacy_rebind_retires_earlier_product_approval_before_any_write(self):
        old_apply = {**self.managed_request(), "product_plan_ref": self.prepared_ref}
        tab = "synthetic-observation-tab"
        request = {"tab_id": tab, "previous_tab_id": self.binding["tab_id"], "original_tab_retired": True}
        code, redone = self.run_session("rebind-tab", request, self.replacement_observation_host)
        self.assertEqual(code, 0, redone)
        legacy = self.state()
        legacy["native_tab_rebind"].pop("request_sha256")
        old_generation = legacy["product_selection_generation"] = "a" * 32
        (self.root / "state/state.json").write_text(json.dumps(legacy))
        self.binding = {**self.binding, "tab_id": tab}
        code, enabled = self.call({"operation": "native_cart_policy", "action": "set", "enabled": True,
                                   "browser_binding": self.binding})
        self.assertEqual(code, 0, enabled)
        frames = len(self.frames)
        code, blocked = self.call(old_apply)
        self.assertEqual(code, 1, blocked)
        self.assertIn("new MENY product plan", blocked["error"])
        self.assertEqual(len(self.frames), frames)
        self.assertEqual(self.writes, 0)
        self.assertNotEqual(self.state()["product_selection_generation"], old_generation)

    def protected_delivery_host(self, *, lose_reply=False, altered_address=False):
        from meny import MENY_MONTH_NAMES, MENY_WEEKDAY_NAMES

        tomorrow = datetime.now(ZoneInfo("Europe/Oslo")).date() + timedelta(days=1)
        slot_ref = f"meny:{tomorrow}T09:00/12:00"
        day = f"{tomorrow.day}. {MENY_MONTH_NAMES[tomorrow.month - 1]}"
        window = f"{MENY_WEEKDAY_NAMES[tomorrow.weekday()]} {day} kl. 09:00-12:00"
        price_label = f"fra 49,00 kr fra 49,00 kroner, {day} klokka 09:00 til 12:00"
        state = {"selected": False, "writes": 0}

        def host(frame):
            operation, effect = frame["operation"], frame["effect"]
            if operation == "get_delivery_slots":
                result = {"authenticated": True, "ready": True, "source_url": "https://meny.no/varer",
                          "dialog_count": 1, "slots": [{"slot_id": price_label, "display": price_label,
                              "date": str(tomorrow), "start": "09:00", "end": "12:00", "selected": state["selected"]}]}
                if state.get("empty_calendar"):
                    result["slots"] = []
                    if state.get("empty_calendar") == "verified":
                        result["no_selectable_windows"] = True
            elif operation == "delivery_context":
                result = {"source_url": "https://meny.no/varer", "authenticated": True, "ordinary_cart": True,
                          "home_delivery": True, "address_sha256": ("d" if state.get("changed") and altered_address else "c") * 64,
                          "delivery": window if state["selected"] else None}
            elif operation == "get_cart":
                result = self.snapshot()
                if state["selected"] and self.quantity:
                    result.update(delivery_count=1, delivery={"display": window})
            elif operation == "select_delivery_window":
                self.assertEqual(effect, "delivery_selection")
                pending = self.state()["native_delivery_selection"]
                self.assertEqual(pending["status"], "dispatching")
                self.assertEqual(pending["call_id"], frame["call_id"])
                self.assertEqual(pending["review_digest"], frame["review_digest"])
                self.assertEqual(frame["arguments"]["calendar_label"], price_label)
                state.update(selected=True, writes=state["writes"] + 1)
                if lose_reply:
                    return None
                result = {"dispatched": True}
            elif operation == "review_checkout":
                self.assertEqual(effect, "checkout_review")
                result = {"ordinary_cart": True, "url": "https://meny.no/kassen", "address_sha256": "c" * 64,
                          "items": {PRODUCT: 1}, "payment": {"ready": True, "authenticated": True, "vipps_checked": True,
                              "home_delivery": True, "submit_enabled": True, "submit_controls": 1, "total": 69.0,
                              "delivery": window, "price_rows": [False, [["product_subtotal", 2000],
                                  ["delivery_price", 4900], ["reserved_total", 6900]]]}}
            elif operation == "verify_observation_context":
                result = {**{key: self.binding[key] for key in ("account_sha256", "cart_context_sha256")},
                          "authenticated": True, "new_cart": True}
            else:
                return self.host(frame)
            return {"reply_to": frame["call_id"], "browser_binding": self.binding,
                    "observed_at": datetime.now(timezone.utc).isoformat(), "result": result}

        return slot_ref, state, host

    def test_reviewed_delivery_selection_and_vipps_handoff(self):
        self.quantity = 1
        reference, live, host = self.protected_delivery_host()
        code, prepared = self.call({"operation": "delivery", "action": "prepare", "slot_ref": reference}, host=host)
        self.assertEqual(code, 0, prepared)
        review = prepared["result"]
        self.assertEqual(review["status"], "delivery_review")
        self.assertFalse(review["dispatch"])
        self.assertEqual(live["writes"], 0)
        select_review = {"operation": "delivery", "action": "select",
                         "reservation_approved": True,
                         **{key: review[key] for key in ("review_id", "review_digest")}}
        code, selected = self.call(select_review, host=host)
        self.assertEqual(code, 0, selected)
        self.assertEqual(selected["result"]["status"], "delivery_selection_observed")
        self.assertEqual(selected["result"]["backend_freshness"], "unverified")
        self.assertEqual(live["writes"], 1)
        code, repeated = self.call(select_review, host=host)
        self.assertEqual(code, 1, repeated)
        code, ready = self.call({"operation": "checkout", "action": "prepare"}, host=host)
        self.assertEqual(code, 0, ready)
        self.assertEqual(ready["result"]["status"], "owner_checkout_ready")
        self.assertEqual(ready["result"]["checkout_total_ore"], 6900)
        self.assertFalse(ready["result"]["dispatch"])
        self.assertEqual(live["writes"], 1)

    def test_delivery_lost_reply_reconciles_same_selection_after_restart(self):
        reference, live, host = self.protected_delivery_host(lose_reply=True, altered_address=True)
        code, prepared = self.call({"operation": "delivery", "action": "prepare", "slot_ref": reference}, host=host)
        self.assertEqual(code, 0, prepared)
        review = prepared["result"]
        review_ref = {key: review[key] for key in ("review_id", "review_digest")}
        command = {"operation": "delivery", "action": "select", "reservation_approved": True, **review_ref}
        live["changed"] = True
        code, drifted = self.call(command, host=host)
        self.assertEqual(code, 1, drifted)
        self.assertIn("address, cart or calendar offer changed", drifted["error"])
        self.assertEqual(live["writes"], 0)
        live["changed"] = False
        code, lost = self.call(command, host=host)
        self.assertEqual(code, 1, lost)
        self.assertEqual(live["writes"], 1)
        before = len(self.frames)
        for request in (command, self.ensure(), {"operation": "checkout", "action": "prepare"}):
            code, refused = self.call(request, host=host)
            self.assertEqual(code, 1, refused)
        self.assertEqual(len(self.frames), before)
        pending = self.state()
        pending["native_delivery_selection"]["dispatch_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        (self.root / "state/state.json").write_text(json.dumps(pending))
        code, ending = self.call({"operation": "delivery", "action": "reconcile", **review_ref}, host=host)
        self.assertEqual(code, 0, ending)
        self.assertEqual(ending["result"]["original_dispatch_outcome"], "unknown")
        self.assertEqual(live["writes"], 1)

    def test_retired_tab_can_only_reconcile_the_original_delivery(self):
        reference, live, host = self.protected_delivery_host(lose_reply=True)
        code, review = self.call({"operation": "delivery", "action": "prepare", "slot_ref": reference}, host=host)
        self.assertEqual(code, 0, review)
        review_ref = {key: review["result"][key] for key in ("review_id", "review_digest")}
        code, lost = self.call({"operation": "delivery", "action": "select", "reservation_approved": True,
                                **review_ref}, host=host)
        self.assertEqual(code, 1, lost)
        before = len(self.frames)
        tab = "synthetic-observation-tab"
        rebind = {"tab_id": tab, "previous_tab_id": self.binding["tab_id"], "original_tab_retired": True,
                  "delivery_recovery": review_ref}
        code, stopped = self.run_session("rebind-tab", rebind)
        self.assertEqual(code, 1, stopped)
        self.assertIn("original expired", stopped["error"])
        self.assertEqual(len(self.frames), before)
        pending = self.state()
        pending["native_delivery_selection"]["dispatch_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        (self.root / "state/state.json").write_text(json.dumps(pending))

        def replacement(frame):
            operation = frame["operation"]
            self.assertEqual(frame["effect"], "read")
            if operation in ("verify_observation_context", "get_cart"):
                return self.replacement_observation_host(frame)
            reply = host(frame)
            return {**reply, "browser_binding": frame["browser_binding"], "result": {
                "verified_context": {**{key: self.binding[key] for key in ("account_sha256", "cart_context_sha256")},
                                     "account_verified_before_and_after": True}, "observation": reply["result"]}}

        code, settled = self.run_session("rebind-tab", rebind, replacement)
        self.assertEqual(code, 0, settled)
        self.assertEqual(settled["result"]["delivery_recovery"]["original_dispatch_outcome"], "unknown")
        self.assertEqual(self.state()["native_tab_rebind"]["tab_id"], tab)
        self.assertEqual(live["writes"], 1)
        changed = self.state()
        changed.pop("native_delivery_selection")
        (self.root / "state/state.json").write_text(json.dumps(changed))
        code, original = self.run_session("rebind-tab", rebind)
        self.assertEqual(code, 0, original)
        self.assertEqual(original["result"]["delivery_recovery"]["original_dispatch_outcome"], "unknown")
        self.assertEqual(original["result"]["delivery_recovery"]["observation_scope"], "historical")
        code, conflict = self.run_session("rebind-tab", {**rebind, "delivery_recovery": {**review_ref, "review_id": str(uuid.uuid4())}})
        self.assertEqual(code, 1, conflict)

    def test_owner_can_reconcile_an_expired_dispatch_on_an_explicitly_empty_picker(self):
        reference, live, host = self.protected_delivery_host(lose_reply=True, altered_address=True)
        code, review = self.call({"operation": "delivery", "action": "prepare", "slot_ref": reference}, host=host)
        self.assertEqual(code, 0, review)
        review_ref = {key: review["result"][key] for key in ("review_id", "review_digest")}
        code, lost = self.call({"operation": "delivery", "action": "select", "reservation_approved": True,
                                **review_ref}, host=host)
        self.assertEqual(code, 1, lost)
        started = self.state()
        started["native_delivery_selection"]["dispatch_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        (self.root / "state/state.json").write_text(json.dumps(started))
        accept = {"operation": "delivery", "action": "accept_current", "owner_accepted_unknown": True,
                  "original_executor_stopped": True, **review_ref}
        live.update(selected=False, empty_calendar="unverified")
        code, blocked = self.call(accept, host=host)
        self.assertEqual(code, 1, blocked)
        self.assertEqual(self.state()["native_delivery_selection"]["status"], "dispatching")
        live["empty_calendar"] = "verified"
        live["changed"] = True
        account_reads = 0

        def swapped_account(frame):
            nonlocal account_reads
            reply = host(frame)
            if frame["operation"] == "verify_observation_context":
                account_reads += 1
                if account_reads == 2:
                    reply["result"]["account_sha256"] = "f" * 64
            return reply

        code, refused = self.call(accept, host=swapped_account)
        self.assertEqual(code, 1, refused)
        self.assertIn("original MENY account", refused["error"])
        self.assertEqual(account_reads, 2)
        self.assertEqual(self.state()["native_delivery_selection"]["status"], "dispatching")
        code, ending = self.call(accept, host=host)
        self.assertEqual(code, 0, ending)
        self.assertEqual(ending["result"]["status"], "delivery_archived_unknown")
        self.assertTrue(ending["result"]["observed_address_changed"])
        self.assertIsNone(ending["result"]["observed_current_slot_ref"])
        self.assertEqual(ending["result"]["original_dispatch_outcome"], "unknown")
        self.assertEqual(live["writes"], 1)

    def test_meny_order_handoff_checks_the_exact_eligible_receipt(self):
        expected = "123456"

        def host(frame):
            self.assertEqual(frame["effect"], "read")
            self.assertEqual(frame["operation"], "review_order_change")
            return {"reply_to": frame["call_id"], "browser_binding": self.binding, "observed_at": datetime.now(timezone.utc).isoformat(),
                    "result": {"source_origin": "https://meny.no", "source_path": f"/trumf-profil/nettbutikk/bestilling/{expected}",
                               "source_query_keys": [], "source_hash": "",
                               "order_number": expected, "exact_order": True, "authenticated": True,
                               "archived": False, "available_changes": ["items"]}}

        check = {"operation": "orders", "action": "change_handoff", "order_id": expected}
        code, good = self.call(check, host=host)
        self.assertEqual(code, 0, good)
        self.assertEqual(good["result"]["status"], "owner_order_change_ready")
        self.assertFalse(good["result"]["dispatch"])
        code, wrong = self.call({**check, "order_id": "123459"}, host=host)
        self.assertEqual(code, 1, wrong)

    def meny_edit_host(self):
        order_id = "123456"
        day = (datetime.now(ZoneInfo("Europe/Oslo")) + timedelta(days=3)).date()
        weekdays = ("mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag")
        months = ("januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "desember")
        display = f"{weekdays[day.weekday()]} {day.day}. {months[day.month - 1]} kl. 08:00-12:00"
        receipt_display = f"{weekdays[day.weekday()]} {day.day}. {months[day.month - 1][:3]}. kl. 08:00 - 12:00"
        original = {"url": f"https://meny.no/trumf-profil/nettbutikk/bestilling/{order_id}",
                    "order_number": order_id, "authenticated": True, "account_sha256": self.binding["account_sha256"],
                    "edit_code_sha256": "c" * 64, "address_sha256": "d" * 64,
                    "slot_ref": f"meny:{day.isoformat()}T08:00/12:00", "delivery_display": receipt_display,
                    "reservation_ore": 12800, "product_subtotal_ore": 2000, "status": "editable", "items_complete": True,
                    "item_count": 1, "items": [{"identity": "Synthetic oats 500 g", "quantity": 1}]}
        addition = {"product_id": EDIT_PRODUCT, "identity": "Synthetic pasta 500 g", "quantity": 1}
        draft = {**{key: original[key] for key in ("authenticated", "account_sha256", "edit_code_sha256", "address_sha256", "slot_ref")},
                 "url": "https://meny.no/kassen", "ordinary_cart": False, "abort_available": True,
                 "submit_label": "Send oppdatering", "items_complete": True, "item_count": 2,
                 "items": [{**original["items"][0], "product_id": PRODUCT, "unit_price_ore": 2000},
                           {**addition, "unit_price_ore": 1690}],
                 "payment": {"ready": True, "authenticated": True, "vipps_checked": True, "home_delivery": True,
                             "submit_enabled": True, "submit_controls": 1, "total": 144.90, "delivery": display,
                             "price_rows": [False, [["product_subtotal", 3690], ["delivery_price", 4900],
                                                    ["picking_fee", 5900], ["reserved_total", 14490]]]}}
        ready = {"status": "ready_for_readback", "origin": "https://meny.no", "path": "/kassen",
                 "order_id": None, "reservation_ore": None}
        live = {"original": original, "draft": draft, "handoff": ready, "ready": ready,
                "cutoff": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "lose_receipt": False}

        def host(frame):
            self.assertEqual(frame["effect"], "read")
            self.assertEqual(frame["browser_binding"], self.binding)
            operation = frame["operation"]
            if operation == "verify_observation_context":
                response = {key: self.binding[key] for key in ("account_sha256", "cart_context_sha256")}
                response.update(authenticated=True, new_cart=True)
            elif operation == "get_cart":
                response = self.snapshot()
            elif operation == "read_edit_receipt":
                phase = frame["arguments"]["phase"]
                self.assertEqual(frame["arguments"]["order_id"], order_id)
                if live["lose_receipt"]:
                    return None
                response = dict(live["original"])
                if response["status"] == "cancelled":
                    response = {key: response[key] for key in ("url", "order_number", "authenticated", "account_sha256", "status")}
                if phase in ("original", "approval"):
                    response["cutoff_at"] = live["cutoff"]
                if phase == "approval":
                    response.update(receipt_tab_id="same-browser-read-tab", receipt_browser_id=self.binding["browser_id"])
                if phase in ("discarded", "closed_outcome") and response["status"] != "cancelled":
                    response.update(original_handoff_ended=live.get("handoff_ended", True),
                                    no_payment_pending=live.get("no_payment_pending", True))
            elif operation == "review_edit_checkout":
                self.assertEqual(frame["arguments"]["order_id"], order_id)
                self.assertEqual(frame["arguments"]["addition"], addition)
                response = live["draft"]
            else:
                self.assertEqual(operation, "observe_edit_handoff")
                response = live["handoff"]
            return {"reply_to": frame["call_id"], "browser_binding": self.binding,
                    "observed_at": datetime.now(timezone.utc).isoformat(), "result": response}

        begin = {"operation": "orders", "action": "change_begin", "order_id": order_id, "addition": addition}
        return begin, live, host

    def begin_meny_edit(self, *, original_subtotal=2000):
        begin, live, host = self.meny_edit_host()
        live["original"]["product_subtotal_ore"] = original_subtotal
        code, started = self.call(begin, host=host)
        self.assertEqual(code, 0, started)
        request = {"operation": "orders", "edit_id": started["result"]["edit_id"]}
        code, reviewed = self.call({**request, "action": "change_review"}, host=host)
        self.assertEqual(code, 0, reviewed)
        return request, reviewed["result"], live, host

    def approve_meny_edit(self, request, review, host, *, command_id=None):
        return self.call({**request, "action": "change_approve", "review_id": review["review_id"],
                          "review_digest": review["review_digest"], "approved_reservation_ore": review["quote"]["reservation_ore"]},
                         command_id=command_id, host=host)

    def test_meny_edit_fee_drift_requires_new_approval_and_handoff_is_never_replayed(self):
        request, review, live, host = self.begin_meny_edit()
        live["draft"]["payment"]["price_rows"][1][1:3] = [["delivery_price", 5000], ["picking_fee", 5800]]
        code, changed = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 0, changed)
        revised = changed["result"]
        self.assertTrue(revised["quote_changed"])
        self.assertEqual(revised["quote"]["reservation_ore"], review["quote"]["reservation_ore"])
        self.assertNotEqual(revised["review_digest"], review["review_digest"])
        code, stale = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 1, stale)
        command_id = str(uuid.uuid4())
        code, live_handoff = self.approve_meny_edit(request, revised, host, command_id=command_id)
        self.assertEqual(code, 0, live_handoff)
        self.assertTrue(live_handoff["result"]["owner_handoff_ready"])
        self.assertFalse(live_handoff["result"]["dispatch"])
        before = len(self.frames)
        code, historical = self.approve_meny_edit(request, revised, host, command_id=command_id)
        self.assertEqual(code, 0, historical)
        self.assertFalse(historical["result"]["owner_handoff_ready"])
        self.assertNotIn("next_control", historical["result"])
        self.assertEqual(historical["result"]["observation_scope"], "historical")
        code, duplicate = self.approve_meny_edit(request, revised, host)
        self.assertEqual(code, 1, duplicate)
        for conflict in (self.ensure(), {"operation": "orders", "action": "list"},
                         {"operation": "checkout", "action": "prepare"}):
            code, blocked = self.call(conflict, host=host)
            self.assertEqual(code, 1, blocked)
        self.assertEqual(len(self.frames), before)

    def test_meny_edit_reconcile_preserves_vipps_and_survives_loss_of_receipt(self):
        request, review, live, host = self.begin_meny_edit()
        code, approved = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 0, approved)
        reconcile = {**request, "action": "change_reconcile"}
        live["handoff"] = {"status": "payment_pending", "origin": "https://api.vipps.no",
                           "path": "/dwo-api-application/v1/deeplink/vippsgateway", "order_id": None, "reservation_ore": 14490}
        before = len(self.frames)
        code, pending = self.call(reconcile, host=host)
        self.assertEqual(code, 0, pending)
        self.assertTrue(pending["result"]["owner_payment_pending"])
        self.assertEqual([entry["operation"] for entry in self.frames[before:]], ["observe_edit_handoff"])
        live["handoff"] = {**live["ready"], "status": "edit_confirmed", "path": "/kassen/bekreftelse",
                           "order_id": live["original"]["order_number"], "visible_update_ack": False}
        code, plain = self.call(reconcile, host=host)
        self.assertEqual(code, 1, plain)
        self.assertNotIn("confirmation_observed_at", self.state()["native_meny_edit"])
        live["handoff"]["visible_update_ack"] = True
        live["lose_receipt"] = True
        code, lost = self.call(reconcile, host=host)
        self.assertEqual(code, 1, lost)
        self.assertIn("confirmation_observed_at", self.state()["native_meny_edit"])
        live["handoff"] = live["ready"]
        live["lose_receipt"] = False
        code, old = self.call(reconcile, host=host)
        self.assertEqual(code, 0, old)
        self.assertFalse(old["result"]["observed"])
        live["original"].update(status="confirmed", item_count=2, reservation_ore=14490, product_subtotal_ore=None,
                                edit_code_sha256=None,
                                items=[{"identity": line["identity"], "quantity": line["quantity"]}
                                       for line in live["draft"]["items"]])
        code, completed = self.call(reconcile, host=host)
        self.assertEqual(code, 0, completed)
        self.assertEqual(completed["result"]["status"], "edit_observed")
        self.assertTrue(completed["result"]["original_confirmation_seen"])
        self.assertIsNone(completed["result"]["receipt_product_subtotal_ore"])
        self.assertFalse(completed["result"]["dispatch"])

    def test_meny_edit_stale_original_and_wrong_draft_stop_and_terminal_cancel_releases_guard(self):
        request, review, live, host = self.begin_meny_edit()
        live["original"]["reservation_ore"] += 100
        code, overwritten = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 1, overwritten)
        self.assertIn("committed original MENY order changed", overwritten["error"])
        live["original"]["reservation_ore"] -= 100
        for field, bad in (("submit_label", "Til betaling"), ("ordinary_cart", True), ("account_sha256", "f" * 64),
                           ("edit_code_sha256", "e" * 64), ("address_sha256", "f" * 64)):
            saved = live["draft"][field]
            live["draft"][field] = bad
            code, wrong = self.approve_meny_edit(request, review, host)
            self.assertEqual(code, 1, wrong)
            live["draft"][field] = saved
        live["original"]["status"] = "cancelled"
        code, cancelled = self.call({**request, "action": "change_reconcile"}, host=host)
        self.assertEqual(code, 0, cancelled)
        self.assertEqual(cancelled["result"]["status"], "edit_original_order_cancelled")
        self.assertFalse(cancelled["result"]["dispatch"])
        code, ordinary = self.call({"operation": "cart", "action": "get"}, host=host)
        self.assertEqual(code, 0, ordinary)

    def test_meny_edit_package_prices_reconcile_with_goods_and_included_discounts(self):
        request, review, live, host = self.begin_meny_edit()
        draft = live["draft"]
        draft["items"][0]["unit_price_ore"] += 1000
        code, wrong = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 1, wrong)
        self.assertIn("package prices do not match", wrong["error"])
        self.assertEqual(self.state()["native_meny_edit"]["status"], "edit_review")

        draft["payment"]["price_rows"][1].append(["discounts", -1000])
        code, changed = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 0, changed)
        revised = changed["result"]
        self.assertTrue(revised["quote_changed"])
        self.assertTrue(revised["quote"]["price_breakdown"]["discounts_already_in_subtotal"])
        code, approved = self.approve_meny_edit(request, revised, host)
        self.assertEqual(code, 0, approved)
        self.assertTrue(approved["result"]["owner_handoff_ready"])

    def test_meny_edit_optional_receipt_subtotal_does_not_block_recovery(self):
        request, review, live, host = self.begin_meny_edit()
        discard = {**request, "action": "change_discard", "owner_confirmed_no_submit": True}
        live["original"]["product_subtotal_ore"] = 2500
        for code, refused in (self.approve_meny_edit(request, review, host), self.call(discard, host=host)):
            self.assertEqual(code, 1, refused)
        live["original"]["product_subtotal_ore"] = None
        code, discarded = self.call(discard, host=host)
        self.assertEqual(code, 0, discarded)
        self.assertEqual(discarded["result"]["status"], "edit_discarded")

        code, ordinary = self.call({"operation": "cart", "action": "get"}, host=host)
        self.assertEqual(code, 0, ordinary)

        request, review, live, host = self.begin_meny_edit(original_subtotal=None)
        live["original"]["product_subtotal_ore"] = 2000
        code, approved = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 0, approved)
        self.assertTrue(approved["result"]["owner_handoff_ready"])
        discard = {**request, "action": "change_discard", "owner_confirmed_no_submit": True}
        count = len(self.frames)
        code, too_soon = self.call(discard, host=host)
        self.assertEqual(code, 1, too_soon)
        self.assertIn("admission to expire", too_soon["error"])
        self.assertEqual(len(self.frames), count)
        pending = self.state()
        pending["native_meny_edit"]["owner_handoff_admit_by"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        (self.root / "state/state.json").write_text(json.dumps(pending))
        code, discarded = self.call(discard, host=host)
        self.assertEqual(code, 0, discarded)
        self.assertEqual(discarded["result"]["status"], "edit_discarded")

    def test_meny_edit_acknowledged_discrepancy_needs_fresh_exact_owner_acceptance(self):
        request, review, live, host = self.begin_meny_edit()
        code, approved = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 0, approved)
        reconcile = {**request, "action": "change_reconcile"}
        order = live["original"]
        updated_lines = [{"identity": line["identity"], "quantity": line["quantity"]} for line in live["draft"]["items"]]
        order.update(status="confirmed", item_count=2, items=updated_lines, reservation_ore=14590,
                     product_subtotal_ore=3690)
        code, no_ack = self.call(reconcile, host=host)
        self.assertEqual(code, 0, no_ack)
        self.assertFalse(no_ack["result"]["discrepancy_current"])

        live["handoff"] = {**live["ready"], "status": "edit_confirmed", "path": "/kassen/bekreftelse",
                           "order_id": order["order_number"], "visible_update_ack": True}
        order.update(item_count=1, items=updated_lines[:1], reservation_ore=12800, product_subtotal_ore=2500)
        code, repriced = self.call(reconcile, host=host)
        self.assertEqual(code, 0, repriced)
        self.assertEqual(repriced["result"]["discrepancy"]["product_subtotal_ore"], 2500)
        order.update(item_count=2, items=updated_lines, reservation_ore=14590, product_subtotal_ore=3690)
        code, reviewed = self.call(reconcile, host=host)
        self.assertEqual(code, 0, reviewed)
        difference = reviewed["result"]["discrepancy"]
        self.assertEqual(difference["reservation_ore"], 14590)
        self.assertEqual(reviewed["result"]["quote"]["reservation_ore"], 14490)
        self.assertTrue(reviewed["result"]["discrepancy_acceptance_required"])
        self.assertFalse(reviewed["result"]["observed"])

        acceptance = {**reconcile, "accept_observed_digest": difference["digest"]}
        live["handoff"] = {"status": "payment_pending", "origin": "https://api.vipps.no",
                           "path": "/dwo-api-application/v1/deeplink/vippsgateway", "order_id": None, "reservation_ore": 14490}
        code, pending = self.call(acceptance, host=host)
        self.assertEqual(code, 0, pending)
        self.assertTrue(pending["result"]["owner_payment_pending"])

        live["handoff"] = live["ready"]
        live["no_payment_pending"] = False
        code, unverified = self.call(acceptance, host=host)
        self.assertEqual(code, 1, unverified)
        self.assertIn("still unresolved", unverified["error"])
        live["no_payment_pending"] = True
        order.update(item_count=1, items=updated_lines[:1], reservation_ore=12800, product_subtotal_ore=2000)
        code, old = self.call(acceptance, host=host)
        self.assertEqual(code, 0, old)
        self.assertFalse(old["result"]["discrepancy_current"])
        self.assertEqual(old["result"]["status"], "edit_owner_pending")

        order.update(item_count=2, items=updated_lines, reservation_ore=14690, product_subtotal_ore=3690)
        code, changed = self.call(acceptance, host=host)
        self.assertEqual(code, 0, changed)
        self.assertTrue(changed["result"]["discrepancy_acceptance_required"])
        self.assertNotEqual(changed["result"]["discrepancy"]["digest"], difference["digest"])
        code, closed = self.call({**reconcile, "accept_observed_digest": changed["result"]["discrepancy"]["digest"]}, host=host)
        self.assertEqual(code, 0, closed)
        self.assertEqual(closed["result"]["status"], "edit_acknowledged_with_drift")
        self.assertTrue(closed["result"]["discrepancy_accepted"])
        self.assertTrue(closed["result"]["original_confirmation_seen"])
        self.assertFalse(closed["result"]["observed"])
        self.assertFalse(closed["result"]["dispatch"])
        code, ordinary = self.call({"operation": "cart", "action": "get"}, host=host)
        self.assertEqual(code, 0, ordinary)

    def test_meny_edit_archives_verified_delivery_without_inferring_a_reservation(self):
        request, review, live, host = self.begin_meny_edit()
        code, approved = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 0, approved)
        order = live["original"]
        live["handoff"] = {**live["ready"], "status": "edit_confirmed", "path": "/kassen/bekreftelse",
                           "order_id": order["order_number"], "visible_update_ack": True}
        order.update(status="delivered", reservation_ore=None, edit_code_sha256="f" * 64)
        reconcile = {**request, "action": "change_reconcile"}
        code, reported = self.call(reconcile, host=host)
        self.assertEqual(code, 0, reported)
        discrepancy = reported["result"]["discrepancy"]
        self.assertTrue(discrepancy["delivered"])
        self.assertIsNone(discrepancy["reservation_ore"])
        code, closed = self.call({**reconcile, "accept_observed_digest": discrepancy["digest"]}, host=host)
        self.assertEqual(code, 0, closed)
        self.assertEqual(closed["result"]["status"], "edit_original_order_delivered")
        self.assertFalse(closed["result"]["observed"])

    def test_meny_edit_date_only_delivery_closes_without_assuming_an_amendment(self):
        request, _, live, host = self.begin_meny_edit()
        order = live["original"]
        day = datetime.fromisoformat(order["slot_ref"][len("meny:"):].split("T", 1)[0])
        order.update(status="delivered", slot_ref=None, reservation_ore=None, edit_code_sha256=None,
                     delivery_display=(day + timedelta(days=1)).strftime("%d.%m.%Y"))
        reconcile = {**request, "action": "change_reconcile"}
        code, wrong = self.call(reconcile, host=host)
        self.assertEqual(code, 1, wrong)
        order["delivery_display"] = day.strftime("%d.%m.%Y")
        code, reported = self.call(reconcile, host=host)
        self.assertEqual(code, 0, reported)
        difference = reported["result"]["discrepancy"]
        self.assertTrue(difference["delivered"])
        self.assertFalse(difference["window_verified"])
        code, closed = self.call({**reconcile, "accept_observed_digest": difference["digest"]}, host=host)
        self.assertEqual(code, 0, closed)
        self.assertEqual(closed["result"]["status"], "edit_original_order_delivered")
        self.assertFalse(closed["result"]["observed"])
        self.assertFalse(closed["result"]["original_confirmation_seen"])

    def test_meny_edit_fences_draft_reads_and_requires_exact_replacement_recovery(self):
        request, review, live, host = self.begin_meny_edit()
        count = len(self.frames)
        code, refused = self.call({"operation": "cart", "action": "get"}, host=host)
        self.assertEqual(code, 1, refused)
        self.assertEqual(len(self.frames), count)
        rebind = {"tab_id": "synthetic-observation-tab", "previous_tab_id": self.binding["tab_id"],
                  "original_tab_retired": True}
        code, refused = self.run_session("rebind-tab", rebind, self.replacement_observation_host)
        self.assertEqual(code, 1, refused)
        self.assertIn("use the original MENY edit", refused["error"])
        self.assertEqual(len(self.frames), count)
        recovery = {**rebind, "edit_recovery": request["edit_id"]}
        self.quantity = 1
        code, refused = self.run_session("rebind-tab", recovery, self.replacement_observation_host)
        self.assertEqual(code, 1, refused)
        self.assertIn("empty ordinary MENY cart", refused["error"])
        self.quantity = 0
        code, rebound = self.run_session("rebind-tab", recovery, self.replacement_observation_host)
        self.assertEqual(code, 0, rebound)
        self.assertFalse(rebound["result"]["writes_enabled"])
        self.binding = {**self.binding, "tab_id": rebind["tab_id"]}
        code, refused = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 1, refused)
        self.assertIn("original MENY edit tab or cutoff", refused["error"])
        discard = {**request, "action": "change_discard", "owner_confirmed_no_submit": True}
        code, closed = self.call(discard, host=host)
        self.assertEqual(code, 0, closed)
        self.assertEqual(closed["result"]["status"], "edit_discarded")

    def test_meny_edit_replacement_readback_requires_original_handoff_closure(self):
        request, review, live, host = self.begin_meny_edit()
        code, approved = self.approve_meny_edit(request, review, host)
        self.assertEqual(code, 0, approved)
        pending = self.state()
        pending["native_meny_edit"]["owner_handoff_admit_by"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        (self.root / "state/state.json").write_text(json.dumps(pending))
        rebind = {"tab_id": "synthetic-observation-tab", "previous_tab_id": self.binding["tab_id"],
                  "original_tab_retired": True, "edit_recovery": request["edit_id"]}
        code, rebound = self.run_session("rebind-tab", rebind, self.replacement_observation_host)
        self.assertEqual(code, 0, rebound)
        self.binding = {**self.binding, "tab_id": rebind["tab_id"]}
        live["original"].update(status="confirmed", item_count=2, reservation_ore=14490, product_subtotal_ore=3690,
                                items=[{"identity": line["identity"], "quantity": line["quantity"]}
                                       for line in live["draft"]["items"]])
        reconcile = {**request, "action": "change_reconcile"}
        live["no_payment_pending"] = False
        code, blocked = self.call(reconcile, host=host)
        self.assertEqual(code, 1, blocked)
        self.assertEqual(self.state()["native_meny_edit"]["status"], "edit_owner_pending")
        live["no_payment_pending"] = True
        code, closed = self.call(reconcile, host=host)
        self.assertEqual(code, 0, closed)
        self.assertEqual(closed["result"]["status"], "edit_observed")

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
        self.assertEqual(code, 1, result)
        self.assertIn("pending_cart_change", self.state())
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
        self.assertEqual(code, 1, result)
        self.assertIn("pending_cart_change", self.state())
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
            return None if frame["operation"] == "get_cart" and self.writes else reply
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
            restored = termios.tcgetattr(slave)
            if sys.platform == "darwin":
                # Darwin sets PENDIN when canonical mode is restored. It is
                # pending-input state, not a terminal setting left changed.
                restored[3] &= ~termios.PENDIN
                original[3] &= ~termios.PENDIN
            self.assertEqual(restored, original)
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
