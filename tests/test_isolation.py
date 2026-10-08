from __future__ import annotations

import importlib
import subprocess
import tempfile
import unittest
from pathlib import Path


class IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        module = importlib.import_module("orchestra_kit.isolation")
        self.create = module.create_task_worktree
        self.prepare = module.prepare_task_worktree
        self.scope = module.check_write_scope
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "tracked.txt").write_text("base", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        self.git("add", "tracked.txt", ".gitignore")
        self.git("commit", "-m", "base")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True,
                              text=True, capture_output=True).stdout

    def test_creates_detached_private_worktree_without_a_commit_or_cleanup(self) -> None:
        destination = Path(self.temp.name) / "leaf"
        result = self.create(self.repo, "HEAD", destination)
        self.assertEqual(Path(result["worktree"]), destination.resolve())
        self.assertEqual(result["base_commit"], self.git("rev-parse", "HEAD").strip())
        self.assertTrue((destination / ".git").exists())
        self.assertTrue(Path(result["owner_marker"]).exists())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=destination).strip(), result["base_commit"])
        self.assertEqual(self.git("status", "--porcelain", cwd=destination), "")

    def test_ref_and_existing_destination_are_rejected_without_overwriting_user_files(self) -> None:
        destination = Path(self.temp.name) / "kept"
        destination.mkdir()
        keep = destination / "keep.txt"
        keep.write_text("user", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.create(self.repo, "--bad", destination)
        self.assertEqual(keep.read_text(encoding="utf-8"), "user")

    def test_scope_covers_committed_baseline_staged_unstaged_untracked_and_new_ignored_files(self) -> None:
        worktree = Path(self.temp.name) / "leaf"
        self.create(self.repo, "HEAD", worktree)
        (worktree / "tracked.txt").write_text("changed", encoding="utf-8")
        (worktree / "staged.txt").write_text("staged", encoding="utf-8")
        self.git("add", "staged.txt", cwd=worktree)
        (worktree / "new.txt").write_text("new", encoding="utf-8")
        (worktree / "ignored").mkdir()
        (worktree / "ignored" / "new.txt").write_text("ignored", encoding="utf-8")
        out = self.scope(worktree, ["tracked.txt", "staged.txt", "new.txt"])
        self.assertEqual(out["status"], "out-of-scope")
        self.assertEqual(out["out_of_scope"], ["ignored/new.txt"])
        within = self.scope(worktree, ["tracked.txt", "staged.txt", "new.txt", "ignored/new.txt"])
        self.assertEqual(within["status"], "within-scope")

    def test_scope_rejects_a_leaf_commit_even_when_the_worktree_diff_is_clean(self) -> None:
        worktree = Path(self.temp.name) / "leaf"
        self.create(self.repo, "HEAD", worktree)
        (worktree / "tracked.txt").write_text("committed leaf change", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=worktree)
        self.git("commit", "-m", "leaf change", cwd=worktree)
        result = self.scope(worktree, ["tracked.txt"])
        self.assertEqual(result["status"], "out-of-scope")
        self.assertFalse(result["base_commit_matches_head"])
        self.assertIn(".git/HEAD", result["out_of_scope"])

    def test_scope_forbids_git_metadata_as_a_declared_write_target(self) -> None:
        worktree = Path(self.temp.name) / "leaf"
        self.create(self.repo, "HEAD", worktree)
        with self.assertRaisesRegex(ValueError, "normalized"):
            self.scope(worktree, [".git/config"])

    def test_prepare_copies_parent_policy_without_mutating_parent_and_pins_leaf_policy(self) -> None:
        kit = Path(__file__).resolve().parents[1]
        policy = self.repo / "policy.md"
        policy.write_text("parent policy", encoding="utf-8")
        orchestra = self.repo / ".orchestra"
        orchestra.mkdir()
        (orchestra / "project.toml").write_text((kit / "templates" / "project.toml").read_text().replace(
            'policy_files = []', 'policy_files = ["policy.md"]'), encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("parent instructions", encoding="utf-8")
        worktree = Path(self.temp.name) / "leaf"
        result = self.prepare(self.repo, kit, "HEAD", worktree)
        self.assertEqual((worktree / "policy.md").read_text(encoding="utf-8"), "parent policy")
        self.assertEqual((worktree / "AGENTS.md").read_text(encoding="utf-8"), "parent instructions")
        self.assertEqual(policy.read_text(encoding="utf-8"), "parent policy")
        self.assertIn(".orchestra/project.toml", result["managed_files"])
        (worktree / ".orchestra" / "project.toml").write_text("changed", encoding="utf-8")
        checked = self.scope(worktree, ["policy.md"])
        self.assertEqual(checked["status"], "out-of-scope")
        self.assertIn(".orchestra/project.toml", checked["out_of_scope"])

    def test_prepare_keeps_checkouts_isolated_and_excludes_only_runner_artifact_directories(self) -> None:
        kit = Path(__file__).resolve().parents[1]
        (self.repo / ".orchestra").mkdir()
        (self.repo / ".orchestra" / "project.toml").write_text(
            (kit / "templates" / "project.toml").read_text(), encoding="utf-8")
        first, second = Path(self.temp.name) / "first", Path(self.temp.name) / "second"
        self.prepare(self.repo, kit, "HEAD", first)
        self.prepare(self.repo, kit, "HEAD", second)
        (first / "tracked.txt").write_text("first only", encoding="utf-8")
        self.assertEqual((second / "tracked.txt").read_text(encoding="utf-8"), "base")
        artifact = first / ".orchestra" / "executions" / "run" / "receipt.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("runner artifact", encoding="utf-8")
        (first / ".orchestra" / "evil.txt").write_text("outside", encoding="utf-8")
        checked = self.scope(first, ["tracked.txt"])
        self.assertEqual(checked["status"], "out-of-scope")
        self.assertIn(".orchestra/evil.txt", checked["out_of_scope"])
        self.assertNotIn(".orchestra/executions/run/receipt.json", checked["changed_paths"])
