"""Native lifecycle context, with configuration ownership separate from host trust."""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

from .bootstrap import workflow_context
from .project import ProjectError, _atomic_write, _digest, _safe_path

MANIFEST = '.orchestra-hooks-manifest.json'
LEAF_CONTEXT = ('You are a bounded leaf. Follow applicable project instructions and the root brief. '
                'Do not activate root orchestration or delegate. Verify your scoped result.')


def handle_hook(event: object, skill: Path, expected_sha256: str, *, leaf: bool = False) -> dict:
    if not isinstance(event, dict) or not isinstance(event.get('hook_event_name'), str):
        raise ValueError('hook input requires hook_event_name')
    name = event['hook_event_name']
    if name != 'UserPromptSubmit' and not (name == 'SessionStart' and event.get('source') == 'compact'):
        return {}
    if not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('expected skill digest must be a SHA-256 hex string')
    try:
        source_path = skill / 'SKILL.md'
        if source_path.is_symlink() or source_path.stat().st_size > 8000:
            raise ProjectError('canonical skill source is unsafe or oversized')
        source = source_path.read_text(encoding='utf-8')
        if _digest(source) != expected_sha256:
            raise ProjectError('canonical skill digest changed')
        context = workflow_context(skill, source)
        if leaf:
            context += '\n\nNative executor role hint: ' + LEAF_CONTEXT
        if len(context.encode('utf-8')) > 8000:
            raise ProjectError('hook context exceeds byte budget')
    except (OSError, UnicodeError, ProjectError):
        reason = 'Orchestra source is missing, changed, or invalid. Repair the installed skill and review the updated hook through /hooks.'
        if name == 'UserPromptSubmit':
            return {'decision': 'block', 'reason': reason}
        return {'continue': False, 'stopReason': reason}
    return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': context}}


def _definitions(skill: Path, source: str | None = None) -> dict[str, list]:
    if source is None:
        if (skill / 'SKILL.md').is_symlink():
            raise ProjectError('refusing symlinked canonical skill source')
        source = (skill / 'SKILL.md').read_text(encoding='utf-8')
    workflow_context(skill, source)  # Validate before configuring a command.
    argv = [sys.executable, str(skill / 'scripts/orchestra.py'), 'hook',
            '--skill-path', str(skill), '--expected-sha256', _digest(source)]
    handler = {'type': 'command', 'command': shlex.join(argv), 'timeout': 10,
               'additionalContextLimit': 10000, 'statusMessage': 'Orchestra: load canonical workflow'}
    return {'UserPromptSubmit': [{'hooks': [handler]}],
            'SessionStart': [{'matcher': '^compact$', 'hooks': [dict(handler)]}]}


def _validate_owned(owned: dict, skill: Path) -> dict:
    if set(owned) != {'generated_by', 'format_version', 'definitions'} or \
            owned.get('generated_by') != 'OrchestraKit' or type(owned.get('format_version')) is not int or \
            owned['format_version'] != 1:
        raise ProjectError('invalid Orchestra hook ownership manifest')
    definitions = owned['definitions']
    if not isinstance(definitions, dict) or set(definitions) != {'UserPromptSubmit', 'SessionStart'}:
        raise ProjectError('ownership manifest contains unexpected hook events')
    for name, groups in definitions.items():
        if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
            raise ProjectError('invalid owned hook group')
        group = groups[0]
        keys = {'hooks', 'matcher'} if name == 'SessionStart' else {'hooks'}
        if set(group) != keys or (name == 'SessionStart' and group['matcher'] != '^compact$'):
            raise ProjectError('invalid owned hook matcher')
        handlers = group['hooks']
        if not isinstance(handlers, list) or len(handlers) != 1 or not isinstance(handlers[0], dict):
            raise ProjectError('invalid owned hook handler')
        handler = handlers[0]
        baseline = {'type': 'command', 'timeout': 10, 'additionalContextLimit': 10000,
                    'statusMessage': 'Orchestra: load canonical workflow'}
        if set(handler) != set(baseline) | {'command'} or any(handler[k] != v for k, v in baseline.items()):
            raise ProjectError('ownership manifest claims an unrelated hook handler')
        try:
            argv = shlex.split(handler['command'])
        except (ValueError, TypeError) as exc:
            raise ProjectError('invalid owned hook command') from exc
        if len(argv) != 7 or argv[:6] != [sys.executable, str(skill / 'scripts/orchestra.py'),
                'hook', '--skill-path', str(skill), '--expected-sha256'] or not re.fullmatch('[0-9a-f]{64}', argv[6]):
            raise ProjectError('ownership manifest claims an unrelated hook command')
    return definitions


def _json_file(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_000_000:
        raise ProjectError(f'unsafe or oversized hook configuration: {path}')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ProjectError('invalid hook JSON') from exc
    if not isinstance(value, dict):
        raise ProjectError('hook configuration must be an object')
    return value


def prepare_hooks(
    codex_home: Path, skill: Path, *, source: str | None = None
) -> dict[Path, str]:
    codex_home = codex_home.expanduser().resolve()
    skill = skill.expanduser().resolve()
    path = _safe_path(codex_home, 'hooks.json')
    manifest_path = _safe_path(codex_home, MANIFEST)
    # _safe_path resolves internal symlinks too; never modify a symlinked hook source.
    if (codex_home / 'hooks.json').is_symlink() or (codex_home / MANIFEST).is_symlink():
        raise ProjectError('refusing symlinked hook configuration')
    data = _json_file(path, {'hooks': {}})
    hooks = data.setdefault('hooks', {})
    if not isinstance(hooks, dict) or not all(isinstance(v, list) for v in hooks.values()):
        raise ProjectError('hooks must map event names to lists')
    owned = _json_file(manifest_path, {})
    prior = _validate_owned(owned, skill) if manifest_path.exists() else {}
    desired = _definitions(skill, source)
    for name, definitions in prior.items():
        for definition in definitions:
            if hooks.get(name, []).count(definition) != 1:
                raise ProjectError('managed Orchestra hook was modified or removed; review it before reinstalling')
            hooks[name].remove(definition)
    for name, definitions in desired.items():
        current = hooks.setdefault(name, [])
        if any(definition in current for definition in definitions):
            raise ProjectError('refusing to take ownership of an existing unowned Orchestra hook')
        current.extend(definitions)
    manifest = {'generated_by': 'OrchestraKit', 'format_version': 1, 'definitions': desired}
    return {path: json.dumps(data, ensure_ascii=False, indent=2) + '\n',
            manifest_path: json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'}


def install_hooks(codex_home: Path, skill: Path) -> None:
    for path, content in prepare_hooks(codex_home, skill).items():
        _atomic_write(path, content)


def check_hooks(codex_home: Path, skill: Path) -> tuple[str, ...]:
    try:
        expected = prepare_hooks(codex_home, skill)
        for path, content in expected.items():
            if not path.is_file() or json.loads(path.read_text()) != json.loads(content):
                return (f'missing or drifted Orchestra hooks: {path}',)
    except (OSError, ValueError, ProjectError) as exc:
        return (str(exc),)
    return ()
