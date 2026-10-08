"""Validation and UI projection for project-owned model catalogs."""
from __future__ import annotations

import json
import re
from typing import Any

from .config import SUPPORTED_EFFORTS


MAX_CATALOG_BYTES = 256 * 1024
MAX_MODELS = 200
MODEL_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
SENSITIVE_KEYS = frozenset({"apikey", "accesstoken", "password", "authorization", "clientsecret", "secret", "token", "xapikey", "privatekey", "bearertoken"})


def _catalog_error(message: str) -> ValueError:
    return ValueError(f"Некорректный каталог моделей: {message}")


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise _catalog_error(f"поле {field} должно быть непустой строкой")
    return value.strip()


def _reasoning_efforts(model: dict[str, Any]) -> list[str]:
    efforts: list[str] = []
    levels = model.get("supported_reasoning_levels")
    if levels is not None:
        if not isinstance(levels, list) or len(levels) > len(SUPPORTED_EFFORTS):
            raise _catalog_error("supported_reasoning_levels должен быть ограниченным списком")
        for level in levels:
            if not isinstance(level, dict) or not isinstance(level.get("effort"), str):
                raise _catalog_error("каждый reasoning level должен содержать effort")
            effort = level["effort"]
            if effort not in SUPPORTED_EFFORTS or effort in efforts:
                raise _catalog_error("reasoning effort неизвестен или повторяется")
            description = level.get("description")
            if description is not None:
                _bounded_text(description, "description", 500)
            efforts.append(effort)
    simple = model.get("supported_reasoning_efforts")
    if simple is not None:
        if levels is not None or not isinstance(simple, list) or len(simple) > len(SUPPORTED_EFFORTS):
            raise _catalog_error("supported_reasoning_efforts должен быть единственным списком effort")
        for effort in simple:
            if not isinstance(effort, str) or effort not in SUPPORTED_EFFORTS or effort in efforts:
                raise _catalog_error("reasoning effort неизвестен или повторяется")
            efforts.append(effort)
    for key in ("default_reasoning_level", "default_reasoning_effort"):
        default = model.get(key)
        if default is not None and (not isinstance(default, str) or default not in SUPPORTED_EFFORTS):
            raise _catalog_error(f"{key} содержит неизвестный effort")
    return efforts


def _reject_sensitive_metadata(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and re.sub(r"[^a-z0-9]", "", key.casefold()) in SENSITIVE_KEYS:
                raise _catalog_error("метаданные не должны содержать секреты")
            _reject_sensitive_metadata(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_metadata(item)


def _native_metadata(model: dict[str, Any]) -> None:
    """Check required native fields without discarding provider extensions."""
    required = {
        "supported_reasoning_levels", "shell_type", "visibility",
        "supported_in_api", "priority", "support_verbosity",
        "truncation_policy", "experimental_supported_tools",
    }
    missing = required - set(model)
    if missing:
        raise _catalog_error("нужны полные метаданные Codex: " + ", ".join(sorted(missing)))
    enums = {
        "shell_type": {"default", "local", "shell_command", "unified_exec", "disabled"},
        "visibility": {"list", "hide", "none"},
    }
    for field, choices in enums.items():
        if not isinstance(model[field], str) or model[field] not in choices:
            raise _catalog_error(f"{field} должен быть одним из: {', '.join(sorted(choices))}")
    for field in ("supported_in_api", "support_verbosity"):
        if type(model[field]) is not bool:
            raise _catalog_error(f"{field} должен быть логическим значением")
    if type(model["priority"]) is not int or not -(2**31) <= model["priority"] < 2**31:
        raise _catalog_error("priority должен быть целым числом i32")
    policy = model["truncation_policy"]
    if (
        not isinstance(policy, dict)
        or not isinstance(policy.get("mode"), str)
        or policy["mode"] not in {"tokens", "bytes"}
        or type(policy.get("limit")) is not int
        or not -(2**63) <= policy["limit"] < 2**63
    ):
        raise _catalog_error("truncation_policy требует mode (tokens или bytes) и целое limit i64")
    tools = model["experimental_supported_tools"]
    if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
        raise _catalog_error("experimental_supported_tools должен быть списком строк")
    levels = model["supported_reasoning_levels"]
    if not isinstance(levels, list) or any(
        not isinstance(level, dict) or not isinstance(level.get("description"), str)
        for level in levels
    ):
        raise _catalog_error("supported_reasoning_levels требует список объектов с description")


def validate_catalog(value: Any) -> dict[str, Any]:
    """Validate a Codex catalog while preserving its bounded metadata unchanged."""
    if not isinstance(value, dict) or not isinstance(value.get("models"), list):
        raise _catalog_error('ожидается объект с массивом "models"')
    _reject_sensitive_metadata(value)
    models = value["models"]
    if not 1 <= len(models) <= MAX_MODELS:
        raise _catalog_error(f"число моделей должно быть от 1 до {MAX_MODELS}")
    seen: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            raise _catalog_error("каждая модель должна быть объектом")
        _native_metadata(model)
        messages=model.get('model_messages')
        if messages is not None and not isinstance(messages,dict):
            raise _catalog_error('model_messages должен быть объектом')
        instructions=model.get('base_instructions') or (messages or {}).get('instructions_template')
        if not isinstance(instructions,str) or not instructions.strip():
            raise _catalog_error('нужны base_instructions или model_messages.instructions_template из каталога провайдера')
        slug = _bounded_text(model.get("slug"), "slug", 200)
        if model["slug"] != slug or not MODEL_SLUG_PATTERN.fullmatch(slug):
            raise _catalog_error("slug содержит недопустимые символы")
        if slug in seen:
            raise _catalog_error("slug моделей не должны повторяться")
        seen.add(slug)
        _bounded_text(model.get("display_name"), "display_name", 200)
        _reasoning_efforts(model)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise _catalog_error("метаданные должны быть корректным JSON") from exc
    if len(encoded) > MAX_CATALOG_BYTES:
        raise _catalog_error(f"размер превышает {MAX_CATALOG_BYTES} байт")
    return value


def catalog_text(value: Any) -> str:
    catalog = validate_catalog(value)
    text = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_CATALOG_BYTES:
        raise _catalog_error(f"размер превышает {MAX_CATALOG_BYTES} байт")
    return text


def model_efforts(model: dict[str, Any]) -> list[str]:
    """Return validated effort metadata for a settings dropdown."""
    return _reasoning_efforts(model)
