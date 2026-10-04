"""Foreground command/turn boundaries through the real normalizer and planner."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from runtime_ownership import file_lock


class DotsClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mc-dots-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.batch = self.home / "batch"
        self.root = self.home / "observation"
        self.value = {"request_id": "synthetic-oats", "recipes": [{"key": "oats", "recipe": {
            "name": "Synthetic oats", "portions": 2,
            "ingredients": [{"raw": "100 g havregryn", "item": "havregryn", "quantity": 100,
                             "unit": "g", "scalable": True}], "steps": ["Cook the oats."],
            "source": {"kind": "user", "publisher": "Synthetic kitchen", "relationship": "user_supplied"},
            "rights": {"storage": "full", "credit": "Synthetic test"}}}],
            "menu": [{"recipe": "oats", "date": "2026-10-06", "portions": 6}]}
        code, output = self.run_command("on_demand.py", ["create", "--root", str(self.batch)], self.value)
        self.assertEqual(code, 0, output)
        self.batch_result = output["result"]

    def run_command(self, script, arguments, value=None, *, offline=False):
        argv = [sys.executable, "-I", "-B"]
        if offline:
            launcher = ("import sys,runpy\n"
                        "def audit(event,args):\n"
                        " if event.startswith('socket.') or event in ('subprocess.Popen','os.system','urllib.Request'):"
                        " raise AssertionError('unexpected external effect '+event)\n"
                        "sys.addaudithook(audit)\n"
                        f"sys.argv[0]={str(SOURCE / script)!r}\n"
                        "runpy.run_path(sys.argv[0],run_name='__main__')\n")
            argv += ["-c", launcher]
        else:
            argv += [str(SOURCE / script)]
        run = subprocess.run(argv + arguments, input=json.dumps(value).encode() if value is not None else b"",
                             capture_output=True, timeout=90)
        self.assertEqual(run.stderr, b"", run.stderr.decode())
        return run.returncode, json.loads(run.stdout)

    def command(self, action, value=None, *, offline=False):
        arguments = [action]
        if action in {"requirements", "request"}:
            arguments += ["--batch", str(self.batch)]
        if action != "requirements":
            arguments += ["--root", str(self.root)]
        return self.run_command("clients/dots.py", arguments, value, offline=offline)

    def issue(self):
        code, output = self.command("requirements", offline=True)
        self.assertEqual(code, 0, output)
        requirement = output["result"]["requirements"][0]
        self.assertEqual(requirement["quantity"], {"numerator": 300, "denominator": 1})
        code, output = self.command("request", {"requirement_id": requirement["requirement_id"]}, offline=True)
        self.assertEqual(code, 0, output)
        self.request = output["result"]
        return {"request_id": self.request["request_id"], "query": self.request["query"],
                "source_url": "https://meny.no/varer/middag/havregryn-1234567890123",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "products": [{"product_id": "/varer/middag/havregryn-1234567890123",
                              "name": "Synthetic oats", "package": "500 g", "price": "20,00 kr",
                              "detail_price": "20,00 kroner.", "deposit_status": "none", "available": True}],
                "candidate_refs": ["/varer/middag/havregryn-1234567890123"]}

    def files(self):
        return {str(p.relative_to(self.home)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.home.rglob("*") if p.is_file()}

    def test_real_planning_later_command_and_immutable_batch(self):
        value = self.issue()
        before = {str(p.relative_to(self.batch)): p.read_bytes() for p in self.batch.rglob("*") if p.is_file()}
        code, output = self.command("plan", value, offline=True)
        self.assertEqual(code, 0, output)
        result = output["result"]
        self.assertEqual(result["status"], "candidate_plan", result)
        self.assertFalse(result["dispatchable"])
        self.assertEqual(result["candidate_totals"]["package_count"], 1)
        self.assertEqual(result["candidate_totals"]["total_payable_ore"], 2000)
        self.assertEqual(result["provenance"]["kind"], "host_attested")
        self.assertEqual(result["requirements"][0]["observation"]["scope"]["kind"], "host_observation")
        self.assertNotIn("product_plan_digest", result)
        self.assertEqual(before, {str(p.relative_to(self.batch)): p.read_bytes() for p in self.batch.rglob("*") if p.is_file()})
        self.assertEqual(hashlib.sha256((self.batch / "export.pdf").read_bytes()).hexdigest(),
                         self.batch_result["artifact"]["sha256"])
        retained = self.files()
        for action in ("inspect", "plan"):
            code, later = self.command(action, value if action == "plan" else None, offline=True)
            self.assertEqual(code, 0, later)
            self.assertEqual(later["result"], result)
            self.assertEqual(retained, self.files())
        changed = deepcopy(value)
        changed["products"][0]["price"] = "1,00"
        code, conflict = self.command("plan", changed)
        self.assertEqual(code, 1, conflict)
        self.assertIn("conflicts", conflict["error"])
        self.assertEqual(retained, self.files())

    def test_unknown_fields_remain_unresolved(self):
        value = self.issue()
        value["products"] = [{"product_id": value["candidate_refs"][0], "name": "Synthetic oats"}]
        code, output = self.command("plan", value, offline=True)
        self.assertEqual(code, 0, output)
        result = output["result"]
        self.assertEqual(result["status"], "needs_input")
        self.assertIsNone(result["candidate_totals"])
        product = result["requirements"][0]["observation"]["products"][0]
        self.assertEqual(product["availability"], "unknown")
        self.assertNotIn("package", product)
        self.assertEqual(product["purchase_options"][0]["price_kind"], "unavailable")

    def test_invalid_observations_never_accept_or_mutate(self):
        value = self.issue()
        variants = [{**value, "query": "different"}, {**value, "request_id": "different"},
                    {**value, "source_url": "https://example.com/varer"},
                    {**value, "source_url": "https://meny.no/varer/../konto"},
                    {**value, "source_url": "https://meny.no/varer/%252e%252e/konto"},
                    {**value, "source_url": "https://meny.no/varer/foo%2fbar"},
                    {**value, "observed_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
                    {**value, "observed_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
                    {**value, "checkout": True},
                    {**value, "products": [{"product_id": "/varer/not-a-product", "name": "Bad path"}]},
                    {**value, "products": [{"product_id": "/varer/../konto/x-1234", "name": "Bad path"}]},
                    {**value, "products": [{"product_id": "/varer/%2e%2e/konto/x-1234", "name": "Bad path"}]},
                    {**value, "products": [{"product_id": value["candidate_refs"][0], "name": "\ud800"}]},
                    {**value, "candidate_refs": ["/varer/middag/unobserved-1234567890123"]},
                    {**value, "products": [{"product_id": value["candidate_refs"][0], "name": "x" * 66000}]}]
        before = self.files()
        for invalid in variants:
            with self.subTest(invalid=invalid.keys()):
                code, result = self.command("plan", invalid)
                self.assertEqual(code, 1, result)
                self.assertEqual(before, self.files())

    def test_missing_snapshot_lock_busy_and_partial_publication_fail_without_replay(self):
        value = self.issue()
        with file_lock(self.root / "command.lock"):
            code, busy = self.command("plan", value)
        self.assertEqual(code, 1, busy)
        self.assertIn("already owned", busy["error"])
        self.assertFalse((self.root / "observation.json").exists())
        # Preserve an accepted observation whose result publication was interrupted.
        data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        (self.root / "observation.json").write_bytes(data)
        before = self.files()
        code, result = self.command("plan", value)
        self.assertEqual(code, 1, result)
        self.assertEqual(before, self.files())
        code, result = self.command("inspect")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["result"]["status"], "incomplete")
        (self.batch / "menu.json").unlink()
        before = self.files()
        code, result = self.command("plan", value)
        self.assertEqual(code, 1, result)
        self.assertEqual(before, self.files())
        self.assertFalse((self.batch / "menu.json").exists())

    def full_menu_observations(self):
        self.value["recipes"][0]["recipe"]["ingredients"].append(
            {"raw": "200 ml melk", "item": "melk", "quantity": 200, "unit": "ml", "scalable": True})
        self.batch = self.home / "full-menu"
        code, output = self.run_command("on_demand.py", ["create", "--root", str(self.batch)], self.value)
        self.assertEqual(code, 0, output)
        self.batch_result = output["result"]
        code, output = self.command("requirements")
        self.assertEqual(code, 0, output)
        roots = []
        for requirement in output["result"]["requirements"]:
            self.root = self.home / requirement["item"]
            roots.append(str(self.root))
            code, issued = self.command("request", {"requirement_id": requirement["requirement_id"]})
            self.assertEqual(code, 0, issued)
            request = issued["result"]
            milk = requirement["item"] == "melk"
            product = {"product_id": f"/varer/middag/{requirement['item']}-{'9876543210123' if milk else '1234567890123'}",
                       "name": f"Synthetic {requirement['item']}", "package": "1 l" if milk else "500 g",
                       "price": "30,00 kr" if milk else "20,00 kr",
                       "detail_price": "30,00 kroner." if milk else "20,00 kroner.",
                       "deposit_status": "none", "available": True}
            observation = {"request_id": request["request_id"], "query": request["query"],
                           "source_url": "https://meny.no" + product["product_id"],
                           "observed_at": datetime.now(timezone.utc).isoformat(),
                           "products": [product], "candidate_refs": [product["product_id"]]}
            code, planned = self.command("plan", observation)
            self.assertEqual(code, 0, planned)
            self.assertEqual(planned["result"]["status"], "needs_input")  # Other ingredient still absent.
        return roots

    def combine(self, roots, name="combined"):
        return self.run_command("clients/dots.py", ["combine", "--batch", str(self.batch),
                                                    "--root", str(self.home / name)],
                                {"observation_roots": roots}, offline=True)

    def test_whole_menu_combination_and_later_inspect_preserve_originals(self):
        roots = self.full_menu_observations()
        before = self.files()
        code, output = self.combine(roots)
        self.assertEqual(code, 0, output)
        result = output["result"]
        self.assertEqual(result["status"], "candidate_plan", result)
        self.assertFalse(result["dispatchable"])
        self.assertEqual(result["candidate_totals"]["package_count"], 2)
        self.assertEqual(result["candidate_totals"]["total_payable_ore"], 5000)
        self.assertEqual({r["item"]: r["quantity"]["numerator"] for r in result["requirements"]},
                         {"havregryn": 300, "melk": 600})
        self.assertEqual(len(result["provenance"]["observations"]), 2)
        self.assertEqual(before, {k: v for k, v in self.files().items() if not k.startswith("combined/")})
        retained = self.files()
        code, inspected = self.run_command("clients/dots.py", ["inspect", "--root", str(self.home / "combined")],
                                           offline=True)
        self.assertEqual(code, 0, inspected)
        self.assertEqual(inspected["result"], result)
        self.assertEqual(retained, self.files())
        code, conflict = self.combine(roots)
        self.assertEqual(code, 1, conflict)
        self.assertEqual(retained, self.files())
        # Expiry is not silently extended; historical evidence is labelled.
        self.assertIn("not refreshed", result["provenance"]["freshness"])

    def test_combination_reports_missing_evidence_and_rejects_duplicates_other_batch(self):
        roots = self.full_menu_observations()
        code, partial = self.combine(roots[:1], "partial")
        self.assertEqual(code, 0, partial)
        self.assertEqual(partial["result"]["status"], "needs_input")
        self.assertIsNone(partial["result"]["candidate_totals"])
        self.assertTrue(any(r["reason"] == "provider_search_unavailable"
                            for r in partial["result"]["unresolved_requirements"]))
        before = self.files()
        for invalid in (roots + roots[:1], [], [str(self.home / "missing")]):
            code, error = self.combine(invalid, "invalid")
            self.assertEqual(code, 1, error)
            self.assertEqual(before, self.files())
        other = self.home / "other-batch"
        code, output = self.run_command("on_demand.py", ["create", "--root", str(other)], self.value)
        self.assertEqual(code, 0, output)
        self.batch = other
        before = self.files()
        code, error = self.combine(roots, "wrong-batch")
        self.assertEqual(code, 1, error)
        self.assertEqual(before, self.files())

    def test_combination_inspect_detects_changed_inputs_and_never_replays_partial_publication(self):
        roots = self.full_menu_observations()
        code, output = self.combine(roots)
        self.assertEqual(code, 0, output)
        combined = self.home / "combined"
        original = (combined / "result.json").read_bytes()
        (combined / "result.json").unlink()  # Interrupted publication fixture.
        before = self.files()
        code, output = self.run_command("clients/dots.py", ["inspect", "--root", str(combined)], offline=True)
        self.assertEqual(code, 0, output)
        self.assertEqual(output["result"]["status"], "incomplete")
        self.assertEqual(before, self.files())
        (combined / "result.json").write_bytes(original)
        source = Path(roots[0]) / "observation.json"
        value = json.loads(source.read_bytes())
        value["products"][0]["price"] = "1,00 kr"
        source.write_text(json.dumps(value))
        before = self.files()
        code, output = self.run_command("clients/dots.py", ["inspect", "--root", str(combined)], offline=True)
        self.assertEqual(code, 1, output)
        self.assertEqual(before, self.files())


if __name__ == "__main__":
    unittest.main()
