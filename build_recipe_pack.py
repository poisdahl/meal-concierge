"""Reproducible offline pack builder; never reads a household bank or fetches URLs."""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time
import unicodedata

from recipe_pack_sources import SourceHTML, SourceParseError, attribution_links, mealdb_recipe, plain, readiness, wikibooks_recipe

FORMAT = 'meal-concierge-recipes'
NORMALIZER_VERSION = '1'
RIGHTS_POLICY = 'wikibooks-cc-text-explicit-cc-pd-images-v1'
MAX_RECORD_BYTES = 512 * 1024
MAX_SOURCE_BODY = 32 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * MAX_ARCHIVE_BYTES


class PackBuildError(ValueError):
    pass


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def confined(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if not relative or parts.is_absolute() or str(parts) != relative or any(p in {'.', '..'} for p in parts.parts) or '\\' in relative:
        raise PackBuildError('invalid relative path')
    target = root
    for part in parts.parts:
        target = target / part
        if target.is_symlink():
            raise PackBuildError('symlink paths are unsupported')
    return target


def absolute_root(root: Path) -> Path:
    if not root.is_absolute():
        raise PackBuildError('input and output roots must be absolute')
    # macOS exposes its standard temporary paths through system symlinks. Resolve
    # only those fixed aliases, then reject all task-controlled symlink segments.
    for alias in (Path('/tmp'), Path('/var')):
        if (root == alias or alias in root.parents) and alias.is_symlink() and alias.resolve() == Path('/private') / alias.name:
            root = alias.resolve() / root.relative_to(alias)
    confined(Path('/'), str(root).lstrip('/'))
    return root


def read_file(root, relative, maximum, expected=None):
    path = confined(root, relative)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        mode = os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or mode.st_size > maximum:
            raise PackBuildError('input is not a bounded regular file')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise PackBuildError('input exceeded byte limit')
    if expected and (len(data) != expected['bytes'] or digest(data) != expected['sha256']):
        raise PackBuildError('source or output checksum mismatch')
    return data


def load_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PackBuildError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(PackBuildError('nonfinite JSON')))


def write_file(root, relative, data):
    path = confined(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'path': relative, 'bytes': len(data), 'sha256': digest(data)}


def fingerprint():
    modules = ['recipes', 'recipe_quantities', 'recipe_assets', 'recipe_pack_sources', 'recipe_portable']
    values = {name: digest(Path(importlib.import_module(name).__file__).read_bytes()) for name in modules}
    values['builder'] = digest(Path(__file__).read_bytes())
    for name in ['PIL', 'simplejpeg', 'numpy']:
        values[name] = importlib.import_module(name).__version__
    values['python'] = '.'.join(map(str, sys.version_info[:3]))
    values['unicode'] = unicodedata.unidata_version
    return values


def _image_credit(image):
    metadata = image.get('license_metadata', {})
    values = {key: plain(str(metadata[key].get('value', ''))) for key in
              ('Artist', 'Credit', 'LicenseShortName', 'LicenseUrl', 'UsageTerms', 'Permission', 'Attribution', 'Restrictions', 'Copyrighted', 'License') if key in metadata}
    values['links'] = attribution_links(SourceHTML(' '.join(str(metadata[key].get('value', '')) for key in values)).root)
    license_name = values.get('LicenseShortName', '')
    license_url = values.get('LicenseUrl') or None
    if license_url and license_url.startswith('http://creativecommons.org/'):
        license_url = 'https://' + license_url.removeprefix('http://')
    permitted = bool(re.fullmatch(r'CC BY(?:-SA)? (?:1\.0|2\.0|2\.5|3\.0|4\.0)(?: [a-z]{2})?', license_name)
                     and license_url and license_url.startswith('https://creativecommons.org/licenses/'))
    permitted |= license_name == 'CC0' and bool(license_url and license_url.startswith('https://creativecommons.org/publicdomain/zero/'))
    permitted |= license_name == 'Public domain' and values.get('Copyrighted', '').casefold() == 'false'
    if values.get('Restrictions'):
        permitted = False
    if not permitted:
        return None, values
    # The full ordinary notices remain in attribution.json if the wire field is
    # shorter; never truncate the only copy of a required credit.
    credit = values.get('Credit') or values.get('Attribution') or None
    record = {'alt': None, 'source_url': image.get('description_url') or image.get('source_url'),
              'creator': values.get('Artist') or None, 'credit': credit,
              'license': license_name, 'license_url': license_url,
              'changes': 'EXIF orientation applied; resized to at most 1600 pixels; JPEG quality 85; embedded metadata removed.'}
    if any(len(record[key] or '') > 500 for key in ('creator', 'credit', 'license')):
        return None, values
    return record, values


