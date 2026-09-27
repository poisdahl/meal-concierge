"""A size-limited host cannot expose a partial local pack to the importer."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from recipe_pack_transfer import assemble, split


class RecipePackTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'approved.zip'
        self.content = bytes(range(256)) * 5
        self.source.write_bytes(self.content)
        self.size = len(self.content)
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.parts = self.root / 'parts'
        split(self.source, self.parts, self.size, self.digest, 500)
        self.output = self.root / 'received.zip'

    def test_exact_reassembly(self):
        result = assemble(self.parts / 'pack-transfer.json', self.output,
                          self.size, self.digest)
        self.assertEqual(result['parts'], 3)
        self.assertEqual(self.output.read_bytes(), self.content)

    def test_missing_or_corrupt_part_never_exposes_output(self):
        part = self.parts / 'part-000001'
        original = part.read_bytes()
        part.write_bytes(original[:-1])
        with self.assertRaisesRegex(ValueError, 'part failed verification'):
            assemble(self.parts / 'pack-transfer.json', self.output,
                     self.size, self.digest)
        self.assertFalse(self.output.exists())
        part.unlink()
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            assemble(self.parts / 'pack-transfer.json', self.output,
                     self.size, self.digest)
        self.assertFalse(self.output.exists())

    def test_oversized_part_and_manifest_never_expose_output(self):
        part = self.parts / 'part-000001'
        with part.open('ab') as outgoing:
            outgoing.write(b'unapproved extra bytes')
        with self.assertRaisesRegex(ValueError, 'exceeds approved size'):
            assemble(self.parts / 'pack-transfer.json', self.output,
                     self.size, self.digest)
        self.assertFalse(self.output.exists())
        (self.parts / 'pack-transfer.json').write_bytes(b' ' * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, 'manifest too large'):
            assemble(self.parts / 'pack-transfer.json', self.output,
                     self.size, self.digest)
        self.assertFalse(self.output.exists())

    def test_reordered_or_untrusted_manifest_never_exposes_output(self):
        manifest_path = self.parts / 'pack-transfer.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['parts'][0], manifest['parts'][1] = manifest['parts'][1], manifest['parts'][0]
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'reordered'):
            assemble(manifest_path, self.output, self.size, self.digest)
        self.assertFalse(self.output.exists())
        manifest['parts'][0], manifest['parts'][1] = manifest['parts'][1], manifest['parts'][0]
        manifest['sha256'] = '0' * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'differs from approved'):
            assemble(manifest_path, self.output, self.size, self.digest)
        self.assertFalse(self.output.exists())

    def test_approved_digest_and_existing_output_are_required(self):
        with self.assertRaisesRegex(ValueError, 'differs from approved'):
            split(self.source, self.root / 'wrong', self.size, '0' * 64, 500)
        self.assertFalse((self.root / 'wrong' / 'pack-transfer.json').exists())
        self.output.write_bytes(b'do not overwrite')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            assemble(self.parts / 'pack-transfer.json', self.output,
                     self.size, self.digest)
        self.assertEqual(self.output.read_bytes(), b'do not overwrite')


if __name__ == '__main__':
    unittest.main()
