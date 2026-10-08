"""Validated project settings; secrets live outside project files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import ENV_PATTERN, NAME_PATTERN, SUPPORTED_EFFORTS, ProjectConfig, load_config, parse_config
from .model_catalog import MAX_CATALOG_BYTES, catalog_text, model_efforts, validate_catalog
from .project import (
    MANIFEST_PATH, ProjectError, _atomic_write, _digest, _extract_agents_block,
    _load_manifest, _managed_outputs, _preflight_managed_file, _safe_path, sync_project,
)


class SettingsConflict(ValueError):
    pass


@contextmanager
def _settings_lock(path: Path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Некорректный файл блокировки")
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SettingsConflict("Настройки уже сохраняются; повторите обновление") from None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SettingsConflict("Настройки уже сохраняются; повторите обновление") from None
        yield


def _text(value: Any, name: str, maximum: int = 500) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(character) < 32 for character in value)):
        raise ValueError("Некорректное поле: " + name)
    return value.strip()


def _set_table(source: str, table: str, values: dict[str, Any]) -> str:
    """Update only explicitly owned scalar keys, leaving unrelated tables/comments."""
    lines = source.splitlines(keepends=True)
    header = "[" + table + "]"
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    encode = lambda item: json.dumps(item, ensure_ascii=False)
    entries = [key + " = " + encode(item) + "\n" for key, item in values.items() if item is not None]
    if start is None:
        return source.rstrip() + "\n\n" + header + "\n" + "".join(entries)
    end = next((index for index in range(start + 1, len(lines))
                if re.match(r"^\s*\[", lines[index])), len(lines))
    body: list[str] = []
    remaining = dict(values)
    for line in lines[start + 1:end]:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match[1] in values:
            key = match[1]
            if key in remaining and remaining[key] is not None:
                body.append(key + " = " + encode(remaining[key]) + "\n")
            remaining.pop(key, None)
        else:
            body.append(line)
    for key, item in remaining.items():
        if item is not None:
            body.append(key + " = " + encode(item) + "\n")
    return "".join(lines[:start + 1] + body + lines[end:])


def _catalog_path(project: Path, relative: str) -> Path:
    candidate = Path(relative)
    if (candidate.is_absolute() or ".." in candidate.parts or len(candidate.parts) != 3
            or candidate.parts[:2] != (".orchestra", "providers") or candidate.suffix != ".json"):
        raise ValueError("Каталог моделей должен находиться в .orchestra/providers")
    try:
        return _safe_path(project, relative)
    except ProjectError:
        raise ValueError("Каталог моделей не должен использовать символические ссылки или выходить из проекта") from None


def _catalog_bytes(project: Path, relative: str) -> bytes:
    path = _catalog_path(project, relative)
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_CATALOG_BYTES + 1)
    except FileNotFoundError:
        raise ValueError(f"Каталог моделей не найден: {relative}") from None
    if not path.is_file() or len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("Некорректный или слишком большой каталог моделей")
    return raw


def _load_catalog(project: Path, relative: str) -> dict[str, Any]:
    try:
        value = json.loads(_catalog_bytes(project, relative))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(f"Некорректный JSON каталога моделей: {relative}") from None
    return validate_catalog(value)


def _settings_fingerprint(project: Path, config_raw: bytes, config: ProjectConfig) -> str:
    digest = hashlib.sha256(b"project.toml\0" + len(config_raw).to_bytes(8, "big") + config_raw)
    for name, provider in sorted(config.providers.items()):
        if provider.model_catalog_json is None:
            continue
        raw = _catalog_bytes(project, provider.model_catalog_json)
        identity = (name + "\0" + provider.model_catalog_json).encode("utf-8")
        digest.update(len(identity).to_bytes(8, "big") + identity)
        digest.update(len(raw).to_bytes(8, "big") + raw)
    return digest.hexdigest()


def _model_options(config: ProjectConfig, catalogs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    options: dict[tuple[str | None, str], dict[str, Any]] = {}
    for profile in config.profiles.values():
        key = (profile.provider, profile.model)
        option = options.setdefault(key, {"id": profile.model, "label": profile.model, "efforts": [],
                                          "source": "configured_profile", "provider": profile.provider})
    for provider_name, catalog in catalogs.items():
        for model in catalog["models"]:
            options[(provider_name, model["slug"])] = {
                "id": model["slug"], "label": model["display_name"],
                "efforts": model_efforts(model), "source": "provider_catalog", "provider": provider_name,
            }
    return sorted(options.values(), key=lambda item: (item["provider"] or "", item["label"].casefold(), item["id"]))


class SettingsService:
    def __init__(self, kit_root: Path, *, credentials=None):
        self.kit_root = kit_root.resolve()
        self.credentials = credentials
        self.lock = threading.RLock()

    def _credentials(self):
        if self.credentials is None:
            from .providers import ProviderCredentialService
            self.credentials = ProviderCredentialService()
        return self.credentials

    def get(self, project: Path):
        project = project.resolve()
        path = _safe_path(project, ".orchestra/project.toml")
        raw = path.read_bytes()
        if len(raw) > MAX_CATALOG_BYTES:
            raise ValueError("Настройки слишком велики")
        config = load_config(project, self.kit_root)
        catalogs: dict[str, dict[str, Any]] = {}
        providers: dict[str, dict[str, Any]] = {}
        for name, provider in config.providers.items():
            catalog = None
            if provider.model_catalog_json is not None:
                catalog = _load_catalog(project, provider.model_catalog_json)
                catalogs[name] = catalog
            if provider.env_key is None:
                credential = {"available": True, "source": "not_required"}
            else:
                try:
                    status = self._credentials().status(provider)
                    credential = {"available": status.available, "source": status.source}
                except Exception:
                    credential = {"available": False, "source": None, "error": "Связка ключей недоступна"}
            providers[name] = {
                "id": name, "name": provider.display_name, "base_url": provider.base_url,
                "env_key": provider.env_key, "wire_api": provider.wire_api,
                "capabilities": list(provider.capabilities), "credential": credential, "catalog": catalog,
            }
        return {
            "fingerprint": _settings_fingerprint(project, raw, config),
            "profiles": {name: {"model": profile.model, "effort": profile.effort, "provider": profile.provider}
                         for name, profile in config.profiles.items()},
            "roles": {name: role.profile for name, role in config.roles.items()},
            "routing": {"enabled": config.routing.enabled, "profile_order": list(config.routing.profile_order)},
            "providers": providers, "model_options": _model_options(config, catalogs), "protocol": "responses",
        }

    def save(self, project: Path, payload: Any):
        allowed = {"expected_fingerprint", "profiles", "roles", "routing", "provider", "api_key", "delete_key"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("Некорректные настройки")
        project = project.resolve()
        config_path = _safe_path(project, ".orchestra/project.toml")
        lock_path = _safe_path(project, ".orchestra/settings.lock")
        with self.lock, _settings_lock(lock_path):
            try:
                source = config_path.read_text(encoding="utf-8")
                config = load_config(project, self.kit_root)
                fingerprint = _settings_fingerprint(project, source.encode("utf-8"), config)
                if payload.get("expected_fingerprint") != fingerprint:
                    raise SettingsConflict("Настройки изменились в другом окне. Обновите форму")
                candidate = source
                provider = payload.get("provider")
                provider_id: str | None = None
                provider_env: str | None = None
                catalog_path: Path | None = None
                catalog_relative: str | None = None
                catalog_content: str | None = None
                if provider is not None:
                    provider_allowed = {"id", "name", "base_url", "env_key", "capabilities", "catalog"}
                    if not isinstance(provider, dict) or set(provider) - provider_allowed:
                        raise ValueError("Некорректный провайдер")
                    provider_id = _text(provider.get("id"), "ID провайдера", 64)
                    if not NAME_PATTERN.fullmatch(provider_id) or provider_id in {"openai", "ollama", "lmstudio"}:
                        raise ValueError("ID провайдера некорректен или зарезервирован")
                    url = _text(provider.get("base_url"), "Адрес API", 1500)
                    parsed = urlsplit(url)
                    if (parsed.username or parsed.password or parsed.query or parsed.fragment
                            or parsed.scheme not in {"http", "https"} or not parsed.hostname):
                        raise ValueError("Укажите адрес API без ключей и параметров")
                    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                        raise ValueError("Удалённый API должен использовать HTTPS")
                    raw_env = provider.get("env_key")
                    provider_env = None if raw_env is None else _text(raw_env, "Имя переменной ключа", 100)
                    if provider_env is not None and (not ENV_PATTERN.fullmatch(provider_env)
                            or provider_env in {"HOME", "PATH", "PYTHONPATH", "CODEX_HOME", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"}
                            or provider_env.startswith(("DYLD_", "LD_", "PYTHON", "CODEX_"))):
                        raise ValueError("Укажите безопасное имя переменной API-ключа")
                    capabilities = provider.get("capabilities", [])
                    if (not isinstance(capabilities, list) or len(capabilities) > 20
                            or any(not isinstance(item, str) or not NAME_PATTERN.fullmatch(item) for item in capabilities)
                            or len(set(capabilities)) != len(capabilities)):
                        raise ValueError("Некорректные возможности провайдера")
                    values: dict[str, Any] = {
                        "name": _text(provider.get("name"), "Название"), "base_url": url,
                        "env_key": provider_env, "wire_api": "responses", "capabilities": capabilities,
                        "supports_standalone_web_search": "web_search" in capabilities,
                    }
                    if "catalog" in provider:
                        catalog_content = catalog_text(provider["catalog"])
                        catalog_relative = f".orchestra/providers/{provider_id}-models.json"
                        catalog_path = _catalog_path(project, catalog_relative)
                        referenced = (provider_id in config.providers
                                      and config.providers[provider_id].model_catalog_json == catalog_relative)
                        if catalog_path.exists() and not referenced:
                            raise ValueError("Существующий каталог моделей не принадлежит этому провайдеру")
                        values["model_catalog_json"] = catalog_relative
                    candidate = _set_table(candidate, "providers." + provider_id, values)

                profile_values = payload.get("profiles", {})
                if not isinstance(profile_values, dict) or set(profile_values) - set(config.profiles):
                    raise ValueError("Неизвестный профиль")
                for name, profile in profile_values.items():
                    if (not isinstance(profile, dict) or set(profile) != {"model", "effort", "provider"}
                            or profile.get("effort") not in SUPPORTED_EFFORTS):
                        raise ValueError("Некорректный профиль модели")
                    provider_name = profile.get("provider")
                    if provider_name is not None and (not isinstance(provider_name, str)
                            or provider_name not in {*config.providers, provider_id}):
                        raise ValueError("Неизвестный провайдер профиля")
                    candidate = _set_table(candidate, "profiles." + name, {
                        "model": _text(profile.get("model"), "Модель", 300),
                        "effort": profile["effort"], "provider": provider_name,
                    })

                role_values = payload.get("roles", {})
                if not isinstance(role_values, dict) or set(role_values) - set(config.roles):
                    raise ValueError("Неизвестная роль")
                for name, profile_name in role_values.items():
                    if not isinstance(profile_name, str) or profile_name not in config.profiles:
                        raise ValueError("Роль ссылается на неизвестный профиль")
                    candidate = _set_table(candidate, "roles." + name, {"profile": profile_name})

                routing = payload.get("routing")
                if routing is not None:
                    if not isinstance(routing, dict) or set(routing) != {"enabled", "profile_order"}:
                        raise ValueError("Некорректная маршрутизация")
                    enabled, order = routing["enabled"], routing["profile_order"]
                    if type(enabled) is not bool or not isinstance(order, list) or any(not isinstance(item, str) for item in order):
                        raise ValueError("Некорректная маршрутизация")
                    if len(order) != len(config.profiles) or len(set(order)) != len(order) or set(order) != set(config.profiles):
                        raise ValueError("Порядок маршрутизации должен содержать каждый профиль ровно один раз")
                    candidate = _set_table(candidate, "routing", {"enabled": enabled, "profile_order": order})

                secret = payload.get("api_key")
                if secret is not None:
                    if (provider_id is None or provider_env is None or not isinstance(secret, str) or not secret
                            or len(secret) > 8192 or any(character in secret for character in "\0\n\r")):
                        raise ValueError("Некорректный ключ")
                    if catalog_content is not None and secret in catalog_content:
                        raise ValueError("Каталог моделей не должен содержать API-ключ")
                delete = payload.get("delete_key")
                if delete is not None and (not isinstance(delete, str) or delete not in config.providers
                                           or config.providers[delete].env_key is None or secret is not None):
                    raise ValueError("Некорректное удаление ключа")

                compiled = parse_config(project, self.kit_root, tomllib.loads(candidate))
                for profile in compiled.profiles.values():
                    if profile.provider is None:
                        continue
                    current_catalog = provider.get('catalog') if provider is not None and profile.provider == provider_id and provider.get('catalog') is not None else None
                    selected_provider = compiled.providers[profile.provider]
                    if current_catalog is None and selected_provider.model_catalog_json is not None:
                        current_catalog = _load_catalog(project,selected_provider.model_catalog_json)
                    if current_catalog:
                        for model in current_catalog['models']:
                            levels = model_efforts(model)
                            if model['slug'] == profile.model and levels and profile.effort not in levels:
                                raise ValueError('Выбранное усилие не поддерживается моделью каталога')

                outputs = _managed_outputs(compiled)
                previous = _load_manifest(project)
                if not previous:
                    raise ValueError("Нет сохранённой проверки интеграции. Выполните orchestra sync и проверьте изменения")
                agents_path = _safe_path(project, "AGENTS.md")
                block = _extract_agents_block(agents_path.read_text()) if agents_path.exists() else None
                if block is None or _digest(block) != previous.get("agents_block_sha256"):
                    raise ValueError("Инструкции AGENTS.md изменены вручную; сохранение остановлено")
                names = set(outputs) | set(previous["files"] if previous else ()) | {
                    "AGENTS.md", str(MANIFEST_PATH), ".orchestra/project.toml",
                }
                backups: dict[str, str | None] = {}
                for name in names:
                    path = _safe_path(project, name)
                    if name not in {"AGENTS.md", str(MANIFEST_PATH), ".orchestra/project.toml"}:
                        _preflight_managed_file(path)
                        if (path.exists() and previous and name in previous["files"]
                                and _digest(path.read_text()) != previous["files"][name]):
                            raise ValueError("Интеграционные файлы изменены вручную; сохранение остановлено")
                    backups[name] = path.read_text() if path.exists() else None
                if catalog_path is not None:
                    backups[catalog_relative] = catalog_path.read_text() if catalog_path.exists() else None
                current_fingerprint = _settings_fingerprint(project, config_path.read_bytes(), config)
                if current_fingerprint != fingerprint:
                    raise SettingsConflict("Настройки изменились; обновите форму")
                try:
                    if catalog_path is not None and catalog_content is not None:
                        if _catalog_path(project, catalog_relative) != catalog_path:
                            raise ValueError("Путь каталога моделей изменился во время сохранения")
                        _atomic_write(catalog_path, catalog_content)
                    _atomic_write(config_path, candidate)
                    sync_project(project, self.kit_root)
                    if secret is not None:
                        self._credentials().set(compiled.providers[provider_id], secret)
                    if delete is not None:
                        self._credentials().delete(config.providers[delete])
                except Exception:
                    for name, old in backups.items():
                        path = _safe_path(project, name)
                        if old is None:
                            path.unlink(missing_ok=True)
                        else:
                            _atomic_write(path, old)
                    raise
                return self.get(project)
            except ProjectError:
                raise ValueError("Есть чужие или вручную изменённые интеграционные файлы. Сохранение остановлено") from None
