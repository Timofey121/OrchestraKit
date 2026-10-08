"""Durable, local SQLite DAG queue with explicitly recovered leases."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import load_config
from .fingerprint import check_fingerprint, fingerprint_paths


_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TERMINAL = {"verified", "failed", "cancelled", "blocked"}


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("queue values must be finite JSON") from exc


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be an ASCII identifier of at most 128 characters")
    return value


class Queue:
    def __init__(self, project: Path, kit_root: Path) -> None:
        if not isinstance(project, Path) or not isinstance(kit_root, Path):
            raise ValueError("project and kit_root must be Paths")
        self.project = project.resolve(strict=True)
        if not self.project.is_dir():
            raise ValueError("project must be a directory")
        self.kit_root = kit_root
        self.path = self.project / ".orchestra" / "queue.sqlite"
        self._secure_path()
        self._initialize()

    def _secure_path(self) -> None:
        current = self.project
        for part in (".orchestra",):
            current = current / part
            if current.exists() and current.is_symlink():
                raise ValueError("queue storage must not use symlink parents")
        for candidate in (self.path, Path(str(self.path) + "-journal"), Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if candidate.is_symlink():
                raise ValueError("queue storage and SQLite sidecars must not be symlinks")
        parent = self.path.parent.resolve(strict=False)
        if parent != self.project and self.project not in parent.parents:
            raise ValueError("queue storage escapes project")

    def _connect(self) -> sqlite3.Connection:
        self._secure_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._secure_path()
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        os.chmod(self.path, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS graphs (
                    graph_id TEXT PRIMARY KEY, submitted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    graph_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    job_json TEXT NOT NULL, acceptance_json TEXT NOT NULL,
                    estimated_tokens INTEGER, source_json TEXT, input_blocked INTEGER NOT NULL,
                    status TEXT NOT NULL, block_reason TEXT, owner TEXT, lease_token TEXT,
                    lease_expires REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    launches INTEGER NOT NULL DEFAULT 0, usage_known INTEGER NOT NULL DEFAULT 1,
                    evidence_ref TEXT, observed_tokens INTEGER,
                    PRIMARY KEY(graph_id, node_id), FOREIGN KEY(graph_id) REFERENCES graphs(graph_id)
                );
                CREATE TABLE IF NOT EXISTS dependencies (
                    graph_id TEXT NOT NULL, node_id TEXT NOT NULL, depends_on TEXT NOT NULL,
                    PRIMARY KEY(graph_id, node_id, depends_on),
                    FOREIGN KEY(graph_id, node_id) REFERENCES tasks(graph_id, node_id),
                    FOREIGN KEY(graph_id, depends_on) REFERENCES tasks(graph_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    graph_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                    observed_tokens INTEGER, usage_known INTEGER NOT NULL DEFAULT 1,
                    closed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(graph_id, node_id, attempt),
                    FOREIGN KEY(graph_id, node_id) REFERENCES tasks(graph_id, node_id)
                );
            """)

    @staticmethod
    def _validate_nodes(nodes: Any) -> list[dict[str, Any]]:
        if not isinstance(nodes, list) or not nodes or len(nodes) > 256:
            raise ValueError("nodes must be a nonempty list of at most 256 nodes")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for node in nodes:
            if not isinstance(node, dict) or set(node) - {"id", "depends_on", "job", "acceptance_refs", "estimated_tokens", "source_snapshot", "input_blocked"}:
                raise ValueError("invalid node fields")
            node_id = _identifier(node.get("id"), "node id")
            if node_id in seen:
                raise ValueError("node ids must be unique")
            seen.add(node_id)
            depends = node.get("depends_on")
            if not isinstance(depends, list) or any(not isinstance(item, str) for item in depends) or len(set(depends)) != len(depends):
                raise ValueError("depends_on must be a unique list of node identifiers")
            for dependency in depends:
                _identifier(dependency, "dependency id")
                if dependency == node_id:
                    raise ValueError("a node cannot depend on itself")
            if not isinstance(node.get("job"), dict):
                raise ValueError("job must be an object")
            if len(_json(node["job"])) > 12000:
                raise ValueError("job JSON must be at most 12000 characters")
            references = node.get("acceptance_refs")
            if not isinstance(references, list) or not references or any(not isinstance(item, str) or not item.strip() for item in references):
                raise ValueError("acceptance_refs must be a nonempty list of text")
            if len(_json(references)) > 6000:
                raise ValueError("acceptance_refs JSON must be at most 6000 characters")
            estimate = node.get("estimated_tokens")
            if estimate is not None and (type(estimate) is not int or estimate < 0):
                raise ValueError("estimated_tokens must be a nonnegative integer or null")
            source = node.get("source_snapshot")
            if source is not None:
                # Validate strict fingerprint shape now; it will be rehashed before claim.
                _json(source)
            blocked = node.get("input_blocked", False)
            if type(blocked) is not bool:
                raise ValueError("input_blocked must be boolean")
            normalized.append({"id": node_id, "depends_on": depends, "job": node["job"],
                               "acceptance_refs": references, "estimated_tokens": estimate,
                               "source_snapshot": source, "input_blocked": blocked})
        for node in normalized:
            if any(dep not in seen for dep in node["depends_on"]):
                raise ValueError("dependencies must name submitted nodes")
        visiting, complete = set(), set()
        by_id = {node["id"]: node for node in normalized}
        for node_id in by_id:
            if node_id in complete:
                continue
            stack = [(node_id, False)]
            while stack:
                current, leaving = stack.pop()
                if leaving:
                    visiting.remove(current); complete.add(current); continue
                if current in complete:
                    continue
                if current in visiting:
                    raise ValueError("graph contains a cycle")
                visiting.add(current)
                stack.append((current, True))
                stack.extend((dependency, False) for dependency in by_id[current]["depends_on"])
        return normalized

    def submit(self, graph_id: str, nodes: list[dict[str, Any]]) -> None:
        graph_id = _identifier(graph_id, "graph id")
        nodes = self._validate_nodes(nodes)
        for node in nodes:
            if node["source_snapshot"] is not None:
                # ``check_fingerprint`` validates the supplied snapshot before it
                # becomes immutable queue state; claim repeats it immediately
                # before granting a lease.
                check_fingerprint(self.project, node["source_snapshot"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("SELECT 1 FROM graphs WHERE graph_id = ?", (graph_id,)).fetchone():
                    raise ValueError("graph is immutable and already exists")
                db.execute("INSERT INTO graphs VALUES (?, ?)", (graph_id, time.time()))
                for node in nodes:
                    status = "ready" if not node["depends_on"] else "pending"
                    db.execute("INSERT INTO tasks(graph_id,node_id,job_json,acceptance_json,estimated_tokens,source_json,input_blocked,status) VALUES (?,?,?,?,?,?,?,?)",
                               (graph_id, node["id"], _json(node["job"]), _json(node["acceptance_refs"]), node["estimated_tokens"],
                                _json(node["source_snapshot"]) if node["source_snapshot"] is not None else None, int(node["input_blocked"]), status))
                for node in nodes:
                    for dep in node["depends_on"]:
                        db.execute("INSERT INTO dependencies VALUES (?,?,?)", (graph_id, node["id"], dep))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def _config(self):
        return load_config(self.project, self.kit_root)

    def _budget(self, db: sqlite3.Connection) -> tuple[int, bool, int]:
        rows = db.execute("SELECT observed_tokens, usage_known FROM attempts WHERE closed=1").fetchall()
        observed = sum(row["observed_tokens"] or 0 for row in rows)
        known = all(row["usage_known"] for row in rows)
        reserved = db.execute("SELECT COALESCE(SUM(estimated_tokens),0) FROM tasks WHERE status = 'running'").fetchone()[0]
        return observed, known, reserved

    def _refresh_dependents(self, db: sqlite3.Connection, graph_id: str) -> None:
        db.execute("UPDATE tasks SET status='blocked', block_reason='failed_dependency' WHERE graph_id=? AND status IN ('pending','ready') AND EXISTS (SELECT 1 FROM dependencies d JOIN tasks parent ON parent.graph_id=d.graph_id AND parent.node_id=d.depends_on WHERE d.graph_id=tasks.graph_id AND d.node_id=tasks.node_id AND parent.status IN ('failed','cancelled','blocked'))", (graph_id,))
        db.execute("UPDATE tasks SET status='ready' WHERE graph_id=? AND status='pending' AND NOT EXISTS (SELECT 1 FROM dependencies d JOIN tasks parent ON parent.graph_id=d.graph_id AND parent.node_id=d.depends_on WHERE d.graph_id=tasks.graph_id AND d.node_id=tasks.node_id AND parent.status != 'verified')", (graph_id,))

    def claim(self, graph_id: str, owner: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        graph_id, owner = _identifier(graph_id, "graph id"), _identifier(owner, "owner")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86400:
            raise ValueError("lease_seconds must be an integer from 1 to 86400")
        policy = fingerprint_paths(self.project,[".orchestra/project.toml"])
        config = self._config()
        if check_fingerprint(self.project,policy):
            raise ValueError("configuration changed before queue admission")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._refresh_dependents(db, graph_id)
                if not db.execute("SELECT 1 FROM graphs WHERE graph_id=?", (graph_id,)).fetchone():
                    raise ValueError("unknown graph")
                candidates = db.execute("SELECT * FROM tasks WHERE graph_id=? AND status='ready' ORDER BY rowid", (graph_id,)).fetchall()
                for row in candidates:
                    if row["input_blocked"]:
                        db.execute("UPDATE tasks SET status='blocked', block_reason='input_blocked' WHERE graph_id=? AND node_id=?", (graph_id, row["node_id"])); continue
                    if config.workflow.require_fresh_evidence and not row["source_json"]:
                        db.execute("UPDATE tasks SET status='blocked', block_reason='source_snapshot_required' WHERE graph_id=? AND node_id=?", (graph_id, row["node_id"])); continue
                    if row["source_json"]:
                        try:
                            clues = check_fingerprint(self.project, json.loads(row["source_json"]))
                        except (ValueError, json.JSONDecodeError, RecursionError):
                            db.execute("UPDATE tasks SET status='blocked', block_reason='source_snapshot_invalid' WHERE graph_id=? AND node_id=?", (graph_id, row["node_id"])); continue
                        if clues:
                            db.execute("UPDATE tasks SET status='blocked', block_reason=? WHERE graph_id=? AND node_id=?", ("source_stale:" + ",".join(clues), graph_id, row["node_id"])); continue
                    running = db.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
                    if running >= config.max_parallel:
                        break
                    observed, known, reserved = self._budget(db)
                    limit = config.workflow.max_observed_tokens
                    launches = db.execute("SELECT COALESCE(SUM(launches),0) FROM tasks").fetchone()[0]
                    max_launches = getattr(config.workflow, "max_launches", None)
                    if (max_launches is not None and launches >= max_launches) or (limit is not None and not known):
                        break
                    estimate = row["estimated_tokens"]
                    if limit is not None and (estimate is None or observed + reserved + estimate > limit):
                        continue
                    token, expires = uuid4().hex, time.time() + lease_seconds
                    db.execute("UPDATE tasks SET status='running', owner=?, lease_token=?, lease_expires=?, launches=launches+1 WHERE graph_id=? AND node_id=?", (owner, token, expires, graph_id, row["node_id"]))
                    db.execute("INSERT INTO attempts(graph_id,node_id,attempt) VALUES (?,?,?)", (graph_id, row["node_id"], row["launches"] + 1))
                    if check_fingerprint(self.project,policy):
                        raise ValueError("configuration changed during queue admission")
                    db.execute("COMMIT")
                    return {"id": row["node_id"], "job": json.loads(row["job_json"]), "acceptance_refs": json.loads(row["acceptance_json"]), "estimated_tokens": estimate, "lease_token": token, "lease_expires": expires}
                if check_fingerprint(self.project,policy):
                    raise ValueError("configuration changed during queue admission")
                db.execute("COMMIT"); return None
            except Exception:
                db.execute("ROLLBACK")
                raise

    def _lease(self, db: sqlite3.Connection, graph_id: str, node_id: str, owner: str, token: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM tasks WHERE graph_id=? AND node_id=?", (graph_id, node_id)).fetchone()
        if row is None or row["status"] != "running" or row["owner"] != owner or row["lease_token"] != token or row["lease_expires"] < time.time():
            raise ValueError("lease is stale or not owned by caller")
        return row

    def heartbeat(self, graph_id: str, node_id: str, owner: str, lease_token: str, lease_seconds: int = 60) -> dict[str, Any]:
        graph_id, node_id, owner = _identifier(graph_id, "graph id"), _identifier(node_id, "node id"), _identifier(owner, "owner")
        if not isinstance(lease_token, str) or not lease_token or type(lease_seconds) is not int or not 1 <= lease_seconds <= 86400:
            raise ValueError("invalid lease heartbeat")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._lease(db, graph_id, node_id, owner, lease_token)
                expires = time.time() + lease_seconds
                db.execute("UPDATE tasks SET lease_expires=? WHERE graph_id=? AND node_id=?", (expires, graph_id, node_id))
                db.execute("COMMIT"); return {"lease_expires": expires}
            except Exception:
                db.execute("ROLLBACK"); raise

    def finish(self, graph_id: str, node_id: str, owner: str, lease_token: str, status: str, evidence_ref: str | None, observed_tokens: int | None) -> None:
        graph_id, node_id, owner = _identifier(graph_id, "graph id"), _identifier(node_id, "node id"), _identifier(owner, "owner")
        if status not in _TERMINAL or (status == "verified" and (not isinstance(evidence_ref, str) or not evidence_ref.strip())):
            raise ValueError("invalid terminal status or missing verification evidence")
        if status != "verified" and evidence_ref is not None and (not isinstance(evidence_ref, str) or not evidence_ref.strip()):
            raise ValueError("evidence_ref must be nonempty text when set")
        if observed_tokens is not None and (type(observed_tokens) is not int or observed_tokens < 0):
            raise ValueError("observed_tokens must be a nonnegative integer or null")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._lease(db, graph_id, node_id, owner, lease_token)
                final = "cancelled" if row["cancel_requested"] else status
                db.execute("UPDATE tasks SET status=?, evidence_ref=?, observed_tokens=?, usage_known=?, owner=NULL, lease_token=NULL, lease_expires=NULL WHERE graph_id=? AND node_id=?", (final, evidence_ref, observed_tokens, int(observed_tokens is not None), graph_id, node_id))
                db.execute("UPDATE attempts SET observed_tokens=?, usage_known=?, closed=1 WHERE graph_id=? AND node_id=? AND attempt=?", (observed_tokens, int(observed_tokens is not None), graph_id, node_id, row["launches"]))
                self._refresh_dependents(db, graph_id)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK"); raise

    def recover(self, graph_id: str, node_id: str, *, execution_absent: bool, descendants_absent: bool, authorized: bool, disposition: str) -> None:
        graph_id, node_id = _identifier(graph_id, "graph id"), _identifier(node_id, "node id")
        if any(type(flag) is not bool for flag in (execution_absent, descendants_absent, authorized)) or not execution_absent or not descendants_absent or not authorized:
            raise ValueError("recovery requires authorized true evidence that execution and descendants are absent")
        if disposition not in {"blocked", "cancelled", "retry"}:
            raise ValueError("invalid recovery disposition")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM tasks WHERE graph_id=? AND node_id=?", (graph_id, node_id)).fetchone()
                if row is None or row["status"] != "running" or row["lease_expires"] >= time.time():
                    raise ValueError("recovery requires an expired running lease")
                if row["cancel_requested"] and disposition == "retry":
                    raise ValueError("cancel-requested work cannot be retried during recovery")
                status = "ready" if disposition == "retry" else disposition
                db.execute("UPDATE tasks SET status=?, block_reason=?, owner=NULL, lease_token=NULL, lease_expires=NULL, usage_known=0 WHERE graph_id=? AND node_id=?", (status, None if disposition == "retry" else "recovered", graph_id, node_id))
                db.execute("UPDATE attempts SET usage_known=0, closed=1 WHERE graph_id=? AND node_id=? AND attempt=?", (graph_id, node_id, row["launches"]))
                self._refresh_dependents(db, graph_id)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK"); raise

    def cancel(self, graph_id: str, node_id: str) -> None:
        graph_id, node_id = _identifier(graph_id, "graph id"), _identifier(node_id, "node id")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT status FROM tasks WHERE graph_id=? AND node_id=?", (graph_id, node_id)).fetchone()
                if row is None:
                    raise ValueError("unknown queue task")
                if row["status"] in {"pending", "ready"}:
                    db.execute("UPDATE tasks SET status='cancelled', block_reason='cancelled' WHERE graph_id=? AND node_id=?", (graph_id, node_id))
                    self._refresh_dependents(db, graph_id)
                elif row["status"] == "running":
                    db.execute("UPDATE tasks SET cancel_requested=1 WHERE graph_id=? AND node_id=?", (graph_id, node_id))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK"); raise

    def inspect(self, graph_id: str) -> dict[str, Any]:
        graph_id = _identifier(graph_id, "graph id")
        config = self._config()
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM graphs WHERE graph_id=?", (graph_id,)).fetchone():
                raise ValueError("unknown graph")
            observed, known, reserved = self._budget(db)
            count = db.execute("SELECT COALESCE(SUM(launches),0) FROM tasks").fetchone()[0]
            tasks = []
            for row in db.execute("SELECT * FROM tasks WHERE graph_id=? ORDER BY rowid", (graph_id,)):
                tasks.append({"id": row["node_id"], "status": row["status"], "block_reason": row["block_reason"], "cancel_requested": bool(row["cancel_requested"]), "lease_expires": row["lease_expires"], "launches": row["launches"], "observed_tokens": row["observed_tokens"], "acceptance_refs": json.loads(row["acceptance_json"]), "source_snapshot": json.loads(row["source_json"]) if row["source_json"] else None})
            return {"graph_id": graph_id, "tasks": tasks, "budget": {"limit": config.workflow.max_observed_tokens, "observed_tokens": observed, "reserved_tokens": reserved, "coverage": "complete" if known else "unknown"}, "launches": {"limit": getattr(config.workflow, "max_launches", None), "count": count}}
