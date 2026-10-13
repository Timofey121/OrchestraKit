"""Exercise the public stage-two commands rather than only library helpers."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parents[1]


class StageTwoCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        initialized = self.cli('init', str(self.root))
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'orchestra_kit', *args],
                              cwd=KIT, env=dict(os.environ, PYTHONPATH=str(KIT / 'src')),
                              capture_output=True, text=True)

    def test_memory_search_requires_explicit_environment_and_returns_no_invented_fix(self):
        env = self.root / 'environment.json'
        env.write_text(json.dumps({'python': '3.11'}))
        result = self.cli('memory', 'search', str(self.root), '--query', 'KeyError',
                          '--environment', str(env))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['solutions'], [])
        refused = self.cli('memory', 'search', str(self.root), '--query', 'KeyError')
        self.assertEqual(refused.returncode, 2)

    def test_navigation_build_search_and_stale_source(self):
        source = self.root / 'sample.py'
        source.write_text('def repair_payload():\n    return 1\n')
        built = self.cli('navigate', 'build', str(self.root), '--path', 'sample.py')
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertNotIn('repair_payload', built.stdout)
        found = self.cli('navigate', 'search', str(self.root), '--query', 'repair_payload')
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn('repair_payload', found.stdout)
        source.write_text('def other():\n    return 2\n')
        stale = self.cli('navigate', 'search', str(self.root), '--query', 'repair_payload')
        self.assertEqual(stale.returncode, 0, stale.stderr)
        self.assertNotIn('repair_payload', json.dumps(json.loads(stale.stdout).get('results', [])))

    def test_handoff_refuses_unverified_facts_before_persistence(self):
        started = self.cli('task', 'start', str(self.root), '--goal', 'Keep constraints')
        state = json.loads(started.stdout)
        data = {'task_id': state['task_id'], 'revision': 0, 'goal': state['goal'],
                'constraints': ['Do not weaken checks'], 'acceptance': ['All checks pass'],
                'unresolved_failures': [], 'next_step': 'Inspect source',
                'evidence_refs': [], 'raw_history_refs': [],
                'verified_facts': [{'text': 'Tests passed', 'reason': 'Claimed check',
                                    'evidence_ref': '.orchestra/evidence/missing.json'}]}
        path = self.root / 'handoff.json'
        path.write_text(json.dumps(data))
        refused = self.cli('handoff', str(self.root), '--input', str(path))
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertFalse((self.root / '.orchestra/handoffs').exists())

    def test_memory_store_cli_uses_accepted_task_and_reports_stale_source(self):
        from orchestra_kit.evidence import create_evidence, run_evidence
        from orchestra_kit.state import start_task, record_task
        (self.root / 'app.py').write_text('value = 1\n')
        task = start_task(self.root, KIT, 'Fix parser')
        checked = run_evidence(self.root, ['app.py'], [
            {'argv': [sys.executable, '-c', 'pass'], 'failure_cause': 'implementation'}])
        report = self.root / 'review.txt'
        report.write_text('VERDICT: PASS\nTest fixture review\n')
        refs = {kind: create_evidence(self.root, ['app.py'], checked['checks'],
                  owner=task['task_id'], review_verdict='PASS' if kind == 'review' else None,
                  review_report=report if kind == 'review' else None)
                for kind in ('check', 'review')}
        record_task(self.root, KIT, task['task_id'], {
            'status': 'completed', 'summary': 'Fixed parser', 'completed_steps': ['fixed'],
            'next_steps': [], 'changed_files': ['app.py'],
            'checks': [{'command': 'check', 'outcome': 'pass', 'evidence': 'check log',
                        'evidence_ref': refs['check']}],
            'reviews': [{'level': 'final', 'verdict': 'PASS', 'evidence': 'fixture review',
                         'evidence_ref': refs['review']}],
            'acceptance': [{'criterion': 'parser works', 'satisfied': True, 'evidence': 'fixture'}],
            'metrics': {}, 'failure_cause': None}, expected_revision=0)
        data = {'task_id': task['task_id'], 'symptom': 'KeyError payload', 'scope': 'parser',
                'environment': {'python': '3.11'}, 'cause': 'wrong field', 'fix': 'read items',
                'files': ['app.py']}
        path = self.root / 'solution.json'; path.write_text(json.dumps(data))
        stored = self.cli('memory', 'store', str(self.root), '--input', str(path))
        self.assertEqual(stored.returncode, 0, stored.stderr)
        env = self.root / 'environment.json'; env.write_text(json.dumps(data['environment']))
        arguments = ('memory', 'search', str(self.root), '--query', 'KeyError', '--environment', str(env))
        found = self.cli(*arguments)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(len(json.loads(found.stdout)['solutions']), 1)
        budget = max(512, len(found.stdout.encode('utf-8')))
        bounded = self.cli(*arguments, '--max-chars', str(budget))
        self.assertEqual(bounded.returncode, 0, bounded.stderr)
        self.assertLessEqual(len(bounded.stdout.encode('utf-8')), budget)
        (self.root / 'app.py').write_text('value = 2\n')
        stale = json.loads(self.cli(*arguments).stdout)
        self.assertEqual(stale['solutions'], [])
        self.assertEqual(stale['stale_count'], 1)

    def test_root_can_bind_current_execution_evidence_to_existing_task(self):
        from orchestra_kit.evidence import run_evidence, verify_evidence, create_evidence
        from orchestra_kit.state import start_task
        (self.root / 'app.py').write_text('value = 1\n')
        task = start_task(self.root, KIT, 'Adopt checked result')
        checked = run_evidence(self.root, ['app.py'], [
            {'argv': [sys.executable, '-c', 'pass'], 'failure_cause': 'implementation'}])
        result = self.cli('evidence', 'bind', str(self.root), '--ref', checked['evidence_ref'],
                          '--task-id', task['task_id'], '--kind', 'check')
        self.assertEqual(result.returncode, 0, result.stderr)
        bound = verify_evidence(self.root, json.loads(result.stdout)['evidence_ref'], kind='check')
        self.assertEqual(bound['owner'], task['task_id'])
        original = verify_evidence(self.root, checked['evidence_ref'], kind='check')
        self.assertNotEqual(original['owner'], task['task_id'])
        denied_review = self.cli('evidence', 'bind', str(self.root), '--ref', checked['evidence_ref'],
                                '--task-id', task['task_id'], '--kind', 'review')
        self.assertEqual(denied_review.returncode, 2)
        report = self.root / 'independent-review.txt'
        report.write_text('VERDICT: PASS\nIndependent fixture review\n')
        reviewed = create_evidence(self.root, ['app.py'], checked['checks'], owner='reviewer',
                                  review_verdict='PASS', review_report=report)
        adopted_review = self.cli('evidence', 'bind', str(self.root), '--ref', reviewed,
                                 '--task-id', task['task_id'], '--kind', 'review')
        self.assertEqual(adopted_review.returncode, 0, adopted_review.stderr)
        cert = verify_evidence(self.root, json.loads(adopted_review.stdout)['evidence_ref'], kind='review')
        self.assertEqual(cert['owner'], task['task_id'])
        self.assertEqual(cert['review_report'], verify_evidence(self.root, reviewed, kind='review')['review_report'])
        (self.root / 'app.py').write_text('value = 2\n')
        stale = self.cli('evidence', 'bind', str(self.root), '--ref', checked['evidence_ref'],
                         '--task-id', task['task_id'], '--kind', 'check')
        self.assertEqual(stale.returncode, 2)

    def test_navigation_cli_honors_output_budget(self):
        (self.root / 'many.py').write_text(''.join(f'def matching_{i}():\n    pass\n' for i in range(20)))
        self.assertEqual(self.cli('navigate', 'build', str(self.root), '--path', 'many.py').returncode, 0)
        result = self.cli('navigate', 'search', str(self.root), '--query', 'matching', '--max-chars', '600')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout.encode('utf-8')), 600)
        self.assertTrue(json.loads(result.stdout)['results'])


if __name__ == '__main__':
    unittest.main()
