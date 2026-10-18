"""Host review context must be usable, bounded and tamper evident."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra_kit.evidence import run_evidence
from orchestra_kit.fingerprint import check_fingerprint
from orchestra_kit.review_materials import capture_baseline, review_materials


class ReviewMaterialsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root/'.orchestra/workflows').mkdir(parents=True)
        (self.root/'app.py').write_text('before\n')
        self.directory = self.root/'.orchestra/workflows/test/review-materials'

    def worker(self, checks=None):
        checked = run_evidence(self.root, ['app.py'], checks or [
            {'argv': [sys.executable, '-B', '-c', 'print("checked")'],
             'failure_cause': 'implementation'}])
        self.assertEqual(checked['status'], 'verified')
        return {**checked, 'final': 'Done.'}

    def test_materials_use_real_logs_and_preserve_absent_baseline_files(self):
        baseline = capture_baseline(self.root, ['app.py', 'new.py'], self.directory)
        (self.root/'new.py').write_text('new function\n')
        material = review_materials(self.root, ['app.py'], baseline, self.worker(), self.directory)
        packet = json.loads((self.root/material['manifest']).read_text())
        before = json.loads((self.root/packet['baseline']).read_text())
        self.assertEqual(before['files']['new.py']['state'], 'missing')
        self.assertIn('+new function', packet['diff'])
        self.assertIn('checked', (self.root/packet['checks'][0]['log']).read_text())
        self.assertFalse(check_fingerprint(self.root, material['snapshot']))

    def test_source_and_worker_secrets_are_redacted_in_retained_material(self):
        secret = 'private-review-credential-marker-123456'
        with patch.dict(os.environ, {'REVIEW_TEST_API_KEY': secret}):
            (self.root/'app.py').write_text(secret)
            baseline = capture_baseline(self.root, ['app.py'], self.directory)
            worker = self.worker([{'argv': [sys.executable, '-B', '-c', 'pass', secret],
                                   'failure_cause': 'implementation'}])
            worker['final'] = 'Claim: ' + secret
            review_materials(self.root, ['app.py'], baseline, worker, self.directory)
        for path in self.directory.iterdir():
            self.assertNotIn(secret, path.read_text())
        self.assertIn('[REDACTED]', (self.directory/'baseline.json').read_text())

    def test_large_and_binary_sources_are_explicit_gaps_and_small_file_remains(self):
        (self.root/'large').write_bytes(b'x' * 70_000)
        (self.root/'binary').write_bytes(b'\x00\xff')
        baseline = capture_baseline(self.root, ['app.py', 'large', 'binary'], self.directory)
        data = json.loads((self.root/baseline['path']).read_text())
        self.assertEqual(data['files']['app.py']['text'], 'before\n')
        self.assertEqual(data['files']['large']['unavailable'], 'size-limit')
        self.assertEqual(data['files']['binary']['unavailable'], 'binary')
        self.assertLess((self.root/baseline['path']).stat().st_size, 4000)

    def test_symlink_source_and_replaced_baseline_are_refused(self):
        (self.root/'link.py').symlink_to(self.root/'app.py')
        with self.assertRaises(ValueError):
            capture_baseline(self.root, ['link.py'], self.directory)
        baseline = capture_baseline(self.root, ['app.py'], self.directory.parent/'other')
        (self.root/baseline['path']).write_text('{}')
        with self.assertRaises(ValueError):
            review_materials(self.root, ['app.py'], baseline, self.worker(), self.directory.parent/'other')
