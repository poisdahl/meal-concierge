"""Observable portable builder/reader boundary tests; no live effects."""

import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipe_portable import FORMAT, canonical_bytes, open_archive, write_archive
from recipes import RecipeError, normalize_recipe


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipe = normalize_recipe({"name": "Lentils", "portions": 2,
            "ingredients": ["200 g lentils"], "steps": ["Simmer."],
            "source": {"kind": "user", "relationship": "user_supplied"},
            "rights": {"storage": "full"}})
        self.record = {"recipe_id": "wikibooks:123", "status": "draft", "recipe": self.recipe}
        self.manifest = {"format": FORMAT, "format_version": 1, "kind": "bundled",
            "pack_id": "test", "pack_version": "1", "normalizer_version": "test1",
            "recipe_schema_version": 1, "records_count": 1}

    def package(self, records=None):
        source = self.root / "records.jsonl"
        source.write_bytes(b"".join(canonical_bytes(item) + b"\n" for item in (records or [self.record])))
        path = self.root / "pack.zip"
        write_archive(path, self.manifest, {"records.jsonl": source})
        return path

    def raw_package(self, members, *, manifest=None):
        value = dict(manifest or self.manifest)
        value["files"] = [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                          for name, raw in members.items() if name != "manifest.json"]
        path = self.root / "raw.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", canonical_bytes(value))
            for name, raw in members.items():
                archive.writestr(name, raw)
        return path

    def test_real_writer_reader_roundtrip_and_reopen(self):
        path = self.package()
        for _ in range(2):
            with open_archive(path) as archive:
                self.assertEqual(archive.verify()["records_count"], 1)
                self.assertEqual(list(archive.records()), [self.record])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.package()
        self.assertEqual(path.read_bytes(), before)

    def test_streams_more_than_rpc_frame_without_changing_rpc_limit(self):
        records = [{**self.record, "recipe_id": f"wikibooks:{index}",
                    "recipe": {**self.recipe, "notes": "a" * 2000}} for index in range(1000)]
        self.manifest["records_count"] = len(records)
        path = self.package(records)
        with open_archive(path) as archive:
            self.assertGreater(archive.verify()["expanded_bytes"], 2 * 1024 * 1024)
            self.assertEqual(len(list(archive.records())), len(records))

    def test_identical_inputs_have_identical_archive_bytes(self):
        first = self.package()
        second = self.root / "second.zip"
        os.utime(self.root / "records.jsonl", (1_500_000_000, 1_500_000_000))
        with patch("time.localtime", return_value=(2026, 9, 6, 12, 13, 14, 6, 249, 0)):
            write_archive(second, self.manifest, {"records.jsonl": self.root / "records.jsonl"})
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_changed_digest_and_truncation_rejected(self):
        path = self.package()
        with zipfile.ZipFile(path) as source:
            members = {name: source.read(name) for name in source.namelist()}
        members["records.jsonl"] = members["records.jsonl"].replace(b"Lentils", b"Changed")
        other = self.root / "corrupt.zip"
        with zipfile.ZipFile(other, "w") as output:
            for name, raw in members.items():
                output.writestr(name, raw)
        with self.assertRaises(RecipeError):
            with open_archive(other) as archive:
                archive.verify()
        path.write_bytes(path.read_bytes()[:-15])
        with self.assertRaises(RecipeError):
            with open_archive(path):
                pass

    def test_unsafe_member_names_and_symlink_are_never_extracted(self):
        for name in ("../outside", "/tmp/escape", "assets\\evil.jpg", "records.jsonl/", "extra.txt"):
            with self.subTest(name=name):
                path = self.raw_package({"records.jsonl": canonical_bytes(self.record) + b"\n", name: b"evil"})
                with self.assertRaises(RecipeError):
                    with open_archive(path):
                        pass
        path = self.package()
        info = zipfile.ZipInfo("coverage.json")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "a") as output:
            output.writestr(info, b"/outside")
        with self.assertRaises(RecipeError):
            with open_archive(path):
                pass

    def test_duplicate_member_and_json_keys_rejected(self):
        path = self.package()
        with zipfile.ZipFile(path, "a") as output:
            output.writestr("records.jsonl", b"{}\n")
        with self.assertRaises(RecipeError):
            with open_archive(path):
                pass
        path = self.raw_package({"records.jsonl": b'{"recipe_id":"a","recipe_id":"b"}\n'})
        with self.assertRaises(RecipeError):
            with open_archive(path) as archive:
                archive.verify()

    def test_duplicate_identity_missing_newline_and_count_are_errors(self):
        cases = [canonical_bytes(self.record), canonical_bytes(self.record) + b"\n" + canonical_bytes(self.record) + b"\n", b""]
        for raw in cases:
            with self.subTest(raw=raw[:20]):
                path = self.raw_package({"records.jsonl": raw})
                with self.assertRaises(RecipeError):
                    with open_archive(path) as archive:
                        archive.verify()

    def test_size_caps_apply_to_expansion_and_individual_records(self):
        path = self.package()
        with patch("recipe_portable.MAX_RECORD_BYTES", 10):
            with self.assertRaises(RecipeError):
                with open_archive(path) as archive:
                    archive.verify()
        with patch("recipe_portable.MAX_RECORDS_BYTES", 10):
            with self.assertRaises(RecipeError):
                with open_archive(path):
                    pass
        with patch("recipe_portable.MAX_ARCHIVE_BYTES", 10):
            with self.assertRaises(RecipeError):
                with open_archive(path):
                    pass

    def test_link_input_and_missing_cover_rejected(self):
        path = self.package()
        link = self.root / "link.zip"
        link.symlink_to(path)
        with self.assertRaises(RecipeError):
            with open_archive(link):
                pass
        self.record["recipe"]["image"] = {"asset_id": "sha256:" + "a" * 64}
        path = self.raw_package({"records.jsonl": canonical_bytes(self.record) + b"\n"})
        with self.assertRaises(RecipeError):
            with open_archive(path) as archive:
                archive.verify()

    def test_codec_never_confers_trust_or_accepts_private_as_bundle(self):
        path = self.package()
        with open_archive(path) as archive:
            self.assertFalse(hasattr(archive, "trusted"))
        value = {**self.manifest, "kind": "private"}
        path = self.raw_package({"records.jsonl": canonical_bytes(self.record) + b"\n"}, manifest=value)
        with self.assertRaisesRegex(RecipeError, "not yet supported"):
            with open_archive(path):
                pass

    def test_zip64_overrides_and_forged_counts_rejected_before_zipfile(self):
        path = self.package()
        raw = path.read_bytes()
        end = raw.rfind(b"PK\x05\x06")
        count = struct.unpack_from("<H", raw, end + 10)[0]
        cd_size, cd_offset = struct.unpack_from("<LL", raw, end + 12)
        zip64 = struct.pack("<4sQHHLLQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, cd_size, cd_offset)
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, end, 1)
        forged_end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 1, cd_offset, 0)
        path.write_bytes(raw[:end] + zip64 + locator + forged_end)
        with patch("recipe_portable.zipfile.ZipFile", side_effect=AssertionError("allocated before bounds")):
            with self.assertRaises(RecipeError):
                with open_archive(path):
                    pass
        forged = bytearray(raw)
        struct.pack_into("<HH", forged, end + 8, 1, 1)
        path.write_bytes(forged)
        with patch("recipe_portable.zipfile.ZipFile", side_effect=AssertionError("allocated before bounds")):
            with self.assertRaises(RecipeError):
                with open_archive(path):
                    pass

    def test_local_zip64_rejected(self):
        path = self.package()
        with zipfile.ZipFile(path) as source:
            members = {name: source.read(name) for name in source.namelist()}
        with zipfile.ZipFile(path, "w") as target:
            for name, data in members.items():
                with target.open(name, "w", force_zip64=True) as member:
                    member.write(data)
        with self.assertRaisesRegex(RecipeError, "ZIP64"):
            with open_archive(path):
                pass

    def test_unknown_member_version_is_a_recipe_error_before_allocation(self):
        path = self.package()
        raw = bytearray(path.read_bytes())
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<H", raw, central + 6, 100)
        path.write_bytes(raw)
        with patch("recipe_portable.zipfile.ZipFile", side_effect=AssertionError("allocated before bounds")):
            with self.assertRaises(RecipeError):
                with open_archive(path):
                    pass

    def test_multidisk_member_is_rejected(self):
        path = self.package()
        raw = bytearray(path.read_bytes())
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<H", raw, central + 34, 1)
        path.write_bytes(raw)
        with self.assertRaisesRegex(RecipeError, "multidisk"):
            with open_archive(path):
                pass

    def test_malformed_status_and_corrupt_deflate_are_recipe_errors(self):
        raw_record = {**self.record, "status": []}
        path = self.raw_package({"records.jsonl": canonical_bytes(raw_record) + b"\n"})
        with self.assertRaises(RecipeError):
            with open_archive(path) as archive:
                archive.verify()
        path = self.package()
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("records.jsonl")
        raw = bytearray(path.read_bytes())
        filename_size, extra_size = struct.unpack_from("<HH", raw, info.header_offset + 26)
        raw[info.header_offset + 30 + filename_size + extra_size] = 7
        path.write_bytes(raw)
        with self.assertRaisesRegex(RecipeError, "corrupt"):
            with open_archive(path) as archive:
                archive.verify()


