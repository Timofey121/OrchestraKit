from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestra_kit.project import ProjectError, doctor_project, init_project, sync_project


CONFIG = """\
version = 1
name = "Demo"

[orchestration]
max_parallel = 2
default_profile = "strong"

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


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.kit = self.root / "kit"
        self.project.mkdir()
        (self.kit / "templates" / "roles").mkdir(parents=True)
        (self.kit / "templates" / "project.toml").write_text(
            CONFIG.replace('name = "Demo"', 'name = "__PROJECT_NAME__"'), encoding="utf-8"
        )
        (self.kit / "templates" / "roles" / "worker.md").write_text(
            "Implement only the assigned leaf.", encoding="utf-8"
        )
        (self.kit / "templates" / "roles" / "reviewer.md").write_text(
            "Review without editing.", encoding="utf-8"
        )
        (self.project / ".orchestra").mkdir()
        (self.project / ".orchestra" / "project.toml").write_text(CONFIG, encoding="utf-8")
        (self.project / ".orchestra" / "providers").mkdir()
        (self.project / ".orchestra" / "providers" / "deepseek-models.json").write_text(
            '{"models": []}\n', encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sync_generates_model_bound_leaf_agent_without_secret(self) -> None:
        sync_project(self.project, self.kit)

        agent = (self.project / ".codex/agents/orchestra-worker.toml").read_text(encoding="utf-8")
        self.assertIn('name = "orchestra_worker"', agent)
        self.assertIn('model = "deepseek-flash"', agent)
        self.assertIn('model_provider = "deepseek"', agent)
        self.assertIn('env_key = "DEEPSEEK_API_KEY"', agent)
        self.assertIn('model_catalog_json = ".orchestra/providers/deepseek-models.json"', agent)
        self.assertNotIn("secret-value", agent)

    def test_sync_preserves_existing_agents_content_and_is_idempotent(self) -> None:
        (self.project / "AGENTS.md").write_text("# Existing instructions\n", encoding="utf-8")

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

        self.assertFalse((self.project / ".codex/agents/orchestra-reviewer.toml").exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "personal")

    def test_doctor_detects_drift_and_missing_provider_key(self) -> None:
        sync_project(self.project, self.kit)
        clean = doctor_project(self.project, self.kit, {"DEEPSEEK_API_KEY": "secret-value"})
        agent = self.project / ".codex/agents/orchestra-worker.toml"
        agent.write_text(agent.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        drifted = doctor_project(self.project, self.kit, {})

        self.assertTrue(clean.ok, clean.errors)
        self.assertFalse(drifted.ok)
        self.assertTrue(any("drift" in error for error in drifted.errors))
        self.assertTrue(any("DEEPSEEK_API_KEY" in error for error in drifted.errors))

    def test_manifest_contains_relative_managed_paths(self) -> None:
        sync_project(self.project, self.kit)

        manifest = json.loads((self.project / ".orchestra/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["generated_by"], "OrchestraKit")
        self.assertIn(".codex/agents/orchestra-worker.toml", manifest["files"])
        self.assertFalse(any(str(self.root) in path for path in manifest["files"]))

    def test_init_creates_config_and_compiles_project(self) -> None:
        fresh = self.root / "fresh"
        fresh.mkdir()

        init_project(fresh, self.kit, "Fresh Project")

        config = (fresh / ".orchestra/project.toml").read_text(encoding="utf-8")
        self.assertIn('name = "Fresh Project"', config)
        self.assertTrue((fresh / ".codex/agents/orchestra-worker.toml").exists())
        self.assertTrue((fresh / ".agents/skills/orchestrate-project/SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
