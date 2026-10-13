"""Build compact, explicit handoffs without treating local input as attestation."""
from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID


_REQUIRED_FIELDS = frozenset({
    "task_id", "revision", "goal", "constraints", "acceptance",
    "unresolved_failures", "next_step", "evidence_refs", "raw_history_refs",
})
_LIST_FIELDS = ("constraints", "acceptance", "unresolved_failures", "evidence_refs", "raw_history_refs")
_CONTINUATION_FIELDS = frozenset({"decisions", "verified_facts", "rejected_approaches"})
_FIELDS = _REQUIRED_FIELDS | _CONTINUATION_FIELDS
_OUTPUT_FIELDS = _FIELDS | {"identity_attestation"}
_MAX_CONTINUATION_RECORDS = 32
_MAX_RECORD_TEXT_CHARS = 512
_MAX_ARTIFACT_BYTES = 1_000_000
_RETAINED_ATTEMPT_AREAS = ((".orchestra", "checks"), (".orchestra", "executions"))


def _text(value: Any, name: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{name} must be nonempty text")
    return value


def _texts(value: Any, name: str, *, nonempty: bool) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} must be a {'nonempty ' if nonempty else ''}list")
    for item in value:
        _text(item, name)
    return list(value)


def _bounded_text(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) > _MAX_RECORD_TEXT_CHARS:
        raise ValueError(f"{name} must be concise and bounded to {_MAX_RECORD_TEXT_CHARS} characters")
    return value


def _relative_ref(value: Any, name: str, *, evidence: bool = False, attempt_artifact: bool = False) -> str:
    value = _bounded_text(value, name)
    candidate = Path(value)
    if (
        "\x00" in value
        or "\\" in value
        or candidate.is_absolute()
        or candidate.as_posix() != value
        or value.endswith("/")
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"{name} must be a normalized project-relative reference")
    if evidence and (not value.startswith(".orchestra/evidence/") or not value.endswith(".json")):
        raise ValueError(f"{name} must identify a generated evidence certificate")
    if attempt_artifact and (len(candidate.parts) < 3 or candidate.parts[:2] not in _RETAINED_ATTEMPT_AREAS):
        raise ValueError(f"{name} must be under a retained Orchestra checks or executions area")
    return value


