from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
import test_meal_concierge_recipes as fixtures
from core import StateStore
from service import Application
from recipes import RecipeStore, RecipeError, normalize_recipe
from recipe_libraries import RecipeLibraryDefiniteError, RecipeLibraryError
import recipe_migration as migration


class Library(fixtures.SyntheticLibraryAdapter):
    def __init__(self, library_id, recipes=()):
        super().__init__(library_id, fixtures.full_recipe(), reconcile=True, favorite_state=False)
        self.docs = {str(i): normalize_recipe(doc) for i, doc in enumerate(recipes)}
        self.versions = {key: 'v1' for key in self.docs}
        self.created_operations = {}
        self.labels = []
        self.associations = []
        self.label_writes = []
        self.labels_enabled = False
        self.page_size = 2
        self.fail_names = set()
        self.timeout = False
        self.reconcile_available = True
        self.storage_supported = True

    def ref(self, key):
        return {'library_id': self.library_id, 'recipe_id': key, 'version': self.versions[key]}

    def capabilities(self):
        result = super().capabilities()
        result['create_from_discovery'] = self.storage_supported
        result['label_read'] = result['label_apply_existing'] = result['label_reconcile'] = self.labels_enabled
        return result

    def search(self, query, filters, cursor, limit):
        offset = int(cursor or 0)
        keys = [k for k, d in self.docs.items() if query in d['name']]
        selected = keys[offset:offset + min(limit, self.page_size)]
        return {'recipes': [{'library_recipe_ref': self.ref(k)} for k in selected],
                'cursor': str(offset + len(selected)) if offset + len(selected) < len(keys) else None}

    def get(self, ref):
        return {**deepcopy(self.docs[ref['recipe_id']]), 'library_recipe_ref': self.ref(ref['recipe_id']), 'is_favorite': self.favorite_state}

    def _native_payload(self, snapshot, operation):
        return {}, deepcopy(snapshot)

    def create_from_snapshot(self, snapshot, operation):
        self.create_calls += 1
        if snapshot['name'] in self.fail_names:
            raise RecipeLibraryDefiniteError('private provider rejection')
        key = 'new-' + str(self.create_calls)
        self.docs[key] = normalize_recipe(snapshot)
        self.versions[key] = 'v1'
        self.reference = self.ref(key)
        result = {'library_recipe_ref': self.ref(key), 'recipe': deepcopy(snapshot)}
        self.created_operations[operation['operation_id']] = result
        if self.timeout:
            raise TimeoutError('private provider response')
        return result

    def reconcile_create(self, snapshot, operation):
        self.reconcile_calls += 1
        return deepcopy(self.created_operations.get(operation['operation_id'])) if self.reconcile_available else None

    def list_labels(self):
        return deepcopy(self.labels)

    def get_recipe_labels(self, ref):
        return deepcopy(self.associations)

    def set_label(self, reference, label_reference, present, *, expected_label_revision=None):
        self.label_writes.append(label_reference)
        self.associations = [label for label in self.labels if label["library_label_ref"] == label_reference]
        return {'library_id': self.library_id, 'library_recipe_ref': reference, 'library_label_ref': label_reference, 'present': present}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name), {**fixtures.CONFIG, 'provider': 'oda'})
        self.app = Application(self.store, fixtures.FakeOda(), fixtures.FakeBrowser())
        self.source = Library('source', [fixtures.full_recipe('First', external_id='one'), fixtures.full_recipe('Second', external_id='two')])
        self.dest = Library('destination')
        for library in (self.source, self.dest):
            self.app.recipe_libraries[library.library_id] = {'library_id': library.library_id, 'provider': 'mealie', 'display_name': library.library_id, 'read_only': False}
            self.app.recipe_library_adapters[library.library_id] = library

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **kwargs):
        return self.app.handle({'operation': 'migration', 'action': 'prepare', 'source_library_id': 'source', 'destination_library_id': 'destination', 'metadata_options': {'favorites': 'omit', 'labels': 'omit'}, **kwargs})

    def execute(self, plan):
        return self.app.handle({'operation': 'migration', 'action': 'execute', 'plan_id': plan['plan_id'], 'confirmation': {'plan_digest': plan['plan_digest'], 'statement': plan['confirmation_statement']}})

    def test_copy_preview_exact_confirmation_pagination_idempotency_private_output(self):
        self.source.page_size = 1
        source_before = deepcopy(self.source.docs)
        plan = self.prepare()
        self.assertEqual([i['status'] for i in plan['items']], ['create', 'create'])
        self.assertEqual(self.dest.create_calls, 0)
        self.assertNotIn('Stek fisken', json.dumps(plan))
        with self.assertRaises(RecipeError):
            self.app.handle({'operation': 'migration', 'action': 'execute', 'plan_id': plan['plan_id'], 'confirmation': True})
        result = self.execute(plan)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.dest.create_calls, 2)
        self.assertEqual(self.execute(plan), result)
        self.assertEqual(self.source.docs, source_before)
        self.assertEqual(self.app.primary_recipe_library_id, 'builtin')
        again = self.prepare()
        self.assertEqual([i['status'] for i in again['items']], ['already_mapped', 'already_mapped'])

    def test_exact_existing_conflicts_and_no_name_url_dedup(self):
        self.dest.docs['exact'] = deepcopy(self.source.docs['0']); self.dest.versions['exact'] = 'v1'
        conflict = deepcopy(self.source.docs['1']); conflict['steps'] = ['Different']
        self.dest.docs['conflict'] = conflict; self.dest.versions['conflict'] = 'v1'
        plan = self.prepare()
        self.assertEqual([i['status'] for i in plan['items']], ['exact_existing', 'conflict'])
        result = self.execute(plan)
        self.assertEqual([i['copy_status'] for i in result['items']], ['confirmed', 'skipped'])
        self.assertEqual(self.dest.create_calls, 0)
        for doc in self.source.docs.values():
            doc['source']['external_id'] = None
            doc['source']['url'] = 'https://example.org/same'
            doc['name'] = 'Same name'
        plan = self.prepare(source_refs=[self.source.ref('1')])
        self.assertEqual(plan['items'][0]['status'], 'create')

    def test_source_and_destination_drift_block_dispatch(self):
        for drift in ('source', 'destination', 'metadata'):
            with self.subTest(drift=drift):
                plan = self.prepare(source_refs=[self.source.ref('0')], metadata_options={'favorites': 'preserve', 'labels': 'omit'})
                if drift == 'source': self.source.versions['0'] = 'v2'
                elif drift == 'metadata': self.source.favorite_state = True
                else:
                    self.dest.docs['drift'] = deepcopy(self.source.docs['0']); self.dest.versions['drift'] = 'v1'
                result = self.execute(plan)
                self.assertEqual(result['status'], 'needs_review', result)
                self.assertEqual(self.dest.create_calls, 0)
                self.source.versions['0'] = 'v1'; self.source.favorite_state = False; self.dest.docs.clear(); self.dest.versions.clear()

    def test_timeout_restart_reconciliation_and_cross_plan_block(self):
        self.dest.timeout = True; self.dest.reconcile_available = False
        plan = self.prepare(source_refs=[self.source.ref('0')])
        result = self.execute(plan)
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(self.prepare(source_refs=[self.source.ref('0')])['items'][0]['status'], 'unavailable')
        self.app._recipe_operations_recovered = False
        self.assertEqual(self.execute(plan)['status'], 'uncertain')
        self.dest.reconcile_available = True
        self.assertEqual(self.execute(plan)['status'], 'complete')
        self.assertEqual(self.dest.create_calls, 1)

    def test_partial_failure_preserves_confirmed_and_concurrent_resume(self):
        self.dest.fail_names.add('Second')
        plan = self.prepare()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.execute(plan))) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(10)
        self.assertEqual(len(results), 2)
        self.assertEqual([i['copy_status'] for i in results[-1]['items']], ['confirmed', 'failed'])
        self.assertEqual(self.dest.create_calls, 2)
        self.assertEqual(len(self.dest.docs), 1)

    def test_rights_unsupported_metadata_and_explicit_omit(self):
        self.dest.storage_supported = False
        self.assertEqual(self.prepare()['items'][0]['status'], 'unsupported_rights')
        self.dest.storage_supported = True
        plan = self.prepare(metadata_options={'favorites': 'omit', 'labels': 'preserve'})
        self.assertEqual(plan['items'][0]['metadata']['labels']['status'], 'unsupported')
        self.execute(plan)
        self.assertEqual(self.dest.create_calls, 0)
        self.assertEqual(self.execute(self.prepare())['status'], 'complete')

    def test_favorite_metadata_partial_never_rolls_back_recipe(self):
        self.source.favorite_state = True
        self.dest.favorite_set_mode = 'definite'
        plan = self.prepare(source_refs=[self.source.ref('0')], metadata_options={'favorites': 'preserve', 'labels': 'omit'})
        result = self.execute(plan)
        self.assertEqual(result['items'][0]['copy_status'], 'confirmed')
        self.assertEqual(result['items'][0]['metadata_status'], 'partial', result)
        self.assertIn('metadata not fully applied', result['items'][0]['message'])
        self.assertEqual(len(self.dest.docs), 1)
        self.execute(plan)
        self.assertEqual(self.dest.create_calls, 1)

    def test_duplicate_label_names_need_exact_mapping(self):
        def label(library, key):
            return {'library_id': library, 'library_label_ref': {'library_id': library, 'label_id': key}, 'name': 'Dinner', 'normalized_name': 'dinner'}
        self.source.labels_enabled = self.dest.labels_enabled = True
        self.source.associations = [label('source', 'src')]
        self.dest.labels = [label('destination', 'one'), label('destination', 'two')]
        options = {'favorites': 'omit', 'labels': 'preserve'}
        self.assertEqual(self.prepare(metadata_options=options)['items'][0]['metadata']['labels']['status'], 'conflict')
        options['label_mappings'] = [{'source': self.source.associations[0]['library_label_ref'], 'destination': self.dest.labels[1]['library_label_ref']}]
        plan = self.prepare(source_refs=[self.source.ref('0')], metadata_options=options)
        self.assertEqual(plan['items'][0]['metadata']['labels']['status'], 'preserve', plan)
        result = self.execute(plan)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.dest.label_writes, [self.dest.labels[1]['library_label_ref']])

    def test_builtin_destination_and_link_only_preserve_rights(self):
        doc = self.source.docs['0']
        doc.update(ingredients=[], steps=[], portions=None)
        doc['rights']['storage'] = 'link_only'; doc['source']['url'] = 'https://example.org/recipe'
        plan = self.prepare(source_refs=[self.source.ref('0')], destination_library_id='builtin')
        result = self.execute(plan)
        self.assertEqual(result['status'], 'complete', result)
        ref = result['items'][0]['destination_ref']
        saved = self.app.recipes.get(ref['recipe_id'])
        self.assertEqual(saved['rights']['storage'], 'link_only')
        self.assertEqual(saved['ingredients'], [])
        self.assertEqual(self.execute(plan)['items'][0]['destination_ref'], ref)

    def test_expiry_cleanup_pins_uncertain_and_retains_confirmed_mapping(self):
        plan = self.prepare(source_refs=[self.source.ref('0')])
        with mock.patch.object(migration, 'now', return_value=migration.now() + timedelta(hours=1)):
            self.assertEqual(self.execute(plan)['status'], 'needs_review')
        plan = self.prepare(source_refs=[self.source.ref('0')])
        self.dest.timeout = True; self.dest.reconcile_available = False
        self.execute(plan)
        with mock.patch.object(migration, 'now', return_value=migration.now() + timedelta(days=31)):
            self.assertEqual(self.execute(plan)['status'], 'uncertain')
            self.dest.reconcile_available = True
            self.assertEqual(self.execute(plan)['status'], 'complete')
            with self.assertRaises(RecipeError):
                migration.Migration(self.app).load(plan['plan_id'])
        self.assertEqual(self.prepare(source_refs=[self.source.ref('0')])['items'][0]['status'], 'already_mapped')

    def test_uncertain_origin_blocks_other_source_refs_despite_search_visibility(self):
        self.source.docs['1'] = deepcopy(self.source.docs['0'])
        plan = self.prepare()
        self.dest.timeout = True
        with mock.patch.object(self.dest, 'search', return_value={'recipes': [], 'cursor': None}):
            result = self.execute(plan)
            self.assertEqual(result['status'], 'uncertain')
            self.assertEqual([item['copy_status'] for item in result['items']], ['uncertain', 'needs_review'])
            self.assertEqual(self.dest.create_calls, 1)
            self.assertEqual(self.prepare(source_refs=[self.source.ref('1')])['items'][0]['status'], 'unavailable')

    def test_expiry_checked_after_reads_each_create_and_inside_metadata_dispatch(self):
        instant = [migration.now()]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant[0]
        plan = self.prepare(metadata_options={'favorites': 'preserve', 'labels': 'omit'})
        create = self.dest.create_from_snapshot
        def slow_create(snapshot, operation):
            result = create(snapshot, operation)
            instant[0] += timedelta(hours=1)
            return result
        with mock.patch.object(migration, 'now', side_effect=lambda: instant[0]), mock.patch.object(fixtures.recipe_module, 'datetime', Clock), mock.patch.object(self.dest, 'create_from_snapshot', slow_create):
            result = self.execute(plan)
        self.assertEqual(self.dest.create_calls, 1)
        self.assertEqual(result['items'][1]['copy_status'], 'needs_review')
        self.assertEqual(self.dest.favorite_write_calls, 0)

        instant[0] = migration.now()
        plan = self.prepare(source_refs=[self.source.ref('1')])
        get = self.source.get
        def slow_get(ref):
            result = get(ref)
            instant[0] += timedelta(hours=1)
            return result
        with mock.patch.object(migration, 'now', side_effect=lambda: instant[0]), mock.patch.object(self.source, 'get', slow_get):
            self.assertEqual(self.execute(plan)['status'], 'needs_review')
        self.assertEqual(self.dest.create_calls, 1)

        instant[0] = migration.now(); self.source.favorite_state = True
        plan = self.prepare(source_refs=[self.source.ref('1')], metadata_options={'favorites': 'preserve', 'labels': 'omit'})
        get_favorite = self.dest.get_favorite
        def slow_favorite(ref):
            result = get_favorite(ref)
            instant[0] += timedelta(hours=1)
            return result
        with mock.patch.object(migration, 'now', side_effect=lambda: instant[0]), mock.patch.object(fixtures.recipe_module, 'datetime', Clock), mock.patch.object(self.dest, 'get_favorite', slow_favorite):
            result = self.execute(plan)
        self.assertEqual(result['items'][0]['metadata_status'], 'partial')
        self.assertEqual(self.dest.favorite_write_calls, 0)

    def test_oversized_unicode_preview_rejected_before_persistence(self):
        self.source.docs = {str(i): normalize_recipe(fixtures.full_recipe(str(i), external_id=str(i))) for i in range(10)}
        self.source.versions = {key: 'v1' for key in self.source.docs}
        def label(library, index):
            return {'library_id': library, 'library_label_ref': {'library_id': library, 'label_id': str(index) + '界' * 290, 'version': '界' * 300}, 'name': '界' * 100, 'normalized_name': '界' * 100}
        self.source.labels_enabled = self.dest.labels_enabled = True
        self.source.associations = [label('source', i) for i in range(20)]
        self.dest.labels = [label('destination', i) for i in range(20)]
        mappings = [{'source': src['library_label_ref'], 'destination': dst['library_label_ref']} for src, dst in zip(self.source.associations, self.dest.labels)]
        with self.assertRaisesRegex(RecipeError, 'wire budget'):
            self.prepare(metadata_options={'favorites': 'omit', 'labels': 'preserve', 'label_mappings': mappings})
        with self.app.recipes._connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM migration_plans').fetchone()[0], 0)
        self.assertEqual(self.dest.create_calls, 0)

    def test_discovery_save_cannot_bypass_uncertain_or_confirmed_migration(self):
        self.dest.timeout = True
        plan = self.prepare(source_refs=[self.source.ref('0')])
        self.assertEqual(self.execute(plan)['status'], 'uncertain')
        discovery = self.app.recipes.persist_discovery(self.source.docs['0'])
        request = {'operation': 'recipes', 'action': 'save', 'library_id': 'destination', 'discovery_ref': discovery['discovery_ref'], 'idempotency_key': 'ordinary-save'}
        with self.assertRaisesRegex(RecipeError, 'exact source.*migration'):
            self.app.handle(request)
        self.assertEqual(self.dest.create_calls, 1)
        self.assertEqual(self.execute(plan)['status'], 'complete')
        with self.assertRaisesRegex(RecipeError, 'exact source.*migration'):
            self.app.handle(request)
        self.assertEqual(self.dest.create_calls, 1)

    def test_builtin_restart_after_recipe_commit_before_mapping(self):
        plan = self.prepare(source_refs=[self.source.ref('0')], destination_library_id='builtin')
        with mock.patch.object(migration.Migration, 'save_mapping', side_effect=SystemExit('crash after local commit')):
            with self.assertRaises(SystemExit): self.execute(plan)
        self.assertEqual(len(self.app.recipes.search('')), 1)
        self.assertEqual(self.execute(plan)['status'], 'complete')
        self.assertEqual(len(self.app.recipes.search('')), 1)

    def test_inspect_after_mapping_does_not_claim_pending_metadata_complete(self):
        plan = self.prepare(source_refs=[self.source.ref('0')], metadata_options={'favorites': 'preserve', 'labels': 'omit'})
        with mock.patch.object(migration.Migration, 'apply_metadata', side_effect=SystemExit('crash before metadata')):
            with self.assertRaises(SystemExit): self.execute(plan)
        inspected = self.app.handle({'operation': 'migration', 'action': 'inspect', 'plan_id': plan['plan_id']})
        self.assertEqual(inspected['status'], 'partial')
        self.assertEqual(inspected['items'][0]['metadata_status'], 'pending')
        self.assertEqual(self.execute(plan)['status'], 'complete')

    def test_builtin_favorite_restart_after_write_before_stage_progress(self):
        self.source.favorite_state = True
        plan = self.prepare(source_refs=[self.source.ref('0')], destination_library_id='builtin', metadata_options={'favorites': 'preserve', 'labels': 'omit'})
        original = migration.Migration.update
        def stop(engine, plan, item):
            if item['progress'].get('metadata_stages', {}).get('0', {}).get('status') == 'confirmed':
                raise SystemExit('crash after favorite write')
            return original(engine, plan, item)
        with mock.patch.object(migration.Migration, 'update', stop):
            with self.assertRaises(SystemExit): self.execute(plan)
        result = self.execute(plan)
        self.assertEqual(result['status'], 'complete', result)
        recipe = self.app.recipes.get(result['items'][0]['destination_ref']['recipe_id'])
        self.assertTrue(recipe['is_favorite'])
        self.assertEqual(recipe['favorite_revision'], 1)

    def test_installed_adapters_validate_native_storage_before_any_write(self):
        engine = migration.Migration(self.app)
        for adapter_class in (fixtures.MealieAdapter, fixtures.RecipeSageAdapter):
            adapter = object.__new__(adapter_class)
            adapter.library_id = 'destination'
            self.app.recipe_library_adapters['destination'] = adapter
            doc = deepcopy(self.source.docs['0'])
            self.assertTrue(engine.storage_supported(doc, 'destination'))
            doc.update(ingredients=[], steps=[], portions=None)
            doc['rights']['storage'] = 'link_only'
            doc['source']['url'] = 'https://example.org/recipe'
            # Native link storage drops source ID/tags: preview must reject
            # this exact document rather than discover loss after dispatch.
            self.assertFalse(engine.storage_supported(doc, 'destination'))

    def test_v4_backup_atomic_failure_retry_and_unknown_schema(self):
        path = Path(self.temp.name) / 'old' / 'recipes.sqlite3'; path.parent.mkdir()
        fixtures.create_v3_bank(path, fixtures.full_recipe('v4', external_id='v4'))
        with closing(sqlite3.connect(path)) as connection:
            RecipeStore(path, 'Hus A')._migrate_v3_to_v4(connection); connection.commit()
        store = RecipeStore(path, 'Hus A')
        original = RecipeStore._migrate_v4_to_v5
        def fail(self, connection):
            original(self, connection)
            raise RecipeError('test rollback')
        with mock.patch.object(RecipeStore, '_migrate_v4_to_v5', fail):
            with self.assertRaisesRegex(RecipeError, 'rollback'): store.search('')
        backup = path.with_name('recipes-v4.backup.sqlite3')
        before = (backup.stat().st_ino, backup.read_bytes())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], '4')
            self.assertFalse(connection.execute("SELECT name FROM sqlite_master WHERE name='migration_plans'").fetchone())
        self.assertEqual(len(store.search('')), 1)
        self.assertEqual((backup.stat().st_ino, backup.read_bytes()), before)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], '6')
            connection.execute("UPDATE metadata SET value='7' WHERE key='schema_version'"); connection.commit()
        with self.assertRaisesRegex(RecipeError, 'newer'): store.search('')


