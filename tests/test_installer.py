"""Offline boundary tests; --native ROOT NAME ADAPTER CHROME exercises the real manager.

Native mode uses only a fresh explicitly named scratch installation. It does not
log in, send, order, register an agent, or touch an existing installation.
"""
from __future__ import annotations
import asyncio
import hashlib
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
import install
from runtime_ownership import ownership, listener_ownership, assert_no_legacy_service, file_lock
from core import StateStore
from recipes import RecipeStore

CONFIG = {'household': 'MC03 synthetic', 'provider': 'meny', 'instance': 'mc03'}
RECIPE = {'name': 'Synthetic carrots', 'language': 'en', 'portions': 2,
          'ingredients': [{'raw': '200 g carrots', 'quantity': 200, 'unit': 'g', 'item': 'carrots', 'scalable': True}],
          'steps': ['Cook the carrots.'], 'source': {'kind': 'user', 'relationship': 'user_supplied'},
          'rights': {'storage': 'full', 'credit': 'Synthetic test'}}


def command(*args, success=True, **kwargs):
    result = subprocess.run([str(x) for x in args], text=True, capture_output=True, **kwargs)
    if success and result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result


def installer(*args, success=True, cwd=None):
    return command(sys.executable, CORE / 'install.py', *args, success=success, cwd=cwd)


