"""Move a reviewed local recipe pack through size-limited file transports.

The expected whole-archive size and digest must come from the approval record,
not from the transferred manifest. This tool never imports a pack.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


DEFAULT_PART_BYTES = 64 * 1024 * 1024
MAX_PART_BYTES = 95 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PARTS = 4096
PART_NAME = re.compile(r'part-[0-9]{6}')
DIGEST = re.compile(r'[0-9a-f]{64}')


def regular_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'not a regular file: {path}')


def check_expected(size, digest, expected_bytes, expected_sha256):
    if size != expected_bytes or digest != expected_sha256:
        raise ValueError('whole recipe pack differs from approved size/SHA-256')


def split(source, parts_dir, expected_bytes, expected_sha256, max_part_bytes):
    regular_file(source)
    if not 1 <= max_part_bytes <= MAX_PART_BYTES:
        raise ValueError(f'max part bytes must be 1..{MAX_PART_BYTES}')
    parts_dir.mkdir(parents=True, exist_ok=False)
    whole = hashlib.sha256()
    total = 0
    parts = []
    with source.open('rb') as incoming:
        while True:
            first = incoming.read(1)
            if not first:
                break
            name = f'part-{len(parts):06d}'
            if len(parts) >= MAX_PARTS:
                raise ValueError('too many parts')
            size = 1
            digest = hashlib.sha256(first)
            whole.update(first)
            with (parts_dir / name).open('xb') as outgoing:
                outgoing.write(first)
                while size < max_part_bytes:
                    block = incoming.read(min(1024 * 1024, max_part_bytes - size))
                    if not block:
                        break
                    outgoing.write(block)
                    digest.update(block)
                    whole.update(block)
                    size += len(block)
            total += size
            parts.append({'name': name, 'bytes': size, 'sha256': digest.hexdigest()})
    check_expected(total, whole.hexdigest(), expected_bytes, expected_sha256)
    manifest = {'format': 1, 'filename': source.name, 'bytes': total,
                'sha256': expected_sha256, 'parts': parts}
    (parts_dir / 'pack-transfer.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return {'parts': len(parts), 'bytes': total, 'sha256': expected_sha256}


def assemble(manifest_path, output, expected_bytes, expected_sha256):
    regular_file(manifest_path)
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('transfer manifest too large')
    manifest = json.loads(manifest_path.read_text())
    if (not isinstance(manifest, dict) or manifest.get('format') != 1
            or manifest.get('bytes') != expected_bytes
            or manifest.get('sha256') != expected_sha256):
        raise ValueError('transfer manifest differs from approved archive')
    parts = manifest.get('parts')
    if not isinstance(parts, list) or not parts or len(parts) > MAX_PARTS:
        raise ValueError('invalid transfer parts')
    declared = 0
    for index, entry in enumerate(parts):
        name = entry.get('name') if isinstance(entry, dict) else None
        if name != f'part-{index:06d}' or not PART_NAME.fullmatch(name):
            raise ValueError('missing, reordered, or unsafe transfer part')
        if (type(entry.get('bytes')) is not int or not 1 <= entry['bytes'] <= MAX_PART_BYTES
                or not isinstance(entry.get('sha256'), str)
                or not DIGEST.fullmatch(entry['sha256'])):
            raise ValueError(f'invalid transfer part metadata: {name}')
        declared += entry['bytes']
    if declared != expected_bytes:
        raise ValueError('transfer part sizes differ from approved archive')
    if output.exists() or output.is_symlink():
        raise ValueError(f'output already exists: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    whole = hashlib.sha256()
    total = 0
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.recipe-pack-', delete=False) as outgoing:
            temporary = Path(outgoing.name)
            for index, entry in enumerate(parts):
                name = entry['name']
                path = manifest_path.parent / name
                regular_file(path)
                part_hash = hashlib.sha256()
                part_size = 0
                with path.open('rb') as incoming:
                    while block := incoming.read(1024 * 1024):
                        part_size += len(block)
                        if part_size > entry['bytes'] or total + part_size > expected_bytes:
                            raise ValueError(f'transfer part exceeds approved size: {name}')
                        outgoing.write(block)
                        whole.update(block)
                        part_hash.update(block)
                if part_size != entry.get('bytes') or part_hash.hexdigest() != entry.get('sha256'):
                    raise ValueError(f'transfer part failed verification: {name}')
                total += part_size
            outgoing.flush()
            os.fsync(outgoing.fileno())
        check_expected(total, whole.hexdigest(), expected_bytes, expected_sha256)
        os.link(temporary, output)
        return {'output': str(output), 'parts': len(parts), 'bytes': total,
                'sha256': expected_sha256}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('split', 'assemble'):
        command = commands.add_parser(action)
        command.add_argument('--expected-bytes', type=int, required=True)
        command.add_argument('--expected-sha256', required=True)
        if action == 'split':
            command.add_argument('--source', type=Path, required=True)
            command.add_argument('--parts-dir', type=Path, required=True)
            command.add_argument('--max-part-bytes', type=int, default=DEFAULT_PART_BYTES)
        else:
            command.add_argument('--manifest', type=Path, required=True)
            command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.expected_bytes <= 0 or not DIGEST.fullmatch(args.expected_sha256):
        parser.error('approved size must be positive and SHA-256 must be lowercase hex')
    if args.action == 'split':
        result = split(args.source, args.parts_dir, args.expected_bytes,
                       args.expected_sha256, args.max_part_bytes)
    else:
        result = assemble(args.manifest, args.output, args.expected_bytes,
                          args.expected_sha256)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
