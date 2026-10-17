"""Offline end-to-end repair, routing, freshness and accounting contracts."""
import copy
import json
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra_kit.project import init_project
from orchestra_kit.evidence import create_evidence
from orchestra_kit import workflow

KIT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        init_project(self.project, KIT)
        (self.project/'app.py').write_text('initial\n')
        self.job = {'role':'worker','complexity':'simple','risk':'low',
                    'size':'substantial','independent':True,
                    'brief':{'GOAL':'Implement addition','SOURCES OF TRUTH':['app.py'],
                             'SCOPE':'Only edit app.py. Do not delegate.',
                             'ACCEPTANCE CRITERIA':['Correct addition and all valid zeros'],
                             'VERIFICATION':'Run specified checks', 'CONTEXT':'No external data'},
                    'checks':[{'argv':[sys.executable,'-c','pass'],'failure_cause':'implementation'}],
                    'evidence_files':['app.py']}
        self.calls = []

    def runner(self, project, kit, job, **kwargs):
        self.calls.append((copy.deepcopy(job),kwargs))
        n=len(self.calls)
        logs=project/'.orchestra/checks';logs.mkdir(exist_ok=True)
        log=logs/f'check-{n}.log';log.write_text('passed')
        checks=[{**job['checks'][0],'returncode':0,'log':str(log)}]
        review=job['role']=='reviewer'
        final='VERDICT: PASS\nChecked.' if review else 'Implemented.'
        report=project/'.orchestra'/f'review-{n}.txt';report.write_text(final)
        ref=create_evidence(project,job['evidence_files'],checks,
                            review_verdict='PASS' if review else None,
                            review_report=report if review else None)
        profile=(kwargs.get('recovery') or {}).get('previous_profile','cheap')
        u={'complete':True,'totals':{'input_tokens':10,'output_tokens':2,
           'total_tokens':12,'cached_input_tokens':4,'uncached_input_tokens':6}}
        return {'status':'verified','final':final,'evidence_ref':ref,
                'profile':profile,'requested_model':'fake','requested_effort':'medium',
                'attempts':[{'number':1,'profile':profile,'requested_model':'fake',
                             'requested_effort':'medium','usage':u,'checks':checks}]}

    def run_flow(self, **kwargs):
        return workflow.run_workflow(self.project,KIT,self.job,max_calls=6,
                                     runner=kwargs.pop('runner',self.runner),**kwargs)

    def reject_review(self,project,job,receipt,text):
        receipt.update(status='failed',failure='review verdict: FAIL',final='VERDICT: FAIL\n'+text)
        report=project/'.orchestra'/f'failed-review-{len(self.calls)}.txt'
        report.write_text(receipt['final'])
        receipt['evidence_ref']=create_evidence(project,job['evidence_files'],receipt['attempts'][0]['checks'],
                                                review_verdict='FAIL',review_report=report)
        return receipt

    def test_review_fail_returns_only_findings_plus_original_criteria_to_worker(self):
        def runner(project,kit,job,**kwargs):
            receipt=self.runner(project,kit,job,**kwargs)
            if len(self.calls)==2:
                self.reject_review(project,job,receipt,'Negative zero rejected at app.py:4.')
            return receipt
        result=self.run_flow(runner=runner)
        self.assertEqual(result['status'],'verified')
        self.assertEqual([j['role'] for j,k in self.calls],['worker','reviewer','worker','reviewer'])
        repair=self.calls[2][0]['brief']
        self.assertEqual(repair['ACCEPTANCE CRITERIA'],self.job['brief']['ACCEPTANCE CRITERIA'])
        self.assertEqual(repair['SCOPE'],self.job['brief']['SCOPE'])
        self.assertIn('Negative zero',json.dumps(repair['CONTEXT']))
        self.assertEqual(result['usage']['totals']['total_tokens'],48)
        self.assertEqual(result['repairs'],1)
        self.assertEqual(result['model_attempts'],4)

    def test_failed_review_never_becomes_success_when_repairs_exhausted(self):
        def runner(project,kit,job,**kwargs):
            r=self.runner(project,kit,job,**kwargs)
            if job['role']=='reviewer':self.reject_review(project,job,r,'Wrong output')
            return r
        result=self.run_flow(runner=runner)
        self.assertEqual(result['status'],'failed')
        self.assertLessEqual(result['model_attempts'],6)
        self.assertFalse(result['quality_passed'])
        self.assertGreaterEqual(result['repairs'],1)

    def test_environment_context_and_capability_do_not_trigger_stronger_model(self):
        for cause,action in [('environment','stop'),('context','gather-context'),('capability','keep-root')]:
            with self.subTest(cause=cause):
                self.calls=[]
                def runner(project,kit,job,**kwargs):
                    r=self.runner(project,kit,job,**kwargs)
                    r.update(status='failed',failure=cause)
                    r['attempts'][0]['checks'][0]['returncode']=1
                    r['attempts'][0]['checks'][0]['failure_cause']=cause
                    return r
                result=self.run_flow(runner=runner)
                self.assertEqual(len(self.calls),1)
                self.assertEqual(result['next_action'],action)
                self.assertEqual(result['failure_cause'],cause)

    def test_budget_is_global_across_reviews_and_repairs(self):
        def runner(project,kit,job,**kwargs):
            r=self.runner(project,kit,job,**kwargs)
            if job['role']=='reviewer':self.reject_review(project,job,r,'Wrong')
            return r
        result=workflow.run_workflow(self.project,KIT,self.job,max_calls=2,runner=runner)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(result['stop_reason'],'launch-budget')
        self.assertFalse(result['quality_passed'])

    def test_missing_usage_stays_unknown_and_prevents_automatic_repair(self):
        def runner(project,kit,job,**kwargs):
            r=self.runner(project,kit,job,**kwargs)
            r['attempts'][0]['usage']=None
            r.update(status='failed',failure='implementation')
            r['attempts'][0]['checks'][0]['returncode']=1
            return r
        result=self.run_flow(runner=runner)
        self.assertIsNone(result['usage']['totals']['total_tokens'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(result['stop_reason'],'unknown-usage')

    def test_stale_worker_evidence_or_policy_cannot_finish(self):
        for name in ('app.py','.orchestra/project.toml'):
            with self.subTest(name=name):
                self.calls=[]
                def runner(project,kit,job,**kwargs):
                    r=self.runner(project,kit,job,**kwargs)
                    if job['role']=='reviewer':
                        path=project/name;path.write_text(path.read_text()+'\n# change\n')
                    return r
                result=self.run_flow(runner=runner)
                self.assertEqual(result['status'],'failed')
                self.assertFalse(result['quality_passed'])

    def test_exception_after_possible_dispatch_never_counts_as_zero(self):
        def runner(*args,**kw):raise RuntimeError('possible launch')
        result=self.run_flow(runner=runner)
        self.assertIsNone(result['model_attempts'])
        self.assertFalse(result['usage']['complete'])
        self.assertEqual(result['stop_reason'],'dispatch-uncertain')
        self.assertTrue(Path(result['artifact']).is_file())

    def test_dry_run_does_not_dispatch(self):
        result=self.run_flow(dry_run=True)
        self.assertEqual(result['status'],'dry-run')
        self.assertEqual(self.calls,[])

    def test_invalid_reuse_requires_explicit_environment_and_dependencies(self):
        with self.assertRaises(ValueError):self.run_flow(reuse=True)
        self.assertEqual(self.calls,[])

    def test_reuse_skips_worker_but_honors_always_fresh_reviewer(self):
        options={'reuse':True,'environment':{'runtime':'test'},'dependencies':[]}
        self.assertEqual(self.run_flow(**options)['model_attempts'],2)
        again=self.run_flow(**options)
        self.assertEqual(again['status'],'verified')
        self.assertEqual(again['model_attempts'],1)
        self.assertTrue(again['receipts'][0]['receipt']['reused'])
        self.assertEqual(again['usage']['totals']['total_tokens'],12)
        (self.project/'app.py').write_text('changed')
        self.assertEqual(self.run_flow(**options)['model_attempts'],2)

    def test_explicit_review_reuse_preserves_origin_and_reports_zero_new_calls(self):
        config=self.project/'.orchestra/project.toml'
        config.write_text(config.read_text().replace('fresh_reviewer_always = true','fresh_reviewer_always = false'))
        options={'reuse':True,'environment':{'runtime':'test'},'dependencies':[]}
        self.run_flow(**options)
        again=self.run_flow(**options)
        self.assertEqual(again['model_attempts'],0)
        self.assertEqual(again['usage']['totals']['total_tokens'],0)
        self.assertEqual(len(self.calls),2)
        self.assertTrue(again['receipts'][1]['receipt']['evidence_ref'])

    def test_dependency_and_acceptance_changes_force_fresh_calls(self):
        (self.project/'lock.txt').write_text('1')
        options={'reuse':True,'environment':{'runtime':'test'},'dependencies':['lock.txt']}
        self.run_flow(**options)
        (self.project/'lock.txt').write_text('2')
        self.assertEqual(self.run_flow(**options)['model_attempts'],2)
        self.job['brief']['ACCEPTANCE CRITERIA'].append('new criterion')
        self.assertEqual(self.run_flow(**options)['model_attempts'],2)

    def test_cli_workflow_dry_run_has_no_model_calls(self):
        from orchestra_kit.cli import main
        import contextlib,io
        path=self.project/'job.json';path.write_text(json.dumps(self.job))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code=main(['workflow',str(self.project),'--input',str(path),'--max-launches','4','--dry-run'])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())['status'],'dry-run')

    @unittest.skipIf(os.name=='nt','Native leaf execution requires POSIX host locks')
    def test_native_fake_cli_executes_repair_and_fresh_review_with_real_checks(self):
        script=self.project/'fake_codex.py'
        script.write_text('''import json,pathlib,sys
root=pathlib.Path.cwd()
state=root/'.orchestra/fake-count'
n=int(state.read_text())+1 if state.exists() else 1
state.write_text(str(n))
prompt=sys.stdin.read()
review='review_original_goal' in prompt
if not review:(root/'app.py').write_text('answer = 42\\n')
final=('VERDICT: FAIL\\nHandle the zero case in app.py.' if n==2 else 'VERDICT: PASS\\nChecked original criteria.') if review else 'Implemented.'
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':final}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':4,'output_tokens':2}}))
''')
        self.job['checks']=[{'argv':[sys.executable,'-B','-c',"from app import answer; assert answer == 42"],
                             'failure_cause':'implementation'}]
        result=workflow.run_workflow(self.project,KIT,self.job,max_calls=6,
                                     codex_command=[sys.executable,str(script)])
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(result['model_attempts'],4)
        self.assertEqual(result['usage']['totals']['total_tokens'],48)
        self.assertEqual(result['usage']['totals']['cached_input_tokens'],16)
        self.assertEqual([p['receipt']['profile'] for p in result['receipts']],
                         ['cheap','strong','cheap','strong'])
        self.assertNotEqual(result['receipts'][1]['receipt']['evidence_ref'],
                            result['receipts'][3]['receipt']['evidence_ref'])

    def test_keyboard_interrupt_retains_dispatch_lock_for_reconciliation(self):
        def runner(*a,**kw):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.run_flow(runner=runner)
        self.assertTrue((self.project/'.orchestra/workflow.lock').exists())
        with self.assertRaisesRegex(ValueError,'reconcile'):self.run_flow()

    def test_repeated_worker_failure_stops_even_when_profiles_alias_same_model(self):
        config=self.project/'.orchestra/project.toml'
        config.write_text(config.read_text().replace('model = "gpt-5.6-terra"','model = "gpt-5.6-luna"').replace('effort = "high"','effort = "medium"'))
        def runner(project,kit,job,**kw):
            r=self.runner(project,kit,job,**kw)
            r.update(status='failed',failure='implementation',requested_model='gpt-5.6-luna')
            r['attempts'][0].update(requested_model='gpt-5.6-luna')
            check=r['attempts'][0]['checks'][0];check['returncode']=1
            Path(check['log']).write_text('AssertionError: same failure')
            return r
        result=self.run_flow(runner=runner)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(result['stop_reason'],'repeated-failure-without-progress')

    def test_token_threshold_applies_across_worker_and_reviewer(self):
        config=self.project/'.orchestra/project.toml'
        config.write_text(config.read_text().replace('max_repair_cycles = 2','max_repair_cycles = 2\nmax_observed_tokens = 12'))
        result=self.run_flow()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(result['stop_reason'],'token-budget')

    def test_identical_review_findings_do_not_repeat_same_repair_route(self):
        config=self.project/'.orchestra/project.toml'
        config.write_text(config.read_text()+'\n[routing]\nenabled = false\n')
        def runner(project,kit,job,**kw):
            r=self.runner(project,kit,job,**kw)
            if job['role']=='reviewer':
                self.reject_review(project,job,r,'Same finding')
                r['attempts'][0].update(requested_model='gpt-5.6-sol',requested_effort='high')
            return r
        result=self.run_flow(runner=runner)
        self.assertEqual(len(self.calls),4)
        self.assertEqual(result['stop_reason'],'repeated-failure-without-progress')

    def test_nested_instruction_changes_invalidate_reuse(self):
        (self.project/'sub').mkdir()
        (self.project/'sub/app.py').write_text('initial')
        (self.project/'sub/AGENTS.md').write_text('First policy')
        self.job['evidence_files']=['sub/app.py']
        options={'reuse':True,'environment':{'runtime':'test'},'dependencies':[]}
        self.run_flow(**options)
        (self.project/'sub/AGENTS.md').write_text('Changed policy')
        self.assertEqual(self.run_flow(**options)['model_attempts'],2)

    def test_dispatched_empty_attempts_never_count_as_zero_or_start_repairs(self):
        for status in ('failed','cancelled'):
            self.calls=[]
            def runner(project,kit,job,**kw):
                r=self.runner(project,kit,job,**kw)
                r.update(status=status,attempts=[],failure='review verdict: FAIL',final='VERDICT: FAIL\nUnknown dispatch')
                return r
            result=self.run_flow(runner=runner)
            self.assertEqual(len(self.calls),1)
            self.assertIsNone(result['model_attempts'])
            self.assertFalse(result['usage']['complete'])
            self.assertEqual(result['stop_reason'],'dispatch-uncertain')

    def test_token_stop_does_not_count_a_planned_repair_as_executed(self):
        config=self.project/'.orchestra/project.toml'
        config.write_text(config.read_text().replace('max_repair_cycles = 2','max_repair_cycles = 2\nmax_observed_tokens = 24'))
        def runner(project,kit,job,**kw):
            r=self.runner(project,kit,job,**kw)
            if job['role']=='reviewer':self.reject_review(project,job,r,'Wrong')
            return r
        result=self.run_flow(runner=runner)
        self.assertEqual(result['stop_reason'],'token-budget')
        self.assertEqual(result['repairs'],0)
        self.assertEqual(result['model_attempts'],2)

    def test_fresh_receipt_must_prove_required_checks_and_exact_review_report(self):
        for mismatch in ('checks','review'):
            self.calls=[]
            def runner(project,kit,job,**kw):
                r=self.runner(project,kit,job,**kw)
                if mismatch=='checks':
                    cert_path=project/r['evidence_ref'];cert=json.loads(cert_path.read_text())
                    cert['checks'][0]['argv']=[sys.executable,'-c','print("wrong check")']
                    cert_path.write_text(json.dumps(cert))
                elif job['role']=='reviewer':
                    r['final']='VERDICT: PASS\nA different report'
                return r
            result=self.run_flow(runner=runner)
            self.assertEqual(result['status'],'failed',mismatch)
            self.assertFalse(result['quality_passed'])
            self.assertFalse((self.project/'.orchestra/workflow.lock').exists())

    def test_optional_cache_write_failure_does_not_erase_known_usage_or_leave_lock(self):
        with patch.object(workflow,'store_verification',side_effect=ValueError('unavailable cache')):
            result=self.run_flow(reuse=True,environment={'runtime':'test'},dependencies=[])
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['usage']['totals']['total_tokens'],24)
        self.assertFalse((self.project/'.orchestra/workflow.lock').exists())

    def test_reuse_supports_string_and_structured_original_criteria(self):
        for criteria in ('Correct addition',{'behavior':'Correct addition'}):
            self.job['brief']['ACCEPTANCE CRITERIA']=criteria
            options={'reuse':True,'environment':{'runtime':'test'},'dependencies':[]}
            self.run_flow(**options)
            self.assertEqual(self.run_flow(**options)['model_attempts'],1)

    def test_dispatched_receipt_cannot_declare_its_own_calls_free(self):
        def runner(project,kit,job,**kw):
            r=self.runner(project,kit,job,**kw)
            r['reused']=True
            return r
        result=self.run_flow(runner=runner)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['model_attempts'],2)
        self.assertEqual(result['usage']['totals']['total_tokens'],24)

    def test_unbound_failed_review_cannot_buy_a_repair(self):
        def runner(project,kit,job,**kw):
            r=self.runner(project,kit,job,**kw)
            if job['role']=='reviewer':
                r.update(status='failed',failure='review verdict: FAIL',final='VERDICT: FAIL\nUnbound finding')
            return r
        result=self.run_flow(runner=runner)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['stop_reason'],'stale-or-missing-evidence')


if __name__=='__main__':unittest.main()
