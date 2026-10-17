"""Contract checks for the built-in stage-one evaluation fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from orchestra_kit.evaluation_cases import CASES


class EvaluationCasesTests(unittest.TestCase):
    def test_json_grader_rejects_nonblank_title_encoding_failure(self):
        case=CASES['json-task-store']
        reference=case['solution']['task_store.py']
        mutant=reference.replace('json.dump(records, handle, sort_keys=True)',
                                 'json.dump(records, handle, sort_keys=True, ensure_ascii=False)')
        self.assertNotEqual(reference,mutant)
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            (workspace/'task_store.py').write_text(mutant,encoding='utf-8')
            grader=workspace/'grader.py';grader.write_text(case['tests'],encoding='utf-8')
            result=self._run_grader(grader,workspace)
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)

    def test_csv_grader_rejects_signed_zero_rejection(self):
        case=CASES['expense-csv-decimals']
        reference=case['solution']['expense_loader.py']
        mutant=reference.replace('amount < 0', 'amount.is_signed()')
        self.assertNotEqual(reference,mutant)
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            self._write_files(workspace,case['files'])
            (workspace/'expense_loader.py').write_text(mutant,encoding='utf-8')
            grader=workspace/'grader.py';grader.write_text(case['tests'],encoding='utf-8')
            result=self._run_grader(grader,workspace)
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)

    def test_atomic_grader_rejects_temp_leaks_and_failed_write_overwrites(self) -> None:
        case = CASES["json-task-store"]
        reference = case["solution"]["task_store.py"]
        cleanup_mutant = reference.replace(
            """        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
""",
            """        finally:
            pass
""",
        )
        overwrite_mutant = reference.replace(
            """        except OSError as error:
            raise TaskStoreError("cannot write task store") from error
""",
            """        except OSError as error:
            self.path.write_text(json.dumps(records), encoding="utf-8")
            raise TaskStoreError("cannot write task store") from error
