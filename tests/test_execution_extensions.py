import json
import sys
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from tests import test_execution


class ExtensionTests(unittest.TestCase):
    setUp=test_execution.ExecutionTests.setUp
    _restore_env=test_execution.ExecutionTests._restore_env
    job=test_execution.ExecutionTests.job
    execute=test_execution.ExecutionTests.execute
    def test_cancel_and_terminal_finalization_have_one_winner(self):
        finalizing = threading.Event()
        release = threading.Event()
        cancel_started = threading.Event()
        cancel_done = threading.Event()
        results, cancellations = [], []
        original = self.execution._write_execution_manifest

        def paused_write(path, execution_uuid, state, *args, **kwargs):
            if state == 'terminal':
                finalizing.set()
                if not release.wait(5):
                    raise RuntimeError('test failed to release finalization')
            return original(path, execution_uuid, state, *args, **kwargs)

        def cancel():
            cancel_started.set()
            run = next((self.project/'.orchestra/executions').glob('*/run.json'))
            try:
                cancellations.append(self.execution.cancel_execution(self.project, run.parent.name, 'race'))
            except ValueError as exc:
                cancellations.append(str(exc))
            finally:
                cancel_done.set()

        with patch.object(self.execution, '_write_execution_manifest', side_effect=paused_write):
            runner = threading.Thread(target=lambda: results.append(self.execute(self.job())))
            requester = threading.Thread(target=cancel)
            runner.start()
            try:
                self.assertTrue(finalizing.wait(5))
                requester.start()
                self.assertTrue(cancel_started.wait(5))
                cancel_done.wait(.2)
                release.set()
                runner.join(5)
                requester.join(5)
                self.assertFalse(runner.is_alive())
                self.assertFalse(requester.is_alive())
                if isinstance(cancellations[0], dict):
                    self.assertEqual(results[0]['status'], 'cancelled')
                else:
                    self.assertIn('already terminal', cancellations[0])
                    self.assertEqual(results[0]['status'], 'verified')
            finally:
                release.set()
                runner.join(6)
                if requester.ident is not None:
                    requester.join(6)

    def test_large_trace_keeps_final_and_rejects_trailing_corruption(self):
        original = self.cli.read_text()
        self.cli.write_text(original.replace("mode = os.environ.get", "for _ in range(600): print(json.dumps({'type':'item.completed','item':{'type':'command_execution','aggregated_output':'x'*4000}}))\nmode = os.environ.get"))
        self.assertEqual(self.execute(self.job())['status'], 'verified')
        self.cli.write_text(self.cli.read_text()+"\nprint('broken')\n")
        self.assertEqual(self.execute(self.job())['status'], 'failed')

    def test_result_alias_preserves_records_without_model_retry(self):
        self.cli.write_text(self.cli.read_text().replace("'text': 'finished'", "'text': json.dumps({'cards':[{'id':1,'evidence':'source'}]})"))
        receipt = self.execute(self.job(result_contract={'required':{'repositories':'array'}, 'aliases':{'cards':'repositories'}}))
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual(json.loads(receipt['final']), {'repositories':[{'id':1,'evidence':'source'}]})
        self.assertEqual(self.state.read_text(),'1')
        self.assertTrue(receipt['normalizations'])

    def test_bad_result_contract_stops_without_repair(self):
        receipt = self.execute(self.job(result_contract={'required':{'repositories':'array'},'aliases':{}}))
        self.assertEqual(receipt['status'],'failed')
        self.assertIn('contract',receipt['failure'])
        self.assertEqual(self.state.read_text(),'1')

    def test_launch_limit_stops_automatic_repair_before_another_request(self):
        config=self.project/'.orchestra/project.toml'
        config.write_text(config.read_text().replace('max_repair_cycles = 2','max_repair_cycles = 2\nmax_launches = 1'))
        receipt=self.execute(self.job(checks=[{'argv':[sys.executable,'-c','raise SystemExit(1)'],'failure_cause':'implementation'}]))
        self.assertEqual(receipt['status'],'failed')
        self.assertEqual(self.state.read_text(),'1')
        self.assertIn('launch budget',receipt['failure'])

    def test_cancellation_kills_descendant_even_if_leader_exits_first(self):
        marker=Path(self.temp.name)/'descendant'
        child=f"import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(1);Path({str(marker)!r}).write_text('escaped')"
        self.cli.write_text(f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{child!r}])\ntime.sleep(30)\n")
        result=[];thread=threading.Thread(target=lambda:result.append(self.execute(self.job(),timeout_seconds=60)));thread.start()
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                runs=list((self.project/'.orchestra/executions').glob('*/run.json'))
                if runs:break
                time.sleep(.01)
            time.sleep(.15)
            self.execution.cancel_execution(self.project,runs[0].parent.name,'stop descendants')
            thread.join(8);self.assertFalse(thread.is_alive());self.assertEqual(result[0]['status'],'cancelled')
            time.sleep(1.1);self.assertFalse(marker.exists())
        finally:thread.join(35)

    def test_checks_pin_tested_subject_and_mutation_invalidates_pass(self):
        subject = self.project/'app.py';subject.write_text('before')
        receipt=self.execute(self.job(evidence_files=['app.py']))
        self.assertEqual(receipt['status'],'verified',receipt)
        self.assertTrue(receipt['evidence_ref'])
        from orchestra_kit.evidence import verify_evidence
        verify_evidence(self.project,receipt['evidence_ref'],kind='check',required_files=['app.py'])
        subject.write_text('after')
        with self.assertRaisesRegex(ValueError,'stale'):
            verify_evidence(self.project,receipt['evidence_ref'],kind='check',required_files=['app.py'])
        receipt=self.execute(self.job(evidence_files=['app.py'], checks=[{'argv':[sys.executable,'-c',"from pathlib import Path; Path('app.py').write_text('changed')"],'failure_cause':'implementation'}]))
        self.assertEqual(receipt['status'],'failed')
        self.assertIn('changed:app.py',receipt['failure'])

    def test_explicit_cancel_drains_running_leaf_and_journals_reason(self):
        self.cli.write_text("import time\ntime.sleep(30)\n")
        result=[]
        thread=threading.Thread(target=lambda:result.append(self.execute(self.job(),timeout_seconds=60)))
        thread.start()
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                runs=list((self.project/'.orchestra/executions').glob('*/run.json'))
                if runs:break
                time.sleep(.01)
            self.assertTrue(runs)
            self.execution.cancel_execution(self.project,runs[0].parent.name,'user requested stop')
            thread.join(8)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0]['status'],'cancelled')
            self.assertIn('user requested stop',result[0]['reason'])
            events=[json.loads(s) for s in Path(result[0]['events_path']).read_text().splitlines()]
            self.assertEqual(events[-1]['phase'],'cancelled')
            self.assertTrue(all(e['leaf_id']==runs[0].parent.name for e in events))
        finally:
            thread.join(35)

    def test_prepared_workspace_enforces_scope_and_keeps_parent_unchanged(self):
        import subprocess
        from orchestra_kit.isolation import prepare_task_worktree
        def git(*args):subprocess.run(['git',*args],cwd=self.project,check=True,capture_output=True)
        git('init');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        (self.project/'app.py').write_text('parent')
        git('add','app.py');git('commit','-m','fixture')
        workspace=Path(self.temp.name)/'leaf'
        prepare_task_worktree(self.project,test_execution.KIT,'HEAD',workspace)
        self.cli.write_text(self.cli.read_text()+"\npathlib.Path('app.py').write_text('leaf')\n")
        receipt=self.execute(self.job(evidence_files=['app.py']),workspace=workspace,write_files=['app.py'])
        self.assertEqual(receipt['status'],'verified')
        self.assertTrue(receipt['integration_required'])
        self.assertEqual((self.project/'app.py').read_text(),'parent')
        self.cli.write_text(self.cli.read_text()+"\npathlib.Path('evil.py').write_text('unexpected')\n")
        receipt=self.execute(self.job(evidence_files=['app.py']),workspace=workspace,write_files=['app.py'])
        self.assertEqual(receipt['status'],'failed')
        self.assertIn('scope',receipt['failure'])

    def test_isolated_executions_share_parent_capacity(self):
        import subprocess
        from orchestra_kit.isolation import prepare_task_worktree
        def git(*args):subprocess.run(['git',*args],cwd=self.project,check=True,capture_output=True)
        git('init');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        (self.project/'app.py').write_text('base');git('add','app.py');git('commit','-m','fixture')
        config=self.project/'.orchestra/project.toml';config.write_text(config.read_text().replace('max_parallel = 3','max_parallel = 1'))
        first=Path(self.temp.name)/'first';second=Path(self.temp.name)/'second'
        for w in [first,second]:prepare_task_worktree(self.project,test_execution.KIT,'HEAD',w)
        acquired, release = threading.Event(), threading.Event()
        original = self.execution._write_execution_manifest

        def hold_capacity(path, execution_uuid, state, *args, **kwargs):
            result = original(path, execution_uuid, state, *args, **kwargs)
            if state == 'launch' and first.resolve() in path.resolve().parents:
                acquired.set()
                if not release.wait(30):
                    raise RuntimeError('test failed to release capacity')
            return result

        result = []
        thread = threading.Thread(target=lambda: result.append(self.execute(
            self.job(evidence_files=['app.py']), workspace=first, write_files=['app.py'])))
        with patch.object(self.execution, '_write_execution_manifest', side_effect=hold_capacity):
            thread.start()
            try:
                self.assertTrue(acquired.wait(30))
                blocked = self.execute(self.job(evidence_files=['app.py']), workspace=second, write_files=['app.py'])
                self.assertEqual(blocked['status'], 'locked')
                self.assertIn('capacity', blocked['reason'])
                release.set()
                thread.join(30)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result[0]['status'], 'verified')
            finally:
                release.set()
                thread.join(30)
