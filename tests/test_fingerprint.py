from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path


class FingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.fingerprint = importlib.import_module("orchestra_kit.fingerprint")

    def write(self, name: str, content: str) -> Path:
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_hashes_content_without_reporting_it(self) -> None:
        self.write("src/app.py", "secret implementation\n")
        snapshot = self.fingerprint.fingerprint_paths(self.project, ["src/app.py"])
        self.assertEqual(snapshot["schema"], 1)
        self.assertEqual(snapshot["files"]["src/app.py"]["state"], "regular")
        self.assertRegex(snapshot["files"]["src/app.py"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("secret implementation", repr(snapshot))
        self.assertEqual(self.fingerprint.check_fingerprint(self.project, snapshot), ())

    def test_reports_stale_content_missing_created_and_removed_files(self) -> None:
        self.write("a.txt", "before")
        stale = self.fingerprint.fingerprint_paths(self.project, ["a.txt"])
        self.write("a.txt", "after")
        self.assertEqual(self.fingerprint.check_fingerprint(self.project, stale), ("changed:a.txt",))

        expected = self.fingerprint.fingerprint_paths(self.project, ["new.txt"])
        self.write("new.txt", "created")
        self.assertEqual(self.fingerprint.check_fingerprint(self.project, expected), ("changed:new.txt",))

        nested_expected = self.fingerprint.fingerprint_paths(self.project, ["future/new.txt"])
        self.assertEqual(nested_expected["files"]["future/new.txt"], {"state": "missing"})

        removed = self.fingerprint.fingerprint_paths(self.project, ["a.txt"])
        (self.project / "a.txt").unlink()
        self.assertEqual(self.fingerprint.check_fingerprint(self.project, removed), ("missing:a.txt",))

    def test_ordering_and_duplicates_cannot_change_the_snapshot(self) -> None:
        self.write("a.txt", "a")
        self.write("nested/b.txt", "b")
        first = self.fingerprint.fingerprint_paths(self.project, ["nested/b.txt", "a.txt", "a.txt"])
        second = self.fingerprint.fingerprint_paths(self.project, ["a.txt", "nested/b.txt"])
        self.assertEqual(first, second)
        self.assertEqual(list(first["files"]), ["a.txt", "nested/b.txt"])

    def test_rejects_bad_paths_and_unsafe_file_types(self) -> None:
        self.write("inside.txt", "ok")
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("outside")
        (self.project / "escape.txt").symlink_to(outside)
        outside_dir = Path(self.temp.name) / "outside-dir"
        outside_dir.mkdir()
        (self.project / "linked").symlink_to(outside_dir, target_is_directory=True)
        for paths in ([], [""], [None], ["../outside.txt"], ["/tmp/outside.txt"], ["."], ["inside//file"], ["escape.txt"], ["linked/file.txt"]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.fingerprint.fingerprint_paths(self.project, paths)
        (self.project / "directory").mkdir()
        with self.assertRaises(ValueError):
            self.fingerprint.fingerprint_paths(self.project, ["directory"])
        with self.assertRaises(ValueError):
            self.fingerprint.fingerprint_paths(self.project, ["inside.txt"] * 257)

    def test_check_reports_a_file_that_becomes_unsafe_as_unreadable(self) -> None:
        self.write("a.txt", "safe")
        snapshot = self.fingerprint.fingerprint_paths(self.project, ["a.txt"])
        (self.project / "a.txt").unlink()
        (self.project / "a.txt").symlink_to(Path(self.temp.name) / "outside.txt")
        self.assertEqual(self.fingerprint.check_fingerprint(self.project, snapshot), ("unreadable:a.txt",))

    def test_check_refuses_tampered_or_malformed_snapshots(self) -> None:
        self.write("a.txt", "a")
        snapshot = self.fingerprint.fingerprint_paths(self.project, ["a.txt"])
        for mutate in (
            lambda value: value.update(schema=2),
            lambda value: value.update(schema=True),
            lambda value: value.update(schema=1.0),
            lambda value: value.update(aggregate_sha256="0" * 64),
            lambda value: value.update(aggregate_sha256=True),
            lambda value: value.update(files={"a.txt": {"state": "regular", "sha256": "broken"}}),
            lambda value: value.update(files={"a.txt": {"state": True}}),
            lambda value: value.update(unexpected=True),
        ):
            with self.subTest(mutate=mutate):
                candidate = {key: (dict(value) if isinstance(value, dict) else value) for key, value in snapshot.items()}
                mutate(candidate)
                with self.assertRaises(ValueError):
                    self.fingerprint.check_fingerprint(self.project, candidate)
