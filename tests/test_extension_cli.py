import json
import sys
import unittest
from pathlib import Path
from tests import test_cli

class ExtensionCLITests(unittest.TestCase):
    setUp=test_cli.CliTests.setUp if hasattr(test_cli,'CliTests') else test_cli.CLITests.setUp
    tearDown=test_cli.CliTests.tearDown if hasattr(test_cli,'CliTests') else test_cli.CLITests.tearDown
    run_cli=test_cli.CliTests.run_cli if hasattr(test_cli,'CliTests') else test_cli.CLITests.run_cli

    def test_task_rejects_unbound_leaf_pass_even_with_bound_final_pass(self):
        self.assert_task_completion_requires_binding("review")

    def test_task_rejects_unbound_additional_required_check(self):
        self.assert_task_completion_requires_binding("check")

    def assert_task_completion_requires_binding(self, missing_kind):
        from orchestra_kit.evidence import create_evidence

        def call(*args):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        def run_check(expression):
            source = self.project / "checks.json"
            code = f"from pathlib import Path; assert {expression}"
            source.write_text(json.dumps({"files": ["app.py"], "checks": [{
                "argv": [sys.executable, "-c", code],
                "failure_cause": "implementation",
            }]}))
            return call("evidence", "run", project, "--input", str(source))

        def bind(ref, kind):
            binding = call("evidence", "bind", project, "--ref", ref,
                           "--kind", kind, "--task-id", task_id)
            self.assertEqual(binding["task_id"], task_id)
            self.assertEqual(binding["adopted_from"], ref)
            return binding["evidence_ref"]

        project = str(self.project)
        initialized = self.run_cli("init", project)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        source = self.project / "app.py"
        source.write_text("working")
        task_id = call("task", "start", project, "--goal", "Verify the bounded change")["task_id"]
        result_file = self.project / "result.json"

        def record(data, revision):
            result_file.write_text(json.dumps(data))
            return self.run_cli("task", "record", project, task_id, "--input",
                                str(result_file), "--revision", str(revision))

        opened = record({"status": "blocked", "summary": "Evidence still missing",
                         "next_steps": ["Bind the missing certificate"]}, 0)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        saved = call("task", "show", project, task_id)
        history = self.project / ".orchestra/runs" / f"{task_id}.json"
        original_history = history.read_bytes()

        checked = run_check('Path("app.py").is_file()')
        final_report = self.project / "final.txt"
        final_report.write_text("VERDICT: PASS\nIndependent final fixture")
        final_ref = create_evidence(self.project, ["app.py"], checked["checks"],
                                    review_verdict="PASS", review_report=final_report)
        checks = [{"command": "primary", "outcome": "pass", "evidence": "test log",
                   "evidence_ref": bind(checked["evidence_ref"], "check")}]
        reviews = [{"level": "final", "verdict": "PASS", "evidence": "final review",
                    "evidence_ref": bind(final_ref, "review")}]
        if missing_kind == "review":
            missing = {"level": "leaf", "verdict": "PASS", "evidence": "leaf review"}
            reviews.insert(0, missing)
        else:
            missing = {"command": "additional", "outcome": "pass", "required": True,
                       "evidence": "additional check log"}
            checks.append(missing)

        completed = {"status": "completed", "summary": "All criteria met",
                     "changed_files": ["app.py"], "checks": checks, "reviews": reviews,
                     "acceptance": [{"criterion": "works", "satisfied": True,
                                     "evidence": "confirmed by the checks"}]}

        def assert_rejected(message):
            refused = record(completed, saved["revision"])
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn(message, refused.stderr)
            self.assertEqual(call("task", "show", project, task_id), saved)
            self.assertEqual(history.read_bytes(), original_history)

        assert_rejected(f"completion requires fresh {missing_kind} evidence_ref")
        if missing_kind == "review":
            leaf_report = self.project / "leaf.txt"
            leaf_report.write_text("VERDICT: PASS\nIndependent leaf fixture")
            ref = create_evidence(self.project, ["app.py"], checked["checks"],
                                  review_verdict="PASS", review_report=leaf_report)
        else:
            extra = run_check('Path("app.py").read_text() == "working"')
            ref = extra["evidence_ref"]
        missing["evidence_ref"] = bind(ref, missing_kind)

        if missing_kind == "review":
            for artifact, message in [(source, "stale evidence"),
                                      (Path(checked["checks"][0]["log"]), "artifact changed"),
                                      (final_report, "review artifact changed")]:
                with self.subTest(artifact=artifact.name):
                    original = artifact.read_bytes()
                    try:
                        artifact.write_bytes(b"changed")
                        assert_rejected(message)
                    finally:
                        artifact.write_bytes(original)

        finished = record(completed, saved["revision"])
        self.assertEqual(finished.returncode, 0, finished.stderr)
        shown = call("task", "show", project, task_id)
        self.assertEqual(shown, json.loads(finished.stdout))
        self.assertEqual((shown["task_id"], shown["status"], shown["revision"],
                          shown["verification_state"]), (task_id, "completed", 2, "current"))

    def settlement_fixture(self):
        from orchestra_kit.evidence import create_evidence
        from orchestra_kit.fingerprint import fingerprint_paths
        self.assertEqual(self.run_cli('init', str(self.project)).returncode, 0)
        (self.project/'app.py').write_text('working')
        (self.project/'other.py').write_text('uncovered')
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