from copy import deepcopy
import json
import unittest

from recipe_import_readers import (
    MAX_RECORD_BYTES as SOURCE_RECORD_BYTES, RecipeImportReaderError, read_recipesage_export,
    read_webpage_jsonld, source_candidate,
)


def recipesage_fixture():
    return {
        "@context": "http://schema.org", "@type": "Recipe",
        "identifier": "synthetic-recipe-1", "datePublished": "2026-09-06T00:00:00.000Z",
        "description": "A synthetic loaf fixture.",
        "image": ["https://images.example.test/loaf.jpg"],
        "name": "Two loaves", "prepTime": "PT20M",
        "recipeIngredient": ["350 g flour", "1 cup water", "salt to taste"],
        "recipeInstructions": [
            {"@type": "HowToSection", "name": "Dough"},
            {"@type": "HowToStep", "text": "Mix ingredients."},
            {"@type": "HowToSection", "name": "Bake"},
            {"@type": "HowToStep", "text": "Bake until done."},
        ],
        "recipeYield": "2 loaves", "totalTime": "PT1H",
        "recipeCategory": ["Bread", "Family"],
        "creditText": "Synthetic cookbook",
        "isBasedOn": "http://example.test/loaf?id=4",
        "comment": [{"@type": "Comment", "name": "Author Notes", "text": "Keep this original note."}],
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4", "bestRating": "5"},
        "nutrition": {"@type": "NutritionInformation", "calories": "100 kcal"},
    }


