"""Frozen delivery through Application, Unix RPC/CLI and a loopback-only SMTP sink."""
from __future__ import annotations

from copy import deepcopy
import asyncio
from email import policy
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import smtplib
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from core import HouseholdError, StateStore
from delivery_operations import legacy_id
from delivery_transport import export_part, send_smtp
from menu_planning import menu_ref
from recipe_assets import asset_filename
from service import Application, Server


class Provider:
    def probe(self, **kwargs):
        return {"server": {"name": "synthetic"}, "tool_count": 0}

    def call(self, *args, **kwargs):
        raise AssertionError("recipe delivery must not call the grocery provider")


def fixture(root, *, long=False, count=None):
    app = Application(StateStore(root / "state", {"household": "Synthetic delivery", "provider": "oda"}), Provider(), None)
    app.handle({"operation": "setup", "action": "apply", "keep_current": True})
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (900, 450), "#e0eadc")
    draw = ImageDraw.Draw(image)
    draw.ellipse((270, 45, 630, 405), fill="#fffdf6")
    draw.ellipse((300, 75, 600, 375), fill="#cf7639")
    draw.text((25, 25), "SYNTHETIC TEST COVER", fill="#173f35")
    stream = io.BytesIO()
    image.save(stream, "PNG")
    asset = app.recipes.assets.import_bytes(stream.getvalue())
    recipes = []
    for index in range(count or (3 if long else 1)):
        recipe = {"name": f"Gulrotsuppe med røde bønner – prøve {index + 1}", "portions": 2,
            "ingredients": [{"raw": "200 g gulrot", "item": "gulrot", "quantity": 200, "unit": "g", "scalable": True},
                            {"raw": "1 l vann", "item": "vann", "quantity": 1, "unit": "l", "scalable": True}],
            "steps": [(f"Steg {n + 1}: Skyll bønnene og skjær gulrøttene. Rør forsiktig og smak til. " +
                      ("La suppen småkoke mens du rører langs bunnen. Ærlig kjøkkenprøve med norske mål. " * 12 if long else "")) for n in range(12 if long else 2)],
            "source": {"kind": "user", "publisher": "Syntetisk testkjøkken", "relationship": "user_supplied"},
            "rights": {"storage": "full", "credit": "Oppskrift: syntetisk tekst laget for leveringsprøven"},
            "image": {"asset_id": asset, "alt": "Syntetisk illustrasjon, ikke et matfoto", "creator": "Testfixture",
                      "credit": "Bilde: geometrisk syntetisk testillustrasjon", "license": "Test only"}}
        saved = app.handle({"operation": "recipes", "action": "save", "recipe": recipe, "idempotency_key": f"seed-{index}"})["recipe"]
        recipes.append({"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}, "portions": 6})
    menu = app.handle({"operation": "menu", "action": "save", "menu": {"week": "2026-W37", "dishes": recipes,
                       "schedule": [{"day": f"2026-09-{7 + index:02}", "meal": f"Gulrotsuppe, prøve {index + 1}", "portions": 6}
                                    for index in range(len(recipes))]}})["menu"]
    return app, menu, asset


def chat_cap(**changes):
    return {"transport": "synthetic-native", "verified": True, "evidence": "fixture:capability-inspection",
            "text_limit": 2000, "attachment_limit": 4000000, "pdf": True, "images": True, **changes}


def email_cap(**changes):
    return {"transport": "smtp", "verified": True, "evidence": "fixture:loopback-smtp-inspection",
            "sender": "sender@example.test", "message_limit": 5000000, "attachment_limit": 4000000,
            "pdf": True, "images": True, **changes}


DEST = {"chat": {"platform": "synthetic-native", "conversation": "synthetic-conversation-1"},
        "email": {"sender": "sender@example.test", "recipient": "recipient@example.test"}}


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 synthetic\r\n")
        while line := self.rfile.readline():
            verb = line.split(b" ", 1)[0].strip().upper()
            if verb in {b"EHLO", b"HELO", b"MAIL", b"RCPT", b"RSET"}:
                self.wfile.write(b"250 OK\r\n")
            elif verb == b"DATA":
                self.wfile.write(b"354 continue\r\n")
                lines = []
                while (body := self.rfile.readline()) not in {b".\r\n", b""}:
                    lines.append(body[1:] if body.startswith(b"..") else body)
                self.server.messages.append(b"".join(lines))
                self.wfile.write(b"250 accepted-synthetic\r\n")
            elif verb == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mc53-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app, self.menu, self.asset = fixture(self.root)

    def call(self, action, **kw):
        return self.app.handle({"operation": "recipe_delivery", "action": action, **kw})

    def request(self, request_id="one", **kw):
        return self.call("request", request_id=request_id, delivery_requested=True, menu_ref=menu_ref(self.menu),
                         **{"destinations": {"chat": DEST["chat"]}, "capabilities": {"chat": chat_cap()}, **kw})

    def both(self):
        return self.call("configure", changes={"email": {"enabled": True}}, capabilities={"email": email_cap()}, destinations={"email": DEST["email"]})

    def parts(self, job):
        parts = list(job["parts"])
        while job["next_offset"] is not None:
            job = self.call("get", job_id=job["id"], offset=job["next_offset"])
            parts.extend(job["parts"])
        return parts

    def test_new_defaults_real_pdf_scaled_text_cover_and_frozen_bytes(self):
        status = self.call("status")
        self.assertTrue(status["preferences"]["chat"]["enabled"])
        self.assertFalse(status["preferences"]["email"]["enabled"])
        self.assertEqual([], status["jobs"])
        self.app.handle({"operation": "menu", "action": "get"})
        self.assertEqual([], self.call("status")["jobs"])
        job = self.request()
        parts = self.parts(job)
        text = "\n".join(self.call("read", job_id="one", part_id=p["id"])["text"] for p in parts if p["kind"] == "text")
        for wanted in ("600 g gulrot", "3 l vann", "6 porsjoner", "Syntetisk testkjøkken", "Bilde:", "2026-09-07"):
            self.assertIn(wanted, text)
        pdf = next(p for p in parts if p["kind"] == "pdf")
        def rpc(operation, **kw):
            return self.app.handle({"operation": operation, **kw})
        target = self.root / "menu.pdf"
        export_part(rpc, "one", pdf["id"], target)
        data = target.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertIn(b"/Subtype /Image", data)
        original = deepcopy(self.app.store.read()["recipe_delivery"]["jobs"]["one"])
        self.app.handle({"operation": "menu", "action": "clear", "menu_id": self.menu["menu_id"], "expected_revision": self.menu["revision"]})
        (self.app.store.directory / "recipe-assets" / asset_filename(self.asset)).unlink()
        self.assertEqual(job, self.request())
        export_part(rpc, "one", pdf["id"], self.root / "again.pdf")
        self.assertEqual(data, (self.root / "again.pdf").read_bytes())
        self.assertEqual(original, self.app.store.read()["recipe_delivery"]["jobs"]["one"])

    def test_both_channels_smtp_one_dispatch_and_identical_pdf(self):
        self.both()
        job = self.request(destinations=DEST, capabilities={"chat": chat_cap(), "email": email_cap()})
        parts = self.parts(job)
        pdf = next(p for p in parts if p["kind"] == "pdf")
        email = next(p for p in parts if p["kind"] == "email")
        def rpc(operation, **kw):
            return self.app.handle({"operation": operation, **kw})
        export_part(rpc, "one", email["id"], self.root / "menu.eml")
        raw = (self.root / "menu.eml").read_bytes()
        message = BytesParser(policy=policy.default).parsebytes(raw)
        attachment = next(p.get_payload(decode=True) for p in message.walk() if p.get_content_type() == "application/pdf")
        self.assertEqual(pdf["sha256"], hashlib.sha256(attachment).hexdigest())
        self.assertEqual(1, len([p for p in message.walk() if p.get_content_disposition() == "attachment"]))
        begun = self.call("begin", job_id="one", part_id=email["id"])
        with socketserver.TCPServer(("127.0.0.1", 0), SMTPHandler) as server:
            server.messages = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with smtplib.SMTP(*server.server_address, timeout=5) as smtp:
                    receipt = send_smtp(raw, smtp)
                self.call("ack", job_id="one", part_id=email["id"], token=begun["token"], outcome=receipt["outcome"], evidence=receipt["evidence"])
                self.assertEqual(1, len(server.messages))
                self.assertFalse(self.call("begin", job_id="one", part_id=email["id"])["dispatch"])
                self.assertEqual("recipient@example.test", str(BytesParser(policy=policy.default).parsebytes(server.messages[0])["To"]))
            finally:
                server.shutdown()
                thread.join()

    def test_unknown_partial_pause_resume_and_destination_drift(self):
        job = self.request()
        parts = self.parts(job)
        begun = self.call("begin", job_id="one", part_id=parts[0]["id"])
        self.assertFalse(self.call("begin", job_id="one", part_id=parts[0]["id"])["dispatch"])
        self.assertEqual(begun["token"], self.call("get", job_id="one", part_id=parts[0]["id"])["parts"][0]["token"])
        with self.assertRaises(HouseholdError):
            self.call("retry", job_id="one", part_id=parts[0]["id"])
        self.call("pause")
        self.call("reconcile", job_id="one", part_id=parts[0]["id"], token=begun["token"], outcome="accepted", evidence="native:message-1")
        held = self.call("status")
        self.assertNotIn("one/" + parts[0]["id"], held["held_work"])
        with self.assertRaises(HouseholdError):
            self.call("resume", held_work=[])
        self.call("resume", held_work_digest=held["held_work_digest"])
        self.assertFalse(self.call("begin", job_id="one", part_id=parts[1]["id"])["dispatch"])
        self.call("release_hold", job_id="one", part_id=parts[1]["id"])
        new = self.call("begin", job_id="one", part_id=parts[1]["id"])
        self.assertTrue(new["dispatch"])
        self.call("ack", job_id="one", part_id=parts[1]["id"], token=new["token"], outcome="not_sent", evidence="native:definite-rejection")
        self.call("retry", job_id="one", part_id=parts[1]["id"])
        self.assertTrue(self.call("begin", job_id="one", part_id=parts[1]["id"])["dispatch"])
        with self.assertRaises(HouseholdError):
            self.request(destinations={"chat": {**DEST["chat"], "conversation": "new"}})

    def test_missing_corrupt_images_pdf_failure_and_no_attachment_support(self):
        (self.app.store.directory / "recipe-assets" / asset_filename(self.asset)).write_bytes(b"corrupt")
        job = self.request(capabilities={"chat": chat_cap(images=False)})
        self.assertTrue(any("cover_missing_or_invalid" in w for w in job["warnings"]))
        with mock.patch("delivery_operations.render_pdf", side_effect=RuntimeError("private path must not escape")):
            failed = self.request("pdf-failure")
        self.assertTrue(any("PDF generation failed" in w for w in failed["warnings"]))
        self.assertNotIn("private path", json.dumps(failed))
        unsupported = self.request("no-files", capabilities={"chat": chat_cap(pdf=False, images=False, attachment_limit=0)})
        self.assertTrue(all(p["kind"] == "text" for p in self.parts(unsupported)))

    def test_email_optin_and_too_large_keeps_readable_fallback(self):
        with self.assertRaisesRegex(HouseholdError, "verified sender"):
            self.call("configure", changes={"email": {"enabled": True}}, capabilities=None, destinations=None)
        self.both()
        self.call("disable", channel="chat")
        job = self.request(destinations={"email": DEST["email"]}, capabilities={"email": email_cap(message_limit=256)})
        self.assertFalse(job["all_accepted"])
        for part in self.parts(job):
            self.assertEqual("text_fallback", part["kind"])
            self.assertIn("text", self.call("read", job_id="one", part_id=part["id"]))
            self.assertFalse(self.call("begin", job_id="one", part_id=part["id"])["dispatch"])
        self.call("disable", channel="email")
        with self.assertRaisesRegex(HouseholdError, "configured channel"):
            self.call("automatic")

    def test_email_inline_images_obey_native_attachment_limit(self):
        self.both()
        self.call("disable", channel="chat")
        job = self.request(destinations={"email": DEST["email"]},
                           capabilities={"email": email_cap(attachment_limit=1, pdf=False)})
        email = next(p for p in self.parts(job) if p["kind"] == "email")
        target = self.root / "limited.eml"
        export_part(lambda op, **kw: self.app.handle({"operation": op, **kw}), "one", email["id"], target)
        message = BytesParser(policy=policy.default).parsebytes(target.read_bytes())
        self.assertEqual([], [p for p in message.walk() if p.get_content_type().startswith("image/")])
        self.assertNotIn("<img", message.get_body(preferencelist=("html",)).get_content())
        self.assertIn("Bilde:", message.get_body(preferencelist=("plain",)).get_content())
        self.assertTrue(any("inline attachment limit" in warning for warning in job["warnings"]))

    def test_split_prefers_whole_step_section_that_fits_a_fresh_part(self):
        from recipe_delivery import split_text
        steps = "<h3>Fremgangsmåte</h3><ol>" + "".join(f"<li>Steg {n}: Rør gulrøttene grundig.</li>" for n in range(4)) + "</ol>"
        document = '<section class="recipe"><h2>Suppe</h2><h3>Ingredienser</h3><ul>' + "".join("<li>200 g gulrøtter</li>" for _ in range(4)) + "</ul>" + steps + "</section>"
        parts = split_text(document, 256)
        step_parts = [p for p in parts if "Steg " in p]
        self.assertEqual(1, len(step_parts))
        self.assertIn("Steg 0:", step_parts[0])
        self.assertIn("Steg 3:", step_parts[0])

    def test_migration_does_not_rewrite_legacy_frozen_jobs(self):
        with self.app.store.locked() as state:
            state.pop("recipe_delivery")
            state["email_recipient"] = "old@example.test"
        before = self.app.store.read()
        reopened = StateStore(self.app.store.directory, self.app.store.config).read()
        self.assertEqual(before["email_jobs"], reopened["email_jobs"])
        self.assertEqual("old@example.test", reopened["email_recipient"])
        self.assertFalse(reopened["recipe_delivery"]["preferences"]["chat"]["enabled"])
        self.assertTrue(reopened["recipe_delivery"]["preferences"]["email"]["enabled"])
        self.assertEqual({}, reopened["recipe_delivery"]["jobs"])

    def test_split_long_menu_complete_and_bounded_paginated_recovery(self):
        app, menu, _asset = fixture(self.root / "long", long=True)
        self.app, self.menu = app, menu
        job = self.request(capabilities={"chat": chat_cap(text_limit=256)})
        parts = self.parts(job)
        texts = [self.call("read", job_id="one", part_id=p["id"])["text"] for p in parts if p["kind"] == "text"]
        self.assertGreater(len(texts), 25)
        self.assertTrue(all(len(t.encode()) <= 256 for t in texts))
        joined = "".join(texts)
        self.assertEqual(3, joined.count("Steg 12:"))
        self.assertEqual(3, joined.count("600 g gulrot"))
        last = parts[-1]
        begin = self.call("begin", job_id="one", part_id=last["id"])
        self.assertEqual(begin["token"], self.call("get", job_id="one", part_id=last["id"])["parts"][0]["token"])

    def test_cli_and_real_mcp_transfer_without_sender_or_purchase(self):
        root = self.root / "rpc"
        root.mkdir()
        log = (root / "service.log").open("w+")
        self.addCleanup(log.close)
        process = subprocess.Popen([sys.executable, str(Path(__file__)), "--serve", str(root)], stdout=log, stderr=log)
        def stop():
            process.terminate()
            process.wait(timeout=10)
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not (root / "s.sock").exists():
            self.assertIsNone(process.poll())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        env = {**os.environ, "MEAL_CONCIERGE_SOCKET": str(root / "s.sock")}
        def cli(request, *args):
            result = subprocess.run([sys.executable, "-I", str(SOURCE / "cli.py"), *args],
                input=json.dumps(request), text=True, capture_output=True, env=env, timeout=20)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return json.loads(result.stdout)["result"]
        menu = cli({"operation": "menu", "action": "get"})["menu"]
        async def native_mcp():
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
            parameters = StdioServerParameters(command=sys.executable, args=["-I", str(SOURCE / "mcp_server.py")], env=env)
            async with stdio_client(parameters, errlog=log) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=30) as client:
                    await client.initialize()
                    result = await client.call_tool("meal_concierge_recipe_delivery", {"action": "request", "request_id": "mcp-one",
                        "delivery_requested": True, "menu_ref": menu_ref(menu), "destinations": {"chat": DEST["chat"]},
                        "capabilities": {"chat": chat_cap()}})
                    self.assertFalse(result.is_error)
                    return result.structured_content
        job = asyncio.run(native_mcp())
        pdf = next(p for p in job["parts"] if p["kind"] == "pdf")
        target = root / "native-transfer.pdf"
        result = cli({"operation": "recipe_delivery", "action": "read", "job_id": "mcp-one", "part_id": pdf["id"]},
                     "--delivery-output", str(target))
        self.assertFalse(result["sent"])
        self.assertEqual(pdf["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        with self.assertRaises(AssertionError):
            cli({"operation": "recipe_delivery", "action": "read", "job_id": "mcp-one", "part_id": pdf["id"]},
                "--delivery-output", str(target))

    def test_pause_blocks_legacy_claim_at_original_dispatch_gate(self):
        sys.path.insert(0, str(SOURCE / "tests"))
        from test_email_scheduler import EmailSchedulerTests
        original = EmailSchedulerTests()
        original.setUp()
        self.addCleanup(original.tearDown)
        invocation = original.active()
        claimed = original.call("due", scheduler=invocation)
        before = deepcopy(original.store.read())
        def control(action, **kw):
            return original.app.handle({"operation": "recipe_delivery", "action": action, **kw})
        control("pause")
        stopped = original.call("begin_send", scheduler=invocation, claim_token=claimed["claim_token"])
        self.assertFalse(stopped["dispatch"])
        state = original.store.read()
        self.assertEqual(before["schedule"], state["schedule"])
        self.assertEqual(before["email_jobs"][0]["menu_snapshot"], state["email_jobs"][0]["menu_snapshot"])
        held = control("status")
        control("resume", held_work_digest=held["held_work_digest"])
        self.assertFalse(original.call("begin_send", scheduler=invocation, claim_token=claimed["claim_token"])["dispatch"])
        control("release_order_hold", job_id=legacy_id(state["email_jobs"][0]))
        self.assertTrue(original.call("begin_send", scheduler=invocation, claim_token=claimed["claim_token"])["dispatch"])

    def test_renderer_treats_hostile_content_as_text_and_preserves_missing_facts(self):
        from recipe_delivery import render_menu, render_pdf
        menu = deepcopy(self.menu)
        recipe = menu["dishes"][0]
        recipe["name"] = '<img src="file:///private/secret"><script>execute()</script>'
        recipe["source"] = {"publisher": "Actual generator", "relationship": "generated"}
        recipe["steps"] = []
        rendered = render_menu(menu, self.app.recipes.assets)
        self.assertIn("&lt;img", rendered["html"])
        self.assertIn("Actual generator", rendered["text"])
        self.assertNotIn("Hermes", rendered["text"])
        self.assertIn("Fremgangsmåte mangler", rendered["text"])
        with mock.patch("socket.socket.connect", side_effect=AssertionError("network attempted")):
            data = render_pdf(rendered)
        self.assertTrue(data.startswith(b"%PDF-"))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        root = Path(sys.argv[2])
        app, _menu, _asset = fixture(root)
        Server(root / "s.sock", os.getgid(), os.getuid(), app).run()
    else:
        unittest.main()
