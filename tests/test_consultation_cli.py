from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parents[1]


class ConsultationCliTests(unittest.TestCase):
    def test_dry_run_pins_strong_read_only_route_without_a_model_call(self):
        from orchestra_kit.evidence import run_evidence
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.orchestra').mkdir()
            (root / '.orchestra/project.toml').write_text((KIT / 'templates/project.toml').read_text())
            (root / 'source.py').write_text('value = 1\n')
            checked = run_evidence(root, ['source.py'], [
                {'argv': [sys.executable, '-c', 'pass'], 'failure_cause': 'implementation'}])
            artifact = root / '.orchestra/checks/rejected.log'
            artifact.write_text('The previous attempt did not resolve the failure.\n')
            payload = {'question': 'Which assumption should be checked next?', 'goal': 'Resolve the failure',
                       'constraints': ['Preserve tests'], 'acceptance': ['Original requirements pass'],
                       'files': ['source.py'], 'evidence_refs': [checked['evidence_ref']],
                       'rejected_approaches': [{'text': 'Retry unchanged', 'reason': 'Same failure',
                                               'artifact_ref': '.orchestra/checks/rejected.log'}]}
            path = root / 'consult.json'; path.write_text(json.dumps(payload))
            process = subprocess.run([sys.executable, '-m', 'orchestra_kit', 'consult', str(root),
                                      '--input', str(path), '--dry-run'], cwd=KIT,
                                     env=dict(os.environ, PYTHONPATH=str(KIT / 'src')),
                                     text=True, capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result['status'], 'advisory-dry-run')
            self.assertEqual(result['route']['profile'], 'strong')
            self.assertEqual(result['route']['sandbox'], 'read-only')
            self.assertIsNone(result['usage'])
            self.assertFalse((root / '.orchestra/executions').exists())
