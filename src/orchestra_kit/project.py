from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .config import ConfigError, ProjectConfig, load_config
from .render import (
    AGENTS_END,
    AGENTS_START,
    GENERATED_MARKER,
    render_agent,
    render_agents_block,
    render_skill,
    render_skill_ui,
)


MANIFEST_PATH = Path(".orchestra/manifest.json")


class ProjectError(RuntimeError):
    """Raised when synchronization cannot proceed without risking user files."""


@dataclass(frozen=True)
class SyncResult:
    written: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True)
class DoctorResult:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _managed_outputs(config: ProjectConfig) -> dict[str, str]:
    outputs = {
        f".codex/agents/orchestra-{role.name}.toml": render_agent(config, role)
        for role in config.roles.values()
    }
    outputs[".agents/skills/orchestrate-project/SKILL.md"] = render_skill(config)
    outputs[".agents/skills/orchestrate-project/agents/openai.yaml"] = render_skill_ui()
    return outputs


def _safe_path(project_root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProjectError(f"unsafe managed path in manifest: {relative}")
    root = project_root.resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ProjectError(f"managed path escapes project: {relative}")
    return resolved


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".orchestra-tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _load_manifest(project_root: Path) -> dict[str, object] | None:
    path = _safe_path(project_root, str(MANIFEST_PATH))
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ProjectError(f"invalid OrchestraKit manifest: {path}") from exc
    if not isinstance(manifest, dict) or manifest.get("generated_by") != "OrchestraKit":
        raise ProjectError(f"manifest is not managed by OrchestraKit: {path}")
    files = manifest.get("files")
    if not isinstance(files, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in files.items()):
        raise ProjectError(f"manifest has invalid files table: {path}")
    return manifest


def _updated_agents(existing: str, block: str) -> str:
    start_count = existing.count(AGENTS_START)
    end_count = existing.count(AGENTS_END)
    if start_count != end_count or start_count > 1:
        raise ProjectError("AGENTS.md contains malformed OrchestraKit markers")
    if start_count == 1:
        start = existing.index(AGENTS_START)
        end = existing.index(AGENTS_END, start) + len(AGENTS_END)
        return existing[:start] + block + existing[end:]
    prefix = existing.rstrip()
    return (prefix + "\n\n" if prefix else "") + block + "\n"


def _extract_agents_block(text: str) -> str | None:
    if text.count(AGENTS_START) != 1 or text.count(AGENTS_END) != 1:
        return None
    start = text.index(AGENTS_START)
    end = text.index(AGENTS_END, start) + len(AGENTS_END)
    return text[start:end]


def _preflight_managed_file(path: Path) -> None:
    if path.exists() and GENERATED_MARKER not in path.read_text(encoding="utf-8"):
        raise ProjectError(f"refusing to overwrite file not managed by OrchestraKit: {path}")


def _build_manifest(outputs: dict[str, str], agents_block: str) -> str:
    payload = {
        "generated_by": "OrchestraKit",
        "format_version": 1,
        "files": {path: _digest(text) for path, text in sorted(outputs.items())},
        "agents_block_sha256": _digest(agents_block),
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def sync_project(project_root: Path, kit_root: Path) -> SyncResult:
    project_root = project_root.resolve()
    if not project_root.is_dir():
        raise ProjectError(f"project directory does not exist: {project_root}")
    config = load_config(project_root, kit_root.resolve())
    outputs = _managed_outputs(config)
    previous = _load_manifest(project_root)
    previous_files = set(previous["files"]) if previous else set()
    stale = sorted(previous_files - set(outputs))

    agents_path = _safe_path(project_root, "AGENTS.md")
    existing_agents = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    agents_block = render_agents_block(config)
    updated_agents = _updated_agents(existing_agents, agents_block)

    for relative in outputs:
        _preflight_managed_file(_safe_path(project_root, relative))
    for relative in stale:
        _preflight_managed_file(_safe_path(project_root, relative))
    manifest_path = _safe_path(project_root, str(MANIFEST_PATH))
    if manifest_path.exists() and previous is None:
        raise ProjectError(f"refusing to overwrite unmanaged manifest: {manifest_path}")

    written: list[str] = []
    for relative, content in sorted(outputs.items()):
        path = _safe_path(project_root, relative)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            _atomic_write(path, content)
            written.append(relative)
    if not agents_path.exists() or existing_agents != updated_agents:
        _atomic_write(agents_path, updated_agents)
        written.append("AGENTS.md")

    removed: list[str] = []
    for relative in stale:
        path = _safe_path(project_root, relative)
        if path.exists():
            path.unlink()
            removed.append(relative)

    manifest_text = _build_manifest(outputs, agents_block)
    if not manifest_path.exists() or manifest_path.read_text(encoding="utf-8") != manifest_text:
        _atomic_write(manifest_path, manifest_text)
        written.append(str(MANIFEST_PATH))
    return SyncResult(written=tuple(written), removed=tuple(removed))


def init_project(project_root: Path, kit_root: Path, name: str | None = None) -> SyncResult:
    project_root = project_root.resolve()
    if not project_root.is_dir():
        raise ProjectError(f"project directory does not exist: {project_root}")
    config_path = project_root / ".orchestra" / "project.toml"
    if config_path.exists():
        raise ProjectError(f"project is already initialized: {config_path}; run sync instead")
    template_path = kit_root.resolve() / "templates" / "project.toml"
    try:
        template = template_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProjectError(f"project template not found: {template_path}") from exc
    project_name = name.strip() if name and name.strip() else project_root.name
    rendered = template.replace('"__PROJECT_NAME__"', json.dumps(project_name, ensure_ascii=False))
    _atomic_write(config_path, rendered)
    return sync_project(project_root, kit_root)


def doctor_project(
    project_root: Path,
    kit_root: Path,
    environ: Mapping[str, str] | None = None,
) -> DoctorResult:
    errors: list[str] = []
    warnings: list[str] = []
    environment = os.environ if environ is None else environ
    try:
        config = load_config(project_root.resolve(), kit_root.resolve())
        outputs = _managed_outputs(config)
        manifest = _load_manifest(project_root.resolve())
    except (ConfigError, ProjectError) as exc:
        return DoctorResult(errors=(str(exc),), warnings=())

    if manifest is None:
        errors.append("manifest is missing; run orchestra sync")
        manifest_files: dict[str, str] = {}
    else:
        manifest_files = manifest["files"]  # type: ignore[assignment]

    for relative, expected in sorted(outputs.items()):
        path = _safe_path(project_root.resolve(), relative)
        expected_hash = _digest(expected)
        if not path.exists():
            errors.append(f"generated file is missing: {relative}")
            continue
        actual_hash = _digest(path.read_text(encoding="utf-8"))
        if actual_hash != expected_hash or manifest_files.get(relative) != expected_hash:
            errors.append(f"generated file drift detected: {relative}; run orchestra sync")

    extra_manifest_files = sorted(set(manifest_files) - set(outputs))
    for relative in extra_manifest_files:
        errors.append(f"stale generated file recorded: {relative}; run orchestra sync")

    agents_path = _safe_path(project_root.resolve(), "AGENTS.md")
    if not agents_path.exists():
        errors.append("AGENTS.md integration is missing; run orchestra sync")
    else:
        actual_block = _extract_agents_block(agents_path.read_text(encoding="utf-8"))
        if actual_block != render_agents_block(config):
            errors.append("AGENTS.md OrchestraKit block drift detected; run orchestra sync")

    used_profiles = {role.profile for role in config.roles.values()}
    for profile_name in sorted(used_profiles):
        profile = config.profiles[profile_name]
        if profile.provider is None:
            continue
        provider = config.providers[profile.provider]
        if provider.env_key and not environment.get(provider.env_key):
            errors.append(f"required provider environment variable is not set: {provider.env_key}")
        if provider.model_catalog_json:
            catalog = Path(provider.model_catalog_json)
            if not catalog.is_absolute():
                catalog = project_root.resolve() / catalog
            if not catalog.is_file():
                errors.append(f"provider model catalog is missing: {provider.model_catalog_json}")

    return DoctorResult(errors=tuple(errors), warnings=tuple(warnings))