class Covers:
    """Read exact managed bytes from an explicitly reviewed derivative handoff."""

    def __init__(self, root, expected, snapshot_sha256, entries):
        root = absolute_root(root)
        raw = read_file(root, 'covers-manifest.json', MAX_SOURCE_BODY)
        if not expected or digest(raw) != expected:
            raise PackBuildError('reviewed cover manifest checksum mismatch')
        manifest = load_json(raw)
        if manifest.get('schema') != 'meal-concierge-derived-covers/1' or manifest.get('status') != 'complete' or manifest.get('source_snapshot_sha256') != snapshot_sha256:
            raise PackBuildError('cover manifest is incomplete or belongs to another snapshot')
        if manifest.get('profile', {}).get('id') != 'managed-jpeg-960-q85-v1':
            raise PackBuildError('cover processing profile is unsupported')
        associations = manifest.get('recipes')
        if not isinstance(associations, list) or len(associations) > MAX_ENTRIES:
            raise PackBuildError('cover association count exceeds bounds')
        self.rows = {}
        for row in associations:
            key = (row['source'], row['source_id'])
            if key in self.rows:
                raise PackBuildError('duplicate cover recipe identity')
            self.rows[key] = row
        recipes = {(e['source'], e['source_id']): e for e in entries if e['classification'] == 'recipe'}
        if self.rows.keys() != recipes.keys():
            raise PackBuildError('cover association scope differs from recipe snapshot')
        self.root = root
        self.manifest = manifest
        self.manifest_sha256 = expected
        self.files = {}
        for key, entry in recipes.items():
            row = self.rows[key]
            if row.get('revision') != entry.get('revision') or row.get('source_raw_sha256') != entry['raw']['sha256'] or row.get('source_rendered_sha256') != entry.get('rendered', {}).get('sha256'):
                raise PackBuildError('cover association source revision differs')
            if not entry.get('image'):
                if row.get('status') != 'no_source_cover':
                    raise PackBuildError('cover association invents a source image')
                continue
            original = entry['image']['file']['sha256']
            if row.get('status') != 'complete' or row.get('source_original_sha256') != original:
                raise PackBuildError('cover original image differs or is incomplete')
            asset = manifest['assets'][original]
            if any(row.get(k) != asset.get(k) for k in ('asset_id', 'path', 'sha256', 'bytes')):
                raise PackBuildError('cover asset and association differ')
            if row['asset_id'] != 'sha256:' + row['sha256'] or row['path'] != 'assets/' + row['sha256'] + '.jpg' or not re.fullmatch('[0-9a-f]{64}', row['sha256']):
                raise PackBuildError('cover asset identity is invalid')
            self.files[row['asset_id']] = row

    def read(self, asset_id):
        from recipe_assets import validate_managed, MAX_ASSET_BYTES
        row = self.files[asset_id]
        data = read_file(self.root, row['path'], MAX_ASSET_BYTES, row)
        validate_managed(data, asset_id)
        return data


