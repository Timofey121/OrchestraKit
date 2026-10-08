from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestra_kit.install import install_skill
from orchestra_kit.project import ProjectError


class HookTests(unittest.TestCase):
    def setUp(self):
        from orchestra_kit.hooks import handle_hook, prepare_hooks, check_hooks
        self.handle, self.prepare, self.check = handle_hook, prepare_hooks, check_hooks
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kit = Path(__file__).resolve().parents[1]
        self.skill = install_skill(self.kit, self.root / 'skills')
        self.codex = self.root / 'codex'
        self.codex.mkdir()
        self.digest = hashlib.sha256((self.skill / 'SKILL.md').read_bytes()).hexdigest()

    def event(self, name='UserPromptSubmit'):
        return {'hook_event_name': name, 'session_id': 'session', 'turn_id': 'turn',
                'cwd': str(self.root), 'prompt': 'private user text'}

    def install(self):
        outputs = self.prepare(self.codex, self.skill)
        for path, value in outputs.items():
            path.write_text(value)

    def test_prompt_receives_complete_canonical_body_with_absolute_reference_base(self):
        result = self.handle(self.event(), self.skill, self.digest)
        context = result['hookSpecificOutput']['additionalContext']
        body = (self.skill / 'SKILL.md').read_text().split('---', 2)[2].strip()
        self.assertIn(body, context)
        self.assertIn(str(self.skill / 'references'), context)
        self.assertIn(self.digest, context)
        self.assertNotIn('private user text', json.dumps(result))

    def test_compaction_restores_context_without_waiting_for_another_prompt(self):
        result = self.handle({**self.event('SessionStart'), 'source': 'compact'}, self.skill, self.digest)
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'SessionStart')
        self.assertIn('# Orchestra', result['hookSpecificOutput']['additionalContext'])

    def test_noncompact_session_start_and_unrelated_events_have_no_duplicate_context(self):
        for name in ['Stop', 'PostToolUse', 'SubagentStart', 'SessionEnd']:
            self.assertEqual(self.handle(self.event(name), self.skill, self.digest), {})
        self.assertEqual(self.handle({**self.event('SessionStart'), 'source': 'startup'}, self.skill, self.digest), {})

    def test_leaf_hint_preserves_canonical_context_and_adds_bounded_guard(self):
        result = self.handle(self.event(), self.skill, self.digest, leaf=True)
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('bounded leaf', context)
        self.assertIn('# Orchestra', context)

    def test_invalid_manifest_and_non_utf8_json_preserve_existing_hooks(self):
        self.install()
        path = self.codex / 'hooks.json'
        before = path.read_bytes()
        manifest = self.codex / '.orchestra-hooks-manifest.json'
        for value in [b'\xff', b'{"generated_by":"someone"}', b'{"generated_by":"OrchestraKit","definitions":[]}']:
            manifest.write_bytes(value)
            with self.assertRaises(ProjectError):
                self.prepare(self.codex, self.skill)
            self.assertEqual(path.read_bytes(), before)
        manifest.unlink()
        path.write_bytes(b'\xff')
        with self.assertRaises(ProjectError):
            self.prepare(self.codex, self.skill)

    def test_drift_and_missing_source_block_prompt_without_claiming_activation(self):
        (self.skill / 'SKILL.md').write_text('replaced')
        for missing in [False, True]:
            if missing:
                (self.skill / 'SKILL.md').unlink()
            result = self.handle(self.event(), self.skill, self.digest)
            self.assertEqual(result['decision'], 'block')
            self.assertNotIn('hookSpecificOutput', result)
            self.assertNotIn('private user text', json.dumps(result))

    def test_bad_compaction_source_stops_continuation(self):
        result = self.handle({**self.event('SessionStart'), 'source': 'compact'}, self.skill, '0'*64)
        self.assertIs(result['continue'], False)

    def test_merge_preserves_existing_hooks_and_update_is_idempotent(self):
        prior = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo mine'}]}]},
                 'custom': {'mine': True}}
        (self.codex / 'hooks.json').write_text(json.dumps(prior))
        self.install()
        installed = json.loads((self.codex / 'hooks.json').read_text())
        self.assertEqual(installed['hooks']['Stop'], prior['hooks']['Stop'])
        self.assertEqual(installed['custom'], prior['custom'])
        self.assertEqual(len(installed['hooks']['UserPromptSubmit']), 1)
        self.assertEqual(installed['hooks']['SessionStart'][0]['matcher'], '^compact$')
        self.assertEqual(self.check(self.codex, self.skill), ())
        self.assertEqual(self.prepare(self.codex, self.skill)[self.codex.resolve() / 'hooks.json'],
                         (self.codex / 'hooks.json').read_text())
        self.assertFalse((self.codex / 'hook_trust.json').exists())

    def test_modified_managed_hook_is_not_silently_overwritten(self):
        self.install()
        path = self.codex / 'hooks.json'
        value = json.loads(path.read_text())
        value['hooks']['UserPromptSubmit'][0]['hooks'][0]['command'] = 'echo edited'
        path.write_text(json.dumps(value))
        with self.assertRaises(ProjectError):
            self.prepare(self.codex, self.skill)
        self.assertTrue(self.check(self.codex, self.skill))

    def test_manifest_cannot_take_ownership_of_foreign_hooks_or_unknown_schema(self):
        foreign = {'hooks': [{'type': 'command', 'command': 'echo mine'}]}
        hook_path = self.codex / 'hooks.json'
        hook_path.write_text(json.dumps({'hooks': {'Stop': [foreign]}}))
        before = hook_path.read_bytes()
        manifest = self.codex / '.orchestra-hooks-manifest.json'
        for owned in [{}, {'generated_by': 'OrchestraKit', 'format_version': 999, 'definitions': {}},
                      {'generated_by': 'OrchestraKit', 'format_version': 1, 'definitions': {'Stop': [foreign]}},
                      {'generated_by': 'OrchestraKit', 'format_version': 1, 'definitions': {'UserPromptSubmit': [foreign]}}]:
            manifest.write_text(json.dumps(owned))
            with self.assertRaises(ProjectError):
                self.prepare(self.codex, self.skill)
            self.assertEqual(hook_path.read_bytes(), before)

    def test_direct_api_rejects_internal_skill_source_symlink(self):
        source = self.skill / 'SKILL.md'
        target = self.skill / 'mine.md'
        source.rename(target)
        source.symlink_to(target)
        with self.assertRaises(ProjectError):
            self.prepare(self.codex, self.skill)
        self.assertFalse((self.codex / 'hooks.json').exists())

    def test_malformed_or_symlinked_hooks_fail_before_writes(self):
        path = self.codex / 'hooks.json'
        for value in ['broken', '[]', '{"hooks": []}', '{"hooks":{"Stop":{}}}']:
            path.write_text(value)
            with self.assertRaises((ProjectError, ValueError)):
                self.prepare(self.codex, self.skill)
        path.unlink()
        outside = self.root / 'outside'
        outside.write_text('{}')
        path.symlink_to(outside)
        with self.assertRaises(ProjectError):
            self.prepare(self.codex, self.skill)
        self.assertEqual(outside.read_text(), '{}')

    def test_portable_hook_handler_runs_from_stdin_without_checkout(self):
        launcher = self.skill / 'scripts/orchestra.py'
        result = subprocess.run([sys.executable, str(launcher), 'hook', '--skill-path', str(self.skill),
                                 '--expected-sha256', self.digest], input=json.dumps(self.event()),
                                text=True, capture_output=True, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        result = subprocess.run([sys.executable, str(launcher), 'hook', '--skill-path', str(self.skill),
                                 '--expected-sha256', self.digest], input='{}',
                                text=True, capture_output=True, cwd=self.root)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
