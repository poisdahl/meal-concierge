"""Behavioral tests for deterministic source mapping and sealed build inputs."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_recipe_pack import Covers, PackBuildError, build, confined, digest, encoded, read_file, write_file
from recipe_pack_sources import SourceParseError, SourceHTML, mealdb_recipe, readiness, wikibooks_recipe


def entry(source='wikibooks'):
    return {
        'source': source, 'source_id': '123', 'title': 'Cookbook:Example' if source == 'wikibooks' else 'Example',
        'url': 'https://en.wikibooks.org/wiki/Cookbook:Example' if source == 'wikibooks' else 'https://www.themealdb.com/meal/123',
        'revision': 456, 'fetched_at': '2026-09-06T00:00:00+00:00',
        'raw': {'sha256': '1' * 64}, 'rendered': {'sha256': '2' * 64, 'fetched_at': '2026-09-06T00:00:01+00:00'},
        'history_url': 'https://en.wikibooks.org/w/index.php?title=Cookbook:Example&action=history',
    }


def wiki(html):
    return wikibooks_recipe(entry(), {'parse': {'pageid': 123, 'revid': 456, 'text': html}})


class SourceMappingTests(unittest.TestCase):
    def test_reviewed_variant_keeps_exact_count_and_unselected_source_alternative(self):
        e = entry()
        e.update(source_id='414427', revision=4524828)
        e['rendered']['sha256'] = 'e0f9c27d7c6594a9da66c9ca5149870cbf1739e3413a7306e275d8395f928681'
        original = '12 small potatoes or 10 medium sized ones weighing approximately 971g'
        html = ('<table class="infobox"><tr><th>Servings</th><td>6</td></tr></table>'
                f'<h2>Ingredients</h2><ul><li>{original}</li><li>8 lamb sausages</li>'
                '<li>12 cherry tomatoes</li></ul><h2>Procedure</h2><p>Cook and serve.</p>')
        payload = {'parse': {'pageid': 414427, 'revid': 4524828, 'text': html}}
        recipe, _ = wikibooks_recipe(e, payload)
        from recipes import scale_recipe
        potato = recipe['ingredients'][0]
        self.assertEqual(potato['item'], 'small potatoes')
        self.assertEqual(potato['unit'], 'count')
        self.assertEqual(potato['quantity'], {'numerator': 12, 'denominator': 1})
        self.assertEqual(potato['original_text'], original)
        self.assertEqual(potato['evidence']['quantity']['input'], original)
        self.assertEqual(potato['evidence']['quantity']['basis'], 'source')
        self.assertNotIn('acceptance', potato['evidence']['quantity'])
        self.assertIn('approximate 971 g', recipe['external_snapshot']['changes'])
        self.assertTrue(scale_recipe(recipe, 4)['readiness']['scaling_ready'])
        # A new revision cannot inherit a curated source selection.
        e['revision'] += 1
        payload['parse']['revid'] += 1
        other, _ = wikibooks_recipe(e, payload)
        self.assertIsNone(other['ingredients'][0]['quantity'])
        e['revision'] -= 1
        payload['parse']['revid'] -= 1
        e['rendered']['sha256'] = '0' * 64
        with self.assertRaisesRegex(SourceParseError, 'digest mismatch'):
            wikibooks_recipe(e, payload)

    def test_reviewed_oven_branch_is_complete_and_excludes_stovetop_oil(self):
        e = entry()
        e.update(source_id='266778', revision=4512396)
        e['rendered']['sha256'] = 'f2c4d7fb1d070047effa8c3dab71476caa41b124e1f79e9fa522f879daa566f3'
        html = ('<table class="infobox"><tr><th>Servings</th><td>2</td></tr></table>'
                '<h2>Ingredients</h2><ul><li>2 salmon fillets</li><li>320 ml acorns</li><li>2 egg whites</li></ul>'
                '<h2>Procedure</h2><ol><li>Prepare acorn meal.</li><li>Coat with egg white.</li><li>Coat with acorns.</li></ol>'
                '<h3>Cooking</h3><h4>Oven cooking method</h4><ol>'
                '<li>Transfer coated fillets to a slightly greased or non-stick baking sheet.</li>'
                '<li>Bake until fish is flaky.</li><li>Serve.</li></ol>'
                '<h4>Stovetop cooking method</h4><ol><li>Heat a few tablespoons of oil in a pan.</li>'
                '<li>Pan fry the salmon.</li><li>Serve.</li></ol>')
        payload = {'parse': {'pageid': 266778, 'revid': 4512396, 'text': html}}
        recipe, credit = wikibooks_recipe(e, payload)
        self.assertEqual(len(recipe['steps']), 6)
        self.assertEqual(recipe['steps'][3], 'Transfer coated fillets to a non-stick baking sheet.')
        self.assertEqual(recipe['steps'][-2:], ['Bake until fish is flaky.', 'Serve.'])
        self.assertEqual(credit['normalization_issues'], [])
        self.assertEqual(readiness(recipe), ('ready', []))
        self.assertIn('Heat a few tablespoons of oil', credit['source_notices'])
        self.assertIn('Oven Method', recipe['name'])
        payload['parse']['text'] = html.replace('Coat with acorns.</li>', '')
        with self.assertRaisesRegex(SourceParseError, 'branches mismatch'):
            wikibooks_recipe(e, payload)

    def test_rendered_servings_and_exact_metric_fraction(self):
        recipe, credit = wiki('<table class="infobox"><tr><th>Servings</th><td>4</td></tr></table>'
                              '<h2>Ingredients</h2><ul><li>1/3 kg potatoes</li></ul>'
                              '<h2>Procedure</h2><ol><li>Boil until tender.</li></ol>')
        self.assertEqual(recipe['portions'], 4)
        self.assertEqual(recipe['ingredients'][0]['quantity'], {'numerator': 1, 'denominator': 3})
        self.assertEqual(recipe['ingredients'][0]['original_text'], '1/3 kg potatoes')
        self.assertEqual(readiness(recipe), ('ready', []))
        self.assertEqual(credit['rendered_sha256'], '2' * 64)

    def test_yield_is_not_person_servings(self):
        recipe, _ = wiki('<table class="infobox"><tr><th>Yield</th><td>2 loaves</td></tr></table>'
                         '<h2>Ingredients</h2><ul><li>500 g flour</li></ul>'
                         '<h2>Procedure</h2><p>Bake.</p>')
        self.assertIsNone(recipe['portions'])
        self.assertEqual(recipe['yield']['unit'], 'loaves')
        self.assertEqual(readiness(recipe)[0], 'draft')

    def test_serving_range_spoon_and_cup_remain_unready(self):
        recipe, _ = wiki('<table class="infobox"><tr><th>Servings</th><td>2–4</td></tr></table>'
                         '<h2>Ingredients</h2><ul><li>1 cup flour</li><li>1 tsp salt</li></ul>'
                         '<h2>Procedure</h2><p>Mix.</p>')
        self.assertIsNone(recipe['portions'])
        self.assertIsNone(recipe['ingredients'][0]['quantity'])
        self.assertEqual(recipe['ingredients'][1]['evidence']['unit']['basis'], 'estimate')
        self.assertNotIn('acceptance', recipe['ingredients'][1]['evidence']['unit'])
        self.assertEqual(readiness(recipe)[0], 'draft')

    def test_structured_table_uses_actual_weight_not_density_guess(self):
        recipe, _ = wiki('<h2>Ingredients</h2><table><tr><th>Ingredient</th><th>Volume</th><th>Weight</th></tr>'
                         '<tr><td>Flour</td><td>2 cups</td><td>250 g</td></tr></table>'
                         '<h2>Procedure</h2><ol><li>Mix.</li></ol>')
        ingredient = recipe['ingredients'][0]
        self.assertEqual(ingredient['quantity'], {'numerator': 250, 'denominator': 1})
        self.assertIn('volume: 2 cups', ingredient['original_text'])
        self.assertIn('weight: 250 g', ingredient['evidence']['quantity']['input'])

    def test_multiple_recipe_sections_never_silently_merge(self):
        with self.assertRaises(SourceParseError):
            wiki('<h2>Ingredients</h2><p>100 g flour</p><h2>Procedure</h2><p>Bake.</p>'
                 '<h2>Ingredients</h2><p>200 g rice</p><h2>Procedure</h2><p>Boil.</p>')

    def test_missing_section_is_parse_failure_not_fabricated_draft(self):
        with self.assertRaises(SourceParseError):
            wiki('<h2>Ingredients</h2><p>100 g flour</p>')

    def test_script_text_and_unsafe_attribution_urls_are_inert(self):
        recipe, credit = wiki('<script>touch /private/secret</script><h2>Ingredients</h2><p>100 g flour</p>'
                              '<h2>Procedure</h2><p>Mix.</p><h2>Credits</h2>'
                              '<a href="javascript:alert(1)">Bad link</a><a href="https://example.org/source">Source author</a>')
        self.assertNotIn('touch', credit['source_notices'])
        self.assertEqual(credit['source_links'], ['https://example.org/source'])
        self.assertIn('Source author', credit['source_notices'])
        self.assertIsNone(recipe['source_provider'])

    def test_mealdb_missing_provenance_does_not_establish_rights(self):
        meal = {'idMeal': '123', 'strIngredient1': 'flour', 'strMeasure1': '200 g', 'strInstructions': 'Mix.'}
        recipe, credit = mealdb_recipe(entry('themealdb'), {'meals': [meal]})
        self.assertEqual(credit['text_rights'], 'source_authorship_unresolved')
        self.assertIsNone(recipe['portions'])
        meal['strSource'] = 'https://example.org/original?id=3'
        recipe, credit = mealdb_recipe(entry('themealdb'), {'meals': [meal]})
        self.assertEqual(credit['text_rights'], 'third_party_source_permission_unresolved')
        self.assertEqual(recipe['source']['original']['url'], meal['strSource'])

    def test_revision_mismatch_is_rejected(self):
        with self.assertRaises(SourceParseError):
            wikibooks_recipe(entry(), {'parse': {'pageid': 123, 'revid': 999, 'text': ''}})

    def test_procedure_subsections_keep_all_cooking_steps(self):
        recipe, _ = wiki('<h2>Ingredients</h2><p>200 g salmon</p><h2>Procedure</h2>'
                         '<h3>Preparation</h3><ol><li>Heat the oven.</li></ol>'
                         '<h3>Rice</h3><ol><li>Boil the rice.</li></ol>'
                         '<h3>Salmon</h3><ol><li>Bake the salmon.</li><li>Serve.</li></ol>'
                         '<h2>Warnings</h2><p>Check for bones.</p>')
        self.assertEqual(recipe['steps'], ['Heat the oven.', 'Boil the rice.', 'Bake the salmon.', 'Serve.'])
        self.assertEqual(recipe['notes'], 'Check for bones.')

    def test_unrelated_table_cannot_supply_person_servings(self):
        recipe, _ = wiki('<table><tr><th>Servings</th><td>4</td></tr></table>'
                         '<h2>Ingredients</h2><p>200 g salmon</p><h2>Procedure</h2><p>Bake.</p>')
        self.assertIsNone(recipe['portions'])

    def test_nested_ingredient_alternatives_are_not_one_ready_item(self):
        with self.assertRaises(SourceParseError):
            wiki('<h2>Ingredients</h2><ul><li>200 g flour<ul><li>Or 400 g rice</li></ul></li></ul>'
                 '<h2>Procedure</h2><p>Mix.</p>')

    def test_explicit_optional_source_annotation_survives(self):
        recipe, _ = wiki('<h2>Ingredients</h2><p>20 g sugar (optional)</p><h2>Procedure</h2><p>Mix.</p>')
        self.assertTrue(recipe['ingredients'][0]['optional'])
        self.assertEqual(recipe['ingredients'][0]['item'], 'sugar')
        self.assertIn('(optional)', recipe['ingredients'][0]['original_text'])

    def test_explicit_source_metric_equivalent_is_not_generic_cup_conversion(self):
        recipe, _ = wiki('<h2>Ingredients</h2><ul><li>3 cups (710 ml) corn chips</li>'
                         '<li>2 (400 g) cans beans</li><li>1 cup rice</li></ul>'
                         '<h2>Procedure</h2><p>Mix.</p>')
        self.assertEqual(recipe['ingredients'][0]['quantity'], {'numerator': 710, 'denominator': 1})
        self.assertEqual(recipe['ingredients'][0]['unit'], 'ml')
        self.assertEqual(recipe['ingredients'][0]['evidence']['quantity']['basis'], 'source')
        self.assertIsNone(recipe['ingredients'][1]['quantity'])
        self.assertIsNone(recipe['ingredients'][2]['quantity'])

    def test_count_punctuation_preserves_food_part_identity(self):
        recipe, _ = wiki('<h2>Ingredients</h2><ul><li>2 egg whites</li><li>1 onion , chopped</li></ul>'
                         '<h2>Procedure</h2><p>Mix.</p>')
        self.assertEqual(recipe['ingredients'][0]['item'], 'egg whites')
        self.assertEqual(recipe['ingredients'][0]['unit'], 'count')
        self.assertEqual(recipe['ingredients'][1]['quantity'], {'numerator': 1, 'denominator': 1})
        self.assertEqual(recipe['ingredients'][1]['original_text'], '1 onion , chopped')

    def test_yield_explicitly_naming_people_can_establish_servings(self):
        recipe, _ = wiki('<table class="infobox"><tr><th>Yield</th><td>4 servings</td></tr></table>'
                         '<h2>Ingredients</h2><p>100 g rice</p><h2>Procedure</h2><p>Boil.</p>')
        self.assertEqual(recipe['portions'], 4)
        self.assertEqual(recipe['portions_evidence']['input'], '4 servings')

    def test_additional_and_alternative_amounts_never_become_ready_totals(self):
        for text in ['1 cup (300 g / 10.5 oz) chocolate spread, plus extra for topping',
                     '⅛ tsp (0.625 ml) champagne yeast or 2 tsp (10 ml) kefir grains',
                     '2 cups (100 g/person) rice']:
            with self.subTest(text=text):
                recipe, _ = wiki('<table class="infobox"><tr><th>Servings</th><td>2</td></tr></table>'
                                 f'<h2>Ingredients</h2><p>{text}</p><h2>Procedure</h2><p>Prepare.</p>')
                self.assertIsNone(recipe['ingredients'][0]['quantity'])
                self.assertEqual(readiness(recipe)[0], 'draft')


class FileBoundaryTests(unittest.TestCase):
    def test_traversal_and_symlink_never_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ['../escape', '/absolute', 'a//b', 'a/./b', 'a\\b']:
                with self.assertRaises(PackBuildError):
                    confined(root, name)
            (root / 'link').symlink_to(root.parent, target_is_directory=True)
            with self.assertRaises(PackBuildError):
                confined(root, 'link/escape')

    def test_source_corruption_and_size_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = write_file(root, 'source.json', b'{}')
            self.assertEqual(read_file(root, 'source.json', 2, info), b'{}')
            with self.assertRaises(PackBuildError):
                read_file(root, 'source.json', 1, info)
            (root / 'source.json').write_bytes(b'[]')
            with self.assertRaises(PackBuildError):
                read_file(root, 'source.json', 2, info)

    def test_fifo_is_rejected_without_blocking(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.mkfifo(root / 'pipe')
            with self.assertRaises(PackBuildError):
                read_file(root, 'pipe', 10)

    def test_atomic_write_does_not_modify_hardlinked_source(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'source').write_bytes(b'original')
            os.link(root / 'source', root / 'output')
            os.link(root / 'source', root / 'output.tmp')
            write_file(root, 'output', b'new')
            self.assertEqual((root / 'source').read_bytes(), b'original')
            self.assertEqual((root / 'output').read_bytes(), b'new')


class BuildRoundtripTests(unittest.TestCase):
    def fixture(self, root):
        source = root / 'snapshot'
        source.mkdir()
        rows = []
        for number in (123, 124):
            row = entry()
            row.update(source_id=str(number), classification='recipe', status='downloaded')
            payload = {'parse': {'pageid': number, 'revid': 456, 'text':
                       '<table class="infobox"><tr><th>Servings</th><td>2</td></tr></table>'
                       '<h2>Ingredients</h2><p>100 g rice</p><h2>Procedure</h2><p>Boil.</p>'}}
            info = write_file(source, f'responses/{number}.json', encoded(payload))
            row['raw'] = info
            row['rendered'] = {**info, 'fetched_at': row['fetched_at']}
            rows.append(row)
        files = {}
        for name, data in [('wikibooks-manifest.json', rows), ('themealdb-manifest.json', [])]:
            files[name] = write_file(source, name, encoded(data))
        snapshot = {'snapshot_id': 'synthetic', 'scope': 'Two synthetic recipes', 'source_limitations': [], 'files': files}
        raw = encoded(snapshot)
        write_file(source, 'snapshot.json', raw)
        write_file(source, 'SEALED', b'synthetic fixture\n')
        return source, digest(raw)

    def test_clean_repeat_and_resume_use_real_shared_codec(self):
        from recipe_portable import open_archive
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, sha = self.fixture(root)
            options = {'snapshot_sha256': sha, 'pack_version': 'test.1'}
            first = build(source, root / 'clean', **options)
            path = root / 'clean' / first['archive']
            original = path.read_bytes()
            repeated = build(source, root / 'clean', **options)
            self.assertEqual(repeated['cache_reused'], 2)
            self.assertEqual(original, path.read_bytes())
            partial = build(source, root / 'resume', stop_after=1, **options)
            self.assertFalse(partial['complete'])
            self.assertFalse((root / 'resume' / first['archive']).exists())
            resumed = build(source, root / 'resume', **options)
            self.assertEqual(resumed['cache_reused'], 1)
            self.assertEqual(original, (root / 'resume' / first['archive']).read_bytes())
            with open_archive(path) as archive:
                self.assertEqual(archive.verify()['records_count'], 2)
                records = list(archive.records())
                self.assertEqual([r['recipe_id'] for r in records], ['wikibooks:123', 'wikibooks:124'])
                self.assertTrue(all(r['recipe']['source_provider'] is None for r in records))
                self.assertFalse(any('cache' in name or name.startswith('/') for name in archive.entries))

    def test_build_rejects_overlapping_source_and_wrong_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, sha = self.fixture(root)
            with self.assertRaises(PackBuildError):
                build(source, source / 'output', snapshot_sha256=sha, pack_version='test.1')
            with self.assertRaises(PackBuildError):
                build(source, root / 'output', snapshot_sha256='0' * 64, pack_version='test.1')

    def test_corrupt_cover_repair_resumes_to_clean_exact_archive(self):
        from recipe_portable import open_archive
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = self.fixture(root)
            covers_root = root / 'covers'
            _, cover_manifest, _, image_bytes = CoverHandoffTests().fixture(covers_root)
            rows = json.loads((source / 'wikibooks-manifest.json').read_bytes())
            rows[0]['image'] = {'file': {'sha256': '3' * 64}, 'description_url': 'https://example.org/photo',
                                'license_metadata': {'LicenseShortName': {'value': 'CC BY 4.0'},
                                                     'LicenseUrl': {'value': 'https://creativecommons.org/licenses/by/4.0/'},
                                                     'Artist': {'value': 'Example creator'}}}
            snapshot = json.loads((source / 'snapshot.json').read_bytes())
            snapshot['files']['wikibooks-manifest.json'] = write_file(source, 'wikibooks-manifest.json', encoded(rows))
            snapshot_bytes = encoded(snapshot)
            write_file(source, 'snapshot.json', snapshot_bytes)
            sha = digest(snapshot_bytes)
            cover_manifest['source_snapshot_sha256'] = sha
            association = cover_manifest['recipes'][0]
            association.update(source_raw_sha256=rows[0]['raw']['sha256'], source_rendered_sha256=rows[0]['rendered']['sha256'])
            cover_manifest['recipes'].append({'source': 'wikibooks', 'source_id': '124', 'revision': 456,
                                             'source_raw_sha256': rows[1]['raw']['sha256'],
                                             'source_rendered_sha256': rows[1]['rendered']['sha256'], 'status': 'no_source_cover'})
            cover_manifest['profile_sha256'] = '5' * 64
            cover_bytes = encoded(cover_manifest)
            write_file(covers_root, 'covers-manifest.json', cover_bytes)
            options = {'snapshot_sha256': sha, 'pack_version': 'test.1', 'covers_root': covers_root,
                       'covers_manifest_sha256': digest(cover_bytes)}
            clean = build(source, root / 'clean', **options)
            clean_bytes = (root / 'clean' / clean['archive']).read_bytes()
            write_file(covers_root, association['path'], b'corrupt')
            corrupt = build(source, root / 'repair', **options)
            self.assertEqual(corrupt['records'], 2)
            self.assertEqual(corrupt['assets'], 0)
            self.assertEqual(corrupt['counts']['wikibooks.image_invalid_derivative'], 1)
            (covers_root / association['path']).unlink()
            missing = build(source, root / 'repair', **options)
            self.assertEqual(missing['records'], 2)
            with open_archive(root / 'repair' / missing['archive']) as archive:
                attribution = b''.join(archive.chunks('attribution.json')).decode()
                self.assertNotIn(str(root), attribution)
                self.assertNotIn(str(root.resolve()), attribution)
                self.assertIn('managed_cover_unavailable_or_invalid', attribution)
            write_file(covers_root, association['path'], image_bytes)
            repaired = build(source, root / 'repair', **options)
            self.assertEqual(repaired['assets'], 1)
            self.assertEqual((root / 'repair' / repaired['archive']).read_bytes(), clean_bytes)
            with open_archive(root / 'repair' / repaired['archive']) as archive:
                self.assertEqual(archive.read_asset(association['asset_id']), image_bytes)


class CoverHandoffTests(unittest.TestCase):
    def fixture(self, root):
        import io
        from PIL import Image
        from recipe_assets import sanitize_image
        raw = io.BytesIO()
        Image.new('RGB', (3, 2), 'orange').save(raw, 'PNG')
        data = sanitize_image(raw.getvalue())
        sha = digest(data)
        row = entry()
        row['classification'] = 'recipe'
        row['image'] = {'file': {'sha256': '3' * 64}}
        asset = {'status': 'complete', 'asset_id': 'sha256:' + sha, 'path': f'assets/{sha}.jpg',
                 'sha256': sha, 'bytes': len(data), 'processing': {'selected_frame': 0}}
        association = {**asset, 'source': row['source'], 'source_id': row['source_id'], 'revision': row['revision'],
                       'source_raw_sha256': row['raw']['sha256'], 'source_rendered_sha256': row['rendered']['sha256'],
                       'source_original_sha256': '3' * 64}
        manifest = {'schema': 'meal-concierge-derived-covers/1', 'status': 'complete', 'source_snapshot_sha256': '4' * 64,
                    'profile': {'id': 'managed-jpeg-960-q85-v1'}, 'assets': {'3' * 64: asset}, 'recipes': [association]}
        encoded_manifest = encoded(manifest)
        write_file(root, 'covers-manifest.json', encoded_manifest)
        write_file(root, asset['path'], data)
        return row, manifest, digest(encoded_manifest), data

    def test_verified_handoff_keeps_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row, manifest, sha, data = self.fixture(root)
            covers = Covers(root, sha, '4' * 64, [row])
            self.assertEqual(covers.read(manifest['recipes'][0]['asset_id']), data)
            write_file(root, manifest['recipes'][0]['path'], b'corrupt')
            with self.assertRaises(PackBuildError):
                covers.read(manifest['recipes'][0]['asset_id'])

    def test_wrong_snapshot_or_recipe_association_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row, manifest, sha, _ = self.fixture(root)
            with self.assertRaises(PackBuildError):
                Covers(root, sha, '5' * 64, [row])
            manifest['recipes'][0]['source_original_sha256'] = '6' * 64
            raw = encoded(manifest)
            write_file(root, 'covers-manifest.json', raw)
            with self.assertRaises(PackBuildError):
                Covers(root, digest(raw), '4' * 64, [row])


if __name__ == '__main__':
    unittest.main()
