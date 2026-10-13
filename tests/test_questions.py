from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]


class QuestionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / ".orchestra").mkdir()
        (self.project / ".orchestra" / "project.toml").write_text(
            (KIT / "templates" / "project.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (self.project / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        from orchestra_kit.state import start_task

        self.task = start_task(self.project, KIT, "Preserve the original task goal")

    def request(self, **updates: object) -> dict:
        value = {
            "task_id": self.task["task_id"],
            "revision": self.task["revision"],
            "leaf_id": "126cc43c-e2a1-4c2d-9fb8-1f1dfb90ed9f",
            "question": "Which local source should the root ask an explorer to inspect?",
            "files": ["module.py"],
            "environment": {"runtime": "fixture"},
        }
        value.update(updates)
        return value

    def answer(self, **updates: object) -> dict:
        value = {
            "answer": "Inspect the pinned module before deciding on a repair.",
            "evidence_refs": [],
            "environment": {"runtime": "fixture"},
        }
        value.update(updates)
        return value

    def ask(self) -> dict:
        from orchestra_kit.questions import ask_question

        return ask_question(self.project, KIT, self.request())

    def test_accepts_a_pinned_request_then_root_answer_and_advisory_read(self) -> None:
        from orchestra_kit.questions import answer_question, read_question

        request = self.ask()
        self.assertEqual(request["status"], "pending")
        self.assertEqual(request["dispatch"], "root-mediation-required")
        self.assertEqual(request["authority"], "local-caller-asserted-advisory")
        answered = answer_question(self.project, KIT, request["question_id"], self.answer())
        self.assertEqual(answered["status"], "answered")
        result = read_question(self.project, KIT, request["question_id"], environment={"runtime": "fixture"})
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], self.answer()["answer"])
        self.assertEqual(result["task_goal"], "Preserve the original task goal")
        self.assertEqual(result["advisory"], "root-mediated-local-advisory")

    def test_changed_source_environment_or_revision_stales_and_never_forwards_answer(self) -> None:
        from orchestra_kit.questions import answer_question, read_question

        request = self.ask()
        answer_question(self.project, KIT, request["question_id"], self.answer())
        (self.project / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        stale = read_question(self.project, KIT, request["question_id"], environment={"runtime": "fixture"})
        self.assertEqual(stale["status"], "stale")
        self.assertNotIn("answer", stale)

        request = self.ask()
        answer_question(self.project, KIT, request["question_id"], self.answer())
        stale = read_question(self.project, KIT, request["question_id"], environment={"runtime": "different"})
        self.assertEqual(stale["status"], "stale")
        self.assertNotIn("answer", stale)

        request = self.ask()
        answer_question(self.project, KIT, request["question_id"], self.answer())
        config = self.project / ".orchestra" / "project.toml"
        config.write_text(config.read_text(encoding="utf-8") + "\n# changed after question admission\n", encoding="utf-8")
        stale = read_question(self.project, KIT, request["question_id"], environment={"runtime": "fixture"})
        self.assertEqual(stale["status"], "stale")
        self.assertNotIn("answer", stale)
        config.write_text(config.read_text(encoding="utf-8").replace("\n# changed after question admission\n", "\n"), encoding="utf-8")

        request = self.ask()
        from orchestra_kit.state import record_task
        record_task(self.project, KIT, self.task["task_id"], {
            "status": "running", "summary": "Still investigating", "changed_files": [],
        }, expected_revision=self.task["revision"])
        with self.assertRaisesRegex(ValueError, "revision"):
            answer_question(self.project, KIT, request["question_id"], self.answer())

    def test_answer_is_single_assignment_with_identical_retry_only(self) -> None:
        from orchestra_kit.questions import answer_question

        request = self.ask()
        first = answer_question(self.project, KIT, request["question_id"], self.answer())
        self.assertEqual(first["status"], "answered")
        self.assertEqual(answer_question(self.project, KIT, request["question_id"], self.answer()), first)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            answer_question(self.project, KIT, request["question_id"], self.answer(answer="Different advice."))

    def test_supplied_evidence_must_cover_pinned_files_and_stays_fresh_on_read(self) -> None:
        from orchestra_kit.evidence import run_evidence
        from orchestra_kit.questions import answer_question, read_question

        evidence = run_evidence(self.project, ["module.py"], [{
            "argv": [sys.executable, "-c", "print('ok')"], "failure_cause": "implementation",
        }])
        request = self.ask()
        answer_question(self.project, KIT, request["question_id"], self.answer(evidence_refs=[evidence["evidence_ref"]]))
        self.assertEqual(
            read_question(self.project, KIT, request["question_id"], environment={"runtime": "fixture"})["status"],
            "answered",
        )
        certificate = json.loads((self.project / evidence["evidence_ref"]).read_text(encoding="utf-8"))
        (self.project / certificate["checks"][0]["log"]).write_text("forged", encoding="utf-8")
        stale = read_question(self.project, KIT, request["question_id"], environment={"runtime": "fixture"})
        self.assertEqual(stale["status"], "stale")
        self.assertNotIn("answer", stale)

    def test_capacity_secrets_symlinks_and_corrupt_records_are_refused(self) -> None:
        from orchestra_kit.questions import ask_question, read_question

        with self.assertRaisesRegex(ValueError, "known secret"):
            previous = os.environ.get("TEST_API_KEY")
            os.environ["TEST_API_KEY"] = "question-secret"
            try:
                ask_question(self.project, KIT, self.request(question="question-secret"))
            finally:
                if previous is None:
                    os.environ.pop("TEST_API_KEY", None)
                else:
                    os.environ["TEST_API_KEY"] = previous

        outside = self.project.parent / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        (self.project / "linked.py").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            ask_question(self.project, KIT, self.request(files=["linked.py"]))

        linked_directory = self.project / "linked"
        linked_directory.symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            ask_question(self.project, KIT, self.request(files=["linked/module.py"]))

        request = self.ask()
        record = self.project / ".orchestra" / "questions" / f"{request['question_id']}.json"
        record.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            read_question(self.project, KIT, request["question_id"], environment={"runtime": "fixture"})

        questions = self.project / ".orchestra" / "questions"
        for index in range(99):
            (questions / f"f{index}.json").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "capacity"):
            ask_question(self.project, KIT, self.request())

        config = self.project / ".orchestra" / "project.toml"
        safe_config = self.project.parent / "safe-project.toml"
        safe_config.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
        config.unlink()
        config.symlink_to(safe_config)
        with self.assertRaisesRegex(ValueError, "configuration.*symlink"):
            ask_question(self.project, KIT, self.request())

    def test_shared_admission_lock_serializes_capacity_before_a_record_is_written(self) -> None:
        from orchestra_kit.questions import ask_question

        directory = self.project / ".orchestra" / "questions"
        directory.mkdir(mode=0o700)
        lock = directory / "admission.lock"
        lock.write_text("another admission is counting capacity", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "admission.*locked"):
            ask_question(self.project, KIT, self.request())
        self.assertEqual(list(directory.glob("*.json")), [])

    def test_oversized_task_goal_is_rejected_before_question_storage(self) -> None:
        from orchestra_kit.questions import ask_question
        from orchestra_kit.state import start_task

        task = start_task(self.project, KIT, "g" * 2_100)
        with self.assertRaisesRegex(ValueError, "task_goal"):
            ask_question(self.project, KIT, self.request(task_id=task["task_id"], revision=task["revision"]))
        self.assertFalse((self.project / ".orchestra" / "questions").exists())

    def test_completed_or_unknown_cross_project_tasks_cannot_resolve(self) -> None:
        from orchestra_kit.questions import ask_question, read_question
        from orchestra_kit.state import record_task

        request = self.ask()

        record_task(self.project, KIT, self.task["task_id"], {
            "status": "completed", "summary": "Done", "changed_files": [],
            "checks": [{"command": "fixture", "outcome": "pass", "evidence": "fixture"}],
            "acceptance": [{"criterion": "done", "satisfied": True, "evidence": "fixture"}],
            "reviews": [{"level": "final", "verdict": "PASS", "evidence": "fixture"}],
        }, expected_revision=0)
        with self.assertRaisesRegex(ValueError, "completed"):
            ask_question(self.project, KIT, self.request())

        other = Path(self.temp.name) / "other"
        other.mkdir()
        (other / ".orchestra").mkdir()
        (other / ".orchestra" / "project.toml").write_text(
            (KIT / "templates" / "project.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        copied = other / ".orchestra" / "questions"
        copied.mkdir()
        original = self.project / ".orchestra" / "questions"
        # A syntactically copied record has no corresponding task in this project.
        target = copied / f"{request['question_id']}.json"
        target.write_bytes((original / target.name).read_bytes())
        self.assertEqual(read_question(other, KIT, target.stem, environment={"runtime": "fixture"})["status"], "stale")
