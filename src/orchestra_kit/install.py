from __future__ import annotations

import json
import hashlib
import os
import tempfile
from pathlib import Path

from .project import ProjectError, _atomic_write, _digest, _safe_path


MANIFEST = ".orchestra-skill-manifest.json"


def _skill_path(skill: Path, relative: str) -> Path:
    target = _safe_path(skill, relative)
    lexical = skill
    for part in Path(relative).parts:
        lexical = lexical / part
        if lexical.is_symlink():
            raise ProjectError(f'refusing symlinked managed skill path: {lexical}')
    return target


def _payload_digest(content: str | bytes) -> str:
    return hashlib.sha256(content.encode('utf-8') if isinstance(content, str) else content).hexdigest()


def _write_payload(path: Path, content: str | bytes) -> None:
    if isinstance(content, str):
        _atomic_write(path, content)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.orchestra-', delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _bundle(kit_root: Path) -> dict[str, str | bytes]:
    outputs: dict[str, str | bytes] = {}
    runtime = kit_root / "src/orchestra_kit"
    package = Path(__file__).resolve().parent
    skin = kit_root / "skills/orchestra"
    portable = (kit_root / "lib/orchestra_kit").is_dir() and (kit_root.parent / "SKILL.md").is_file()
    portable_owned = None
    if portable:
        runtime = kit_root / "lib/orchestra_kit"
        skin = kit_root.parent
        portable_owned = _owned_files(skin)
        if portable_owned is None:
            raise ProjectError("portable skill ownership manifest is missing")
    if kit_root.resolve() == (package / "_kit_data").resolve():
        runtime = package
    for source, prefix, pattern in [
        (skin, "", "*"),
        (runtime, "scripts/lib/orchestra_kit/", "*.py"),
        (runtime / "web", "scripts/lib/orchestra_kit/web/", "*"),
        (kit_root / "templates", "scripts/templates/", "*"),
    ]:
        for path in sorted(source.rglob(pattern)):
            relative = path.relative_to(source).as_posix()
            if portable and prefix == "" and (relative not in portable_owned or relative.startswith(("scripts/lib/", "scripts/templates/"))):
                continue
            if path.is_file() and "__pycache__" not in path.parts and "_kit_data" not in path.relative_to(source).parts:
                content = path.read_bytes() if path.suffix.lower() in {'.webp', '.png', '.jpg', '.jpeg'} else path.read_text(encoding='utf-8')
                outputs[prefix + path.relative_to(source).as_posix()] = content
    if "SKILL.md" not in outputs or "scripts/orchestra.py" not in outputs:
        raise ProjectError("skill source or launcher is missing from the kit")
    return outputs


def _owned_files(skill: Path) -> dict[str, str] | None:
    manifest = _skill_path(skill, MANIFEST)
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectError("invalid OrchestraKit skill manifest") from exc
    if not isinstance(data, dict) or data.get("generated_by") != "OrchestraKit":
        raise ProjectError("skill manifest is not owned by OrchestraKit")
    files = data.get("files")
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                            for k, v in files.items()):
        raise ProjectError("invalid skill manifest files")
    for relative in files:
        _skill_path(skill, relative)
    return files


def _prepare_skill_install(
    kit_root: Path, skills_dir: Path, codex_home: Path | None = None
) -> tuple[Path, dict[str, str | bytes], dict[str, str], tuple[Path, str] | None]:
    skills_dir = skills_dir.expanduser().resolve()
    skill = skills_dir / "orchestra"
    if skill.is_symlink():
        raise ProjectError("refusing to replace a symlinked skill")
    outputs = _bundle(kit_root)
    owned = _owned_files(skill)
    if owned is None and skill.exists():
        raise ProjectError(f"refusing to replace unowned skill: {skill}")
    owned = owned or {}
    bootstrap = None
    if codex_home is not None:
        from .bootstrap import prepare_bootstrap
        bootstrap = prepare_bootstrap(codex_home, skill, outputs['SKILL.md'])
    # Preflight every path before any writes, including paths removed by an upgrade.
    for relative in set(outputs) | set(owned):
        target = _skill_path(skill, relative)
        if target.exists() and relative not in owned:
            raise ProjectError(f"refusing to replace unowned skill file: {target}")
        if target.exists() and not target.is_file():
            raise ProjectError(f"skill output is not a regular file: {target}")
    return skill, outputs, owned, bootstrap