@contextmanager
def service(root, *, state=None, profile=None, sock=None):
    config = root / 'config.json'
    if not config.exists():
        config.write_text(json.dumps(CONFIG))
    args = [sys.executable, str(CORE / 'service.py'), '--config', str(config), '--state', str(state or root / 'state'), '--socket', str(sock or root / 'run/service.sock'), '--browser-profile', str(profile or root / 'browser/profile'), '--browser-home', str(root / 'browser'), '--browser-socket-directory', str(root / 'browser/run')]
    with (root / 'service.log').open('w') as log:
        process = subprocess.Popen(args, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while not (sock or root / 'run/service.sock').exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError((root / 'service.log').read_text())
                time.sleep(.02)
            yield process, args
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def raw_rpc(path, request):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(json.dumps(request).encode() + b'\n')
        data = b''
        while b'\n' not in data:
            data += client.recv(65536)
        return json.loads(data)


def create_v1_bank(path: Path, recipe: dict) -> None:
    document = __import__("recipes").normalize_recipe(recipe)
    serialized = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    timestamp = "2026-09-01T12:00:00+00:00"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE recipes (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
                name TEXT NOT NULL, search_text TEXT NOT NULL, source_key TEXT,
                content_fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL,
                document TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE revisions (
                recipe_id TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
                document TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (recipe_id, revision));
            CREATE TABLE idempotency (
                key TEXT PRIMARY KEY, operation TEXT NOT NULL, request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (("household", CONFIG["household"]), ("schema_version", "1")),
        )
        connection.execute(
            "INSERT INTO recipes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "rec_v1", 1, "active", document["name"], document["name"].casefold(),
                "familien:migrated-v1", "fingerprint", "content-hash", serialized,
                timestamp, timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?)",
            ("rec_v1", 1, "active", serialized, timestamp),
        )
        connection.execute(
            "INSERT INTO idempotency VALUES(?,?,?,?,?)",
            ("v1-key", "save", "request-hash", '{"id":"rec_v1"}', timestamp),
        )
        connection.commit()


class InstallerTests(unittest.TestCase):
    def test_restart_waits_for_retiring_supervisor_and_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            meta = {'manager': 'launchd', 'name': 'mc03-test', 'home': directory,
                    'unit': str(Path(directory) / 'service.plist')}
            for lock_busy in (False, True):
                with self.subTest(lock_busy=lock_busy):
                    statuses = iter([True, True, False, *([False] if lock_busy else []), False])
                    events = []
                    ownership_attempts = 0

                    def active(_):
                        value = next(statuses)
                        events.append(('active', value))
                        return value

                    @contextmanager
                    def owner(_):
                        nonlocal ownership_attempts
                        self.assertEqual(events[-1], ('active', False))
                        ownership_attempts += 1
                        if lock_busy and ownership_attempts == 1:
                            raise RuntimeError('target already owned')
                        events.append(('ownership', 'acquired'))
                        yield

                    def native(*args):
                        if args[1] == 'bootstrap':
                            self.assertIn(('ownership', 'acquired'), events)
                        events.append(('native', args[1]))

                    with patch.object(install, 'active', side_effect=active), \
                         patch.object(install, 'data_ownership', side_effect=owner), \
                         patch.object(install, 'run', side_effect=native), \
                         patch.object(install, 'health', return_value={'ok': True}), \
                         patch.object(install.time, 'monotonic', return_value=0), \
                         patch.object(install.time, 'sleep') as sleep:
                        install.lifecycle(meta, 'restart')
                    self.assertEqual(sleep.call_count, 2 if lock_busy else 1)
                    self.assertEqual([e for e in events if e[0] == 'native'],
                                     [('native', 'bootout'), ('native', 'bootstrap')])

    def test_restart_refuses_owner_still_active_at_stop_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            meta = {'manager': 'launchd', 'name': 'mc03-test', 'home': directory,
                    'unit': str(Path(directory) / 'service.plist')}
            with patch.object(install, 'active', return_value=True), \
                 patch.object(install, 'data_ownership') as owner, \
                 patch.object(install, 'run') as native, \
                 patch.object(install.time, 'monotonic', side_effect=[0, 30]):
                with self.assertRaisesRegex(RuntimeError, 'service owner is active'):
                    install.lifecycle(meta, 'restart')
            owner.assert_not_called()
            native.assert_called_once_with('launchctl', 'bootout', f'gui/{os.getuid()}/mc03-test')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mc03-', dir=os.environ.get('TMPDIR', '/tmp'))
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_recipe_artifact_requires_release_digest_and_exact_bounded_size(self):
        source = self.root / 'offline.zip'
        payload = b'synthetic artifact' * 70000
        source.write_bytes(payload)
        release = self.root / 'release'; release.mkdir()
        expected = {'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        with patch.object(install, 'RECIPE_PACK', expected):
            staged = install.stage_recipe_pack(release, source)
            self.assertEqual(staged.read_bytes(), payload)
            source.write_bytes(b'changed after staging')
            self.assertEqual(staged.read_bytes(), payload)
            for invalid in (payload[:-1], payload + b'x', b'x' + payload[1:]):
                source.write_bytes(invalid)
                with self.assertRaisesRegex(RuntimeError, 'release descriptor|pinned'):
                    install.stage_recipe_pack(release, source)
                self.assertEqual(list(release.iterdir()), [staged])

    def test_recipe_artifact_rejects_links_special_files_and_unreleased_input(self):
        source = self.root / 'source'; source.write_bytes(b'keep')
        link = self.root / 'link'; link.symlink_to(source)
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        release = self.root / 'release'; release.mkdir()
        expected = {'bytes': 4, 'sha256': hashlib.sha256(b'keep').hexdigest()}
        with patch.object(install, 'RECIPE_PACK', expected):
            for invalid in (link, fifo):
                with self.assertRaises((OSError, RuntimeError)):
                    install.stage_recipe_pack(release, invalid)
            self.assertEqual(list(release.iterdir()), [])
        with patch.object(install, 'RECIPE_PACK', None):
            self.assertIsNone(install.stage_recipe_pack(release))
            with self.assertRaisesRegex(RuntimeError, 'no released recipe pack'):
                install.stage_recipe_pack(release, source)
        self.assertEqual(source.read_bytes(), b'keep')

    def test_backup_refuses_replaceable_code_and_symlink_aliases(self):
        code = self.root / 'code'; code.mkdir()
        alias = self.root / 'code-alias'; alias.symlink_to(code)
        state = self.root / 'state'; state.mkdir()
        meta = {'code_root': str(code), 'paths': {'state': str(state)}}
        for destination in (code, code / 'release/snapshot', alias / 'snapshot'):
            with self.assertRaisesRegex(RuntimeError, 'outside replaceable program files'):
                install.backup(meta, destination)
        self.assertEqual(list(code.iterdir()), [])

    def test_real_isolated_pack_preflight_uses_installed_source(self):
        from recipe_portable import FORMAT, canonical_bytes, write_archive
        from recipes import normalize_recipe
        recipe = normalize_recipe(RECIPE)
        manifest = {'format': FORMAT, 'format_version': 1, 'kind': 'bundled',
                    'pack_id': 'mc03-test', 'pack_version': '1', 'normalizer_version': 'test1',
                    'recipe_schema_version': 1, 'records_count': 1}
        records = self.root / 'records.jsonl'
        records.write_bytes(canonical_bytes({'recipe_id': 'sample', 'status': 'draft', 'recipe': recipe}) + b'\n')
        archive = self.root / 'pack.zip'
        write_archive(archive, manifest, {'records.jsonl': records})
        expected = {key: value for key, value in manifest.items() if key not in {'kind', 'records_count'}}
        expected.update(url=archive.as_uri(), bytes=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        release = self.root / 'release'; (release / 'venv/bin').mkdir(parents=True)
        (release / 'venv/bin/python').symlink_to(sys.executable)
        for path in CORE.glob('*.py'):
            (release / path.name).symlink_to(path)
        with patch.object(install, 'RECIPE_PACK', expected):
            report = install.recipe_pack_command(release, 'preflight', archive)
        self.assertEqual(report['records_count'], 1)
        self.assertEqual(report['archive_sha256'], expected['sha256'])

    def test_migration_child_keeps_ownership_after_installer_parent_is_killed(self):
        state = self.root / 'state'
        config = self.root / 'config.json'; config.write_text(json.dumps(CONFIG))
        RecipeStore(state / 'recipes.sqlite3', CONFIG['household']).search()
        release = self.root / 'release'; (release / 'venv/bin').mkdir(parents=True)
        (release / 'venv/bin/python').symlink_to(sys.executable)
        for path in CORE.glob('*.py'):
            (release / path.name).symlink_to(path)
        meta = {'paths': {'state': str(state), 'config': str(config),
                         'browser_profile': str(self.root / 'profile'),
                         'browser_home': str(self.root / 'browser'),
                         'browser_socket_directory': str(self.root / 'browser/run'),
                         'socket': str(self.root / 'run/service.sock')}}
        code = "import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from install import migrate; migrate(Path(sys.argv[1]),json.loads(sys.argv[2]))"
        with sqlite3.connect(state / 'recipes.sqlite3') as blocker, (self.root / 'migration.log').open('w') as log:
            blocker.execute('BEGIN IMMEDIATE')  # Hold the real migration in database I/O.
            parent = subprocess.Popen([sys.executable, '-I', '-c', code, str(release), json.dumps(meta)],
                                      stdout=log, stderr=log, start_new_session=True)
            child = None
            child_identity = None
            def identity(pid):
                return command('ps', '-p', pid, '-o', 'lstart=,pgid=,command=', success=False).stdout.strip()
            try:
                deadline = time.monotonic() + 10
                while not (state / 'state.json').exists():
                    self.assertIsNone(parent.poll(), (self.root / 'migration.log').read_text())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                rows = command('ps', '-axo', 'pid=,ppid=').stdout.splitlines()
                children = [int(row.split()[0]) for row in rows if int(row.split()[1]) == parent.pid]
                self.assertEqual(len(children), 1)
                child = children[0]
                self.assertEqual(os.getpgid(child), parent.pid)
                child_identity = identity(child)
                self.assertTrue(child_identity)
                os.kill(child, signal.SIGSTOP)
                parent.kill(); parent.wait(timeout=5)
                blocker.rollback()
                with self.assertRaisesRegex(RuntimeError, 'owned'):
                    with file_lock(state / '.service-owner.lock'):
                        pass
                blocked = command(*install.service_args(meta, release), success=False, timeout=5)
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn('owned', blocked.stderr)
                os.kill(child, signal.SIGCONT)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with file_lock(state / '.service-owner.lock'):
                            break
                    except RuntimeError:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.02)
                with service(self.root):
                    self.assertTrue(raw_rpc(self.root / 'run/service.sock', {'operation': 'health'})['ok'])
            finally:
                blocker.rollback()
                if parent.poll() is None:
                    os.killpg(parent.pid, signal.SIGKILL)
                    parent.wait(timeout=5)
                elif child is not None and identity(child) == child_identity:
                    os.kill(child, signal.SIGKILL)

    def test_live_service_and_alias_locks_leave_listener_and_state_intact(self):
        with service(self.root) as (first, args):
            sock = self.root / 'run/service.sock'
            inode = sock.stat().st_ino
            before = (self.root / 'state/state.json').read_bytes()
            second = command(*args, success=False)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('owned', second.stderr)
            alias = self.root / 'state-alias'
            alias.symlink_to(self.root / 'state')
            with self.assertRaises(RuntimeError):
                with ownership(alias, self.root / 'other-profile'):
                    pass
            with self.assertRaises(RuntimeError):
                with ownership(self.root / 'other-state', self.root / 'browser/profile'):
                    pass
            self.assertEqual(sock.stat().st_ino, inode)
            self.assertEqual((self.root / 'state/state.json').read_bytes(), before)
            self.assertIsNone(first.poll())
            self.assertTrue(raw_rpc(sock, {'operation': 'health'})['ok'])
            bad = raw_rpc(sock, {'operation': 'profile', 'action': 'reset', 'contract': 99})
            self.assertFalse(bad['ok'])
            self.assertIn('incompatible', bad['error'])

    def test_live_legacy_socket_is_not_unlinked_and_regular_files_are_preserved(self):
        path = self.root / 'socket'
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path)); listener.listen()
            inode = path.stat().st_ino
            with self.assertRaisesRegex(RuntimeError, 'active'):
                with listener_ownership(path):
                    pass
            self.assertEqual(path.stat().st_ino, inode)
        with listener_ownership(path):
            self.assertFalse(path.exists())
        path.write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'non-socket'):
            with listener_ownership(path):
                pass
        self.assertEqual(path.read_text(), 'keep')

    def test_legacy_process_paths_with_spaces_and_relative_paths(self):
        old = self.root / 'service.py'
        old.write_text('import time; time.sleep(30)')
        state = self.root / 'household with spaces/state'
        process = subprocess.Popen([sys.executable, str(old), '--config', str(self.root / 'config.json'), '--state', 'household with spaces/state', '--browser-profile', str(self.root / 'unrelated-profile')], cwd=self.root)
        try:
            time.sleep(.1)
            with self.assertRaisesRegex(RuntimeError, 'service PID'):
                assert_no_legacy_service(state, self.root / 'profile')
        finally:
            process.terminate(); process.wait(timeout=5)

    def test_unrelated_service_program_does_not_block_start(self):
        script = self.root / 'service.py'
        script.write_text('import time; time.sleep(30)')
        process = subprocess.Popen([sys.executable, str(script)])
        try:
            time.sleep(.1)
            with ownership(self.root / 'state', self.root / 'profile'):
                self.assertIsNone(process.poll())
        finally:
            process.terminate(); process.wait(timeout=5)

    def test_old_core_is_probed_before_any_mutation(self):
        import rpc_client
        path = self.root / 'old.sock'
        requests = []
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path)); listener.listen()
            def reply():
                connection, _ = listener.accept()
                with connection:
                    requests.append(json.loads(connection.recv(65536)))
                    connection.sendall(b'{"ok":true,"result":{}}\n')
            thread = threading.Thread(target=reply); thread.start()
            previous = rpc_client.SOCKET; rpc_client.SOCKET = path
            try:
                with self.assertRaisesRegex(rpc_client.ServiceError, 'incompatible'):
                    rpc_client.rpc('checkout', action='submit', idempotency_key='original')
            finally:
                rpc_client.SOCKET = previous
                thread.join(3)
        self.assertEqual([x['operation'] for x in requests], ['health'])

    def test_full_backup_restores_database_assets_history_and_uncertain_journals(self):
        config = self.root / 'config.json'; config.write_text(json.dumps(CONFIG))
        state_dir = self.root / 'state'
        store = StateStore(state_dir, CONFIG)
        data = store.read()
        for field in ['pending_checkout', 'pending_cancellation', 'pending_cart_change', 'order_change']:
            data[field] = {'status': 'uncertain', 'idempotency_key': field + '-original', 'provider': 'meny'}
        data['history'] = [{'menu_ref': 'exact-frozen-ref', 'recipe_ref': 'exact-revision'}]
        data['email_jobs'] = {'synthetic': {'status': 'sending', 'claim': 'original-email-claim'}}
        install.write_json(state_dir / 'state.json', data)
        bank = RecipeStore(state_dir / 'recipes.sqlite3', CONFIG['household'])
        record = bank.save(RECIPE, idempotency_key='original-save')
        (state_dir / 'assets').mkdir(); (state_dir / 'assets/cover.bin').write_bytes(b'synthetic future managed asset')
        (state_dir / 'snapshots').mkdir(); (state_dir / 'snapshots/exact.json').write_text('{"revision":1}')
        meta = {'paths': {'state': str(state_dir), 'config': str(config)}}
        with ownership(state_dir, self.root / 'profile'):
            install.backup(meta, self.root / 'backup')
        restored = self.root / 'restored'
        installer('restore', '--backup', self.root / 'backup', '--home', restored)
        self.assertEqual((restored / 'state/state.json').read_bytes(), (state_dir / 'state.json').read_bytes())
        self.assertEqual((restored / 'state/assets/cover.bin').read_bytes(), (state_dir / 'assets/cover.bin').read_bytes())
        self.assertEqual(RecipeStore(restored / 'state/recipes.sqlite3', CONFIG['household']).get(record['id'], 1), bank.get(record['id'], 1))
        self.assertNotEqual(installer('restore', '--backup', self.root / 'backup', '--home', restored, success=False).returncode, 0)

    def test_missing_or_linked_backup_does_not_succeed(self):
        backup = self.root / 'backup'; backup.mkdir()
        install.write_json(backup / 'backup.json', {'format': 1, 'complete': True})
        install.write_json(backup / 'config.json', CONFIG)
        target = self.root / 'restore'
        self.assertNotEqual(installer('restore', '--backup', backup, '--home', target, success=False).returncode, 0)
        self.assertFalse(target.exists())
        (backup / 'state').mkdir(); (backup / 'state/outside').symlink_to('/etc/passwd')
        self.assertNotEqual(installer('restore', '--backup', backup, '--home', target, success=False).returncode, 0)

    def test_real_json_and_sqlite_migration_and_failed_database(self):
        state = self.root / 'state'; config = self.root / 'config.json'
        config.write_text(json.dumps(CONFIG))
        data = StateStore(state, CONFIG).read()
        data['version'] = 5; data['favorites'] = data.pop('product_favorites')
        for key in ('menu_planning', 'planning_feedback', 'batch_outcomes'):
            data.pop(key, None)
        install.write_json(state / 'state.json', data)
        release = self.root / 'release'; release.mkdir(); (release / 'venv/bin').mkdir(parents=True)
        (release / 'venv/bin/python').symlink_to(sys.executable)
        # Use the actual runtime source with the actual migration child.
        for path in CORE.glob('*.py'):
            (release / path.name).symlink_to(path)
        meta = {'paths': {'state': str(state), 'config': str(config),
                         'browser_profile': str(self.root / 'profile'),
                         'browser_home': str(self.root / 'browser'),
                         'browser_socket_directory': str(self.root / 'browser/run'),
                         'socket': str(self.root / 'run/service.sock')}}
        create_v1_bank(state / 'recipes.sqlite3', RECIPE)
        install.migrate(release, meta)
        old = RecipeStore(state / 'recipes.sqlite3', CONFIG['household']).get('rec_v1', 1)
        self.assertEqual(old['name'], RECIPE['name'])
        self.assertEqual(old['library_recipe_ref']['version'], '1')
        self.assertEqual(json.loads((state / 'state.json').read_text())['version'], 12)
        with sqlite3.connect(state / 'recipes.sqlite3') as db:
            self.assertEqual(dict(db.execute('select key,value from metadata'))['schema_version'], '6')
            self.assertEqual(db.execute('select response_json from idempotency where key=?', ('v1-key',)).fetchone()[0], '{"id":"rec_v1"}')
        (state / 'recipes.sqlite3').write_bytes(b'broken database')
        with self.assertRaises(subprocess.CalledProcessError):
            install.migrate(release, meta)