def read_one(raw=None):
    return read_recipesage_export(json.dumps({"recipes": [raw or recipesage_fixture()]}))[0]


def page(raw):
    return '<html><script type="application/ld+json">' + json.dumps(raw) + '</script></html>'


class ReaderTests(unittest.TestCase):
    def test_actual_export_shape_preserves_text_and_reports_losses(self):
        record = read_one()
        raw = record["extracted"]
        self.assertEqual(raw["ingredients"], ["350 g flour", "1 cup water", "salt to taste"])
        self.assertEqual(raw["yield_text"], "2 loaves")
        self.assertEqual(raw["steps"], ["[Dough]", "Mix ingredients.", "[Bake]", "Bake until done."])
        self.assertEqual(raw["notes"], "Keep this original note.")
        self.assertEqual(raw["description"], "A synthetic loaf fixture.")
        self.assertEqual(raw["tags"], ["Bread", "Family"])
        self.assertEqual(raw["source"]["original"], {"url": "http://example.test/loaf?id=4", "publisher": "Synthetic cookbook"})
        self.assertEqual(record["image_candidates"], ["https://images.example.test/loaf.jpg"])
        self.assertEqual(record["image_status"], "requires_asset_import")
        self.assertEqual(record["unsupported_fields"], ["aggregateRating", "datePublished", "nutrition"])

    def test_source_cannot_supply_privileged_fields(self):
        raw = recipesage_fixture()
        raw.update({"source_provider": "oda", "entry_origin": "bundled", "acceptance": {"accepted": True}, "source": {"kind": "trusted"}})
        record = read_one(raw)
        self.assertEqual(record["extracted"]["source"]["kind"], "recipesage")
        self.assertNotIn("source_provider", record["extracted"])
        for field in ("source_provider", "entry_origin", "acceptance", "source"):
            self.assertIn(field, record["unsupported_fields"])

    def test_json_duplicate_keys_and_nonfinite_values_rejected(self):
        for document in ('{"recipes":[],"recipes":[]}', '{"recipes":[{"@type":"Recipe","name":"a","name":"b"}]}', '{"recipes":[],"extra":NaN}', '{"recipes":[],"extra":1e999}', '{"recipes":[],"extra":Infinity}'):
            with self.subTest(document=document), self.assertRaises(RecipeImportReaderError):
                read_recipesage_export(document)

    def test_credential_urls_are_not_echoed(self):
        for field in ("isBasedOn", "image"):
            for url in ("https://user:secret@example.test/x", "https://example.test/x?access_token=secret", "https://example.test/x#token=secret", "https://example.test/x?%74oken=secret", "https://example.test/x?X-Amz-Credential=secret"):
                raw = recipesage_fixture()
                raw[field] = [url] if field == "image" else url
                with self.subTest(field=field, url=url), self.assertRaises(RecipeImportReaderError) as caught:
                    read_one(raw)
                self.assertNotIn("secret", str(caught.exception))

    def test_credential_url_in_notes_rejected(self):
        raw = recipesage_fixture()
        raw["comment"][0]["text"] = "Read https://user:secret@example.test/x"
        with self.assertRaises(RecipeImportReaderError):
            read_one(raw)

    def test_unknown_field_names_cannot_retain_credential_urls(self):
        raw = recipesage_fixture()
        raw["https://user:secret@example.test/"] = "ignored"
        with self.assertRaises(RecipeImportReaderError) as caught:
            read_one(raw)
        self.assertNotIn("secret", str(caught.exception))

    def test_wide_invalid_export_has_bounded_traversal_memory(self):
        import tracemalloc
        value = '{"recipes":[' + ','.join('0' for _ in range(500_000)) + ']}'
        tracemalloc.start()
        try:
            with self.assertRaises(RecipeImportReaderError):
                read_recipesage_export(value)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 16 * 1024 * 1024)

    def test_no_url_fetch_or_origin_claim(self):
        raw = recipesage_fixture()
        raw["@context"] = "https://127.0.0.1:1/context"
        raw["image"] = ["https://127.0.0.1/unfetched.jpg"]
        raw["isBasedOn"] = "https://127.0.0.1/unfetched"
        record = read_one(raw)
        self.assertEqual(record["image_candidates"], raw["image"])
        self.assertNotIn("image", record["extracted"])
        self.assertNotIn("entry_origin", record["extracted"])

    def test_webpage_graph_array_multiple_recipes_and_nested_sections(self):
        raw = recipesage_fixture()
        raw["@type"] = ["Thing", "https://schema.org/Recipe"]
        raw["recipeInstructions"] = [{"@type": "HowToSection", "name": "Dough", "itemListElement": [{"@type": "HowToStep", "text": "Mix."}, "Rest."]}]
        second = deepcopy(raw)
        second["name"] = "Other loaf"
        record = read_webpage_jsonld(page({"@graph": [{"@type": "WebSite"}, raw, [second]]}), source_url="https://example.test/page?id=2")
        self.assertEqual(len(record), 2)
        self.assertEqual(record[0]["extracted"]["steps"], ["[Dough]", "Mix.", "Rest."])
        self.assertEqual(record[0]["extracted"]["source"]["url"], "https://example.test/page?id=2")
        self.assertEqual(record[1]["extracted"]["name"], "Other loaf")

    def test_script_must_be_jsonld(self):
        html = '<script type="application/json">' + json.dumps(recipesage_fixture()) + '</script>'
        with self.assertRaises(RecipeImportReaderError):
            read_webpage_jsonld(html, source_url="https://example.test/page")

    def test_source_url_is_required_and_credential_free(self):
        for url in ("", "http://example.test/page", "https://user:secret@example.test/page", "file:///tmp/source"):
            with self.subTest(url=url), self.assertRaises(RecipeImportReaderError):
                read_webpage_jsonld(page(recipesage_fixture()), source_url=url)

    def test_jsonld_is_not_entity_decoded_or_executed(self):
        raw = recipesage_fixture()
        raw["recipeInstructions"] = ['Keep &quot;literal&quot; and <b>source text</b>.']
        record = read_webpage_jsonld(page(raw), source_url="https://example.test/page")[0]
        self.assertEqual(record["extracted"]["steps"], raw["recipeInstructions"])

    def test_unclosed_malformed_and_overdeep_jsonld_rejected(self):
        for html in ('<script type="application/ld+json">{}', '<script type="application/ld+json">{broken}</script>', '<script type="application/ld+json">' + '[' * 34 + '{}' + ']' * 34 + '</script>'):
            with self.subTest(html=html), self.assertRaises(RecipeImportReaderError):
                read_webpage_jsonld(html, source_url="https://example.test/page")

    def test_structured_ingredients_and_yield_explicitly_unsupported(self):
        for field, value in (("recipeIngredient", [{"@type": "PropertyValue", "value": "1 cup"}]), ("recipeYield", {"@type": "QuantitativeValue", "value": 2})):
            raw = recipesage_fixture()
            raw[field] = value
            with self.subTest(field=field), self.assertRaises(RecipeImportReaderError):
                read_one(raw)

    def test_record_and_text_limits(self):
        raw = recipesage_fixture()
        raw["extra"] = "x" * SOURCE_RECORD_BYTES
        with self.assertRaisesRegex(RecipeImportReaderError, "too large"):
            read_one(raw)
        raw = recipesage_fixture()
        raw["recipeIngredient"] = ["x" * 501]
        with self.assertRaises(RecipeImportReaderError):
            read_one(raw)

    def test_unreadable_encoding_rejected(self):
        for document in (b"\xff", '{"recipes":[{"@type":"Recipe","name":"\\ud800"}]}'):
            with self.subTest(document=document), self.assertRaises(RecipeImportReaderError):
                read_recipesage_export(document)

    def test_empty_export_and_invalid_wrapper(self):
        self.assertEqual(read_recipesage_export('{"recipes": []}'), [])
        for document in ("[]", "null", '{"recipes":null}', '{"recipes":[],"unknown":1}'):
            with self.subTest(document=document), self.assertRaises(RecipeImportReaderError):
                read_recipesage_export(document)

    def test_shared_schema2_source_conversion_without_fallback(self):
        import recipes
        if not hasattr(recipes, "source_ingredient"):
            with self.assertRaisesRegex(RecipeImportReaderError, "schema-2"):
                source_candidate(read_one())
            return
        recipe = recipes.normalize_recipe(source_candidate(read_one()))
        self.assertEqual(recipe['schema_version'], 2)
        self.assertEqual(recipe['ingredients'][0]['quantity'], {'numerator': 350, 'denominator': 1})
        self.assertIsNone(recipe['ingredients'][1]['quantity'])
        self.assertEqual(recipe['ingredients'][1]['original_text'], '1 cup water')
        self.assertIsNone(recipe['portions'])
        self.assertEqual(recipe['yield']['original_text'], '2 loaves')
        native = recipesage_fixture()
        native['recipeYield'] = '4 servings'
        recipe = recipes.normalize_recipe(source_candidate(read_one(native)))
        self.assertEqual(recipe['portions'], 4)
        self.assertEqual(recipe['portions_evidence']['basis'], 'source')


