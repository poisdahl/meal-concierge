import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import build_identity as identity
import install


class BuildIdentityTests(unittest.TestCase):
    def test_staged_bytes_survive_checkout_and_distinguish_modified_exported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'source'; source.mkdir()
            (source / 'example.py').write_text('value = 1\n')
            (source / 'runtime-requirements.txt').write_text('mcp==2.2.0\n')
            (source / 'skill').mkdir(); (source / 'skill/SKILL.md').write_text('guide\n')
            def git(*args):
                subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True)
            git('init'); git('add','.'); git('-c','user.name=Synthetic','-c','user.email=test@example.test','commit','-m','fixture')
            with patch.object(install,'SOURCE',source), patch.object(install,'run'), patch.object(install,'executable',return_value='uv'):
                first = install.stage_release(root / 'code')
                clean = identity.read(first)
                self.assertFalse(clean['source_modified']); self.assertEqual(len(clean['source_commit']),40)
                (source / 'example.py').write_text('value = 2\n')
                modified = identity.read(install.stage_release(root / 'code'))
                self.assertTrue(modified['source_modified'])
                self.assertNotEqual(clean['source_sha256'],modified['source_sha256'])
                shutil.rmtree(source / '.git')
                exported = identity.read(install.stage_release(root / 'code'))
                self.assertIsNone(exported['source_commit']); self.assertIsNone(exported['source_modified'])
                self.assertEqual(exported['source_sha256'],modified['source_sha256'])
            shutil.rmtree(source)
            self.assertEqual(identity.read(first),clean)
            self.assertNotIn(str(root),json.dumps(clean))
            self.assertEqual(identity.read(root)['identity_status'],'unavailable')
