"""Shared validation and execution for bounded project checks."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from .log_capture import sanitized_output, sensitive_values


FAILURE_CAUSES = frozenset({"implementation", "context", "capability", "environment"})


def validate_checks(value: Any) -> list[dict[str, Any]]:
    """Validate the public check schema and return a normalized copy."""
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("checks must contain between 1 and 8 entries")
    checks: list[dict[str, Any]] = []
    for check in value:
        if not isinstance(check, dict) or set(check) != {"argv", "failure_cause"}:
            raise ValueError("each check requires argv and failure_cause")
        argv, cause = check["argv"], check["failure_cause"]
        if not isinstance(argv, list) or not 1 <= len(argv) <= 64 or not all(
                isinstance(item, str) and item and "\0" not in item and len(item) <= 8192
                for item in argv):
            raise ValueError("check argv must be a nonempty string list")
        if not isinstance(cause, str) or cause not in FAILURE_CAUSES:
            raise ValueError("invalid check failure_cause")
        checks.append({"argv": argv, "failure_cause": cause})
    return checks


def _drain(process: subprocess.Popen[Any]) -> None:
    """Terminate a process group and wait for all retained output handles."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def communicate(process: subprocess.Popen[Any], prompt: str | None, timeout: int,
                cancel_path: Path) -> tuple[int | None, bool | str]:
    """Communicate with a process while enforcing timeout and cancellation."""
    deadline = time.monotonic() + timeout
    first = True
    try:
        while True:
            if cancel_path.exists():
                _drain(process)
                return None, "cancelled"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _drain(process)
                return None, True
            try:
                process.communicate(prompt if first else None, timeout=min(.2, remaining))
                if cancel_path.exists():
                    _drain(process)
                    return None, "cancelled"
                _drain(process)
                return process.returncode, False
            except subprocess.TimeoutExpired:
                first = False
    except BaseException:
        _drain(process)
        raise


def create_owned_file(path: Path) -> None:
    """Create a new private regular artifact without following an existing link."""
    if path.exists() or path.is_symlink():
        raise ValueError("execution artifact already exists or is a symlink")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)


def run_checks(project: Path, checks: list[dict[str, Any]], timeout_seconds: int,
               execution_dir: Path, attempt: int,
               secrets: Sequence[str] = ()) -> tuple[str | None, list[dict[str, Any]]]:
    """Run validated checks serially and retain bounded private logs."""
    outcomes: list[dict[str, Any]] = []
    for check in checks:
        try:
            path = execution_dir / f"attempt-{attempt}-check-{len(outcomes) + 1}.log"
            create_owned_file(path)
            with sanitized_output(path, tuple(secrets) + sensitive_values()) as output:
                process = subprocess.Popen(
                    check["argv"], cwd=project, shell=False, stdout=output,
                    stderr=subprocess.STDOUT, text=True, start_new_session=True,
                )
                returncode, stopped = communicate(
                    process, None, timeout_seconds, execution_dir / "cancel.json")
            os.chmod(path, 0o600)
            outcome = {"argv": check["argv"], "returncode": returncode,
                       "failure_cause": check["failure_cause"], "log": str(path)}
            if output.overflowed:
                outcome["output_truncated"] = True
                outcome["reason"] = "environment: check output exceeded the retained log limit"
            outcomes.append(outcome)
            if stopped == "cancelled":
                return "cancelled", outcomes
            if output.overflowed or returncode is None:
                return "environment", outcomes
            if returncode:
                return check["failure_cause"], outcomes
        except (OSError, subprocess.TimeoutExpired):
            outcomes.append({"argv": check["argv"], "returncode": None,
                             "failure_cause": "environment"})
            return "environment", outcomes
    return None, outcomes
