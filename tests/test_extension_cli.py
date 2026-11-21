import json
import shlex
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
        checks = [{"command": shlex.join(checked["checks"][0]["argv"]),
                   "outcome": "pass", "evidence": "test log",
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
            missing["command"] = shlex.join(extra["checks"][0]["argv"])
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

    def test_task_record_rejects_evidence_for_a_different_command(self):
        task, target, checks, result = self.command_evidence_fixture()
        history = self.project / ".orchestra/runs" / f"{task['task_id']}.json"
        before = history.read_bytes()
        for executed in ([sys.executable, "-c", 'print("ok")'],
                         [*target[:-1], "PipelineAcceptance.test_other_stage"]):
            with self.subTest(argv=executed):
                result["checks"][0]["evidence_ref"] = self.bind_check_commands(task, checks, [executed])
                rejected = self.record_check_result(task, result)
                self.assertEqual(rejected.returncode, 2, rejected.stderr)
                self.assertIn("command", rejected.stderr)
                self.assertIn(target[-1], rejected.stderr)
                self.assertEqual(history.read_bytes(), before)

        result["checks"][0]["evidence_ref"] = self.bind_check_commands(task, checks, [target])
        accepted = self.record_check_result(task, result)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        shown = self.show_check_task(task)
        self.assertEqual((shown["task_id"], shown["status"], shown["revision"],
                          shown["verification_state"]),
                         (task["task_id"], "completed", task["revision"] + 1, "current"))

    def test_task_show_marks_preexisting_mismatched_command_stale_without_writing(self):
        task, target, checks, result = self.command_evidence_fixture()
        result["checks"][0]["evidence_ref"] = self.bind_check_commands(task, checks, [target])
        accepted = self.record_check_result(task, result)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(self.show_check_task(task)["verification_state"], "current")

        # Reconstruct the format written by 0.13.2, which accepted this command mismatch.
        history = self.project / ".orchestra/runs" / f"{task['task_id']}.json"
        saved = json.loads(history.read_text())
        wrong = shlex.join([*target[:-1], "PipelineAcceptance.test_other_stage"])
        for command in (wrong, "python 'unterminated", None):
            with self.subTest(command=command):
                for record in (saved["last_result"], saved["history"][-1]["result"]):
                    check = record["checks"][0]
                    if command is None:
                        check.pop("command")
                    else:
                        check["command"] = command
                history.write_text(json.dumps(saved))
                before = history.read_bytes()
                shown = self.show_check_task(task)
                self.assertEqual((shown["status"], shown["revision"], shown["verification_state"]),
                                 ("completed", saved["revision"], "stale"))
                self.assertEqual(history.read_bytes(), before)

    def test_task_accepts_one_certificate_for_multiple_quoted_commands(self):
        task, target, checks, result = self.command_evidence_fixture()
        code = "from pathlib import Path; Path('command-ran').touch(); print('quoted value')"
        additional = [sys.executable, "-c", code, "report name"]
        ref = self.bind_check_commands(task, checks, [additional, target])
        marker = self.project / "command-ran"
        marker.unlink()
        result["checks"][0]["evidence_ref"] = ref
        result["checks"].append({"command": shlex.join(additional[:-1]) + r" report\ name",
                                 "outcome": "pass", "evidence": "check log",
                                 "required": True, "evidence_ref": ref})
        result["checks"].append({"command": "optional diagnostic", "outcome": "pass",
                                 "evidence": "optional", "required": False, "evidence_ref": ref})
        accepted = self.record_check_result(task, result)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(self.show_check_task(task)["verification_state"], "current")
        self.assertFalse(marker.exists())

    def command_evidence_fixture(self):
        from orchestra_kit.evidence import create_evidence

        project = str(self.project)
        initialized = self.run_cli("init", project)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        source = self.project / "acceptance/ci_dry_run.py"
        source.parent.mkdir()
        source.write_text(
            "import unittest\n"
            "class PipelineAcceptance(unittest.TestCase):\n"
            "    def test_retry_only_current_stage(self): self.assertEqual(1, 1)\n"
            "    def test_other_stage(self): self.assertEqual(2, 2)\n"
            "if __name__ == '__main__': unittest.main()\n",
        )
        target = [sys.executable, "acceptance/ci_dry_run.py",
                  "PipelineAcceptance.test_retry_only_current_stage"]
        started = self.run_cli("task", "start", project, "--goal", "Verify stage retry")
        self.assertEqual(started.returncode, 0, started.stderr)
        task = json.loads(started.stdout)
        checks = {"files": ["acceptance/ci_dry_run.py"]}
        receipt = self.run_check_commands(checks, [target])
        report = self.project / "final-review.txt"
        report.write_text("VERDICT: PASS\nIndependent final review fixture")
        review_ref = create_evidence(self.project, checks["files"], receipt["checks"],
                                     review_verdict="PASS", review_report=report)
        result = {"status": "completed", "summary": "Stage retry exercised",
                  "changed_files": checks["files"],
                  "checks": [{"command": shlex.join(target), "outcome": "pass",
                              "evidence": "check log"}],
                  "acceptance": [{"criterion": "Stage retry exercised", "satisfied": True,
                                  "evidence": "documented check"}],
                  "reviews": [{"level": "final", "verdict": "PASS", "evidence": "review report",
                               "evidence_ref": self.bind_command_evidence(task, review_ref, "review")}]}
        return task, target, checks, result

    def run_check_commands(self, checks, commands):
        request = self.project / "check-commands.json"
        request.write_text(json.dumps({**checks, "checks": [
            {"argv": argv, "failure_cause": "implementation"} for argv in commands
        ]}))
        ran = self.run_cli("evidence", "run", str(self.project), "--input", str(request))
        self.assertEqual(ran.returncode, 0, ran.stderr)
        return json.loads(ran.stdout)

    def bind_command_evidence(self, task, ref, kind):
        bound = self.run_cli("evidence", "bind", str(self.project), "--ref", ref,
                             "--kind", kind, "--task-id", task["task_id"])
        self.assertEqual(bound.returncode, 0, bound.stderr)
        return json.loads(bound.stdout)["evidence_ref"]

    def bind_check_commands(self, task, checks, commands):
        receipt = self.run_check_commands(checks, commands)
        return self.bind_command_evidence(task, receipt["evidence_ref"], "check")

    def record_check_result(self, task, result):
        path = self.project / "result.json"
        path.write_text(json.dumps(result))
        return self.run_cli("task", "record", str(self.project), task["task_id"],
                            "--input", str(path), "--revision", str(task["revision"]))

    def show_check_task(self, task):
        shown = self.run_cli("task", "show", str(self.project), task["task_id"])
        self.assertEqual(shown.returncode, 0, shown.stderr)
        return json.loads(shown.stdout)

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
