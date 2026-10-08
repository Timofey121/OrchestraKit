from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestra_kit.install import check_skill, install_skill
from orchestra_kit.project import ProjectError


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.kit = Path(__file__).resolve().parents[1]
        self.skills = self.root / 'skills'
        self.codex = self.root / 'codex'
        self.codex.mkdir()

    def test_bootstrap_preserves_user_guidance_and_loads_skill_before_other_workflows(self) -> None:
        guidance = self.codex / 'AGENTS.md'
        guidance.write_text('# My rules\nPreserve my changes.\n')
        skill = install_skill(self.kit, self.skills, codex_home=self.codex)
        text = guidance.read_text()
        self.assertTrue(text.startswith('# My rules\nPreserve my changes.\n'))
        self.assertIn(str(skill / 'SKILL.md'), text)
        self.assertIn('primary workflow', text)
        self.assertIn('empty folders', ' '.join(text.split()))
        self.assertIn('Leaf agents', text)
        self.assertEqual(check_skill(self.kit, self.skills, codex_home=self.codex), ())
        install_skill(self.kit, self.skills, codex_home=self.codex)
        self.assertEqual(guidance.read_text(), text)

    def test_installer_without_bootstrap_does_not_change_global_guidance(self) -> None:
        guidance = self.codex / 'AGENTS.md'
        guidance.write_text('My rules\n')
        install_skill(self.kit, self.skills)
        self.assertEqual(guidance.read_text(), 'My rules\n')

    def test_bootstrap_delivers_canonical_workflow_without_an_optional_skill_read(self) -> None:
        skill = install_skill(self.kit, self.skills, codex_home=self.codex)
        text = (self.codex / 'AGENTS.md').read_text()
        canonical = (skill / 'SKILL.md').read_text().split('---', 2)[2].strip()
        self.assertIn(canonical, text)
        self.assertIn('already loaded', text)
        self.assertIn(str(skill / 'references'), text)

    def test_changed_canonical_workflow_makes_bootstrap_stale(self) -> None:
        skill = install_skill(self.kit, self.skills, codex_home=self.codex)
        source = skill / 'SKILL.md'
        source.write_text(source.read_text() + '\nNew acceptance rule.\n')
        from orchestra_kit.bootstrap import check_bootstrap
        self.assertTrue(check_bootstrap(self.codex, skill))

    def test_uses_effective_global_override_and_preserves_base_file(self) -> None:
        base = self.codex / 'AGENTS.md'
        base.write_text('base\n')
        override = self.codex / 'AGENTS.override.md'
        override.write_text('override\n')
        install_skill(self.kit, self.skills, codex_home=self.codex)
        self.assertEqual(base.read_text(), 'base\n')
        self.assertIn('primary workflow', override.read_text())
        self.assertEqual(check_skill(self.kit, self.skills, codex_home=self.codex), ())

    def test_empty_override_is_ignored_as_codex_does(self) -> None:
        override = self.codex / 'AGENTS.override.md'
        override.write_text(' \n')
        install_skill(self.kit, self.skills, codex_home=self.codex)
        self.assertEqual(override.read_text(), ' \n')
        self.assertIn('primary workflow', (self.codex / 'AGENTS.md').read_text())

    def test_malformed_markers_prevent_all_installation_writes(self) -> None:
        guidance = self.codex / 'AGENTS.md'
        original = '<!-- orchestra-kit:bootstrap:start -->\nbroken\n'
        guidance.write_text(original)
        with self.assertRaises(ProjectError):
            install_skill(self.kit, self.skills, codex_home=self.codex)
        self.assertFalse((self.skills / 'orchestra').exists())
        self.assertEqual(guidance.read_text(), original)

    def test_bootstrap_refuses_escaping_symlink_before_any_writes(self) -> None:
        outside = self.root / 'outside.md'
        outside.write_text('mine')
        (self.codex / 'AGENTS.md').symlink_to(outside)
        with self.assertRaises(ProjectError):
            install_skill(self.kit, self.skills, codex_home=self.codex)
        self.assertFalse((self.skills / 'orchestra').exists())
        self.assertEqual(outside.read_text(), 'mine')

    def test_internal_global_instruction_symlink_is_preserved_without_installation(self) -> None:
        target = self.codex / 'mine.md'
        target.write_text('user-owned')
        (self.codex / 'AGENTS.md').symlink_to(target)
        with self.assertRaises(ProjectError):
            install_skill(self.kit, self.skills, codex_home=self.codex)
        self.assertFalse((self.skills / 'orchestra').exists())
        self.assertEqual(target.read_text(), 'user-owned')

    def test_check_detects_missing_and_changed_bootstrap_without_mutation(self) -> None:
        install_skill(self.kit, self.skills)
        self.assertTrue(check_skill(self.kit, self.skills, codex_home=self.codex))
        self.assertFalse((self.codex / 'AGENTS.md').exists())
        install_skill(self.kit, self.skills, codex_home=self.codex)
        path = self.codex / 'AGENTS.md'
        path.write_text(path.read_text().replace('primary workflow', 'changed workflow'))
        before = path.read_text()
        self.assertTrue(check_skill(self.kit, self.skills, codex_home=self.codex))
        self.assertEqual(path.read_text(), before)

    def test_oversized_global_instruction_file_does_not_silently_drop_bootstrap(self) -> None:
        guidance = self.codex / 'AGENTS.md'
        guidance.write_text('x' * 32768)
        with self.assertRaisesRegex(ProjectError, 'instruction.*budget'):
            install_skill(self.kit, self.skills, codex_home=self.codex)
        self.assertFalse((self.skills / 'orchestra').exists())


if __name__ == '__main__':
    unittest.main()