""",
        )

        for name, source in {
            "temporary-file-leak": cleanup_mutant,
            "overwrite-after-replace-failure": overwrite_mutant,
        }.items():
            with self.subTest(mutant=name), tempfile.TemporaryDirectory() as tmp:
                self.assertNotEqual(reference, source)
                workspace = Path(tmp)
                (workspace / "task_store.py").write_text(source, encoding="utf-8")
                grader = workspace / "grader.py"
                grader.write_text(case["tests"], encoding="utf-8")
                result = self._run_grader(grader, workspace)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ttl_grader_rejects_immediate_expiry_evicting_a_live_value(self):
        case = CASES['ttl-cache-bounds']
        reference = case['solution']['ttl_cache.py']
        mutant = reference.replace('        self.items[key] = (value, self.clock() + ttl)\n        self._purge_expired()\n',
                                   '        self.items[key] = (value, self.clock() + ttl)\n')
        self.assertNotEqual(reference, mutant)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace/'ttl_cache.py').write_text(mutant, encoding='utf-8')
            grader = workspace/'grader.py'
            grader.write_text(case['tests'], encoding='utf-8')
            result = self._run_grader(grader, workspace)
            self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_ttl_reference_does_not_evict_live_value_for_zero_ttl(self) -> None:
        namespace = {}
        exec(CASES["ttl-cache-bounds"]["solution"]["ttl_cache.py"], namespace)
        cache = namespace["TTLCache"](1, clock=lambda: 100)
        cache.set("live", "keep", 5)
        cache.set("expired", "discard", 0)
        self.assertEqual(cache.get("live"), "keep")
        self.assertIsNone(cache.get("expired"))

    def test_graders_reject_missing_error_context_and_negative_capacity(self) -> None:
        mutants = {
            "expense-csv-decimals": CASES["expense-csv-decimals"]["solution"]["expense_loader.py"].replace(
                'raise ExpenseFormatError("row %d has invalid amount" % line_number)',
                'raise ExpenseFormatError() if amount_text in ("twelve", "NaN") else ExpenseFormatError("row %d has invalid amount" % line_number)',
            ),
            "ttl-cache-bounds": CASES["ttl-cache-bounds"]["solution"]["ttl_cache.py"].replace("capacity <= 0", "capacity == 0"),
        }
        for slug, source in mutants.items():
            with self.subTest(case=slug), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                case = CASES[slug]
                for name, text in case["files"].items():
                    (workspace / name).write_text(text, encoding="utf-8")
                (workspace / case["write_paths"][0]).write_text(source, encoding="utf-8")
                grader = workspace / "grader.py"
                grader.write_text(case["tests"], encoding="utf-8")
                result = self._run_grader(grader, workspace)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_atomic_grader_accepts_equivalent_imports_and_error_propagation(self) -> None:
        case = CASES['json-task-store']
        reference = case['solution']['task_store.py']
        alternatives = [
            reference.replace('import os\n', '').replace('os.replace(temporary_path, self.path)', 'temporary_path.replace(self.path)'),
            reference.replace('from pathlib import Path', 'import pathlib').replace('Path(', 'pathlib.Path('),
            reference.replace('import os\n', 'from os import replace as atomic_replace\n').replace('os.replace(temporary_path, self.path)', 'atomic_replace(temporary_path, self.path)'),
            reference.replace('except OSError as error:\n            raise TaskStoreError("cannot write task store") from error', 'except OSError:\n            raise'),
        ]
        for source in alternatives:
            with self.subTest(source=source[:60]), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                (workspace/'task_store.py').write_text(source, encoding='utf-8')
                grader = workspace/'grader.py'
                grader.write_text(case['tests'], encoding='utf-8')
                result = self._run_grader(grader, workspace)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_cases_have_the_required_schema_and_coverage(self) -> None:
        self.assertEqual(len(CASES), 6)
        self.assertEqual(
            {case["category"] for case in CASES.values()},
            {"bugfix", "feature", "refactor", "analysis"},
        )
        expected_keys = {"category", "goal", "acceptance", "files", "solution", "tests", "write_paths"}
        for slug, case in CASES.items():
            self.assertRegex(slug, r"^[a-z0-9-]+$")
            self.assertEqual(set(case), expected_keys)
            self.assertIn(case["category"], {"bugfix", "feature", "refactor", "analysis"})
            self.assertIsInstance(case["goal"], str)
            self.assertGreaterEqual(len(case["acceptance"]), 2)
            self.assertTrue(all(isinstance(item, str) for item in case["acceptance"]))
            self.assertTrue(case["files"])
            self.assertTrue(case["solution"])
            self.assertTrue(case["write_paths"])
            self.assertEqual(set(case["solution"]), set(case["write_paths"]))
            for path, content in {**case["files"], **case["solution"]}.items():
                self.assertFalse(os.path.isabs(path))
                self.assertNotIn("..", Path(path).parts)
                self.assertTrue(path.endswith(".py") or path.endswith(".csv") or path == "report.json")
                self.assertIsInstance(content, str)
                content.encode("utf-8")
            self.assertIn("sys.argv[1]", case["tests"])
            self.assertIn("unittest.main(argv=[sys.argv[0]])", case["tests"])

    def test_starting_fixture_fails_and_reference_solution_passes(self) -> None:
        for slug, case in CASES.items():
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as temporary_directory:
                workspace = Path(temporary_directory) / "workspace"
                workspace.mkdir()
                originals = dict(case["files"])
                self._write_files(workspace, case["files"])
                grader = workspace / "grader.py"
                grader.write_text(case["tests"], encoding="utf-8")

                baseline = self._run_grader(grader, workspace)
                self.assertNotEqual(
                    baseline.returncode,
                    0,
                    f"starting fixture unexpectedly passed for {slug}: {baseline.stdout}{baseline.stderr}",
                )
                self.assertEqual(case["files"], originals)

                self._write_files(workspace, case["solution"])
                reference = self._run_grader(grader, workspace)
                self.assertEqual(
                    reference.returncode,
                    0,
                    f"reference solution failed for {slug}: {reference.stdout}{reference.stderr}",
                )
                self.assertEqual(case["files"], originals)

    def test_analysis_grader_accepts_equivalent_facts_and_rejects_wrong_facts(self) -> None:
        case = CASES["pipeline-call-chain-analysis"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            self._write_files(workspace, case["files"])
            grader = workspace / "grader.py"
            grader.write_text(case["tests"], encoding="utf-8")

            equivalent_report = """{
              "summary_fields": ["revenue_cents", "order_count"],
              "example_output": {"metrics": {"revenue_cents": 200, "order_count": 2}},
              "filter_behavior": {"note": "extra facts are allowed", "missing_active_included": true, "empty_string_included": false, "zero_included": false, "false_included": false},
              "example_input": [{"id": "paid", "cents": "125", "active": true}, {"id": "cancelled", "cents": "900", "active": false}, {"id": "legacy", "cents": "75"}],
              "call_chain": ["pipeline.publish.build_payload", "pipeline.ingest.fetch_orders", "pipeline.transform.summarize"],
              "entrypoint": "pipeline.publish.build_payload"
            }"""
            self._write_files(workspace, {"report.json": equivalent_report})
            self.assertEqual(self._run_grader(grader, workspace).returncode, 0)

            wrong_report = equivalent_report.replace('"missing_active_included": true', '"missing_active_included": false')
            self._write_files(workspace, {"report.json": wrong_report})
            self.assertNotEqual(self._run_grader(grader, workspace).returncode, 0)

            wrong_fact_type = equivalent_report.replace('"false_included": false', '"false_included": 0')
            self._write_files(workspace, {"report.json": wrong_fact_type})
            self.assertNotEqual(self._run_grader(grader, workspace).returncode, 0)

            wrong_input_type = equivalent_report.replace('"active": true', '"active": 1')
            self._write_files(workspace, {"report.json": wrong_input_type})
            self.assertNotEqual(self._run_grader(grader, workspace).returncode, 0)

            wrong_false_input_type = equivalent_report.replace('"active": false', '"active": 0')
            self._write_files(workspace, {"report.json": wrong_false_input_type})
            self.assertNotEqual(self._run_grader(grader, workspace).returncode, 0)

    def test_task_store_grader_rejects_direct_non_atomic_writes(self) -> None:
        case = CASES["json-task-store"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            self._write_files(workspace, case["files"])
            grader = workspace / "grader.py"
            grader.write_text(case["tests"], encoding="utf-8")
            direct_writer = '''import json
import os
from pathlib import Path


class TaskStoreError(RuntimeError):
    pass


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)

    def list(self):
        if not self.path.exists():
            return []
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TaskStoreError("cannot read task store") from error
        if not isinstance(records, list) or any(
            not isinstance(record, dict)
            or set(record) != {"id", "title"}
            or not isinstance(record["id"], str)
            or not record["id"]
            or not isinstance(record["title"], str)
            or not record["title"]
            or record["id"] != "task-%d" % index
            for index, record in enumerate(records, start=1)
        ):
            raise TaskStoreError("task store has invalid structure")
        return records

    def add(self, title):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be non-empty")
        records = self.list()
        record = {"id": "task-%d" % (len(records) + 1), "title": title.strip()}
        records.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(records), encoding="utf-8")
        return record
'''
            self._write_files(workspace, {"task_store.py": direct_writer})
            result = self._run_grader(grader, workspace)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("test_failed_atomic_replace", result.stderr)
            self.assertIn("FAILED (failures=1)", result.stderr)

    @staticmethod
    def _write_files(workspace: Path, files: dict[str, str]) -> None:
        for relative_path, content in files.items():
            destination = workspace / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")

    @staticmethod
    def _run_grader(grader: Path, workspace: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(grader), str(workspace)],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
