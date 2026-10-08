"""Create bounded git worktrees and inspect their declared write scope."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Iterable

from .config import load_config
from .fingerprint import check_fingerprint, fingerprint_paths


_RUNTIME_ARTIFACT_DIRS = (".orchestra/executions", ".orchestra/evidence", ".orchestra/checks")


def _run(argv: list[str], cwd: Path) -> str:
    try:
        return subprocess.run(argv, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"git command failed: {' '.join(argv[:3])}") from exc


def _relative_paths(paths: Iterable[str]) -> tuple[str, ...]:
    if isinstance(paths, (str, bytes)):
        raise ValueError("declared paths must be an iterable")
    values = []
    for value in paths:
        path = Path(value) if isinstance(value, str) else None
        if path is None or not value or "\0" in value or path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts) or path.parts[0] == ".git":
            raise ValueError("declared paths must be normalized project-relative paths")
        values.append(value)
    if not values:
        raise ValueError("declared paths must not be empty")
    return tuple(sorted(set(values)))


def _marker(destination: Path) -> Path:
    return destination.parent / f".{destination.name}.orchestra-owner.json"


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_target(worktree: Path, relative: str) -> Path:
    target = worktree / relative
    current = worktree
    for part in Path(relative).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("copied policy target contains a symlink")
        current.mkdir(exist_ok=True)
    if target.is_symlink():
        raise ValueError("copied policy target must not be a symlink")
    return target


def _nul_paths(argv: list[str], cwd: Path) -> set[str]:
    return {item for item in _run(argv, cwd).split("\0") if item}


def create_task_worktree(repo: Path, ref: str, destination: Path) -> dict[str, str]:
    """Create a detached worktree; callers retain responsibility for integration/removal."""
    if not isinstance(repo, Path) or not isinstance(destination, Path):
        raise ValueError("repo and destination must be Paths")
    if not isinstance(ref, str) or not ref or "\0" in ref or ref.startswith("-"):
        raise ValueError("ref must be a non-option git revision")
    root = repo.resolve(strict=True)
    if not root.is_dir() or _run(["git", "rev-parse", "--is-inside-work-tree"], root).strip() != "true":
        raise ValueError("repo must be a git worktree")
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination must be a new path beneath an existing directory")
    target = destination.resolve(strict=False)
    if target.exists() or target.is_symlink() or target.parent.is_symlink() or not target.parent.is_dir():
        raise ValueError("destination must be a new path beneath an existing directory")
    marker = _marker(target)
    if marker.exists() or marker.is_symlink():
        raise ValueError("private owner marker already exists")
    commit = _run(["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], root).strip()
    _run(["git", "worktree", "add", "--detach", str(target), commit], root)
    baseline_ignored = sorted(_nul_paths(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"], target))
    payload = {"schema": 1, "owner": str(uuid.uuid4()), "worktree": str(target), "base_commit": commit,
               "ignored_baseline": baseline_ignored}
    _write_marker(marker, payload)
    return {"worktree": str(target), "base_commit": commit, "owner_marker": str(marker), "integration_required": "root"}


def prepare_task_worktree(project: Path, kit_root: Path, ref: str, destination: Path) -> dict[str, Any]:
    """Create an isolated leaf checkout with only the parent execution policy copied in."""
    if not isinstance(project, Path) or not isinstance(kit_root, Path):
        raise ValueError("project and kit_root must be Paths")
    parent = project.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("project must be a directory")
    config = load_config(parent, kit_root.resolve(strict=True))
    sources = [".orchestra/project.toml", *config.context.policy_files]
    agents = parent / "AGENTS.md"
    if agents.is_symlink():
        raise ValueError("AGENTS.md must not be a symlink")
    if agents.exists():
        if not agents.is_file():
            raise ValueError("AGENTS.md must be a regular file")
        sources.append("AGENTS.md")
    parent_snapshot = fingerprint_paths(parent, sources)
    created = create_task_worktree(parent, ref, destination)
    worktree = Path(created["worktree"])
    target_before = fingerprint_paths(worktree, sources)
    for relative in sources:
        source = parent / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError("authoritative policy source must be a regular file")
        shutil.copyfile(source, _copy_target(worktree, relative))
    if check_fingerprint(parent, parent_snapshot):
        raise ValueError("authoritative policy changed while preparing task worktree")
    target_after = fingerprint_paths(worktree, sources)
    marker = _marker(worktree)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload.update({"parent_project":str(parent),"parent_policy_fingerprint": parent_snapshot, "copied_target_before": target_before,
                    "copied_target_fingerprint": target_after,
                    "managed_files": {name: state["sha256"] for name, state in target_after["files"].items()},
                    "runtime_artifact_dirs": list(_RUNTIME_ARTIFACT_DIRS)})
    _write_marker(marker, payload)
    return {**created, "parent_policy_fingerprint": parent_snapshot,
            "copied_target_before": target_before, "copied_target_fingerprint": target_after,
            "managed_files": payload["managed_files"], "runtime_artifact_dirs": list(_RUNTIME_ARTIFACT_DIRS)}


def check_write_scope(worktree: Path, declared_paths: Iterable[str]) -> dict[str, Any]:
    """Fail closed when tracked, untracked, or newly ignored paths exceed scope."""
    if not isinstance(worktree, Path):
        raise ValueError("worktree must be a Path")
    root = worktree.resolve(strict=True)
    allowed = _relative_paths(declared_paths)
    marker = _marker(root)
    try:
        marker_info = marker.lstat()
        if not stat.S_ISREG(marker_info.st_mode) or marker.parent.is_symlink():
            raise ValueError
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("private owner marker is required for scope checks") from exc
    if not isinstance(payload, dict) or payload.get("worktree") != str(root) or not isinstance(payload.get("ignored_baseline"), list):
        raise ValueError("invalid private owner marker")
    base = payload.get("base_commit")
    if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',base):
        raise ValueError("invalid private owner marker")
    managed_snapshot = payload.get("copied_target_fingerprint")
    managed_changed = () if managed_snapshot is None else check_fingerprint(root, managed_snapshot)
    managed_paths = set(payload.get("managed_files", {}))
    if managed_snapshot is not None and (not isinstance(payload.get("managed_files"), dict) or not managed_paths):
        raise ValueError("invalid private owner marker")
    runtime_dirs = payload.get("runtime_artifact_dirs", [])
    if not isinstance(runtime_dirs, list) or any(directory not in _RUNTIME_ARTIFACT_DIRS for directory in runtime_dirs):
        raise ValueError("invalid private owner marker")
    head = _run(["git", "rev-parse", "HEAD"], root).strip()
    tracked = _nul_paths(["git", "diff", "--name-only", "-z", base], root)
    untracked = _nul_paths(["git", "ls-files", "--others", "--exclude-standard", "-z"], root)
    ignored = _nul_paths(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"], root)
    new_ignored = ignored - set(payload["ignored_baseline"])
    raw_changed = tracked | untracked | new_ignored
    filtered = {path for path in raw_changed if path not in managed_paths and not any(path.startswith(directory + "/") for directory in runtime_dirs)}
    changed = sorted(filtered)
    outside = sorted(set(changed) - set(allowed))
    outside = sorted(set(outside) | {clue.split(":", 1)[1] for clue in managed_changed})
    if head != base:
        outside = sorted(set(outside) | {".git/HEAD"})
    return {"status": "out-of-scope" if outside else "within-scope", "declared_paths": list(allowed),
            "changed_paths": changed, "out_of_scope": outside, "base_commit": base,
            "base_commit_matches_head": head == base}
