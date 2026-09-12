from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestra_kit.config import ConfigError, load_config


VALID_CONFIG = """\
version = 1
name = "Demo"

[orchestration]
max_parallel = 3
default_profile = "strong"

[workflow]
mode = "strict"
max_repair_cycles = 2
fresh_worker_per_leaf = true
fresh_reviewer_always = true
review_levels = ["leaf", "final"]
checkpoint_policy = "manual"
git_publication = "human-controlled"

[context]
policy_files = ["docs/agent-policy.md"]

[providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com/"
env_key = "DEEPSEEK_API_KEY"
wire_api = "responses"
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
"""


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.kit = self.root / "kit"
        (self.project / ".orchestra").mkdir(parents=True)
        (self.kit / "templates" / "roles").mkdir(parents=True)
        (self.project / ".orchestra" / "project.toml").write_text(
            VALID_CONFIG, encoding="utf-8"
        )
        (self.kit / "templates" / "roles" / "worker.md").write_text(
            "Implement only the assigned leaf task.", encoding="utf-8"
        )
        (self.project / "docs").mkdir()
        (self.project / "docs" / "agent-policy.md").write_text(
            "# Project policy\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_load_config_resolves_role_profile_and_provider(self) -> None:
        config = load_config(self.project, self.kit)

        self.assertEqual(config.name, "Demo")
        self.assertEqual(config.max_parallel, 3)
        self.assertEqual(config.default_profile, "strong")
        self.assertEqual(config.workflow.mode, "strict")
        self.assertEqual(config.workflow.max_repair_cycles, 2)
        self.assertTrue(config.workflow.fresh_worker_per_leaf)
        self.assertTrue(config.workflow.fresh_reviewer_always)
        self.assertEqual(config.workflow.review_levels, ("leaf", "final"))
        self.assertEqual(config.workflow.checkpoint_policy, "manual")
        self.assertEqual(config.workflow.git_publication, "human-controlled")
        self.assertEqual(config.context.policy_files, ("docs/agent-policy.md",))
        self.assertEqual(config.roles["worker"].profile, "cheap")
        self.assertEqual(
            config.roles["worker"].prompt, "Implement only the assigned leaf task."
        )
        self.assertEqual(config.profiles["cheap"].provider, "deepseek")
        self.assertEqual(config.providers["deepseek"].env_key, "DEEPSEEK_API_KEY")
        self.assertEqual(
            config.providers["deepseek"].capabilities,
            ("function", "apply_patch", "web_search"),
        )

    def test_load_config_rejects_unknown_role_profile(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(
            VALID_CONFIG.replace('profile = "cheap"', 'profile = "missing"'),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "unknown profile 'missing'"):
            load_config(self.project, self.kit)

    def test_load_config_rejects_unknown_profile_provider(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(
            VALID_CONFIG.replace('provider = "deepseek"', 'provider = "missing"'),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "unknown provider 'missing'"):
            load_config(self.project, self.kit)

    def test_load_config_rejects_prompt_path_traversal(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(
            VALID_CONFIG.replace('prompt = "worker.md"', 'prompt = "../worker.md"'),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "simple file name"):
            load_config(self.project, self.kit)

    def test_load_config_rejects_unsupported_effort(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(
            VALID_CONFIG.replace('effort = "high"', 'effort = "turbo"', 1),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "unsupported effort 'turbo'"):
            load_config(self.project, self.kit)

    def test_load_config_applies_workflow_defaults_for_v1_projects(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        workflow_start = VALID_CONFIG.index("[workflow]")
        providers_start = VALID_CONFIG.index("[providers.deepseek]")
        legacy_config = VALID_CONFIG[:workflow_start] + VALID_CONFIG[providers_start:]
        path.write_text(legacy_config, encoding="utf-8")

        config = load_config(self.project, self.kit)

        self.assertEqual(config.workflow.mode, "adaptive")
        self.assertEqual(config.workflow.max_repair_cycles, 2)
        self.assertEqual(config.workflow.review_levels, ("final",))
        self.assertEqual(config.context.policy_files, ())

    def test_load_config_rejects_unknown_workflow_mode(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(
            VALID_CONFIG.replace('mode = "strict"', 'mode = "chaos"'), encoding="utf-8"
        )

        with self.assertRaisesRegex(ConfigError, "workflow.mode"):
            load_config(self.project, self.kit)

    def test_load_config_rejects_repair_cycle_out_of_range(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(
            VALID_CONFIG.replace("max_repair_cycles = 2", "max_repair_cycles = 6"),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "workflow.max_repair_cycles"):
            load_config(self.project, self.kit)

    def test_strict_workflow_requires_leaf_and_final_review(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(
            VALID_CONFIG.replace('["leaf", "final"]', '["final"]'), encoding="utf-8"
        )

        with self.assertRaisesRegex(ConfigError, "strict workflow"):
            load_config(self.project, self.kit)

    def test_load_config_rejects_policy_path_traversal(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(
            VALID_CONFIG.replace('"docs/agent-policy.md"', '"../policy.md"'),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigError, "project-relative"):
            load_config(self.project, self.kit)

    def test_load_config_rejects_missing_policy_file(self) -> None:
        (self.project / "docs" / "agent-policy.md").unlink()

        with self.assertRaisesRegex(ConfigError, "policy file not found"):
            load_config(self.project, self.kit)


if __name__ == "__main__":
    unittest.main()
