"""Identity of the packaged bytes; status never needs Git or network access."""
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import subprocess

MANIFEST = 'build-identity.json'
INTERFACE_VERSION = 1


def verify_release_layout(release):
    # stage_release ships only flat Python modules and these three directories.
    # An additional importable package or native module changes what Python can load.
    for entry in release.iterdir():
        if (entry.is_symlink()
                or (entry.is_dir() and entry.name not in {'venv', 'skill', '__pycache__'})
                or entry.suffix.casefold() in {'.so', '.pyd', '.dylib'}):
            raise RuntimeError('prepared program bytes changed; unexpected release entry')


def installed_runtime_digest(release):
    """Bind installed distributions, configuration, entry points and interpreter."""
    venv = Path(release) / 'venv'
    if not venv.exists():
        return None
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(venv):
        dirs.sort()
        for name in sorted([*files, *dirs]):
            path = Path(root) / name
            info = path.lstat()
            relative = path.relative_to(venv).as_posix()
            digest.update(json.dumps([relative, info.st_mode], separators=(',', ':')).encode() + b'\x00')
            if stat.S_ISLNK(info.st_mode):
                if '__pycache__' in path.parts or path.suffix == '.pyc':
                    raise ValueError('runtime bytecode cache cannot be linked')
                digest.update(os.fsencode(os.readlink(path)) + b'\x00')
                if relative != 'bin/python':
                    continue
            elif stat.S_ISDIR(info.st_mode):
                continue
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError('runtime contains an unsupported filesystem entry')
            with path.open('rb') as content:
                file_digest = hashlib.sha256()
                for chunk in iter(lambda: content.read(1024 * 1024), b''):
                    file_digest.update(chunk)
                digest.update(file_digest.digest())
    return digest.hexdigest()


def create(source, release, python_version):
    source, release = Path(source), Path(release)
    cache = release / '__pycache__'
    if cache.is_symlink():
        raise ValueError('program bytecode cache cannot be linked')
    cached_files = [*release.glob('*.pyc'), *cache.glob('*')]
    if any(p.is_symlink() or not p.is_file() for p in cached_files):
        raise ValueError('program bytecode cache contains an irregular file')
    paths = sorted([*release.glob('*.py'), *cached_files, release / 'runtime-requirements.txt',
                    *(p for p in (release / 'skill').rglob('*') if p.is_file())],
                   key=lambda p: p.relative_to(release).as_posix())
    digest = hashlib.sha256()
    for path in paths:
        name = path.relative_to(release).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, 'big') + name)
        digest.update(len(content).to_bytes(8, 'big') + content)
    commit, dirty, release_tag = None, None, None
    try:
        def git(*args):
            return subprocess.run(['git', '-C', str(source), *args], check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        # An exported directory nested inside another repository is not that build.
        if Path(git('rev-parse', '--show-toplevel')).resolve() == source.resolve():
            commit = git('rev-parse', 'HEAD')
            dirty = bool(git('status', '--porcelain', '--untracked-files=all', '--',
                             '*.py', 'runtime-requirements.txt', 'skill'))
            # Only exact stable program tags identify a release base. An untagged
            # checkout may simply lack tags; never infer that it is newer.
            tags = [tag for tag in git('tag', '--points-at', 'HEAD').splitlines()
                    if re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag)]
            release_tag = max(tags, key=lambda tag: tuple(map(int, tag[1:].split('.'))), default=None)
    except (OSError, subprocess.SubprocessError):
        pass
    return {'format': 1, 'release_tag': release_tag,
            'source_commit': commit, 'source_modified': dirty,
            'source_sha256': digest.hexdigest(),
            'installed_runtime_sha256': installed_runtime_digest(release),
            'dependencies_sha256': hashlib.sha256((release / 'runtime-requirements.txt').read_bytes()).hexdigest(),
            'python': python_version, 'interface_version': INTERFACE_VERSION,
            'identity_status': 'recorded'}


def read(directory):
    try:
        value = json.loads((Path(directory) / MANIFEST).read_text())
        if not isinstance(value, dict) or value.get('format') != 1:
            raise ValueError('unsupported build identity')
        return value
    except (OSError, ValueError):
        return {'identity_status': 'unavailable'}


def at_start(directory):
    """Capture the verified program/runtime the process actually opened."""
    declared = read(directory)
    if not declared.get('installed_runtime_sha256'):
        return declared
    try:
        computed = create(directory, directory, declared['python'])
        if all(computed[key] == declared.get(key) for key in (
                'source_sha256', 'dependencies_sha256', 'installed_runtime_sha256')):
            return declared
    except (OSError, ValueError, KeyError):
        pass
    return {'identity_status': 'unverified'}
