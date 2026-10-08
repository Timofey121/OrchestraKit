from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        self.kit = Path(__file__).resolve().parents[1]
        self.environment = dict(os.environ)
        self.environment["PYTHONPATH"] = str(self.kit / "src")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "orchestra_kit", *arguments],
            cwd=self.kit,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_init_sync_and_doctor_work_end_to_end(self) -> None:
        initialized = self.run_cli("init", str(self.project), "--name", "Demo")
        checked = self.run_cli("doctor", str(self.project))

        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertIn("initialized", initialized.stdout)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("healthy", checked.stdout)

    def test_doctor_fails_when_generated_file_drifted_and_sync_repairs_it(self) -> None:
        self.run_cli("init", str(self.project), "--name", "Demo")
        agent = self.project / ".codex/agents/orchestra-worker.toml"
        agent.write_text(agent.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

        drifted = self.run_cli("doctor", str(self.project))
        synchronized = self.run_cli("sync", str(self.project))
        repaired = self.run_cli("doctor", str(self.project))

        self.assertEqual(drifted.returncode, 1)
        self.assertIn("drift", drifted.stderr)
        self.assertEqual(synchronized.returncode, 0, synchronized.stderr)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)

    def test_init_refuses_to_replace_existing_project_configuration(self) -> None:
        first = self.run_cli("init", str(self.project), "--name", "First")
        config_path = self.project / ".orchestra/project.toml"
        before = config_path.read_text(encoding="utf-8")

        second = self.run_cli("init", str(self.project), "--name", "Second")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(config_path.read_text(encoding="utf-8"), before)

    def test_route_brief_and_task_state_commands(self) -> None:
        self.assertEqual(self.run_cli("init", str(self.project)).returncode, 0)
        route = self.run_cli("route", str(self.project), "--role", "worker",
                             "--complexity", "complex", "--independent")
        self.assertEqual(route.returncode, 0, route.stderr)
        self.assertEqual(json.loads(route.stdout)["agent_type"], "orchestra_worker__strong")
        started = self.run_cli("task", "start", str(self.project), "--goal", "Implement a fix")
        self.assertEqual(started.returncode, 0, started.stderr)
        task_id = json.loads(started.stdout)["task_id"]
        result_path = self.project / "result.json"
        result_path.write_text(json.dumps({"status": "running", "summary": "Investigated",
                                         "next_steps": ["Implement"]}))
        record = self.run_cli("task", "record", str(self.project), task_id,
                              "--input", str(result_path), "--revision", "0")
        self.assertEqual(record.returncode, 0, record.stderr)
        shown = self.run_cli("task", "show", str(self.project), task_id)
        self.assertEqual(json.loads(shown.stdout)["revision"], 1)
        listed = self.run_cli("task", "list", str(self.project))
        self.assertEqual(json.loads(listed.stdout)[0]["task_id"], task_id)
        brief_path = self.project / "brief.json"
        brief_path.write_text(json.dumps({key: "bounded" for key in
                             ["GOAL", "SOURCES OF TRUTH", "SCOPE", "ACCEPTANCE CRITERIA",
                              "VERIFICATION", "CONTEXT"]}))
        brief = self.run_cli("brief", str(self.project), "--input", str(brief_path))
        self.assertEqual(brief.returncode, 0, brief.stderr)
        self.assertIn("ACCEPTANCE CRITERIA", brief.stdout)

    def test_invalid_input_returns_error_without_traceback(self) -> None:
        self.run_cli("init", str(self.project))
        input_path = self.project / "broken.json"
        input_path.write_text("{")
        result = self.run_cli("brief", str(self.project), "--input", str(input_path))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_activation_checks_public_log_evidence(self) -> None:
        skill = self.project / 'SKILL.md'
        log = self.project / 'activation.jsonl'
        log.write_text(json.dumps({'type': 'item.completed', 'item': {
            'id': 'read', 'type': 'command_execution', 'command': f'cat {skill}',
            'exit_code': 0, 'aggregated_output': '---\nname: orchestra\n'}}) + '\n')
        result = self.run_cli('activation', str(log), '--skill-path', str(skill))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'pass')
        log.write_text('')
        result = self.run_cli('activation', str(log), '--skill-path', str(skill))
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_fingerprint_cli_detects_changed_review_subject(self) -> None:
        source = self.project / 'app.py'
        source.write_text('before')
        result = self.run_cli('fingerprint', str(self.project), '--path', 'app.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = self.project / 'review.json'
        snapshot.write_text(result.stdout)
        checked = self.run_cli('fingerprint', str(self.project), '--check', str(snapshot))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        source.write_text('after')
        checked = self.run_cli('fingerprint', str(self.project), '--check', str(snapshot))
        self.assertEqual(checked.returncode, 1, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)['problems'], ['changed:app.py'])

    def test_execute_dry_run_preserves_root_routing(self) -> None:
        self.assertEqual(self.run_cli('init', str(self.project)).returncode, 0)
        job = self.project / 'job.json'
        job.write_text(json.dumps({'role': 'worker', 'complexity': 'simple',
            'risk': 'low', 'size': 'small', 'independent': False,
            'brief': {key: 'bounded' for key in ['GOAL', 'SOURCES OF TRUTH', 'SCOPE',
                'ACCEPTANCE CRITERIA', 'VERIFICATION', 'CONTEXT']},
            'checks': [{'argv': [sys.executable, '-c', 'pass'], 'failure_cause': 'implementation'}]}))
        result = self.run_cli('execute', str(self.project), '--input', str(job), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['route']['execution'], 'root')
        self.assertFalse((self.project / '.orchestra/executions').exists())

    def test_install_check_does_not_create_missing_skill(self) -> None:
        skills = self.project / "skills"
        result = self.run_cli("install-skill", "--skills-dir", str(skills), "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(skills.exists())

    def test_bootstrap_install_and_check_work_end_to_end(self) -> None:
        skills = self.project / 'skills'
        codex = self.project / 'codex'
        arguments = ('install-skill', '--skills-dir', str(skills), '--bootstrap',
                     '--codex-home', str(codex))
        installed = self.run_cli(*arguments)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn('primary workflow', (codex / 'AGENTS.md').read_text())
        checked = self.run_cli(*arguments, '--check')
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_setup_installs_and_checks_complete_user_integration(self) -> None:
        skills = self.project / 'skills'
        codex = self.project / 'codex'
        codex.mkdir()
        (codex / 'AGENTS.md').write_text('My global guidance.\n')
        existing_hook = {'hooks': [{'type': 'command', 'command': 'echo mine'}]}
        (codex / 'hooks.json').write_text(json.dumps({
            'hooks': {'Stop': [existing_hook]}, 'custom': {'mine': True},
        }))
        arguments = ('setup', '--skills-dir', str(skills), '--codex-home', str(codex))

        installed = self.run_cli(*arguments)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        report = json.loads(installed.stdout)
        self.assertEqual(report['configuration'], 'installed')
        self.assertEqual(report['problems'], [])
        self.assertEqual(report['runtime_trust'], 'unknown')
        self.assertIn('/hooks', report['next_step'])
        skill = skills / 'orchestra'
        self.assertTrue((skill / 'scripts/lib/orchestra_kit/web/app.js').is_file())
        self.assertTrue((skill / 'scripts/templates/project.toml').is_file())
        self.assertIn('My global guidance.', (codex / 'AGENTS.md').read_text())
        hooks = json.loads((codex / 'hooks.json').read_text())
        self.assertEqual(hooks['hooks']['Stop'], [existing_hook])
        self.assertEqual(hooks['custom'], {'mine': True})

        checked = self.run_cli(*arguments, '--check')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        report = json.loads(checked.stdout)
        self.assertEqual(report['configuration'], 'current')
        self.assertEqual(report['runtime_trust'], 'unknown')

        hook_path = codex / 'hooks.json'
        hook_path.write_text(json.dumps(json.loads(hook_path.read_text())))
        drifted = self.run_cli(*arguments, '--check')
        self.assertEqual(drifted.returncode, 1, drifted.stderr)
        report = json.loads(drifted.stdout)
        self.assertEqual(report['configuration'], 'drifted')
        self.assertTrue(any('byte content' in problem for problem in report['problems']))

        repaired = self.run_cli(*arguments)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        guidance_path = codex / 'AGENTS.md'
        guidance_path.write_text(guidance_path.read_text().replace(
            '## Orchestra workflow selection', '## Broken workflow selection', 1))
        drifted = self.run_cli(*arguments, '--check')
        self.assertEqual(drifted.returncode, 1, drifted.stderr)
        report = json.loads(drifted.stdout)
        self.assertTrue(any('bootstrap' in problem for problem in report['problems']))

    def test_setup_check_does_not_create_a_missing_home(self) -> None:
        skills = self.project / 'missing' / 'skills'
        codex = self.project / 'missing' / 'codex'
        result = self.run_cli('setup', '--skills-dir', str(skills),
                              '--codex-home', str(codex), '--check')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((self.project / 'missing').exists())

    def test_usage_reports_observed_counters_and_rejects_missing_usage(self) -> None:
        log = self.project / 'session.jsonl'
        log.write_text(json.dumps({'type': 'token_usage_record', 'payload': {
            'response_id': 'r1', 'usage': {'input_tokens': 100,
             'cached_input_tokens': 80, 'output_tokens': 20}}}) + '\n')
        result = self.run_cli('usage', str(log))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['totals']['uncached_input_tokens'], 20)
        log.write_text('')
        result = self.run_cli('usage', str(log))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == "__main__":
    unittest.main()
