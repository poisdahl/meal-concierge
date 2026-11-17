"""Reviewed cleanup of installer-owned artifacts under exclusive offline ownership."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from core import _atomic_json


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _entries(root):
    if root.is_symlink():
        raise RuntimeError('linked retention inventory root')
    if not root.exists():
        return []
    entries = []
    for path in root.iterdir():
        entries.append(path)
        if len(entries) > 10000:
            raise RuntimeError('retention inventory exceeds its bound')
    return sorted(entries)


def _snapshot(path, *, permit_links=False):
    """Pin every leaf without opening links, FIFOs or files on other mounts."""
    objects, size = {}, 0
    device = path.lstat().st_dev
    def walk(parent, name, relative):
        nonlocal size
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if info.st_dev != device or len(objects) >= 100000:
            raise RuntimeError('retention tree changed filesystem or exceeded its bound')
        marker = [info.st_dev, info.st_ino, info.st_mode]
        if stat.S_ISLNK(info.st_mode) and permit_links and relative.startswith('venv/'):
            objects[relative] = marker + [os.readlink(name, dir_fd=parent)]
            return
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError('irregular retention candidate')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(info.st_mode):
            flags |= os.O_DIRECTORY
        descriptor = os.open(name, flags, dir_fd=parent)
        try:
            actual = os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino, actual.st_mode) != tuple(marker):
                raise RuntimeError('retention candidate changed during inventory')
            if stat.S_ISDIR(info.st_mode):
                objects[relative] = marker
                for child in sorted(os.listdir(descriptor)):
                    walk(descriptor, child, (relative + '/' if relative else '') + child)
            else:
                digest = hashlib.sha256()
                for block in iter(lambda: os.read(descriptor, 1024 * 1024), b''):
                    digest.update(block)
                after = os.fstat(descriptor)
                if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    raise RuntimeError('retention file changed while reading')
                objects[relative] = marker + [info.st_size, info.st_mtime_ns, digest.hexdigest()]
                size += info.st_size
        finally:
            os.close(descriptor)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(parent, path.name, '')
    finally:
        os.close(parent)
    return objects, size


def _purge(parent, name, relative, objects):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if [info.st_dev, info.st_ino, info.st_mode] != objects[relative][:3]:
        raise RuntimeError('retention quarantine identity changed')
    if stat.S_ISDIR(info.st_mode):
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent)
        try:
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError('retention quarantine directory changed')
            for item in os.listdir(child):
                key = (relative + '/' if relative else '') + item
                if key not in objects:
                    raise RuntimeError('retention quarantine contains new work')
                _purge(child, item, key, objects)
            os.fsync(child)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=parent)
    else:
        if stat.S_ISREG(info.st_mode) and [info.st_size, info.st_mtime_ns] != objects[relative][3:5]:
            raise RuntimeError('retention quarantine file changed')
        os.unlink(name, dir_fd=parent)


def _roots(home, meta):
    return {'release': Path(meta['code_root']), 'failed_archive': Path(meta['code_root']), 'backup': home / 'backups',
            'preparation': home / 'recipe-preparations', 'failed_prepare': home / 'recipe-preparations',
            'failed_stage': Path(meta['paths']['state']) / '.recipe-pack-staging'}


def inventory(home, meta, days):
    from install import bounded_json, collection_generation, preparation_retention_preview, program_baseline
    if type(days) is not int or not 1 <= days <= 3650:
        raise RuntimeError('retention age must be 1 to 3650 days')
    roots = _roots(home, meta)
    code = roots['release']
    if code.is_symlink() or bounded_json(code / 'owner.json').get('home') != str(home):
        raise RuntimeError('retention requires the owned installation code root')
    for name in ('maintenance.json', 'pending-install.json', 'pending-store.json'):
        if (home / name).exists():
            raise RuntimeError('resolve interrupted maintenance before retention')
    protected = {Path(meta['release']).resolve(), (code / 'current').resolve()}
    if meta.get('previous_release'):
        protected.add(Path(meta['previous_release']).resolve())
    rr = home / 'rollback.json'
    if rr.exists():
        rec = bounded_json(rr, 32 * 1024 * 1024)
        protected.update(Path(value).resolve() for value in (rec['release'], rec['backup'], rec['previous_manifest']['release']))
    for receipt in _entries(home / 'program-preparations'):
        if receipt.is_symlink() or not re.fullmatch(r'[0-9a-f]{64}\.json', receipt.name):
            raise RuntimeError('unrecognized program preparation; verify before pruning')
        data = bounded_json(receipt)
        if data['baseline'] == program_baseline(meta):
            protected.add(Path(data['release']).resolve())
    cutoff = time.time() - days * 86400
    items = []
    def add(kind, path):
        if path.is_symlink() or path.resolve() in protected:
            return
        for value in [home, *meta['paths'].values()]:
            resolved = Path(value).resolve()
            if path.resolve() == resolved or resolved.is_relative_to(path.resolve()):
                return
        try:
            objects, size = _snapshot(path, permit_links=kind == 'release')
        except (OSError, RuntimeError):
            return
        identity = path.name
        if kind == 'preparation':
            identity = path.parent.name
        elif kind == 'failed_archive':
            identity = path.relative_to(code.resolve()).as_posix()
        items.append({'kind': kind, 'id': identity,
                      'bytes': size, 'sha256': _digest(objects), 'objects': objects})
    for row in preparation_retention_preview(home, meta, days)['candidates']:
        path = roots['preparation'] / row['prepared'] / 'archive.zip'
        if not path.parent.is_symlink():
            add('preparation', path)
    for kind, pattern in [('failed_prepare', r'\.prepare-[a-zA-Z0-9_\-]{6,32}'),
                          ('failed_stage', r'\.incoming-[a-zA-Z0-9_\-]{6,32}\.zip'),
                          ('release', r'release-[0-9a-f]{32}')]:
        for path in _entries(roots[kind]):
            if not re.fullmatch(pattern, path.name) or path.is_symlink() or path.lstat().st_mtime > cutoff:
                continue
            if kind == 'release' and not (path / 'build-identity.json').is_file():
                try:
                    if bounded_json(path / '.program-staging.json') != {
                            'format': 1, 'release': path.name, 'purpose': 'staging'}:
                        continue
                except (OSError, RuntimeError, ValueError):
                    continue  # Legacy stages without positive ownership evidence are kept.
            if kind == 'failed_prepare' and (not path.is_dir() or any(
                    not re.fullmatch(r'recipe-pack-[0-9a-f]{32}\.zip', p.name) for p in _entries(path))):
                continue
            if kind == 'failed_stage' and not path.is_file():
                continue
            add(kind, path)
    # The stopped importer copies into its installed release. A killed process
    # can leave this exact temporary filename behind inside a retained release.
    for release in protected:
        if (release.parent != code.resolve() or not re.fullmatch(r'release-[0-9a-f]{32}', release.name)
                or release.is_symlink() or not (release / 'build-identity.json').is_file()):
            continue
        for path in _entries(release):
            if (re.fullmatch(r'recipe-pack-[0-9a-f]{32}\.zip', path.name) and not path.is_symlink()
                    and path.is_file() and path.lstat().st_mtime <= cutoff):
                add('failed_archive', path)
    # Never discard the only copy of a historical external-effect journal.
    # Reclaim byte-equivalent backups while keeping at least one exact survivor.
    groups = {}
    for path in _entries(roots['backup']):
        if path.is_symlink() or not re.fullmatch(r'before-[0-9a-f]{32}', path.name):
            continue
        try:
            descriptor = bounded_json(path / 'backup.json')
            if descriptor.get('complete') is not True or descriptor.get('format') != 1:
                continue
            objects, _ = _snapshot(path)
            # Restorable private bytes and the backup format establish
            # equivalence. Descriptive release metadata and known installer
            # guards are separately protected by active recovery references.
            equivalent = []
            for key, value in sorted(objects.items()):
                if key == 'backup.json':
                    equivalent.append((key, value[2], _digest({k:v for k,v in descriptor.items() if k != 'release'})))
                    continue
                if key == 'state/.release-use.json':
                    lifecycle = bounded_json(path / key)
                    if (isinstance(lifecycle, dict) and set(lifecycle) == {'status','operation','release'}
                            and lifecycle['status'] in {'never_started','possibly_used','rolling_back'}
                            and isinstance(lifecycle['operation'], str) and re.fullmatch(r'[0-9a-f]{32}', lifecycle['operation'])
                            and isinstance(lifecycle['release'], str) and re.fullmatch(r'release-[0-9a-f]{32}', lifecycle['release'])):
                        continue
                if key in {'state/.release-use.lock','state/.service-owner.lock','state/state.lock',
                           'state/state.json.owner.lock','state/recipes.sqlite3.owner.lock'} and len(value) == 6 and value[3] == 0:
                    continue
                equivalent.append((key, value[2], value[-1] if len(value) == 6 else None))
            fingerprint = _digest(equivalent)
            groups.setdefault(fingerprint, []).append(path)
        except (OSError, RuntimeError, ValueError):
            continue
    anchors = {}
    for fingerprint, group in groups.items():
        kept = max(group, key=lambda p: (p.resolve() in protected, p.lstat().st_mtime_ns, p.name))
        anchors[fingerprint] = kept.name
        for path in group:
            if path != kept and path.lstat().st_mtime <= cutoff:
                add('backup', path)
    rows = sorted(items, key=lambda item: (item['kind'], item['id']))
    manifest = {'format': 1, 'installation': program_baseline(meta), 'days': days, 'rows': rows,
                'protection': _digest([sorted(map(str, protected)), collection_generation(meta), anchors])}
    return manifest


def public_preview(manifest):
    return {'dry_run': True, 'review': _digest(manifest), 'deleted': 0,
            'candidates': [{k: v for k, v in row.items() if k != 'objects'} for row in manifest['rows']]}


def apply(home, meta, days, review):
    from install import bounded_json, program_baseline
    if not isinstance(review, str) or not re.fullmatch(r'[0-9a-f]{64}', review):
        raise RuntimeError('retention requires the exact preview digest')
    root = home / 'retention-receipts'
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError('linked retention receipt directory')
    receipt = root / (review + '.json')
    if receipt.exists():
        saved = bounded_json(receipt, 64 * 1024 * 1024)
        manifest = saved['manifest']
        if _digest(manifest) != review or manifest['installation'] != program_baseline(meta) or days != manifest['days']:
            raise RuntimeError('retention receipt no longer matches the installation')
        if saved.get('complete'):
            return {'review': review, 'deleted': len(manifest['rows']), 'replayed': True}
    else:
        manifest = inventory(home, meta, days)
        if _digest(manifest) != review:
            raise RuntimeError('retention preview changed; review it again')
        saved = {'manifest': manifest, 'completed': []}
        if len(json.dumps(saved).encode()) > 16 * 1024 * 1024:
            raise RuntimeError('retention manifest exceeds its bound')
        _atomic_json(receipt, saved)
    roots = _roots(home, meta)
    # Recheck protected roots and current bindings even on resumed cleanup.
    current = inventory(home, meta, days)
    if manifest['protection'] != current['protection']:
        raise RuntimeError('protected releases, backups or collection generation changed')
    eligible = {(r['kind'], r['id']): r['sha256'] for r in current['rows']}
    for index, row in enumerate(manifest['rows']):
        if index in saved['completed']:
            continue
        kind, identity = row['kind'], row['id']
        parent = roots[kind] / identity if kind == 'preparation' else roots[kind]
        name = 'archive.zip' if kind == 'preparation' else identity
        if kind == 'failed_archive':
            release_name, archive_name = identity.split('/')
            if (not re.fullmatch(r'release-[0-9a-f]{32}', release_name)
                    or not re.fullmatch(r'recipe-pack-[0-9a-f]{32}\.zip', archive_name)):
                raise RuntimeError('invalid interrupted import staging identity')
            parent, name = roots[kind] / release_name, archive_name
        if parent.is_symlink() or roots[kind].is_symlink():
            raise RuntimeError('retention root changed')
        source = parent / name
        quarantine = parent / ('.retention-' + review + '-' + str(index))
        if quarantine.is_symlink():
            raise RuntimeError('retention quarantine is linked')
        if not quarantine.exists():
            if source.exists() or source.is_symlink():
                if eligible.get((kind, identity)) != row['sha256']:
                    raise RuntimeError('retention candidate changed or became protected')
                source.rename(quarantine)
            elif saved.get('deleting') != index:
                raise RuntimeError('retention candidate disappeared before quarantine')
        if quarantine.exists():
            objects, _ = _snapshot(quarantine, permit_links=kind == 'release')
            expected = row['objects']
            if objects != expected and (saved.get('deleting') != index or any(expected.get(k) != v for k, v in objects.items())):
                raise RuntimeError('retention candidate changed; retained in quarantine for recovery')
            saved['deleting'] = index
            _atomic_json(receipt, saved)
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _purge(descriptor, quarantine.name, '', objects)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        saved['completed'].append(index)
        _atomic_json(receipt, saved)
    saved['complete'] = True
    _atomic_json(receipt, saved)
    return {'review': review, 'deleted': len(saved['completed'])}
