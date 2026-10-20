"""Local reproduction may save a writer call, never the independent review."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from orchestra_kit.evidence import create_evidence, run_evidence, verify_evidence
from orchestra_kit.project import init_project
from orchestra_kit.recipes import compile_recipe
from orchestra_kit.workflow import run_workflow

KIT = Path(__file__).resolve().parents[1]


class BugfixPreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        init_project(self.project, KIT)
        self.source = self.project/'app.py'
        self.source.write_text('def add(a, b): return a + b\n')
        self.calls = []
        self.probe = {'argv':[sys.executable, '-I', '-B', '-c',
                      "import runpy; assert runpy.run_path('app.py')['add'](2,3)==5"],
                      'failure_cause':'implementation'}
        self.task = {'recipe':'bugfix', 'goal':'Investigate a report of incorrect addition.',
                     'files':['app.py'], 'scope':'Only app.py; preserve valid behavior.',
                     'acceptance':['Addition works for positive, zero and negative operands.'],
                     'checks':[{'argv':[sys.executable, '-I', '-B', '-c',
                               "import runpy; add=runpy.run_path('app.py')['add']; assert add(0,0)==0; assert add(-2,3)==1"],
                               'failure_cause':'implementation'}],
                     'reproduction_checks':[self.probe],
                     'complexity':'simple','risk':'low','size':'substantial','independent':True}

    def runner(self, project, kit, job, **kwargs):
        self.calls.append(copy.deepcopy(job))
        if job['role']=='worker':
            self.source.write_text('def add(a, b): return a + b\n')
        checks = run_evidence(project, job['evidence_files'], job['checks'])
        self.assertEqual(checks['status'], 'verified', checks)
        review = job['role']=='reviewer'
        final = 'VERDICT: PASS\nOriginal criteria and inputs checked.' if review else 'Fixed addition.'
        ref = checks['evidence_ref']
        if review:
            report = project/'.orchestra'/f'review-{len(self.calls)}.txt'
            report.write_text(final)
            ref = create_evidence(project, job['evidence_files'], checks['checks'],
                                  review_verdict='PASS', review_report=report)
        return {'status':'verified','final':final,'evidence_ref':ref,'profile':'cheap',
                'attempts':[{'profile':'cheap','requested_model':'fake','requested_effort':'medium',
                             'checks':checks['checks'],
                             'usage':{'complete':True,'totals':{'input_tokens':10,'output_tokens':2,
                                      'cached_input_tokens':4,'total_tokens':12,'uncached_input_tokens':6}}}]}

    def run_flow(self, runner=None, **kwargs):
        job = compile_recipe(self.project, KIT, self.task)
        return run_workflow(self.project, KIT, job, max_calls=kwargs.pop('max_calls',4),
                            runner=runner or self.runner, **kwargs)

    def test_unchanged_code_uses_local_checks_and_only_fresh_reviewer(self):
        before = self.source.read_bytes(), self.source.stat().st_mtime_ns
        result = self.run_flow(max_calls=1)
        self.assertEqual(result['status'],'verified',result)
        self.assertTrue(result['no_change'])
        self.assertEqual(result['preflight']['status'],'verified')
        self.assertEqual([j['role'] for j in self.calls],['reviewer'])
        self.assertEqual(result['model_attempts'],1)
        self.assertEqual(result['repairs'],0)
        self.assertEqual(result['usage']['totals']['total_tokens'],12)
        self.assertEqual((self.source.read_bytes(),self.source.stat().st_mtime_ns),before)
        verify_evidence(self.project,result['preflight']['evidence_ref'],kind='check',required_files=['app.py'])
        review = self.calls[0]
        self.assertEqual(review['brief']['ACCEPTANCE CRITERIA'],self.task['acceptance'])
        material = json.loads((self.project/review['brief']['CONTEXT']['review_materials']['manifest']).read_text())
        self.assertEqual(len(material['checks']),2)
        self.assertIn('not proof',review['brief']['CONTEXT']['no_change_candidate'])

    def test_reproduced_bug_falls_back_to_worker_and_review(self):
        self.source.write_text('def add(a, b): return a - b\n')
        result = self.run_flow()
        self.assertEqual(result['status'],'verified',result)
        self.assertFalse(result['no_change'])
        self.assertEqual(result['preflight']['status'],'failed')
        self.assertEqual([j['role'] for j in self.calls],['worker','reviewer'])
        self.assertEqual(result['repairs'],0)
        self.assertEqual(result['model_attempts'],2)
        self.assertIn(self.probe,self.calls[0]['checks'])
        self.assertIn('preflight_failure',self.calls[0]['brief']['CONTEXT'])

    def test_green_probes_do_not_waive_failing_required_checks(self):
        self.task['checks'][0] = {'argv':[sys.executable,'-I','-B','-c',
            "from pathlib import Path; assert not Path('app.py').read_text().startswith('# broken')"],
            'failure_cause':'implementation'}
        self.source.write_text('# broken\ndef add(a, b): return a + b\n')
        result = self.run_flow()
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual([j['role'] for j in self.calls],['worker','reviewer'])

    def test_environment_failure_stops_without_buying_a_model_call(self):
        self.task['reproduction_checks']=[{'argv':['/missing/orchestra-test-program'],
                                         'failure_cause':'implementation'}]
        result = self.run_flow()
        self.assertEqual(result['status'],'failed',result)
        self.assertEqual(result['stop_reason'],'preflight-environment')
        self.assertEqual(self.calls,[])
        self.assertEqual(result['model_attempts'],0)

    def test_mutating_probe_cannot_approve_changed_source(self):
        self.task['reproduction_checks']=[{'argv':[sys.executable,'-c',
            "from pathlib import Path; Path('app.py').write_text('changed')"],
            'failure_cause':'implementation'}]
        result = self.run_flow()
        self.assertEqual(result['status'],'failed',result)
        self.assertEqual(result['stop_reason'],'preflight-inputs-changed')
        self.assertEqual(self.calls,[])

    def test_typed_non_implementation_failure_does_not_launch_a_writer(self):
        for cause in ('environment','context','capability'):
            with self.subTest(cause=cause):
                self.task['reproduction_checks']=[{'argv':[sys.executable,'-c','raise SystemExit(1)'],
                                                 'failure_cause':cause}]
                result=self.run_flow()
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['failure_cause'],cause)
                self.assertEqual(self.calls,[])

    def test_host_rejection_prevents_preflight_commands(self):
        self.task['reproduction_checks']=[{'argv':[sys.executable,'-c',
            "from pathlib import Path; Path('unexpected.txt').write_text('ran')"],
            'failure_cause':'implementation'}]
        result=self.run_flow(acceptance=lambda:False)
        self.assertEqual(result['stop_reason'],'host-acceptance')
        self.assertFalse((self.project/'unexpected.txt').exists())
        self.assertEqual(self.calls,[])

    def test_combined_check_limit_is_rejected_before_acquiring_workflow_lock(self):
        self.task['checks']=[{'argv':[sys.executable,'-c',f'assert {i} == {i}'],
                             'failure_cause':'implementation'} for i in range(8)]
        with self.assertRaises(ValueError):
            self.run_flow()
        self.assertFalse((self.project/'.orchestra/workflow.lock').exists())

    def test_preflight_never_reuses_review_even_if_project_allows_review_cache(self):
        config=self.project/'.orchestra/project.toml'
        config.write_text(config.read_text().replace('fresh_reviewer_always = true','fresh_reviewer_always = false'))
        for _ in range(2):
            result=self.run_flow(reuse=True,environment={'fixture':'stable'},dependencies=[])
            self.assertEqual(result['status'],'verified',result)
            self.assertEqual(result['model_attempts'],1)
        self.assertEqual([j['role'] for j in self.calls],['reviewer','reviewer'])

    def test_preflight_diagnostic_overflow_returns_terminal_result_without_lock(self):
        self.task['context']='x'*9800
        self.task['checks']=[{'argv':[sys.executable,'-c',"print('bad '*1000); raise SystemExit(1)"],
                             'failure_cause':'implementation'}]
        result=self.run_flow()
        self.assertEqual(result['status'],'failed',result)
        self.assertEqual(result['stop_reason'],'preflight-context-budget')
        self.assertFalse((self.project/'.orchestra/workflow.lock').exists())
        self.assertEqual(self.calls,[])

    def test_missing_probe_or_non_bugfix_cannot_enable_the_shortcut(self):
        for recipe, probes in [('bugfix',[]),('feature',[self.probe]),('refactor',[self.probe])]:
            with self.subTest(recipe=recipe):
                self.task.update(recipe=recipe,reproduction_checks=probes)
                with self.assertRaises(ValueError):
                    compile_recipe(self.project,KIT,self.task)

    def test_a_review_blocker_still_requires_repair_and_fresh_review(self):
        def runner(project,kit,job,**kwargs):
            receipt = self.runner(project,kit,job,**kwargs)
            if len(self.calls)==1:
                receipt.update(status='failed',failure='review verdict: FAIL',
                               final='VERDICT: FAIL\nMissing original signed-operand evidence.')
                report=project/'.orchestra/blocking-review.txt';report.write_text(receipt['final'])
                receipt['evidence_ref']=create_evidence(project,job['evidence_files'],receipt['attempts'][0]['checks'],
                    review_verdict='FAIL',review_report=report)
            return receipt
        result = self.run_flow(runner=runner)
        self.assertEqual(result['status'],'verified',result)
        self.assertFalse(result['no_change'])
        self.assertEqual([j['role'] for j in self.calls],['reviewer','worker','reviewer'])
        self.assertEqual(result['model_attempts'],3)
        self.assertEqual(result['repairs'],1)
        self.assertEqual(result['usage']['totals']['total_tokens'],36)
        self.assertNotIn('no_change_candidate',self.calls[-1]['brief']['CONTEXT'])

    def test_default_cached_worker_still_counts_subsequent_review_repair(self):
        del self.task['reproduction_checks']
        first=self.run_flow(reuse=True,environment={'fixture':'stable'},dependencies=[])
        self.assertEqual(first['status'],'verified')
        def runner(project,kit,job,**kwargs):
            receipt=self.runner(project,kit,job,**kwargs)
            if len(self.calls)==3:
                receipt.update(status='failed',failure='review verdict: FAIL',
                               final='VERDICT: FAIL\nMissing signed-operand evidence.')
                report=project/'.orchestra/cached-blocker.txt';report.write_text(receipt['final'])
                receipt['evidence_ref']=create_evidence(project,job['evidence_files'],receipt['attempts'][0]['checks'],
                    review_verdict='FAIL',review_report=report)
            return receipt
        result=self.run_flow(runner=runner,reuse=True,environment={'fixture':'stable'},dependencies=[])
        self.assertEqual(result['status'],'verified',result)
        self.assertTrue(result['receipts'][0]['receipt']['reused'])
        self.assertEqual(result['repairs'],1)