def _build(snapshot: Path, output: Path, *, snapshot_sha256: str, pack_version: str, stop_after=None, covers_root=None, covers_manifest_sha256=None):
    from recipe_assets import RecipeAssetError
    from recipe_portable import write_archive
    from recipes import RecipeError, normalize_recipe
    started = time.monotonic()
    # Resolve only after rejecting symlinks in every root component.
    for root in (snapshot, output):
        if not root.is_absolute():
            raise PackBuildError('snapshot and output must be absolute')
        confined(Path('/'), str(root).lstrip('/'))
    if snapshot == output or snapshot in output.parents or output in snapshot.parents:
        raise PackBuildError('input and output must be disjoint')
    if not re.fullmatch(r'[a-f0-9]{64}', snapshot_sha256):
        raise PackBuildError('expected snapshot SHA-256 is required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', pack_version):
        raise PackBuildError('invalid pack version')
    read_file(snapshot, 'SEALED', 4096)
    source_bytes = read_file(snapshot, 'snapshot.json', MAX_SOURCE_BODY)
    if digest(source_bytes) != snapshot_sha256:
        raise PackBuildError('sealed snapshot digest mismatch')
    source = load_json(source_bytes)
    entries = []
    for name in ('wikibooks', 'themealdb'):
        filename = name + '-manifest.json'
        rows = load_json(read_file(snapshot, filename, MAX_SOURCE_BODY, source['files'][filename]))
        if not isinstance(rows, list) or len(rows) > MAX_ENTRIES:
            raise PackBuildError('source manifest exceeds record bound')
        for row in rows:
            if row.get('source') != name or not re.fullmatch(r'\d{1,20}', row.get('source_id', '')):
                raise PackBuildError('source identity mismatch')
        entries.extend(rows)
    identities = [(e['source'], e['source_id']) for e in entries]
    if len(entries) > MAX_ENTRIES or len(set(identities)) != len(entries):
        raise PackBuildError('duplicate or excessive source identities')
    covers = Covers(covers_root, covers_manifest_sha256, snapshot_sha256, entries) if covers_root else None
    if covers_manifest_sha256 and covers is None:
        raise PackBuildError('cover checksum requires its root')
    versions = fingerprint()
    if covers:
        versions['covers_manifest_sha256'] = covers.manifest_sha256
    run_key = digest(encoded({'snapshot': snapshot_sha256, 'versions': versions, 'policy': RIGHTS_POLICY, 'pack_version': pack_version}))
    output.mkdir(parents=True, exist_ok=True)
    cache = f'cache/{run_key}'
    coverage, attribution_paths, record_paths = [], [], []
    attribution_bytes = records_bytes = coverage_bytes = 0
    counts = Counter()
    asset_ids = set()
    reused = 0
    for index, entry in enumerate(sorted(entries, key=lambda e: (e['source'], int(e['source_id'])))):
        identity = entry['source'] + ':' + entry['source_id']
        base = {'source': entry['source'], 'source_id': entry['source_id'], 'url': entry['url'], 'classification': entry['classification']}
        if entry['classification'] != 'recipe':
            base['status'] = 'excluded_' + entry['classification']
            coverage_bytes += len(encoded(base)) + 1
            if coverage_bytes > 16 * 1024 * 1024:
                raise PackBuildError('coverage exceeds portable report bound')
            coverage.append(base)
            counts[(entry['source'], base['status'])] += 1
            continue
        # Verify every consumed original each run, even when normalized output is cached.
        payloads = {kind: load_json(read_file(snapshot, entry[kind]['path'], MAX_SOURCE_BODY, entry[kind])) for kind in ('raw', 'rendered') if kind in entry}
        key = digest(encoded({'entry': entry, 'run': run_key}))
        filename = f'{cache}/{entry["source"]}-{entry["source_id"]}.json'
        cached = None
        try:
            stored = load_json(read_file(output, filename, 2 * MAX_RECORD_BYTES))
            if stored['key'] == key and stored['sha256'] == digest(encoded(stored['result'])):
                cached = stored['result']
                if cached.get('image_status') == 'invalid_derivative':
                    cached = None
                else:
                    reused += 1
        except (FileNotFoundError, ValueError, KeyError):
            cached = None
        if cached is None:
            try:
                reader = wikibooks_recipe if entry['source'] == 'wikibooks' else mealdb_recipe
                recipe, credit = reader(entry, payloads.get('rendered', payloads['raw']))
                status, reasons = readiness(recipe)
                reasons.extend(credit.get('normalization_issues', []))
                if reasons:
                    status = 'draft'
                image_status = entry.get('image_status') or 'no_candidate'
                if entry.get('image'):
                    image_record, notices = _image_credit(entry['image'])
                    credit['image_notices'] = notices
                    credit['image_source_url'] = entry['image'].get('description_url') or entry['image'].get('source_url')
                    image_status = 'rights_unresolved'
                    if image_record:
                        image_status = 'awaiting_reviewed_derivative'
                        if covers:
                            row = covers.rows[(entry['source'], entry['source_id'])]
                            try:
                                covers.read(row['asset_id'])
                                image_record['asset_id'] = row['asset_id']
                                image_record['changes'] = 'Primary frame selected; EXIF orientation applied and embedded ICC converted to sRGB when present; at most 960 pixels; JPEG quality 85; embedded metadata removed.'
                                recipe['image'] = image_record
                                recipe = normalize_recipe(recipe)
                                credit['image_processing'] = covers.manifest['assets'][row['source_original_sha256']]['processing']
                                credit['image_profile_sha256'] = covers.manifest['profile_sha256']
                                image_status = 'included'
                            except (OSError, PackBuildError, RecipeAssetError, RecipeError):
                                recipe['image'] = None
                                image_status = 'invalid_derivative'
                                credit['image_error'] = 'managed_cover_unavailable_or_invalid'
                cached = {'recipe': recipe, 'status': status, 'reasons': reasons, 'credit': credit, 'image_status': image_status}
            except (SourceParseError, RecipeError, KeyError, TypeError, ValueError) as exc:
                cached = {'status': 'failed_parse', 'error': str(exc)}
            cached_bytes = encoded({'key': key, 'sha256': digest(encoded(cached)), 'result': cached})
            if len(cached_bytes) > 2 * MAX_RECORD_BYTES:
                raise PackBuildError('normalized source entry exceeds cache bound')
            write_file(output, filename, cached_bytes)
        status = cached['status']
        counts[(entry['source'], status)] += 1
        base.update({k: v for k, v in cached.items() if k not in {'recipe', 'credit'}})
        if 'recipe' in cached:
            counts[(entry['source'], 'image_' + cached['image_status'])] += 1
            public = entry['source'] == 'wikibooks' and cached['credit']['text_rights'] == 'CC-BY-SA-4.0'
            base['distribution'] = 'included' if public else 'excluded_rights'
            counts[(entry['source'], base['distribution'])] += 1
            if public:
                envelope = {'recipe_id': identity, 'status': status, 'recipe': cached['recipe']}
                if len(encoded(envelope)) > MAX_RECORD_BYTES:
                    raise PackBuildError('normalized envelope exceeds limit')
                envelope_bytes = encoded(envelope)
                records_bytes += len(envelope_bytes)
                if records_bytes > 512 * 1024 * 1024:
                    raise PackBuildError('records exceed portable stream bound')
                record_path = f'{cache}/records/{entry["source"]}-{entry["source_id"]}.json'
                write_file(output, record_path, envelope_bytes)
                record_paths.append(record_path)
                attribution_bytes += len(encoded(cached['credit'])) + len(encoded(identity)) + 2
                if attribution_bytes > 16 * 1024 * 1024:
                    raise PackBuildError('attribution exceeds portable report bound')
                attribution_paths.append((identity, filename))
                if cached['recipe'].get('image'):
                    asset_ids.add(cached['recipe']['image']['asset_id'])
            else:
                base['rights_reason'] = cached['credit']['text_rights']
        coverage_bytes += len(encoded(base)) + 1
        if coverage_bytes > 16 * 1024 * 1024:
            raise PackBuildError('coverage exceeds portable report bound')
        coverage.append(base)
        if stop_after is not None and index + 1 >= stop_after:
            return {'complete': False, 'processed': len(coverage), 'cache_reused': reused}
    release = f'{cache}/release'
    files = []
    def put(name, data):
        info = write_file(output, f'{release}/{name}', data)
        info['path'] = name
        files.append(info)
    records_path = confined(output, release + '/records.jsonl')
    records_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='records-', dir=records_path.parent)
    record_hash = hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as stream:
            for relative in record_paths:
                data = read_file(output, relative, MAX_RECORD_BYTES)
                stream.write(data)
                record_hash.update(data)
        os.replace(temporary, records_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    files.append({'path': 'records.jsonl', 'bytes': records_bytes, 'sha256': record_hash.hexdigest()})
    attribution_path = confined(output, release + '/attribution.json')
    fd, temporary = tempfile.mkstemp(prefix='attribution-', dir=attribution_path.parent)
    attribution_hash = hashlib.sha256()
    attribution_size = 0
    try:
        with os.fdopen(fd, 'wb') as stream:
            def emit(data):
                nonlocal attribution_size
                attribution_size += len(data)
                if attribution_size > 16 * 1024 * 1024:
                    raise PackBuildError('attribution exceeds portable report bound')
                stream.write(data)
                attribution_hash.update(data)
            emit(b'{')
            for n, (identity, relative) in enumerate(attribution_paths):
                cached = load_json(read_file(output, relative, 2 * MAX_RECORD_BYTES))
                emit((b',' if n else b'') + encoded(identity).rstrip(b'\n') + b':' + encoded(cached['result']['credit']).rstrip(b'\n'))
            emit(b'}\n')
        os.replace(temporary, attribution_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    files.append({'path': 'attribution.json', 'bytes': attribution_size, 'sha256': attribution_hash.hexdigest()})
    put('coverage.json', encoded(coverage))
    expanded = sum(f['bytes'] for f in files)
    for asset_id in sorted(asset_ids):
        data = covers.read(asset_id)
        expanded += len(data)
        if expanded > MAX_EXPANDED_BYTES:
            raise PackBuildError('pack exceeds expanded limit before staging asset')
        put('assets/' + asset_id.removeprefix('sha256:') + '.jpg', data)
    manifest = {'format': FORMAT, 'format_version': 1, 'kind': 'bundled',
                'pack_id': 'wikibooks-themealdb-en', 'pack_version': pack_version,
                'recipe_schema_version': 2, 'normalizer_version': NORMALIZER_VERSION,
                'source_snapshot': {'id': source['snapshot_id'], 'sha256': snapshot_sha256},
                'scope': source['scope'], 'source_limitations': source['source_limitations'],
                'rights_policy': RIGHTS_POLICY, 'build_versions': versions,
                'records_count': len(record_paths), 'counts': {f'{k[0]}.{k[1]}': v for k, v in sorted(counts.items())},
                'files': files}
    if sum(f['bytes'] for f in files) > MAX_EXPANDED_BYTES:
        raise PackBuildError('pack exceeds expanded limit')
    archive_name = f'meal-concierge-recipes-{pack_version}.zip'
    with tempfile.TemporaryDirectory(prefix='archive-', dir=output) as staging:
        temp = Path(staging) / archive_name
        manifest = write_archive(temp, manifest, {f['path']: confined(output, f'{release}/{f["path"]}') for f in files})
        os.replace(temp, confined(output, archive_name))
    result = {'complete': True, 'archive': archive_name, 'archive_bytes': (output / archive_name).stat().st_size,
              'expanded_bytes': sum(f['bytes'] for f in files) + len(encoded(manifest)),
              'records': len(record_paths), 'assets': len(asset_ids), 'counts': manifest['counts'],
              'cache_reused': reused, 'seconds': round(time.monotonic() - started, 3)}
    write_file(output, 'build-report.json', encoded(result))
    return result


def build(snapshot: Path, output: Path, **options):
    snapshot, output = absolute_root(snapshot), absolute_root(output)
    if snapshot == output or snapshot in output.parents or output in snapshot.parents:
        raise PackBuildError('input and output must be disjoint')
    covers_root = options.get('covers_root')
    if covers_root:
        covers_root = options['covers_root'] = absolute_root(covers_root)
    if covers_root and (covers_root == output or covers_root in output.parents or output in covers_root.parents):
        raise PackBuildError('cover input and output must be disjoint')
    output.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(confined(output, '.build.lock'), os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PackBuildError('another builder owns this output root') from exc
        return _build(snapshot, output, **options)
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--snapshot-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pack-version', required=True)
    parser.add_argument('--covers-root', type=Path)
    parser.add_argument('--covers-manifest-sha256')
    args = parser.parse_args()
    print(json.dumps(build(args.snapshot, args.output, snapshot_sha256=args.snapshot_sha256, pack_version=args.pack_version,
                           covers_root=args.covers_root, covers_manifest_sha256=args.covers_manifest_sha256), indent=2))


if __name__ == '__main__':
    main()
