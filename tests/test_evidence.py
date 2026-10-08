import json
import sys
import tempfile
import unittest
from pathlib import Path

KIT=Path(__file__).resolve().parents[1]

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name);(self.project/'.orchestra').mkdir()
        (self.project/'.orchestra/project.toml').write_text((KIT/'templates/project.toml').read_text())
        (self.project/'app.py').write_text('working')

    def test_shared_check_execution_boundary_is_public_and_validates(self):
        from orchestra_kit.check_execution import validate_checks
        checks = [{'argv': [sys.executable, '-c', 'pass'], 'failure_cause': 'implementation'}]
        self.assertEqual(validate_checks(checks), checks)
        with self.assertRaisesRegex(ValueError, 'failure_cause'):
            validate_checks([{'argv': ['/usr/bin/true'], 'failure_cause': 'unknown'}])

    def test_actual_check_is_bound_to_source_and_log(self):
        from orchestra_kit.evidence import run_evidence,verify_evidence
        r=run_evidence(self.project,['app.py'],[{'argv':[sys.executable,'-c','print("ok")'],'failure_cause':'implementation'}])
        verify_evidence(self.project,r['evidence_ref'],kind='check',required_files=['app.py'])
        cert=json.loads((self.project/r['evidence_ref']).read_text())
        Path(cert['checks'][0]['log']).write_text('forged')
        with self.assertRaisesRegex(ValueError,'artifact'):
            verify_evidence(self.project,r['evidence_ref'],kind='check',required_files=['app.py'])

    def test_completion_refuses_stale_and_missing_gate_bindings(self):
        from orchestra_kit.evidence import run_evidence,create_evidence
        from orchestra_kit.state import start_task,record_task
        p=self.project/'.orchestra/project.toml';p.write_text(p.read_text().replace('require_fresh_evidence = false','require_fresh_evidence = true'))
        run=start_task(self.project,KIT,'work')
        checked=run_evidence(self.project,['app.py'],[{'argv':[sys.executable,'-c','pass'],'failure_cause':'implementation'}])
        result={'status':'completed','summary':'done','changed_files':['app.py'],'checks':[{'command':'actual','outcome':'pass','evidence':checked['evidence_ref'],'evidence_ref':checked['evidence_ref']}], 'acceptance':[{'criterion':'works','satisfied':True,'evidence':'root inspected'}], 'reviews':[{'level':'final','verdict':'PASS','evidence':'review','evidence_ref':checked['evidence_ref']}]}
        with self.assertRaisesRegex(ValueError,'review'):
            record_task(self.project,KIT,run['task_id'],result,expected_revision=0)
        report=self.project/'review.txt';report.write_text('VERDICT: PASS\nIndependent fixture reviewer')
        cert=create_evidence(self.project,['app.py'],checked['checks'],review_verdict='PASS',owner='independent-review',review_report=report)
        result['reviews'][0]['evidence_ref']=cert
        (self.project/'app.py').write_text('mutated')
        with self.assertRaisesRegex(ValueError,'stale'):
            record_task(self.project,KIT,run['task_id'],result,expected_revision=0)
        checked=run_evidence(self.project,['app.py'],[{'argv':[sys.executable,'-c','pass'],'failure_cause':'implementation'}])
        result['checks'][0]['evidence_ref']=checked['evidence_ref']
        result['reviews'][0]['evidence_ref']=create_evidence(self.project,['app.py'],checked['checks'],review_verdict='PASS',owner='independent-review',review_report=report)
        finished=record_task(self.project,KIT,run['task_id'],result,expected_revision=0)
        self.assertEqual(finished['verification_state'],'current')
        (self.project/'app.py').write_text('later change')
        from orchestra_kit.state import show_task
        self.assertEqual(show_task(self.project,run['task_id'])['verification_state'],'stale')

    def test_separate_review_levels_require_separate_certificates(self):
        from orchestra_kit.evidence import run_evidence,create_evidence
        from orchestra_kit.state import start_task,record_task
        p=self.project/'.orchestra/project.toml'
        p.write_text(p.read_text().replace('mode = "adaptive"','mode = "strict"').replace('review_levels = ["final"]','review_levels = ["leaf", "final"]').replace('require_fresh_evidence = false','require_fresh_evidence = true'))
        run=start_task(self.project,KIT,'work')
        checked=run_evidence(self.project,['app.py'],[{'argv':[sys.executable,'-c','pass'],'failure_cause':'implementation'}])
        report=self.project/'leaf-review.txt';report.write_text('VERDICT: PASS\nIndependent leaf fixture reviewer')
        leaf=create_evidence(self.project,['app.py'],checked['checks'],review_verdict='PASS',owner='leaf-reviewer',review_report=report)
        result={'status':'completed','summary':'done','changed_files':['app.py'],'checks':[{'command':'actual','outcome':'pass','evidence':'checked','evidence_ref':checked['evidence_ref']}],'acceptance':[{'criterion':'works','satisfied':True,'evidence':'root inspected'}],'reviews':[{'level':level,'verdict':'PASS','evidence':'review','evidence_ref':leaf} for level in ['leaf','final']]}
        with self.assertRaisesRegex(ValueError,'separate review'):
            record_task(self.project,KIT,run['task_id'],result,expected_revision=0)
        report=self.project/'final-review.txt';report.write_text('VERDICT: PASS\nIndependent final fixture reviewer')
        result['reviews'][1]['evidence_ref']=create_evidence(self.project,['app.py'],checked['checks'],review_verdict='PASS',owner='final-reviewer',review_report=report)
        finished=record_task(self.project,KIT,run['task_id'],result,expected_revision=0)
        self.assertEqual(finished['verification_state'],'current')

    def test_fanout_policy_fields_are_strict(self):
        from orchestra_kit.config import load_config,ConfigError
        p=self.project/'.orchestra/project.toml';old=p.read_text()
        for value in ['true','0','-1','1.5']:
            p.write_text(old.replace('mode = "adaptive"',f'mode = "adaptive"\nmax_launches = {value}'))
            with self.assertRaises(ConfigError):load_config(self.project,KIT)
        p.write_text(old.replace('mode = "adaptive"','mode = "adaptive"\nmax_launches = 3').replace('require_fresh_evidence = false','require_fresh_evidence = true'))
        self.assertEqual(load_config(self.project,KIT).workflow.max_launches,3)
