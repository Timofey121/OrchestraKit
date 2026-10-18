from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from orchestra_kit.fingerprint import fingerprint_paths
from orchestra_kit.repair_checkpoint import capture_checkpoint, restore_checkpoint


class RepairCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        (self.project / ".orchestra" / "workflows").mkdir(parents=True)

    def checkpoint(self, files: list[str]) -> dict:
        directory = self.project / ".orchestra" / "workflows" / "repair-checkpoint"
        return capture_checkpoint(self.project, files, directory)

    def write(self, relative: str, data: bytes, mode: int = 0o644) -> Path:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def test_restores_binary_content_modes_and_originally_missing_files(self) -> None:
        source = self.write("src/app.bin", b"before\x00\xff\n", 0o751)
        checkpoint = self.checkpoint(["src/app.bin", "src/generated.py"])
        expected = fingerprint_paths(self.project, ["src/app.bin", "src/generated.py"])
        source.write_bytes(b"after")
        source.chmod(0o600)
        self.write("src/generated.py", b"generated")
        expected = fingerprint_paths(self.project, ["src/app.bin", "src/generated.py"])

        restore_checkpoint(self.project, checkpoint, expected)

        self.assertEqual(source.read_bytes(), b"before\x00\xff\n")
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o751)
        self.assertFalse((self.project / "src/generated.py").exists())
        manifest = Path(checkpoint["directory"]) / "manifest.json"
        self.assertTrue(manifest.is_file())
        json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["snapshot"], fingerprint_paths(self.project, ["src/app.bin", "src/generated.py"]))

    def test_refuses_concurrently_changed_target_without_overwriting_it(self) -> None:
        target = self.write("a.txt", b"original")
        checkpoint = self.checkpoint(["a.txt"])
        target.write_bytes(b"review repair")
        expected = fingerprint_paths(self.project, ["a.txt"])
        target.write_bytes(b"concurrent edit")

        with self.assertRaisesRegex(ValueError, "changed"):
            restore_checkpoint(self.project, checkpoint, expected)
        self.assertEqual(target.read_bytes(), b"concurrent edit")

    def test_refuses_target_and_ancestor_symlinks(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        outside_file = outside / "value.txt"
        outside_file.write_bytes(b"outside")

        (self.project / "file-link").symlink_to(outside_file)
        with self.assertRaisesRegex(ValueError, "symlink|unreadable"):
            self.checkpoint(["file-link"])

        (self.project / "parent-link").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.checkpoint(["parent-link/value.txt"])
        self.assertEqual(outside_file.read_bytes(), b"outside")

    def test_refuses_backup_symlink_and_damaged_blob_before_any_write(self) -> None:
        first = self.write("first.txt", b"first-original")
        second = self.write("second.txt", b"second-original")
        checkpoint = self.checkpoint(["first.txt", "second.txt"])
        first.write_bytes(b"first-repair")
        second.write_bytes(b"second-repair")
        expected = fingerprint_paths(self.project, ["first.txt", "second.txt"])
        regular_entries = [entry for entry in checkpoint["entries"] if entry["state"] == "regular"]
        blob = Path(checkpoint["directory"]) / regular_entries[1]["blob"]
        blob.write_bytes(b"damaged")

        with self.assertRaisesRegex(ValueError, "backup"):
            restore_checkpoint(self.project, checkpoint, expected)
        self.assertEqual(first.read_bytes(), b"first-repair")
        self.assertEqual(second.read_bytes(), b"second-repair")

        blob.unlink()
        blob.symlink_to(Path(self.temporary.name) / "outside-blob")
        with self.assertRaisesRegex(ValueError, "backup"):
            restore_checkpoint(self.project, checkpoint, expected)
        self.assertEqual(first.read_bytes(), b"first-repair")

    def test_refuses_modified_manifest_and_unsafe_metadata(self) -> None:
        target = self.write("a.txt", b"original")
        checkpoint = self.checkpoint(["a.txt"])
        target.write_bytes(b"repair")
        expected = fingerprint_paths(self.project, ["a.txt"])
        manifest = Path(checkpoint["directory"]) / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")

        with self.assertRaisesRegex(ValueError, "manifest"):
            restore_checkpoint(self.project, checkpoint, expected)
        self.assertEqual(target.read_bytes(), b"repair")

        unsafe = copy.deepcopy(checkpoint)
        unsafe["entries"][0]["blob"] = "../outside"
        with self.assertRaises(ValueError):
            restore_checkpoint(self.project, unsafe, expected)
        self.assertEqual(target.read_bytes(), b"repair")

    def test_capture_is_bounded_and_requires_a_new_private_directory(self) -> None:
        self.write("large.bin", b"12345")
        with self.assertRaisesRegex(ValueError, "size|bytes"):
            capture_checkpoint(
                self.project,
                ["large.bin"],
                self.project / ".orchestra" / "workflows" / "too-large",
                max_bytes=4,
            )
        existing = self.project / ".orchestra" / "workflows" / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(ValueError, "new"):
            capture_checkpoint(self.project, ["large.bin"], existing)

    def test_capture_creates_private_nested_checkpoint_parents(self) -> None:
        self.write("a.txt", b"original")
        directory = self.project / ".orchestra" / "workflows" / "workflow-id" / "repair-1"

        checkpoint = capture_checkpoint(self.project, ["a.txt"], directory)

        self.assertEqual(Path(checkpoint["directory"]), directory.resolve())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(directory.parent.stat().st_mode), 0o700)

    def test_capture_refuses_symlinked_checkpoint_parent(self) -> None:
        self.write("a.txt", b"original")
        workflows = self.project / ".orchestra" / "workflows"
        real = workflows / "real-parent"
        real.mkdir()
        alias = workflows / "alias-parent"
        alias.symlink_to(real, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            capture_checkpoint(self.project, ["a.txt"], alias / "repair-1")

    def test_restore_rejects_expected_snapshot_for_different_targets(self) -> None:
        target = self.write("a.txt", b"original")
        other = self.write("other.txt", b"other")
        checkpoint = self.checkpoint(["a.txt"])
        target.write_bytes(b"repair")
        expected = fingerprint_paths(self.project, ["other.txt"])

        with self.assertRaisesRegex(ValueError, "same files"):
            restore_checkpoint(self.project, checkpoint, expected)
        self.assertEqual(target.read_bytes(), b"repair")
        self.assertEqual(other.read_bytes(), b"other")

    def test_refuses_symlinked_checkpoint_directory_or_manifest(self) -> None:
        target = self.write("a.txt", b"original")
        checkpoint = self.checkpoint(["a.txt"])
        target.write_bytes(b"repair")
        expected = fingerprint_paths(self.project, ["a.txt"])
        directory = Path(checkpoint["directory"])
        manifest = directory / "manifest.json"
        outside = Path(self.temporary.name) / "outside-manifest"
        outside.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "manifest|symlink"):
            restore_checkpoint(self.project, checkpoint, expected)
        self.assertEqual(target.read_bytes(), b"repair")


if __name__ == "__main__":
    unittest.main()