class NativeRetirementTests(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import urlsplit, parse_qs
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.native = None
        self.cover = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                owner.calls.append((self.command, parsed.path, self.headers.get('Authorization')))
                value, content_type = owner.response(parsed.path, parse_qs(parsed.query))
                data = json.dumps(value).encode() if content_type == 'application/json' else value
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.path == '/compat/v2/recipes/getRecipes':
                    self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    return self.do_GET()
                owner.calls.append(('POST', self.path, None))
                self.send_error(405)

            do_PUT = do_PATCH = do_DELETE = do_POST

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.config = {**fixtures.CONFIG, 'primary_recipe_library_id': 'builtin'}
        self.state = StateStore(self.root / 'state', self.config)
        self.app = Application(self.state, fixtures.FakeOda(), fixtures.FakeBrowser())

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def response(self, path, query):
        raw = self.native
        if self.provider == 'mealie':
            mapping = {'/api/app/about': 'app_about', '/api/users/self': 'authenticated_user',
                       '/api/users/self/favorites': 'favorites', '/api/organizers/tags': 'tag_page'}
            if path in mapping:
                return raw[mapping[path]], 'application/json'
            if path == '/api/recipes':
                page = deepcopy(raw['recipe_page'])
                page['perPage'] = int(query['perPage'][0])
                return page, 'application/json'
            if path.startswith('/api/users/self/ratings/'):
                return raw['favorite_exact'], 'application/json'
            if path == '/api/recipes/' + raw['recipe_get']['id']:
                return raw['recipe_get'], 'application/json'
            if path.endswith('/images/original.webp'):
                return self.cover or b'', 'image/png'
        else:
            mapping = {'/openapi.json': 'openapi', '/compat/v2/users/getMe': 'authenticated_user',
                       '/compat/v2/users/validateSession': 'validate_session', '/compat/v2/recipes/getRecipes': 'recipe_page',
                       '/compat/v2/recipes/getRecipe': 'recipe_get', '/compat/v2/labels/getLabels': 'labels'}
            if path in mapping:
                return raw[mapping[path]], 'application/json'
        raise AssertionError('unexpected synthetic native read ' + path)

    def configure(self, provider='mealie'):
        self.provider = provider
        version = 'v3.24.0.json' if provider == 'mealie' else 'v4.0.6.json'
        self.native = json.loads((Path(__file__).parent / 'fixtures' / provider / version).read_text())
        self.connection = {'library_id': 'native-' + provider, 'provider': provider,
                           'base_url': self.origin, 'read_only': True}
        self.adapter = (fixtures.MealieAdapter if provider == 'mealie' else fixtures.RecipeSageAdapter)(
            self.connection, {'token': 'SYNTHETIC_RETIREMENT_TOKEN'})
        self.app.recipe_libraries[self.adapter.library_id] = self.connection
        self.app.recipe_library_adapters[self.adapter.library_id] = self.adapter
        self.reference = self.adapter._reference(self.native['recipe_get'])
        return self.reference

    def prepare(self, **options):
        return self.app.handle({'operation': 'migration', 'action': 'prepare',
            'source_library_id': self.adapter.library_id, 'destination_library_id': 'builtin',
            'source_refs': [self.reference], 'metadata_options': {'favorites': 'omit', 'labels': 'omit', **options}})

    def execute(self, plan):
        return self.app.handle({'operation': 'migration', 'action': 'execute', 'plan_id': plan['plan_id'],
            'confirmation': {'plan_digest': plan['plan_digest'], 'statement': plan['confirmation_statement']}})

    def test_actual_native_readers_copy_without_sidecars_and_keep_account_identity(self):
        for provider in ('mealie', 'recipesage'):
            with self.subTest(provider=provider):
                self.configure(provider)
                original = deepcopy(self.native)
                if provider == 'mealie':
                    self.native['recipe_get']['extras'] = {'hermes': {'source_provider': 'mathem', 'portions': 999}}
                else:
                    from recipe_library_recipesage import METADATA_BEGIN, METADATA_END
                    self.native['recipe_get']['notes'] = METADATA_BEGIN + '\n' + json.dumps({'source_provider': 'mathem', 'portions': 999}) + '\n' + METADATA_END + '\n\nActual native note.'
                before = deepcopy(self.native)
                plan = self.prepare()
                self.assertEqual(plan['items'][0]['status'], 'create', plan)
                result = self.execute(plan)
                self.assertEqual(result['status'], 'complete', result)
                saved = self.app.recipes.get(result['items'][0]['destination_ref']['recipe_id'])
                self.assertNotEqual(saved['portions'], 999)
                self.assertIsNone(saved['source_provider'])
                self.assertEqual(saved['schema_version'], 2)
                self.assertEqual(self.native, before)
                frozen = migration.Migration(self.app).load(plan['plan_id'])['items'][0]['frozen']
                identity = frozen['source_context']['identity']
                self.assertTrue(identity.startswith('import:v1:'))
                self.assertEqual(self.app.recipes.source_entry(identity)['id'], saved['id'])
                self.assertEqual(self.prepare()['items'][0]['status'], 'already_mapped')
                self.assertNotIn('SYNTHETIC_RETIREMENT_TOKEN', json.dumps(result))
                self.assertNotIn(original['authenticated_user']['id'], json.dumps(result))
        self.assertTrue(all(method == 'GET' or method == 'POST' and path == '/compat/v2/recipes/getRecipes' for method, path, _ in self.calls))

    def test_native_binding_version_and_favorite_drift_block_copy(self):
        for change in ('binding', 'version', 'favorite'):
            with self.subTest(change=change):
                self.configure()
                plan = self.prepare(favorites='preserve')
                self.assertEqual(plan['items'][0]['status'], 'create', plan)
                if change == 'binding':
                    self.native['authenticated_user']['householdId'] = '99999999-9999-4999-8999-999999999999'
                elif change == 'version':
                    self.native['recipe_get']['updatedAt'] = '2026-09-07T01:00:00Z'
                else:
                    self.native['favorite_exact']['isFavorite'] = not self.native['favorite_exact']['isFavorite']
                result = self.execute(plan)
                self.assertEqual(result['status'], 'needs_review', result)
                self.assertEqual(self.app.recipes.search(''), [])

    def test_observed_favorite_label_wording_and_explicit_managed_cover(self):
        from io import BytesIO
        from PIL import Image
        self.configure()
        raw = BytesIO()
        Image.new('RGB', (8, 8), 'blue').save(raw, format='PNG')
        self.cover = raw.getvalue()
        self.native['recipe_get']['image'] = 'native-cover-version'
        omitted = self.prepare()
        self.assertEqual(omitted['items'][0]['metadata']['native_fields']['cover']['status'], 'omitted')
        self.assertFalse(any('/images/' in path for _, path, _ in self.calls))
        self.assertTrue(self.prepare(labels='preserve')['items'][0]['metadata_blocked'])
        plan = self.prepare(favorites='preserve', cover='preserve')
        self.assertEqual(plan['items'][0]['status'], 'create', plan)
        details = plan['items'][0]['metadata']['native_fields']
        self.assertEqual(details['labels']['reference_status'], 'unsupported')
        result = self.execute(plan)
        self.assertEqual(result['status'], 'complete', result)
        saved = self.app.recipes.get(result['items'][0]['destination_ref']['recipe_id'])
        self.assertTrue(saved['is_favorite'])
        self.assertEqual(saved['tags'], details['labels']['names'])
        self.assertEqual(saved['image']['asset_id'], details['cover']['asset_id'])
        self.assertIsNone(saved['image']['license'])
        self.assertEqual(saved['image']['source_url'], details['cover']['source_url'])
        self.assertTrue(saved['image']['source_url'].startswith(self.origin + '/api/media/recipes/'))
        self.assertTrue(self.app.recipes.assets.read(saved['image']['asset_id']).startswith(b'\xff\xd8'))
        self.assertTrue(all(auth is None for _, path, auth in self.calls if '/images/' in path))
        self.assertTrue(all(method == 'GET' or method == 'POST' and path == '/compat/v2/recipes/getRecipes' for method, path, _ in self.calls))

    def test_same_native_id_in_another_account_stays_distinct_and_local_edits_conflict(self):
        self.configure()
        first = self.execute(self.prepare())
        first_id = first['items'][0]['destination_ref']['recipe_id']
        self.native['authenticated_user']['householdId'] = '99999999-9999-4999-8999-999999999999'
        second = self.execute(self.prepare())
        second_id = second['items'][0]['destination_ref']['recipe_id']
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(self.app.recipes.search('')), 2)
        recipe = self.app.recipes.get(second_id)
        recipe['name'] = 'Local correction'
        self.app.recipes.update(second_id, recipe['revision'], recipe)
        self.assertEqual(self.prepare()['items'][0]['status'], 'conflict')
        self.assertEqual(self.app.recipes.get(second_id)['name'], 'Local correction')

    def test_cover_limit_is_rejected_and_not_imported(self):
        self.configure()
        self.native['recipe_get']['image'] = 'native-cover-version'
        self.cover = b'x' * (1024 * 1024 + 1)
        plan = self.prepare(cover='preserve')
        self.assertEqual(plan['items'][0]['status'], 'unavailable')
        self.assertEqual(self.app.recipes.search(''), [])
        self.assertFalse(list(self.state.directory.rglob('*.jpg')))
        with self.assertRaises(RecipeError): self.prepare(cover=[])

    def test_native_restart_after_commit_and_legacy_mapping_binding_conflict(self):
        self.configure()
        plan = self.prepare()
        with mock.patch.object(migration.Migration, 'save_mapping', side_effect=SystemExit('synthetic interruption')):
            with self.assertRaises(SystemExit): self.execute(plan)
        self.assertEqual(len(self.app.recipes.search('')), 1)
        self.assertEqual(self.execute(plan)['status'], 'complete')
        self.assertEqual(len(self.app.recipes.search('')), 1)
        # A legacy raw-ID mapping has no evidence of its source account binding.
        with self.app.recipes._connection() as connection:
            connection.execute('UPDATE migration_mappings SET source_id=?', (self.reference['recipe_id'],))
        plan = self.prepare()
        self.assertEqual(plan['items'][0]['reason'], 'legacy_mapping_binding_unproven')
        self.assertEqual(self.execute(plan)['items'][0]['copy_status'], 'skipped')
        self.assertEqual(len(self.app.recipes.search('')), 1)


