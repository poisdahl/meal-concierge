"""Identity of the packaged bytes; status never needs Git or network access."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

MANIFEST = 'build-identity.json'
INTERFACE_VERSION = 1


def create(source, release, python_version):
    source, release = Path(source), Path(release)
    paths = sorted([*release.glob('*.py'), release / 'runtime-requirements.txt',
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
