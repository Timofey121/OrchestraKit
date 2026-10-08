from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from orchestra_kit.project import (
    ProjectError,
    doctor_project,
    init_project,
    sync_project,
)


CONFIG = """\
version = 1
name = "Demo"

[orchestration]
max_parallel = 2
default_profile = "strong"

[workflow]
mode = "adaptive"
max_repair_cycles = 2
fresh_worker_per_leaf = true
fresh_reviewer_always = true
review_levels = ["final"]
checkpoint_policy = "manual"
git_publication = "human-controlled"

[context]
policy_files = ["docs/agent-policy.md"]

[providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com/"
env_key = "DEEPSEEK_API_KEY"
wire_api = "responses"
model_catalog_json = ".orchestra/providers/deepseek-models.json"
supports_standalone_web_search = true
capabilities = ["function", "apply_patch", "web_search"]

[profiles.cheap]
model = "deepseek-flash"
effort = "high"
provider = "deepseek"

[profiles.strong]
model = "gpt-5.6-sol"
effort = "high"

[roles.worker]
description = "Implements one bounded task."
profile = "cheap"
sandbox = "workspace-write"
prompt = "worker.md"

[roles.reviewer]
description = "Reviews one bounded change."
profile = "strong"
sandbox = "read-only"
prompt = "reviewer.md"
"""

