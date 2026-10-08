from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .config import ProjectConfig, load_config
from .fingerprint import fingerprint_paths, check_fingerprint


METRICS = ("input_tokens", "output_tokens", "elapsed_seconds", "cost_usd")
RESULT_FIELDS = {"status", "summary", "completed_steps", "next_steps", "changed_files", "checks", "acceptance", "reviews", "metrics", "failure_cause"}
MAX_TASK_BYTES = 256 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_root(project_root: Path) -> Path:
    root = project_root.resolve()
    path = root / ".orchestra/runs"
    if root not in path.resolve().parents:
        raise ValueError("task storage escapes project")
    return path


def _task_path(project_root: Path, task_id: str) -> Path:
    try:
        if str(UUID(task_id)) != task_id:
            raise ValueError("noncanonical UUID")
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("task_id must be a canonical UUID") from exc
    path = _runs_root(project_root) / f"{task_id}.json"
    if path.is_symlink():
        raise ValueError("task file must not be a symlink")
    return path


def _write(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    # Keep the current result and cumulative metrics intact. Older results are
    # a bounded tail, so long-running tasks stay readable by the dashboard.
    while len(content.encode("utf-8")) > MAX_TASK_BYTES and payload["history"]:
        payload["history"].pop(0)
        payload["history_dropped"] = payload.get("history_dropped", 0) + 1
        content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(content.encode("utf-8")) > MAX_TASK_BYTES:
        raise ValueError("task state exceeds 256 KiB; link detailed artifacts instead")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".orchestra-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read task state: {path}") from exc
    if not isinstance(payload, dict) or payload.get("generated_by") != "OrchestraKit" or payload.get("task_id") != path.stem:
        raise ValueError("invalid OrchestraKit task state")
    if type(payload.get("revision")) is not int or payload["revision"] < 0 or payload.get("status") not in {"planned", "running", "blocked", "completed"}:
        raise ValueError("invalid task revision or status")
    if not isinstance(payload.get("history"), list) or not isinstance(payload.get("metrics"), dict):
        raise ValueError("invalid task evidence history")
    return payload


def start_task(project_root: Path, kit_root: Path, goal: str, *, chat_id: str | None = None) -> dict[str, Any]:
    config = load_config(project_root, kit_root)
    if not isinstance(goal, str) or not goal.strip() or len(goal) > config.context.max_brief_chars:
        raise ValueError("goal must be nonempty and fit the brief budget")
    if chat_id is not None:
        try:
            if not isinstance(chat_id, str) or str(UUID(chat_id)) != chat_id:
                raise ValueError('noncanonical chat identity')
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError('chat_id must be a canonical UUID') from exc
    task_id = str(uuid4())
    timestamp = _now()
    payload = {
        "generated_by": "OrchestraKit", "format_version": 1,
        "task_id": task_id, "revision": 0, "goal": goal.strip(),
        "status": "planned", "created_at": timestamp, "updated_at": timestamp,
        "last_result": None, "history": [],
        "metrics": {name: None for name in METRICS},
    }
    if chat_id is not None:
        payload['chat_id'] = chat_id
    _write(_task_path(project_root, task_id), payload)
    return show_task(project_root, task_id)


