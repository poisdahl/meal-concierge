#!/usr/bin/env python3
"""Native, owner-local installation. No agent registration or provider login side effects."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_ownership import file_lock, ownership, listener_ownership

SOURCE = Path(__file__).resolve().parent
PYTHON = '3.12.12'
# Published with the matching runtime after the public artifact is verified.
# The archive itself, command-line input and household config cannot supply trust.
RECIPE_PACK = {
    'format': 'meal-concierge-recipes',
    'format_version': 1,
    'recipe_schema_version': 2,
    'normalizer_version': '1',
    'pack_id': 'wikibooks-themealdb-en',
    'pack_version': '2026-09-06.4',
    'bytes': 186706769,
    'sha256': '08b3cd051208e0e2146eca512b2f751e4ee1a86af13fd069e4dcc321019afca7',
    'url': 'https://github.com/poisdahl/meal-concierge/releases/download/recipes-2026-09-06.4/meal-concierge-recipes-2026-09-06.4.zip',
}
MAX_PACK_BYTES = 1024 * 1024 * 1024


def stage_recipe_pack(release, source=None):
    """Copy a release-pinned artifact; offline input has the same trust boundary."""
    expected = RECIPE_PACK
    if expected is None:
        if source is not None:
            raise RuntimeError('no released recipe pack is pinned by this runtime')
        return None
    if not 0 < expected['bytes'] <= MAX_PACK_BYTES:
        raise RuntimeError('release recipe pack exceeds the supported archive size')
    destination = Path(release) / ('recipe-pack-' + uuid.uuid4().hex + '.zip')
    digest = hashlib.sha256()
    size = 0
    try:
        if source is None:
            handle = urllib.request.urlopen(expected['url'], timeout=60)
        else:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            handle = os.fdopen(descriptor, 'rb')
        with handle:
            if source is not None and not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise RuntimeError('recipe pack input must be a regular file')
            with destination.open('xb') as output:
                while chunk := handle.read(min(1024 * 1024, expected['bytes'] - size + 1)):
                    size += len(chunk)
                    if size > expected['bytes']:
                        raise RuntimeError('recipe pack is larger than its release descriptor')
                    digest.update(chunk)
                    output.write(chunk)
        if size != expected['bytes'] or digest.hexdigest() != expected['sha256']:
            raise RuntimeError('recipe pack differs from the artifact pinned by this runtime')
        destination.chmod(0o400)
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def recipe_pack_command(release, action, archive, meta=None):
    code = "import sys,json; sys.path.insert(0,sys.argv[1]); from recipe_portable import preflight_archive; print(json.dumps(preflight_archive(sys.argv[2],json.loads(sys.argv[3]))))"
    args = [release / 'venv/bin/python', '-I', '-c', code, release, archive, json.dumps(RECIPE_PACK)]
    if action == 'apply':
        # The applying process owns its locks itself. Killing this installer
        # cannot release ownership while its surviving child still writes.
        code = "import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from install import apply_recipe_pack; apply_recipe_pack(Path(sys.argv[2]),json.loads(sys.argv[3]))"
        args = [release / 'venv/bin/python', '-I', '-c', code, release, archive, json.dumps(meta)]
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True)
    if result.returncode not in ({0, 2} if action == 'apply' else {0}):
        detail = (result.stdout or result.stderr).strip()[:2000]
        raise RuntimeError(f'recipe pack {action} failed ({result.returncode}): {detail}')
    return json.loads(result.stdout)


def apply_recipe_pack(archive, meta):
    """Installed-runtime child entry point; no parent-owned lifetime locks."""
    from recipe_portable import apply_archive
    with offline(meta):
        settings = json.loads(Path(meta['paths']['config']).read_text())
        report = apply_archive(archive, Path(meta['paths']['state']), settings['household'], RECIPE_PACK)
        print(json.dumps(report))
    if report['status'] != 'complete':
        raise SystemExit(2)


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def executable(value, candidates, label):
    for name in ([value] if value else candidates):
        path = Path(name).expanduser() if '/' in str(name) else Path(shutil.which(name) or '/nonexistent')
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.absolute())
    raise RuntimeError(f'{label} is missing; supply its explicit executable path')


def browser_paths(args, provider):
    if provider == 'mathem':
        return {}
    adapter = executable(args.agent_browser, ['agent-browser', str(Path.home() / '.local/lib/meal-concierge/node_modules/.bin/agent-browser')], 'agent-browser')
    version = run(adapter, '--version', capture_output=True, text=True).stdout.strip()
    if not re.search(r'\b0\.33\.1\b', version):
        raise RuntimeError('install the tested agent-browser@0.33.1')
    chrome = executable(args.browser_executable, ['chromium', 'chromium-browser', 'google-chrome', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', str(Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome')], 'non-snap Chrome/Chromium')
    if str(Path(chrome).resolve()).startswith('/snap/') or b'/snap/bin/chromium' in Path(chrome).open('rb').read(4096):
        raise RuntimeError('Snap Chromium cannot access the private browser profile')
    return {'browser_binary': adapter, 'browser_executable': chrome}


def native_manager():
    if platform.system() == 'Linux':
        run('systemctl', '--user', 'show-environment', stdout=subprocess.DEVNULL)
        return 'systemd'
    if platform.system() == 'Darwin' and platform.machine() == 'arm64':
        run('launchctl', 'print', f'gui/{os.getuid()}', stdout=subprocess.DEVNULL)
        return 'launchd'
    raise RuntimeError('requires Linux/user-systemd or Apple Silicon macOS/launchd')


def unit_path(manager, name):
    if manager == 'systemd':
        return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'systemd/user' / (name + '.service')
    return Path.home() / 'Library/LaunchAgents' / (name + '.plist')


def active(meta):
    if meta['manager'] == 'systemd':
        result = subprocess.run(['systemctl', '--user', 'is-active', meta['name'] + '.service'], capture_output=True, text=True)
        return result.stdout.strip() not in {'inactive', 'failed', 'unknown'}
    return subprocess.run(['launchctl', 'print', f"gui/{os.getuid()}/{meta['name']}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def assert_stopped(meta):
    if active(meta):
        raise RuntimeError('service owner is active; use the explicit stop command before offline backup/update')


def service_args(meta, release=None):
    root = Path(release or meta['code_root'])
    result = [str(root / 'venv/bin/python'), '-I', str(root / 'service.py')]
    for key, value in meta['paths'].items():
        result += ['--' + key.replace('_', '-'), value]
    return result


def health(meta):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(meta['paths']['socket'])
        client.sendall(b'{"operation":"health","contract":1}\n')
        data = b''
        while b'\n' not in data and len(data) <= 2 * 1024 * 1024:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    value = json.loads(data)
    if value.get('ok') is not True or value.get('contract') != 1:
        raise RuntimeError('incompatible service; update the stopped core before attachment')
    return value['result']


def lifecycle(meta, action):
    if action in {'start', 'restart'} and (Path(meta['home']) / 'maintenance.json').exists():
        raise RuntimeError('incomplete migration/update; retry the stopped update or recover offline before starting')
    if action == 'restart':
        lifecycle(meta, 'stop')
        lifecycle(meta, 'start')
        return
    if meta['manager'] == 'systemd':
        run('systemctl', '--user', action, meta['name'] + '.service')
    elif action == 'stop':
        if active(meta):
            run('launchctl', 'bootout', f"gui/{os.getuid()}/{meta['name']}")
    elif not active(meta):
        run('launchctl', 'bootstrap', f'gui/{os.getuid()}', meta['unit'])
    if action == 'start':
        deadline = time.monotonic() + 20
        while True:
            try:
                health(meta)
                return
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"service did not become ready; inspect {meta['home']}/service.err.log")
                time.sleep(.1)
    else:
        # launchd bootout can return before process exit; never take over early.
        deadline = time.monotonic() + 30
        while True:
            try:
                with offline(meta):
                    return
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)


@contextmanager
def offline(meta):
    assert_stopped(meta)
    with data_ownership(meta):
        yield


@contextmanager
def data_ownership(meta):
    p = meta['paths']
    with ownership(p['state'], p['browser_profile'], p['browser_home'], p['browser_socket_directory']), listener_ownership(p['socket']):
        yield


def copy_data(source, target):
    """Offline full-tree copy, including WAL, snapshots and assets. Never follow links."""
    source, target = Path(source), Path(target)
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError('data source must be an existing regular directory')
    for root, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise RuntimeError('data backup requires regular files/directories; relocate linked assets explicitly first')
    shutil.copytree(source, target)


def backup(meta, destination):
    destination = Path(destination).resolve()
    state = Path(meta['paths']['state']).resolve()
    if destination == state or destination.is_relative_to(state):
        raise RuntimeError('backup must be outside the state directory')
    if meta.get('code_root'):
        code = Path(meta['code_root']).resolve()
        if destination == code or destination.is_relative_to(code):
            raise RuntimeError('backup must be outside replaceable program files')
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    copy_data(state, destination / 'state')
    shutil.copyfile(meta['paths']['config'], destination / 'config.json')
    write_json(destination / 'backup.json', {'format': 1, 'complete': True, 'release': meta.get('release'), 'note': 'Private offline state/config backup; no credentials or browser session. Restore only to empty state; reconcile later external outcomes.'})
    return str(destination)


def stage_release(code_root):
    uv = executable(None, ['uv'], 'uv')
    release = code_root / ('release-' + uuid.uuid4().hex)
    release.mkdir(parents=True, mode=0o700)
    for path in SOURCE.glob('*.py'):
        shutil.copyfile(path, release / path.name)
    shutil.copyfile(SOURCE / 'runtime-requirements.txt', release / 'runtime-requirements.txt')
    shutil.copytree(SOURCE / 'skill', release / 'skill')
    run(uv, '--no-config', 'venv', '--python', PYTHON, release / 'venv')
    run(uv, '--no-config', 'pip', 'sync', '--python', release / 'venv/bin/python', release / 'runtime-requirements.txt')
    # Check both versions AND the actual loaded modules inside this private venv.
    run(release / 'venv/bin/python', '-I', '-c', "import sys,importlib.metadata as m,pathlib,mcp,mcp.types; assert sys.version_info[:3]==(3,12,12); assert m.version('mcp')==m.version('mcp-types')=='2.1.1'; assert all(pathlib.Path(x.__file__).is_relative_to(sys.prefix) for x in [mcp,mcp.types]); print('runtime:',sys.version.split()[0],m.version('mcp'),m.version('mcp-types'),mcp.__file__,mcp.types.__file__)")
    return release


def migrate(release, meta):
    code = "import sys,json; sys.path.insert(0,sys.argv[1]); from install import migrate_owned; migrate_owned(json.loads(sys.argv[2]))"
    run(release / 'venv/bin/python', '-I', '-c', code, release, json.dumps(meta))


def migrate_owned(meta):
    from core import StateStore
    from service import config
    from recipes import RecipeStore
    with data_ownership(meta):
        state = StateStore(Path(meta['paths']['state']), config(Path(meta['paths']['config'])))
        RecipeStore(state.directory / 'recipes.sqlite3', str(state.config['household'])).search(limit=1)


def write_unit(meta):
    # The unit points at stable current paths. An atomic code switch never reloads an agent.
    args = service_args(meta, Path(meta['code_root']) / 'current')
    env = {'PATH': meta['runtime_path'], 'PYTHONNOUSERSITE': '1'}
    target = Path(meta['unit'])
    definition = Path(meta['home']) / ('service.plist' if meta['manager'] == 'launchd' else 'service.unit')
    target.parent.mkdir(parents=True, exist_ok=True)
    if meta['manager'] == 'systemd':
        def quote(s):
            if any(c in s for c in '\n\r\x00'):
                raise RuntimeError('unsupported newline/NUL in unit argument')
            return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        content = '[Unit]\nDescription=Meal Concierge\n[Service]\nType=simple\nUMask=0077\nExecStart=' + ' '.join(map(quote, args)) + '\nEnvironment=' + quote('PATH=' + env['PATH']) + '\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=30\n[Install]\nWantedBy=default.target\n'
        content = content.encode()
    else:
        content = plistlib.dumps({'Label': meta['name'], 'ProgramArguments': args, 'EnvironmentVariables': env, 'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 5, 'StandardOutPath': str(Path(meta['home']) / 'service.out.log'), 'StandardErrorPath': str(Path(meta['home']) / 'service.err.log')})
    # Native registration is an exclusive link to this installation's durable
    # definition. Pending installs can retry, but cannot overwrite another owner.
    if target.is_symlink():
        if target.resolve() != definition.resolve():
            raise RuntimeError('service definition belongs to another installation')
    elif target.exists():
        raise RuntimeError('refusing to overwrite an existing service definition')
    candidate = definition.with_suffix('.tmp')
    candidate.write_bytes(content)
    candidate.chmod(0o600)
    os.replace(candidate, definition)
    if not target.is_symlink():
        target.symlink_to(definition)
    if meta['manager'] == 'systemd':
        run('systemctl', '--user', 'daemon-reload')
        run('systemctl', '--user', 'enable', meta['name'] + '.service')


def discover(home):
    candidates = {home, Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'meal-concierge'}
    return [{'home': str(p), 'standalone': (p / 'runtime.json').exists(), 'config': (p / 'config.json').exists(), 'state': (p / 'state/state.json').exists()} for p in sorted(candidates) if p.exists()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'update', 'attach', 'start', 'stop', 'restart', 'backup', 'restore', 'discover'])
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('MEAL_CONCIERGE_HOME', str(Path.home() / '.local/share/meal-concierge'))))
    parser.add_argument('--code-root', type=Path)
    parser.add_argument('--name', default='meal-concierge' if platform.system() == 'Linux' else 'com.meal-concierge')
    parser.add_argument('--provider', choices=['oda', 'meny', 'mathem'])
    parser.add_argument('--household')
    parser.add_argument('--adopt', action='store_true')
    parser.add_argument('--legacy-unit', help='exact already stopped native service owner for adoption')
    for field in ['config', 'state', 'socket', 'tokens', 'browser-profile', 'browser-home', 'browser-socket-directory', 'agent-browser', 'browser-executable']:
        parser.add_argument('--' + field)
    parser.add_argument('--recipe-pack', type=Path, help='downloaded artifact matching the recipe pack pinned by this runtime')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    home = args.home.expanduser().resolve()
    if args.recipe_pack is not None:
        if args.action not in {'install', 'update'}:
            raise RuntimeError('--recipe-pack applies only to install/update')
        if RECIPE_PACK is None:
            raise RuntimeError('no released recipe pack is pinned by this runtime')
    if args.action == 'discover':
        print(json.dumps(discover(home), indent=2)); return
    if args.action == 'restore':
        if not args.backup:
            raise RuntimeError('--backup is required')
        marker = json.loads((args.backup / 'backup.json').read_text())
        if marker.get('format') != 1 or marker.get('complete') is not True:
            raise RuntimeError('backup is incomplete or unsupported')
        if (args.backup / 'config.json').is_symlink() or not (args.backup / 'state').is_dir():
            raise RuntimeError('backup data/config missing or linked')
        if home.exists():
            raise RuntimeError('restore only to a new empty home; never roll back current outcome journals')
        home.mkdir(parents=True, mode=0o700)
        copy_data(args.backup / 'state', home / 'state')
        shutil.copyfile(args.backup / 'config.json', home / 'config.json')
        print('Restored private data; review later external outcomes before explicit adoption. No service started.')
        return
    if args.action == 'attach':
        meta = json.loads((home / 'runtime.json').read_text())
        health(meta)
        current = Path(meta['code_root']) / 'current'
        print(json.dumps({'command': str(current / 'venv/bin/python'), 'args': ['-I', str(current / 'mcp_server.py')], 'env': {'MEAL_CONCIERGE_SOCKET': meta['paths']['socket']}, 'skill': str(current / 'skill/SKILL.md')}, indent=2)); return
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    with file_lock(home / '.installer.lock'):
        path = home / 'runtime.json'
        pending = home / 'pending-install.json'
        manifest = path if path.exists() else pending
        settings = None
        if manifest.exists():
            meta = json.loads(manifest.read_text())
            settings = meta.pop('initial_config', None)
            if args.action == 'install' and path.exists():
                raise RuntimeError('existing installation; use attach or an explicit stopped-service update')
            if args.action in {'start', 'stop', 'restart'}:
                lifecycle(meta, args.action); return
            if args.action == 'backup':
                if not args.backup:
                    raise RuntimeError('--backup destination is required')
                with offline(meta):
                    print(backup(meta, args.backup))
                return
            assert_stopped(meta)
        else:
            if args.action != 'install':
                raise RuntimeError('no standalone runtime manifest; discover and explicitly adopt existing paths first')
            manager = native_manager()
            if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,90}', args.name) is None:
                raise RuntimeError('invalid service name')
            unit = unit_path(manager, args.name)
            if unit.exists():
                raise RuntimeError('service definition already exists; choose a distinct name after stopping the old owner')
            found = [item for item in discover(home) if item['config'] or item['state'] or item['standalone']]
            if (found or args.config or args.state) and not args.adopt:
                raise RuntimeError('existing data/config discovered; explicit --adopt and exact paths are required')
            if args.adopt and not args.legacy_unit:
                raise RuntimeError('--adopt requires --legacy-unit naming the stopped old owner (or none for restored/offline data)')
            if args.legacy_unit and args.legacy_unit != 'none':
                old_name = args.legacy_unit.removesuffix('.service')
                assert_stopped({'manager': manager, 'name': old_name})
                if manager == 'systemd':
                    enabled = subprocess.run(['systemctl', '--user', 'is-enabled', old_name + '.service'], capture_output=True, text=True).stdout.strip()
                    if enabled not in {'disabled', 'not-found', 'masked'}:
                        raise RuntimeError('old owner must be disabled/masked before adoption; installer does not transfer scheduler/service authority')
                elif unit_path(manager, old_name).exists():
                    raise RuntimeError('old launchd plist must be retired before adoption so it cannot restart at login')
            paths = {key: str(Path(getattr(args, key) or default).expanduser().resolve()) for key, default in {
                'config': home / 'config.json', 'state': home / 'state', 'socket': home / 'run/service.sock',
                'browser_home': home / 'browser', 'browser_profile': home / 'browser/profile',
                'browser_socket_directory': home / 'browser/run', 'tokens': home / 'tokens',
            }.items()}
            config_path = Path(paths['config'])
            if config_path.exists():
                settings = json.loads(config_path.read_text())
                if args.provider and args.provider != settings.get('provider') or args.household and args.household != settings.get('household'):
                    raise RuntimeError('existing config household/provider differs; no implicit conversion')
            else:
                if args.adopt or not args.provider or not args.household:
                    raise RuntimeError('new install requires --provider and --household; adoption requires an existing config')
                settings = {'household': args.household, 'instance': 'household', 'provider': args.provider, 'confirmation_policy': 'fresh', 'primary_recipe_library_id': 'builtin', 'recipe_libraries': [{'library_id': 'builtin', 'provider': 'builtin', 'read_only': False}]}
            paths.update(browser_paths(args, settings['provider']))
            code_root = (args.code_root or Path.home() / '.local/lib/meal-concierge' / args.name).expanduser().resolve()
            # No replaceable tree may contain any user data, or vice versa.
            for target in [home, *[Path(v) for k, v in paths.items() if k not in {'browser_binary', 'browser_executable'}]]:
                if target == code_root or target.is_relative_to(code_root) or code_root.is_relative_to(target):
                    raise RuntimeError('code and private data paths must be disjoint')
            meta = {'format': 1, 'home': str(home), 'code_root': str(code_root), 'name': args.name, 'manager': manager, 'unit': str(unit), 'paths': paths, 'runtime_path': os.environ.get('PATH', os.defpath)}
            if active(meta):
                raise RuntimeError('another service already uses this name')
        actual_settings = settings or json.loads(Path(meta['paths']['config']).read_text())
        socket_limit = 103 if platform.system() == 'Darwin' else 107
        socket_paths = [meta['paths']['socket']]
        if actual_settings.get('provider') != 'mathem':
            prefix = 'meal-concierge-meny-' if actual_settings.get('provider') == 'meny' else 'oda-household-'
            session = prefix + str(actual_settings.get('instance') or 'household')
            socket_paths.append(str(Path(meta['paths']['browser_socket_directory']) / (session + '.sock')))
        if any(len(os.fsencode(p)) > socket_limit for p in socket_paths):
            raise RuntimeError('Unix socket path is too long; choose shorter --socket / --browser-socket-directory paths')
        code_root = Path(meta['code_root'])
        with file_lock(code_root / '.installer.lock'):
            owner = code_root / 'owner.json'
            if owner.exists() and json.loads(owner.read_text()).get('home') != str(home):
                raise RuntimeError('code root belongs to a different installation')
            if not owner.exists():
                if (code_root / 'current').exists():
                    raise RuntimeError('unowned existing code root; use an empty code directory')
                write_json(owner, {'home': str(home)})
            publish(meta, path, home, settings, args.recipe_pack)


def publish(meta, path, home, settings, recipe_pack=None):
    release = stage_release(Path(meta['code_root']))
    archive = None
    pack_error = None
    try:
        archive = stage_recipe_pack(release, recipe_pack)
        if archive is not None:
            recipe_pack_command(release, 'preflight', archive)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        if recipe_pack is not None:
            raise  # Explicit invalid input never reaches database migration.
        archive = None
        pack_error = str(exc)
    with offline(meta):
        meta['paths']['maintenance'] = str(home / 'maintenance.json')
        # Persist the exact owner before any unit publication or data migration.
        # A killed first installation resumes with `update`, without rediscovery.
        write_json(home / 'pending-install.json', {**meta, 'initial_config': settings})
        if not Path(meta['paths']['config']).exists():
            write_json(meta['paths']['config'], settings)
        if Path(meta['paths']['state']).exists():
            destination = home / 'backups' / ('before-' + uuid.uuid4().hex)
            backup(meta, destination)
            print('Offline backup:', destination)
        write_json(home / 'maintenance.json', {'candidate': str(release), 'reason': 'offline update incomplete; repair before starting'})
    # The writing child owns its locks; parent death cannot free them early.
    # Maintenance remains set across this handoff and the subsequent publication.
    migrate(release, meta)
    with offline(meta):
        write_unit(meta)
        current = Path(meta['code_root']) / 'current'
        replacement = current.with_name('.current-' + uuid.uuid4().hex)
        replacement.symlink_to(release.name)
        os.replace(replacement, current)
        meta['previous_release'] = meta.get('release')
        meta['release'] = str(release)
        write_json(path, meta)
        (home / 'maintenance.json').unlink()
        (home / 'pending-install.json').unlink()
    # The complete core is usable if pack application cannot acquire ownership.
    # The child reacquires lifetime ownership before it writes; a competing core
    # startup makes application fail safely rather than sharing mutable state.
    if archive is not None:
        try:
            report = recipe_pack_command(release, 'apply', archive, meta)
            print('Recipe pack:', json.dumps({key: value for key, value in report.items() if key != 'results'}))
            if report['status'] != 'complete':
                pack_error = 'recipe pack import is incomplete; committed recipes remain available and reported conflicts were preserved'
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            pack_error = str(exc)
    print('Installed, stopped. Start explicitly; attach prints agent configuration without changing it.')
    if pack_error:
        raise RuntimeError('Runtime is usable; recipe pack needs attention: ' + pack_error + '. Retry update after addressing the reported problem.')
    if RECIPE_PACK is None:
        print('No recipe pack is pinned in this source build.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
