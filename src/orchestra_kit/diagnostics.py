"""Read bounded, redacted diagnostic excerpts from retained Orchestra logs."""
from __future__ import annotations

import json
import re
import os
import stat
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from .log_capture import MAX_RETAINED_BYTES, TRUNCATION_MARKER, redacted_text, sensitive_values, _patterns


_ALLOWED_AREAS = ((".orchestra", "checks"), (".orchestra", "executions"))
_DIAGNOSTIC_WORDS = ("traceback", "exception", "error", "failure", "failed")
_TRUNCATION_TEXT = TRUNCATION_MARKER.decode("utf-8").strip()
_MAX_QUERY_LINES = 200
_MAX_CONTEXT_LINES = 20
_MAX_QUERY_CHARS = 6_000
_EXCEPTION_LINE = re.compile(r"\b[A-Za-z_]\w*(?:Error|Exception):")


def _redaction_values() -> tuple[str, ...]:
    values=set(sensitive_values())
    # A line-oriented reader must also redact fragments of multiline values.
    for value in tuple(values):
        if '\n' in value or '\r' in value:
            values.update(part for part in value.splitlines() if part)
    return tuple(values)


def _project_root(project: Path) -> Path:
    root = Path(project).resolve()
    if not root.is_dir():
        raise ValueError("project must be an existing directory")
    return root