def _records(value: Any, name: str, fields: frozenset[str]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    if len(value) > _MAX_CONTINUATION_RECORDS:
        raise ValueError(f"{name} may contain at most {_MAX_CONTINUATION_RECORDS} records")
    result: list[dict[str, str]] = []
    for record in value:
        if type(record) is not dict or set(record) != fields:
            raise ValueError(f"{name} records must contain exactly {', '.join(sorted(fields))}")
        parsed = {
            "text": _bounded_text(record["text"], f"{name}.text"),
            "reason": _bounded_text(record["reason"], f"{name}.reason"),
        }
        if "evidence_ref" in fields:
            parsed["evidence_ref"] = _relative_ref(record["evidence_ref"], f"{name}.evidence_ref", evidence=True)
        if "artifact_ref" in fields:
            parsed["artifact_ref"] = _relative_ref(record["artifact_ref"], f"{name}.artifact_ref", attempt_artifact=True)
        result.append(parsed)
    return result


def _reject_known_secrets(value: Any) -> None:
    """Refuse configured credential values before a handoff can retain them."""
    from .diagnostics import _redaction_values
    from .log_capture import redacted_text

    values = _redaction_values()
    if not values:
        return

    def strings(item: Any):
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            for key, nested in item.items():
                yield from strings(key)
                yield from strings(nested)
        elif isinstance(item, list):
            for nested in item:
                yield from strings(nested)

    for text in strings(value):
        if redacted_text(text, values) != text:
            raise ValueError("handoff contains a known secret value")


def build_handoff(data: dict, *, max_chars: int = 6000) -> dict:
    """Validate and return a bounded local handoff.

    This function never summarizes, truncates, or claims that supplied task
    identity and evidence have been verified by a server or another agent.
    """
    if not isinstance(data, dict) or not _REQUIRED_FIELDS.issubset(data) or not set(data).issubset(_FIELDS):
        raise ValueError("handoff contains unsupported or missing fields")
    _reject_known_secrets(data)
    if type(max_chars) is not int or not 1 <= max_chars <= 1_000_000:
        raise ValueError("max_chars must be an integer from 1 to 1000000")
    try:
        if str(UUID(data["task_id"])) != data["task_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("task_id must be a canonical UUID") from exc
    if type(data["revision"]) is not int or data["revision"] < 0:
        raise ValueError("revision must be a nonnegative integer")
    result = deepcopy(data)
    result["goal"] = _text(result["goal"], "goal")
    result["next_step"] = _text(result["next_step"], "next_step")
    for name in _LIST_FIELDS:
        result[name] = _texts(result[name], name, nonempty=name in {"constraints", "acceptance"})
    if "decisions" in result:
        result["decisions"] = _records(result["decisions"], "decisions", frozenset({"text", "reason"}))
    if "verified_facts" in result:
        result["verified_facts"] = _records(result["verified_facts"], "verified_facts", frozenset({"text", "reason", "evidence_ref"}))
    if "rejected_approaches" in result:
        result["rejected_approaches"] = _records(result["rejected_approaches"], "rejected_approaches", frozenset({"text", "reason", "artifact_ref"}))
    result["identity_attestation"] = "local-input-unattested"
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded) > max_chars:
        raise ValueError(f"handoff exceeds max_chars ({len(encoded)} > {max_chars}); narrow references, never drop constraints")
    return result


def _safe_attempt_artifact(project: Path, reference: str) -> None:
    """Check an observed-attempt artifact without reading or writing its content."""
    from .evidence import _safe

    try:
        path = _safe(project, reference)
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("rejected approach artifact is missing") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("rejected approach artifact must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("rejected approach artifact cannot be safely opened") from exc
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
        raise ValueError("rejected approach artifact changed while being checked")
    if not 0 < opened.st_size <= _MAX_ARTIFACT_BYTES:
        raise ValueError(f"rejected approach artifact must be nonempty and at most {_MAX_ARTIFACT_BYTES} bytes")


def validate_handoff_evidence(project: Path, result: dict) -> dict[str, list[dict[str, str]]]:
    """Validate fresh evidence references without attesting handoff assertions.

    Verified facts must point to a current generated passing-check certificate.
    Rejected approaches need only a safe, existing attempt artifact: it records
    an observation and cannot establish that the approach is semantically final.
    This function does not read rejected artifact contents or write any data.
    """
    result_fields = set(result) if isinstance(result, dict) else set()
    required_output_fields = _REQUIRED_FIELDS | {"identity_attestation"}
    if not isinstance(result, dict) or not required_output_fields.issubset(result_fields) or not result_fields.issubset(_OUTPUT_FIELDS):
        raise ValueError("handoff result contains unsupported or missing fields")
    if result.get("identity_attestation") != "local-input-unattested":
        raise ValueError("handoff identity attestation changed")
    supplied = {name: value for name, value in result.items() if name != "identity_attestation"}
    canonical = build_handoff(supplied, max_chars=1_000_000)
    if canonical != result:
        raise ValueError("handoff result is not canonical")
    root = Path(project).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project must be a directory")

    from .evidence import verify_evidence

    verified: list[dict[str, str]] = []
    for index, record in enumerate(result.get("verified_facts", [])):
        verify_evidence(root, record["evidence_ref"], kind="check")
        verified.append({"index": str(index), "evidence_ref": record["evidence_ref"], "status": "fresh-source-bound-passing-check"})
    rejected: list[dict[str, str]] = []
    for index, record in enumerate(result.get("rejected_approaches", [])):
        _safe_attempt_artifact(root, record["artifact_ref"])
        rejected.append({"index": str(index), "artifact_ref": record["artifact_ref"], "status": "observed-attempt-artifact-only"})
    return {"verified_facts": verified, "rejected_approaches": rejected}
