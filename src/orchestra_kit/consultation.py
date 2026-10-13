"""Bounded, read-only strong consultation for an explicitly supplied question.

Consultation is advisory.  It cannot accept code, replace a review, or turn a
failed attempt into another worker run.  The native runner owns credentials,
cancellation, usage capture, and its read-only sandbox; this wrapper narrows
the supplied context and rejects a result that is not advice.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Sequence

from .config import load_config
from .evidence import verify_evidence
from .execution import run_job
from .fingerprint import check_fingerprint, fingerprint_paths
from .handoff import _reject_known_secrets, _relative_ref, _safe_attempt_artifact
from .routing import build_brief, route_task


_INPUT_FIELDS = frozenset({
    "question", "goal", "constraints", "acceptance", "files", "evidence_refs",
    "rejected_approaches",
})
_MAX_ITEMS = 32
_MAX_TEXT = 2_048
_MAX_ITEM_TEXT = 512
_ADVICE_CONTRACT = {
    "required": {"hypotheses": "array", "next_checks": "array"},
    "aliases": {},
}


def _text(value: Any, name: str, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be bounded nonempty text")
    return value


def _texts(value: Any, name: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or len(value) > _MAX_ITEMS:
        qualifier = "nonempty " if nonempty else ""
        raise ValueError(f"{name} must be a {qualifier}list with at most {_MAX_ITEMS} entries")
    return [_text(item, name, limit=_MAX_ITEM_TEXT) for item in value]


def _input(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _INPUT_FIELDS:
        raise ValueError("consultation input contains unsupported or missing fields")
    _reject_known_secrets(value)
    result = {
        "question": _text(value["question"], "question"),
        "goal": _text(value["goal"], "goal"),
        "constraints": _texts(value["constraints"], "constraints"),
        "acceptance": _texts(value["acceptance"], "acceptance"),
        "files": _texts(value["files"], "files"),
        # Consultation may start from an observed failure and pinned source
        # files.  Provided certificates are still verified, but a missing PASS
        # certificate is not itself a claim that the task has been accepted.
        "evidence_refs": _texts(value["evidence_refs"], "evidence_refs", nonempty=False),
        "rejected_approaches": value["rejected_approaches"],
    }
    if len(set(result["files"])) != len(result["files"]):
        raise ValueError("files must not contain duplicates")
    if len(set(result["evidence_refs"])) != len(result["evidence_refs"]):
        raise ValueError("evidence_refs must not contain duplicates")
    rejected = result["rejected_approaches"]
    if not isinstance(rejected, list) or not rejected or len(rejected) > _MAX_ITEMS:
        raise ValueError(f"rejected_approaches must be a nonempty list with at most {_MAX_ITEMS} entries")
    records: list[dict[str, str]] = []
    for record in rejected:
        if type(record) is not dict or set(record) != {"text", "reason", "artifact_ref"}:
            raise ValueError("rejected_approaches records require text, reason, and artifact_ref")
        records.append({
            "text": _text(record["text"], "rejected_approaches.text", limit=_MAX_ITEM_TEXT),
            "reason": _text(record["reason"], "rejected_approaches.reason", limit=_MAX_ITEM_TEXT),
            "artifact_ref": _relative_ref(
                record["artifact_ref"], "rejected_approaches.artifact_ref", attempt_artifact=True
            ),
        })
    result["rejected_approaches"] = records
    return result


def _source_paths(data: dict[str, Any]) -> list[str]:
    values = [*data["files"], *data["evidence_refs"],
              *(record["artifact_ref"] for record in data["rejected_approaches"]),
              ".orchestra/project.toml"]
    # Exact path identity prevents a race check from being weakened through an
    # alias while retaining the caller's ordered source list in the brief.
    return list(dict.fromkeys(values))


def _reject_symlinked_context_paths(project: Path, paths: list[str]) -> None:
    """Reject every symlink component before any advisory context is hashed.

    ``fingerprint_paths`` deliberately permits a parent symlink that resolves
    inside the project.  That is useful for normal source snapshots, but an
    advisory context must name its concrete inputs without an alias.
    """
    for relative in paths:
        if not isinstance(relative, str) or not relative or "\x00" in relative or "\\" in relative:
            raise ValueError("consultation context paths must be strict project-relative paths")
        candidate = Path(relative)
        if (candidate.is_absolute() or candidate.as_posix() != relative or relative.endswith("/")
                or any(part in {"", ".", ".."} for part in candidate.parts)):
            raise ValueError("consultation context paths must be strict project-relative paths")
        current = project
        for part in candidate.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ValueError("consultation context paths must be safely inspectable") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("consultation context paths must not contain symlinks")


def _validate_sources(project: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Pin all advisory context and validate its evidence without reading logs."""
    paths = _source_paths(data)
    _reject_symlinked_context_paths(project, paths)
    snapshot = fingerprint_paths(project, paths)
    for path in data["files"]:
        if snapshot["files"][path].get("state") != "regular":
            raise ValueError("consultation files must be existing regular files")
    for reference in data["evidence_refs"]:
        # Evidence binds the same named source subjects to a fresh passing check.
        verify_evidence(project, reference, kind="check", required_files=data["files"])
    for record in data["rejected_approaches"]:
        _safe_attempt_artifact(project, record["artifact_ref"])
    if check_fingerprint(project, snapshot):
        raise ValueError("consultation context changed during validation")
    return snapshot


