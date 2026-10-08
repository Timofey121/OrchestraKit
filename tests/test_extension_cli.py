import json
import sys
import unittest
from tests import test_cli

class ExtensionCLITests(unittest.TestCase):
    setUp=test_cli.CliTests.setUp if hasattr(test_cli,'CliTests') else test_cli.CLITests.setUp
    tearDown=test_cli.CliTests.tearDown if hasattr(test_cli,'CliTests') else test_cli.CLITests.tearDown
    run_cli=test_cli.CliTests.run_cli if hasattr(test_cli,'CliTests') else test_cli.CLITests.run_cli

    def settlement_fixture(self):
        from orchestra_kit.evidence import create_evidence
        from orchestra_kit.fingerprint import fingerprint_paths
        self.assertEqual(self.run_cli('init', str(self.project)).returncode, 0)
        (self.project/'app.py').write_text('working')
        (self.project/'other.py').write_text('uncovered')
        config=self.project/'.orchestra/project.toml'
        config.write_text(config.read_text().replace('require_fresh_evidence = false', 'require_fresh_evidence = true'))
        data=self.project/'check.json'
        data.write_text(json.dumps({'files':['app.py'], 'checks':[{'argv':[sys.executable,'-c','print("passed")'], 'failure_cause':'implementation'}]}))
        run=self.run_cli('evidence','run',str(self.project),'--input',str(data))
        self.assertEqual(run.returncode,0,run.stderr)
        checked=json.loads(run.stdout)
        report=self.project/'review.txt'
        report.write_text('VERDICT: PASS\nTest fixture report; not a model independence attestation.')
        review_ref=create_evidence(self.project,['app.py'],checked['checks'],review_verdict='PASS',review_report=report)
        data.write_text(json.dumps({'graph_id':'g','nodes':[
            {'id':'one','depends_on':[],'job':{},'acceptance_refs':['works'],'source_snapshot':fingerprint_paths(self.project,['app.py'])},
            {'id':'child','depends_on':['one'],'job':{},'acceptance_refs':['later'],'source_snapshot':fingerprint_paths(self.project,['app.py'])}]}))
        submitted=self.run_cli('queue','submit',str(self.project),'--input',str(data))
        self.assertEqual(submitted.returncode,0,submitted.stderr)
        claim=json.loads(self.run_cli('queue','claim',str(self.project),'g','--owner','root').stdout)
        result={'status':'completed','summary':'root accepted','changed_files':['app.py'],
            'checks':[{'command':'actual check','outcome':'pass','evidence':'test log','evidence_ref':checked['evidence_ref']}],
            'reviews':[{'level':'final','verdict':'PASS','evidence':'fixture report','evidence_ref':review_ref}],
            'acceptance':[{'criterion':'works','satisfied':True,'evidence':'fixture acceptance'}]}
        settlement={'status':'verified','result':result,'evidence_ref':checked['evidence_ref'],'observed_tokens':42}
        return settlement,claim,checked,report

    def settle(self, settlement, claim):
        data=self.project/'settlement.json'
        data.write_text(json.dumps(settlement))
        return self.run_cli('queue','settle',str(self.project),'g','one','--owner','root',
                            '--lease-token',claim['lease_token'],'--input',str(data))

    def assert_still_running(self):
        rows=json.loads(self.run_cli('queue','inspect',str(self.project),'g').stdout)['tasks']
        self.assertEqual({r['id']:r['status'] for r in rows},{'one':'running','child':'pending'})

    def test_public_settlement_enforces_root_criteria_and_all_fresh_gate_refs(self):
        settlement,claim,checked,_=self.settlement_fixture()
        cases=[]
        def case(message, mutate):
            data=json.loads(json.dumps(settlement));mutate(data);cases.append((message,data))
        case('completed root acceptance',lambda d:d['result'].update(status='running'))
        case('acceptance criteria not covered',lambda d:d['result']['acceptance'][0].update(criterion='different'))
        case('satisfied acceptance',lambda d:d['result']['acceptance'][0].update(satisfied=False))
        case('passing required checks',lambda d:d['result']['checks'][0].update(outcome='fail'))
        case('passing review gates',lambda d:d['result'].update(reviews=[]))
        case('fresh evidence refs',lambda d:d['result']['checks'][0].pop('evidence_ref'))
        case('fresh evidence refs',lambda d:d['result']['reviews'][0].pop('evidence_ref'))
        case('independent PASS',lambda d:d['result']['reviews'][0].update(evidence_ref=checked['evidence_ref']))
        case('required subjects',lambda d:d['result'].update(changed_files=['app.py','other.py']))
        case('required check evidence',lambda d:d.update(evidence_ref=d['result']['reviews'][0]['evidence_ref']))
        for message,data in cases:
            with self.subTest(message=message):
                rejected=self.settle(data,claim)
                self.assertNotEqual(rejected.returncode,0)
                self.assertIn(message,rejected.stderr)
                self.assert_still_running()
        accepted=self.settle(settlement,claim)
        self.assertEqual(accepted.returncode,0,accepted.stderr)
        rows=json.loads(accepted.stdout)['tasks']
        self.assertEqual({r['id']:r['status'] for r in rows},{'one':'verified','child':'ready'})
        self.assertEqual(json.loads(accepted.stdout)['budget']['observed_tokens'],42)

    def test_public_settlement_rejects_stale_sources_logs_reports_and_lease(self):
        settlement,claim,checked,report=self.settlement_fixture()
        for path,message in [(self.project/'app.py','stale evidence'),
                             (self.project/checked['checks'][0]['log'],'artifact changed'),
                             (report,'review artifact changed')]:
            with self.subTest(path=path.name):
                original=path.read_bytes()
                try:
                    path.write_bytes(b'changed')
                    rejected=self.settle(settlement,claim)
                    self.assertNotEqual(rejected.returncode,0)
                    self.assertIn(message,rejected.stderr)
                    self.assert_still_running()
                finally:
                    path.write_bytes(original)
        rejected=self.settle(settlement,dict(claim,lease_token='wrong'))
        self.assertNotEqual(rejected.returncode,0)
        self.assertIn('lease',rejected.stderr)
        self.assert_still_running()
        accepted=self.settle(settlement,claim)
        self.assertEqual(accepted.returncode,0,accepted.stderr)

    def test_evidence_and_queue_lifecycle_needs_actual_fresh_gates(self):
        self.assertEqual(self.run_cli('init',str(self.project)).returncode,0)
        (self.project/'app.py').write_text('works')
        config=self.project/'.orchestra/project.toml'
        config.write_text(config.read_text().replace('require_fresh_evidence = false','require_fresh_evidence = true'))
        data=self.project/'check.json';data.write_text(json.dumps({'files':['app.py'],'checks':[{'argv':[sys.executable,'-c','print("passed")'],'failure_cause':'implementation'}]}))
        checked=self.run_cli('evidence','run',str(self.project),'--input',str(data))
        self.assertEqual(checked.returncode,0,checked.stderr)
        result=json.loads(checked.stdout)
        validated=self.run_cli('evidence','check',str(self.project),'--ref',result['evidence_ref'],'--path','app.py')
        self.assertEqual(validated.returncode,0,validated.stderr)
        from orchestra_kit.fingerprint import fingerprint_paths
        data.write_text(json.dumps({'graph_id':'g','nodes':[{'id':'one','depends_on':[],'job':{},'acceptance_refs':['works'],'source_snapshot':fingerprint_paths(self.project,['app.py'])}]}))
        submitted=self.run_cli('queue','submit',str(self.project),'--input',str(data))
        self.assertEqual(submitted.returncode,0,submitted.stderr)
        claim=self.run_cli('queue','claim',str(self.project),'g','--owner','root')
        self.assertEqual(claim.returncode,0,claim.stderr)
        self.assertEqual(json.loads(claim.stdout)['id'],'one')
        observed=self.run_cli('queue','inspect',str(self.project),'g')
        self.assertEqual(json.loads(observed.stdout)['tasks'][0]['status'],'running')
