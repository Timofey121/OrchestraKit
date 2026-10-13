"""Platform-specific behavior for the portable common check runner."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import select
import os
from pathlib import Path
from unittest.mock import Mock, patch


class CheckPortabilityTests(unittest.TestCase):
    def test_windows_common_check_runs_without_posix_session_options(self) -> None:
        from orchestra_kit.check_execution import run_checks

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("orchestra_kit.check_execution._is_windows", return_value=True):
                reason, outcomes = run_checks(
                    root,
                    [{"argv": [sys.executable, "-c", "print('portable check')"],
                      "failure_cause": "implementation"}],
                    timeout_seconds=5,
                    execution_dir=root,
                    attempt=1,
                )
            self.assertIsNone(reason)
            self.assertEqual(outcomes[0]["returncode"], 0)
            self.assertIn("portable check", Path(outcomes[0]["log"]).read_text())

    def test_windows_drain_uses_bounded_direct_process_termination(self) -> None:
        from orchestra_kit.check_execution import _drain

        process = Mock()
        process.pid = 123
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["check"], 5), None,
        ]
        with patch("orchestra_kit.check_execution._is_windows", return_value=True), patch(
                "orchestra_kit.check_execution.os.killpg", create=True) as killpg:
            _drain(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.communicate.call_args_list[0].kwargs, {"timeout": 5})
        self.assertEqual(process.communicate.call_args_list[1].kwargs, {"timeout": 5})
        killpg.assert_not_called()

    def test_windows_pipe_readiness_uses_peeknamedpipe_instead_of_select(self) -> None:
        from orchestra_kit import log_capture

        reader = Mock()
        reader.fileno.return_value = 42
        with patch("orchestra_kit.log_capture._is_windows", return_value=True), patch(
                "orchestra_kit.log_capture._windows_pipe_available", return_value=17
        ) as available, patch("orchestra_kit.log_capture.select.select",
                              side_effect=AssertionError("select is not valid for Windows pipes")):
            self.assertEqual(log_capture._readable_bytes(reader, .01), 17)
        available.assert_called_once_with(42)

    def test_windows_pipe_capture_drains_and_redacts_without_selecting_the_pipe(self) -> None:
        from orchestra_kit.log_capture import sanitized_output
        from orchestra_kit import log_capture

        native_select = select.select
        native_windows_peek = log_capture._windows_pipe_available

        def simulated_peek(fd: int) -> int:
            if os.name == 'nt':
                return native_windows_peek(fd)
            ready, _, _ = native_select([fd], [], [], 0)
            return 65536 if ready else 0

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "output.log"
            with patch("orchestra_kit.log_capture._is_windows", return_value=True), patch(
                    "orchestra_kit.log_capture._windows_pipe_available", side_effect=simulated_peek), patch(
                    "orchestra_kit.log_capture.select.select",
                    side_effect=AssertionError("Windows capture must not select anonymous pipes")):
                with sanitized_output(path, ["top-secret"]) as output:
                    output.write(b"before top-secret after")
            self.assertEqual(path.read_text(), "before [REDACTED] after")
