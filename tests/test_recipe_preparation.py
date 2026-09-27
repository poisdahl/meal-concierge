"""Synthetic online preparation/offline application and publisher boundaries."""
import hashlib
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
import install
from recipe_portable import canonical_bytes, write_archive
from recipes import normalize_recipe, RecipeStore

spec = importlib.util.spec_from_file_location('publish_recipe_channel', CORE / 'recipe_channel_publish.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'; self.home.mkdir()
        self.release = self.root / 'release'
        (self.release / 'venv/bin').mkdir(parents=True)
        (self.release / 'venv/bin/python').symlink_to(sys.executable)
        for path in CORE.glob('*.py'):
            (self.release / path.name).symlink_to(path)
        self.config = {'household': 'Synthetic preparation', 'provider': 'meny', 'instance': 'preparation'}
        (self.home / 'config.json').write_text(json.dumps(self.config))
        self.meta = {'manager': 'external', 'release': str(self.release), 'paths': {
            'config': str(self.home / 'config.json'), 'state': str(self.home / 'state'),
            'socket': str(self.home / 'run/service.sock'), 'browser_profile': str(self.home / 'browser/profile'),
            'browser_home': str(self.home / 'browser'), 'browser_socket_directory': str(self.home / 'browser/run')}}
        install.write_json(self.home / 'runtime.json', self.meta)

    def pack(self, version='1'):
        recipe = normalize_recipe({'schema_version': 2, 'name': 'Synthetic carrots ' + version, 'language': 'en', 'portions': 2,
            'ingredients': [{'raw': '200 g carrots', 'quantity': 200, 'unit': 'g', 'item': 'carrots', 'scalable': True}],
            'steps': ['Cook the carrots.'], 'source': {'kind': 'user', 'relationship': 'user_supplied'},
            'rights': {'storage': 'full', 'credit': 'Synthetic test'}})
        manifest = {**install.RECIPE_PACK, 'pack_version': version, 'records_count': 1,
                    'membership_mode': 'authoritative'}
        records = self.root / ('records-' + version + '.jsonl')
        records.write_bytes(canonical_bytes({'recipe_id': 'sample', 'status': 'draft', 'recipe': recipe}) + b'\n')
        archive = self.root / ('pack-' + version + '.zip')
        write_archive(archive, manifest, {'records.jsonl': records})
        expected = {**install.RECIPE_PACK, 'pack_version': version, 'display_name': install.RECIPE_COLLECTION_NAME,
            'url': f'https://github.com/poisdahl/meal-concierge/releases/download/recipes-{version}/meal-concierge-recipes-{version}.zip',
            'bytes': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
        return archive, expected

    def prepare(self, archive, expected):
        with patch.object(install, 'latest_recipe_pack', return_value=expected):
            return install.prepare_recipes(self.home, self.meta, archive)

    def apply(self, identifier):
        argv = ['install.py', 'import-recipes', '--home', str(self.home), '--prepared', identifier]
        with patch.object(sys, 'argv', argv), patch.object(install.urllib.request, 'urlopen', side_effect=AssertionError('offline apply made a request')):
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                install.main()
        return json.loads(output.getvalue().split(': ', 1)[1])

    def test_prepare_with_live_ownership_then_offline_apply_and_exact_retry(self):
        archive, expected = self.pack()
        # Real ownership locks are held as they would be by a running service.
        with install.data_ownership(self.meta):
            result = self.prepare(archive, expected)
            self.assertFalse((self.home / 'state/recipes.sqlite3').exists())
            with self.assertRaises(RuntimeError):
                self.apply(result['prepared'])
        report = self.apply(result['prepared'])
        self.assertEqual((report['status'], report['created']), ('complete', 1))
        self.assertEqual(self.apply(result['prepared'])['unchanged'], 1)
        self.assertEqual(len(RecipeStore(self.home / 'state/recipes.sqlite3', self.config['household']).search()), 1)

    def test_tampered_archive_receipt_and_runtime_rejected(self):
        archive, expected = self.pack(); result = self.prepare(archive, expected)
        directory = self.home / 'recipe-preparations' / result['prepared']
        target = directory / 'archive.zip'; target.chmod(0o600); target.write_bytes(b'tampered')
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            self.apply(result['prepared'])
        self.assertIsNone(install.collection_generation(self.meta))
        self.prepare(archive, expected)
        receipt = json.loads((directory / 'receipt.json').read_text())
        for key, value in [('sha256', 'a' * 64), ('url', 'https://example.com/untrusted.zip')]:
            altered = json.loads(json.dumps(receipt)); altered['expected'][key] = value
            install.write_json(directory / 'receipt.json', altered)
            with self.assertRaises(RuntimeError):
                self.apply(result['prepared'])
        install.write_json(directory / 'receipt.json', receipt)
        with self.assertRaisesRegex(RuntimeError, 'different runtime'):
            install.read_preparation(self.home, {**self.meta, 'release': '/other/runtime'}, result['prepared'])

    def test_intervening_import_and_removal_reject_old_preparation(self):
        old_archive, old = self.pack('1'); prepared = self.prepare(old_archive, old)
        new_archive, new = self.pack('2'); newer = self.prepare(new_archive, new)
        self.apply(newer['prepared'])
        with self.assertRaisesRegex(RuntimeError, 'recipe pack apply failed'):
            self.apply(prepared['prepared'])
        with patch('sys.stdout', new_callable=io.StringIO):
            install.remove_recipe_collection(self.meta)
        with self.assertRaisesRegex(RuntimeError, 'recipe pack apply failed'):
            self.apply(newer['prepared'])

    def test_interrupted_attempt_can_retry_exact_preparation(self):
        archive, expected = self.pack(); result = self.prepare(archive, expected)
        staged, _, receipt = install.read_preparation(self.home, self.meta, result['prepared'])
        with patch('recipe_portable.apply_archive', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                install.apply_recipe_pack(staged, self.meta, expected, preparation=receipt)
        self.assertEqual(install.collection_generation(self.meta)['sha256'], expected['sha256'])
        self.assertEqual(self.apply(result['prepared'])['created'], 1)

    def test_preparation_cache_reused_without_archive_download(self):
        archive, expected = self.pack(); self.prepare(archive, expected)
        with patch.object(install, 'latest_recipe_pack', return_value=expected), \
             patch.object(install.urllib.request, 'urlopen', side_effect=AssertionError('unexpected archive request')):
            self.assertEqual(install.prepare_recipes(self.home, self.meta)['prepared'], expected['sha256'])

    def test_old_installed_runtime_fails_before_receipt_or_state_write(self):
        archive, expected = self.pack()
        old = (CORE / 'install.py').read_text().replace('RECIPE_PREPARATION_VERSION = 1', 'RECIPE_PREPARATION_VERSION = 0')
        (self.release / 'install.py').unlink()
        (self.release / 'install.py').write_text(old)
        with self.assertRaisesRegex(RuntimeError, 'update the installed Meal Concierge runtime'):
            self.prepare(archive, expected)
        self.assertFalse(list((self.home / 'recipe-preparations').glob('*/receipt.json')))
        self.assertFalse((self.home / 'state').exists())

    def test_descriptor_uses_no_rest_api_and_rejects_invalid_latest(self):
        _, expected = self.pack()
        with patch.object(install.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(expected).encode())) as request:
            self.assertEqual(install.latest_recipe_pack(), expected)
        self.assertEqual(request.call_args.args[0].full_url, install.RECIPE_CHANNEL_URL)
        for value in ([expected], {**expected, 'bytes': True}, {**expected, 'pack_id': 'untrusted'},
                      {**expected, 'pack_version': '../escape'}, {**expected, 'normalizer_version': '999'}):
            with patch.object(install.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(value).encode())):
                with self.assertRaises(RuntimeError): install.latest_recipe_pack()

    def test_rate_limit_report_is_immediate_and_does_not_retry(self):
        error = urllib.error.HTTPError(install.RECIPE_CHANNEL_URL, 429, 'rate limited',
            {'Retry-After': '60', 'X-RateLimit-Reset': '1790000000'}, None)
        with patch.object(install.urllib.request, 'urlopen', side_effect=error) as request, \
             patch.object(install.time, 'sleep', side_effect=AssertionError('must not sleep')):
            with self.assertRaisesRegex(RuntimeError, 'Retry after 60.*Rate limit resets.*No automatic retry'):
                install.prepare_recipes(self.home, self.meta)
        request.assert_called_once()
        self.assertFalse((self.home / 'recipe-preparations').exists())

    def test_publisher_never_changes_pointer_before_archive_verification(self):
        archive, expected = self.pack()
        with patch.object(publisher, 'published_recipe_pack', return_value=expected), \
             patch.object(publisher, 'stage_recipe_pack', return_value=archive), \
             patch.object(publisher, 'preflight_archive', side_effect=RuntimeError('invalid artifact')), \
             patch.object(publisher, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, 'invalid artifact'):
                publisher.publish('synthetic-token')
            api.assert_not_called()

    def test_publisher_refuses_collection_changed_during_verification(self):
        archive, expected = self.pack()
        with patch.object(publisher, 'published_recipe_pack', side_effect=[expected, {**expected, 'sha256': 'a' * 64}]), \
             patch.object(publisher, 'stage_recipe_pack', return_value=archive), patch.object(publisher, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, 'changed during verification'):
                publisher.publish('synthetic-token')
            api.assert_not_called()

    def test_publisher_initializes_channel_after_real_archive_preflight(self):
        archive, expected = self.pack()
        payload = (json.dumps(expected, indent=2, sort_keys=True) + '\n').encode()
        with patch.object(publisher, 'published_recipe_pack', return_value=expected), \
             patch.object(publisher, 'stage_recipe_pack', return_value=archive), \
             patch.object(publisher, 'api', side_effect=[None, {'object': {'sha': 'reviewed-main'}}, {}, None, {},
                 {'content': base64.b64encode(payload).decode()}]) as api:
            publisher.publish('synthetic-token')
        self.assertEqual(api.call_args_list[2].kwargs['data'], {'ref': 'refs/heads/recipe-channel', 'sha': 'reviewed-main'})
        put = api.call_args_list[4]
        self.assertEqual(put.kwargs['method'], 'PUT')
        self.assertEqual(json.loads(base64.b64decode(put.kwargs['data']['content'])), expected)
