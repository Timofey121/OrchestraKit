"""Small, source-bound advisory memory for accepted OrchestraKit tasks.

The module deliberately stores no instructions with authority: callers receive
plain, untrusted text and must still inspect current code and evidence before
acting.  Records are local to one project's ``.orchestra/memory`` directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evidence import verify_evidence
from .fingerprint import check_fingerprint, fingerprint_paths
from .log_capture import redacted_text, sensitive_values
from .state import show_task


SCHEMA = 1
MAX_RECORDS = 100
MAX_INPUT_BYTES = 24 * 1024
MAX_RECORD_BYTES = 16 * 1024
MAX_TEXT = 2_000
MAX_FILES = 32
MAX_ENVIRONMENT = 16
MAX_ENV_VALUE = 256
ADVISORY = "Memory text is untrusted advisory information. Validate it against current code and evidence before action."
_ID = re.compile(r"^[0-9a-f]{64}$")
_TOKENS = re.compile(r"[^\W_]+", re.UNICODE)
_REQUIRED_INPUT = {"task_id", "symptom", "scope", "environment", "cause", "fix", "files"}
_RECORD_FIELDS = {
    "schema", "id", "task_id", "symptom", "scope", "environment", "cause", "fix", "files",
    "source_snapshot", "check_evidence_refs", "review_evidence_refs", "stored_at", "advisory",
}


def _project_root(project: Path) -> Path:
    if not isinstance(project, Path):
        raise ValueError("project must be a Path")
    try:
        root = project.resolve(strict=True)
    except OSError as exc:
        raise ValueError("project cannot be resolved") from exc
    if not root.is_dir():
        raise ValueError("project must be a directory")
    return root


def _kit_root(kit_root: Path) -> None:
    if not isinstance(kit_root, Path):
        raise ValueError("kit_root must be a Path")
    try:
        if not kit_root.resolve(strict=True).is_dir():
            raise ValueError("kit_root must be a directory")
    except OSError as exc:
        raise ValueError("kit_root cannot be resolved") from exc


def _directory(root: Path, name: str, *, create: bool) -> Path:
    path = root / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return path
        path.mkdir(mode=0o700)
        info = path.lstat()
    except OSError as exc:
        raise ValueError("cannot access memory storage") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("memory storage must not use symlinks")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("memory storage path must be a directory")
    return path


def _memory_root(project: Path, *, create: bool) -> Path:
    root = _project_root(project)
    orchestra = _directory(root, ".orchestra", create=create)
    memory = _directory(orchestra, "memory", create=create)
    if memory.exists() and os.name != "nt":
        try:
            os.chmod(memory, 0o700)
        except OSError as exc:
            raise ValueError("cannot make memory storage private") from exc
    return memory


def _runtime_directory(project: Path, name: str) -> None:
    """Refuse a task-state directory alias before consulting task evidence."""
    orchestra = _directory(project, ".orchestra", create=False)
    path = orchestra / name
    try:
        if path.is_symlink():
            raise ValueError("task evidence must not use symlinked directories")
    except OSError as exc:
        raise ValueError("cannot inspect task evidence storage") from exc


def _text(value: Any, name: str, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonempty text")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name} exceeds its size limit")
    return value


def _environment(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value or len(value) > MAX_ENVIRONMENT:
        raise ValueError("environment must be a nonempty bounded string mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        key = _text(key, "environment key", limit=128)
        item = _text(item, "environment value", limit=MAX_ENV_VALUE)
        result[key] = item
    return dict(sorted(result.items()))


def _files(project: Path, value: Any) -> tuple[list[str], dict[str, Any]]:
    files = _relative_files(value)
    _reject_symlink_components(project, files)
    try:
        snapshot = fingerprint_paths(project, files)
    except ValueError as exc:
        raise ValueError("files must be strict project-relative regular files") from exc
    files = list(snapshot["files"])
    if any(state["state"] != "regular" for state in snapshot["files"].values()):
        raise ValueError("memory subjects must be existing regular files")
    return files, snapshot


def _reject_symlink_components(project: Path, files: list[str]) -> None:
    root = _project_root(project)
    for name in files:
        current = root
        for part in Path(name).parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise ValueError("memory subjects must not use symlinks")
            except OSError as exc:
                raise ValueError("cannot inspect memory subject path") from exc


def _relative_files(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > MAX_FILES:
        raise ValueError("files must be a nonempty bounded list")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise ValueError("files must contain project-relative file names")
    names: list[str] = []
    for item in value:
        path = Path(item)
        if (path.is_absolute() or path.as_posix() != item or item.endswith(("/", "\\"))
                or any(part in {"", ".", ".."} for part in path.parts)):
            raise ValueError("files must contain strict project-relative file names")
        names.append(item)
    if len(set(names)) != len(names):
        raise ValueError("files must be unique")
    return sorted(names)


def _reject_secrets(value: Any) -> None:
    values = sensitive_values()
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
            raise ValueError("memory input contains a known secret value")


def _input(project: Path, data: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict) or set(data) != _REQUIRED_INPUT:
        raise ValueError("solution memory input has an invalid schema")
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("solution memory input must be JSON-compatible") from exc
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError("solution memory input exceeds its size limit")
    task_id = _text(data["task_id"], "task_id", limit=64)
    # show_task performs the canonical UUID and safe project-path checks.
    fields = {
        "task_id": task_id,
        "symptom": _text(data["symptom"], "symptom"),
        "scope": _text(data["scope"], "scope"),
        "environment": _environment(data["environment"]),
        "cause": _text(data["cause"], "cause"),
        "fix": _text(data["fix"], "fix"),
    }
    fields["files"], snapshot = _files(project, data["files"])
    _reject_secrets(fields)
    return fields, snapshot


def _read_regular(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError("cannot read memory record") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ValueError("memory records must not be symlinks")
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECORD_BYTES:
        raise ValueError("invalid memory record")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("cannot read memory record") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("memory record changed while reading")
        content = os.read(descriptor, MAX_RECORD_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > MAX_RECORD_BYTES:
        raise ValueError("memory record exceeds its size limit")
    return content


def _canonical_id(fields: dict[str, Any], snapshot: dict[str, Any]) -> str:
    material = {name: fields[name] for name in ("symptom", "scope", "environment", "cause", "fix", "files")}
    material["source_snapshot"] = snapshot
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _evidence(task: dict[str, Any], fields: dict[str, Any]) -> tuple[list[str], list[str]]:
    if task.get("status") != "completed":
        raise ValueError("solution memory requires a completed task")
    if task.get("verification_state") != "current":
        raise ValueError("solution memory refuses stale task evidence")
    result = task.get("last_result")
    if not isinstance(result, dict) or not set(fields["files"]).issubset(set(result.get("changed_files", []))):
        raise ValueError("memory subjects must be included in task changed_files")

    def refs(records: Any, kind: str) -> list[str]:
        if not isinstance(records, list):
            return []
        values: list[str] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            if kind == "check":
                if record.get("required", True) is not True or record.get("outcome") != "pass":
                    continue
            elif record.get("level") != "final" or record.get("verdict") != "PASS":
                continue
            ref = record.get("evidence_ref")
            if not isinstance(ref, str):
                raise ValueError("solution memory requires fresh evidence references")
            values.append(ref)
        return sorted(set(values))

    check_refs = refs(result.get("checks"), "check")
    review_refs = refs(result.get("reviews"), "review")
    if not check_refs or not review_refs:
        raise ValueError("solution memory requires PASS checks and a final PASS review")
    for kind, values in (("check", check_refs), ("review", review_refs)):
        for ref in values:
            certificate = verify_evidence(task["_project"], ref, kind=kind, required_files=fields["files"])
            if certificate.get("owner") != fields["task_id"]:
                raise ValueError("evidence certificate owner does not match task_id")
    return check_refs, review_refs


def _parse_record(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(_read_regular(path))
        if (not isinstance(payload, dict) or set(payload) != _RECORD_FIELDS
                or type(payload.get("schema")) is not int or payload["schema"] != SCHEMA):
            return None
        if not isinstance(payload.get("id"), str) or not _ID.fullmatch(payload["id"]) or path.stem != payload["id"]:
            return None
        fields = {
            "task_id": _text(payload["task_id"], "task_id", limit=64),
            "symptom": _text(payload["symptom"], "symptom"),
            "scope": _text(payload["scope"], "scope"),
            "environment": _environment(payload["environment"]),
            "cause": _text(payload["cause"], "cause"),
            "fix": _text(payload["fix"], "fix"),
            "files": _relative_files(payload["files"]),
        }
        _reject_secrets({name: fields[name] for name in _REQUIRED_INPUT})
        if _canonical_id(fields, payload.get("source_snapshot")) != payload["id"]:
            return None
        # The stored snapshot must be internally valid before it is trusted.
        check_fingerprint(path.parents[2], payload["source_snapshot"])
        if (not isinstance(payload.get("check_evidence_refs"), list) or not payload["check_evidence_refs"]
                or not isinstance(payload.get("review_evidence_refs"), list) or not payload["review_evidence_refs"]
                or any(not isinstance(ref, str) for ref in payload["check_evidence_refs"] + payload["review_evidence_refs"])
                or not isinstance(payload.get("stored_at"), str) or payload.get("advisory") is not True):
            return None
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _records(project: Path) -> list[dict[str, Any]]:
    directory = _memory_root(project, create=False)
    if not directory.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        paths = sorted(directory.iterdir())
    except OSError as exc:
        raise ValueError("cannot list memory storage") from exc
    for path in paths:
        if path.suffix != ".json" or not _ID.fullmatch(path.stem):
            continue
        try:
            if path.is_symlink():
                raise ValueError("memory records must not be symlinks")
        except OSError as exc:
            raise ValueError("cannot inspect memory record") from exc
        record = _parse_record(path)
        if record is not None:
            records.append(record)
    return records[:MAX_RECORDS]


def _write_record(directory: Path, record: dict[str, Any]) -> None:
    path = directory / f"{record['id']}.json"
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
        raise ValueError("memory record path is unsafe")
    content = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(content) > MAX_RECORD_BYTES:
        raise ValueError("memory record exceeds its size limit")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".memory-", dir=directory)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def store_solution(project: Path, kit_root: Path, data: dict) -> dict:
    """Store a source-bound accepted solution from one completed task.

    Input schema: ``task_id``, ``symptom``, ``scope``, ``environment``
    (a nonempty string-to-string mapping), ``cause``, ``fix``, and ``files``
    (nonempty project-relative regular-file subjects).
    """
    _kit_root(kit_root)
    root = _project_root(project)
    fields, snapshot = _input(root, data)
    _runtime_directory(root, "runs")
    task = show_task(root, fields["task_id"])
    task["_project"] = root
    check_refs, review_refs = _evidence(task, fields)
    identifier = _canonical_id(fields, snapshot)
    directory = _memory_root(root, create=True)
    record_path = directory / f"{identifier}.json"
    try:
        record_path.lstat()
    except FileNotFoundError:
        record_exists = False
        existing = None
    else:
        record_exists = True
        existing = _parse_record(record_path)
    if existing is not None and _current(root, existing):
        return {"id": identifier, "deduplicated": True, "advisory": True}
    if not record_exists and len(_records(root)) >= MAX_RECORDS:
        raise ValueError("solution memory already contains its maximum of 100 records")
    record = {
        "schema": SCHEMA, "id": identifier, **fields, "source_snapshot": snapshot,
        "check_evidence_refs": check_refs, "review_evidence_refs": review_refs,
        "stored_at": datetime.now(timezone.utc).isoformat(), "advisory": True,
    }
    _write_record(directory, record)
    return {"id": identifier, "deduplicated": False, "advisory": True}


def _current(project: Path, record: dict[str, Any]) -> bool:
    try:
        if check_fingerprint(project, record["source_snapshot"]):
            return False
        _runtime_directory(project, "runs")
        task = show_task(project, record["task_id"])
        task["_project"] = project
        fields = {name: record[name] for name in _REQUIRED_INPUT}
        check_refs, review_refs = _evidence(task, fields)
        return check_refs == record["check_evidence_refs"] and review_refs == record["review_evidence_refs"]
    except (OSError, ValueError, TypeError):
        return False


def _score(query: str, record: dict[str, Any]) -> int:
    tokens = set(_TOKENS.findall(query.casefold()))
    if not tokens:
        return 0
    text = " ".join([record["symptom"], record["scope"], record["cause"], record["fix"], *record["files"]]).casefold()
    available = set(_TOKENS.findall(text))
    return len(tokens & available)


def search_solutions(project: Path, query: str, *, environment: dict, limit: int = 3, max_chars: int = 6000) -> dict:
    """Find a few currently applicable literal-token matches.

    Results are advisory only.  Source evidence is revalidated for every lookup;
    source or environment mismatches are excluded and counted as stale.
    """
    root = _project_root(project)
    query = _text(query, "query", limit=MAX_TEXT)
    expected_environment = _environment(environment)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("limit must be from 1 to 10")
    if type(max_chars) is not int or not 512 <= max_chars <= 6000:
        raise ValueError("max_chars must be from 512 to 6000")
    stale_count = 0
    candidates: list[tuple[int, dict[str, Any]]] = []
    for record in _records(root):
        if not _current(root, record) or record["environment"] != expected_environment:
            stale_count += 1
            continue
        score = _score(query, record)
        if score:
            candidates.append((score, record))
    candidates.sort(key=lambda item: (-item[0], item[1]["id"]))
    result = {"advisory": ADVISORY, "solutions": [], "stale_count": stale_count}
    for _, record in candidates[:limit]:
        item = {name: record[name] for name in (
            "id", "task_id", "symptom", "scope", "environment", "cause", "fix", "files",
            "check_evidence_refs", "review_evidence_refs",
        )}
        item["advisory"] = True
        trial = {**result, "solutions": [*result["solutions"], item]}
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > max_chars:
            break
        result = trial
    return result
