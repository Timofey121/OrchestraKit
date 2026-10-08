"""Bounded local monitoring and authenticated loopback project settings.

Monitoring reads whitelisted fragments of OrchestraKit and Codex state. Explicit
settings/connect requests may update only known local project integrations.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import socket
import secrets
import hmac
import sqlite3
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote, unquote, urlsplit, parse_qs

from .config import load_config


_MAX_JSON_BYTES = 256 * 1024
_MAX_GLOBAL_STATE_BYTES = 2 * 1024 * 1024
_MAX_STATIC_BYTES = 8 * 1024 * 1024
_MAX_EVENT_BYTES = 256 * 1024
_MAX_ROLLOUT_BYTES = 2 * 1024 * 1024
_MAX_PROJECTS = 100
_MAX_REGISTERED_PROJECTS = 10_000
_MAX_TASKS = 100
_MAX_EXECUTIONS = 100
_MAX_EVENTS = 100
_MAX_CHATS = 200
_MAX_QUEUE = 256
_MAX_TEXT = 16_000
_LIVE_ACTIVITY_SECONDS = 5 * 60
_CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _number(value: Any) -> int | float | None:
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def _timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        return _short(value, 100)
    number = _number(value)
    if number is None:
        return None
    try:
        seconds = float(number) / 1000 if number > 10_000_000_000 else float(number)
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _safe_child(root: Path, path: Path) -> Path | None:
    """Return a regular non-symlink file under ``root``, or no file."""
    try:
        if root.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if resolved_root != resolved and resolved_root not in resolved.parents:
            return None
        return resolved
    except OSError:
        return None


def _orchestra_root(project: Path, warnings: list[str]) -> Path | None:
    """Return the actual Orchestra storage root without following a symlink."""
    try:
        approved = project.resolve(strict=True)
        root = project / ".orchestra"
        if root.is_symlink() or not root.is_dir():
            return None
        resolved = root.resolve(strict=True)
        if approved not in resolved.parents:
            return None
        return resolved
    except OSError as exc:
        if len(warnings) < 100:
            warnings.append(f"cannot inspect Orchestra storage: {exc}"[:400])
        return None


def _read_limited(path: Path, maximum: int) -> bytes:
    size = path.stat().st_size
    if size > maximum:
        raise ValueError(f"file exceeds {maximum} byte dashboard limit")
    with path.open("rb") as source:
        return source.read(maximum + 1)


def _json_file(root: Path, path: Path, maximum: int = _MAX_JSON_BYTES) -> Any:
    safe = _safe_child(root, path)
    if safe is None:
        raise ValueError("artifact is not a regular file inside its approved root")
    try:
        raw = _read_limited(safe, maximum)
        if len(raw) > maximum:
            raise ValueError("artifact exceeds dashboard limit")
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("artifact is not valid JSON") from exc


def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise ValueError("database is not a regular file")
    uri = "file:" + quote(str(path.resolve(strict=True))) + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")
    return db


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    if not table.replace("_", "").isalnum():
        return set()
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def _value(row: sqlite3.Row, name: str) -> Any:
    return row[name] if name in row.keys() else None


class Dashboard:
    """A bounded local view of explicitly supplied and Codex-known projects."""

    def __init__(self, projects: Sequence[Path], codex_home: Path | None, kit_root: Path) -> None:
        if not isinstance(kit_root, Path):
            raise ValueError("kit_root must be a Path")
        self.kit_root = kit_root
        self.codex_home = codex_home if isinstance(codex_home, Path) else None
        self._explicit = tuple(item for item in projects if isinstance(item, Path))
        self._display_paths: dict[Path, str] = {}
        self._registered_names: dict[Path, str] = {}
        self._registered_ids_by_path: dict[Path, set[str]] = {}
        self._project_paths_by_registered_id: dict[str, Path] = {}
        self._cache_lock = threading.Lock()
        self._cache_at = 0.0
        self._cache: tuple[list[dict[str, Any]], list[str]] | None = None
        for item in self._explicit:
            try:
                self._display_paths[item.resolve(strict=True)] = str(item.absolute())
            except OSError:
                continue

    @staticmethod
    def _project_id(path: Path) -> str:
        return "p-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]

    def _warn(self, warnings: list[str], message: str) -> None:
        if len(warnings) < 100:
            warnings.append(message[:400])

    def _candidate_projects(self, warnings: list[str], threads: list[dict[str, Any]],
                            global_data: dict[str, Any]) -> list[Path]:
        registered = list(self._explicit)
        self._registered_names.clear()
        self._registered_ids_by_path.clear()
        self._project_paths_by_registered_id.clear()
        local = global_data.get("local-projects") if isinstance(global_data, dict) else None
        if isinstance(local, dict):
            for legacy_id, item in local.items():
                if not isinstance(item, dict):
                    continue
                roots = item.get("rootPaths")
                if isinstance(roots, list):
                    for root in roots:
                        if not isinstance(root, str):
                            continue
                        candidate = Path(root)
                        registered.append(candidate)
                        try:
                            resolved = candidate.resolve(strict=True)
                        except OSError:
                            continue
                        self._display_paths.setdefault(resolved, root)
                        if isinstance(legacy_id, str):
                            self._registered_ids_by_path.setdefault(resolved, set()).add(legacy_id)
                            self._project_paths_by_registered_id[legacy_id] = resolved
                        name = item.get("name")
                        if isinstance(name, str) and name.strip():
                            self._registered_names[resolved] = name.strip()[:300]
        registered.extend(self._database_projects(warnings))
        if len(registered) > _MAX_REGISTERED_PROJECTS:
            self._warn(warnings, "registered project discovery limit reached")
            registered = registered[:_MAX_REGISTERED_PROJECTS]
        inferred = self._saved_workspace_roots(global_data) + self._thread_cwds(threads)
        result: list[Path] = []
        seen: set[Path] = set()
        for candidate in registered:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_dir() or resolved in seen:
                continue
            seen.add(resolved)
            result.append(resolved)
        for candidate in inferred:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_dir() or resolved in seen:
                continue
            if len(result) >= _MAX_PROJECTS:
                self._warn(warnings, "inferred project discovery limit reached")
                break
            seen.add(resolved)
            result.append(resolved)
        return result

    def _database_projects(self, warnings: list[str]) -> list[Path]:
        """Read project roots registered in current Codex state databases."""
        roots: list[Path] = []
        newer_project_ids: set[str] = set()
        database_paths: set[Path] = set()
        for state_path in self._state_paths(warnings):
            current_project_ids: set[str] = set()
            current_primary_ids: set[str] = set()
            try:
                with _sqlite_readonly(state_path) as db:
                    if not {"projects", "project_roots"} <= {
                        str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    }:
                        continue
                    project_columns = _columns(db, "projects")
                    root_columns = _columns(db, "project_roots")
                    if not {"id", "name"} <= project_columns or not {"project_id", "path"} <= root_columns:
                        continue
                    query = ("SELECT p.id, p.name, r.path FROM projects p "
                             "JOIN project_roots r ON r.project_id = p.id "
                             "ORDER BY " + ("p.position, " if "position" in project_columns else "")
                             + ("r.position" if "position" in root_columns else "r.path") + " LIMIT ?")
                    found = 0
                    for row in db.execute(query, (_MAX_REGISTERED_PROJECTS + 1,)):
                        found += 1
                        if found > _MAX_REGISTERED_PROJECTS:
                            self._warn(warnings, f"registered project discovery limit reached in {state_path.name}")
                            break
                        identifier = _short(_value(row, "id"), 300)
                        if identifier and identifier in newer_project_ids:
                            continue
                        if identifier:
                            current_project_ids.add(identifier)
                        raw = _value(row, "path")
                        if not isinstance(raw, str):
                            continue
                        try:
                            resolved = Path(raw).resolve(strict=True)
                        except OSError:
                            continue
                        if not resolved.is_dir():
                            continue
                        if identifier:
                            self._registered_ids_by_path.setdefault(resolved, set()).add(identifier)
                            if identifier not in current_primary_ids:
                                self._project_paths_by_registered_id[identifier] = resolved
                                current_primary_ids.add(identifier)
                        name = _short(_value(row, "name"), 300)
                        if resolved not in database_paths and name and name.strip():
                            self._registered_names[resolved] = name.strip()
                        database_paths.add(resolved)
                        self._display_paths.setdefault(resolved, raw)
                        roots.append(resolved)
            except (OSError, sqlite3.Error, ValueError) as exc:
                self._warn(warnings, f"cannot read registered projects from {state_path.name}: {exc}")
            newer_project_ids.update(current_project_ids)
        return roots

    @staticmethod
    def _saved_workspace_roots(value: Any) -> list[Path]:
        """Extract only explicitly named workspace-root arrays from global state."""
        roots: list[Path] = []
        names = {"rootpaths", "root_paths", "workspaceroots", "workspace_roots"}
        pending = [value]
        examined = 0
        while pending and examined < 1_000:
            current = pending.pop()
            examined += 1
            if isinstance(current, dict):
                for key, item in current.items():
                    normalized = key.lower().replace("-", "_") if isinstance(key, str) else ""
                    if normalized in names and isinstance(item, list):
                        roots.extend(Path(root) for root in item if isinstance(root, str))
                    elif isinstance(item, (dict, list)):
                        pending.append(item)
            elif isinstance(current, list):
                pending.extend(current[:_MAX_PROJECTS])
        return roots[:_MAX_PROJECTS]

    def _global_state(self, warnings: list[str]) -> dict[str, Any]:
        if self.codex_home is None or self.codex_home.is_symlink() or not self.codex_home.is_dir():
            return {}
        path = self.codex_home / ".codex-global-state.json"
        if not path.exists():
            return {}
        try:
            value = _json_file(self.codex_home, path, _MAX_GLOBAL_STATE_BYTES)
            return value if isinstance(value, dict) else {}
        except ValueError as exc:
            self._warn(warnings, f"cannot read Codex global state: {exc}")
            return {}

    def _state_paths(self, warnings: list[str]) -> list[Path]:
        if self.codex_home is None or self.codex_home.is_symlink():
            return []
        try:
            def version(path: Path) -> int:
                suffix = path.stem.removeprefix("state_")
                return int(suffix) if suffix.isdecimal() else -1
            paths = sorted(self.codex_home.glob("state_*.sqlite"), key=version, reverse=True)[:20]
        except OSError as exc:
            self._warn(warnings, f"cannot list Codex state: {exc}")
            return []
        return [path for path in paths if _safe_child(self.codex_home, path) is not None]

    def _read_threads(self, warnings: list[str]) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        selected = ("id", "name", "title", "cwd", "source", "agent_path", "model", "reasoning_effort",
                    "updated_at", "updated_at_ms", "archived", "project_id", "rollout_path")
        for state_path in self._state_paths(warnings):
            try:
                with _sqlite_readonly(state_path) as db:
                    tables = [str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'")]
                    if not tables:
                        continue
                    available = _columns(db, "threads")
                    if "id" not in available:
                        continue
                    names = [name for name in selected if name in available]
                    filters: list[str] = []
                    params: list[Any] = []
                    if "source" in available:
                        filters.append("(source IS NULL OR (source NOT LIKE ? AND source NOT LIKE ? AND source != ?))")
                        params.extend(("%guardian%", "%subagent%", "exec"))
                    if "agent_path" in available:
                        filters.append("agent_path IS NULL")
                    order = ""
                    if "updated_at" in available:
                        order = " ORDER BY updated_at DESC"
                    if "archived" in available:
                        order = " ORDER BY CASE WHEN archived THEN 1 ELSE 0 END, updated_at DESC" if "updated_at" in available else " ORDER BY CASE WHEN archived THEN 1 ELSE 0 END"
                    query = "SELECT " + ", ".join(names) + " FROM threads" + (" WHERE " + " AND ".join(filters) if filters else "") + order + " LIMIT ?"
                    params.append(_MAX_CHATS + 1)
                    found = 0
                    for row in db.execute(query, params):
                        found += 1
                        if found > _MAX_CHATS:
                            self._warn(warnings, f"chat discovery limit reached in {state_path.name}")
                            break
                        identifier = _short(_value(row, "id"), 300)
                        if not identifier or identifier in rows:
                            continue
                        rows[identifier] = {name: _value(row, name) for name in names}
            except (OSError, sqlite3.Error, ValueError) as exc:
                self._warn(warnings, f"cannot read Codex state {state_path.name}: {exc}")
        def recency(item: dict[str, Any]) -> float:
            value = item.get("updated_at_ms") if item.get("updated_at_ms") is not None else item.get("updated_at")
            if type(value) in {int, float}:
                return float(value)
            timestamp = _timestamp(value)
            try:
                return datetime.fromisoformat(timestamp).timestamp() if timestamp else 0.0
            except ValueError:
                return 0.0
        result = sorted(rows.values(), key=recency, reverse=True)
        if len(result) > _MAX_CHATS:
            self._warn(warnings, "global chat discovery limit reached")
        return result[:_MAX_CHATS]

    def _thread_cwds(self, threads: list[dict[str, Any]]) -> list[Path]:
        roots: list[Path] = []
        for row in threads:
            cwd = row.get("cwd")
            if bool(row.get("archived")) or not isinstance(cwd, str):
                continue
            try:
                current = Path(cwd).resolve(strict=True)
            except OSError:
                continue
            for project in (current, *list(current.parents)[:12]):
                storage = project / ".orchestra"
                config = storage / "project.toml"
                if not storage.is_symlink() and _safe_child(storage, config) is not None:
                    if project == current:
                        self._display_paths.setdefault(project, cwd)
                    roots.append(project)
                    break
        return roots

    @staticmethod
    def _apply_global_thread_assignments(threads: list[dict[str, Any]], global_data: dict[str, Any]) -> None:
        """Fill unmigrated SQLite project IDs from bounded desktop global state."""
        assignments = global_data.get("thread-project-assignments")
        if not isinstance(assignments, dict):
            return
        for row in threads:
            if isinstance(row.get("project_id"), str) and row["project_id"]:
                continue
            identifier = row.get("id")
            assignment = assignments.get(identifier) if isinstance(identifier, str) else None
            if not isinstance(assignment, dict) or assignment.get("projectKind") != "local":
                continue
            project_id = _short(assignment.get("projectId"), 300)
            if project_id:
                row["project_id"] = project_id

    def _live_chat(self, row: dict[str, Any], warnings: list[str]) -> tuple[str, str | None, str | None, str | None]:
        """Read only the tail lifecycle and assistant commentary of a rollout."""
        if bool(row.get("archived")):
            return "archived", None, _short(row.get("model"), 300), _short(row.get("reasoning_effort"), 100)
        if self.codex_home is None or self.codex_home.is_symlink() or not isinstance(row.get("rollout_path"), str):
            return "unknown", None, _short(row.get("model"), 300), _short(row.get("reasoning_effort"), 100)
        try:
            raw_path = Path(row["rollout_path"])
            if not raw_path.is_absolute() or raw_path.is_symlink():
                raise ValueError("rollout path is a symlink")
            path = raw_path.resolve(strict=True)
            allowed = []
            for name in ("sessions", "archived_sessions"):
                candidate = self.codex_home / name
                if candidate.is_dir() and not candidate.is_symlink():
                    resolved = candidate.resolve(strict=True)
                    home = self.codex_home.resolve(strict=True)
                    if home == resolved or home in resolved.parents:
                        allowed.append(resolved)
            if not any(root == path or root in path.parents for root in allowed) or not path.is_file():
                raise ValueError("rollout is outside approved sessions roots")
            size = path.stat().st_size
            with path.open("rb") as source:
                source.seek(max(0, size - _MAX_ROLLOUT_BYTES))
                data = source.read(_MAX_ROLLOUT_BYTES)
            lines = data.splitlines()
            if size > _MAX_ROLLOUT_BYTES and lines:
                lines = lines[1:]
                self._warn(warnings, f"rollout tail truncated for chat {_short(row.get('id'), 80) or 'unknown'}")
            status, activity = "unknown", None
            lifecycle_position = -1
            activity_position = -1
            model, effort = _short(row.get("model"), 300), _short(row.get("reasoning_effort"), 100)
            for position, line in enumerate(lines):
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                payload = event.get("payload") if event_type in {"event_msg", "response_item"} else event
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("type")
                if kind == "task_started":
                    status = "running"
                    activity = None
                    lifecycle_position = position
                elif kind == "task_complete":
                    status = "idle"
                    lifecycle_position = position
                elif kind == "turn_aborted":
                    status = "stopped"
                    lifecycle_position = position
                context = event.get("payload") if event_type == "turn_context" else payload if kind == "turn_context" else payload.get("turn_context")
                if isinstance(context, dict):
                    model = _short(context.get("model"), 300) or model
                    effort = _short(context.get("effort") or context.get("reasoning_effort") or context.get("model_reasoning_effort"), 100) or effort
                item = payload.get("item")
                if kind in {"item_completed", "response_item"} and isinstance(item, dict) and item.get("type") == "AgentMessage" and item.get("phase") == "commentary":
                    content = _short(item.get("content"), 240)
                    if content:
                        activity = " ".join(content.split())[:240]
                        activity_position = position
                if kind in {"message", "response_item"} and payload.get("role") == "assistant" and payload.get("phase") == "commentary":
                    content = payload.get("content")
                    if isinstance(content, list):
                        texts = [part.get("text") for part in content if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)]
                        if texts:
                            activity = " ".join(" ".join(texts).split())[:240]
                            activity_position = position
                if not activity and kind in {"item_completed", "response_item"} and isinstance(item, dict) and item.get("type") in {"CommandExecution", "FileChange", "McpToolCall", "SubAgentActivity"}:
                    activity = {"CommandExecution": "Выполняет инструмент", "FileChange": "Работает с файлами", "McpToolCall": "Выполняет инструмент", "SubAgentActivity": "Работает с исполнителем"}[item["type"]]
                    activity_position = position
            fresh = max(0.0, time.time() - path.stat().st_mtime) <= _LIVE_ACTIVITY_SECONDS
            if activity_position > lifecycle_position and status != "running":
                status = "running" if fresh else "unknown"
            elif status == "running" and not fresh:
                status = "unknown"
            return status, activity, model, effort
        except (OSError, ValueError) as exc:
            self._warn(warnings, f"cannot read live chat activity: {exc}")
            return "unknown", None, _short(row.get("model"), 300), _short(row.get("reasoning_effort"), 100)

    @staticmethod
    def _chat_title(row: dict[str, Any]) -> str:
        name = _short(row.get("name"), 160)
        if name and name.strip():
            return " ".join(name.split())[:160]
        title = _short(row.get("title"), 300)
        if title and len(title) <= 200 and "\n" not in title and not title.lstrip().startswith(("#", "<", "Files mentioned")):
            return " ".join(title.split())[:200]
        identifier = _short(row.get("id"), 8) or "unknown"
        return f"Chat {identifier}"

    def _chats(self, projects: list[Path], threads: list[dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
        identities = {path: self._project_id(path) for path in projects}
        configured = {path for path in projects if _orchestra_root(path, []) is not None and _safe_child(path / ".orchestra", path / ".orchestra" / "project.toml") is not None}
        chats: list[dict[str, Any]] = []
        for row in threads:
            cwd = row.get("cwd")
            project: Path | None = None
            try:
                workspace = Path(cwd).resolve(strict=True) if isinstance(cwd, str) else None
            except OSError:
                workspace = None
            if workspace in configured:
                project = workspace
            registered_id = row.get("project_id")
            if project is None and isinstance(registered_id, str):
                project = self._project_paths_by_registered_id.get(registered_id)
            if project is None and workspace is not None:
                matches = [candidate for candidate in identities if candidate == workspace or candidate in workspace.parents]
                project = max(matches, key=lambda candidate: len(candidate.parts)) if matches else None
            project_id = identities.get(project) if project is not None else None
            if project_id is None:
                continue
            status, activity, model, effort = self._live_chat(row, warnings)
            updated = row.get("updated_at_ms") if row.get("updated_at_ms") is not None else row.get("updated_at")
            chat = {"id": _short(row.get("id"), 300), "title": self._chat_title(row),
                    "project_id": project_id, "model": model, "effort": effort,
                    "updated_at": _timestamp(updated), "status": status,
                    "archived": bool(row.get("archived"))}
            if activity:
                chat["last_activity"] = activity
            chats.append(chat)
        chats.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return chats[:_MAX_CHATS]

    def _task_records(self, project: Path, warnings: list[str]) -> list[dict[str, Any]]:
        storage = _orchestra_root(project, warnings)
        if storage is None:
            return []
        root = storage / "runs"
        if root.is_symlink() or not root.is_dir():
            return []
        try:
            if storage not in root.resolve(strict=True).parents:
                return []
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        try:
            paths = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
            if len(paths) > _MAX_TASKS:
                self._warn(warnings, f"task discovery limit reached for {project.name}")
            paths = paths[:_MAX_TASKS]
        except OSError as exc:
            self._warn(warnings, f"cannot list task states: {exc}")
            return []
        for path in paths:
            try:
                from .state import MAX_TASK_BYTES
                data = _json_file(root, path, MAX_TASK_BYTES)
                if not isinstance(data, dict) or data.get("generated_by") != "OrchestraKit" or data.get("task_id") != path.stem:
                    raise ValueError("invalid task state")
                result = data.get("last_result") if isinstance(data.get("last_result"), dict) else {}
                verification = "unbound"
                if data.get("status") == "completed":
                    from .state import show_task
                    verification = _short(show_task(project, path.stem).get("verification_state"), 100) or "unbound"
                records.append({"id": _short(data.get("task_id"), 100), "goal": _short(data.get("goal")),
                    "status": _short(data.get("status"), 100), "revision": data.get("revision") if type(data.get("revision")) is int else None,
                    "updated_at": _short(data.get("updated_at"), 100), "chat_id": _short(data.get("chat_id"), 300),
                    "summary": _short(result.get("summary")), "completed_steps": _text_list(result.get("completed_steps")),
                    "next_steps": _text_list(result.get("next_steps")), "checks": _safe_records(result.get("checks")),
                    "reviews": _safe_records(result.get("reviews")), "verification_state": verification,
                    "metrics": _metrics(data.get("metrics")), "changed_files": _text_list(result.get("changed_files"))})
            except (ValueError, OSError, AttributeError, KeyError, TypeError) as exc:
                self._warn(warnings, f"cannot read task {path.name}: {exc}")
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return records

    def _executions(self, project: Path, warnings: list[str]) -> list[dict[str, Any]]:
        storage = _orchestra_root(project, warnings)
        if storage is None:
            return []
        root = storage / "executions"
        try:
            if root.is_symlink() or not root.is_dir() or storage not in root.resolve(strict=True).parents:
                return []
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        try:
            directories = [item for item in root.iterdir() if item.is_dir() and not item.is_symlink()]
            # Select recent local runs before the cap; filesystem enumeration
            # order is arbitrary. The name also makes equal timestamps stable.
            directories.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
            if len(directories) > _MAX_EXECUTIONS:
                self._warn(warnings, f"execution discovery limit reached for {project.name}")
            directories = directories[:_MAX_EXECUTIONS]
        except OSError as exc:
            self._warn(warnings, f"cannot list executions: {exc}")
            return []
        for directory in directories:
            try:
                if root not in directory.resolve(strict=True).parents:
                    raise ValueError("execution directory escapes storage")
                manifest = _json_file(root, directory / "run.json")
                if not isinstance(manifest, dict):
                    raise ValueError("invalid run manifest")
                receipt: dict[str, Any] = {}
                receipt_path = directory / "receipt.json"
                if receipt_path.exists():
                    data = _json_file(root, receipt_path)
                    receipt = data if isinstance(data, dict) else {}
                events = self._events(root, directory / "events.jsonl", warnings)
                task_id = (_short(receipt.get("task_id"), 100) or _short(manifest.get("task_id"), 100)
                           or next((event["task_id"] for event in events if event.get("task_id")), None))
                attempts = receipt.get("attempts") if isinstance(receipt.get("attempts"), list) else manifest.get("attempts")
                records.append({"id": _short(manifest.get("execution_uuid"), 300) or directory.name,
                    "task_id": task_id, "status": _short(receipt.get("status"), 100) or _short(manifest.get("status"), 100) or _short(manifest.get("state"), 100),
                    "phase": _short(manifest.get("state"), 100), "timestamp": _short(manifest.get("timestamp"), 100),
                    "role": _short(receipt.get("role"), 100), "profile": _short(receipt.get("profile"), 100),
                    "requested_model": _short(receipt.get("requested_model"), 300), "attempts": _attempts(attempts),
                    "failure": _short(receipt.get("failure")), "reason": _short(receipt.get("reason")),
                    "usage": _usage(receipt.get("usage")), "budget": _small_object(receipt.get("budget")),
                    "integration_required": receipt.get("integration_required") is True, "events": events})
            except (ValueError, OSError) as exc:
                self._warn(warnings, f"cannot read execution {directory.name}: {exc}")
        records.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        return records

    def _events(self, root: Path, path: Path, warnings: list[str]) -> list[dict[str, Any]]:
        safe = _safe_child(root, path)
        if safe is None:
            return []
        try:
            raw = _read_limited(safe, _MAX_EVENT_BYTES)
        except (OSError, ValueError) as exc:
            self._warn(warnings, f"cannot read events: {exc}")
            return []
        events: list[dict[str, Any]] = []
        for line in raw.splitlines()[:_MAX_EVENTS]:
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._warn(warnings, "malformed execution event")
                continue
            if not isinstance(event, dict):
                continue
            events.append({"phase": _short(event.get("phase"), 100), "timestamp": _short(event.get("timestamp"), 100),
                           "attempt": event.get("attempt") if type(event.get("attempt")) is int else None,
                           "task_id": _short(event.get("task_id"), 100)})
        return events

    def _queue(self, project: Path, warnings: list[str]) -> list[dict[str, Any]]:
        storage = _orchestra_root(project, warnings)
        if storage is None:
            return []
        path = storage / "queue.sqlite"
        if not path.exists():
            return []
        try:
            with _sqlite_readonly(path) as db:
                columns = _columns(db, "tasks")
                if not {"graph_id", "node_id", "status"} <= columns:
                    return []
                names = [name for name in ("graph_id", "node_id", "status", "block_reason", "lease_expires", "launches", "observed_tokens") if name in columns]
                dependencies: dict[tuple[str, str], list[str]] = {}
                if {"graph_id", "node_id", "depends_on"} <= _columns(db, "dependencies"):
                    for row in db.execute("SELECT graph_id, node_id, depends_on FROM dependencies LIMIT ?", (4096,)):
                        dependencies.setdefault((str(row["graph_id"]), str(row["node_id"])), []).append(str(row["depends_on"]))
                records = []
                for index, row in enumerate(db.execute("SELECT " + ", ".join(names) + " FROM tasks LIMIT ?", (_MAX_QUEUE + 1,))):
                    if index >= _MAX_QUEUE:
                        self._warn(warnings, f"queue discovery limit reached for {project.name}")
                        break
                    graph, node = str(row["graph_id"]), str(row["node_id"])
                    records.append({"graph_id": graph, "node_id": node, "status": _short(_value(row, "status"), 100),
                        "block_reason": _short(_value(row, "block_reason")), "depends_on": dependencies.get((graph, node), []),
                        "launches": _value(row, "launches") if type(_value(row, "launches")) is int else None,
                        "observed_tokens": _value(row, "observed_tokens") if type(_value(row, "observed_tokens")) is int else None,
                        "lease_expires": _number(_value(row, "lease_expires"))})
                return records
        except (OSError, sqlite3.Error, ValueError) as exc:
            self._warn(warnings, f"cannot read queue: {exc}")
            return []

    def _project(self, path: Path, chats: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
        warning_start = len(warnings)
        storage = _orchestra_root(path, warnings)
        config_path = storage / "project.toml" if storage is not None else None
        configured = config_path is not None and _safe_child(storage, config_path) is not None
        profiles: list[dict[str, Any]] = []
        max_parallel: int | None = None
        max_repair_cycles: int | None = None
        max_observed_tokens: int | None = None
        max_launches: int | None = None
        workflow_mode: str | None = None
        if configured:
            try:
                config = load_config(path, self.kit_root)
                profiles = [{"name": item.name, "model": item.model, "effort": item.effort} for item in config.profiles.values()]
                max_parallel = config.max_parallel
                max_repair_cycles = config.workflow.max_repair_cycles
                max_observed_tokens = config.workflow.max_observed_tokens
                max_launches = config.workflow.max_launches
                workflow_mode = config.workflow.mode
            except (OSError, ValueError) as exc:
                self._warn(warnings, f"cannot load project config for {path.name}: {exc}")
        tasks = self._task_records(path, warnings)
        executions = self._executions(path, warnings)
        queue = self._queue(path, warnings)
        project_id = self._project_id(path)
        project_chats = [chat for chat in chats if chat["project_id"] == project_id]
        terminal_execution_states = {"verified", "failed", "completed", "complete", "terminal", "stopped", "cancelled", "canceled", "aborted"}
        def nonterminal_execution(item: dict[str, Any]) -> bool:
            status = str(item.get("status") or item.get("phase") or "").lower()
            return bool(status) and status not in terminal_execution_states
        tasks_by_chat: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            chat_id = task.get("chat_id")
            if isinstance(chat_id, str):
                tasks_by_chat.setdefault(chat_id, []).append(task)
        executions_by_task: dict[str, list[dict[str, Any]]] = {}
        for execution in executions:
            task_id = execution.get("task_id")
            if isinstance(task_id, str):
                executions_by_task.setdefault(task_id, []).append(execution)
        for chat in project_chats:
            chat_tasks = tasks_by_chat.get(str(chat.get("id")), [])
            chat_executions = [execution for task in chat_tasks for execution in executions_by_task.get(str(task.get("id")), [])]
            chat["task_ids"] = [str(task["id"]) for task in chat_tasks if task.get("id")]
            chat["execution_ids"] = [str(execution["id"]) for execution in chat_executions if execution.get("id")]
            nonterminal = sum(nonterminal_execution(execution) for execution in chat_executions)
            chat["counts"] = {"tasks": len(chat_tasks),
                "persisted_running_tasks": sum(task.get("status") == "running" for task in chat_tasks),
                "executions": len(chat_executions), "nonterminal_executions": nonterminal,
                "agents": len(chat_executions), "nonterminal_agents": nonterminal}
        persisted_running = sum(item["status"] == "running" for item in tasks)
        nonterminal_executions = sum(nonterminal_execution(item) for item in executions)
        counts = {"tasks": len(tasks), "running": persisted_running, "persisted_running_tasks": persisted_running,
                  "live_chats": sum(item.get("status") == "running" for item in project_chats),
                  "blocked": sum(item["status"] == "blocked" for item in tasks), "executions": len(executions),
                  "nonterminal_executions": nonterminal_executions, "nonterminal_agents": nonterminal_executions,
                  "chats": len(project_chats)}
        configured_name = config.name if configured and 'config' in locals() and config.name != "__PROJECT_NAME__" else None
        display_name = self._registered_names.get(path) or configured_name or path.name
        return {"id": project_id, "name": display_name, "path": self._display_paths.get(path, str(path)), "configured": configured, "counts": counts,
                "warnings": warnings[warning_start:], "profiles": profiles, "max_parallel": max_parallel,
                "max_repair_cycles": max_repair_cycles, "max_observed_tokens": max_observed_tokens,
                "max_launches": max_launches, "workflow_mode": workflow_mode,
                "tasks": tasks, "executions": executions, "queue": queue, "chats": project_chats}

    def _all(self, *, refresh: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
        with self._cache_lock:
            if not refresh and self._cache is not None and time.monotonic() - self._cache_at < 1:
                return deepcopy(self._cache)
        warnings: list[str] = []
        global_data = self._global_state(warnings)
        threads = self._read_threads(warnings)
        self._apply_global_thread_assignments(threads, global_data)
        paths = self._candidate_projects(warnings, threads, global_data)
        chats = self._chats(paths, threads, warnings)
        result = ([self._project(path, chats, warnings) for path in paths], warnings)
        with self._cache_lock:
            self._cache_at = time.monotonic()
            self._cache = deepcopy(result)
        return result

    def snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        projects, warnings = self._all(refresh=refresh)
        overview_projects = [{key: item[key] for key in ("id", "name", "path", "configured", "counts", "warnings")} for item in projects]
        chats = [chat for item in projects for chat in item["chats"]]
        totals = {"projects": len(projects), "tasks": sum(item["counts"]["tasks"] for item in projects),
                  "running": sum(item["counts"]["running"] for item in projects), "blocked": sum(item["counts"]["blocked"] for item in projects),
                  "chats": len(chats)}
        return {"generated_at": _now(), "projects": overview_projects, "chats": chats, "totals": totals, "warnings": warnings}

    def project_details(self, project_id: str, *, chat_id: str | None = None, refresh: bool = False) -> dict[str, Any]:
        projects, warnings = self._all(refresh=refresh)
        for item in projects:
            if item["id"] == project_id:
                project = {key: item[key] for key in ("id", "name", "path", "configured", "counts", "warnings", "profiles", "max_parallel", "max_repair_cycles", "max_observed_tokens", "max_launches", "workflow_mode")}
                tasks = item["tasks"]
                executions = item["executions"]
                chats = item["chats"]
                queue = item["queue"]
                result: dict[str, Any] = {"project": project, "tasks": tasks, "executions": executions,
                    "queue": queue, "chats": chats, "warnings": warnings}
                if chat_id is not None:
                    selected = [chat for chat in chats if chat.get("id") == chat_id]
                    if not selected:
                        raise KeyError("unknown chat")
                    selected_tasks = [task for task in tasks if task.get("chat_id") == chat_id]
                    task_ids = {task.get("id") for task in selected_tasks}
                    result.update({"tasks": selected_tasks,
                        "executions": [execution for execution in executions if execution.get("task_id") in task_ids],
                        "queue": [], "chats": selected, "filter": {"chat_id": chat_id}})
                return result
        raise KeyError("unknown project")


def _text_list(value: Any) -> list[str]:
    return [_short(item) or "" for item in value[:100] if isinstance(item, str)] if isinstance(value, list) else []


def _safe_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for record in value[:100]:
        if isinstance(record, dict):
            cleaned: dict[str, Any] = {}
            for key, item in record.items():
                if not isinstance(key, str):
                    continue
                name = key[:100]
                if isinstance(item, str):
                    cleaned[name] = _short(item)
                elif type(item) in {bool, type(None)}:
                    cleaned[name] = item
                elif _number(item) is not None:
                    cleaned[name] = item
                elif name == "argv" and isinstance(item, list) and all(isinstance(part, str) for part in item[:64]):
                    cleaned[name] = [_short(part, 1000) for part in item[:64]]
            result.append(cleaned)
    return result


def _metrics(value: Any) -> dict[str, int | float | None]:
    source = value if isinstance(value, dict) else {}
    return {name: _number(source.get(name)) for name in ("input_tokens", "output_tokens", "elapsed_seconds", "cost_usd")}


def _usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    totals = value.get("totals")
    if not isinstance(totals, dict):
        return None
    return {"complete": value.get("complete") is True, "totals": {key: item for key, item in totals.items() if isinstance(key, str) and _number(item) is not None}}


def _small_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, str):
            result[key[:100]] = _short(item)
        elif type(item) in {bool, type(None)} or _number(item) is not None:
            result[key[:100]] = item
    return result


def _attempts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:20]:
        if isinstance(item, dict):
            result.append({"number": item.get("number") if type(item.get("number")) is int else None,
                "profile": _short(item.get("profile"), 100), "requested_model": _short(item.get("requested_model"), 300),
                "requested_effort": _short(item.get("requested_effort"), 100), "returncode": item.get("returncode") if type(item.get("returncode")) is int else None,
                "usage": _usage(item.get("usage")), "checks": _safe_records(item.get("checks"))})
    return result


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(dashboard: Dashboard, host: str = "127.0.0.1", port: int = 0, static_root: Path | None = None, *, settings_service=None) -> ThreadingHTTPServer:
    """Serve the dashboard only on loopback; the caller owns server lifetime."""
    if not _loopback(host):
        raise ValueError("dashboard host must be loopback")
    root = static_root.resolve(strict=True) if static_root is not None and static_root.is_dir() and not static_root.is_symlink() else None

    from .settings import SettingsService,SettingsConflict
    from .providers import CredentialStoreError
    from .project import init_project,ProjectError
    service=settings_service or SettingsService(dashboard.kit_root)
    session_token=secrets.token_urlsafe(32)
    def project_path(project_id):
        for item in dashboard.snapshot(refresh=True)['projects']:
            if item['id']==project_id:
                path=Path(item['path']).resolve(strict=True)
                if path.is_dir() and dashboard._project_id(path)==project_id:
                    return path,item
        raise KeyError('unknown project')

    class Handler(BaseHTTPRequestHandler):
        server_version = "OrchestraKitDashboard/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _valid_host(self) -> bool:
            header = self.headers.get("Host")
            try:
                parsed = urlsplit("//" + header) if header else None
                return bool(parsed and _loopback(parsed.hostname) and parsed.port == self.server.server_address[1])
            except ValueError:
                return False

        def _valid_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            try:
                parsed = urlsplit(origin)
                return parsed.scheme == "http" and _loopback(parsed.hostname) and parsed.port == self.server.server_address[1] and not parsed.username and not parsed.password
            except ValueError:
                return False

        def _headers(self, content_type: str, length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")

        def _reply(self, status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8") -> None:
            self.send_response(status)
            self._headers(content_type, len(body))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            path=unquote(urlsplit(self.path).path)
            parts=path.strip('/').split('/')
            if len(parts)!=4 or parts[:2]!=['api','projects'] or parts[3] not in {'settings','connect'}:
                self._reply(HTTPStatus.METHOD_NOT_ALLOWED,b'method not allowed');return
            if not self._valid_host():
                self._reply(HTTPStatus.MISDIRECTED_REQUEST,b'invalid Host');return
            supplied=self.headers.get('X-Orchestra-Token','')
            if not self.headers.get('Origin') or not self._valid_origin() or not hmac.compare_digest(supplied.encode(),session_token.encode()):
                self._reply(HTTPStatus.FORBIDDEN,b'forbidden');return
            try:
                if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                    raise ValueError('Требуется JSON')
                if self.headers.get('Transfer-Encoding'):
                    raise ValueError('Неподдерживаемый формат запроса')
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=320*1024: raise ValueError('Запрос слишком велик или пуст')
                project,_=project_path(parts[2])
                self.connection.settimeout(5)
                payload=json.loads(self.rfile.read(length))
                if parts[3]=='connect':
                    if payload!={}: raise ValueError('Некорректный запрос подключения')
                    from .project import _safe_path
                    _safe_path(project,'.orchestra/project.toml')
                    init_project(project,dashboard.kit_root)
                    result=service.get(project)
                else: result=service.save(project,payload)
                dashboard.snapshot(refresh=True)
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND,'Проект не найден');return
            except SettingsConflict as exc:
                self._json_error(HTTPStatus.CONFLICT,str(exc));return
            except CredentialStoreError:
                self._json_error(HTTPStatus.BAD_REQUEST,'Не удалось сохранить ключ в системной связке');return
            except (ValueError,ProjectError,TypeError,UnicodeError):
                self._json_error(HTTPStatus.BAD_REQUEST,'Проверьте настройки. Адрес, модель и провайдер должны быть корректны; чужие интеграционные файлы не перезаписываются');return
            except OSError:
                self._json_error(HTTPStatus.BAD_REQUEST,'Не удалось записать настройки проекта');return
            self._reply(HTTPStatus.OK,json.dumps(result,ensure_ascii=False).encode(),'application/json; charset=utf-8')

        def _json_error(self,status,message):
            self._reply(status,json.dumps({'error':message},ensure_ascii=False).encode(),'application/json; charset=utf-8')

        def do_PUT(self) -> None:  # noqa: N802
            self._reply(HTTPStatus.METHOD_NOT_ALLOWED,b'method not allowed')

        def do_DELETE(self) -> None:  # noqa: N802
            self._reply(HTTPStatus.METHOD_NOT_ALLOWED,b'method not allowed')

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._reply(HTTPStatus.METHOD_NOT_ALLOWED,b'method not allowed')

        def do_PATCH(self) -> None:  # noqa: N802
            self._reply(HTTPStatus.METHOD_NOT_ALLOWED,b'method not allowed')

        def do_GET(self) -> None:  # noqa: N802
            if not self._valid_host():
                self._reply(HTTPStatus.MISDIRECTED_REQUEST, b"invalid Host")
                return
            if not self._valid_origin():
                self._reply(HTTPStatus.FORBIDDEN, b"invalid Origin")
                return
            path = unquote(urlsplit(self.path).path)
            if path=='/api/session':
                self._reply(HTTPStatus.OK,json.dumps({'token':session_token}).encode(),'application/json; charset=utf-8');return
            if path == "/api/overview":
                self._reply(HTTPStatus.OK, json.dumps(dashboard.snapshot(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            prefix = "/api/projects/"
            if path.startswith(prefix):
                project_id=path[len(prefix):]
                if project_id.endswith('/settings'):
                    try:
                        project,item=project_path(project_id[:-9])
                        payload=service.get(project) if item['configured'] else {'configured':False,'profiles':{},'providers':{}}
                    except KeyError:
                        self._json_error(HTTPStatus.NOT_FOUND,'Проект не найден');return
                    except (ValueError,OSError,ProjectError):
                        self._json_error(HTTPStatus.BAD_REQUEST,'Не удалось прочитать настройки проекта');return
                    self._reply(HTTPStatus.OK,json.dumps(payload,ensure_ascii=False).encode(),'application/json; charset=utf-8');return
                try:
                    chat_id=parse_qs(urlsplit(self.path).query).get('chat_id',[None])[0]
                    payload = dashboard.project_details(project_id,chat_id=chat_id)
                except KeyError:
                    self._reply(HTTPStatus.NOT_FOUND, b"unknown project")
                else:
                    self._reply(HTTPStatus.OK, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            names = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/room.png": "room.png", "/room.webp": "room.webp"}
            name = names.get(path)
            if root is None or name is None:
                self._reply(HTTPStatus.NOT_FOUND, b"not found")
                return
            artifact = _safe_child(root, root / name)
            if artifact is None:
                self._reply(HTTPStatus.NOT_FOUND, b"not found")
                return
            try:
                body = _read_limited(artifact, _MAX_STATIC_BYTES)
            except (OSError, ValueError):
                self._reply(HTTPStatus.NOT_FOUND, b"not found")
                return
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self._reply(HTTPStatus.OK, body, content_type)

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    return Server((host, port), Handler)