async def native_bridge(meta, restart=False):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    current = Path(meta['code_root']) / 'current'
    params = StdioServerParameters(command=str(current / 'venv/bin/python'), args=['-I', str(current / 'mcp_server.py')], env={'MEAL_CONCIERGE_SOCKET': meta['paths']['socket'], 'HOME': meta['home']})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            self_tools = await client.list_tools()
            assert any(t.name == 'meal_concierge_recipes' for t in self_tools.tools)
            result = await client.call_tool('meal_concierge_setup', {'action': 'show'})
            assert not result.is_error, result
            if restart:
                await asyncio.to_thread(installer, 'restart', '--home', meta['home'])
                result = await client.call_tool('meal_concierge_setup', {'action': 'show'})
                assert not result.is_error, result
            return len(self_tools.tools)


def native(root, name, adapter, chrome):
    root = Path(root).resolve()
    home, code = root / 'native-home', root / 'native-code'
    if home.exists() or code.exists():
        raise RuntimeError('native test requires fresh scratch directories')
    root.mkdir(parents=True, exist_ok=True)
    installed = False
    try:
        print(installer('install', '--home', home, '--code-root', code, '--name', name, '--provider', 'meny', '--household', 'MC03 synthetic', '--agent-browser', adapter, '--browser-executable', chrome).stdout)
        installed = True
        meta = json.loads((home / 'runtime.json').read_text())
        assert not install.active(meta)
        # Emulate termination after unit publication but before the final manifest.
        (home / 'runtime.json').rename(home / 'pending-install.json')
        install.write_json(home / 'maintenance.json', {'reason': 'interrupted initial publication'})
        assert installer('start', '--home', home, success=False).returncode
        installer('update', '--home', home)
        meta = json.loads((home / 'runtime.json').read_text())
        installer('start', '--home', home)
        inode = Path(meta['paths']['socket']).stat().st_ino
        args = install.service_args(meta, Path(meta['code_root']) / 'current')
        duplicate = command(*args, success=False)
        assert duplicate.returncode and Path(meta['paths']['socket']).stat().st_ino == inode
        attachment = json.loads(installer('attach', '--home', home).stdout)
        assert Path(meta['paths']['socket']).stat().st_ino == inode
        # Run native MCP with the installed Python, independent of this test runner.
        probe = command(attachment['command'], '-I', __file__, '--bridge', home)
        print(probe.stdout.strip())
        assert install.health(meta)['ok']
        assert installer('update', '--home', home, success=False).returncode
        print(command(attachment['command'], '-I', __file__, '--bridge-restart', home).stdout.strip())
        installer('stop', '--home', home)
        state = Path(meta['paths']['state'])
        bank = RecipeStore(state / 'recipes.sqlite3', 'MC03 synthetic')
        saved = bank.save(RECIPE, idempotency_key='native-exact')
        (state / 'assets').mkdir(); (state / 'assets/frozen.bin').write_bytes(b'preserved asset')
        data = json.loads((state / 'state.json').read_text())
        data['pending_checkout'] = {'status': 'uncertain', 'idempotency_key': 'native-original'}
        install.write_json(state / 'state.json', data)
        before = (state / 'state.json').read_bytes()
        installer('backup', '--home', home, '--backup', root / 'native-backup')
        print(installer('update', '--home', home).stdout)
        assert (state / 'state.json').read_bytes() == before
        assert RecipeStore(state / 'recipes.sqlite3', 'MC03 synthetic').get(saved['id'], 1) == bank.get(saved['id'], 1)
        assert (state / 'assets/frozen.bin').read_bytes() == b'preserved asset'
        installer('start', '--home', home)
        print(command(attachment['command'], '-I', __file__, '--bridge', home).stdout.strip())
        installer('stop', '--home', home)
        installer('restore', '--home', root / 'relocated', '--backup', root / 'native-backup')
        assert (root / 'relocated/state/assets/frozen.bin').read_bytes() == b'preserved asset'
        # Isolated actual shared browser wrapper; no retailer URL/login is used.
        print(command(attachment['command'], '-I', __file__, '--browser', home).stdout.strip())
        # Inspect adapter display separately.
        browser_env = {**os.environ, 'HOME': str(root / 'browser-home'), 'AGENT_BROWSER_SOCKET_DIR': str(root / 'browser-run')}
        browser_args = [adapter, '--session', 'mc03-native', '--profile', str(root / 'browser-profile'), '--executable-path', chrome]
        try:
            command(*browser_args, 'open', 'about:blank', env=browser_env)
            command(*browser_args, 'eval', "document.title='MC03 synthetic'; document.title", env=browser_env)
            assert 'MC03 synthetic' in command(*browser_args, 'get', 'title', env=browser_env).stdout
        finally:
            command(*browser_args, 'close', env=browser_env, success=False)
        # Retire the exact isolated old native owner, then adopt its configured
        # nonstandard paths with the same reserved name. No files are moved.
        if meta['manager'] == 'systemd':
            command('systemctl', '--user', 'disable', meta['name'] + '.service')
        Path(meta['unit']).unlink(missing_ok=True)
        if meta['manager'] == 'systemd':
            command('systemctl', '--user', 'daemon-reload')
        adopted = root / 'adopted-home'
        adopt_args = ['install', '--home', adopted, '--code-root', root / 'adopted-code', '--name', name,
                      '--adopt', '--legacy-unit', name, '--agent-browser', adapter, '--browser-executable', chrome]
        for key in ['state', 'config', 'socket', 'tokens', 'browser_home', 'browser_profile', 'browser_socket_directory']:
            adopt_args += ['--' + key.replace('_', '-'), meta['paths'][key]]
        installer(*adopt_args)
        home = adopted
        adopted_meta = json.loads((home / 'runtime.json').read_text())
        for key in ['state', 'config', 'socket', 'tokens', 'browser_home', 'browser_profile', 'browser_socket_directory']:
            assert adopted_meta['paths'][key] == meta['paths'][key]
        meta = adopted_meta
        installer('start', '--home', home)
        installer('attach', '--home', home)
        installer('stop', '--home', home)
        assert (state / 'state.json').read_bytes() == before
        print('PASS native install / interrupted publication / singleton / SDK reconnect / offline update / exact state+recipe+asset restore / browser / existing-path adoption')
    finally:
        if installed:
            meta = json.loads((home / 'runtime.json').read_text())
            installer('stop', '--home', home)
            if meta['manager'] == 'systemd':
                command('systemctl', '--user', 'disable', meta['name'] + '.service')
            Path(meta['unit']).unlink(missing_ok=True)
            if meta['manager'] == 'systemd':
                command('systemctl', '--user', 'daemon-reload')
            assert not install.active(meta)


