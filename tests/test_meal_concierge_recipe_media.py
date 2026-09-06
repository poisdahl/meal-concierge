"""Frozen production renderer and managed MIME boundary tests."""
from copy import deepcopy
from email import policy
from email.parser import BytesParser
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT
if not CORE.exists():
    CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))

from PIL import Image
from core import HouseholdError
from recipe_assets import RecipeAssets, asset_filename
from recipe_email import build_message, prepare_recipe_media
from service_common import menu_email_html


def picture(color):
    output = io.BytesIO()
    with Image.new("RGB", (40, 20), color) as image:
        image.save(output, "PNG")
    return output.getvalue()


class FrozenRecipeMimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = RecipeAssets(self.root / "recipe-assets")
        self.first = self.assets.import_bytes(picture("red"))
        self.frozen = {"week": "2026-W37", "dishes": [{"name": "Synthetic soup",
            "ingredients": [{"amount": "200 g", "item": "carrots"}], "steps": ["Cook the carrots."],
            "rights": {"credit": "Synthetic author"}, "image": {
            "asset_id": self.first, "alt": "Soup", "credit": "Synthetic cover creator",
            "license": "Fixture only", "source_url": "https://example.invalid/cover",
        }}]}

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, *, supported=True):
        media = prepare_recipe_media(self.frozen, self.assets, images_supported=supported)
        return {"recipient": "recipient@example.invalid", "subject": "Synthetic recipe email",
                "html": menu_email_html(self.frozen, image_cids=media["image_cids"]),
                "html_without_images": menu_email_html(self.frozen),
                "inline_images": media["inline_images"]}

    def parsed(self, payload, assets=None):
        message = build_message(payload, assets or self.assets, sender="sender@example.invalid")
        return BytesParser(policy=policy.default).parsebytes(message.as_bytes())

    def test_actual_mime_parts_resolve_each_frozen_cid_and_keep_both_credits(self):
        payload = self.payload()
        message = self.parsed(payload)
        self.assertEqual(str(message["To"]), payload["recipient"])
        text = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("200 g carrots", text)
        self.assertIn("Synthetic author", text)
        self.assertIn("Synthetic cover creator", text)
        self.assertIn("https://example.invalid/cover", text)
        self.assertNotIn("font-family", text)
        html = message.get_body(preferencelist=("html",)).get_content()
        parts = [part for part in message.walk() if part.get_content_type() == "image/jpeg"]
        self.assertEqual(len(parts), 1)
        for descriptor, part in zip(payload["inline_images"], parts):
            self.assertIn("cid:" + descriptor["content_id"], html)
            self.assertEqual(part["Content-ID"], "<" + descriptor["content_id"] + ">")
            self.assertEqual(part.get_payload(decode=True), self.assets.read(descriptor["asset_id"]))
            self.assertEqual(part.get_content_disposition(), "inline")
            self.assertFalse(Image.open(io.BytesIO(part.get_payload(decode=True))).getexif())

    def test_unsupported_destination_does_not_access_assets_and_keeps_text(self):
        with mock.patch.object(self.assets, "read", side_effect=AssertionError("unsupported destination must not access images")):
            media = prepare_recipe_media(self.frozen, self.assets)
            self.assertFalse(media["inline_images"])
            payload = self.payload(supported=False)
            message = self.parsed(payload)
        self.assertFalse([part for part in message.walk() if part.get_content_type().startswith("image/")])
        self.assertIn("Synthetic cover creator", message.get_body(preferencelist=("plain",)).get_content())

    def test_replacement_and_relocated_restore_use_original_frozen_cover(self):
        payload = self.payload()
        original = self.assets.read(self.first)
        replacement = self.assets.import_bytes(picture("blue"))
        self.assertNotEqual(replacement, self.first)
        restored = RecipeAssets(self.root / "restored-assets")
        for asset_id in (self.first, replacement):
            restored.install_managed(asset_id, self.assets.read(asset_id))
        shutil.rmtree(self.assets.root)
        message = self.parsed(payload, restored)
        parts = [part for part in message.walk() if part.get_content_type() == "image/jpeg"]
        self.assertEqual(parts[0].get_payload(decode=True), original)
        self.assertNotIn(replacement, message.as_string())

    def test_missing_or_corrupt_at_prepare_and_sender_time_falls_back_without_cids(self):
        payload = self.payload()
        leaf = self.assets.root / asset_filename(self.first)
        for corruption in (None, b"broken"):
            if corruption is None:
                leaf.unlink()
            else:
                leaf.write_bytes(corruption)
            media = prepare_recipe_media(self.frozen, self.assets, images_supported=True)
            self.assertFalse(media["inline_images"])
            self.assertEqual(media["image_warnings"][0]["reason"], "cover_missing_or_invalid")
            message = self.parsed(payload)
            self.assertNotIn("cid:", message.get_body(preferencelist=("html",)).get_content())
            self.assertIn("Synthetic soup", message.get_body(preferencelist=("plain",)).get_content())
            self.assertFalse([part for part in message.walk() if part.get_content_type().startswith("image/")])

    def test_duplicate_cover_has_one_related_part(self):
        self.frozen["salads"] = deepcopy(self.frozen["dishes"])
        payload = self.payload()
        self.assertEqual(len(payload["inline_images"]), 1)
        message = self.parsed(payload)
        self.assertEqual(len([part for part in message.walk() if part.get_content_type() == "image/jpeg"]), 1)

    def test_bad_descriptor_and_unresolved_html_reference_use_text_fallback(self):
        for field, value in (("asset_id", "../../outside"), ("filename", "/private/outside"),
                             ("content_id", "different@host"), ("content_type", "text/html")):
            payload = self.payload()
            payload["inline_images"][0][field] = value
            message = self.parsed(payload)
            self.assertNotIn("cid:", message.get_body(preferencelist=("html",)).get_content())
        payload = self.payload()
        payload["html"] += '<img src="https://example.invalid/tracker">'
        self.assertNotIn("tracker", self.parsed(payload).get_body(preferencelist=("html",)).get_content())

    def test_invalid_headers_do_not_create_extra_recipients(self):
        payload = self.payload(supported=False)
        payload["subject"] = "Recipe\r\nBcc: unexpected@example.invalid"
        with self.assertRaises(HouseholdError):
            self.parsed(payload)

    def test_metadata_text_containing_cid_is_not_an_image_reference(self):
        payload = self.payload(supported=False)
        payload["html"] = payload["html_without_images"] = "<p>Source note cid:literal</p>"
        self.assertIn("cid:literal", self.parsed(payload).get_body(preferencelist=("plain",)).get_content())

    def test_renderer_escapes_independent_image_metadata_and_rejects_remote_sources(self):
        cover = self.frozen["dishes"][0]["image"]
        cover.update({"alt": '\"><script>bad()</script>', "creator": "<creator>",
                      "credit": "<credit>", "changes": "<changes>",
                      "license": "<license>", "license_url": "javascript:bad()",
                      "source_url": "javascript:bad()"})
        rendered = self.payload()["html"]
        for text in ("creator", "credit", "changes", "license"):
            self.assertIn("&lt;" + text + "&gt;", rendered)
        self.assertIn("Synthetic author", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("<img", menu_email_html(self.frozen, image_cids={self.first: "https://example.invalid/tracker"}))


if __name__ == "__main__":
    unittest.main()
