"""Conservative local reuse for passing source-bound verification evidence.

This module is deliberately a cache, never an authority.  Every lookup
rehashes its declared subjects and revalidates the referenced evidence
certificate and its check/review artifacts before returning a hit.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Iterable

from .evidence import verify_evidence
from .check_execution import validate_checks
from .fingerprint import check_fingerprint, fingerprint_paths


_SCHEMA = 1
_CONTEXT_FIELDS = frozenset(
    {"job", "acceptance", "checks", "environment", "dependencies", "settings"}
)
_MAX_CONTEXT_BYTES = 64 * 1024
_MAX_CACHE_BYTES = 256 * 1024
_MAX_CONFIG_BYTES = 512 * 1024


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


def _json_value(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> Any:
    """Return only plain JSON values, rejecting arbitrary serializable objects."""
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if depth > 24 or nodes[0] > 4096:
        raise ValueError("context is too deeply nested or large")
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("context contains a non-finite number")
        return value
    if isinstance(value, list):
        return [_json_value(item, depth=depth + 1, nodes=nodes) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 512:
                raise ValueError("context keys must be nonempty strings")
            result[key] = _json_value(item, depth=depth + 1, nodes=nodes)
        return result
    raise ValueError("context must contain only JSON values")


def _context(value: Any) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, dict) or not value or not _CONTEXT_FIELDS.issubset(value):
        raise ValueError(
            "context must explicitly include job, acceptance, checks, environment, dependencies, and settings"
        )
    normalized = _json_value(value)
    assert isinstance(normalized, dict)
    if not isinstance(normalized["job"], dict) or not normalized["job"]:
        raise ValueError("context job must be a nonempty object")
    criteria = normalized["acceptance"]
    if (not isinstance(criteria, (str, list, dict)) or not criteria
            or isinstance(criteria, str) and not criteria.strip()):
        raise ValueError("context acceptance must contain explicit criteria")
    if not isinstance(normalized["environment"], dict) or not normalized["environment"]:
        raise ValueError("context environment must be a nonempty object")
    if not isinstance(normalized["settings"], dict) or not normalized["settings"]:
        raise ValueError("context settings must be a nonempty object")
    dependencies = normalized["dependencies"]
    if not isinstance(dependencies, (list, dict)):
        raise ValueError("context dependencies must be an explicit list or object")
    try:
        normalized["checks"] = validate_checks(normalized["checks"])
    except ValueError as exc:
        raise ValueError("context checks must be a nonempty validated check list") from exc
    try:
        encoded = json.dumps(
            normalized, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ValueError("context must be a JSON object") from exc
    if len(encoded) > _MAX_CONTEXT_BYTES:
        raise ValueError("context is too large")
    return normalized, encoded


def _certificate_checks(certificate: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the exact command identity that produced passing evidence."""
    checks = certificate.get("checks")
    if not isinstance(checks, list):
        raise ValueError("evidence lacks checks")
    try:
        return validate_checks([
            {"argv": check["argv"], "failure_cause": check["failure_cause"]}
            for check in checks
        ])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("evidence has invalid checks") from exc


def _ordinary(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"missing required cache path: {path.name}") from exc
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if path.is_symlink() or not expected:
        raise ValueError(f"unsafe cache path: {path.name}")


def _config_policy_paths(root: Path) -> list[str]:
    orchestra = root / ".orchestra"
    _ordinary(orchestra, directory=True)
    config = orchestra / "project.toml"
    _ordinary(config, directory=False)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(config, flags)
    except OSError as exc:
        raise ValueError("configuration cannot be safely read") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("configuration must be a regular file")
        data = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > _MAX_CONFIG_BYTES:
        raise ValueError("configuration is too large")
    try:
        raw = tomllib.loads(data.decode("utf-8"))
        context = raw.get("context", {})
        policies = context.get("policy_files", []) if isinstance(context, dict) else None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("configuration cannot be parsed") from exc
    if not isinstance(policies, list) or not all(isinstance(item, str) and item for item in policies):
        raise ValueError("configuration has invalid policy files")
    if len(policies) > 64 or len(set(policies)) != len(policies):
        raise ValueError("configuration has invalid policy files")
    for relative in policies:
        candidate = Path(relative)
        if (
            candidate.is_absolute()
            or candidate.as_posix() != relative
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or candidate.suffix.lower() != ".md"
        ):
            raise ValueError("configuration has unsafe policy file")
    return [".orchestra/project.toml", *policies]


def _subjects(project: Path, files: Iterable[str]) -> tuple[Path, dict[str, Any]]:
    root = _root(project)
    # Ask fingerprint_paths to normalize and validate caller-declared paths
    # before adding project-owned verification policy files.
    declared = fingerprint_paths(root, files)
    names = [*declared["files"], *_config_policy_paths(root)]
    return root, fingerprint_paths(root, names)


