from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


KIT = Path(__file__).resolve().parents[1]


class SolutionMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        (self.project / ".orchestra").mkdir(parents=True)
        (self.project / ".orchestra/project.toml").write_text(
            (KIT / "templates/project.toml").read_text()
        )
        (self.project / "src").mkdir()
        (self.project / "src/app.py").write_text("value = 1\n")

    def data(self, **overrides):
        value = {
            "task_id": self.task["task_id"],
            "symptom": "app rejects a duplicate request",
            "scope": "request validation",
            "environment": {"python": "3.11", "mode": "test"},
            "cause": "duplicate identifiers were not normalized",
            "fix": "normalize identifiers before validation",
            "files": ["src/app.py"],
        }
        value.update(overrides)
        return value

    def complete_task(self, *, check_owner=None, review_owner=None) -> None:
        from orchestra_kit.evidence import create_evidence, run_evidence
        from orchestra_kit.state import record_task, start_task

        self.task = start_task(self.project, KIT, "Fix duplicate requests")
        checked = run_evidence(
            self.project,
            ["src/app.py"],
            [{"argv": [sys.executable, "-c", "pass"], "failure_cause": "implementation"}],
        )
        report = self.project / "final-review.txt"
        report.write_text("VERDICT: PASS\nIndependent final review\n")
        check_ref = create_evidence(
            self.project, ["src/app.py"], checked["checks"], owner=check_owner or self.task["task_id"]
        )
        review_ref = create_evidence(
            self.project, ["src/app.py"], checked["checks"], review_verdict="PASS",
            owner=review_owner or self.task["task_id"], review_report=report,
        )
        result = {
            "status": "completed", "summary": "Fixed duplicate requests",
            "completed_steps": ["normalized input"], "next_steps": [],
            "changed_files": ["src/app.py"],
            "checks": [{"command": "test", "outcome": "pass", "evidence": "pass", "evidence_ref": check_ref}],
            "acceptance": [{"criterion": "duplicates are rejected", "satisfied": True, "evidence": "test"}],
            "reviews": [{"level": "final", "verdict": "PASS", "evidence": "review", "evidence_ref": review_ref}],
            "metrics": {},
        }
        record_task(self.project, KIT, self.task["task_id"], result, expected_revision=0)

    def test_stores_only_completed_task_with_owned_fresh_evidence_and_dedupes(self) -> None:
        from orchestra_kit.memory import store_solution

        self.complete_task()
        first = store_solution(self.project, KIT, self.data())
        second = store_solution(self.project, KIT, self.data())
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["deduplicated"])
        path = self.project / ".orchestra/memory" / f"{first['id']}.json"
        saved = json.loads(path.read_text())
        self.assertEqual(saved["task_id"], self.task["task_id"])
        self.assertTrue(saved["advisory"])
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_reaccepted_solution_refreshes_stale_proofs_with_the_same_id(self) -> None:
        from orchestra_kit.memory import search_solutions, store_solution

        self.complete_task()
        first = store_solution(self.project, KIT, self.data())
        old = json.loads((self.project / ".orchestra/memory" / f"{first['id']}.json").read_text())
        certificate = json.loads((self.project / old["check_evidence_refs"][0]).read_text())
        Path(certificate["checks"][0]["log"]).write_text("changed artifact")
        self.complete_task()
        refreshed = store_solution(self.project, KIT, self.data())
        self.assertEqual(refreshed["id"], first["id"])
        self.assertFalse(refreshed["deduplicated"])
        saved = json.loads((self.project / ".orchestra/memory" / f"{first['id']}.json").read_text())
        self.assertEqual(saved["task_id"], self.task["task_id"])
        found = search_solutions(self.project, "duplicate", environment={"python": "3.11", "mode": "test"})
        self.assertEqual(len(found["solutions"]), 1)

    def test_capacity_allows_refresh_but_refuses_a_distinct_solution(self) -> None:
        from orchestra_kit.memory import store_solution

        self.complete_task()
        first = store_solution(self.project, KIT, self.data())
        old = json.loads((self.project / ".orchestra/memory" / f"{first['id']}.json").read_text())
        certificate = json.loads((self.project / old["check_evidence_refs"][0]).read_text())
        Path(certificate["checks"][0]["log"]).write_text("changed artifact")
        self.complete_task()
        with patch("orchestra_kit.memory.MAX_RECORDS", 1):
            refreshed = store_solution(self.project, KIT, self.data())
            self.assertFalse(refreshed["deduplicated"])
            with self.assertRaisesRegex(ValueError, "maximum"):
                store_solution(self.project, KIT, self.data(symptom="a distinct duplicate symptom"))

    def test_refuses_self_report_cross_task_owner_stale_and_uncovered_subjects(self) -> None:
        from orchestra_kit.memory import store_solution
        from orchestra_kit.state import record_task, start_task

        fake = start_task(self.project, KIT, "Claim success without evidence")
        self.task = fake
        fake_result = {
            "status": "completed", "summary": "claimed", "completed_steps": ["claimed"], "next_steps": [],
            "changed_files": ["src/app.py"],
            "checks": [{"command": "claimed", "outcome": "pass", "evidence": "claimed"}],
            "acceptance": [{"criterion": "claimed", "satisfied": True, "evidence": "claimed"}],
            "reviews": [{"level": "final", "verdict": "PASS", "evidence": "claimed"}], "metrics": {},
        }
        record_task(self.project, KIT, fake["task_id"], fake_result, expected_revision=0)
        fake_data = self.data(task_id=fake["task_id"])
        with self.assertRaisesRegex(ValueError, "stale"):
            store_solution(self.project, KIT, fake_data)

        self.complete_task(check_owner="not-the-task")
        with self.assertRaisesRegex(ValueError, "owner"):
            store_solution(self.project, KIT, self.data())

        self.complete_task()
        (self.project / "other.py").write_text("other = 1\n")
        with self.assertRaisesRegex(ValueError, "changed_files"):
            store_solution(self.project, KIT, self.data(files=["other.py"]))
        (self.project / "src/app.py").write_text("value = 2\n")
        with self.assertRaisesRegex(ValueError, "stale"):
            store_solution(self.project, KIT, self.data())

    def test_search_is_bounded_literal_and_marks_source_or_environment_stale(self) -> None:
        from orchestra_kit.memory import search_solutions, store_solution

        self.complete_task()
        store_solution(self.project, KIT, self.data())
        found = search_solutions(
            self.project, "duplicate identifiers", environment={"python": "3.11", "mode": "test"}
        )
        self.assertEqual(len(found["solutions"]), 1)
        self.assertTrue(found["solutions"][0]["advisory"])
        self.assertTrue(found["solutions"][0]["check_evidence_refs"])
        self.assertTrue(found["solutions"][0]["review_evidence_refs"])
        self.assertEqual(search_solutions(self.project, "unrelated", environment={"python": "3.11", "mode": "test"})["solutions"], [])
        from orchestra_kit.evidence import create_evidence
        record_path = self.project / ".orchestra/memory" / f"{found['solutions'][0]['id']}.json"
        record = json.loads(record_path.read_text())
        checked = json.loads((self.project / record["check_evidence_refs"][0]).read_text())
        extra = create_evidence(self.project, ["src/app.py"], checked["checks"], owner=self.task["task_id"])
        record["check_evidence_refs"] = [extra]
        record_path.write_text(json.dumps(record))
        unbound = search_solutions(self.project, "duplicate", environment={"python": "3.11", "mode": "test"})
        self.assertEqual(unbound["stale_count"], 1)
        record["check_evidence_refs"] = found["solutions"][0]["check_evidence_refs"]
        record_path.write_text(json.dumps(record))
        mismatch = search_solutions(self.project, "duplicate", environment={"python": "3.12", "mode": "test"})
        self.assertEqual(mismatch["stale_count"], 1)
        (self.project / "src/app.py").write_text("value = 2\n")
        stale = search_solutions(self.project, "duplicate", environment={"python": "3.11", "mode": "test"})
        self.assertEqual(stale["stale_count"], 1)

    def test_rejects_secrets_and_unsafe_memory_paths(self) -> None:
        from orchestra_kit.memory import search_solutions, store_solution

        self.complete_task()
        with patch.dict(os.environ, {"TEST_API_KEY": "known-secret"}):
            with self.assertRaisesRegex(ValueError, "secret"):
                store_solution(self.project, KIT, self.data(fix="use known-secret safely"))
        with patch.dict(os.environ, {"TEST_API_KEY": 'line\n"quoted"\\value'}):
            with self.assertRaisesRegex(ValueError, "secret"):
                store_solution(self.project, KIT, self.data(fix='line\\n\\"quoted\\"\\\\value'))
        outside_source = Path(self.temp.name) / "outside-source.py"
        outside_source.write_text("outside = 1\n")
        (self.project / "src/linked.py").symlink_to(outside_source)
        with self.assertRaisesRegex(ValueError, "symlink"):
            store_solution(self.project, KIT, self.data(files=["src/linked.py"]))
        (self.project / "src/actual").mkdir()
        (self.project / "src/actual/inside.py").write_text("inside = 1\n")
        (self.project / "src/alias").symlink_to("actual", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            store_solution(self.project, KIT, self.data(files=["src/alias/inside.py"]))
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        memory = self.project / ".orchestra/memory"
        memory.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            search_solutions(self.project, "duplicate", environment={"python": "3.11", "mode": "test"})
