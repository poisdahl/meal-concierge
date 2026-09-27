import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_recipe_preparation as fixtures
from recipe_portable import validate_withdrawals
from recipes import RecipeError
import recipe_channel_publish as publisher


class WithdrawalTests(unittest.TestCase):
    def test_membership_and_cumulative_reasons(self):
        reason = {'category':'quality','reason':'Cannot recover a usable method.'}
        publisher.verify_withdrawals(({'a','b'},{}), ({'a'}, {'b':reason}))
        publisher.verify_withdrawals(({'a'},{'b':reason}), ({'a','c'},{'b':reason}))
        publisher.verify_withdrawals(({'a'},{'b':reason}), ({'a','b'},{}))
        for current in (({'a'},{}), ({'a'},{'z':reason}), ({'a','b'},{'b':reason})):
            with self.assertRaises(RuntimeError): publisher.verify_withdrawals(({'a','b'},{}),current)
        with self.assertRaises(RuntimeError): publisher.verify_withdrawals(({'a'},{'b':reason}),({'a'},{}))
        with self.assertRaises(RecipeError): validate_withdrawals({'a':{'category':'rights','reason':' '}})

    def test_real_changed_channel_reads_baseline_before_put(self):
        fixture = fixtures.PreparationTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        old, previous = fixture.pack('1'); new, current = fixture.pack('2')
        payload = (json.dumps(current,indent=2,sort_keys=True)+'\n').encode()
        def stage(directory, *, expected):
            self.assertTrue(Path(directory).is_dir())
            return old if expected == previous else new
        with patch.object(publisher,'published_recipe_pack',return_value=current), patch.object(publisher,'stage_recipe_pack',side_effect=stage), patch.object(publisher,'api',side_effect=[{'object':{'sha':'channel'}},{'sha':'old-content','content':base64.b64encode(json.dumps(previous).encode()).decode()}, {}, {'content':base64.b64encode(payload).decode()}]) as api:
            publisher.publish('synthetic')
        self.assertEqual(api.call_args_list[2].kwargs['data']['sha'],'old-content')

    def test_missing_baseline_needs_explicit_initial_publication(self):
        fixture = fixtures.PreparationTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        archive, expected = fixture.pack()
        with patch.object(publisher,'published_recipe_pack',return_value=expected), patch.object(publisher,'stage_recipe_pack',return_value=archive), patch.object(publisher,'api',return_value=None) as api:
            with self.assertRaisesRegex(RuntimeError,'initial-publication'): publisher.publish('synthetic')
            self.assertEqual(api.call_count,1)

    def test_builder_omits_withdrawn_record_and_import_preserves_frozen_history(self):
        import hashlib
        import tempfile
        from build_recipe_pack import build
        from test_recipe_pack_build import BuildRoundtripTests
        from recipe_portable import open_archive, preflight_archive, apply_archive
        from recipes import RecipeStore
        from core import StateStore
        import install
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source, digest=BuildRoundtripTests().fixture(root)
            state=StateStore(root/'household', {'household':'withdrawal-test','provider':'oda'})
            def archive(version, **options):
                result=build(source,root/('build-'+version),snapshot_sha256=digest,pack_version=version,**options)
                path=root/('build-'+version)/result['archive']
                with open_archive(path) as opened:
                    manifest=opened.manifest
                expected={key:manifest[key] for key in ('format','format_version','kind','recipe_schema_version','pack_id','pack_version','normalizer_version')}
                expected.update(bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                preflight_archive(path,expected)
                return path,expected,result
            old, expected, _=archive('test.1')
            apply_archive(old,state.directory,'withdrawal-test',expected)
            bank=RecipeStore(state.directory/'recipes.sqlite3','withdrawal-test')
            rows=bank.search(limit=10)
            removed=next(row for row in rows if row['pack']['recipe_id']=='wikibooks:123')
            bank.set_favorite(removed['library_recipe_ref'],True,idempotency_key='favorite')
            with state.locked() as data:
                data['order_snapshots']['synthetic-order']={'dishes':[removed]}
            frozen=state.path.read_bytes()
            reasons={'wikibooks:123':{'category':'quality','reason':'Missing reliable cooking details.'}}
            ledger=root/'withdrawals.json'; ledger.write_text(json.dumps(reasons))
            new, expected, result=archive('test.2',withdrawals=ledger)
            self.assertEqual(result['records'],1)
            self.assertEqual(result['counts']['wikibooks.withdrawn'],1)
            report=apply_archive(new,state.directory,'withdrawal-test',expected)
            self.assertEqual((report['deleted'],report['deleted_favorites']),(1,1))
            self.assertEqual(report['withdrawals'],[{'recipe_id':'wikibooks:123',**reasons['wikibooks:123']}])
            self.assertEqual(state.path.read_bytes(),frozen)
            self.assertEqual([row['pack']['recipe_id'] for row in bank.search(limit=10)],['wikibooks:124'])
