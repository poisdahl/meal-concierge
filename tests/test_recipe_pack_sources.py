"""Regression shapes from the sealed Wikibooks failures, using inert source HTML."""
from pathlib import Path
import sys
import json
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recipe_pack_sources import SourceHTML, SourceParseError, readiness, wikibooks_recipe


def read(html, source_id, revision):
    entry = {
        'source': 'wikibooks', 'source_id': source_id, 'revision': revision,
        'title': 'Cookbook:Source fixture',
        'url': 'https://en.wikibooks.org/wiki/Cookbook:Source_fixture',
        'credit': 'Wikibooks contributors; synthetic regression structure',
        'fetched_at': '2026-09-06T00:00:00+00:00',
        'raw': {'sha256': '1' * 64},
        'rendered': {'sha256': '2' * 64, 'fetched_at': '2026-09-06T00:00:00+00:00'},
    }
    return wikibooks_recipe(entry, {'parse': {'pageid': int(source_id), 'revid': revision, 'text': html}})


def dish(ingredients='250 g flour', ingredient_heading='Ingredients', procedure_heading='Procedure'):
    return (f'<h2>{ingredient_heading}</h2><ul><li>{ingredients}</li></ul>'
            f'<h2>{procedure_heading}</h2><ol><li>Mix and cook.</li></ol>')


