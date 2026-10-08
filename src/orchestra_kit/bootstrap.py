from __future__ import annotations

from pathlib import Path

from .project import ProjectError, _safe_path


START = '<!-- orchestra-kit:bootstrap:start -->'
END = '<!-- orchestra-kit:bootstrap:end -->'
DEFAULT_INSTRUCTION_BUDGET = 32768


def _instruction_path(codex_home: Path) -> Path:
    for name in ('AGENTS.override.md', 'AGENTS.md'):
        if (codex_home / name).is_symlink():
            raise ProjectError('refusing symlinked global instruction file')
    override = _safe_path(codex_home, 'AGENTS.override.md')
    if override.is_file() and override.read_text(encoding='utf-8').strip():
        return override
    return _safe_path(codex_home, 'AGENTS.md')


def workflow_context(skill: Path, source: str | None = None) -> str:
    from .project import _digest
    skill = skill.resolve()
    if source is None:
        source = (skill / 'SKILL.md').read_text(encoding='utf-8')
    parts = source.split('---', 2)
    if len(parts) != 3 or parts[0].strip() or 'name: orchestra' not in parts[1].splitlines():
        raise ProjectError('invalid canonical Orchestra skill header')
    body = parts[2].strip()
    if not body:
        raise ProjectError('canonical Orchestra workflow is empty')
    context = f'''Orchestra workflow is already loaded below; do not reread unchanged SKILL.md.
Apply this primary workflow to multi-step coding and verification, including
empty folders. Skip unrelated questions and trivial text edits. A bounded leaf
follows its brief and never activates root orchestration or delegates.
Source: `{skill / 'SKILL.md'}`; sha256={_digest(source)}.
Resolve references relative to `{skill / 'references'}` and the launcher
relative to `{skill}`. Respect higher and nearer instructions.

{body}'''
    if len(context.encode('utf-8')) > 8000:
        raise ProjectError('canonical workflow exceeds the host-context byte budget')
    return context


def render_bootstrap(skill: Path, source: str | None = None) -> str:
    skill_path = str(skill.resolve() / 'SKILL.md')
    if any(character in skill_path for character in ('\n', '\r', '`')):
        raise ProjectError('skill path cannot be safely included in global instructions')
    return f'''{START}
## Orchestra workflow selection

Leaf agents follow their bounded briefs. The following canonical instructions
are provided by the host; optional skill selection is unnecessary.

{workflow_context(skill, source)}
{END}'''


def _extract(text: str) -> str | None:
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise ProjectError('global instructions contain malformed OrchestraKit bootstrap markers')
    if START not in text:
        return None
    start = text.index(START)
    end = text.index(END)
    if end < start:
        raise ProjectError('global instructions contain reversed bootstrap markers')
    return text[start:end + len(END)]


def prepare_bootstrap(codex_home: Path, skill: Path, source: str | None = None) -> tuple[Path, str]:
    path = _instruction_path(codex_home.expanduser().resolve())
    if path.exists() and not path.is_file():
        raise ProjectError(f'global instruction path is not a file: {path}')
    existing = path.read_text(encoding='utf-8') if path.exists() else ''
    previous = _extract(existing)
    block = render_bootstrap(skill, source)
    if previous is not None:
        updated = existing.replace(previous, block, 1)
    else:
        prefix = existing + ('\n' if existing.endswith('\n') else '\n\n') if existing else ''
        updated = prefix + block + '\n'
    if len(updated.encode('utf-8')) > DEFAULT_INSTRUCTION_BUDGET:
        raise ProjectError('global instruction file exceeds the default Codex instruction budget; shorten it before installing bootstrap')
    return path, updated


def check_bootstrap(codex_home: Path, skill: Path) -> tuple[str, ...]:
    try:
        path = _instruction_path(codex_home.expanduser().resolve())
        text = path.read_text(encoding='utf-8') if path.is_file() else ''
        if _extract(text) != render_bootstrap(skill):
            return (f'missing or drifted OrchestraKit bootstrap: {path}',)
        if len(text.encode('utf-8')) > DEFAULT_INSTRUCTION_BUDGET:
            return (f'global instructions exceed the default Codex instruction budget: {path}',)
    except (ProjectError, OSError, UnicodeError) as exc:
        return (str(exc),)
    return ()