class RetirementSetupTests(unittest.TestCase):
    def setUp(self):
        import recipe_library_setup as setup
        self.setup = setup
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'installation'
        self.home.mkdir()
        self.state = self.root / 'adopted-state'
        self.state.mkdir()
        self.config_path = self.root / 'config.json'
        self.config = {**fixtures.CONFIG, 'primary_recipe_library_id': 'source', 'recipe_libraries': [
            {'library_id': 'source', 'provider': 'mealie', 'base_url': 'https://synthetic.example', 'read_only': False}]}
        self.config_path.write_text(json.dumps(self.config))
        self.meta = {'format': 1, 'home': str(self.home), 'manager': 'launchd', 'name': 'synthetic-retirement',
            'paths': {'config': str(self.config_path), 'state': str(self.state),
                      'browser_profile': str(self.home / 'profile'), 'browser_home': str(self.home / 'browser'),
                      'browser_socket_directory': str(self.home / 'browser-run'), 'socket': str(self.home / 'run/service.sock')}}
        (self.home / 'runtime.json').write_text(json.dumps(self.meta))
        self.args = self.setup.parser().parse_args(['--config', str(self.config_path), '--home', str(self.home), 'retire-external'])

    def test_stopped_retirement_preserves_database_credentials_and_optional_independence(self):
        import install
        bank = RecipeStore(self.state / 'recipes.sqlite3', self.config['household'])
        saved = bank.save(fixtures.full_recipe())
        discovery = bank.persist_discovery(fixtures.full_recipe('Uncertain source', external_id='uncertain'))
        operation = bank.begin_library_create(discovery['discovery_ref'], 'source', status='active', idempotency_key='retained-uncertain')
        bank.claim_library_dispatch(operation['operation_id'])
        bank.finish_library_create(operation['operation_id'], 'uncertain')
        source = Library('source', [fixtures.full_recipe('Copied', external_id='copied'), fixtures.full_recipe('Not copied', external_id='not-copied')])
        app = Application(StateStore(self.state, self.config), fixtures.FakeOda(), fixtures.FakeBrowser(), recipe_library_adapters={'source': source})
        engine = migration.Migration(app)
        request = {'source_library_id': 'source', 'destination_library_id': 'builtin',
                   'metadata_options': {'favorites': 'omit', 'labels': 'omit'}}
        complete = engine.prepare({**request, 'source_refs': [source.ref('0')]})
        engine.handle({'action': 'execute', 'plan_id': complete['plan_id'],
                       'confirmation': {'plan_digest': complete['plan_digest'], 'statement': complete['confirmation_statement']}})
        engine.prepare({**request, 'source_refs': [source.ref('1')]})
        pending = bank.persist_discovery(fixtures.full_recipe('Pending source', external_id='pending'))
        bank.begin_library_create(pending['discovery_ref'], 'source', status='active', idempotency_key='retained-pending')
        before = bank.path.read_bytes()
        secret = self.home / 'recipe-libraries' / 'source.json'
        secret.parent.mkdir()
        secret.write_text('{"token":"SYNTHETIC_RETAINED_SECRET"}')
        with mock.patch.object(install, 'active', return_value=False), mock.patch.object(self.setup, '_confirm', side_effect=AssertionError('no redundant confirmation')), \
                mock.patch.object(self.setup, '_probe', side_effect=AssertionError('retirement must not contact source')), \
                mock.patch.object(self.setup, '_restart_after_change', side_effect=AssertionError('retirement must not restart')):
            result = self.setup.retire_external(self.args, self.config)
            self.assertTrue(result['changed'])
            replay = self.setup.retire_external(self.args, self.config)
            self.assertFalse(replay['changed'])
            self.assertEqual(result['retained_obligations'], replay['retained_obligations'])
        self.assertEqual(bank.path.read_bytes(), before)
        self.assertEqual(result['retained_obligations']['libraries']['source'], {
            'pending_operations': 1, 'uncertain_operations': 1, 'unfinished_migration_plans': 1, 'migration_mappings': 1})
        self.assertFalse(result['migration_complete'])
        self.assertIn('SYNTHETIC_RETAINED_SECRET', secret.read_text())
        updated = json.loads(self.config_path.read_text())
        self.assertEqual(updated['primary_recipe_library_id'], 'builtin')
        self.assertTrue(next(c for c in updated['recipe_libraries'] if c['library_id'] == 'source')['read_only'])
        self.assertEqual(updated['household'], self.config['household'])
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        app = Application(StateStore(self.state, updated), fixtures.FakeOda(), fixtures.FakeBrowser())
        response = app.handle({'operation': 'recipes', 'action': 'save', 'recipe': fixtures.full_recipe('New builtin', external_id='new')})
        self.assertEqual(response['library_id'], 'builtin')
        self.assertEqual(bank.get(saved['id'])['id'], saved['id'])
        self.assertNotIn('SYNTHETIC_RETAINED_SECRET', json.dumps(result))

    @unittest.skipUnless(__import__('sys').platform == 'darwin', 'actual launchd inactive-owner probe requires macOS')
    def test_real_retirement_cli_uses_native_inactive_probe_and_does_not_restart(self):
        import subprocess
        import sys
        self.meta['name'] = 'synthetic-retirement-' + self.root.name
        (self.home / 'runtime.json').write_text(json.dumps(self.meta))
        completed = subprocess.run([sys.executable, '-B', str(Path(self.setup.__file__)),
            '--config', str(self.config_path), '--home', str(self.home), 'retire-external'],
            input='', text=True, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(self.config_path.read_text())['primary_recipe_library_id'], 'builtin')
        result = json.loads(completed.stdout)
        self.assertFalse(result['service_started'])
        self.assertFalse(result['migration_complete'])
        self.assertEqual(result['retained_obligations']['bank_status'], 'absent')
        self.assertTrue((self.state / '.service-owner.lock').is_file())

    def test_unavailable_local_inventory_fails_before_configuration_mutation(self):
        import install
        before = self.config_path.read_bytes()
        bank = self.state / 'recipes.sqlite3'
        bank.write_bytes(b'not a bank')
        with mock.patch.object(install, 'active', return_value=False):
            with self.assertRaisesRegex(RecipeLibraryError, 'inventory'):
                self.setup.retire_external(self.args, self.config)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(bank.read_bytes(), b'not a bank')

    def test_inventory_rejects_fifo_dangling_alias_and_aliased_recovery_journal(self):
        import os
        import install
        before = self.config_path.read_bytes()
        database = self.state / 'recipes.sqlite3'
        os.mkfifo(database)
        with mock.patch.object(install, 'active', return_value=False):
            with self.assertRaisesRegex(RecipeLibraryError, 'regular recipe bank'):
                self.setup.retire_external(self.args, self.config)
        database.unlink()
        target = self.root / 'aliased-bank.sqlite3'
        database.symlink_to(target)
        with mock.patch.object(install, 'active', return_value=False):
            with self.assertRaisesRegex(RecipeLibraryError, 'inventory'):
                self.setup.retire_external(self.args, self.config)
        RecipeStore(target, self.config['household']).save(fixtures.full_recipe())
        recovery = Path(str(target) + '-journal')
        recovery.write_bytes(b'synthetic unfinished journal')
        with mock.patch.object(install, 'active', return_value=False):
            with self.assertRaisesRegex(RecipeLibraryError, 'idle recipe bank'):
                self.setup.retire_external(self.args, self.config)
        self.assertEqual(recovery.read_bytes(), b'synthetic unfinished journal')
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_declared_schema_requires_its_obligation_tables(self):
        import install
        database = self.state / 'recipes.sqlite3'
        before = self.config_path.read_bytes()
        for table in ('library_operations', 'migration_plans', 'migration_items', 'migration_mappings'):
            with self.subTest(table=table):
                database.unlink(missing_ok=True)
                RecipeStore(database, self.config['household']).save(fixtures.full_recipe())
                with sqlite3.connect(database) as connection:
                    connection.execute('DROP TABLE ' + table)
                untouched = database.read_bytes()
                with mock.patch.object(install, 'active', return_value=False):
                    with self.assertRaisesRegex(RecipeLibraryError, 'inventory'):
                        self.setup.retire_external(self.args, self.config)
                self.assertEqual(database.read_bytes(), untouched)
                self.assertEqual(self.config_path.read_bytes(), before)

    def test_retirement_resolves_config_alias_and_retains_the_alias(self):
        import install
        alias = self.root / 'config-alias.json'
        alias.symlink_to(self.config_path)
        self.args.config = alias
        with mock.patch.object(install, 'active', return_value=False), mock.patch.object(self.setup, '_confirm'):
            self.setup.retire_external(self.args, self.config)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(json.loads(self.config_path.read_text())['primary_recipe_library_id'], 'builtin')

    def test_active_ownership_and_manifest_mismatch_fail_without_config_change(self):
        import install
        from runtime_ownership import file_lock
        before = self.config_path.read_bytes()
        with mock.patch.object(install, 'active', return_value=True), mock.patch.object(self.setup, '_confirm'):
            with self.assertRaisesRegex(RecipeLibraryError, 'stopped installation'):
                self.setup.retire_external(self.args, self.config)
        with file_lock(self.home / '.installer.lock'), mock.patch.object(install, 'active', return_value=False):
            with self.assertRaisesRegex(RecipeLibraryError, 'ownership locks'):
                self.setup.retire_external(self.args, self.config)
        self.meta['paths']['config'] = str(self.root / 'other.json')
        (self.home / 'runtime.json').write_text(json.dumps(self.meta))
        with self.assertRaisesRegex(RecipeLibraryError, 'matching runtime.json'):
            self.setup.retire_external(self.args, self.config)
        self.assertEqual(self.config_path.read_bytes(), before)