def _apply_skill_install(
    skill: Path,
    outputs: dict[str, str | bytes],
    owned: dict[str, str],
    bootstrap: tuple[Path, str] | None,
) -> None:
    for relative, content in outputs.items():
        _write_payload(_skill_path(skill, relative), content)
    for relative in set(owned) - set(outputs):
        target = _skill_path(skill, relative)
        if target.exists():
            if _payload_digest(target.read_bytes()) != owned[relative]:
                continue  # Preserve edits to retired files.
            target.unlink()
    _atomic_write(_skill_path(skill, MANIFEST), json.dumps({
        "generated_by": "OrchestraKit", "files": {p: _payload_digest(t) for p, t in outputs.items()},
    }, indent=2) + "\n")
    if bootstrap is not None:
        _atomic_write(*bootstrap)


def install_skill(kit_root: Path, skills_dir: Path, *, codex_home: Path | None = None) -> Path:
    skill, outputs, owned, bootstrap = _prepare_skill_install(
        kit_root, skills_dir, codex_home
    )
    _apply_skill_install(skill, outputs, owned, bootstrap)
    return skill


def install_user_setup(kit_root: Path, skills_dir: Path, codex_home: Path) -> Path:
    """Install the portable skill, workflow bootstrap, and native hook definitions."""
    skill, outputs, owned, bootstrap = _prepare_skill_install(
        kit_root, skills_dir, codex_home
    )
    from .hooks import prepare_hooks
    source = outputs["SKILL.md"]
    if not isinstance(source, str):
        raise ProjectError("canonical skill source must be UTF-8 text")
    hook_outputs = prepare_hooks(codex_home, skill, source=source)
    _apply_skill_install(skill, outputs, owned, bootstrap)
    for path, content in hook_outputs.items():
        _atomic_write(path, content)
    errors = check_user_setup(kit_root, skills_dir, codex_home)
    if errors:
        raise ProjectError("setup verification failed: " + "; ".join(errors))
    return skill


def check_skill(kit_root: Path, skills_dir: Path, *, codex_home: Path | None = None) -> tuple[str, ...]:
    skill = skills_dir.expanduser().resolve() / "orchestra"
    if skill.is_symlink():
        return (f"refusing to check a symlinked skill: {skill}",)
    if not skill.exists():
        return (f"skill is not installed: {skill}",)
    try:
        owned = _owned_files(skill)
    except ProjectError as exc:
        return (str(exc),)
    if owned is None:
        return (f"skill is not owned by OrchestraKit: {skill}",)
    outputs = _bundle(kit_root)
    integrity = []
    for relative, digest in owned.items():
        target = _skill_path(skill, relative)
        if not target.is_file() or _payload_digest(target.read_bytes()) != digest:
            integrity.append(f"owned skill file drifted: {relative}")
    errors = []
    for relative, content in outputs.items():
        try:
            target = _skill_path(skill, relative)
        except ProjectError as exc:
            errors.append(str(exc))
            continue
        if not target.is_file() or _payload_digest(target.read_bytes()) != _payload_digest(content):
            errors.append(f"missing or drifted skill file: {relative}")
    if owned != {p: _payload_digest(t) for p, t in outputs.items()}:
        errors.append("skill manifest differs from this kit")
    if codex_home is not None:
        from .bootstrap import check_bootstrap
        errors.extend(check_bootstrap(codex_home, skill))
    return tuple(integrity + errors)


def check_user_setup(kit_root: Path, skills_dir: Path, codex_home: Path) -> tuple[str, ...]:
    errors = list(check_skill(kit_root, skills_dir, codex_home=codex_home))
    if errors:
        return tuple(errors)
    from .hooks import prepare_hooks
    skill = skills_dir.expanduser().resolve() / "orchestra"
    try:
        expected = prepare_hooks(codex_home, skill)
        for path, content in expected.items():
            if not path.is_file() or path.read_bytes() != content.encode('utf-8'):
                errors.append(f"missing or drifted hook byte content: {path}")
    except (OSError, ValueError, ProjectError) as exc:
        errors.append(str(exc))
    return tuple(errors)
