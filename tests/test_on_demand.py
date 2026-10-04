"""Observable offline command, PDF and interrupted-publication behavior."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image
import pypdfium2 as pdfium

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import on_demand


def fixture(directory):
    Image.new("RGB", (120, 80), "orange").save(directory / "cover.png")
    return {"request_id": "synthetic-menu", "household": "Synthetic household",
            "recipes": [{"key": "soup", "cover": "cover.png", "recipe": {
                "name": "Synthetic carrot soup", "portions": 2,
                "ingredients": [{"raw": "200 g carrot", "item": "carrot", "quantity": 200, "unit": "g", "scalable": True},
                                {"raw": "1 l water", "item": "water", "quantity": 1, "unit": "l", "scalable": True}],
                "steps": ["Chop the carrots.", "Simmer in the water."],
                "source": {"kind": "user", "publisher": "Synthetic kitchen", "relationship": "user_supplied"},
                "rights": {"storage": "full", "credit": "Recipe credit: synthetic test author"},
                "image": {"alt": "Synthetic orange rectangle", "creator": "Test fixture",
                          "credit": "Cover credit: synthetic geometry", "license": "CC0"}}}],
            "menu": [{"recipe": "soup", "date": "2026-10-05", "portions": 6}]}


class OnDemandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mc-on-demand-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "batch"
        self.value = fixture(self.directory)

    def command(self, action="create", value=None, *, launcher=None):
        argv = [sys.executable, "-I", "-B"]
        argv += ["-c", launcher] if launcher else [str(SOURCE / "on_demand.py")]
        argv += [action, "--root", str(self.root)]
        if action == "create":
            argv += ["--input-directory", str(self.directory)]
        run = subprocess.run(argv, input=json.dumps(value if value is not None else self.value).encode(),
                             capture_output=True, timeout=90)
        self.assertEqual(run.stderr, b"", run.stderr.decode())
        return run.returncode, json.loads(run.stdout)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_real_command_scaled_credited_pdf_and_read_only_recovery(self):
        code, output = self.command()
        self.assertEqual(code, 0, output)
        result = output["result"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["integration"]["status"], "unavailable")
        self.assertFalse(result["sent"])
        self.assertFalse(result["artifact"]["sent"])
        self.assertEqual(set(result["menu_ref"]), {"menu_id", "revision", "digest"})
        data = (self.root / "export.pdf").read_bytes()
        self.assertEqual(len(data), result["artifact"]["bytes"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), result["artifact"]["sha256"])
        self.assertIn(b"/Subtype /Image", data)
        with pdfium.PdfDocument(data) as pdf:
            text = ""
            for page in pdf:
                textpage = page.get_textpage()
                try:
                    text += textpage.get_text_range()
                finally:
                    textpage.close()
                page.close()
        text = " ".join(text.split())
        for expected in ("600 g", "3 l", "6 porsjoner", "2026-10-05", "Chop the carrots.",
                         "Simmer in the water.", "synthetic test author", "synthetic geometry"):
            self.assertIn(expected, text)
        state = json.loads((self.root / "state/state.json").read_text())
        self.assertEqual(state["recipe_delivery"]["jobs"], {})
        before = self.snapshot()
        (self.directory / "cover.png").unlink()
        launcher = (f"import sys; sys.path.insert(0, {str(SOURCE)!r}); import on_demand; "
                    "code = on_demand.main(); assert 'service' not in sys.modules; raise SystemExit(code)")
        code, checked = self.command("inspect", launcher=launcher)
        self.assertEqual(code, 0, checked)
        self.assertEqual(checked["result"], result)
        self.assertEqual(before, self.snapshot())
        # Existing root refusal does not rely on still having an input cover.
        duplicate = deepcopy(self.value)
        duplicate["recipes"][0].pop("cover")
        code, refused = self.command(value=duplicate)
        self.assertEqual(code, 1, refused)
        self.assertEqual(before, self.snapshot())
        (self.root / "export.pdf").write_bytes(data + b"changed")
        code, changed = self.command("inspect")
        self.assertEqual(code, 1, changed)
        self.assertIn("changed", changed["error"])

    def test_no_socket_process_or_network_during_import_and_batch(self):
        launcher = f'''import sys, runpy
def audit(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'urllib.Request'):
        raise AssertionError('offline batch attempted ' + event)
sys.addaudithook(audit)
sys.argv[0] = {str(SOURCE / 'on_demand.py')!r}
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        code, result = self.command(launcher=launcher)
        self.assertEqual(code, 0, result)

    def test_frozen_menu_integrity_and_old_batch_inspection(self):
        code, output = self.command()
        self.assertEqual(code, 0, output)
        metadata = output["result"]["menu_snapshot"]
        menu = (self.root / "menu.json").read_bytes()
        self.assertEqual(hashlib.sha256(menu).hexdigest(), metadata["sha256"])
        self.assertEqual(len(menu), metadata["bytes"])
        (self.root / "menu.json").write_bytes(menu + b"changed")
        code, output = self.command("inspect")
        self.assertEqual(code, 1, output)
        self.assertIn("menu is missing or changed", output["error"])
        # Earlier releases did not publish a materialized menu. They remain
        # inspectable without initializing state or regenerating artifacts.
        record = json.loads((self.root / "result.json").read_text())
        record.pop("menu_snapshot")
        (self.root / "result.json").write_text(json.dumps(record))
        (self.root / "menu.json").unlink()
        before = self.snapshot()
        code, output = self.command("inspect")
        self.assertEqual(code, 0, output)
        self.assertEqual(before, self.snapshot())

    def test_unsupported_envelope_and_cover_escape_fail_before_state_creation(self):
        for value in ({**self.value, "operation": "checkout"},
                      {**self.value, "capabilities": {"verified": True}},
                      {**self.value, "recipes": [{**self.value["recipes"][0], "cover": "../outside.png"}]}):
            with self.subTest(value=value.keys()):
                code, result = self.command(value=value)
                self.assertEqual(code, 1, result)
                self.assertFalse(self.root.exists())

    def test_empty_sources_do_not_construct_optional_clients(self):
        import service
        with mock.patch.object(service, "TheMealDBSource", side_effect=AssertionError("external source")), \
             mock.patch.object(service, "WikibooksSource", side_effect=AssertionError("external source")):
            app = on_demand._application(self.root, "Synthetic")
        self.assertEqual(app.external_recipe_sources, {})
        self.assertEqual(app.integration["status"], "unavailable")
        self.assertIsNone(app.browser)
        from core import StateStore
        with mock.patch.object(service, "TheMealDBSource") as mealdb, \
             mock.patch.object(service, "WikibooksSource") as wiki:
            default = service.Application(StateStore(self.directory / "defaults", {"household": "Synthetic"}),
                                          app.provider_client, None)
        mealdb.assert_called_once()
        wiki.assert_called_once()
        self.assertEqual(set(default.external_recipe_sources), {"themealdb", "wikibooks"})

    def test_interrupted_completed_publication_remains_incomplete_without_replay(self):
        # Stop the actual child at the publication boundary after it writes the
        # real PDF. There is no production test switch or artificial checkpoint.
        launcher = f'''import sys, time
sys.path.insert(0, {str(SOURCE)!r})
import on_demand
publish = on_demand._publish
def before_publish(root, record):
    if record.get('status') == 'completed':
        print('pdf-written', flush=True)
        time.sleep(60)
    publish(root, record)
on_demand._publish = before_publish
raise SystemExit(on_demand.main())
'''
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", launcher, "create", "--root", str(self.root),
                                    "--input-directory", str(self.directory)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.stdin.write(json.dumps(self.value).encode())
            process.stdin.close()
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(30), "child did not reach publication")
                self.assertEqual(process.stdout.readline(), b"pdf-written\n")
            process.terminate()
            process.wait(timeout=5)
            before = self.snapshot()
            code, result = self.command("inspect")
            self.assertEqual(code, 2, result)
            self.assertEqual(result["result"]["status"], "incomplete")
            self.assertEqual(before, self.snapshot())
            self.assertTrue((self.root / "export.pdf").is_file())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
