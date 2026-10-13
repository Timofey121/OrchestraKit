from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class NavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.navigation = importlib.import_module("orchestra_kit.navigation")

    def write(self, name: str, content: str, *, encoding: str = "utf-8") -> Path:
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding)
        return path

    def test_builds_deterministic_python_symbols_and_path_only_other_files(self) -> None:
        self.write("pkg/sample.py", "class Thing:\n    def method(self):\n        pass\n\ndef helper():\n    pass\n")
        self.write("web/app.js", "export function client() {}\n")

        index = self.navigation.build_index(self.project, ["web/app.js", "pkg/sample.py"])

        self.assertEqual(index["schema"], 1)
        self.assertEqual([entry["path"] for entry in index["files"]], ["pkg/sample.py", "web/app.js"])
        python = index["files"][0]
        self.assertEqual(python["status"], "indexed")
        self.assertEqual(python["language"], "python")
        self.assertEqual(
            python["symbols"],
            [
                {"name": "Thing", "kind": "class", "line": 1},
                {"name": "method", "kind": "function", "line": 2},
                {"name": "helper", "kind": "function", "line": 5},
            ],
        )
        other = index["files"][1]
        self.assertEqual(other["status"], "unsupported")
        self.assertEqual(other["language"], "javascript")
        self.assertEqual(other["symbols"], [])
        stored = json.loads((self.project / ".orchestra/navigation/index.json").read_text())
        self.assertEqual(stored, index)

    def test_search_is_literal_ranked_and_rechecks_source_digests(self) -> None:
        self.write("a.py", "def alphabet():\n    pass\ndef beta_alpha():\n    pass\n")
        self.write("b.py", "def alpha():\n    pass\n")
        self.navigation.build_index(self.project, ["a.py", "b.py"])

        found = self.navigation.search_index(self.project, "alpha")
        self.assertEqual([item["name"] for item in found["results"]], ["alpha", "alphabet", "beta_alpha"])
        self.assertEqual(found["stale"], [])

        self.write("b.py", "def changed():\n    pass\n")
        stale = self.navigation.search_index(self.project, "alpha")
        self.assertEqual([item["path"] for item in stale["results"]], ["a.py", "a.py"])
        self.assertEqual(stale["stale"], ["changed:b.py"])
        self.assertIn("build_index", stale["rebuild_hint"])

        rebuilt = self.navigation.build_index(self.project, ["a.py", "b.py"])
        self.assertEqual(rebuilt, self.navigation.build_index(self.project, ["a.py", "b.py"]))
        self.assertEqual([item["name"] for item in self.navigation.search_index(self.project, "changed")["results"]], ["changed"])

    def test_search_returns_a_path_hit_for_current_python_without_symbols(self) -> None:
        self.write("settings_only.py", "VALUE = 1\n")
        self.navigation.build_index(self.project, ["settings_only.py"])

        found = self.navigation.search_index(self.project, "settings")

        self.assertEqual(found["results"], [{"path": "settings_only.py", "name": "settings_only.py", "kind": "path", "line": 0, "language": "python", "parser_status": "indexed"}])
        self.assertTrue(found["advisory"])

    def test_reports_missing_syntax_and_encoding_errors_explicitly(self) -> None:
        self.write("bad.py", "def broken(:\n")
        binary = self.project / "binary.py"
        binary.write_bytes(b"def nope():\n\xff")
        index = self.navigation.build_index(self.project, ["bad.py", "binary.py", "missing.py"])
        entries = {entry["path"]: entry for entry in index["files"]}
        self.assertEqual(entries["bad.py"]["status"], "syntax_error")
        self.assertIn("line 1", entries["bad.py"]["error"])
        self.assertEqual(entries["binary.py"]["status"], "encoding_error")
        self.assertEqual(entries["missing.py"]["status"], "missing")
        self.assertEqual(
            self.navigation.search_index(self.project, "bad.py")["results"],
            [{"path": "bad.py", "name": "bad.py", "kind": "path", "line": 0, "language": "python", "parser_status": "syntax_error"}],
        )
        self.assertEqual(
            self.navigation.search_index(self.project, "binary.py")["results"],
            [{"path": "binary.py", "name": "binary.py", "kind": "path", "line": 0, "language": "python", "parser_status": "encoding_error"}],
        )

    def test_rejects_unsafe_paths_symlinks_giant_files_and_known_secrets(self) -> None:
        self.write("safe.py", "def safe():\n    pass\n")
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("def outside():\n    pass\n")
        (self.project / "link.py").symlink_to(outside)
        self.write("nested/inside.py", "def inside():\n    pass\n")
        (self.project / "linked").symlink_to(self.project / "nested", target_is_directory=True)
        self.write("secret.py", "OPENAI_API_KEY = 'sk-super-secret'\n")
        self.write("runtime.py", "token = 'actual-runtime-secret'\n")
        self.write("large.py", "x" * (self.navigation.MAX_FILE_BYTES + 1))

        for paths in (["../outside.py"], ["link.py"], ["linked/inside.py"], ["large.py"], ["secret.py"], ["safe.py"] * 257):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.navigation.build_index(self.project, paths)
        with mock.patch.dict("os.environ", {"CUSTOM_TOKEN": "actual-runtime-secret"}):
            with self.assertRaises(ValueError):
                self.navigation.build_index(self.project, ["runtime.py"])

    def test_corrupt_index_fails_safely_and_results_are_bounded(self) -> None:
        self.write("many.py", "\n".join(f"def target_{number}(): pass" for number in range(20)))
        self.navigation.build_index(self.project, ["many.py"])
        found = self.navigation.search_index(self.project, "target", limit=3, max_chars=600)
        self.assertEqual(len(found["results"]), 3)
        self.assertLessEqual(len(json.dumps(found)), 600)
        self.assertTrue(found["advisory"])

        destination = self.project / ".orchestra/navigation/index.json"
        destination.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.navigation.search_index(self.project, "target")

        destination.write_text(json.dumps({"schema": 1, "files": [{"path": "many.py", "sha256": "0" * 64, "status": "indexed", "language": "python", "symbols": [], "untrusted": True}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.navigation.search_index(self.project, "target")

        outside = Path(self.temp.name) / "outside-index.json"
        outside.write_text("{}", encoding="utf-8")
        destination.unlink()
        destination.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.navigation.search_index(self.project, "target")

    def test_unchanged_python_uses_cached_symbols_without_reparsing(self) -> None:
        self.write("cached.py", "def cached():\n    pass\n")
        self.navigation.build_index(self.project, ["cached.py"])
        with mock.patch.object(self.navigation, "_symbols", wraps=self.navigation._symbols) as symbols:
            self.navigation.build_index(self.project, ["cached.py"])
        symbols.assert_not_called()