def native_mathem(root, name):
    home = Path(root) / 'mathem-home'
    foreign = Path(root) / 'foreign-project'
    foreign.mkdir(parents=True, exist_ok=True)
    (foreign / 'pyproject.toml').write_text('[tool.uv]\nrequired-version = "==0.0.0"\nexclude-newer = "2000-01-01T00:00:00Z"\n')
    inherited = command('uv', 'venv', '--python', install.PYTHON, foreign / 'unwanted-venv', cwd=foreign, success=False)
    assert inherited.returncode and 'required' in inherited.stderr.lower(), inherited.stderr
    installer('install', '--home', home, '--code-root', Path(root) / 'mathem-code', '--name', name,
              '--provider', 'mathem', '--household', 'MC03 Mathem synthetic', cwd=foreign)
    meta = json.loads((home / 'runtime.json').read_text())
    try:
        assert 'browser_binary' not in meta['paths']
        installer('start', '--home', home)
        attachment = json.loads(installer('attach', '--home', home).stdout)
        print(command(attachment['command'], '-I', __file__, '--bridge', home).stdout.strip())
        assert install.health(meta)['integration']['status'] == 'awaiting_login'
        print(command(attachment['command'], '-I', __file__, '--bridge-restart', home).stdout.strip())
        installer('stop', '--home', home)
        state = Path(meta['paths']['state'])
        bank = RecipeStore(state / 'recipes.sqlite3', 'MC03 Mathem synthetic')
        saved = bank.save(RECIPE, idempotency_key='mathem-native-exact')
        frozen = bank.get(saved['id'], 1)
        data = json.loads((state / 'state.json').read_text())
        data['pending_checkout'] = {'status': 'uncertain', 'idempotency_key': 'mathem-native-original'}
        install.write_json(state / 'state.json', data)
        before = (state / 'state.json').read_bytes()
        installer('update', '--home', home, cwd=foreign)
        updated = json.loads((home / 'runtime.json').read_text())
        assert updated['release'] != meta['release']
        assert (state / 'state.json').read_bytes() == before
        assert RecipeStore(state / 'recipes.sqlite3', 'MC03 Mathem synthetic').get(saved['id'], 1) == frozen
        meta = updated
        installer('start', '--home', home)
        print(command(attachment['command'], '-I', __file__, '--bridge', home).stdout.strip())
        print('PASS native Mathem core and own bank without browser or Hermes; provider authentication not configured in this isolated test')
        print('PASS standalone runtime ignores an incompatible invoking-project uv configuration')
        print('PASS restart/reconnect and offline upgrade preserve the exact saved recipe and uncertain operation')
    finally:
        installer('stop', '--home', home)
        if meta['manager'] == 'systemd':
            command('systemctl', '--user', 'disable', meta['name'] + '.service')
        Path(meta['unit']).unlink(missing_ok=True)
        if meta['manager'] == 'systemd':
            command('systemctl', '--user', 'daemon-reload')