def _text(value: Any, location: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be nonempty text")


def _records(data: dict[str, Any], name: str, fields: set[str], required: set[str]) -> list[dict[str, Any]]:
    items = data.get(name, [])
    if not isinstance(items, list):
        raise ValueError(f"{name} must be a list")
    for item in items:
        if not isinstance(item, dict) or set(item) - fields or required - set(item):
            raise ValueError(f"invalid {name} record")
    return items


def _validate_result(config: ProjectConfig, data: Any) -> None:
    if not isinstance(data, dict) or set(data) - RESULT_FIELDS:
        raise ValueError("unknown task result fields or invalid object")
    if data.get("status") not in {"running", "blocked", "completed"}:
        raise ValueError("result status must be running, blocked or completed")
    _text(data.get("summary"), "summary")
    for name in ("completed_steps", "next_steps", "changed_files"):
        items = data.get(name, [])
        if not isinstance(items, list):
            raise ValueError(f"{name} must be a list")
        for item in items:
            _text(item, name)
            if name == "changed_files" and (Path(item).is_absolute() or ".." in Path(item).parts):
                raise ValueError("changed_files must be project-relative paths")
    checks = _records(data, "checks", {"command", "outcome", "evidence", "required", "evidence_ref"}, {"command", "outcome", "evidence"})
    for check in checks:
        _text(check["command"], "check command")
        _text(check["evidence"], "check evidence")
        if check["outcome"] not in {"pass", "fail", "skipped"} or type(check.get("required", True)) is not bool:
            raise ValueError("invalid check outcome or required flag")
    acceptance = _records(data, "acceptance", {"criterion", "satisfied", "evidence"}, {"criterion", "satisfied", "evidence"})
    for criterion in acceptance:
        _text(criterion["criterion"], "acceptance criterion")
        _text(criterion["evidence"], "acceptance evidence")
        if type(criterion["satisfied"]) is not bool:
            raise ValueError("acceptance satisfied must be boolean")
    reviews = _records(data, "reviews", {"level", "verdict", "evidence", "evidence_ref"}, {"level", "verdict", "evidence"})
    for review in reviews:
        _text(review["evidence"], "review evidence")
        if review["level"] not in {"leaf", "work-package", "final"} or review["verdict"] not in {"PASS", "FAIL"}:
            raise ValueError("invalid review level or verdict")
    metrics = data.get("metrics", {})
    if not isinstance(metrics, dict) or set(metrics) - set(METRICS):
        raise ValueError("unknown metrics")
    for name, value in metrics.items():
        if value is None:
            continue
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError("metrics must be finite nonnegative numbers or null")
        if name.endswith("tokens") and type(value) is not int:
            raise ValueError("token counts must be integers")
    if data.get("failure_cause") not in {None, "context", "implementation", "capability", "environment"}:
        raise ValueError("unknown failure_cause")
    if len(json.dumps(data, ensure_ascii=False, allow_nan=False)) > config.context.max_result_chars:
        raise ValueError("result exceeds context budget; link detailed artifacts instead")
    if data["status"] == "completed":
        review_levels_by_ref: dict[str, str] = {}
        for review in reviews:
            ref = review.get("evidence_ref")
            if ref is not None:
                _text(ref, "review evidence_ref")
                if ref in review_levels_by_ref and review_levels_by_ref[ref] != review["level"]:
                    raise ValueError("separate review levels require separate review evidence")
                review_levels_by_ref[ref] = review["level"]
        if not checks or any(check["outcome"] != "pass" for check in checks if check.get("required", True)):
            raise ValueError("completion requires passing required checks")
        if not any(check["outcome"] == "pass" for check in checks):
            raise ValueError("completion requires at least one verified check")
        if not acceptance or any(not item["satisfied"] for item in acceptance):
            raise ValueError("completion requires satisfied acceptance criteria")
        passed = {review["level"] for review in reviews if review["verdict"] == "PASS"}
        missing = set(config.workflow.review_levels) - passed
        if missing or any(review["verdict"] == "FAIL" for review in reviews):
            raise ValueError("completion requires passing review gates: " + ", ".join(sorted(missing)))
        if data.get("next_steps") or data.get("failure_cause") is not None:
            raise ValueError("completed tasks cannot have pending steps or unresolved failures")


def record_task(project_root: Path, kit_root: Path, task_id: str, result: Any, *, expected_revision: int) -> dict[str, Any]:
    policy = fingerprint_paths(project_root,[".orchestra/project.toml"])
    config = load_config(project_root, kit_root)
    if check_fingerprint(project_root,policy):
        raise ValueError("configuration changed before task admission")
    _validate_result(config, result)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be a nonnegative integer")
    path = _task_path(project_root, task_id)
    lock = path.with_suffix(".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("task is locked by another writer; inspect before retrying") from exc
    try:
        os.close(descriptor)
        payload = _read(path)
        if payload["revision"] != expected_revision:
            raise ValueError("task revision changed; re-read before recording")
        if payload["status"] == "completed":
            raise ValueError("completed task cannot be reopened; start a new task")
        if result['status']=='completed':
            from .evidence import verify_evidence
            required_files=result.get('changed_files',[])
            for kind,records in [('check',result.get('checks',[])),('review',result.get('reviews',[]))]:
                for record in records:
                    if kind=='check' and not record.get('required',True):continue
                    ref=record.get('evidence_ref')
                    if config.workflow.require_fresh_evidence and not ref:
                        raise ValueError(f'completion requires fresh {kind} evidence_ref')
                    if ref:verify_evidence(project_root,ref,kind=kind,required_files=required_files)
        supplied = result.get("metrics", {})
        first = payload["revision"] == 0
        for name in METRICS:
            previous, current = payload["metrics"].get(name), supplied.get(name)
            value = current if first else (previous + current if previous is not None and current is not None else None)
            if value is not None and not math.isfinite(value):
                raise ValueError("accumulated metrics exceed finite range")
            payload["metrics"][name] = value
        timestamp = _now()
        payload.update(revision=expected_revision + 1, updated_at=timestamp, status=result["status"], last_result=result)
        payload["history"].append({"recorded_at": timestamp, "result": result})
        if check_fingerprint(project_root,policy):
            raise ValueError("configuration changed during task admission")
        _write(path, payload)
    finally:
        lock.unlink(missing_ok=True)
    return show_task(project_root, task_id)


def show_task(project_root: Path, task_id: str) -> dict[str, Any]:
    payload = _read(_task_path(project_root, task_id))
    result={key: value for key, value in payload.items() if key != "history"}
    if payload['status']=='completed':
        from .evidence import verify_evidence
        refs=[(kind,r.get('evidence_ref')) for kind,records in [('check',payload['last_result'].get('checks',[])),('review',payload['last_result'].get('reviews',[]))] for r in records if kind!='check' or r.get('required',True)]
        result['verification_state']='current' if refs and all(ref for _,ref in refs) else 'unbound'
        try:
            for kind,ref in refs:
                if ref:verify_evidence(project_root,ref,kind=kind,required_files=payload['last_result'].get('changed_files',[]))
        except (ValueError,OSError):result['verification_state']='stale'
    return result


def list_tasks(project_root: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be from 1 to 100")
    root = _runs_root(project_root)
    if not root.exists():
        return []
    runs = [show_task(project_root, path.stem) for path in root.glob("*.json")]
    runs.sort(key=lambda run: run["updated_at"], reverse=True)
    return [{key: run[key] for key in ("task_id", "goal", "status", "revision", "updated_at")} for run in runs[:limit]]
