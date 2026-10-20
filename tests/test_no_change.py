"""No-change completion must retain real checks and independent review."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from orchestra_kit.evidence import verify_evidence
from orchestra_kit.project import init_project
from orchestra_kit.recipes import compile_recipe
from orchestra_kit.workflow import run_workflow

KIT = Path(__file__).resolve().parents[1]


class NoChangeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Native executor requires POSIX process groups")
    def test_native_no_change_keeps_source_and_review_while_a_false_claim_fails_checks(self):
        # The subprocess is a deterministic model double. Checks and evidence are real;
        # these counters are NOT measurements of model usage or prompt effectiveness.
        for correct in (True, False):
            with self.subTest(correct=correct), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root/'project'
                project.mkdir()
                init_project(project, KIT)
                source = project/'app.py'
                source.write_text('def add(a, b): return a ' + ('+' if correct else '-') + ' b\n')
                before = (source.read_bytes(), source.stat().st_mtime_ns)
                trace = root/'trace.jsonl'
                cli = root/'fake.py'
                cli.write_text(
                    'import json,sys\nfrom pathlib import Path\n'
                    'prompt=sys.stdin.read()\n'
                    f'with Path({str(trace)!r}).open("a") as f: f.write(json.dumps({{"args":sys.argv,"prompt":prompt}})+"\\n")\n'
                    'review="review_original_goal" in prompt\n'
                    'text="VERDICT: PASS\\nOriginal criteria independently checked" if review else "No-change: report not reproduced on add(2,3); observed 5, expected 5"\n'
                    'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":text}}))\n'
                    'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2,"cached_input_tokens":0}}))\n'
                )
                check = [sys.executable, '-I', '-B', '-c',
                         'import runpy,sys; add=runpy.run_path(sys.argv[1])["add"]; assert add(2,3)==5; assert add(0,0)==0; assert add(-2,3)==1', str(source)]
                job = compile_recipe(project, KIT, {
                    'recipe':'bugfix', 'goal':'Investigate a report that addition returns a wrong value.',
                    'files':['app.py'], 'scope':'Only app.py; preserve correct behavior.',
                    'acceptance':['add(2, 3) returns 5', 'Zeros and signed inputs keep ordinary addition semantics'],
                    'checks':[{'argv':check,'failure_cause':'implementation'}],
                    'complexity':'simple','risk':'low','size':'substantial','independent':True,
                })
                result = run_workflow(project, KIT, job, max_calls=2,
                                      codex_command=[sys.executable, str(cli)])
                self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)
                rows = [json.loads(line) for line in trace.read_text().splitlines()]
                policy = json.loads(next(arg.split('=',1)[1] for arg in rows[0]['args'] if arg.startswith('developer_instructions=')))
                self.assertNotIn('no-change', policy)  # task-specific guidance stays in the selected recipe
                if correct:
                    self.assertEqual(result['status'],'verified',result)
                    self.assertEqual([p['role'] for p in result['receipts']],['worker','reviewer'])
                    self.assertEqual(result['model_attempts'],2)
                    self.assertEqual(result['repair_decisions'],0)
                    for phase in result['receipts']:
                        verify_evidence(project,phase['receipt']['evidence_ref'],kind='review' if phase['role']=='reviewer' else 'check')
                else:
                    self.assertEqual(result['status'],'failed',result)
                    self.assertTrue(all(p['role']=='worker' for p in result['receipts']))
                    self.assertNotEqual(subprocess.run(check,capture_output=True).returncode,0)