from copy import deepcopy
import json
from pathlib import Path
import stat
import unittest
import zipfile

from recipe_import_readers import RecipeImportReaderError, open_mealie_export_zip, read_mealie_json, source_candidate
from recipes import normalize_recipe



def fixture():
    # Native exporter recipe.model_dump_json() writes model field names.
    # mealie/schema/recipe/{recipe.py,recipe_ingredient.py,recipe_step.py}, v3.24.0.
    return {
        'id': 'synthetic-recipe-id', 'slug': 'lentil-soup', 'name': 'Lentil soup',
        'user_id': 'PRIVATE_USER_ID', 'household_id': 'PRIVATE_HOUSEHOLD_ID',
        'group_id': 'PRIVATE_GROUP_ID', 'image': 'native-cache-key',
        'recipe_servings': 4, 'recipe_yield_quantity': 2, 'recipe_yield': 'litres',
        'description': 'Synthetic soup.', 'org_url': 'http://example.test/soup?id=2',
        'recipe_ingredient': [{
            'quantity': 1.5, 'unit': {'name': 'deciliter', 'abbreviation': 'dl', 'use_abbreviation': True, 'extras': {'private': 'PRIVATE_UNIT_EXTRA'}},
            'food': {'name': 'water', 'plural_name': 'water', 'households_with_ingredient_food': ['PRIVATE_HOUSEHOLD']},
            'original_text': '1½ dl water', 'display': '1.5 dl water', 'note': 'Warm.',
            'reference_id': 'PRIVATE_REFERENCE_ID',
        }, {'quantity': 0, 'unit': None, 'food': None, 'note': 'Salt to taste.'}],
        'recipe_instructions': [{'title': 'Cook', 'summary': 'Use a pot.', 'text': 'Simmer gently.', 'id': 'PRIVATE_STEP_ID', 'ingredient_references': []}],
        'notes': [{'title': 'Storage', 'text': 'Refrigerate.'}],
        'tags': [{'name': 'Soup', 'id': 'PRIVATE_TAG_ID'}], 'recipe_category': [{'name': 'Dinner'}],
        'prep_time': 'PT10M', 'perform_time': 'PT20M',
        'extras': {'hermes_recipe': {'entry_origin': 'bundled', 'acceptance': 'PRIVATE_SIDECAR'}},
    }


