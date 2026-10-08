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
        self.assertIsNone(config.workflow.max_observed_tokens)
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

    def test_provider_urls_and_environment_names_are_safe(self):
        path=self.project / '.orchestra/project.toml'
        for url in ['https://user:secret@example.test/v1', 'https://example.test/v1?api_key=x', 'https://example.test/v1#x', 'http://example.test/v1']:
            path.write_text(VALID_CONFIG.replace('https://api.deepseek.com/',url))
            with self.subTest(url=url), self.assertRaises(ConfigError):
                load_config(self.project,self.kit)
        for key in ['PATH','HOME','CODEX_HOME','PYTHONPATH','DYLD_INSERT_LIBRARIES']:
            path.write_text(VALID_CONFIG.replace('DEEPSEEK_API_KEY',key))
            with self.subTest(key=key), self.assertRaises(ConfigError):
                load_config(self.project,self.kit)

    def test_custom_providers_cannot_override_reserved_codex_ids(self):
        p=self.project/'.orchestra/project.toml'
        for name in ['openai','ollama','lmstudio']:
            p.write_text(VALID_CONFIG.replace('providers.deepseek','providers.'+name).replace('provider = "deepseek"','provider = "'+name+'"'))
            with self.subTest(name=name),self.assertRaisesRegex(ConfigError,'reserved'):
                load_config(self.project,self.kit)

    def test_observed_token_retry_limit_is_optional_and_strict(self) -> None:
        path = self.project / '.orchestra/project.toml'
        path.write_text(VALID_CONFIG.replace('max_repair_cycles = 2',
                                            'max_repair_cycles = 2\nmax_observed_tokens = 200000'))
        self.assertEqual(load_config(self.project, self.kit).workflow.max_observed_tokens, 200000)
        for value in ['0', '-1', 'true', '2.5', '"200000"']:
            path.write_text(VALID_CONFIG.replace('max_repair_cycles = 2',
                'max_repair_cycles = 2\nmax_observed_tokens = ' + value))
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, 'max_observed_tokens'):
                load_config(self.project, self.kit)

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

    def test_routing_and_context_defaults_keep_legacy_projects_usable(self) -> None:
        config = load_config(self.project, self.kit)
        self.assertTrue(config.routing.enabled)
        self.assertEqual(config.routing.profile_order, ("cheap", "strong"))
        self.assertEqual(config.context.max_brief_chars, 12000)
        self.assertEqual(config.context.max_result_chars, 6000)
        self.assertEqual(config.context.max_skill_catalog_tokens, 1000)

    def test_leaf_skill_catalog_budget_is_bounded_and_project_configured(self) -> None:
        path = self.project / '.orchestra/project.toml'
        path.write_text(VALID_CONFIG.replace('[context]', '[context]\nmax_skill_catalog_tokens = 2000'))
        self.assertEqual(load_config(self.project, self.kit).context.max_skill_catalog_tokens, 2000)
        for value in ('true', '0', '10001'):
            with self.subTest(value=value):
                path.write_text(VALID_CONFIG.replace('[context]', '[context]\nmax_skill_catalog_tokens = ' + value))
                with self.assertRaises(ConfigError):
                    load_config(self.project, self.kit)

    def test_explicit_routing_order_and_context_budgets_are_loaded(self) -> None:
        path = self.project / ".orchestra/project.toml"
        path.write_text(VALID_CONFIG.replace(
            '[context]', '[routing]\nenabled = false\nprofile_order = ["strong", "cheap"]\n\n[context]\nmax_brief_chars = 4000\nmax_result_chars = 2000'
        ), encoding="utf-8")
        config = load_config(self.project, self.kit)
        self.assertFalse(config.routing.enabled)
        self.assertEqual(config.routing.profile_order, ("strong", "cheap"))
        self.assertEqual(config.context.max_brief_chars, 4000)

    def test_routing_rejects_unknown_duplicate_and_empty_profiles(self) -> None:
        path = self.project / ".orchestra/project.toml"
        for order in ['["missing"]', '["cheap", "cheap"]', '[]']:
            with self.subTest(order=order):
                path.write_text(VALID_CONFIG + '\n[routing]\nprofile_order = ' + order)
                with self.assertRaises(ConfigError):
                    load_config(self.project, self.kit)

    def test_context_budgets_reject_boolean_and_unbounded_values(self) -> None:
        path = self.project / ".orchestra/project.toml"
        for value in ['true', '0', '1000001']:
            with self.subTest(value=value):
                path.write_text(VALID_CONFIG.replace('[context]', '[context]\nmax_brief_chars = ' + value))
                with self.assertRaises(ConfigError):
                    load_config(self.project, self.kit)


if __name__ == "__main__":
    unittest.main()
