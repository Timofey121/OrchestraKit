from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from orchestra_kit.execution import run_job
from orchestra_kit.providers import ProviderCredentialService


KIT = Path(__file__).resolve().parents[1]
SECRET = "custom-provider-secret"
VALID_CATALOG = {"models": [{
    "slug": "deepseek-chat",
    "display_name": "DeepSeek Chat",
    "supported_reasoning_levels": [{"effort": "medium", "description": "Balanced"}],
    "shell_type": "shell_command",
    "visibility": "list",
    "supported_in_api": True,
    "priority": 1,
    "support_verbosity": False,
    "truncation_policy": {"mode": "tokens", "limit": 10000},
    "experimental_supported_tools": [],
    "base_instructions": "Follow the bounded task brief.",
}]}


class FakeCredentialBackend:
    def __init__(self, secret: str | None) -> None:
        self.secret = secret

    def get(self, account: str) -> str | None:
        return self.secret

    def contains(self, account: str) -> bool:
        return self.secret is not None

    def set(self, account: str, secret: str) -> None:
        self.secret = secret

    def delete(self, account: str) -> None:
        self.secret = None


class ProviderRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.project = root / "project"
        self.project.mkdir()
        (self.project / ".orchestra").mkdir()
        config = (KIT / "templates/project.toml").read_text(encoding="utf-8")
        config = config.replace(
            '[profiles.cheap]\nmodel = "gpt-5.6-luna"\neffort = "medium"',
            '[providers.deepseek]\nname = "DeepSeek"\nbase_url = "https://api.deepseek.example/v1"\n'
            'env_key = "DEEPSEEK_API_KEY"\nwire_api = "responses"\n'
            'supports_standalone_web_search = true\ncapabilities = ["function"]\n\n'
            '[profiles.cheap]\nmodel = "deepseek-chat"\neffort = "medium"\nprovider = "deepseek"',
        )
        (self.project / ".orchestra/project.toml").write_text(config, encoding="utf-8")
        self.capture = root / "capture.json"
        expected_digest = hashlib.sha256(SECRET.encode()).hexdigest()
        self.cli = root / "fake-codex.py"
        self.cli.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import hashlib, json, os, pathlib, sys
                secret = os.environ.get("DEEPSEEK_API_KEY", "")
                pathlib.Path({str(self.capture)!r}).write_text(json.dumps({{
                    "argv": sys.argv,
                    "credential_matches": hashlib.sha256(secret.encode()).hexdigest() == {expected_digest!r},
                    "leaf": os.environ.get("ORCHESTRA_LEAF_EXECUTION"),
                }}))
                print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "finished"}}}}))
                print(json.dumps({{"type": "turn.completed"}}))
                """
            ),
            encoding="utf-8",
        )
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)

    @staticmethod
    def job() -> dict[str, object]:
        return {
            "role": "worker",
            "complexity": "simple",
            "risk": "low",
            "size": "substantial",
            "independent": True,
            "brief": {
                name: name.lower()
                for name in (
                    "GOAL",
                    "SOURCES OF TRUTH",
                    "SCOPE",
                    "ACCEPTANCE CRITERIA",
                    "VERIFICATION",
                    "CONTEXT",
                )
            },
            "checks": [{"argv": ["/usr/bin/true"], "failure_cause": "implementation"}],
        }

    def service(self, secret: str | None) -> ProviderCredentialService:
        return ProviderCredentialService(environment={}, backend=FakeCredentialBackend(secret))

    def test_custom_provider_runs_with_exact_config_and_child_only_secret(self) -> None:
        os.environ.pop("DEEPSEEK_API_KEY", None)

        receipt = run_job(
            self.project,
            KIT,
            self.job(),
            codex_command=(sys.executable, str(self.cli)),
            credential_service=self.service(SECRET),
        )

        self.assertEqual(receipt["status"], "verified")
        captured = json.loads(self.capture.read_text())
        self.assertTrue(captured["credential_matches"])
        self.assertEqual(captured["leaf"], "1")
        overrides = [captured["argv"][index + 1] for index, value in enumerate(captured["argv"]) if value == "-c"]
        self.assertIn('model_provider="deepseek"', overrides)
        self.assertIn('model_providers.deepseek.name="DeepSeek"', overrides)
        self.assertIn('model_providers.deepseek.base_url="https://api.deepseek.example/v1"', overrides)
        self.assertIn('model_providers.deepseek.wire_api="responses"', overrides)
        self.assertIn('model_providers.deepseek.env_key="DEEPSEEK_API_KEY"', overrides)
        self.assertNotIn('model_provider="openai"', overrides)
        self.assertNotIn(SECRET, json.dumps(captured["argv"]))
        self.assertNotIn(SECRET, json.dumps(receipt))
        self.assertNotIn(SECRET, Path(receipt["manifests"][0]).read_text())
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)

    def test_provider_secret_is_redacted_before_stdout_and_stderr_are_retained(self):
        source=self.cli.read_text()
        extra='print(secret, file=sys.stderr)\nprint(json.dumps({"type":"item.completed","item":{"type":"command_execution","aggregated_output":secret}}))\n'
        self.cli.write_text(source.replace('print(json.dumps(',extra+'print(json.dumps(',1))
        receipt=run_job(self.project,KIT,self.job(),codex_command=(sys.executable,str(self.cli)),credential_service=self.service(SECRET))
        self.assertEqual(receipt['status'],'verified',receipt)
        artifacts=list((self.project/'.orchestra/executions').rglob('*.log'))
        self.assertTrue(artifacts)
        for artifact in artifacts:
            self.assertNotIn(SECRET,artifact.read_text())
        self.assertTrue(any('[REDACTED]' in artifact.read_text() for artifact in artifacts))

    def test_inherited_aws_credential_is_redacted_from_retained_logs(self):
        from unittest.mock import patch
        value='inherited-demo-credential'
        source=self.cli.read_text().replace('print(json.dumps(', 'print(os.environ.get("AWS_SECRET_ACCESS_KEY"),file=sys.stderr)\nprint(json.dumps(',1)
        self.cli.write_text(source)
        with patch.dict(os.environ,{'AWS_SECRET_ACCESS_KEY':value}):
            receipt=run_job(self.project,KIT,self.job(),codex_command=(sys.executable,str(self.cli)),credential_service=self.service(SECRET))
        self.assertEqual(receipt['status'],'verified',receipt)
        for path in (self.project/'.orchestra/executions').rglob('*.log'):
            self.assertNotIn(value,path.read_text())

    def test_catalog_path_is_absolute_in_native_child_config(self):
        relative='.orchestra/providers/models.json'
        catalog=self.project/relative;catalog.parent.mkdir(parents=True);catalog.write_text(json.dumps(VALID_CATALOG))
        config=self.project/'.orchestra/project.toml';config.write_text(config.read_text().replace('wire_api = "responses"','wire_api = "responses"\nmodel_catalog_json = "'+relative+'"',1))
        receipt=run_job(self.project,KIT,self.job(),codex_command=(sys.executable,str(self.cli)),credential_service=self.service(SECRET))
        self.assertEqual(receipt['status'],'verified')
        argv=json.loads(self.capture.read_text())['argv']
        self.assertIn('model_catalog_json='+json.dumps(str(catalog.resolve())),argv)

    def test_catalog_mutation_during_leaf_invalidates_verification(self):
        relative='.orchestra/providers/models.json';catalog=self.project/relative;catalog.parent.mkdir(parents=True);catalog.write_text(json.dumps(VALID_CATALOG))
        config=self.project/'.orchestra/project.toml';config.write_text(config.read_text().replace('wire_api = "responses"','wire_api = "responses"\nmodel_catalog_json = "'+relative+'"',1))
        source=self.cli.read_text();self.cli.write_text(source.replace('print(json.dumps(', 'pathlib.Path('+repr(str(catalog))+').write_text("mutated")\nprint(json.dumps(',1))
        receipt=run_job(self.project,KIT,self.job(),codex_command=(sys.executable,str(self.cli)),credential_service=self.service(SECRET))
        self.assertEqual(receipt['status'],'failed')

    def test_missing_custom_provider_credential_fails_generically_before_launch(self) -> None:
        receipt = run_job(
            self.project,
            KIT,
            self.job(),
            codex_command=(sys.executable, str(self.cli)),
            credential_service=self.service(None),
        )

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failure"], "provider credential is unavailable")
        self.assertFalse(self.capture.exists())
        self.assertNotIn("DEEPSEEK_API_KEY", json.dumps(receipt))


if __name__ == "__main__":
    unittest.main()