class SealedSourceRecoveryTests(unittest.TestCase):
    def test_related_link_table_is_not_an_instruction_but_keeps_attribution(self):
        notice = ('<table class="plainlinks messagebox mbox-side mbox-side-notice"><tr><td>'
                  'Wikipedia has related information at <a href="https://en.wikipedia.org/wiki/Ceviche">'
                  'Ceviche</a>.</td></tr></table>')
        html = dish() + notice
        recipe, credit = read(html, '9016', 4510241)
        self.assertEqual(recipe['steps'], ['Mix and cook.'])
        self.assertEqual(credit['source_notices'], SourceHTML(html).root.text())
        self.assertIn('https://en.wikipedia.org/wiki/Ceviche', credit['source_links'])
        with self.assertRaisesRegex(SourceParseError, 'procedure table'):
            read(html, '9016', 4510242)
        # A culinary table with the same location cannot inherit the exception.
        with self.assertRaisesRegex(SourceParseError, 'procedure table'):
            read(html.replace('Wikipedia has related information at', 'Add these ingredients:'), '9016', 4510241)

    def test_notice_and_nutrition_tables_do_not_add_food_or_servings(self):
        cases = [
            ('18354', 4617976, '<table class="box-Metricate"><tr><td>This article or section exclusively uses non- SI units of measurement.</td></tr></table>'),
            ('38535', 4532021, '<table><tr><th>NUTRITION FACTS</th></tr><tr><td>Serving Size: 100 g</td></tr><tr><td>Servings Per Recipe: 30</td></tr></table>'),
        ]
        for sid, rev, table in cases:
            with self.subTest(source_id=sid):
                html = dish().replace('<h2>Procedure', table + '<h2>Procedure')
                recipe, credit = read(html, sid, rev)
                self.assertEqual(len(recipe['ingredients']), 1)
                self.assertIsNone(recipe['portions'])
                self.assertEqual(credit['source_notices'], SourceHTML(html).root.text())
        text = 'vg This recipe is vegetarian ; it contains no meat. Milk is present.'
        html = dish() + '<table><tr><td><table><tr><td>' + text + '</td></tr></table></td></tr></table>'
        recipe, credit = read(html, '23267', 4522414)
        self.assertEqual(recipe['steps'], ['Mix and cook.'])
        self.assertIn('Milk is present.', credit['source_notices'])

    def test_heading_citations_remain_in_source_evidence(self):
        citation = '<sup class="reference"><a href="https://example.org/source">[1]</a></sup>'
        html = dish(procedure_heading='Procedure ' + citation)
        recipe, credit = read(html, '33062', 4514693)
        self.assertEqual(recipe['steps'], ['Mix and cook.'])
        self.assertIn('[1]', credit['source_notices'])
        self.assertEqual(credit['source_notices'], SourceHTML(html).root.text())
        with self.assertRaisesRegex(SourceParseError, 'reviewed source structure changed'):
            read(html.replace('class="reference"', 'class="other"'), '33062', 4514693)

    def test_ingredient_and_method_subheadings_stay_in_notes(self):
        html = (dish() + '<h2>Notes, tips, and variations</h2><h3>Ingredients</h3>'
                '<p>Use more flour for a thicker cookie.</p><h3>Method</h3><p>Cool on a rack.</p>')
        recipe, credit = read(html, '415629', 4630845)
        self.assertEqual(len(recipe['ingredients']), 1)
        self.assertEqual(recipe['steps'], ['Mix and cook.'])
        self.assertEqual(recipe['notes'], 'Use more flour for a thicker cookie.\nCool on a rack.')
        self.assertEqual(credit['source_notices'], SourceHTML(html).root.text())
        # A second top-level Ingredients section is still conflicting source data.
        with self.assertRaisesRegex(SourceParseError, 'reviewed source structure changed|multiple ingredient'):
            read(html.replace('<h3>Ingredients</h3>', '<h2>Ingredients</h2>'), '415629', 4630845)

    def test_exact_kefir_servings_and_quantities_survive_notes_recovery(self):
        html = ('<table class="infobox"><tr><th>Servings</th><td>5</td></tr></table>'
                '<h2>Ingredients</h2><ul><li>50 grams kefir grains</li><li>500 ml milk</li></ul>'
                '<h2>Preparation</h2><p>Combine and ferment.</p>'
                '<h2>Notes</h2><h3>Ingredients</h3><p>Use the source grains.</p>')
        recipe, credit = read(html, '461702', 4511022)
        self.assertEqual(recipe['portions'], 5)
        self.assertEqual([i['quantity'] for i in recipe['ingredients']],
                         [{'numerator': 50, 'denominator': 1}, {'numerator': 500, 'denominator': 1}])
        self.assertEqual(readiness(recipe), ('ready', []))  # Amounts alone are resolved.
        self.assertEqual(credit['normalization_issues'], ['source_quantity_guidance_conflict'])
        self.assertIn('about 1:10 or 5% w/w', recipe['notes'])

    def test_conflicting_kefir_source_is_draft_in_the_actual_build_consumer(self):
        from build_recipe_pack import build, digest, encoded, write_file
        html = ('<table class="infobox"><tr><th>Servings</th><td>5</td></tr></table>'
                '<h2>Ingredients</h2><ul><li>50 grams kefir grains</li><li>500 ml milk</li></ul>'
                '<h2>Preparation</h2><ol><li>Shake kefir grains to remove excess kefir. '
                'Rinsing is not necessary (but optionally, rinse in fresh milk).</li>'
                '<li>Place kefir grains in glass jar or jug with fresh milk. Generally, keep a ratio '
                'of kefir grains to milk of about 1:10 or 5% w/w.</li></ol>'
                '<h2>Notes</h2><h3>Ingredients</h3><p>Keep the original source guidance.</p>')
        recipe, credit = read(html, '461702', 4511022)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'source', root / 'output'
            source.mkdir()
            payload = {'parse': {'pageid': 461702, 'revid': 4511022, 'text': html}}
            raw = write_file(source, 'response.json', encoded(payload))
            row = {'source': 'wikibooks', 'source_id': '461702', 'revision': 4511022,
                   'title': 'Cookbook:Plain Kefir', 'classification': 'recipe',
                   'url': 'https://en.wikibooks.org/wiki/Cookbook:Plain_Kefir',
                   'fetched_at': '2026-09-06T00:00:00+00:00', 'raw': raw,
                   'rendered': {**raw, 'fetched_at': '2026-09-06T00:00:00+00:00'}}
            files = {name: write_file(source, name, encoded(value)) for name, value in
                     [('wikibooks-manifest.json', [row]), ('themealdb-manifest.json', [])]}
            snapshot = encoded({'snapshot_id': 'synthetic-kefir', 'scope': 'One synthetic regression',
                                'source_limitations': [], 'files': files})
            write_file(source, 'snapshot.json', snapshot)
            write_file(source, 'SEALED', b'synthetic fixture\n')
            result = build(source, output, snapshot_sha256=digest(snapshot), pack_version='test.1', stop_after=1)
            self.assertFalse(result['complete'])
            self.assertEqual(list(output.rglob('*.zip')), [])
            cached = json.loads(next(output.glob('cache/*/wikibooks-461702.json')).read_text())['result']
            self.assertEqual(cached['status'], 'draft')
            self.assertIn('source_quantity_guidance_conflict', cached['reasons'])
            self.assertEqual(cached['recipe']['ingredients'], recipe['ingredients'])
            self.assertEqual(cached['recipe']['steps'], recipe['steps'])
            self.assertEqual(cached['credit']['source_notices'], credit['source_notices'])
            self.assertIn('about 1:10 or 5% w/w', cached['recipe']['steps'][1])

    def test_known_heading_repairs_keep_unknowns_and_do_not_generalize(self):
        cases = [
            ('415349', 4517861, 'Ingredients', 'Process'),
            ('462355', 4613690, 'Ingredients', 'Process'),
            ('462387', 4508979, 'Ingredients', 'Process'),
            ('254237', 4509833, 'Ingredients for 4 people', 'Preparation'),
            ('447194', 4535488, 'Procedures', 'Procedure'),
            ('456925', 4587492, 'Procedure', 'Instructions'),
            ('476395', 4522969, 'Recipe', 'Preparation'),
            ('479750', 4601579, 'Recipes', 'Procedure'),
            ('479752', 4601580, 'Recipes', 'Procedure'),
            ('483186', 4634713, 'INGRDIENTS', 'PROCEDURE'),
            ('412220', 4587440, 'Ingredients', 'Recipe'),
        ]
        for sid, rev, ingredients, procedure in cases:
            with self.subTest(source_id=sid):
                html = dish('Salt to taste', ingredients, procedure)
                recipe, credit = read(html, sid, rev)
                self.assertEqual(recipe['ingredients'][0]['original_text'], 'Salt to taste')
                self.assertIsNone(recipe['ingredients'][0]['quantity'])
                self.assertIsNone(recipe['portions'])
                self.assertEqual(readiness(recipe)[0], 'draft')
                self.assertEqual(credit['source_notices'], SourceHTML(html).root.text())
                with self.assertRaises(SourceParseError):
                    read(html, sid, rev + 1)

    def test_only_empty_nested_wrapper_is_unwrapped_and_incompleteness_remains(self):
        html = ('<p>This recipe is incomplete.</p><h2>Ingredients</h2><ul><li><ul>'
                '<li>1 kg beef</li><li>Salt to taste</li><li>Wooden skewers</li></ul></li></ul>'
                '<h2>Procedure</h2><p>Skewer and cook.</p>')
        recipe, credit = read(html, '483114', 4634633)
        self.assertEqual([i['original_text'] for i in recipe['ingredients']],
                         ['1 kg beef', 'Salt to taste', 'Wooden skewers'])
        self.assertIn('source_marks_recipe_incomplete', credit['normalization_issues'])
        self.assertIsNone(recipe['portions'])
        self.assertEqual(readiness(recipe)[0], 'draft')
        self.assertEqual(credit['source_notices'], SourceHTML(html).root.text())
        with self.assertRaisesRegex(SourceParseError, 'nested ingredient'):
            read(html.replace('<li><ul>', '<li>Choose any of these:<ul>'), '483114', 4634633)

    def test_missing_or_repeated_repair_shape_fails_explicitly(self):
        for html in (dish(), dish(procedure_heading='Process') + '<h2>Process</h2><p>Again.</p>'):
            with self.subTest(html=html), self.assertRaisesRegex(SourceParseError, 'reviewed source structure changed'):
                read(html, '415349', 4517861)
        with self.assertRaisesRegex(SourceParseError, 'procedure table'):
            read(dish(procedure_heading='Process') + '<table><tr><td>Extra cooking step.</td></tr></table>',
                 '415349', 4517861)

    def test_unreviewed_alternatives_and_conflicts_still_fail(self):
        html = dish().replace('250 g flour', 'Filling of choice<ul><li>Salmon</li><li>Tuna</li></ul>')
        with self.assertRaisesRegex(SourceParseError, 'nested ingredient'):
            read(html, '14077', 123)
        with self.assertRaisesRegex(SourceParseError, 'multiple ingredient'):
            read(dish() + dish('3 spoons oil'), '446276', 123)
        with self.assertRaisesRegex(SourceParseError, 'missing complete'):
            read('<h2>Ingredients</h2><ul><li>2 potatoes</li></ul>', '482477', 123)


if __name__ == '__main__':
    unittest.main()
