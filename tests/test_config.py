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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_load_config_resolves_role_profile_and_provider(self) -> None:
        config = load_config(self.project, self.kit)

        self.assertEqual(config.name, "Demo")
        self.assertEqual(config.max_parallel, 3)
        self.assertEqual(config.default_profile, "strong")
        self.assertEqual(config.roles["worker"].profile, "cheap")
        self.assertEqual(config.roles["worker"].prompt, "Implement only the assigned leaf task.")
        self.assertEqual(config.profiles["cheap"].provider, "deepseek")
        self.assertEqual(config.providers["deepseek"].env_key, "DEEPSEEK_API_KEY")
        self.assertEqual(
            config.providers["deepseek"].capabilities,
            ("function", "apply_patch", "web_search"),
        )

    def test_load_config_rejects_unknown_role_profile(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(VALID_CONFIG.replace('profile = "cheap"', 'profile = "missing"'), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "unknown profile 'missing'"):
            load_config(self.project, self.kit)

    def test_load_config_rejects_unknown_profile_provider(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(VALID_CONFIG.replace('provider = "deepseek"', 'provider = "missing"'), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "unknown provider 'missing'"):
            load_config(self.project, self.kit)

    def test_load_config_rejects_prompt_path_traversal(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(VALID_CONFIG.replace('prompt = "worker.md"', 'prompt = "../worker.md"'), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "simple file name"):
            load_config(self.project, self.kit)

    def test_load_config_rejects_unsupported_effort(self) -> None:
        path = self.project / ".orchestra" / "project.toml"
        path.write_text(VALID_CONFIG.replace('effort = "high"', 'effort = "turbo"', 1), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "unsupported effort 'turbo'"):
            load_config(self.project, self.kit)


if __name__ == "__main__":
    unittest.main()
