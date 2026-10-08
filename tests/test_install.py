from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("orchestra_kit.install"))
        from orchestra_kit.install import check_skill, install_skill
        self.install = install_skill
        self.check = check_skill
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.kit = Path(__file__).resolve().parents[1]
        self.skills = self.root / "skills"

    def test_installed_skill_runs_without_source_checkout_or_pythonpath(self) -> None:
        skill = self.install(self.kit, self.skills)
        self.assertEqual(skill, (self.skills / "orchestra").resolve())
        self.assertEqual(self.check(self.kit, self.skills), ())
        self.assertIn("allow_implicit_invocation: true", (skill / "agents/openai.yaml").read_text())
        project = self.root / "elsewhere"
        project.mkdir()
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        command = [sys.executable, str(skill / "scripts/orchestra.py")]
        for arguments in [("init", str(project)), ("doctor", str(project))]:
            result = subprocess.run(command + list(arguments), cwd=self.root, env=environment,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(command + ["route", str(project), "--role", "worker",
                                "--complexity", "simple", "--risk", "low"],
                                cwd=self.root, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["profile"], "cheap")
        job = self.root / 'job.json'
        job.write_text(json.dumps({'role': 'worker', 'size': 'substantial',
            'independent': True, 'complexity': 'simple', 'risk': 'low',
            'brief': {key: 'bounded' for key in ['GOAL', 'SOURCES OF TRUTH', 'SCOPE',
                'ACCEPTANCE CRITERIA', 'VERIFICATION', 'CONTEXT']},
            'checks': [{'argv': [sys.executable, '-c', 'pass'], 'failure_cause': 'implementation'}]}))
        result = subprocess.run(command + ['execute', str(project), '--input', str(job), '--dry-run'],
                                cwd=self.root, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['route']['model'], 'gpt-5.6-luna')
        self.assertFalse((project / '.orchestra/executions').exists())

    def test_installed_launcher_can_check_its_bundle_without_checkout(self):
        skill=self.install(self.kit,self.skills)
        command=[sys.executable,str(skill/'scripts/orchestra.py'),'install-skill','--skills-dir',str(self.skills),'--check']
        env=dict(os.environ);env.pop('PYTHONPATH',None)
        result=subprocess.run(command,cwd=self.root,env=env,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        path=skill/'SKILL.md';path.write_text(path.read_text()+'\nmanual drift\n')
        result=subprocess.run(command,cwd=self.root,env=env,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('drift',result.stderr)

    def test_installed_launcher_can_set_up_another_temporary_home(self) -> None:
        skill = self.install(self.kit, self.skills)
        destination = self.root / 'fresh-home' / 'skills'
        codex = self.root / 'fresh-home' / 'codex'
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        command = [sys.executable, str(skill / 'scripts/orchestra.py'), 'setup',
                   '--skills-dir', str(destination), '--codex-home', str(codex)]
        result = subprocess.run(command, cwd=self.root, env=environment,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = destination / 'orchestra'
        self.assertTrue((installed / 'scripts/lib/orchestra_kit/web/index.html').is_file())
        self.assertTrue((installed / 'scripts/templates/roles/worker.md').is_file())
        self.assertTrue((codex / 'AGENTS.md').is_file())
        self.assertTrue((codex / 'hooks.json').is_file())
        checked = subprocess.run(command + ['--check'], cwd=self.root, env=environment,
                                 text=True, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_update_repairs_owned_drift_and_preserves_unmanaged_files(self) -> None:
        skill = self.install(self.kit, self.skills)
        (skill / "SKILL.md").write_text("drift")
        user_file = skill / "notes.txt"
        user_file.write_text("mine")
        self.assertTrue(self.check(self.kit, self.skills))
        self.install(self.kit, self.skills)
        self.assertEqual(user_file.read_text(), "mine")
        self.assertEqual(self.check(self.kit, self.skills), ())

    def test_refuses_unowned_skill_without_mutation(self) -> None:
        from orchestra_kit.project import ProjectError
        target = self.skills / "orchestra"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("user skill")
        with self.assertRaises(ProjectError):
            self.install(self.kit, self.skills)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["SKILL.md"])

    def test_refuses_symlink_escape_before_writing(self) -> None:
        from orchestra_kit.project import ProjectError
        skill = self.install(self.kit, self.skills)
        outside = self.root / "outside"
        outside.mkdir()
        launcher = skill / "scripts/orchestra.py"
        launcher.unlink()
        launcher.symlink_to(outside / "overwritten.py")
        with self.assertRaises(ProjectError):
            self.install(self.kit, self.skills)
        self.assertFalse((outside / "overwritten.py").exists())

    def test_update_does_not_follow_a_temporary_file_symlink(self) -> None:
        skill = self.install(self.kit, self.skills)
        outside = self.root / 'outside.txt'
        outside.write_text('untouched')
        (skill / 'SKILL.md.orchestra-tmp').symlink_to(outside)
        self.install(self.kit, self.skills)
        self.assertEqual(outside.read_text(), 'untouched')

    def test_check_rejects_a_symlinked_skill_even_when_bundle_is_healthy(self) -> None:
        skill = self.install(self.kit, self.skills)
        outside = self.root / 'moved-skill'
        skill.rename(outside)
        skill.symlink_to(outside, target_is_directory=True)
        self.assertTrue(self.check(self.kit, self.skills))

    def test_internal_managed_file_symlink_is_rejected_without_modifying_target(self) -> None:
        from orchestra_kit.project import ProjectError
        skill = self.install(self.kit, self.skills)
        target = skill / 'mine.md'
        target.write_text((skill / 'SKILL.md').read_text())
        before = target.read_text()
        (skill / 'SKILL.md').unlink()
        (skill / 'SKILL.md').symlink_to(target)
        with self.assertRaises(ProjectError):
            self.install(self.kit, self.skills)
        self.assertTrue(self.check(self.kit, self.skills))
        self.assertEqual(target.read_text(), before)

    def test_setup_preflights_malformed_hooks_before_any_installation_writes(self) -> None:
        from orchestra_kit.install import install_user_setup
        from orchestra_kit.project import ProjectError

        codex = self.root / "codex"
        codex.mkdir()
        guidance = codex / "AGENTS.md"
        guidance.write_text("my global guidance\n", encoding="utf-8")
        hooks = codex / "hooks.json"
        hooks.write_text("[]\n", encoding="utf-8")

        with self.assertRaisesRegex(ProjectError, "hook configuration must be an object"):
            install_user_setup(self.kit, self.skills, codex)

        self.assertFalse((self.skills / "orchestra").exists())
        self.assertEqual(guidance.read_text(encoding="utf-8"), "my global guidance\n")
        self.assertEqual(hooks.read_text(encoding="utf-8"), "[]\n")
        self.assertFalse((codex / ".orchestra-hooks-manifest.json").exists())

    def test_setup_preflights_bootstrap_conflict_before_any_installation_writes(self) -> None:
        from orchestra_kit.install import install_user_setup
        from orchestra_kit.project import ProjectError

        codex = self.root / "codex"
        codex.mkdir()
        guidance = codex / "AGENTS.md"
        original = "<!-- orchestra-kit:bootstrap:start -->\nbroken\n"
        guidance.write_text(original, encoding="utf-8")

        with self.assertRaisesRegex(ProjectError, "malformed OrchestraKit bootstrap"):
            install_user_setup(self.kit, self.skills, codex)

        self.assertFalse((self.skills / "orchestra").exists())
        self.assertEqual(guidance.read_text(encoding="utf-8"), original)
        self.assertFalse((codex / "hooks.json").exists())
        self.assertFalse((codex / ".orchestra-hooks-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
