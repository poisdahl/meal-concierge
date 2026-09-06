"""Synthetic local cover tests; no downloaded photos, services or recipients."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT
if not CORE.exists():
    CORE = Path(__file__).resolve().parents[1]  # standalone product export
sys.path.insert(0, str(CORE))

from PIL import Image, PngImagePlugin
import recipe_assets as assets
from recipe_assets import RecipeAssets, RecipeAssetError
from recipes import RecipeStore, RecipeError, normalize_recipe, recipe_digest


def picture(format="PNG", size=(80, 40), color="tomato", **options):
    with Image.new("RGB", size, color) as image:
        output = io.BytesIO()
        image.save(output, format, **options)
        return output.getvalue()


def reference(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class RecipeAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = RecipeAssets(self.root / "recipe-assets")

    def tearDown(self):
        self.temp.cleanup()

    def test_supported_inputs_are_decoded_and_deterministic(self):
        for format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(format=format):
                data = picture(format)
                (self.root / "attachment").write_bytes(data)  # extension is irrelevant
                asset_id = self.assets.import_file(self.root, "attachment")
                rendition = self.assets.read(asset_id)
                self.assertEqual(asset_id, reference(rendition))
                self.assertEqual(self.assets.import_bytes(data), asset_id)
                with Image.open(io.BytesIO(rendition)) as decoded:
                    decoded.load()
                    self.assertEqual((decoded.format, decoded.mode, decoded.size), ("JPEG", "RGB", (80, 40)))
                self.assertEqual((self.assets.root / assets.asset_filename(asset_id)).stat().st_mode & 0o777, 0o600)

    def test_optional_decoder_is_not_required_to_import_text_runtime(self):
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c",
            "import sys; sys.path.insert(0,sys.argv[1]); import recipe_assets, service; "
            "assert 'PIL' not in sys.modules\n"
            "try:\n recipe_assets.sanitize_image(b'photo')\n"
            "except recipe_assets.RecipeAssetError as error:\n assert 'Pillow' in str(error)\n"
            "else:\n raise AssertionError('missing decoder must fail image import')",
            str(CORE)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_grayscale_cmyk_and_progressive_raw_jpeg_decode(self):
        for mode in ("L", "CMYK", "RGB"):
            with self.subTest(mode=mode), Image.new(mode, (80, 40)) as source:
                output = io.BytesIO()
                source.save(output, "JPEG", progressive=True)
                asset_id = self.assets.import_bytes(output.getvalue())
                with Image.open(io.BytesIO(self.assets.read(asset_id))) as decoded:
                    self.assertEqual(decoded.mode, "RGB")

    def test_orientation_gps_device_and_png_private_metadata_are_removed(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[271] = "Private device"
        exif[34853] = {1: "N", 2: (59.0, 0.0, 0.0), 3: "E", 4: (10.0, 0.0, 0.0)}
        data = picture("JPEG", exif=exif)
        source = Image.open(io.BytesIO(data))
        self.assertTrue(source.getexif().get_ifd(34853))
        source.close()
        rendition = self.assets.read(self.assets.import_bytes(data))
        with Image.open(io.BytesIO(rendition)) as decoded:
            self.assertEqual(decoded.size, (40, 80))
            self.assertFalse(decoded.getexif())
            self.assertNotIn(b"Private device", rendition)
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Private location", "Do not retain")
        data = picture("PNG", pnginfo=metadata, icc_profile=b"private ICC bytes", exif=exif)
        rendition = self.assets.read(self.assets.import_bytes(data))
        self.assertNotIn(b"Do not retain", rendition)
        self.assertNotIn(b"private ICC", rendition)
        with Image.open(io.BytesIO(rendition)) as decoded:
            self.assertNotIn("icc_profile", decoded.info)
            self.assertFalse(decoded.getexif())

    def test_alpha_composites_on_white_and_dimensions_are_reduced(self):
        with Image.new("RGBA", (2000, 1000), (255, 0, 0, 0)) as source:
            output = io.BytesIO()
            source.save(output, "PNG")
        rendition = self.assets.read(self.assets.import_bytes(output.getvalue()))
        with Image.open(io.BytesIO(rendition)) as decoded:
            self.assertEqual(decoded.size, (1600, 800))
            self.assertEqual(decoded.getpixel((0, 0)), (255, 255, 255))

    def test_invalid_input_cannot_damage_existing_asset(self):
        original = self.assets.import_bytes(picture())
        before = self.assets.read(original)
        for data in (b"", b"not an image", picture("GIF"), picture("PNG")[:-15],
                     picture("JPEG")[:-20], b"x" * (assets.MAX_INPUT_BYTES + 1)):
            with self.subTest(prefix=data[:12]), self.assertRaises(RecipeAssetError):
                self.assets.import_bytes(data)
        self.assertEqual(self.assets.read(original), before)
        self.assertEqual(len(list(self.assets.root.iterdir())), 1)

    def test_bounds_checked_before_full_decode(self):
        data = picture(size=(300, 300))
        with mock.patch.object(assets, "MAX_INPUT_PIXELS", 89_999), mock.patch.object(Image.Image, "load", side_effect=AssertionError("must not load pixels")):
            with self.assertRaisesRegex(RecipeAssetError, "dimensions"):
                self.assets.import_bytes(data)
        with mock.patch.object(assets, "MAX_INPUT_EDGE", 299):
            with self.assertRaisesRegex(RecipeAssetError, "dimensions"):
                self.assets.import_bytes(data)

    def test_animated_png_and_webp_are_rejected(self):
        for format in ("PNG", "WEBP"):
            with self.subTest(format=format), Image.new("RGB", (4, 4), "red") as first, Image.new("RGB", (4, 4), "blue") as second:
                output = io.BytesIO()
                first.save(output, format, save_all=True, append_images=[second], duration=100, loop=0)
                with self.assertRaisesRegex(RecipeAssetError, "animated"):
                    self.assets.import_bytes(output.getvalue())

    def test_managed_dimension_bomb_becomes_asset_error(self):
        value = assets.sanitize_image(picture())
        position = value.index(b"\xff\xc0")
        value = value[:position + 5] + b"\xff\xff\xff\xff" + value[position + 9:]
        with self.assertRaises(RecipeAssetError):
            self.assets.install_managed(reference(value), value)

    def test_incomplete_jpeg_scans_are_rejected(self):
        value = picture("JPEG", size=(128, 128))
        position = value.index(b"\xff\xda")
        scan = position + 2 + int.from_bytes(value[position + 2:position + 4], "big")
        for broken in (value[:scan] + value[-2:], value[:scan + 20] + value[-2:]):
            with self.assertRaises(RecipeAssetError):
                self.assets.import_bytes(broken)
            with self.assertRaises(RecipeAssetError):
                self.assets.install_managed(reference(broken), broken)

    def test_confined_attachment_paths_symlinks_and_nonregular_files(self):
        source = self.root / "source"
        source.mkdir()
        outside = self.root / "outside.png"
        outside.write_bytes(picture())
        (source / "link.png").symlink_to(outside)
        (source / "escape").symlink_to(self.root, target_is_directory=True)
        (self.root / "root-link").symlink_to(source, target_is_directory=True)
        os.mkfifo(source / "pipe")
        for name in ("../outside.png", str(outside), "./image.png", "a//b", "a\\b", "link.png", "escape/outside.png", "pipe", ""):
            with self.subTest(name=name), self.assertRaises(RecipeAssetError):
                self.assets.import_file(source, name)
        with self.assertRaises(RecipeAssetError):
            self.assets.import_file(self.root / "root-link", "whatever")
        nested = source / "nested"
        nested.mkdir()
        (nested / "cover.png").write_bytes(picture())
        self.assertTrue(self.assets.import_file(source, "nested/cover.png"))
        self.assertEqual(outside.read_bytes(), picture())

    def test_managed_reference_and_storage_symlinks_are_rejected(self):
        asset_id = self.assets.import_bytes(picture())
        data = self.assets.read(asset_id)
        for invalid in ("../outside", "file:///private/foo", "https://example.com/image.jpg", asset_id.upper(), asset_id + "/more", None):
            with self.subTest(invalid=invalid), self.assertRaises(RecipeAssetError):
                self.assets.read(invalid)
        leaf = self.assets.root / assets.asset_filename(asset_id)
        leaf.unlink()
        outside = self.root / "outside.jpg"
        outside.write_bytes(data)
        leaf.symlink_to(outside)
        for operation in (lambda: self.assets.read(asset_id), lambda: self.assets.install_managed(asset_id, data)):
            with self.assertRaises(RecipeAssetError):
                operation()
        root_link = self.root / "asset-link"
        root_link.symlink_to(self.assets.root, target_is_directory=True)
        with self.assertRaises(RecipeAssetError):
            RecipeAssets(root_link).read(asset_id)
        with self.assertRaises(RecipeAssetError):
            RecipeAssets(root_link).install_managed(asset_id, data)
        self.assertEqual(outside.read_bytes(), data)

    def test_digest_corruption_is_explicit_and_never_overwritten(self):
        asset_id = self.assets.import_bytes(picture())
        data = self.assets.read(asset_id)
        leaf = self.assets.root / assets.asset_filename(asset_id)
        leaf.write_bytes(b"damaged")
        with self.assertRaisesRegex(RecipeAssetError, "digest"):
            self.assets.read(asset_id)
        with self.assertRaisesRegex(RecipeAssetError, "digest"):
            self.assets.install_managed(asset_id, data)
        self.assertEqual(leaf.read_bytes(), b"damaged")
        self.assertFalse(list(self.assets.root.glob(".import-*")))

    def test_managed_restore_preserves_exact_current_and_historical_bytes(self):
        first = self.assets.import_bytes(picture(color="red"))
        second = self.assets.import_bytes(picture(color="blue"))
        saved = {key: self.assets.read(key) for key in (first, second)}
        destination = RecipeAssets(self.root / "restored-assets")
        for key, value in saved.items():
            destination.install_managed(key, value)
        shutil.rmtree(self.assets.root)
        for key, value in saved.items():
            self.assertEqual(destination.read(key), value)
        self.assertNotEqual(first, second)

    def test_concurrent_import_never_clobbers_or_leaves_partial_files(self):
        data = picture()
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(self.assets.import_bytes, [data] * 12))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(list(self.assets.root.iterdir())), 1)
        self.assertEqual(reference(self.assets.read(results[0])), results[0])

    def test_managed_jpeg_rejects_metadata_in_headers_after_scan_and_trailing(self):
        clean = assets.sanitize_image(picture())
        for marker in (b"\xff\xef", b"\xff\xfe", b"\xff\xe1"):
            segment = marker + b"\x00\x09private"
            for value in (clean[:2] + segment + clean[2:], clean[:-2] + segment + clean[-2:]):
                with self.subTest(marker=marker), self.assertRaises(RecipeAssetError):
                    self.assets.install_managed(reference(value), value)
        bad_jfif = clean.replace(assets._JFIF, assets._JFIF[:-2] + b"\x01\x01", 1)
        for value in (clean + b"private", clean[:-2] + b"PRIVATE GPS CAMERA METADATA" + clean[-2:],
                      clean + clean, clean[:2] + b"\xff\xd8" + clean[2:],
                      clean[:4] + b"\xff\xff" + clean[6:], bad_jfif, clean[:-1]):
            with self.subTest(length=len(value)), self.assertRaises(RecipeAssetError):
                self.assets.install_managed(reference(value), value)
        rng = random.Random(39)
        with Image.frombytes("RGB", (128, 128), rng.randbytes(128 * 128 * 3)) as image:
            output = io.BytesIO()
            image.save(output, "PNG")
        noisy = assets.sanitize_image(output.getvalue())
        self.assertIn(b"\xff\x00", noisy)
        self.assets.install_managed(reference(noisy), noisy)


class RecipeAssetBankTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bank = RecipeStore(self.root / "recipes.sqlite3", "Synthetic")
        self.cover = self.bank.assets.import_bytes(picture())
        self.recipe = {"schema_version": 2, "name": "Synthetic soup", "portions": 2,
            "source": {"kind": "user", "relationship": "user_supplied", "url": "https://example.invalid/soup"},
            "rights": {"storage": "full", "credit": "Text creator"},
            "ingredients": [{"item": "carrots", "quantity": 200, "unit": "g"}], "steps": ["Cook carrots."],
            "image": {"asset_id": self.cover, "credit": "Independent image creator"}}

    def tearDown(self):
        self.temp.cleanup()

    def pack(self, recipe=None, **kwargs):
        return self.bank.import_pack_record(recipe or self.recipe, pack_id="synthetic-pack", recipe_id="soup", version=kwargs.pop("version", "1"), **kwargs)

    def test_user_metadata_cannot_be_forged_and_exact_duplicate_keeps_origin(self):
        forged = {**self.recipe, "entry_origin": "bundled", "pack": {"pack_id": "fake"}, "locally_modified": True}
        saved = self.bank.save(forged)
        self.assertEqual(saved["entry_origin"], "user")
        self.assertIsNone(saved["pack"])
        self.assertFalse(saved["locally_modified"])
        conflict = self.pack()
        self.assertEqual((conflict["outcome"], conflict["reason"]), ("conflict", "source_identity_exists"))
        self.assertEqual(conflict["recipe"]["id"], saved["id"])
        self.assertEqual(self.bank.import_records([forged])["skipped"], 1)
        self.assertEqual(len(self.bank.search(entry_origin="user")), 1)
        self.assertEqual(self.bank.search(entry_origin="bundled"), [])

    def test_pack_reimport_preserves_favorite_archive_history_and_detects_local_edits(self):
        first = self.pack()["recipe"]
        self.assertEqual(first["entry_origin"], "bundled")
        self.assertEqual(first["pack"]["baseline_hash"], recipe_digest(normalize_recipe(self.recipe)))
        self.bank.set_favorite(first["library_recipe_ref"], True, idempotency_key="favorite")
        archived = self.bank.archive(first["id"], 1)
        repeat = self.pack(version="2")
        self.assertEqual(repeat["outcome"], "unchanged")
        self.assertEqual((repeat["recipe"]["revision"], repeat["recipe"]["status"]), (2, "archived"))
        self.assertTrue(repeat["recipe"]["is_favorite"])
        self.assertEqual(repeat["recipe"]["pack"]["version"], "2")
        self.assertFalse(repeat["recipe"]["locally_modified"])
        changed = deepcopy(first)
        changed["name"] = "Local edit"
        changed["source"]["url"] = "https://example.invalid/locally-changed-source"
        self.bank.update(first["id"], archived["revision"], changed)
        conflict = self.pack()
        self.assertEqual((conflict["outcome"], conflict["reason"]), ("conflict", "locally_modified"))
        self.assertEqual(conflict["recipe"]["id"], first["id"])
        self.assertEqual(len(self.bank.search(include_archived=True)), 1)
        historical = self.bank.get(first["id"], 1)
        self.assertEqual(historical["name"], "Synthetic soup")
        self.assertTrue(historical["locally_modified"])
        self.assertTrue(historical["is_favorite"])

    def test_changed_untouched_pack_upgrades_and_interrupted_per_record_import_resumes(self):
        first = self.pack(status="draft")["recipe"]
        changed = deepcopy(self.recipe)
        changed["name"] = "New upstream edition"
        updated = self.pack(changed, version="2")
        self.assertEqual(updated["outcome"], "updated")
        self.assertEqual(updated["recipe"]["status"], "active")
        self.assertEqual(updated["recipe"]["id"], first["id"])
        self.assertEqual(self.bank.get(first["id"], 1)["name"], first["name"])
        bad = deepcopy(changed)
        bad["source"]["url"] = "https://example.invalid/other"
        bad["image"]["asset_id"] = "sha256:" + "a" * 64
        with self.assertRaisesRegex(RecipeError, "available managed asset"):
            self.bank.import_pack_record(bad, pack_id="synthetic-pack", recipe_id="other", version="1")
        self.assertEqual(self.pack(changed, version="2")["outcome"], "unchanged")
        bad["image"] = None
        second = self.bank.import_pack_record(bad, pack_id="synthetic-pack", recipe_id="other", version="1")
        self.assertEqual(second["outcome"], "created")
        self.assertEqual(second["recipe"]["status"], "active")
        self.assertEqual(self.bank.get(first["id"])["revision"], 2)
        bad["source_provider"] = "oda"
        with self.assertRaisesRegex(RecipeError, "store-bound"):
            self.bank.import_pack_record(bad, pack_id="synthetic-pack", recipe_id="bound", version="1")

    def test_retries_metadata_edits_and_history_do_not_require_missing_old_assets(self):
        first = self.bank.save(self.recipe, idempotency_key="save")
        changed = deepcopy(self.recipe)
        changed["image"]["asset_id"] = self.bank.assets.import_bytes(picture(color="blue"))
        second = self.bank.update(first["id"], 1, changed, idempotency_key="replace")
        self.assertEqual(self.bank.assets.read(self.cover), self.bank.assets.read(first["image"]["asset_id"]))
        shutil.rmtree(self.bank.assets.root)
        self.assertEqual(self.bank.save(self.recipe, idempotency_key="save")["id"], first["id"])
        self.assertEqual(self.bank.update(first["id"], 1, changed, idempotency_key="replace")["revision"], 2)
        self.assertEqual(self.bank.get(first["id"], 1)["image"]["asset_id"], self.cover)
        changed["image"]["credit"] = "Corrected independent credit"
        third = self.bank.update(first["id"], 2, changed)
        self.assertEqual(third["revision"], 3)
        changed["image"]["asset_id"] = self.cover
        with self.assertRaisesRegex(RecipeError, "available managed asset"):
            self.bank.update(first["id"], 3, changed)
        changed["image"] = None
        self.assertIsNone(self.bank.update(first["id"], 3, changed)["image"])
        self.assertEqual(second["rights"]["credit"], "Text creator")

    def test_legacy_migration_and_old_idempotency_overlay_do_not_rewrite_documents(self):
        saved = self.bank.save(self.recipe, idempotency_key="legacy-save")
        with sqlite3.connect(self.bank.path) as connection:
            original = connection.execute("SELECT document FROM recipes").fetchone()[0]
            replay = json.loads(connection.execute("SELECT response_json FROM idempotency").fetchone()[0])
            for key in ("entry_origin", "pack", "locally_modified"):
                replay.pop(key, None)
            connection.execute("UPDATE idempotency SET response_json=?", (json.dumps(replay),))
            connection.execute("DROP TABLE recipe_entry_metadata")
            connection.execute("UPDATE metadata SET value='5' WHERE key='schema_version'")
        reopened = RecipeStore(self.bank.path, "Synthetic")
        self.assertEqual(reopened.get(saved["id"])["entry_origin"], "unknown")
        self.assertEqual(reopened.get(saved["id"], 1)["recipe_digest"], saved["recipe_digest"])
        self.assertEqual(reopened.save(self.recipe, idempotency_key="legacy-save")["entry_origin"], "unknown")
        self.assertEqual(len(reopened.search(entry_origin="unknown", limit=1)), 1)
        with sqlite3.connect(self.bank.path) as connection:
            self.assertEqual(connection.execute("SELECT document FROM recipes").fetchone()[0], original)
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], "6")

    def test_relocated_native_backup_keeps_origins_history_and_assets_after_deletion(self):
        first = self.pack()["recipe"]
        changed = deepcopy(self.recipe)
        changed["image"]["asset_id"] = self.bank.assets.import_bytes(picture(color="blue"))
        self.bank.update(first["id"], 1, changed)
        destination = self.root / "relocated"
        destination.mkdir()
        self.bank.backup(destination / "recipes.sqlite3")
        shutil.copytree(self.bank.assets.root, destination / "recipe-assets")
        self.bank.delete(first["id"], 2)
        self.assertTrue(self.bank.assets.read(self.cover))
        restored = RecipeStore(destination / "recipes.sqlite3", "Synthetic")
        for revision in (1, 2):
            record = restored.get(first["id"], revision)
            self.assertEqual(record["entry_origin"], "bundled")
            self.assertTrue(restored.assets.read(record["image"]["asset_id"]))

    def test_explicit_local_image_cli_dry_run_then_import_is_repeat_safe(self):
        (self.root / "state.json").write_text(json.dumps({"household": "Synthetic"}))
        (self.root / "attachment.png").write_bytes(picture(color="green"))
        command = [sys.executable, str(CORE / "import_recipes.py"), "--state-directory", str(self.root), "--import-root", str(self.root), "--image", "attachment.png"]
        preview = subprocess.run(command + ["--dry-run"], capture_output=True, text=True, check=True)
        asset_id = json.loads(preview.stdout)["asset_id"]
        with self.assertRaises(RecipeAssetError):
            self.bank.assets.read(asset_id)
        actual = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(actual.stdout)["asset_id"], asset_id)
        self.assertTrue(self.bank.assets.read(asset_id))
        repeat = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(repeat.stdout, actual.stdout)
        self.assertFalse(self.bank.path.exists())


if __name__ == "__main__":
    unittest.main()
