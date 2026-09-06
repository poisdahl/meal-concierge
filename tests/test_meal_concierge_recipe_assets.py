"""Synthetic local cover tests; no downloaded photos, services or recipients."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
from pathlib import Path
import random
import shutil
import subprocess
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


if __name__ == "__main__":
    unittest.main()