def _has_symlink_component(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _relative_parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or not relative_path or "\0" in relative_path:
        raise ValueError("log path must be a nonempty relative path")
    path = Path(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("log path must be normalized and project-relative")
    parts = path.parts
    if relative_path != Path(*parts).as_posix():
        raise ValueError("log path must be normalized and project-relative")
    if len(parts) < 3 or parts[:2] not in _ALLOWED_AREAS:
        raise ValueError("log path is outside retained Orchestra log areas")
    return parts


def _safe_retained_log(project: Path, relative_path: str) -> tuple[Path, str]:
    """Return a regular, non-symlink retained log under an allowed area."""
    root = _project_root(project)
    parts = _relative_parts(relative_path)
    path = root
    for part in parts:
        path = path / part
        if _has_symlink_component(path, root):
            raise ValueError("symlinked log path is refused")
    try:
        resolved = path.resolve(strict=True)
        details = path.stat()
    except OSError as exc:
        raise ValueError("retained log does not exist") from exc
    if resolved != root and root not in resolved.parents:
        raise ValueError("retained log escapes project")
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("retained log must be a regular file")
    if details.st_size > MAX_RETAINED_BYTES:
        raise ValueError("retained log exceeds the capture limit")
    return path, Path(*parts).as_posix()


def _open_retained_log(path: Path, root: Path) -> int:
    """Open a checked log without following a replacement final symlink.

    ``O_NOFOLLOW`` is used where the host exposes it.  Every platform then gets
    an after-open identity and root check, so the file descriptor is never read
    when the pathname was replaced between validation and opening.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("retained log could not be opened safely") from exc
    try:
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise ValueError("retained log must be a regular file")
        if descriptor_stat.st_size > MAX_RETAINED_BYTES:
            raise ValueError("retained log exceeds the capture limit")
        resolved = path.resolve(strict=True)
        if _has_symlink_component(path, root) or (resolved != root and root not in resolved.parents):
            raise ValueError("retained log escapes project")
        name_stat = path.stat()
        if not os.path.samestat(descriptor_stat, name_stat):
            raise ValueError("retained log changed while opening")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@dataclass
class _ReadState:
    incomplete: bool = False


def _check_query(query: str | None, values: Sequence[str]) -> str | None:
    if query is None:
        return None
    if not isinstance(query, str) or not query or len(query) > 1_000:
        raise ValueError("query must be a nonempty bounded literal string")
    safe_query = redacted_text(query, values)
    if safe_query != query or "[REDACTED]" in query:
        raise ValueError("secret queries are refused")
    return query


def _iter_redacted_lines(path: Path, values: Sequence[str], *, max_chars: int,
                         root: Path, state: _ReadState) -> Iterator[tuple[int, str, bool]]:
    """Yield bounded redacted lines without retaining giant lines in memory."""
    # Four bytes per Unicode scalar is enough for a useful UTF-8 prefix.  A small
    # margin avoids dropping a complete short credential at the boundary.
    secret_padding = max(map(len, _patterns(values)), default=0)
    retained = max(256, max_chars * 4 + secret_padding)
    prefix = bytearray()
    overflowed = False
    number = 0

    def append(segment: bytes) -> None:
        nonlocal overflowed
        room = retained - len(prefix)
        if room > 0:
            prefix.extend(segment[:room])
        if len(segment) > room:
            overflowed = True

    def emit() -> tuple[int, str, bool]:
        nonlocal prefix, overflowed, number
        number += 1
        raw = bytes(prefix).rstrip(b"\r\n")
        text = redacted_text(raw.decode("utf-8", errors="replace"), values)
        line_overflowed = overflowed or len(text) > max_chars
        if len(text) > max_chars:
            text = text[:max_chars]
        result = (number, text, line_overflowed)
        prefix = bytearray()
        overflowed = False
        return result

    descriptor = _open_retained_log(path, root)
    try:
        source = os.fdopen(descriptor, "rb")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    try:
        remaining = MAX_RETAINED_BYTES
        while remaining:
            chunk = source.read(min(8192, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            start = 0
            while True:
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    append(chunk[start:])
                    break
                append(chunk[start:newline + 1])
                yield emit()
                start = newline + 1
        # A concurrent writer may extend the file after validation.  The
        # descriptor is safe, but only the capture limit is ever consumed.
        state.incomplete = os.fstat(source.fileno()).st_size > (
            MAX_RETAINED_BYTES - remaining
        )
    finally:
        source.close()
    if prefix or overflowed:
        yield emit()


def _validate_window(start_line: int, max_lines: int, context_lines: int, max_chars: int) -> None:
    if type(start_line) is not int or start_line < 1:
        raise ValueError("start_line must be a positive integer")
    if type(max_lines) is not int or not 1 <= max_lines <= _MAX_QUERY_LINES:
        raise ValueError("max_lines is outside the bounded range")
    if type(context_lines) is not int or not 0 <= context_lines <= _MAX_CONTEXT_LINES:
        raise ValueError("context_lines is outside the bounded range")
    if type(max_chars) is not int or not 1 <= max_chars <= _MAX_QUERY_CHARS:
        raise ValueError("max_chars is outside the bounded range")


def query_log(project: Path, relative_path: str, *, query: str | None = None,
              start_line: int = 1, max_lines: int = 40, context_lines: int = 2,
              max_chars: int = 6000) -> dict[str, Any]:
    """Return a page of literal-searchable, redacted retained-log lines.

    ``start_line`` is a source line number.  When ``query`` is present, callers
    can use the highest returned match line plus one for the next page.
    """
    _validate_window(start_line, max_lines, context_lines, max_chars)
    _relative_parts(relative_path)
    values = _redaction_values()
    literal = _check_query(query, values)
    if redacted_text(relative_path, values) != relative_path:
        raise ValueError("secret log paths are refused")
    root = _project_root(project)
    path, reference = _safe_retained_log(root, relative_path)
    output: dict[int, dict[str, Any]] = {}
    before: deque[tuple[int, str, bool]] = deque(maxlen=context_lines)
    after = 0
    more = False
    incomplete = False
    last_scanned = start_line - 1
    last_match: int | None = None
    output_chars = 0

    def add_line(number: int, text: str, line_truncated: bool) -> bool:
        """Add one line while keeping the total returned text within max_chars."""
        nonlocal output_chars
        if number in output:
            return True
        remaining = max_chars - output_chars
        if remaining <= 0:
            return False
        shown = text[:remaining]
        truncated = line_truncated or len(shown) != len(text)
        output[number] = {"number": number, "line_number": number, "text": shown,
                          "line_truncated": truncated}
        output_chars += len(shown)
        return True

    read_state = _ReadState()
    for number, text, line_truncated in _iter_redacted_lines(
            path, values, max_chars=max_chars, root=root, state=read_state):
        marker = _TRUNCATION_TEXT in text
        incomplete = incomplete or marker or line_truncated
        if number < start_line:
            if literal is not None:
                before.append((number, text, line_truncated))
            continue
        last_scanned = number
        if literal is None:
            if len(output) < max_lines:
                if not add_line(number, text, line_truncated):
                    more = True
                    break
            else:
                more = True
                break
            continue

        matched = literal in text
        if matched:
            if len(output) >= max_lines:
                more = True
                continue
            if not add_line(number, text, line_truncated):
                more = True
                continue
            last_match = number
            after = context_lines
            for prior_number, prior_text, prior_truncated in before:
                if len(output) >= max_lines:
                    break
                add_line(prior_number, prior_text, prior_truncated)
        elif after and len(output) < max_lines:
            if add_line(number, text, line_truncated):
                after -= 1
            else:
                more = True
                after = 0
        elif after:
            more = True
            after = 0
        before.append((number, text, line_truncated))

    lines = [output[number] for number in sorted(output)]
    for item in lines:
        if not item["line_truncated"]:
            item.pop("line_truncated")
    next_start = (last_match + 1 if literal is not None and last_match is not None
                  else max(output) + 1 if literal is None and output else last_scanned + 1)
    return {
        "path": reference,
        "start_line": start_line,
        "next_start_line": next_start,
        "lines": lines,
        "more": more,
        "truncated": incomplete or read_state.incomplete,
        "incomplete": incomplete or read_state.incomplete,
    }


def _check_log_reference(project: Path, value: Any) -> tuple[Path, str]:
    if not isinstance(value, str) or not value:
        raise ValueError("failed check requires a valid log reference")
    candidate = Path(value)
    if candidate.is_absolute():
        root = _project_root(project)
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("failed check log is outside the project") from exc
    else:
        relative = value
    return _safe_retained_log(project, relative)


def _repair_details(path: Path, values: Sequence[str], root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    selected: list[tuple[int, dict[str, Any]]] = []
    tail: deque[dict[str, Any]] = deque(maxlen=4)
    incomplete = False
    read_state = _ReadState()
    for number, text, line_truncated in _iter_redacted_lines(
            path, values, max_chars=700, root=root, state=read_state):
        item = {"line": number, "text": text}
        if line_truncated:
            item["line_truncated"] = True
        if line_truncated or _TRUNCATION_TEXT in text:
            incomplete = True
        lower=text.lower()
        score=4 if _EXCEPTION_LINE.search(text) else 3 if 'traceback' in lower else 2 if lower.startswith(('error','critical','fatal')) else 1 if any(word in lower for word in _DIAGNOSTIC_WORDS) and 'error_count=0' not in lower else 0
        if score:
            selected.append((score,item))
            selected.sort(key=lambda entry:(-entry[0],entry[1]['line']))
            del selected[8:]
        tail.append(item)
    diagnostics=sorted((entry[1] for entry in selected),key=lambda item:item['line'])
    return diagnostics, list(tail), incomplete or read_state.incomplete


def _compact(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def build_repair_packet(project: Path, checks: list[dict], *, max_chars: int = 2400) -> str:
    """Build compact, untrusted diagnostic evidence for a retry without mutation."""
    if not isinstance(checks, list):
        raise ValueError("checks must be a list")
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    values = _redaction_values()
    root = _project_root(project)
    failures: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("each check must be an object")
        returncode = check.get("returncode")
        if type(returncode) is not int and returncode is not None:
            raise ValueError("check returncode must be an integer or null")
        if returncode == 0:
            continue
        argv, cause = check.get("argv"), check.get("failure_cause")
        if (not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv)
                or not isinstance(cause, str) or not cause):
            raise ValueError("failed check requires actual argv and failure_cause")
        path, reference = _check_log_reference(project, check.get("log"))
        if redacted_text(reference, values) != reference:
            raise ValueError("secret log paths are refused")
        diagnostics, tail, incomplete = _repair_details(path, values, root)
        failures.append({"argv": [redacted_text(part, values) for part in argv],
                         "returncode": returncode,
                         "failure_cause": redacted_text(cause, values),
                         "log": reference, "diagnostics": diagnostics, "tail": tail,
                         "output_incomplete": incomplete})

    packet: dict[str, Any] = {
        "schema": 1,
        "untrusted": True,
        "original_criteria_remain_binding": True,
        "limited": False,
        "failures": [],
    }
    if len(_compact(packet)) > max_chars:
        raise ValueError("max_chars cannot fit required repair-packet constraints")
    for index, failure in enumerate(failures):
        candidate = {**packet, "failures": [*packet["failures"], failure]}
        if len(_compact(candidate)) <= max_chars:
            packet = candidate
            continue
        limited = {**failure, "diagnostics": [], "tail": [], "detail_limited": True}
        candidate = {**packet, "limited": True, "failures": [*packet["failures"], limited]}
        if len(_compact(candidate)) <= max_chars:
            packet = candidate
            continue
        # Keeping argv, returncode, failure_cause, and a verified log reference is
        # mandatory; do not silently shorten or omit those facts.
        if not packet["failures"]:
            raise ValueError("max_chars cannot fit required failed-check metadata")
        candidate = {**packet, "limited": True, "omitted_failures": len(failures) - index}
        if len(_compact(candidate)) > max_chars:
            raise ValueError("max_chars cannot report the bounded repair packet")
        packet = candidate
        break
    return _compact(packet)