def socket_container(home, name, runtime):
    # Explicit isolated Linux test: the client only receives the socket directory
    # and a non-secret Python runtime, never household state or browser files.
    home = Path(home)
    meta = json.loads((home / 'runtime.json').read_text())
    args = install.service_args(meta, Path(meta['code_root']) / 'current')
    docker = ['sudo', '-n', 'docker']
    process = None
    with (home / 'container-service.log').open('w') as log:
        try:
            process = subprocess.Popen(args, stdout=log, stderr=log)
            deadline = time.monotonic() + 10
            while True:
                try:
                    install.health(meta); break
                except OSError:
                    assert process.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(.05)
            identity = command(*docker, 'run', '-d', '--name', name, '--network', 'none', '--read-only',
                '--user', f'{os.getuid()}:{os.getgid()}',
                '--mount', f"type=bind,source={Path(meta['paths']['socket']).parent},target=/socket,readonly",
                '--mount', f'type=bind,source={runtime},target=/runtime,readonly',
                '--entrypoint', '/runtime/bin/python3.12', 'meal-concierge-complete:20260905',
                '-c', 'import time; time.sleep(300)').stdout.strip()
            probe = "import socket,json; s=socket.socket(socket.AF_UNIX); s.connect('/socket/service.sock'); s.sendall(b'{\"operation\":\"health\",\"contract\":1}\\n'); r=json.loads(s.recv(65536)); assert r['ok'] and r['contract']==1; print('health ok')"
            command(*docker, 'exec', name, '/runtime/bin/python3.12', '-c', probe)
            process.terminate(); process.wait(timeout=10)
            process = subprocess.Popen(args, stdout=log, stderr=log)
            deadline = time.monotonic() + 10
            while True:
                try:
                    install.health(meta); break
                except OSError:
                    assert process.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(.05)
            command(*docker, 'exec', name, '/runtime/bin/python3.12', '-c', probe)
            after = command(*docker, 'inspect', name, '--format', '{{.Id}}').stdout.strip()
            assert identity == after
            print('PASS same owner-UID container reconnects through socket-only directory after host service restart')
        finally:
            if process is not None and process.poll() is None:
                process.terminate(); process.wait(timeout=10)
            command(*docker, 'rm', '-f', name, success=False)


