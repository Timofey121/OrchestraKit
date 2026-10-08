"""Content fingerprints for the exact project files covered by review evidence.

Snapshots deliberately contain paths and digests only.  The public functions
accept at most 256 explicit, project-relative file names; they never expand
directories or globs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from pathlib import Path
from typing import Any, Iterable


SCHEMA = 1
MAX_PATHS = 256
_HASH_BYTES = 128 * 1024


class _UnreadableFile(Exception):
    pass


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


def _paths(paths: Iterable[str]) -> tuple[str, ...]:
    if isinstance(paths, (str, bytes)):
        raise ValueError("paths must be an iterable of path strings")
    try:
        iterator = iter(paths)
    except TypeError as exc:
        raise ValueError("paths must be an iterable of path strings") from exc
    values: list[str] = []
    for _ in range(MAX_PATHS + 1):
        try:
            value = next(iterator)
        except StopIteration:
            break
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("paths must be nonempty strings")
        candidate = Path(value)
        if candidate.is_absolute() or candidate.as_posix() != value or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("paths must be normalized project-relative file names")
        if candidate == Path(".") or value.endswith(("/", "\\")):
            raise ValueError("paths must name files")
        values.append(value)
    else:
        raise ValueError(f"paths may contain at most {MAX_PATHS} entries")
    if not values:
        raise ValueError("paths must not be empty")
    return tuple(sorted(set(values)))


def _path(root: Path, relative: str) -> Path:
    # _paths has excluded traversal.  This resolved-parent check also prevents
    # a parent directory symlink from taking a spelling outside the project.
    path = root / relative
    try:
        parent = path.parent.resolve(strict=False)
    except OSError as exc:
        raise _UnreadableFile from exc
    if parent != root and root not in parent.parents:
        raise ValueError("path escapes project")
    return path


def _metadata(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _state(root: Path, relative: str) -> dict[str, str]:
    path = _path(root, relative)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"state": "missing"}
    except OSError as exc:
        raise _UnreadableFile from exc
    if not stat.S_ISREG(before.st_mode):
        raise _UnreadableFile
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _UnreadableFile from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _metadata(before) != _metadata(opened):
            raise _UnreadableFile
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _HASH_BYTES):
            digest.update(chunk)
        after_opened = os.fstat(descriptor)
        after = path.lstat()
    except OSError as exc:
        raise _UnreadableFile from exc
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(after.st_mode) or _metadata(before) != _metadata(after_opened) or _metadata(before) != _metadata(after):
        raise _UnreadableFile
    return {"state": "regular", "sha256": digest.hexdigest()}


def _aggregate(files: dict[str, dict[str, str]]) -> str:
    payload = {"schema": SCHEMA, "files": files}
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def fingerprint_paths(project: Path, paths: Iterable[str]) -> dict[str, Any]:
    """Return a deterministic digest snapshot for explicit paths in *project*.

    Missing names are represented so a later created file invalidates review
    evidence.  Unreadable files, symlinks, directories, and unsafe names fail.
    """
    root = _project_root(project)
    names = _paths(paths)
    files: dict[str, dict[str, str]] = {}
    for relative in names:
        try:
            files[relative] = _state(root, relative)
        except _UnreadableFile as exc:
            raise ValueError(f"unreadable file: {relative}") from exc
    return {"schema": SCHEMA, "files": files, "aggregate_sha256": _aggregate(files)}


def _snapshot(snapshot: Any) -> dict[str, dict[str, str]]:
    if not isinstance(snapshot, dict) or set(snapshot) != {"schema", "files", "aggregate_sha256"}:
        raise ValueError("invalid fingerprint snapshot")
    if type(snapshot["schema"]) is not int or snapshot["schema"] != SCHEMA or not isinstance(snapshot["files"], dict):
        raise ValueError("invalid fingerprint snapshot")
    names = _paths(snapshot["files"].keys())
    files = snapshot["files"]
    if tuple(files) != names:
        raise ValueError("fingerprint paths must be sorted and unique")
    for relative, state_value in files.items():
        if not isinstance(state_value, dict):
            raise ValueError("invalid fingerprint file state")
        if state_value == {"state": "missing"}:
            continue
        if set(state_value) != {"state", "sha256"} or state_value.get("state") != "regular":
            raise ValueError("invalid fingerprint file state")
        digest = state_value.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid fingerprint file digest")
    aggregate = snapshot["aggregate_sha256"]
    if not isinstance(aggregate, str) or len(aggregate) != 64 or any(char not in "0123456789abcdef" for char in aggregate):
        raise ValueError("invalid fingerprint aggregate")
    if not hmac.compare_digest(aggregate, _aggregate(files)):
        raise ValueError("fingerprint aggregate does not match file states")
    return files


def check_fingerprint(project: Path, snapshot: Any) -> tuple[str, ...]:
    """Rehash a strict snapshot and return stable change clues.

    Each clue is ``changed:<path>``, ``missing:<path>``, or
    ``unreadable:<path>``.  A malformed or internally inconsistent snapshot is
    rejected instead of being trusted.
    """
    root = _project_root(project)
    previous = _snapshot(snapshot)
    clues: list[str] = []
    for relative, old_state in previous.items():
        try:
            current = _state(root, relative)
        except (_UnreadableFile, ValueError):
            clues.append(f"unreadable:{relative}")
            continue
        if current == old_state:
            continue
        if old_state["state"] == "regular" and current["state"] == "missing":
            clues.append(f"missing:{relative}")
        else:
            clues.append(f"changed:{relative}")
    return tuple(clues)
