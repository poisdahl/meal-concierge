"""Harmless Finder metadata does not block collection maintenance."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recipes import RecipeError
from recipe_portable import _pack_metadata_inventory


class CollectionMetadataTests(unittest.TestCase):
    def test_finder_file_is_left_in_place_and_never_read(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            metadata = state / 'pack-metadata'
            metadata.mkdir()
            finder = metadata / '.DS_Store'
            finder.write_bytes(b'Synthetic Finder metadata')
            finder.chmod(0o000)
            try:
                result = _pack_metadata_inventory(state, 'synthetic')
            finally:
                finder.chmod(0o600)
            self.assertEqual(result, {'directories': [], 'empty_directories': [], 'assets': set()})
            self.assertEqual(finder.read_bytes(), b'Synthetic Finder metadata')

    def test_finder_name_cannot_hide_a_symlink_or_directory(self):
        for kind in ('link', 'directory'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                state = Path(root)
                metadata = state / 'pack-metadata'
                metadata.mkdir()
                finder = metadata / '.DS_Store'
                if kind == 'link':
                    finder.symlink_to(state)
                else:
                    finder.mkdir()
                with self.assertRaisesRegex(RecipeError, r'invalid directory.*DS_Store'):
                    _pack_metadata_inventory(state, 'synthetic')


if __name__ == '__main__':
    unittest.main()