def _brief(data: dict[str, Any]) -> dict[str, str]:
    source_lines = [*(f"- Source: {path}" for path in data["files"]),
                    *(f"- Fresh check certificate: {reference}" for reference in data["evidence_refs"])]
    rejected = [
        {"text": record["text"], "reason": record["reason"], "artifact_ref": record["artifact_ref"]}
        for record in data["rejected_approaches"]
    ]
    return {
        "GOAL": f"Answer this bounded investigation question: {data['question']}\nTask goal: {data['goal']}",
        "SOURCES OF TRUTH": "\n".join(source_lines),
        "SCOPE": "Read only the listed sources and certificates. Provide investigation advice only; do not modify files, configuration, task state, or scope.",
        "ACCEPTANCE CRITERIA": 'Return ONLY a JSON object with exactly two keys: "hypotheses" (a nonempty array of strings) and "next_checks" (an array of strings, possibly empty). Each array has at most 32 items, each string at most 512 characters. Ground hypotheses in the listed sources. Do not add markdown or other keys.',
        "VERIFICATION": "Read-only retrieval commands may inspect the listed sources. Do not execute project code, checks, tests, reviews, repairs, changes, or follow-up model calls. The root evaluates any suggested next check.",
        "CONTEXT": json.dumps({
            "constraints": data["constraints"], "acceptance": data["acceptance"],
            "rejected_approaches": rejected,
        }, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    }


def _advice(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, str):
        raise ValueError("advisory result has no JSON text")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("advisory result is not valid JSON") from exc
    if type(parsed) is not dict or set(parsed) != {"hypotheses", "next_checks"}:
        raise ValueError("advisory result violates the strict advice contract")
    hypotheses, next_checks = parsed["hypotheses"], parsed["next_checks"]
    if (not isinstance(hypotheses, list) or not hypotheses or len(hypotheses) > _MAX_ITEMS
            or not isinstance(next_checks, list) or len(next_checks) > _MAX_ITEMS):
        raise ValueError("advisory result violates the strict advice contract")
    for field, values in (("hypotheses", hypotheses), ("next_checks", next_checks)):
        if any(not isinstance(item, str) or not item.strip() or len(item) > _MAX_ITEM_TEXT for item in values):
            raise ValueError(f"advisory result {field} must contain bounded nonempty strings")
    return {"hypotheses": hypotheses, "next_checks": next_checks}


def _root_result(reason: str, receipt: dict | None, before: dict[str, Any], after: dict[str, Any] | None = None) -> dict:
    return {
        "status": "keep-root", "reason": reason, "advice": None,
        "usage": receipt.get("usage") if isinstance(receipt, dict) else None,
        "receipt": receipt, "source_fingerprints": {"before": before, "after": after},
    }


def consult(project: Path, kit: Path, input: dict, *, codex_command: Sequence[str] | None = None,
            timeout_seconds: int = 180, dry_run: bool = False) -> dict:
    """Run one opt-in, strong, read-only consultation without retrying work.

    A completed answer is advisory, and includes the native receipt and observed
    usage.  Failure or an unsafe result stays with the root; it never starts a
    repair or stronger worker automatically.
    """
    if not isinstance(project, Path) or not isinstance(kit, Path):
        raise ValueError("project and kit must be Paths")
    if type(timeout_seconds) is not int or not 0 < timeout_seconds <= 3600:
        raise ValueError("timeout_seconds must be an integer from 1 to 3600")
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be boolean")
    root = project.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project must be a directory")
    data = _input(input)
    before = _validate_sources(root, data)
    config = load_config(root, kit.resolve())
    if check_fingerprint(root, before):
        raise ValueError("consultation context changed while loading policy")
    route = route_task(config, role="explorer", complexity="complex", risk="high",
                       size="substantial", independent=True, required_capabilities=['function'])
    if (route["action"] != "execute" or route["execution"] != "leaf"
            or route["profile"] != "strong" or route["sandbox"] != "read-only"):
        return _root_result("configured route cannot provide a strong read-only consultation", None, before, before)
    rendered = _brief(data)
    # Build through the normal context limit check before the runner receives it.
    build_brief(config, rendered)
    job = {
        "role": "explorer", "complexity": "complex", "risk": "high", "size": "substantial",
        "independent": True, "brief": rendered, "checks": [], "review_files": [],
        "evidence_files": data["files"], "result_contract": _ADVICE_CONTRACT,
        "required_capabilities": ['function'],
    }
    if dry_run:
        return {
            "status": "advisory-dry-run", "route": route, "advice": None, "usage": None,
            "receipt": None, "source_fingerprints": {"before": before, "after": before},
        }
    try:
        receipt = run_job(root, kit.resolve(), job, codex_command=codex_command,
                          timeout_seconds=timeout_seconds, dry_run=False, advisory_only=True)
    except (OSError, TypeError, ValueError) as exc:
        return _root_result(f"native advisory consultation was unavailable: {exc}", None, before)
    after = fingerprint_paths(root, _source_paths(data))
    if check_fingerprint(root, before):
        return _root_result("advice context changed while consultation ran", receipt, before, after)
    if not isinstance(receipt, dict) or receipt.get("status") != "advisory":
        return _root_result("native advisory consultation did not complete", receipt if isinstance(receipt, dict) else None, before, after)
    attempts = receipt.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        return _root_result("native runner did not guarantee a single native call", receipt, before, after)
    try:
        advice = _advice(receipt.get("final"))
    except ValueError as exc:
        return _root_result(str(exc), receipt, before, after)
    try:
        _reject_known_secrets(advice)
    except ValueError as exc:
        return _root_result(str(exc), receipt, before, after)
    return {
        "status": "advisory", "advice": advice, "usage": receipt.get("usage"), "receipt": receipt,
        "route": route, "source_fingerprints": {"before": before, "after": after},
    }
