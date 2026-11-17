"""Unsupported bootstrap Python fails before importing the product or changing data."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapPythonTests(unittest.TestCase):
    def test_unsupported_python_has_actionable_error_before_product_imports(self):
        code = ('import runpy, sys; sys.version_info=(3,9,6); '
                'runpy.run_path(sys.argv[1], run_name="__main__")')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'install.py'), '--help'],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('requires Python 3.10+', result.stderr)
        self.assertIn('python3', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(result.stdout)

    def test_supported_bootstrap_keeps_the_cli(self):
        result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'install.py'), '--help'],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('discover', result.stdout)
        self.assertIn('prepare-recipes', result.stdout)


if __name__ == '__main__':
    unittest.main()
