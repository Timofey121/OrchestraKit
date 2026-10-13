from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parents[1]


class QuestionCliTests(unittest.TestCase):
    def test_root_can_correlate_question_answer_and_refuse_stale_reply(self):
        from orchestra_kit.state import start_task
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.orchestra').mkdir()
            (root / '.orchestra/project.toml').write_text((KIT / 'templates/project.toml').read_text())
            (root / 'source.py').write_text('value = 1\n')
            task = start_task(root, KIT, 'Preserve goal')
            request = {'task_id': task['task_id'], 'revision': 0,
                       'leaf_id': '126cc43c-e2a1-4c2d-9fb8-1f1dfb90ed9f',
                       'question': 'Which source assumption needs inspection?',
                       'files': ['source.py'], 'environment': {'python': '3.11'}}
            path = root / 'request.json'; path.write_text(json.dumps(request))
            def cli(*args):
                return subprocess.run([sys.executable, '-m', 'orchestra_kit', 'question', *args],
                                      cwd=KIT, env=dict(os.environ, PYTHONPATH=str(KIT / 'src')),
                                      capture_output=True, text=True)
            asked = cli('ask', str(root), '--input', str(path))
            self.assertEqual(asked.returncode, 0, asked.stderr)
            question_id = json.loads(asked.stdout)['question_id']
            answer = root / 'answer.json'
            answer.write_text(json.dumps({'answer': 'Inspect source.py', 'evidence_refs': [],
                                          'environment': request['environment']}))
            replied = cli('answer', str(root), question_id, '--input', str(answer))
            self.assertEqual(replied.returncode, 0, replied.stderr)
            env = root / 'environment.json'; env.write_text(json.dumps(request['environment']))
            read = cli('read', str(root), question_id, '--environment', str(env))
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertEqual(json.loads(read.stdout)['answer'], 'Inspect source.py')
            (root / 'source.py').write_text('value = 2\n')
            stale = cli('read', str(root), question_id, '--environment', str(env))
            self.assertEqual(stale.returncode, 1, stale.stderr)
            self.assertIsNone(json.loads(stale.stdout).get('answer'))
