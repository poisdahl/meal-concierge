"""Real host PDF rendering in the pinned standalone runtime; no Poppler or OCR."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw

CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))
from pdf_pages import render


def text_pdf(path, count=3):
    """Small real PDF with an independently identifiable text mark on every page."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Count %d /Kids [%s] >>' %
                (count, ' '.join(f'{4 + 2*i} 0 R' for i in range(count)))).encode(),
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for index in range(count):
        objects.append(('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] '
                        '/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>' %
                        (5 + 2*index)).encode())
        content = f'BT /F1 24 Tf 30 150 Td (Recipe page {index + 1}) Tj ET'.encode()
        objects.append(b'<< /Length %d >>\nstream\n' % len(content) + content + b'\nendstream')
    data = b'%PDF-1.4\n'
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    data += f'trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(data)


class PdfPages(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mc-pdf-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_cli_reads_text_pdf_without_path_tools_and_preserves_original(self):
        source = self.root / "recipe ' $ unusual.pdf"
        text_pdf(source)
        original = source.read_bytes()
        result = subprocess.run([sys.executable, '-I', str(CORE / 'pdf_pages.py'), str(source),
                                 '--output', str(self.root / 'pages')], capture_output=True, text=True,
                                env={**os.environ, 'PATH': '/nonexistent'}, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['complete_document'])
        self.assertEqual(report['total_pages'], 3)
        self.assertEqual(report['source_sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(source.read_bytes(), original)
        hashes = []
        for page in report['pages']:
            with Image.open(page['image']) as image:
                self.assertEqual(image.size, (600, 600))
                self.assertLess(image.convert('L').getextrema()[0], 50)
            hashes.append(hashlib.sha256(Path(page['image']).read_bytes()).hexdigest())
        self.assertEqual(len(set(hashes)), 3)
        self.assertEqual(json.loads((self.root / 'pages/pages.json').read_text()), report)

    def test_scanned_twelve_page_pdf_includes_last_page(self):
        source = self.root / 'scan.pdf'
        images = []
        for number in range(1, 13):
            image = Image.new('RGB', (600, 800), (255, 255, 255))
            ImageDraw.Draw(image).rectangle((50, 50, 50 + number*20, 300), fill=(20, 80, 120))
            images.append(image)
        images[0].save(source, 'PDF', save_all=True, append_images=images[1:])
        self.addCleanup(lambda: [image.close() for image in images])
        report = render(source, self.root / 'scan-pages')
        self.assertTrue(report['complete_document'])
        self.assertEqual([page['page'] for page in report['pages']], list(range(1, 13)))
        with Image.open(report['pages'][-1]['image']) as image:
            self.assertLess(image.convert('L').getpixel((400, 400)), 150)

    def test_long_document_requires_explicit_batch_and_keeps_page_numbers(self):
        source = self.root / 'long.pdf'
        text_pdf(source, 21)
        with self.assertRaisesRegex(ValueError, '21 pages'):
            render(source, self.root / 'too-many')
        self.assertFalse((self.root / 'too-many').exists())
        report = render(source, self.root / 'selected', '20-21')
        self.assertFalse(report['complete_document'])
        self.assertEqual(report['total_pages'], 21)
        self.assertEqual([page['page'] for page in report['pages']], [20, 21])

    def test_bad_pdf_ranges_and_existing_output_do_not_replace_files(self):
        source = self.root / 'recipe.pdf'
        text_pdf(source)
        output = self.root / 'existing'
        output.mkdir()
        keep = output / 'keep'
        keep.write_text('unrelated')
        with self.assertRaises(FileExistsError):
            render(source, output)
        self.assertEqual(list(output.iterdir()), [keep])
        self.assertEqual(keep.read_text(), 'unrelated')
        for selection in ('0-2', '2-1', '1-4', '1,3'):
            with self.assertRaises(ValueError):
                render(source, self.root / 'invalid-range', selection)
        self.assertFalse((self.root / 'invalid-range').exists())
        source.write_bytes(b'not a PDF')
        with self.assertRaises(Exception):
            render(source, self.root / 'invalid-pdf')
        self.assertFalse((self.root / 'invalid-pdf').exists())

    def test_nonregular_and_oversized_inputs_fail_before_rendering(self):
        pipe = self.root / 'pipe.pdf'
        os.mkfifo(pipe)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            render(pipe, self.root / 'pipe-output')
        large = self.root / 'large.pdf'
        with large.open('wb') as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, '64 MiB'):
            render(large, self.root / 'large-output')
        self.assertFalse((self.root / 'pipe-output').exists())
        self.assertFalse((self.root / 'large-output').exists())


if __name__ == '__main__':
    unittest.main()
