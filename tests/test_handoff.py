from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        handoff = importlib.import_module("orchestra_kit.handoff")
        self.build = handoff.build_handoff
        self.validate = handoff.validate_handoff_evidence

    def data(self, **updates: object) -> dict:
        value = {
            "task_id": str(uuid4()), "revision": 2, "goal": "Add bounded handoffs",
            "constraints": ["Do not change configuration", "Use stdlib only"],
            "acceptance": ["All focused tests pass"], "unresolved_failures": [],
            "next_step": "Root reviews the patch", "evidence_refs": ["tests/test_handoff.py"],
            "raw_history_refs": [".orchestra/runs/example.json"],
        }
        value.update(updates)
        return value

    def test_builds_explicit_local_unattested_handoff_without_losing_references(self) -> None:
        data = self.data()
        result = self.build(data)
        self.assertEqual(result["constraints"], data["constraints"])
        self.assertEqual(result["evidence_refs"], data["evidence_refs"])
        self.assertEqual(result["raw_history_refs"], data["raw_history_refs"])
        self.assertEqual(result["identity_attestation"], "local-input-unattested")

    def test_rejects_missing_semantic_constraint_and_noncanonical_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "constraints"):
            self.build(self.data(constraints=[]))
        with self.assertRaisesRegex(ValueError, "canonical UUID"):
            self.build(self.data(task_id=str(uuid4()).upper()))
        with self.assertRaisesRegex(ValueError, "revision"):
            self.build(self.data(revision=True))

    def test_rejects_unknown_fields_and_over_budget_input_without_clipping(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.build({**self.data(), "server_attestation": "PASS"})
        data = self.data(goal="x" * 100)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.build(data, max_chars=50)
        self.assertEqual(len(data["goal"]), 100)
        self.assertGreater(len(json.dumps(data)), 50)

    def test_accepts_bounded_structured_continuation_records_without_changing_legacy_shape(self) -> None:
        legacy = self.build(self.data())
        self.assertNotIn("decisions", legacy)
        result = self.build(self.data(
            decisions=[{"text": "Use the existing evidence verifier", "reason": "It binds checks to source"}],
            verified_facts=[{"text": "Focused tests passed", "reason": "The generated check certificate records success", "evidence_ref": ".orchestra/evidence/check.json"}],
            rejected_approaches=[{"text": "Infer success from a handoff", "reason": "Self-reports are not verification", "artifact_ref": ".orchestra/checks/attempt.log"}],
        ))
        self.assertEqual(result["decisions"][0]["text"], "Use the existing evidence verifier")
        self.assertEqual(result["verified_facts"][0]["evidence_ref"], ".orchestra/evidence/check.json")
        self.assertEqual(result["rejected_approaches"][0]["artifact_ref"], ".orchestra/checks/attempt.log")

    def test_refuses_incomplete_or_unbounded_continuation_records(self) -> None:
        with self.assertRaisesRegex(ValueError, "verified_facts"):
            self.build(self.data(verified_facts=[{"text": "Passed", "reason": "Trust me"}]))
        with self.assertRaisesRegex(ValueError, "rejected_approaches"):
            self.build(self.data(rejected_approaches=[{"text": "Retry", "reason": "No artifact"}]))
        with self.assertRaisesRegex(ValueError, "decisions"):
            self.build(self.data(decisions=[{"text": "Only text"}]))
        with self.assertRaisesRegex(ValueError, "bounded"):
            self.build(self.data(decisions=[{"text": "x" * 513, "reason": "bounded"}]))

    def test_refuses_known_secret_values_in_base_fields_records_and_references(self) -> None:
        with patch.dict(os.environ, {"TEST_API_KEY": "known-secret"}):
            secret = "known-secret"
            cases = [
                {"goal": f"contains {secret}"},
                {"constraints": [f"contains {secret}"]},
                {"acceptance": [f"contains {secret}"]},
                {"unresolved_failures": [f"contains {secret}"]},
                {"next_step": f"contains {secret}"},
                {"evidence_refs": [f"tests/{secret}.log"]},
                {"raw_history_refs": [f".orchestra/runs/{secret}.json"]},
                {"decisions": [{"text": secret, "reason": "safe reason"}]},
                {"decisions": [{"text": "safe text", "reason": secret}]},
                {"verified_facts": [{"text": "safe text", "reason": "safe reason", "evidence_ref": f".orchestra/evidence/{secret}.json"}]},
                {"rejected_approaches": [{"text": "safe text", "reason": "safe reason", "artifact_ref": f".orchestra/checks/{secret}.log"}]},
            ]
            for updates in cases:
                with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "known secret"):
                    self.build(self.data(**updates))

        multiline = 'line\n"quoted"\\value'
        with patch.dict(os.environ, {"TEST_API_KEY": multiline}):
            with self.assertRaisesRegex(ValueError, "known secret"):
                self.build(self.data(goal='line\\n\\"quoted\\"\\\\value'))
            with self.assertRaisesRegex(ValueError, "known secret"):
                self.build(self.data(constraints=["line"]))

    def test_refuses_rejection_artifacts_outside_retained_runtime_areas(self) -> None:
        with self.assertRaisesRegex(ValueError, "retained Orchestra"):
            self.build(self.data(rejected_approaches=[{
                "text": "Read a report", "reason": "It was absent", "artifact_ref": "tests/attempt.log",
            }]))

    def test_validation_requires_fresh_source_bound_evidence_and_safe_attempt_artifact(self) -> None:
        from orchestra_kit.evidence import run_evidence

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "app.py").write_text("working")
            checked = run_evidence(project, ["app.py"], [{
                "argv": [sys.executable, "-c", "pass"],
                "failure_cause": "implementation",
            }])
            attempt = project / ".orchestra/checks/attempt.log"
            attempt.parent.mkdir(parents=True, exist_ok=True)
            attempt.write_text("observed failing attempt")
            result = self.build(self.data(
                verified_facts=[{"text": "app is checked", "reason": "A source-bound check passed", "evidence_ref": checked["evidence_ref"]}],
                rejected_approaches=[{"text": "Assume a result", "reason": "The attempt failed", "artifact_ref": ".orchestra/checks/attempt.log"}],
            ))
            validation = self.validate(project, result)
            self.assertEqual(validation["verified_facts"][0]["status"], "fresh-source-bound-passing-check")
            self.assertEqual(validation["rejected_approaches"][0]["status"], "observed-attempt-artifact-only")
            (project / "app.py").write_text("changed")
            with self.assertRaisesRegex(ValueError, "stale"):
                self.validate(project, result)

    def test_validation_refuses_missing_or_symlinked_rejection_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            result = self.build(self.data(rejected_approaches=[{
                "text": "Read a report", "reason": "It was absent", "artifact_ref": ".orchestra/checks/attempt.log",
            }]))
            with self.assertRaisesRegex(ValueError, "artifact"):
                self.validate(project, result)
            outside = project.parent / "handoff-outside.log"
            outside.write_text("outside")
            artifact = project / ".orchestra/checks/attempt.log"
            artifact.parent.mkdir(parents=True)
            artifact.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.validate(project, result)