def compose_split():
    # Run only in a task container with uid0 and SETUID/SETGID, no DAC_OVERRIDE,
    # and separate browser-owned 0700 tmpfs mounts. Never against host paths.
    root = Path('/state')
    root.joinpath('config.json').write_text(json.dumps(CONFIG))
    args = [sys.executable, str(CORE / 'service.py'), '--config', '/state/config.json', '--state', '/state/data',
            '--socket', '/socket/service.sock', '--socket-group', '10002', '--agent-uid', '10000',
            '--browser-profile', '/browser/profile', '--browser-home', '/browser',
            '--browser-socket-directory', '/browser-run', '--browser-uid', '10001', '--browser-gid', '10002']
    for _ in range(2):
        with root.joinpath('log').open('w') as log:
            process = subprocess.Popen(args, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        if raw_rpc('/socket/service.sock', {'operation': 'health'})['ok']:
                            break
                    except OSError:
                        pass
                    assert process.poll() is None, root.joinpath('log').read_text()
                    assert time.monotonic() < deadline, root.joinpath('log').read_text()
                    time.sleep(.05)
                assert command(*args, success=False).returncode != 0
                assert Path('/state/data/state.json').stat().st_uid == 0
                # Inspect private browser locks with their real owner identity.
                from runtime_ownership import effective_owner
                with effective_owner(10001, 10002):
                    assert Path('/browser/profile/.service-owner.lock').stat().st_uid == 10001
                assert os.geteuid() == 0 and os.getegid() == 10002
            finally:
                process.terminate(); process.wait(timeout=10)
    print('PASS isolated Compose UID/capability split, singleton, restart and restored core identity')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--socket-container']:
        socket_container(*sys.argv[2:])
    elif sys.argv[1:2] == ['--mathem']:
        native_mathem(*sys.argv[2:])
    elif sys.argv[1:2] == ['--compose-split']:
        compose_split()
    elif sys.argv[1:2] == ['--native']:
        native(*sys.argv[2:])
    elif sys.argv[1:2] == ['--browser']:
        meta = json.loads((Path(sys.argv[2]) / 'runtime.json').read_text())
        p = meta['paths']
        sys.path.insert(0, str(Path(meta['code_root']) / 'current'))
        from meny import MenyClient
        browser = MenyClient(instance=json.loads(Path(p['config']).read_text())['instance'], binary=p['browser_binary'], executable=p['browser_executable'],
                             profile=p['browser_profile'], home=p['browser_home'], socket_directory=p['browser_socket_directory'],
                             uid=os.getuid(), gid=os.getgid())
        try:
            browser._invoke('open', 'about:blank')
            value = browser._invoke('eval', "document.title='MC03 synthetic'; document.title")
            assert 'MC03 synthetic' in json.dumps(value), value
            print('PASS shared browser wrapper with native service paths')
        finally:
            browser._invoke('close')
    elif sys.argv[1:2] in (['--bridge'], ['--bridge-restart']):
        meta = json.loads((Path(sys.argv[2]) / 'runtime.json').read_text())
        print('real installed SDK discovery/setup tools:', asyncio.run(native_bridge(meta, restart=sys.argv[1] == '--bridge-restart')))
    else:
        unittest.main()
