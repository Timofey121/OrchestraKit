"""Root-mediated, local advisory questions for a running OrchestraKit task.

The worker-facing request is a stored artifact, never an instruction to launch
an explorer.  A root caller decides whether and how to ask an explorer, then
records one bounded answer.  ``leaf_id`` and the root caller are local input
assertions only; this module has no authentication, callbacks, model calls, or
authority to run checks.

Lock files fail closed: a root must inspect an abandoned ``*.lock`` before
removing it manually.  The protocol never guesses whether a writer is dead.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

from .config import load_config
from .evidence import verify_evidence
from .fingerprint import check_fingerprint, fingerprint_paths
from .handoff import _reject_known_secrets
from .state import show_task


SCHEMA = 1
MAX_RECORDS = 100
MAX_RECORD_BYTES = 6_000
MAX_FILES = 16
MAX_SOURCE_BYTES = 1_000_000
MAX_ENVIRONMENT = 16
MAX_ENV_KEY = 128
MAX_ENV_VALUE = 256
MAX_QUESTION = 2_000
MAX_ANSWER = 2_000
MAX_EVIDENCE_REFS = 16
MAX_REFERENCE = 512
_REQUEST_FIELDS = frozenset({"task_id", "revision", "leaf_id", "question", "files", "environment"})
_ANSWER_FIELDS = frozenset({"answer", "evidence_refs", "environment"})
_RECORD_FIELDS = frozenset({
    "schema", "question_id", "project_identity", "task_id", "revision", "leaf_id", "question",
    "files", "environment", "task_goal", "source_snapshot", "config_snapshot", "created_at",
    "status", "answer_data", "authority",
})
_STATUSES = frozenset({"pending", "answered"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(project: Path) -> Path:
    if not isinstance(project, Path):
        raise ValueError("project must be a Path")
    try:
        root = project.resolve(strict=True)
    except OSError as exc:
        raise ValueError("project cannot be resolved") from exc
    if not root.is_dir():
        raise ValueError("project must be a directory")
    return root


def _kit(kit: Path) -> Path:
    if not isinstance(kit, Path):
        raise ValueError("kit must be a Path")
    try:
        root = kit.resolve(strict=True)
    except OSError as exc:
        raise ValueError("kit cannot be resolved") from exc
    if not root.is_dir():
        raise ValueError("kit must be a directory")
    return root


def _identity(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()


def _directory(parent: Path, name: str, *, create: bool, label: str) -> Path:
    path = parent / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return path
        try:
            path.mkdir(mode=0o700)
            info = path.lstat()
        except OSError as exc:
            raise ValueError(f"cannot create {label}") from exc
    except OSError as exc:
        raise ValueError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{label} must not use symlinks")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} must be a directory")
    return path


def _store(root: Path, *, create: bool) -> Path:
    orchestra = _directory(root, ".orchestra", create=create, label="Orchestra runtime")
    questions = _directory(orchestra, "questions", create=create, label="question storage")
    if create and os.name != "nt":
        try:
            os.chmod(questions, 0o700)
        except OSError as exc:
            raise ValueError("cannot make question storage private") from exc
    return questions


def _uuid(value: Any, name: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{name} must be a canonical UUID") from exc
    return value


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > limit:
        raise ValueError(f"{name} must be bounded nonempty text")
    return value.strip()


def _environment(value: Any) -> dict[str, str]:
    if type(value) is not dict or not value or len(value) > MAX_ENVIRONMENT:
        raise ValueError("environment must be a nonempty bounded explicit mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("environment keys must be text")
        clean_key = _text(key, "environment key", MAX_ENV_KEY)
        clean_value = _text(item, "environment value", MAX_ENV_VALUE)
        if clean_key in result:
            raise ValueError("environment keys must be unique")
        result[clean_key] = clean_value
    return dict(sorted(result.items()))


def _files(root: Path, value: Any) -> tuple[list[str], dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_FILES:
        raise ValueError(f"files must be a nonempty list of at most {MAX_FILES} paths")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item or len(item) > MAX_REFERENCE:
            raise ValueError("files must be bounded strict project-relative file paths")
        candidate = Path(item)
        if (candidate.is_absolute() or candidate.as_posix() != item or item.endswith(("/", "\\"))
                or any(part in {"", ".", ".."} for part in candidate.parts)):
            raise ValueError("files must be strict project-relative file paths")
        names.append(item)
    if len(set(names)) != len(names):
        raise ValueError("files must be unique")
    names.sort()
    for name in names:
        current = root
        for part in Path(name).parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ValueError("files cannot be safely inspected") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("files must not contain symlinks")
    try:
        snapshot = fingerprint_paths(root, names)
    except ValueError as exc:
        raise ValueError("files must be safe regular project files") from exc
    for name in names:
        path = root / name
        try:
            info = path.lstat()
        except OSError as exc:
            raise ValueError("files must be existing regular project files") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            raise ValueError("files must be existing regular files no larger than 1 MiB")
        if snapshot["files"][name].get("state") != "regular":
            raise ValueError("files must be existing regular project files")
    return names, snapshot


def _config_snapshot(root: Path) -> dict[str, Any]:
    """Pin the policy file without allowing a symlinked runtime path."""
    relative = ".orchestra/project.toml"
    current = root
    for part in Path(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError("configuration cannot be safely inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("configuration must not use symlinks")
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
        raise ValueError("configuration must be a regular file no larger than 1 MiB")
    try:
        return fingerprint_paths(root, [relative])
    except ValueError as exc:
        raise ValueError("configuration cannot be safely fingerprinted") from exc


def _request(root: Path, data: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if type(data) is not dict or set(data) != _REQUEST_FIELDS:
        raise ValueError("question request has unsupported or missing fields")
    _reject_known_secrets(data)
    task_id = _uuid(data["task_id"], "task_id")
    leaf_id = _uuid(data["leaf_id"], "leaf_id")
    if type(data["revision"]) is not int or data["revision"] < 0:
        raise ValueError("revision must be a nonnegative integer")
    files, snapshot = _files(root, data["files"])
    return {
        "task_id": task_id, "revision": data["revision"], "leaf_id": leaf_id,
        "question": _text(data["question"], "question", MAX_QUESTION), "files": files,
        "environment": _environment(data["environment"]),
    }, snapshot


def _evidence_refs(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_REFS:
        raise ValueError(f"evidence_refs must be a list of at most {MAX_EVIDENCE_REFS} references")
    refs: list[str] = []
    for item in value:
        reference = _text(item, "evidence_ref", MAX_REFERENCE)
        path = Path(reference)
        if (not reference.startswith(".orchestra/evidence/") or not reference.endswith(".json")
                or path.is_absolute() or path.as_posix() != reference
                or any(part in {"", ".", ".."} for part in path.parts)):
            raise ValueError("evidence_refs must identify generated evidence certificates")
        refs.append(reference)
    if len(set(refs)) != len(refs):
        raise ValueError("evidence_refs must be unique")
    return refs


def _answer(data: Any) -> dict[str, Any]:
    if type(data) is not dict or set(data) != _ANSWER_FIELDS:
        raise ValueError("question answer has unsupported or missing fields")
    _reject_known_secrets(data)
    return {
        "answer": _text(data["answer"], "answer", MAX_ANSWER),
        "evidence_refs": _evidence_refs(data["evidence_refs"]),
        "environment": _environment(data["environment"]),
    }


def _path(directory: Path, question_id: Any) -> Path:
    return directory / f"{_uuid(question_id, 'question_id')}.json"


def _read_record(root: Path, directory: Path, question_id: str) -> dict[str, Any]:
    path = _path(directory, question_id)
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("question record is missing") from exc
    except OSError as exc:
        raise ValueError("question record cannot be read") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECORD_BYTES:
        raise ValueError("question record is corrupt or unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("question record is corrupt or unsafe")
            content = os.read(descriptor, MAX_RECORD_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError("question record cannot be read") from exc
    if len(content) > MAX_RECORD_BYTES:
        raise ValueError("question record is corrupt")
    try:
        record = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("question record is corrupt") from exc
    return _record(root, record, question_id)


def _record(root: Path, value: Any, question_id: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _RECORD_FIELDS:
        raise ValueError("question record is corrupt")
    if value.get("schema") != SCHEMA or value.get("question_id") != question_id:
        raise ValueError("question record is corrupt")
    try:
        _reject_known_secrets(value)
    except ValueError as exc:
        raise ValueError("question record is corrupt") from exc
    _uuid(value.get("task_id"), "task_id")
    _uuid(value.get("leaf_id"), "leaf_id")
    if type(value.get("revision")) is not int or value["revision"] < 0:
        raise ValueError("question record is corrupt")
    if not isinstance(value.get("project_identity"), str) or len(value["project_identity"]) != 64:
        raise ValueError("question record is corrupt")
    if not isinstance(value.get("files"), list) or not value["files"] or len(value["files"]) > MAX_FILES:
        raise ValueError("question record is corrupt")
    if value["files"] != sorted(value["files"]) or len(set(value["files"])) != len(value["files"]):
        raise ValueError("question record is corrupt")
    for item in value["files"]:
        if not isinstance(item, str):
            raise ValueError("question record is corrupt")
    _text(value.get("question"), "question", MAX_QUESTION)
    _text(value.get("task_goal"), "task_goal", MAX_QUESTION)
    if not isinstance(value.get("created_at"), str) or not value["created_at"]:
        raise ValueError("question record is corrupt")
    if value.get("authority") != "local-caller-asserted-advisory" or value.get("status") not in _STATUSES:
        raise ValueError("question record is corrupt")
    canonical_environment = _environment(value.get("environment"))
    if canonical_environment != value["environment"]:
        raise ValueError("question record is corrupt")
    try:
        check_fingerprint(root, value["source_snapshot"])
        check_fingerprint(root, value["config_snapshot"])
    except ValueError as exc:
        raise ValueError("question record is corrupt") from exc
    source_files = value["source_snapshot"].get("files")
    config_files = value["config_snapshot"].get("files")
    if not isinstance(source_files, dict) or set(source_files) != set(value["files"]):
        raise ValueError("question record is corrupt")
    if not isinstance(config_files, dict) or set(config_files) != {".orchestra/project.toml"}:
        raise ValueError("question record is corrupt")
    if value["status"] == "pending":
        if value.get("answer_data") is not None:
            raise ValueError("question record is corrupt")
    else:
        canonical_answer = _answer(value.get("answer_data"))
        if canonical_answer != value["answer_data"]:
            raise ValueError("question record is corrupt")
    return value


def _encoded(record: dict[str, Any]) -> bytes:
    try:
        content = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("question record cannot be encoded") from exc
    if len(content) > MAX_RECORD_BYTES:
        raise ValueError("question record exceeds the 6000 byte limit")
    return content


def _count(directory: Path) -> int:
    try:
        paths = list(directory.glob("*.json"))
    except OSError as exc:
        raise ValueError("question storage cannot be listed") from exc
    for path in paths:
        try:
            info = path.lstat()
        except OSError as exc:
            raise ValueError("question storage cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("question storage contains unsafe records")
    return len(paths)


def _write_initial(directory: Path, question_id: str, record: dict[str, Any]) -> None:
    content = _encoded(record)
    destination = _path(directory, question_id)
    temporary = directory / f".{question_id}.{uuid4()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            _write_bytes(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise ValueError("question record already exists") from exc
    except OSError as exc:
        raise ValueError("question record cannot be written") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_replace(directory: Path, question_id: str, record: dict[str, Any]) -> None:
    content = _encoded(record)
    destination = _path(directory, question_id)
    temporary = directory / f".{question_id}.{uuid4()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            _write_bytes(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        if os.name != "nt":
            os.chmod(destination, 0o600)
    except OSError as exc:
        raise ValueError("question record cannot be written") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_bytes(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("could not write question record")
        remaining = remaining[written:]


@contextmanager
def _locked(directory: Path, question_id: str) -> Iterator[None]:
    lock = directory / f"{_uuid(question_id, 'question_id')}.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(lock, flags, 0o600)
    except FileExistsError as exc:
        raise ValueError("question is locked by another writer; retry after re-reading") from exc
    except OSError as exc:
        raise ValueError("question lock cannot be created") from exc
    try:
        os.close(descriptor)
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _admission_locked(directory: Path) -> Iterator[None]:
    """Serialize capacity counting and initial record creation across UUIDs."""
    lock = directory / "admission.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(lock, flags, 0o600)
    except FileExistsError as exc:
        raise ValueError("question admission is locked by another writer; retry after re-reading") from exc
    except OSError as exc:
        raise ValueError("question admission lock cannot be created") from exc
    try:
        os.close(descriptor)
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _task(root: Path, task_id: str, revision: int, task_goal: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    try:
        task = show_task(root, task_id)
    except ValueError:
        return None, "task is unknown in this project"
    if task.get("status") == "completed":
        return task, "task is completed"
    if task.get("revision") != revision:
        return task, "task revision changed"
    if task_goal is not None and task.get("goal") != task_goal:
        return task, "task goal changed"
    return task, None


def _freshness(root: Path, record: dict[str, Any], *, environment: dict[str, str]) -> list[str]:
    reasons: list[str] = []
    if record["project_identity"] != _identity(root):
        reasons.append("record belongs to another project")
    _, task_reason = _task(root, record["task_id"], record["revision"], record["task_goal"])
    if task_reason:
        reasons.append(task_reason)
    if environment != record["environment"]:
        reasons.append("environment changed")
    try:
        config_changed = check_fingerprint(root, record["config_snapshot"])
        source_changed = check_fingerprint(root, record["source_snapshot"])
    except ValueError as exc:
        raise ValueError("question record is corrupt") from exc
    if config_changed:
        reasons.append("configuration changed")
    if source_changed:
        reasons.append("source changed")
    if record["status"] == "answered":
        for reference in record["answer_data"]["evidence_refs"]:
            try:
                verify_evidence(root, reference, kind="check", required_files=record["files"])
            except ValueError:
                reasons.append("answer evidence is stale or unavailable")
                break
    return reasons


def _public(record: dict[str, Any], *, status: str, reasons: list[str] = ()) -> dict[str, Any]:
    result = {
        "question_id": record["question_id"], "task_id": record["task_id"], "revision": record["revision"],
        "leaf_id": record["leaf_id"], "question": record["question"], "files": list(record["files"]),
        "environment": dict(record["environment"]), "task_goal": record["task_goal"], "status": status,
        "dispatch": "root-mediation-required", "authority": "local-caller-asserted-advisory",
        "advisory": "root-mediated-local-advisory",
    }
    if status == "answered":
        answer = record["answer_data"]
        result["answer"] = answer["answer"]
        result["evidence_refs"] = list(answer["evidence_refs"])
    if status == "stale":
        result["stale_reasons"] = list(reasons)
    return result


def ask_question(project: Path, kit: Path, data: dict) -> dict:
    """Persist one pinned worker question for a root to mediate manually."""
    root, kit_root = _root(project), _kit(kit)
    request, source_snapshot = _request(root, data)
    config_snapshot = _config_snapshot(root)
    load_config(root, kit_root)
    if check_fingerprint(root, source_snapshot) or check_fingerprint(root, config_snapshot):
        raise ValueError("question source or configuration changed during admission")
    task, reason = _task(root, request["task_id"], request["revision"])
    if reason:
        raise ValueError(f"question requires a current nonterminal task: {reason}")
    assert task is not None
    _reject_known_secrets(task["goal"])
    _text(task["goal"], "task_goal", MAX_QUESTION)
    directory = _store(root, create=True)
    question_id = str(uuid4())
    record = {
        "schema": SCHEMA, "question_id": question_id, "project_identity": _identity(root),
        **request, "task_goal": task["goal"], "source_snapshot": source_snapshot,
        "config_snapshot": config_snapshot, "created_at": _now(), "status": "pending", "answer_data": None,
        "authority": "local-caller-asserted-advisory",
    }
    with _admission_locked(directory):
        if _count(directory) >= MAX_RECORDS:
            raise ValueError("question storage capacity reached (100 records)")
        if check_fingerprint(root, source_snapshot) or check_fingerprint(root, config_snapshot):
            raise ValueError("question source or configuration changed before storage")
        _, reason = _task(root, request["task_id"], request["revision"], task["goal"])
        if reason:
            raise ValueError(f"question requires a current nonterminal task: {reason}")
        _write_initial(directory, question_id, record)
    return _public(record, status="pending")


def answer_question(project: Path, kit: Path, question_id: str, data: dict) -> dict:
    """Record one root-selected advisory answer after revalidating every pin."""
    root, kit_root = _root(project), _kit(kit)
    answer = _answer(data)
    directory = _store(root, create=False)
    if not directory.exists():
        raise ValueError("question record is missing")
    with _locked(directory, question_id):
        record = _read_record(root, directory, question_id)
        _config_snapshot(root)
        load_config(root, kit_root)
        reasons = _freshness(root, record, environment=answer["environment"])
        if reasons:
            raise ValueError("stale question cannot receive an answer: " + "; ".join(reasons))
        for reference in answer["evidence_refs"]:
            verify_evidence(root, reference, kind="check", required_files=record["files"])
        reasons = _freshness(root, record, environment=answer["environment"])
        if reasons:
            raise ValueError("stale question cannot receive an answer: " + "; ".join(reasons))
        if record["status"] == "answered":
            if record["answer_data"] != answer:
                raise ValueError("question already has a conflicting answer")
            return _public(record, status="answered")
        record["answer_data"] = answer
        record["status"] = "answered"
        _write_replace(directory, question_id, record)
    return _public(record, status="answered")


def read_question(project: Path, kit: Path, question_id: str, *, environment: dict) -> dict:
    """Return a pending or current answer; suppress answers when any pin is stale."""
    root, kit_root = _root(project), _kit(kit)
    supplied_environment = _environment(environment)
    _reject_known_secrets(supplied_environment)
    directory = _store(root, create=False)
    if not directory.exists():
        raise ValueError("question record is missing")
    record = _read_record(root, directory, question_id)
    _config_snapshot(root)
    load_config(root, kit_root)
    reasons = _freshness(root, record, environment=supplied_environment)
    if reasons:
        return _public(record, status="stale", reasons=reasons)
    return _public(record, status=record["status"])
