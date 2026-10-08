from __future__ import annotations

import importlib
import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / ".orchestra").mkdir()
        (self.project / ".orchestra" / "project.toml").write_text(
            (KIT / "templates" / "project.toml").read_text(), encoding="utf-8"
        )
        self.cli = Path(self.temp.name) / "fake-codex.py"
        self.cli.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, os, pathlib, sys
            path = pathlib.Path(os.environ['FAKE_STATE'])
            count = int(path.read_text()) if path.exists() else 0
            path.write_text(str(count + 1))
            mode = os.environ.get('FAKE_MODE', 'ok')
            if mode == 'missing-final':
                print(json.dumps({'type': 'turn.completed'}))
            elif mode == 'fail':
                sys.exit(9)
            elif mode == 'review-fail':
                print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'VERDICT: FAIL\\nBlocking defect'}}))
                print(json.dumps({'type': 'turn.completed'}))
            elif mode == 'review-pass':
                print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'VERDICT: PASS\\nNo blocking defects'}}))
                print(json.dumps({'type': 'turn.completed'}))
            else:
                print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'finished'}}))
                print(json.dumps({'type': 'turn.completed'}))
        """), encoding="utf-8")
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        self.state = Path(self.temp.name) / "state"
        self.old_env = {key: os.environ.get(key) for key in ("FAKE_STATE", "FAKE_MODE")}
        os.environ["FAKE_STATE"] = str(self.state)
        os.environ.pop("FAKE_MODE", None)
        self.addCleanup(self._restore_env)
        self.execution = importlib.import_module("orchestra_kit.execution")

    def _restore_env(self) -> None:
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def job(self, **updates: object) -> dict:
        value = {
            "role": "worker", "complexity": "simple", "risk": "low",
            "size": "substantial", "independent": True,
            "brief": {name: name.lower() for name in (
                "GOAL", "SOURCES OF TRUTH", "SCOPE", "ACCEPTANCE CRITERIA",
                "VERIFICATION", "CONTEXT")},
            "checks": [{"argv": ["/usr/bin/true"], "failure_cause": "implementation"}],
        }
        value.update(updates)
        return value

    def execute(self, job: dict, **kwargs: object) -> dict:
        return self.execution.run_job(
            self.project, KIT, job, codex_command=(sys.executable, str(self.cli)), **kwargs
        )

    def set_observed_token_limit(self, limit: int) -> None:
        config = self.project / '.orchestra' / 'project.toml'
        config.write_text((KIT / 'templates' / 'project.toml').read_text().replace(
            'max_repair_cycles = 2', f'max_repair_cycles = 2\nmax_observed_tokens = {limit}'),
            encoding='utf-8')

    def test_cheap_route_executes_and_records_private_trace(self) -> None:
        receipt = self.execute(self.job())
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["profile"], "cheap")
        self.assertEqual(receipt["usage"], None)
        self.assertEqual(self.state.read_text(), "1")
        log = Path(receipt["logs"][0])
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_leaf_marker_is_scoped_to_child_and_preserves_other_environment(self) -> None:
        marker = Path(self.temp.name) / 'marker.json'
        self.cli.write_text(self.cli.read_text().replace("mode = os.environ.get('FAKE_MODE', 'ok')",
            f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'leaf':os.environ.get('ORCHESTRA_LEAF_EXECUTION'), 'state':os.environ.get('FAKE_STATE')}})); mode = 'ok'"))
        before = os.environ.get('ORCHESTRA_LEAF_EXECUTION')
        self.assertEqual(self.execute(self.job())['status'], 'verified')
        self.assertEqual(json.loads(marker.read_text()), {'leaf': '1', 'state': str(self.state)})
        self.assertEqual(os.environ.get('ORCHESTRA_LEAF_EXECUTION'), before)

    def test_terminal_receipt_is_durable_and_includes_observed_failed_attempt_usage(self) -> None:
        self.cli.write_text(self.cli.read_text().replace("{'type': 'turn.completed'}",
            "{'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 10}}"))
        receipt = self.execute(self.job(checks=[{'argv': [sys.executable, '-c', 'raise SystemExit(1)'],
                                               'failure_cause': 'implementation'}]))
        stored = Path(receipt['receipt_path'])
        self.assertEqual(json.loads(stored.read_text()), receipt)
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['usage']['totals']['input_tokens'], 300)

    def test_corrupt_json_trace_cannot_pass_after_valid_completion(self) -> None:
        self.cli.write_text(self.cli.read_text() + "\nprint('not-json')\n")
        receipt = self.execute(self.job())
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(self.state.read_text(), '1')

    def test_non_utf8_json_and_final_after_completion_fail_closed(self) -> None:
        original = self.cli.read_text()
        for code in [
            "import sys; sys.stdout.buffer.write(b'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"fin\\xffished\"}}\\n{\"type\":\"turn.completed\"}\\n')",
            "print(json.dumps({'type':'turn.completed'})); print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'late'}}))"]:
            with self.subTest(code=code):
                self.cli.write_text('import json\n' + code + '\n')
                receipt = self.execute(self.job())
                self.assertEqual(receipt['status'], 'failed')
        self.cli.write_text(original)

    def test_bad_classification_and_nul_commands_fail_preflight_without_artifacts(self) -> None:
        for updates in ({'role': []}, {'complexity': []}, {'risk': None}, {'size': []},
            {'checks': [{'argv': [sys.executable], 'failure_cause': []}]},
            {'checks': [{'argv': [sys.executable, '\0'], 'failure_cause': 'implementation'}]}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.execute(self.job(**updates))
        self.assertFalse((self.project / '.orchestra/executions').exists())

    def test_repair_gets_actual_failure_once_without_duplicate_brief(self) -> None:
        trace = Path(self.temp.name) / 'trace.jsonl'
        self.cli.write_text(self.cli.read_text().replace('path.write_text(str(count + 1))',
            "path.write_text(str(count + 1));\n" +
            f"with open({str(trace)!r}, 'a') as trace:\n trace.write(json.dumps({{'args': sys.argv, 'prompt': sys.stdin.read()}})+'\\n')"))
        checks = [{'argv': [sys.executable, '-c', "print('EXPECTED_FAILURE_DETAIL'); raise SystemExit(1)"],
                   'failure_cause': 'implementation'}]
        job = self.job(checks=checks)
        job['brief']['GOAL'] = 'ONE_TASK_GOAL_MARKER'
        receipt = self.execute(job)
        rows = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual([a['profile'] for a in receipt['attempts']], ['cheap', 'cheap', 'balanced'])
        self.assertEqual(len(rows), 3)
        for row in rows:
            policy = next(arg for arg in row['args'] if arg.startswith('developer_instructions='))
            self.assertNotIn('ONE_TASK_GOAL_MARKER', policy)
            self.assertEqual(row['prompt'].count('ONE_TASK_GOAL_MARKER'), 1)
            self.assertEqual(row['prompt'].count('ACCEPTANCE CRITERIA'), 1)
            self.assertIn('skills.max_context_tokens=1000', row['args'])
        self.assertIn('EXPECTED_FAILURE_DETAIL', rows[1]['prompt'])

    def test_mutation_by_check_invalidates_configuration_and_review(self) -> None:
        for subject in ['.orchestra/project.toml', 'reviewed.txt']:
            with self.subTest(subject=subject):
                reviewed = self.project / 'reviewed.txt'
                reviewed.write_text('unchanged')
                config = self.project / '.orchestra/project.toml'
                config.write_text((KIT / 'templates/project.toml').read_text())
                check = [{'argv': [sys.executable, '-c',
                    f"from pathlib import Path; p=Path({subject!r}); p.write_text(p.read_text()+'\\n# changed')"],
                    'failure_cause': 'implementation'}]
                receipt = self.execute(self.job(checks=check, review_files=['reviewed.txt']))
                self.assertEqual(receipt['status'], 'failed')
                self.assertIn('changed:' + subject, receipt['failure'])

    def test_leaf_failure_event_cannot_pass_after_completed_event(self) -> None:
        self.cli.write_text(self.cli.read_text() + "\nprint(json.dumps({'type':'turn.failed'}))\n")
        receipt = self.execute(self.job())
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(self.state.read_text(), '1')

    def test_missing_checker_is_environment_failure_without_escalation(self) -> None:
        receipt = self.execute(self.job(checks=[{'argv': [str(self.project / 'missing-command')],
                                               'failure_cause': 'implementation'}]))
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['failure'], 'environment')
        self.assertEqual(self.state.read_text(), '1')

    def test_passing_check_with_excess_output_is_rejected_as_environment_failure(self) -> None:
        check = [sys.executable, '-c', "import sys; sys.stdout.write('x' * 2_500_000)"]
        with patch('orchestra_kit.log_capture.MAX_RETAINED_BYTES', 256):
            receipt = self.execute(self.job(checks=[{
                'argv': check, 'failure_cause': 'implementation',
            }]))
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('environment:', receipt['failure'])
        self.assertIn('output exceeded', receipt['failure'])
        self.assertEqual(receipt['attempts'][0]['checks'][0]['returncode'], 0)
        log = Path(receipt['attempts'][0]['checks'][0]['log'])
        self.assertLessEqual(log.stat().st_size, 256)
        self.assertIn('[ORCHESTRA OUTPUT TRUNCATED]', log.read_text())

    def test_truncated_leaf_jsonl_cannot_supply_final_or_usage(self) -> None:
        self.cli.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json
            print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'finished'}}))
            print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
            print('x' * 4096)
        """), encoding='utf-8')
        with patch('orchestra_kit.log_capture.MAX_RETAINED_BYTES', 256):
            receipt = self.execute(self.job())
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('environment:', receipt['failure'])
        self.assertIn('output exceeded', receipt['failure'])
        self.assertIsNone(receipt['final'])
        self.assertIsNone(receipt['usage'])

    def test_check_timeout_reaps_its_child_process_group(self) -> None:
        marker = Path(self.temp.name) / 'leaked-child'
        child_code = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).write_text('leaked')"
        parent_code = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(20)"
        receipt = self.execute(self.job(checks=[{'argv': [sys.executable, '-c', parent_code],
                                               'failure_cause': 'implementation'}]), timeout_seconds=1)
        self.assertEqual(receipt['failure'], 'environment')
        self.assertEqual(len(receipt['attempts']), 1)
        time.sleep(2.2)
        self.assertFalse(marker.exists())

    def test_changed_policy_overlay_invalidates_leaf_checks(self) -> None:
        policy = self.project / 'policy.md'
        policy.write_text('Project policy')
        config = self.project / '.orchestra/project.toml'
        config.write_text(config.read_text().replace('policy_files = []', 'policy_files = ["policy.md"]'))
        check = [{'argv': [sys.executable, '-c', "from pathlib import Path; Path('policy.md').write_text('changed')"],
                  'failure_cause': 'implementation'}]
        receipt = self.execute(self.job(checks=check))
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('changed:policy.md', receipt['failure'])

    def test_repair_cannot_overrun_brief_budget_or_drop_criteria(self) -> None:
        from orchestra_kit.config import load_config
        from orchestra_kit.routing import build_brief
        config = load_config(self.project, KIT)
        job = self.job(checks=[{'argv': [sys.executable, '-c', "print('failure'); raise SystemExit(1)"],
                               'failure_cause': 'implementation'}])
        original_size = len(build_brief(config, job['brief']))
        job['brief']['CONTEXT'] += 'x' * (config.context.max_brief_chars - original_size)
        receipt = self.execute(job)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('context:', receipt['failure'])
        self.assertEqual(len(receipt['attempts']), 1)

    def test_first_check_failure_repairs_then_escalates_on_second_failure(self) -> None:
        checker = Path(self.temp.name) / "check.py"
        checker.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
        receipt = self.execute(self.job(checks=[{"argv": ["python3", str(checker)], "failure_cause": "implementation"}]))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(self.state.read_text(), "3")
        self.assertEqual(receipt["attempts"][-1]["profile"], "balanced")

    def test_observed_token_limit_allows_below_and_stops_at_or_above_limit(self) -> None:
        self.cli.write_text(self.cli.read_text().replace("{'type': 'turn.completed'}",
            "{'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 10}}"))
        for limit, calls, decision in ((1000, '3', 'allow-retry'), (110, '1', 'stop-token-budget'),
                                       (109, '1', 'stop-token-budget')):
            with self.subTest(limit=limit):
                self.set_observed_token_limit(limit)
                self.state.unlink(missing_ok=True)
                receipt = self.execute(self.job(checks=[{'argv': [sys.executable, '-c', 'raise SystemExit(1)'],
                                                       'failure_cause': 'implementation'}]))
                self.assertEqual(receipt['status'], 'failed')
                self.assertEqual(self.state.read_text(), calls)
                self.assertEqual(receipt['budget']['decision'], decision)
                manifest = json.loads(Path(receipt['manifests'][0]).read_text())
                self.assertEqual(manifest['failure'], receipt['failure'])
                self.assertEqual(manifest['budget'], receipt['budget'])

    def test_missing_or_malformed_usage_stops_retry_with_unknown_budget(self) -> None:
        self.set_observed_token_limit(999)
        for usage in ('', ", 'usage': {'input_tokens': 'bad', 'output_tokens': 1}"):
            with self.subTest(usage=usage):
                self.state.unlink(missing_ok=True)
                self.cli.write_text(textwrap.dedent("""\
                    #!/usr/bin/env python3
                    import json, os, pathlib
                    path = pathlib.Path(os.environ['FAKE_STATE'])
                    path.write_text(str(int(path.read_text()) + 1) if path.exists() else '1')
                    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'finished'}}))
                    print(json.dumps({'type': 'turn.completed'%s}))
                """ % usage))
                receipt = self.execute(self.job(checks=[{'argv': [sys.executable, '-c', 'raise SystemExit(1)'],
                                                       'failure_cause': 'implementation'}]))
                self.assertEqual(self.state.read_text(), '1')
                self.assertIn('unknown observed token budget', receipt['failure'])
                self.assertEqual(receipt['budget']['observed_total'], None)

    def test_run_manifest_is_atomic_private_and_records_execution_stages(self) -> None:
        receipt = self.execute(self.job())
        manifest = Path(receipt['manifests'][0])
        data = json.loads(manifest.read_text())
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        self.assertEqual(data['schema'], 1)
        self.assertEqual(data['state'], 'terminal')
        self.assertIsNone(data['failure'])
        self.assertEqual(data['budget'], receipt['budget'])
        self.assertEqual(data['execution_uuid'], manifest.parent.name)
        self.assertEqual(data['attempts'][0]['requested_model'], receipt['requested_model'])
        self.assertTrue(Path(data['attempts'][0]['log']).exists())

    def test_interruption_leaves_a_readable_launch_manifest(self) -> None:
        original = self.execution._run_leaf
        def interrupt(*args: object, **kwargs: object) -> tuple[int | None, bool]:
            raise KeyboardInterrupt
        self.execution._run_leaf = interrupt
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.execute(self.job())
        finally:
            self.execution._run_leaf = original
        manifests = list((self.project / '.orchestra' / 'executions').glob('*/run.json'))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())['state'], 'launch')

    def test_mixed_attempt_usage_is_unknown_and_capability_does_not_retry(self) -> None:
        first = Path(self.temp.name) / 'first.jsonl'
        second = Path(self.temp.name) / 'second.jsonl'
        first.write_text(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 2}}) + '\n')
        second.write_text('{}\n')
        budget = self.execution._budget_metadata([str(first), str(second)], 10)
        self.assertEqual(budget['decision'], 'stop-unknown-budget')
        self.set_observed_token_limit(10)
        receipt = self.execute(self.job(checks=[{'argv': [sys.executable, '-c', 'raise SystemExit(1)'],
                                               'failure_cause': 'capability'}]))
        self.assertEqual(self.state.read_text(), '1')
        self.assertEqual(receipt['failure'], 'capability')

    def test_process_failure_fails_closed_without_escalation(self) -> None:
        os.environ["FAKE_MODE"] = "fail"
        receipt = self.execute(self.job())
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(self.state.read_text(), "1")

    def test_empty_checks_and_bad_command_are_rejected_before_execution_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 8"):
            self.execute(self.job(checks=[]))
        with self.assertRaisesRegex(ValueError, "codex_command"):
            self.execution.run_job(self.project, KIT, self.job(), codex_command="codex")
        self.assertFalse((self.project / ".orchestra" / "executions").exists())

    def test_missing_final_never_reports_verified(self) -> None:
        os.environ["FAKE_MODE"] = "missing-final"
        receipt = self.execute(self.job())
        self.assertEqual(receipt["status"], "failed")
        self.assertNotEqual(receipt["status"], "verified")

    def test_reviewer_fail_is_failed_in_receipt_cli_and_dashboard_with_evidence(self) -> None:
        reviewed = self.project / 'reviewed.txt'
        reviewed.write_text('subject', encoding='utf-8')
        job = self.job(role='reviewer', evidence_files=['reviewed.txt'])
        job_path = Path(self.temp.name) / 'review-job.json'
        job_path.write_text(json.dumps(job), encoding='utf-8')
        os.environ['FAKE_MODE'] = 'review-fail'

        from orchestra_kit.cli import main
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(['execute', str(self.project), '--input', str(job_path),
                              '--codex', str(self.cli)])
        receipt = json.loads(output.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['failure'], 'review verdict: FAIL')
        self.assertEqual(receipt['final'], 'VERDICT: FAIL\nBlocking defect')
        self.assertTrue(receipt['evidence_ref'])
        certificate = json.loads((self.project / receipt['evidence_ref']).read_text())
        self.assertEqual(certificate['review_verdict'], 'FAIL')
        self.assertEqual(json.loads(Path(receipt['receipt_path']).read_text()), receipt)

        from orchestra_kit.dashboard import Dashboard
        dashboard = Dashboard([self.project], None, KIT)
        project_id = dashboard.snapshot()['projects'][0]['id']
        projected = dashboard.project_details(project_id)['executions'][0]
        self.assertEqual(projected['status'], 'failed')
        self.assertEqual(projected['failure'], 'review verdict: FAIL')

    def test_reviewer_pass_remains_verified(self) -> None:
        reviewed = self.project / 'reviewed.txt'
        reviewed.write_text('subject', encoding='utf-8')
        os.environ['FAKE_MODE'] = 'review-pass'

        receipt = self.execute(self.job(role='reviewer', evidence_files=['reviewed.txt']))

        self.assertEqual(receipt['status'], 'verified')
        self.assertIsNone(receipt['failure'])
        from orchestra_kit.evidence import verify_evidence
        certificate = verify_evidence(
            self.project, receipt['evidence_ref'], kind='review', required_files=['reviewed.txt'])
        self.assertEqual(certificate['review_verdict'], 'PASS')

    def test_reviewer_fail_without_evidence_files_still_fails_closed(self) -> None:
        os.environ['FAKE_MODE'] = 'review-fail'

        receipt = self.execute(self.job(role='reviewer'))

        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['failure'], 'review verdict: FAIL')
        self.assertIsNone(receipt['evidence_ref'])

    def test_dry_run_and_root_route_create_no_execution_files(self) -> None:
        before = list((self.project / ".orchestra").iterdir())
        dry = self.execute(self.job(), dry_run=True)
        root = self.execute(self.job(size="small", independent=False))
        self.assertEqual(dry["status"], "dry-run")
        self.assertEqual(root["status"], "keep-root")
        self.assertEqual(before, list((self.project / ".orchestra").iterdir()))
        self.assertFalse(self.state.exists())

    def test_receipt_schema_is_consistent_across_execution_outcomes(self) -> None:
        import fcntl
        dry = self.execute(self.job(), dry_run=True)
        root = self.execute(self.job(size='small', independent=False))
        with patch.object(self.execution, 'fcntl', None):
            unsupported = self.execute(self.job())
        lock = self.project / '.orchestra' / 'execution.lock'
        with lock.open('w') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = self.execute(self.job())
        for receipt in (dry, root, unsupported, locked):
            with self.subTest(status=receipt['status']):
                for field in ('final', 'failure', 'reason', 'logs', 'attempts',
                              'budget', 'usage', 'receipt_path', 'manifests'):
                    self.assertIn(field, receipt)
                self.assertIsNone(receipt['receipt_path'])
                self.assertEqual(receipt['manifests'], [])
        verified = self.execute(self.job())
        os.environ['FAKE_MODE'] = 'fail'
        failed = self.execute(self.job())
        self.assertEqual(set(verified), set(failed))
        self.assertEqual(set(verified), set(dry))

    def test_lock_and_stale_review_files_fail_closed(self) -> None:
        import fcntl
        reviewed = self.project / "reviewed.txt"
        reviewed.write_text("before", encoding="utf-8")
        lock = self.project / ".orchestra" / "execution.lock"
        with lock.open("w") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = self.execute(self.job())
        self.assertEqual(receipt["status"], "locked")

        self.cli.write_text(self.cli.read_text().replace(
            "path.write_text(str(count + 1))",
            "path.write_text(str(count + 1)); pathlib.Path(os.environ['REVIEW_FILE']).write_text('after')",
        ), encoding="utf-8")
        os.environ["REVIEW_FILE"] = str(reviewed)
        receipt = self.execute(self.job(review_files=["reviewed.txt"]))
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("changed:reviewed.txt", receipt["failure"])
