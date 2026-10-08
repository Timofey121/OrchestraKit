"""Build compact, explicit handoffs without treating local input as attestation."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from uuid import UUID


_FIELDS = frozenset({
    "task_id", "revision", "goal", "constraints", "acceptance",
    "unresolved_failures", "next_step", "evidence_refs", "raw_history_refs",
})
_LIST_FIELDS = ("constraints", "acceptance", "unresolved_failures", "evidence_refs", "raw_history_refs")


def _text(value: Any, name: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{name} must be nonempty text")
    return value


def _texts(value: Any, name: str, *, nonempty: bool) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} must be a {'nonempty ' if nonempty else ''}list")
    for item in value:
        _text(item, name)
    return list(value)


def build_handoff(data: dict, *, max_chars: int = 6000) -> dict:
    """Validate and return a bounded local handoff.

    This function never summarizes, truncates, or claims that supplied task
    identity and evidence have been verified by a server or another agent.
    """
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise ValueError("handoff contains unsupported or missing fields")
    if type(max_chars) is not int or not 1 <= max_chars <= 1_000_000:
        raise ValueError("max_chars must be an integer from 1 to 1000000")
    try:
        if str(UUID(data["task_id"])) != data["task_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("task_id must be a canonical UUID") from exc
    if type(data["revision"]) is not int or data["revision"] < 0:
        raise ValueError("revision must be a nonnegative integer")
    result = deepcopy(data)
    result["goal"] = _text(result["goal"], "goal")
    result["next_step"] = _text(result["next_step"], "next_step")
    for name in _LIST_FIELDS:
        result[name] = _texts(result[name], name, nonempty=name in {"constraints", "acceptance"})
    result["identity_attestation"] = "local-input-unattested"
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded) > max_chars:
        raise ValueError(f"handoff exceeds max_chars ({len(encoded)} > {max_chars}); narrow references, never drop constraints")
    return result
