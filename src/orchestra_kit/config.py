from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SUPPORTED_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
SUPPORTED_SANDBOXES = frozenset({"read-only", "workspace-write"})
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class ConfigError(ValueError):
    """Raised when a project configuration cannot be safely compiled."""


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    display_name: str
    base_url: str
    env_key: str | None
    wire_api: str
    model_catalog_json: str | None
    supports_standalone_web_search: bool
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    model: str
    effort: str
    provider: str | None


@dataclass(frozen=True)
class RoleConfig:
    name: str
    description: str
    profile: str
    sandbox: str
    prompt_file: str
    prompt: str


@dataclass(frozen=True)
class ProjectConfig:
    version: int
    name: str
    max_parallel: int
    default_profile: str
    providers: dict[str, ProviderConfig]
    profiles: dict[str, ProfileConfig]
    roles: dict[str, RoleConfig]


def _table(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be a TOML table")
    return value


def _required_string(table: dict[str, Any], key: str, location: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location}.{key} must be a non-empty string")
    return value.strip()


def _optional_string(table: dict[str, Any], key: str, location: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{location}.{key} must be a non-empty string when set")
    return value.strip()


def _reject_unknown(table: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{location} contains unsupported key(s): {', '.join(unknown)}")


def _validate_identifier(name: str, location: str) -> None:
    if not NAME_PATTERN.fullmatch(name):
        raise ConfigError(f"{location} name '{name}' must match {NAME_PATTERN.pattern}")


def _load_provider(name: str, raw: Any) -> ProviderConfig:
    location = f"providers.{name}"
    _validate_identifier(name, location)
    table = _table(raw, location)
    _reject_unknown(
        table,
        {
            "name",
            "base_url",
            "env_key",
            "wire_api",
            "model_catalog_json",
            "supports_standalone_web_search",
            "capabilities",
        },
        location,
    )
    display_name = _required_string(table, "name", location)
    base_url = _required_string(table, "base_url", location)
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ConfigError(f"{location}.base_url must be an absolute HTTP(S) URL")
    env_key = _optional_string(table, "env_key", location)
    if env_key is not None and not ENV_PATTERN.fullmatch(env_key):
        raise ConfigError(f"{location}.env_key '{env_key}' is not a valid environment variable name")
    wire_api = table.get("wire_api", "responses")
    if wire_api != "responses":
        raise ConfigError(f"{location}.wire_api must be 'responses'")
    catalog = _optional_string(table, "model_catalog_json", location)
    supports_search = table.get("supports_standalone_web_search", False)
    if not isinstance(supports_search, bool):
        raise ConfigError(f"{location}.supports_standalone_web_search must be a boolean")
    capabilities_raw = table.get("capabilities", [])
    if not isinstance(capabilities_raw, list) or not all(
        isinstance(item, str) and NAME_PATTERN.fullmatch(item) for item in capabilities_raw
    ):
        raise ConfigError(f"{location}.capabilities must be an array of capability names")
    capabilities = tuple(dict.fromkeys(capabilities_raw))
    return ProviderConfig(
        name=name,
        display_name=display_name,
        base_url=base_url,
        env_key=env_key,
        wire_api=wire_api,
        model_catalog_json=catalog,
        supports_standalone_web_search=supports_search,
        capabilities=capabilities,
    )


def _load_profile(name: str, raw: Any) -> ProfileConfig:
    location = f"profiles.{name}"
    _validate_identifier(name, location)
    table = _table(raw, location)
    _reject_unknown(table, {"model", "effort", "provider"}, location)
    model = _required_string(table, "model", location)
    effort = _required_string(table, "effort", location)
    if effort not in SUPPORTED_EFFORTS:
        raise ConfigError(f"{location} uses unsupported effort '{effort}'")
    provider = _optional_string(table, "provider", location)
    return ProfileConfig(name=name, model=model, effort=effort, provider=provider)


def _load_role(name: str, raw: Any, kit_root: Path) -> RoleConfig:
    location = f"roles.{name}"
    _validate_identifier(name, location)
    table = _table(raw, location)
    _reject_unknown(table, {"description", "profile", "sandbox", "prompt"}, location)
    description = _required_string(table, "description", location)
    profile = _required_string(table, "profile", location)
    sandbox = _required_string(table, "sandbox", location)
    if sandbox not in SUPPORTED_SANDBOXES:
        raise ConfigError(f"{location} uses unsupported sandbox '{sandbox}'")
    prompt_file = _required_string(table, "prompt", location)
    prompt_path = Path(prompt_file)
    if prompt_path.name != prompt_file or prompt_path.suffix != ".md":
        raise ConfigError(f"{location}.prompt must be a simple file name ending in .md")
    resolved_prompt = kit_root / "templates" / "roles" / prompt_file
    try:
        prompt = resolved_prompt.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"{location}.prompt file not found: {prompt_file}") from exc
    if not prompt:
        raise ConfigError(f"{location}.prompt file is empty: {prompt_file}")
    return RoleConfig(
        name=name,
        description=description,
        profile=profile,
        sandbox=sandbox,
        prompt_file=prompt_file,
        prompt=prompt,
    )


def load_config(project_root: Path, kit_root: Path) -> ProjectConfig:
    config_path = project_root / ".orchestra" / "project.toml"
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    _reject_unknown(raw, {"version", "name", "orchestration", "providers", "profiles", "roles"}, "root")
    if raw.get("version") != 1:
        raise ConfigError("version must be 1")
    name = _required_string(raw, "name", "root")

    orchestration = _table(raw.get("orchestration", {}), "orchestration")
    _reject_unknown(orchestration, {"max_parallel", "default_profile"}, "orchestration")
    max_parallel = orchestration.get("max_parallel", 3)
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or not 1 <= max_parallel <= 8:
        raise ConfigError("orchestration.max_parallel must be an integer from 1 to 8")
    default_profile = _required_string(orchestration, "default_profile", "orchestration")

    providers_raw = _table(raw.get("providers", {}), "providers")
    providers = {name: _load_provider(name, value) for name, value in providers_raw.items()}
    profiles_raw = _table(raw.get("profiles"), "profiles")
    if not profiles_raw:
        raise ConfigError("profiles must contain at least one profile")
    profiles = {name: _load_profile(name, value) for name, value in profiles_raw.items()}
    roles_raw = _table(raw.get("roles"), "roles")
    if not roles_raw:
        raise ConfigError("roles must contain at least one role")
    roles = {name: _load_role(name, value, kit_root) for name, value in roles_raw.items()}

    if default_profile not in profiles:
        raise ConfigError(f"orchestration.default_profile references unknown profile '{default_profile}'")
    for profile in profiles.values():
        if profile.provider is not None and profile.provider not in providers:
            raise ConfigError(f"profile '{profile.name}' references unknown provider '{profile.provider}'")
    for role in roles.values():
        if role.profile not in profiles:
            raise ConfigError(f"role '{role.name}' references unknown profile '{role.profile}'")

    return ProjectConfig(
        version=1,
        name=name,
        max_parallel=max_parallel,
        default_profile=default_profile,
        providers=providers,
        profiles=profiles,
        roles=roles,
    )
