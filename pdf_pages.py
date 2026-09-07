#!/usr/bin/env python3
"""Render a host's PDF attachment for native image reading; never import or save recipes."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 64 * 1024 * 1024
MAX_PAGES = 20
MAX_EDGE = 2400


def render(source: Path, output: Path, pages: str | None = None) -> dict:
    # Nonblocking open also lets us reject a pipe/device before trying to read it.
    descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('PDF input must be a regular file')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('PDF input exceeds 64 MiB')

    import pypdfium2 as pdfium

    with pdfium.PdfDocument(data) as document:
        document.init_forms()
        total = len(document)
        first, last = 1, total
        if pages is not None:
            match = re.fullmatch(r'([1-9][0-9]*)-([1-9][0-9]*)', pages)
            if not match:
                raise ValueError('pages must be a one-based range such as 1-12')
            first, last = map(int, match.groups())
        if not 1 <= first <= last <= total:
            raise ValueError(f'page range is outside this {total}-page PDF')
        if last - first + 1 > MAX_PAGES:
            raise ValueError(f'PDF has {total} pages; use --pages FIRST-LAST for at most 20 pages per batch')

        output = output.absolute()
        output.mkdir(mode=0o700, exist_ok=False)
        rendered = []
        for number in range(first, last + 1):
            with closing(document[number - 1]) as page:
                width, height = page.get_size()
                if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                    raise ValueError(f'page {number} has invalid dimensions')
                scale = min(2.0, MAX_EDGE / max(width, height))
                with closing(page.render(scale=scale, draw_annots=True)) as bitmap:
                    with bitmap.to_pil() as image:
                        destination = output / f'page-{number:04d}.png'
                        with destination.open('xb') as stream:
                            image.save(stream, format='PNG')
                        rendered.append({'page': number, 'image': str(destination),
                                         'width': image.width, 'height': image.height,
                                         'scale': scale})
        result = {'source_sha256': hashlib.sha256(data).hexdigest(),
                  'total_pages': total, 'complete_document': first == 1 and last == total,
                  'pages': rendered}
        # A failed render leaves no success manifest; retained partial PNGs are not an import.
        (output / 'pages.json').write_text(json.dumps(result, indent=2) + '\n')
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='new directory for page images')
    parser.add_argument('--pages', help='one-based inclusive range, at most 20 pages')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = render(args.source, args.output, args.pages)
    except Exception as exc:
        parser.exit(1, f'Cannot render PDF: {exc}\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