VALID_CATALOG = {
    "models": [
        {
            "slug": "deepseek-flash",
            "display_name": "DeepSeek Flash",
            "supported_reasoning_levels": [
                {"effort": "high", "description": "Thorough reasoning"}
            ],
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 1,
            "support_verbosity": False,
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "experimental_supported_tools": [],
            "base_instructions": "Follow the bounded task brief.",
        }
    ]
}


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.kit = self.root / "kit"
        self.project.mkdir()
        (self.kit / "templates" / "roles").mkdir(parents=True)
        (self.kit / "templates" / "project.toml").write_text(
            CONFIG.replace('name = "Demo"', 'name = "__PROJECT_NAME__"').replace(
                'policy_files = ["docs/agent-policy.md"]', "policy_files = []"
            ),
            encoding="utf-8",
        )
        (self.kit / "templates" / "roles" / "worker.md").write_text(
            "Implement only the assigned leaf.", encoding="utf-8"
        )
        (self.kit / "templates" / "roles" / "reviewer.md").write_text(
            "Review without editing.", encoding="utf-8"
        )
        (self.project / ".orchestra").mkdir()
        (self.project / ".orchestra" / "project.toml").write_text(
            CONFIG, encoding="utf-8"
        )
        (self.project / "docs").mkdir()
        (self.project / "docs" / "agent-policy.md").write_text(
            "# Project policy\n", encoding="utf-8"
        )
        (self.project / ".orchestra" / "providers").mkdir()
        (self.project / ".orchestra" / "providers" / "deepseek-models.json").write_text(
            json.dumps(VALID_CATALOG) + "\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sync_refuses_internal_symlink_before_stale_output_removal(self):
        sync_project(self.project,self.kit)
        agents=self.project/'.codex/agents'
        stale=next(agents.glob('*--*.toml'))
        target=agents/'orchestra-worker.toml';before=target.read_bytes()
        stale.unlink();stale.symlink_to(target.name)
        p=self.project/'.orchestra/project.toml';p.write_text(p.read_text()+'\n[routing]\nenabled=false\n')
        with self.assertRaisesRegex(ProjectError,'symlink'):
            sync_project(self.project,self.kit)
        self.assertEqual(target.read_bytes(),before)
        self.assertTrue(stale.is_symlink())

    def test_sync_generates_model_bound_leaf_agent_without_secret(self) -> None:
        sync_project(self.project, self.kit)

        agent = (self.project / ".codex/agents/orchestra-worker.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('name = "orchestra_worker"', agent)
        self.assertIn('model = "deepseek-flash"', agent)
        self.assertIn('model_provider = "deepseek"', agent)
        self.assertIn('env_key = "DEEPSEEK_API_KEY"', agent)
        self.assertIn(
            'model_catalog_json = ".orchestra/providers/deepseek-models.json"', agent
        )
        self.assertNotIn("secret-value", agent)

    def test_sync_preserves_existing_agents_content_and_is_idempotent(self) -> None:
        (self.project / "AGENTS.md").write_text(
            "# Existing instructions\n", encoding="utf-8"
        )

        sync_project(self.project, self.kit)
        first = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        sync_project(self.project, self.kit)
        second = (self.project / "AGENTS.md").read_text(encoding="utf-8")

        self.assertTrue(first.startswith("# Existing instructions\n"))
        self.assertEqual(first, second)
        self.assertEqual(first.count("<!-- orchestra-kit:start -->"), 1)

    def test_sync_refuses_unmarked_agent_file(self) -> None:
        path = self.project / ".codex/agents/orchestra-worker.toml"
        path.parent.mkdir(parents=True)
        path.write_text("user owned", encoding="utf-8")

        with self.assertRaisesRegex(ProjectError, "not managed by OrchestraKit"):
            sync_project(self.project, self.kit)

        self.assertEqual(path.read_text(encoding="utf-8"), "user owned")

    def test_sync_refuses_agents_symlink_that_escapes_project(self) -> None:
        outside = self.root / "outside-agents.md"
        outside.write_text("outside", encoding="utf-8")
        (self.project / "AGENTS.md").symlink_to(outside)

        with self.assertRaisesRegex(ProjectError, "escapes project"):
            sync_project(self.project, self.kit)

        self.assertTrue((self.project / "AGENTS.md").is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_sync_removes_only_stale_managed_agent_files(self) -> None:
        sync_project(self.project, self.kit)
        config_path = self.project / ".orchestra" / "project.toml"
        config_path.write_text(CONFIG.split("[roles.reviewer]")[0], encoding="utf-8")
        unrelated = self.project / ".codex/agents/personal.toml"
        unrelated.write_text("personal", encoding="utf-8")

        sync_project(self.project, self.kit)

        self.assertFalse(
            (self.project / ".codex/agents/orchestra-reviewer.toml").exists()
        )
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "personal")

    def test_doctor_detects_drift_and_missing_provider_key(self) -> None:
        sync_project(self.project, self.kit)
        clean = doctor_project(
            self.project, self.kit, {"DEEPSEEK_API_KEY": "secret-value"}
        )
        agent = self.project / ".codex/agents/orchestra-worker.toml"
        agent.write_text(
            agent.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8"
        )
        drifted = doctor_project(self.project, self.kit, {})

        self.assertTrue(clean.ok, clean.errors)
        self.assertFalse(drifted.ok)
        self.assertTrue(any("drift" in error for error in drifted.errors))
        self.assertTrue(any("DEEPSEEK_API_KEY" in error for error in drifted.errors))

    def test_doctor_validates_configured_provider_model_catalog(self) -> None:
        sync_project(self.project, self.kit)
        catalog = self.project / ".orchestra/providers/deepseek-models.json"

        catalog.write_text("not json", encoding="utf-8")
        malformed = doctor_project(
            self.project, self.kit, {"DEEPSEEK_API_KEY": "configured"}
        )
        self.assertFalse(malformed.ok)
        self.assertTrue(
            any("model catalog" in error and "JSON" in error for error in malformed.errors),
            malformed.errors,
        )

        catalog.write_text('{"models": []}\n', encoding="utf-8")
        invalid_schema = doctor_project(
            self.project, self.kit, {"DEEPSEEK_API_KEY": "configured"}
        )
        self.assertFalse(invalid_schema.ok)
        self.assertTrue(
            any("model catalog" in error and "число моделей" in error for error in invalid_schema.errors),
            invalid_schema.errors,
        )

        wrong_types = json.loads(json.dumps(VALID_CATALOG))
        wrong_types["models"][0]["priority"] = "first"
        catalog.write_text(json.dumps(wrong_types), encoding="utf-8")
        invalid_types = doctor_project(
            self.project, self.kit, {"DEEPSEEK_API_KEY": "configured"}
        )
        self.assertFalse(invalid_types.ok)
        self.assertTrue(any("priority" in error for error in invalid_types.errors))

        catalog.write_text(json.dumps(VALID_CATALOG) + "\n", encoding="utf-8")
        valid = doctor_project(
            self.project, self.kit, {"DEEPSEEK_API_KEY": "configured"}
        )
        self.assertTrue(valid.ok, valid.errors)

    def test_manifest_contains_relative_managed_paths(self) -> None:
        sync_project(self.project, self.kit)

        manifest = json.loads(
            (self.project / ".orchestra/manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["generated_by"], "OrchestraKit")
        self.assertIn(".codex/agents/orchestra-worker.toml", manifest["files"])
        self.assertIn(
            ".agents/skills/orchestrate-project/references/execution-contract.md",
            manifest["files"],
        )
        self.assertFalse(any(str(self.root) in path for path in manifest["files"]))

    def test_sync_generates_project_workflow_contract(self) -> None:
        sync_project(self.project, self.kit)

        reference = (
            self.project
            / ".agents/skills/orchestrate-project/references/execution-contract.md"
        )
        text = reference.read_text(encoding="utf-8")
        self.assertIn("Mode: `adaptive`", text)
        self.assertIn("Maximum repair cycles: `2`", text)
        self.assertIn("docs/agent-policy.md", text)
        self.assertIn("route PROJECT", text)
        self.assertIn("task record PROJECT TASK_ID", text)
        self.assertIn("12000", text)
        skill = (self.project / '.agents/skills/orchestrate-project/SKILL.md').read_text()
        self.assertIn('orchestra_worker__strong', skill)

    def test_init_creates_config_and_compiles_project(self) -> None:
        fresh = self.root / "fresh"
        fresh.mkdir()

        init_project(fresh, self.kit, "Fresh Project")

        config = (fresh / ".orchestra/project.toml").read_text(encoding="utf-8")
        self.assertIn('name = "Fresh Project"', config)
        self.assertTrue((fresh / ".codex/agents/orchestra-worker.toml").exists())
        self.assertTrue(
            (fresh / ".agents/skills/orchestrate-project/SKILL.md").exists()
        )

    def test_init_rejects_symlink_storage_without_writing_outside(self) -> None:
        fresh, outside = self.root / 'fresh', self.root / 'outside'
        fresh.mkdir()
        outside.mkdir()
        (fresh / '.orchestra').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ProjectError):
            init_project(fresh, self.kit)
        self.assertFalse((outside / 'project.toml').exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_init_preserves_foreign_files_and_can_be_retried(self) -> None:
        fresh = self.root / 'fresh'
        foreign = fresh / '.codex/agents/orchestra-worker.toml'
        foreign.parent.mkdir(parents=True)
        foreign.write_text('User-owned instructions')
        with self.assertRaises(ProjectError):
            init_project(fresh, self.kit)
        self.assertFalse((fresh / '.orchestra/project.toml').exists())
        self.assertEqual(foreign.read_text(), 'User-owned instructions')
        self.assertFalse((fresh / 'AGENTS.md').exists())
        foreign.unlink()
        init_project(fresh, self.kit)
        self.assertTrue((fresh / '.orchestra/project.toml').is_file())

    def test_sync_compiles_executable_role_profile_variants(self) -> None:
        sync_project(self.project, self.kit)
        path = self.project / '.codex/agents/orchestra-worker--strong.toml'
        self.assertTrue(path.is_file(), 'routed worker variant must exist')
        data = tomllib.loads(path.read_text())
        self.assertEqual(data['name'], 'orchestra_worker__strong')
        self.assertEqual(data['model'], 'gpt-5.6-sol')
        self.assertEqual(data['model_reasoning_effort'], 'high')
        self.assertEqual(data['sandbox_mode'], 'workspace-write')
        reviewer = tomllib.loads((self.project / '.codex/agents/orchestra-reviewer--cheap.toml').read_text())
        self.assertEqual(reviewer['sandbox_mode'], 'read-only')
        checked = doctor_project(self.project, self.kit, {'DEEPSEEK_API_KEY': 'configured'})
        self.assertTrue(checked.ok, checked.errors)

    def test_routing_variants_are_removed_when_routing_is_disabled(self) -> None:
        sync_project(self.project, self.kit)
        variant = self.project / '.codex/agents/orchestra-worker--strong.toml'
        self.assertTrue(variant.exists())
        (self.project / '.orchestra/project.toml').write_text(CONFIG + '\n[routing]\nenabled = false\n')
        sync_project(self.project, self.kit)
        self.assertFalse(variant.exists())
        self.assertTrue((self.project / '.codex/agents/orchestra-worker.toml').exists())


if __name__ == "__main__":
    unittest.main()