def read(raw=None):
    return read_mealie_json(json.dumps(raw or fixture()))


def write_zip(root, name, members, compression=zipfile.ZIP_STORED):
    path = root / name
    with zipfile.ZipFile(path, 'w', compression=compression) as archive:
        for member, content in members:
            archive.writestr(member, content)
    return path


class MealieReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_native_json_fields_preserved_and_private_data_not_retained(self):
        result = read()
        raw = result['extracted']
        self.assertEqual(raw['name'], 'Lentil soup')
        self.assertEqual(raw['servings'], 4)
        self.assertEqual(raw['yield_text'], '2 litres')
        self.assertEqual(raw['yield_label'], 'litres')
        self.assertEqual(raw['ingredients'], ['1½ dl water', 'Salt to taste.'])
        self.assertEqual(raw['ingredient_details'][0]['quantity'], 1.5)
        self.assertEqual(raw['ingredient_details'][0]['unit']['abbreviation'], 'dl')
        self.assertEqual(raw['ingredient_details'][0]['food']['name'], 'water')
        self.assertEqual(raw['ingredient_details'][0]['note'], 'Warm.')
        self.assertEqual(raw['steps'], ['[Cook]\nUse a pot.\nSimmer gently.'])
        self.assertEqual(raw['notes'], '[Storage]\nRefrigerate.')
        self.assertEqual(raw['tags'], ['Soup', 'Dinner'])
        self.assertEqual(raw['source']['original']['url'], 'http://example.test/soup?id=2')
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.assertIn('extras', result['unsupported_fields'])
        self.assertEqual(result['image_status'], 'requires_export_cover')

    def test_documented_api_aliases_also_accepted(self):
        raw = fixture()
        for old, new in [('recipe_ingredient', 'recipeIngredient'), ('recipe_instructions', 'recipeInstructions'), ('recipe_servings', 'recipeServings'), ('recipe_yield', 'recipeYield'), ('recipe_yield_quantity', 'recipeYieldQuantity'), ('org_url', 'orgURL')]:
            raw[new] = raw.pop(old)
        raw['recipeIngredient'][0]['originalText'] = raw['recipeIngredient'][0].pop('original_text')
        self.assertEqual(read(raw)['extracted'], read()['extracted'])

    def test_inert_selfhosted_source_url_retains_valid_port(self):
        raw = fixture()
        raw['org_url'] = 'http://recipes.example.test:9000/soup?id=2'
        self.assertEqual(read(raw)['extracted']['source']['original']['url'], raw['org_url'])

    def test_ambiguous_aliases_rejected(self):
        raw = fixture()
        raw['recipeServings'] = 6
        with self.assertRaises(RecipeImportReaderError):
            read(raw)

    def test_bounded_numbers_and_credentials_rejected(self):
        for value in (-1, True, '4', 10**13, 10**1000):
            raw = fixture()
            raw['recipe_servings'] = value
            with self.subTest(value=value), self.assertRaises(RecipeImportReaderError):
                read(raw)
        raw = fixture()
        raw['org_url'] = 'https://user:secret@example.test/soup'
        with self.assertRaises(RecipeImportReaderError) as caught:
            read(raw)
        self.assertNotIn('secret', str(caught.exception))

    def test_shared_schema2_structured_amount_servings_and_yield(self):
        recipe = normalize_recipe(source_candidate(read()))
        self.assertEqual(recipe['ingredients'][0]['quantity'], {'numerator': 3, 'denominator': 2})
        self.assertEqual(recipe['ingredients'][0]['unit'], 'dl')
        self.assertEqual(recipe['ingredients'][0]['item'], 'water')
        self.assertEqual(recipe['ingredients'][0]['original_text'], '1½ dl water')
        self.assertEqual(recipe['ingredients'][0]['notes'], 'Warm.')
        self.assertEqual(recipe['portions'], 4)
        self.assertEqual(recipe['yield']['quantity'], {'numerator': 2, 'denominator': 1})
        self.assertEqual(recipe['yield']['unit'], 'litres')
        self.assertEqual(recipe['portions_evidence']['input'], 'recipeServings: 4')

    def test_shared_schema2_does_not_revive_stale_original_amount(self):
        raw = fixture()
        raw['recipe_ingredient'][0]['quantity'] = 0
        recipe = normalize_recipe(source_candidate(read(raw)))
        self.assertIsNone(recipe['ingredients'][0]['quantity'])
        self.assertFalse(recipe['ingredients'][0]['scalable'])
        self.assertEqual(recipe['ingredients'][0]['original_text'], '1½ dl water')

    def test_shared_schema2_never_infers_servings_from_mealie_yield(self):
        raw = fixture()
        raw['recipe_servings'] = 0
        raw['recipe_yield'] = 'servings'
        recipe = normalize_recipe(source_candidate(read(raw)))
        self.assertIsNone(recipe['portions'])
        self.assertEqual(recipe['yield']['original_text'], '2 servings')

    def test_actual_shared_zip_layout_and_separate_cover_read(self):
        path = write_zip(self.root, 'single-native.zip', [('lentil-soup.json', json.dumps(fixture())), ('original.webp', b'SYNTHETIC_UNDECODED_WEBP_CANDIDATE')])
        with open_mealie_export_zip(path) as archive:
            records = list(archive.records())
            self.assertEqual(len(records), 1)
            self.assertNotIn('SYNTHETIC_UNDECODED', json.dumps(records))
            self.assertEqual(records[0]['image_member_candidates'][0]['member'], 'original.webp')
            self.assertEqual(archive.read_cover('original.webp'), b'SYNTHETIC_UNDECODED_WEBP_CANDIDATE')
            with self.assertRaises(RecipeImportReaderError):
                archive.read_cover('lentil-soup.json')

    def test_actual_multi_recipe_layout_with_unread_attachments(self):
        other = fixture()
        other['slug'] = 'other-soup'
        path = write_zip(self.root, 'multi-native.zip', [
            ('recipes/lentil-soup/lentil-soup.json', json.dumps(fixture())),
            ('recipes/lentil-soup/images/original.webp', b'COVER'),
            ('recipes/lentil-soup/images/min-original.webp', b'THUMBNAIL_NOT_READ'),
            ('recipes/lentil-soup/assets/article.pdf', b'ATTACHMENT_NOT_READ'),
            ('recipes/lentil-soup/assets/data.json', b'NOT_PARSED_AS_RECIPE_JSON'),
            ('recipes/other-soup/other-soup.json', json.dumps(other)),
        ], zipfile.ZIP_DEFLATED)
        with open_mealie_export_zip(path) as archive:
            records = list(archive.records())
            self.assertEqual(len(records), 2)
            self.assertEqual(archive.unsupported_member_count, 3)
            self.assertEqual(archive.read_cover('recipes/lentil-soup/images/original.webp'), b'COVER')
            self.assertEqual(records[1]['image_member_candidates'], [])
            with self.assertRaises(RecipeImportReaderError):
                archive.read_cover('recipes/lentil-soup/assets/article.pdf')

    def test_zip_slug_mismatch_rejected(self):
        path = write_zip(self.root, 'mismatch.zip', [('different.json', json.dumps(fixture()))])
        with open_mealie_export_zip(path) as archive, self.assertRaises(RecipeImportReaderError):
            list(archive.records())

    def test_unsafe_members_and_wrong_layouts_rejected(self):
        for index, name in enumerate(('../outside', '/absolute', 'recipes/lentil-soup/../../outside', 'C:/outside', 'folder\\outside', 'unrelated.txt', 'recipes/wrong/name.json')):
            path = write_zip(self.root, f'unsafe-{index}.zip', [('recipes/lentil-soup/lentil-soup.json', json.dumps(fixture())), (name, b'bad')])
            with self.subTest(name=name), self.assertRaises(RecipeImportReaderError):
                with open_mealie_export_zip(path):
                    pass

    def test_symlink_and_duplicate_members_rejected(self):
        info = zipfile.ZipInfo('original.webp')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        path = write_zip(self.root, 'symlink-member.zip', [('lentil-soup.json', json.dumps(fixture())), (info, b'/outside')])
        with self.assertRaises(RecipeImportReaderError):
            with open_mealie_export_zip(path):
                pass
        path = write_zip(self.root, 'duplicate-member.zip', [('lentil-soup.json', json.dumps(fixture())), ('lentil-soup.json', json.dumps(fixture()))])
        with self.assertRaises(RecipeImportReaderError):
            with open_mealie_export_zip(path):
                pass

    def test_symlink_archive_path_rejected(self):
        target = write_zip(self.root, 'symlink-target.zip', [('lentil-soup.json', json.dumps(fixture()))])
        path = self.root / 'symlink-path.zip'
        if not path.exists() and not path.is_symlink():
            path.symlink_to(target)
        with self.assertRaises(RecipeImportReaderError):
            with open_mealie_export_zip(path):
                pass

    def test_invalid_unicode_url_is_not_retained(self):
        raw = fixture()
        raw['org_url'] = 'https://\ud800.example.test/soup'
        with self.assertRaises(RecipeImportReaderError):
            read(raw)

    def test_native_local_header_uses_shared_validation(self):
        path = self.root / 'local-version.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            with archive.open('lentil-soup.json', 'w', force_zip64=True) as output:
                output.write(json.dumps(fixture()).encode())
        raw = bytearray(path.read_bytes())
        central = raw.index(b'PK\x01\x02')
        struct.pack_into('<H', raw, central + 6, 20)
        path.write_bytes(raw)
        with self.assertRaises(RecipeImportReaderError):
            with open_mealie_export_zip(path) as archive:
                list(archive.records())


if __name__ == "__main__":
    unittest.main()
