"""Durable, bounded file checkpoints for guarded post-review repairs.

This module only preserves explicitly named project files.  It deliberately
does not discover files, resume interrupted workflows, or decide whether a
repair should be rolled back.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from .file_metadata import metadata_signature, stat_matches_fstat
from .fingerprint import MAX_PATHS, _paths, _project_root, _snapshot, check_fingerprint, fingerprint_paths


SCHEMA = 1
MAX_BYTES = 64 * 1024 * 1024
_CHUNK = 128 * 1024
_MANIFEST = "manifest.json"


def _hex_digest(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _reject_symlink_components(root: Path, relative: str, *, include_target: bool = True) -> Path:
    current = root
    parts = Path(relative).parts
    selected = parts if include_target else parts[:-1]
    for part in selected:
        current = current / part
        try:
            if current.is_symlink():
                raise ValueError(f"symlinked path component: {relative}")
        except OSError as exc:
            raise ValueError(f"unreadable path component: {relative}") from exc
    return root / relative


def _workflow_root(root: Path) -> Path:
    relative = ".orchestra/workflows"
    workflows = _reject_symlink_components(root, relative)
    if not workflows.is_dir():
        raise ValueError("workflow directory must be an existing real directory")
    return workflows


def _checkpoint_directory(root: Path, directory: Path, *, new: bool) -> tuple[Path, str]:
    if not isinstance(directory, Path):
        raise ValueError("checkpoint directory must be a Path")
    workflows = _workflow_root(root)
    spelling = directory if directory.is_absolute() else root / directory
    lexical_workflows = None
    try:
        for ancestor in (spelling, *spelling.parents):
            if ancestor.resolve(strict=False) == workflows:
                lexical_workflows = ancestor
                break
        if lexical_workflows is None:
            raise ValueError
        relative = spelling.relative_to(lexical_workflows)
        candidate = workflows.joinpath(*relative.parts)
    except (OSError, ValueError) as exc:
        raise ValueError("checkpoint directory must be beneath .orchestra/workflows") from exc
    if (not relative.parts or any(part in {"", ".", ".."} or "\x00" in part for part in relative.parts)
            or candidate.name in {"", ".", ".."}):
        raise ValueError("checkpoint directory must be a normalized child of .orchestra/workflows")
    current = workflows
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("checkpoint directory must not contain symlinks")
    if new:
        if candidate.exists():
            raise ValueError("checkpoint directory must be new")
    elif not candidate.is_dir():
        raise ValueError("checkpoint directory must be a real directory")
    return candidate, str(candidate)


def _create_private_directory(workflows: Path, target: Path) -> None:
    relative = target.relative_to(workflows)
    current = workflows
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("checkpoint parent must not be a symlink")
        if current.exists():
            if not current.is_dir():
                raise ValueError("checkpoint parent must be a directory")
        else:
            current.mkdir(mode=0o700)
            os.chmod(current, 0o700)
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)


def _stable_regular(path: Path, *, limit: int, label: str) -> tuple[bytes, int]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError(f"unreadable {label}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if before.st_size > limit:
        raise ValueError(f"{label} exceeds byte size limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"unreadable {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not stat_matches_fstat(before, opened):
            raise ValueError(f"unstable {label}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_CHUNK, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError(f"{label} exceeds byte limit")
            chunks.append(chunk)
        after_opened = os.fstat(descriptor)
        after = path.lstat()
    except OSError as exc:
        raise ValueError(f"unreadable {label}") from exc
    finally:
        os.close(descriptor)
    if (not stat.S_ISREG(after.st_mode)
            or metadata_signature(before) != metadata_signature(after)
            or metadata_signature(opened) != metadata_signature(after_opened)
            or not stat_matches_fstat(after, after_opened)):
        raise ValueError(f"unstable {label}")
    return b"".join(chunks), stat.S_IMODE(before.st_mode) & 0o777


def _exclusive_bytes(path: Path, content: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, mode)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest_bytes(core: dict[str, Any]) -> bytes:
    return (json.dumps(core, ensure_ascii=True, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("ascii")


def capture_checkpoint(
    project: Path,
    files: list[str],
    directory: Path,
    *,
    max_bytes: int = MAX_BYTES,
) -> dict[str, Any]:
    """Persist a bounded snapshot of explicit files for a possible rollback."""
    if type(max_bytes) is not int or max_bytes < 0 or max_bytes > MAX_BYTES:
        raise ValueError(f"max_bytes must be between 0 and {MAX_BYTES}")
    root = _project_root(project)
    names = _paths(files)
    if len(names) > MAX_PATHS:  # Defensive even though _paths enforces this.
        raise ValueError(f"files may contain at most {MAX_PATHS} entries")
    for relative in names:
        _reject_symlink_components(root, relative)
    snapshot = fingerprint_paths(root, names)
    target, relative_directory = _checkpoint_directory(root, directory, new=True)
    _create_private_directory(_workflow_root(root), target)

    entries: list[dict[str, Any]] = []
    total = 0
    for index, relative in enumerate(names):
        state = snapshot["files"][relative]
        if state["state"] == "missing":
            entries.append({"path": relative, "state": "missing"})
            continue
        path = _reject_symlink_components(root, relative)
        content, mode = _stable_regular(path, limit=max_bytes - total, label=f"checkpoint source: {relative}")
        total += len(content)
        digest = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(digest, state["sha256"]):
            raise ValueError(f"checkpoint source changed: {relative}")
        blob = f"{index:03d}.blob"
        _exclusive_bytes(target / blob, content, 0o600)
        entries.append({"path": relative, "state": "regular", "blob": blob,
                        "sha256": digest, "size": len(content), "mode": mode})

    if check_fingerprint(root, snapshot):
        raise ValueError("checkpoint sources changed during capture")
    core = {"schema": SCHEMA, "snapshot": snapshot, "directory": relative_directory, "entries": entries}
    encoded = _manifest_bytes(core)
    _exclusive_bytes(target / _MANIFEST, encoded, 0o600)
    _sync_directory(target)
    checkpoint = dict(core)
    checkpoint["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
    return checkpoint


def _validated_checkpoint(root: Path, checkpoint: dict[str, Any]) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(checkpoint, dict) or set(checkpoint) != {
        "schema", "snapshot", "directory", "entries", "manifest_sha256"
    }:
        raise ValueError("invalid checkpoint metadata")
    if type(checkpoint["schema"]) is not int or checkpoint["schema"] != SCHEMA:
        raise ValueError("invalid checkpoint schema")
    if not isinstance(checkpoint["directory"], str) or not checkpoint["directory"]:
        raise ValueError("invalid checkpoint directory")
    if not _hex_digest(checkpoint["manifest_sha256"]):
        raise ValueError("invalid checkpoint manifest digest")
    directory, relative_directory = _checkpoint_directory(root, Path(checkpoint["directory"]), new=False)
    if relative_directory != checkpoint["directory"]:
        raise ValueError("checkpoint directory must be normalized")
    snapshot_files = _snapshot(checkpoint["snapshot"])
    entries = checkpoint["entries"]
    if not isinstance(entries, list) or len(entries) != len(snapshot_files):
        raise ValueError("invalid checkpoint entries")
    paths: list[str] = []
    blobs: set[str] = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("invalid checkpoint entry")
        path = entry["path"]
        paths.append(path)
        state = snapshot_files.get(path)
        if entry == {"path": path, "state": "missing"}:
            if state != {"state": "missing"}:
                raise ValueError("checkpoint entry disagrees with snapshot")
            continue
        if set(entry) != {"path", "state", "blob", "sha256", "size", "mode"} or entry.get("state") != "regular":
            raise ValueError("invalid checkpoint entry")
        blob = entry["blob"]
        size = entry["size"]
        mode = entry["mode"]
        if (not isinstance(blob, str) or not blob or Path(blob).name != blob or blob in {_MANIFEST, ".", ".."}
                or "\x00" in blob or blob in blobs):
            raise ValueError("unsafe checkpoint backup path")
        if not _hex_digest(entry["sha256"]) or state != {"state": "regular", "sha256": entry["sha256"]}:
            raise ValueError("checkpoint entry disagrees with snapshot")
        if type(size) is not int or size < 0 or type(mode) is not int or mode < 0 or mode > 0o777:
            raise ValueError("invalid checkpoint backup metadata")
        total += size
        if total > MAX_BYTES:
            raise ValueError("checkpoint exceeds byte limit")
        blobs.add(blob)
    if tuple(paths) != tuple(snapshot_files):
        raise ValueError("checkpoint entries must match sorted snapshot paths")

    manifest = directory / _MANIFEST
    if manifest.is_symlink():
        raise ValueError("checkpoint manifest must not be a symlink")
    raw, _ = _stable_regular(manifest, limit=1024 * 1024, label="checkpoint manifest")
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), checkpoint["manifest_sha256"]):
        raise ValueError("checkpoint manifest was modified")
    try:
        persisted = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid checkpoint manifest") from exc
    core = {key: checkpoint[key] for key in ("schema", "snapshot", "directory", "entries")}
    if persisted != core or raw != _manifest_bytes(core):
        raise ValueError("checkpoint manifest does not match metadata")
    return directory, snapshot_files, entries


def _expected_files(root: Path, expected: dict[str, Any], names: tuple[str, ...]) -> dict[str, dict[str, str]]:
    files = _snapshot(expected)
    if tuple(files) != names:
        raise ValueError("expected snapshot must cover the same files")
    clues = check_fingerprint(root, expected)
    if clues:
        raise ValueError(f"current targets changed: {', '.join(clues)}")
    return files


def _recheck_remaining(root: Path, expected: dict[str, dict[str, str]], remaining: list[str]) -> None:
    if not remaining:
        return
    for relative in remaining:
        _reject_symlink_components(root, relative)
    current = fingerprint_paths(root, remaining)["files"]
    previous = {relative: expected[relative] for relative in remaining}
    if current != previous:
        raise ValueError("current targets changed during restore")


def _atomic_restore(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.repair-checkpoint.tmp")
    _exclusive_bytes(temporary, content, 0o600)
    try:
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def restore_checkpoint(project: Path, checkpoint: dict[str, Any], expected: dict[str, Any]) -> None:
    """Restore a checkpoint iff every backup and current target is unchanged."""
    root = _project_root(project)
    directory, saved_files, entries = _validated_checkpoint(root, checkpoint)
    names = tuple(saved_files)
    expected_files = _expected_files(root, expected, names)

    backups: dict[str, bytes] = {}
    total = 0
    for entry in entries:
        if entry["state"] == "missing":
            continue
        blob = directory / entry["blob"]
        if blob.is_symlink():
            raise ValueError("checkpoint backup must not be a symlink")
        content, _ = _stable_regular(blob, limit=entry["size"], label=f"checkpoint backup: {entry['blob']}")
        total += len(content)
        if (len(content) != entry["size"] or total > MAX_BYTES
                or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), entry["sha256"])):
            raise ValueError("checkpoint backup is damaged")
        backups[entry["path"]] = content

    # Revalidate the manifest after reading blobs, then all current targets.
    _validated_checkpoint(root, checkpoint)
    _recheck_remaining(root, expected_files, list(names))

    remaining = list(names)
    try:
        for entry in entries:
            relative = entry["path"]
            _recheck_remaining(root, expected_files, remaining)
            path = _reject_symlink_components(root, relative)
            if entry["state"] == "missing":
                if path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise ValueError(f"unsafe restore target: {relative}")
                    path.unlink()
                    _sync_directory(path.parent)
            else:
                parent = _reject_symlink_components(root, relative, include_target=False).parent
                if not parent.is_dir() or parent.is_symlink():
                    raise ValueError(f"unsafe restore parent: {relative}")
                _atomic_restore(path, backups[relative], entry["mode"])
            remaining.remove(relative)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("checkpoint restore failed after validation") from exc

    clues = check_fingerprint(root, checkpoint["snapshot"])
    if clues:
        raise ValueError(f"checkpoint restore fingerprint mismatch: {', '.join(clues)}")
    for entry in entries:
        if entry["state"] == "regular":
            mode = stat.S_IMODE((root / entry["path"]).stat().st_mode) & 0o777
            if mode != entry["mode"]:
                raise ValueError(f"checkpoint restore mode mismatch: {entry['path']}")
