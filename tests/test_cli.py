from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        self.kit = Path(__file__).resolve().parents[1]
        self.environment = dict(os.environ)
        self.environment["PYTHONPATH"] = str(self.kit / "src")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "orchestra_kit", *arguments],
            cwd=self.kit,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_init_sync_and_doctor_work_end_to_end(self) -> None:
        initialized = self.run_cli("init", str(self.project), "--name", "Demo")
        checked = self.run_cli("doctor", str(self.project))

        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertIn("initialized", initialized.stdout)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("healthy", checked.stdout)

    def test_doctor_fails_when_generated_file_drifted_and_sync_repairs_it(self) -> None:
        self.run_cli("init", str(self.project), "--name", "Demo")
        agent = self.project / ".codex/agents/orchestra-worker.toml"
        agent.write_text(agent.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

        drifted = self.run_cli("doctor", str(self.project))
        synchronized = self.run_cli("sync", str(self.project))
        repaired = self.run_cli("doctor", str(self.project))

        self.assertEqual(drifted.returncode, 1)
        self.assertIn("drift", drifted.stderr)
        self.assertEqual(synchronized.returncode, 0, synchronized.stderr)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)

    def test_init_refuses_to_replace_existing_project_configuration(self) -> None:
        first = self.run_cli("init", str(self.project), "--name", "First")
        config_path = self.project / ".orchestra/project.toml"
        before = config_path.read_text(encoding="utf-8")

        second = self.run_cli("init", str(self.project), "--name", "Second")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(config_path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
