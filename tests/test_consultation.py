from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


KIT = Path(__file__).resolve().parents[1]


class ConsultationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / ".orchestra").mkdir()
        (self.project / ".orchestra" / "project.toml").write_text(
            (KIT / "templates" / "project.toml").read_text(), encoding="utf-8"
        )
        (self.project / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.check_ref = self._evidence()
        self.artifact = self.project / ".orchestra" / "checks" / "attempt.log"
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        self.artifact.write_text("the first hypothesis failed", encoding="utf-8")
        self.module = importlib.import_module("orchestra_kit.consultation")

    def _evidence(self) -> str:
        from orchestra_kit.evidence import run_evidence

        result = run_evidence(self.project, ["module.py"], [{
            "argv": [sys.executable, "-c", "pass"],
            "failure_cause": "implementation",
        }])
        self.assertEqual(result["status"], "verified")
        return result["evidence_ref"]

    def request(self, **updates: object) -> dict:
        value = {
            "question": "Why does the focused regression still fail?",
            "goal": "Identify the next bounded investigation.",
            "constraints": ["Do not edit files", "Do not widen scope"],
            "acceptance": ["Give evidence-backed investigation steps"],
            "files": ["module.py"],
            "evidence_refs": [self.check_ref],
            "rejected_approaches": [{
                "text": "Assume the old value is correct",
                "reason": "The retained attempt contradicts it",
                "artifact_ref": ".orchestra/checks/attempt.log",
            }],
        }
        value.update(updates)
        return value

    def test_custom_provider_without_file_tool_capability_is_not_launched(self) -> None:
        path = self.project / '.orchestra/project.toml'
        config = path.read_text().replace('[profiles.strong]', '[profiles.strong]\nprovider = "no_tools"')
        config += '\n[providers.no_tools]\nname = "No tools"\nbase_url = "https://example.invalid/v1"\nenv_key = "NO_TOOLS_API_KEY"\nwire_api = "responses"\ncapabilities = []\n'
        path.write_text(config)
        with patch.object(self.module, 'run_job') as runner:
            result = self.module.consult(self.project, KIT, self.request(), dry_run=True)
        self.assertEqual(result['status'], 'keep-root')
        runner.assert_not_called()

    def receipt(self, final: str | None = None, **updates: object) -> dict:
        value = {
            "status": "advisory",
            "role": "explorer",
            "profile": "strong",
            "requested_model": "configured-strong-model",
            "requested_effort": "high",
            "route": {
                "role": "explorer", "profile": "strong", "sandbox": "read-only",
                "execution": "leaf", "action": "execute",
            },
            "final": final or json.dumps({
                "hypotheses": ["The focused source differs from the check subject."],
                "next_checks": ["Compare the source snapshot with the failure."],
            }),
            "usage": {"complete": True, "totals": {"total_tokens": 37}},
            "attempts": [{"number": 1}],
        }
        value.update(updates)
        return value

    def test_runs_one_configured_strong_read_only_advisory_and_returns_usage(self) -> None:
        captured: dict = {}

        def fake_run(project, kit, job, **kwargs):
            captured.update({"project": project, "kit": kit, "job": job, "kwargs": kwargs})
            return self.receipt()

        with patch("orchestra_kit.consultation.run_job", side_effect=fake_run):
            result = self.module.consult(self.project, KIT, self.request(), codex_command=("fake-codex",))

        self.assertEqual(result["status"], "advisory")
        self.assertEqual(result["advice"]["hypotheses"], ["The focused source differs from the check subject."])
        self.assertEqual(result["usage"]["totals"]["total_tokens"], 37)
        self.assertEqual(result["receipt"]["status"], "advisory")
        self.assertEqual(captured["job"]["role"], "explorer")
        self.assertEqual(captured["job"]["complexity"], "complex")
        self.assertEqual(captured["job"]["risk"], "high")
        self.assertTrue(captured["job"]["independent"])
        self.assertEqual(captured["job"]["checks"], [])
        self.assertEqual(captured["job"]["evidence_files"], ["module.py"])
        self.assertEqual(captured["job"]["result_contract"], {
            "required": {"hypotheses": "array", "next_checks": "array"}, "aliases": {},
        })
        self.assertTrue(captured["kwargs"]["advisory_only"])
        self.assertEqual(captured["kwargs"]["codex_command"], ("fake-codex",))

    def test_dry_run_builds_the_same_safe_route_without_invoking_a_model(self) -> None:
        with patch("orchestra_kit.consultation.run_job") as run:
            result = self.module.consult(self.project, KIT, self.request(), dry_run=True)

        self.assertEqual(result["status"], "advisory-dry-run")
        self.assertEqual(result["route"]["profile"], "strong")
        run.assert_not_called()

    def test_dry_run_allows_pinned_sources_and_a_retained_failure_without_passing_evidence(self) -> None:
        with patch("orchestra_kit.consultation.run_job") as run:
            result = self.module.consult(
                self.project, KIT, self.request(evidence_refs=[]), dry_run=True,
            )

        self.assertEqual(result["status"], "advisory-dry-run")
        self.assertEqual(result["source_fingerprints"]["before"]["files"]["module.py"]["state"], "regular")
        run.assert_not_called()

    def test_refuses_unknown_or_unbounded_input_and_known_secrets_before_runner(self) -> None:
        with patch("orchestra_kit.consultation.run_job") as run:
            with self.assertRaisesRegex(ValueError, "unsupported"):
                self.module.consult(self.project, KIT, self.request(extra="no"))
            with self.assertRaisesRegex(ValueError, "at most 32"):
                self.module.consult(self.project, KIT, self.request(files=["module.py"] * 33))
            with patch.dict(os.environ, {"TEST_API_KEY": "known-secret"}):
                with self.assertRaisesRegex(ValueError, "known secret"):
                    self.module.consult(self.project, KIT, self.request(question="known-secret"))
        run.assert_not_called()

    def test_refuses_stale_evidence_and_symlinked_sources_before_runner(self) -> None:
        (self.project / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        with patch("orchestra_kit.consultation.run_job") as run:
            with self.assertRaisesRegex(ValueError, "stale evidence"):
                self.module.consult(self.project, KIT, self.request())
        run.assert_not_called()

        (self.project / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        check_ref = self._evidence()
        (self.project / "link.py").symlink_to(self.project / "module.py")
        with patch("orchestra_kit.consultation.run_job") as run:
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.module.consult(self.project, KIT, self.request(files=["link.py"], evidence_refs=[check_ref]))
        run.assert_not_called()

    def test_refuses_a_source_hidden_behind_an_in_project_parent_symlink_before_hashing(self) -> None:
        source_dir = self.project / "real-source"
        source_dir.mkdir()
        (source_dir / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.project / "linked-source").symlink_to(source_dir, target_is_directory=True)
        from orchestra_kit.evidence import run_evidence

        evidence = run_evidence(self.project, ["linked-source/module.py"], [{
            "argv": [sys.executable, "-c", "pass"], "failure_cause": "implementation",
        }])
        self.assertEqual(evidence["status"], "verified")
        with patch("orchestra_kit.consultation.run_job") as run, \
                patch("orchestra_kit.consultation.fingerprint_paths") as fingerprint:
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.module.consult(self.project, KIT, self.request(
                    files=["linked-source/module.py"], evidence_refs=[evidence["evidence_ref"]],
                ))
        run.assert_not_called()
        fingerprint.assert_not_called()

    def test_refuses_extra_advice_fields_changed_context_or_multiple_attempts(self) -> None:
        invalid = self.receipt(final=json.dumps({"hypotheses": ["x"], "next_checks": [], "command": "rm -rf ."}))
        with patch("orchestra_kit.consultation.run_job", return_value=invalid):
            result = self.module.consult(self.project, KIT, self.request())
        self.assertEqual(result["status"], "keep-root")
        self.assertIn("strict advice contract", result["reason"])

        def changed_context(*_args, **_kwargs):
            self.project.joinpath("module.py").write_text("changed", encoding="utf-8")
            return self.receipt()

        with patch("orchestra_kit.consultation.run_job", side_effect=changed_context):
            result = self.module.consult(self.project, KIT, self.request())
        self.assertEqual(result["status"], "keep-root")
        self.assertIn("advice context changed", result["reason"])

        (self.project / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.check_ref = self._evidence()
        with patch("orchestra_kit.consultation.run_job", return_value=self.receipt(attempts=[{"number": 1}, {"number": 2}])):
            result = self.module.consult(self.project, KIT, self.request())
        self.assertEqual(result["status"], "keep-root")
        self.assertIn("single native call", result["reason"])

    def test_reports_an_unavailable_advisory_adapter_without_retrying(self) -> None:
        with patch("orchestra_kit.consultation.run_job", side_effect=TypeError("unsupported adapter")) as run:
            result = self.module.consult(self.project, KIT, self.request())

        self.assertEqual(result["status"], "keep-root")
        self.assertIn("unavailable", result["reason"])
        self.assertIsNone(result["receipt"])
        run.assert_called_once()

    def test_rejects_secrets_in_untrusted_advice_and_allows_only_read_only_retrieval(self) -> None:
        receipt = self.receipt(final=json.dumps({
            "hypotheses": ["known-secret"], "next_checks": ["Inspect the source."],
        }))
        with patch.dict(os.environ, {"TEST_API_KEY": "known-secret"}):
            with patch("orchestra_kit.consultation.run_job", return_value=receipt):
                result = self.module.consult(self.project, KIT, self.request())
        self.assertEqual(result["status"], "keep-root")
        self.assertIn("known secret", result["reason"])

        verification = self.module._brief(self.request())["VERIFICATION"]
        self.assertIn("read-only retrieval", verification.lower())
        self.assertIn("project code", verification)
        self.assertIn("follow-up model calls", verification)
