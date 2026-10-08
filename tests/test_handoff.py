from __future__ import annotations

import importlib
import json
import unittest
from uuid import uuid4


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.build = importlib.import_module("orchestra_kit.handoff").build_handoff

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
