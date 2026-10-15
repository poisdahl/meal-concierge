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
                self.assertIsNone(clean['release_tag'])
                # Recipe releases, prereleases and malformed tags are not program releases.
                for tag in ('recipes-20261015', 'v9.0.0-rc1', 'v01.2.3', 'v1.2'):
                    git('tag', tag)
                self.assertIsNone(identity.read(install.stage_release(root / 'code'))['release_tag'])
                git('tag', 'v0.1.9')
                git('-c', 'user.name=Synthetic', '-c', 'user.email=test@example.test',
                    'tag', '-a', 'v0.1.10', '-m', 'synthetic release')
                tagged = identity.read(install.stage_release(root / 'code'))
                self.assertEqual(tagged['release_tag'], 'v0.1.10')
                self.assertFalse(tagged['source_modified'])
                self.assertEqual(tagged['source_commit'], clean['source_commit'])
                # The actual service health and agent status projection preserve this identity.
                from core import StateStore
                import service
                from test_meal_concierge_mathem import MathemShop
                with patch.object(service, 'BUILD_IDENTITY', tagged):
                    app = service.Application(StateStore(root / 'household', {
                        'household': 'Synthetic', 'provider': 'mathem',
                        'profile_overrides': {}, 'confirmation_policy': 'fresh',
                    }), MathemShop(), None)
                    self.assertEqual(app.handle({'operation': 'health'})['build'], tagged)
                    self.assertEqual(app.handle({'operation': 'status', 'response_view': 'agent'})['build'], tagged)
                (source / 'example.py').write_text('value = 2\n')
                modified = identity.read(install.stage_release(root / 'code'))
                self.assertTrue(modified['source_modified'])
                self.assertEqual(modified['release_tag'], 'v0.1.10')
                self.assertNotEqual(clean['source_sha256'],modified['source_sha256'])
                git('add', '.'); git('-c', 'user.name=Synthetic', '-c', 'user.email=test@example.test',
                                    'commit', '-m', 'development change')
                development = identity.read(install.stage_release(root / 'code'))
                self.assertIsNone(development['release_tag'])
                self.assertFalse(development['source_modified'])
                self.assertNotEqual(development['source_commit'], tagged['source_commit'])
                shutil.rmtree(source / '.git')
                exported = identity.read(install.stage_release(root / 'code'))
                self.assertIsNone(exported['source_commit']); self.assertIsNone(exported['source_modified'])
                self.assertIsNone(exported['release_tag'])
                self.assertEqual(exported['source_sha256'],modified['source_sha256'])
            shutil.rmtree(source)
            self.assertEqual(identity.read(first),clean)
            self.assertNotIn(str(root),json.dumps(clean))
            self.assertEqual(identity.read(root)['identity_status'],'unavailable')

    def test_old_manifest_and_nested_export_remain_unidentified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = {'format': 1, 'source_commit': 'a' * 40, 'source_modified': False}
            (root / identity.MANIFEST).write_text(json.dumps(old))
            self.assertEqual(identity.read(root), old)
            # An exported copy must not inherit its containing repository's tag.
            subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Synthetic',
                            '-c', 'user.email=test@example.test', 'commit', '-m', 'fixture'],
                           check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), 'tag', 'v1.2.3'], check=True, capture_output=True)
            source = root / 'export'; source.mkdir()
            (source / 'runtime-requirements.txt').write_text('')
            exported = identity.create(source, source, '3.12.12')
            self.assertIsNone(exported['release_tag'])
            self.assertIsNone(exported['source_commit'])