def _key(kind: str, context: dict[str, Any], snapshot: dict[str, Any]) -> str:
    if kind not in {"check", "review"}:
        raise ValueError("kind must be check or review")
    payload = {"schema": _SCHEMA, "kind": kind, "context": context, "snapshot": snapshot}
    encoded = json.dumps(payload, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _context_digest(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _cache_directory(root: Path, kind: str) -> Path:
    orchestra = root / ".orchestra"
    _ordinary(orchestra, directory=True)
    cache = orchestra / "verification-cache"
    if cache.exists() or cache.is_symlink():
        _ordinary(cache, directory=True)
    else:
        cache.mkdir(mode=0o700)
        _ordinary(cache, directory=True)
    target = cache / kind
    if target.exists() or target.is_symlink():
        _ordinary(target, directory=True)
    else:
        target.mkdir(mode=0o700)
        _ordinary(target, directory=True)
    return target


def _cache_path(root: Path, kind: str, key: str, *, create: bool) -> tuple[Path, str]:
    if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
        raise ValueError("invalid cache key")
    directory = _cache_directory(root, kind) if create else root / ".orchestra" / "verification-cache" / kind
    if not create:
        orchestra = root / ".orchestra"
        cache = orchestra / "verification-cache"
        if not directory.exists():
            raise ValueError("cache is unavailable")
        # Check every path component: checking only ``kind`` would still let a
        # parent directory symlink redirect a supposedly private cache read.
        _ordinary(orchestra, directory=True)
        _ordinary(cache, directory=True)
        _ordinary(directory, directory=True)
    relative = f".orchestra/verification-cache/{kind}/{key}.json"
    return directory / f"{key}.json", relative


def _read(path: Path) -> dict[str, Any] | None:
    try:
        _ordinary(path, directory=False)
        if path.stat().st_size > _MAX_CACHE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _record(
    value: Any, *, key: str, kind: str, context_digest: str, snapshot: dict[str, Any]
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {
        "schema", "kind", "cache_key", "context_sha256", "snapshot", "evidence_ref"
    }:
        return None
    if value.get("schema") != _SCHEMA or value.get("kind") != kind or value.get("cache_key") != key:
        return None
    if (
        not isinstance(value.get("evidence_ref"), str)
        or not isinstance(value.get("context_sha256"), str)
        or value["context_sha256"] != context_digest
    ):
        return None
    if value.get("snapshot") != snapshot:
        return None
    return value


def _hit(root: Path, *, key: str, kind: str, context: dict[str, Any],
         context_digest: str, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    try:
        path, relative = _cache_path(root, kind, key, create=False)
    except ValueError:
        return None
    record = _record(
        _read(path), key=key, kind=kind, context_digest=context_digest, snapshot=snapshot
    )
    if record is None:
        return None
    try:
        if check_fingerprint(root, snapshot):
            return None
        certificate = verify_evidence(
            root, record["evidence_ref"], kind=kind, required_files=list(snapshot["files"])
        )
        if _certificate_checks(certificate) != context["checks"]:
            return None
    except (ValueError, OSError, KeyError, TypeError):
        return None
    return {
        "reused": True,
        "kind": kind,
        "evidence_ref": record["evidence_ref"],
        "cache_key": key,
        "cache_ref": relative,
        "subjects": list(snapshot["files"]),
    }


def find_verification(project: Path, *, files: Iterable[str], context: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """Return current matching passing evidence, or ``None`` without authority."""
    try:
        root, snapshot = _subjects(project, files)
        normalized, encoded = _context(context)
        key = _key(kind, normalized, snapshot)
        return _hit(
            root, key=key, kind=kind, context=normalized,
            context_digest=_context_digest(encoded), snapshot=snapshot,
        )
    except (ValueError, OSError, TypeError):
        return None


def _write(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".verification-", suffix=".json", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Replacing a malicious symlink replaces the link itself; it never
        # follows it.  The directory was checked above and remains private.
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def store_verification(
    project: Path, *, files: Iterable[str], context: dict[str, Any], evidence_ref: str, kind: str
) -> dict[str, Any]:
    """Validate and cache existing passing evidence for an exact future lookup."""
    root, snapshot = _subjects(project, files)
    normalized, encoded = _context(context)
    key = _key(kind, normalized, snapshot)
    context_digest = _context_digest(encoded)
    subjects = list(snapshot["files"])
    # The certificate is checked before it reaches the cache, and again on each
    # hit.  Cache metadata alone can therefore never grant a passing result.
    certificate = verify_evidence(root, evidence_ref, kind=kind, required_files=subjects)
    if _certificate_checks(certificate) != normalized["checks"]:
        raise ValueError("context checks do not match the verified evidence checks")
    if check_fingerprint(root, snapshot):
        raise ValueError("verification subjects changed while storing cache entry")
    existing = _hit(
        root, key=key, kind=kind, context=normalized,
        context_digest=context_digest, snapshot=snapshot,
    )
    if existing is not None:
        return {**existing, "stored": False}
    path, relative = _cache_path(root, kind, key, create=True)
    payload = {
        "schema": _SCHEMA,
        "kind": kind,
        "cache_key": key,
        "context_sha256": context_digest,
        "snapshot": snapshot,
        "evidence_ref": evidence_ref,
    }
    _write(path, payload)
    # Do not return success until the just-written record proves itself usable.
    hit = _hit(
        root, key=key, kind=kind, context=normalized,
        context_digest=context_digest, snapshot=snapshot,
    )
    if hit is None:
        raise ValueError("verification cache entry could not be validated")
    return {**hit, "stored": True, "reused": False}
