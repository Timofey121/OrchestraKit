"""Compact, local recipes; routing and quality gates remain project-owned."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .config import ProjectConfig, load_config
from .fingerprint import fingerprint_paths
from .routing import build_brief, route_task


_RECIPES = {
    'bugfix': {
        'worker': 'Reproduce the reported bug and identify its cause. Add a regression test that fails before the fix. Make the smallest causal fix; check the regression and nearby valid/invalid cases. Preserve unrelated behavior. Report missing reproduction evidence instead of guessing.',
        'reviewer': 'Check reproduction evidence, the causal fix, regression coverage and nearby behavior against the original criteria. Report missing evidence or a remaining blocker; do not invent a reproduction.',
    },
    'feature': {
        'worker': 'Map the requested behavior to acceptance tests, including relevant invalid inputs. Observe a failing test before implementation. Add the smallest implementation within scope, then run required checks and compatibility tests. Avoid unrelated abstractions or features.',
        'reviewer': 'Check each original acceptance criterion, relevant invalid inputs, integration and compatibility. Identify concrete missing behavior or regressions; do not expand the requested feature.',
    },
    'refactor': {
        'worker': 'Run baseline checks before editing. Add characterization tests for uncovered behavior being changed. Refactor in small steps while preserving public behavior and interfaces, unless explicitly authorized otherwise. Compare before/after behavior and run required checks. Report pre-existing failures separately.',
        'reviewer': 'Check before/after behavior, interfaces and baseline evidence. Verify the requested structural change and relevant edge cases; flag unintended behavior changes or missing equivalence evidence.',
    },
}
_REQUIRED = {'recipe', 'goal', 'files', 'scope', 'acceptance', 'checks'}
_OPTIONAL = {'context', 'complexity', 'risk', 'size', 'independent', 'task_id', 'required_capabilities'}


def recipe_instructions(name: str, role: str) -> str:
    if not isinstance(name, str) or name not in _RECIPES:
        raise ValueError('recipe must be bugfix, feature or refactor')
    if role not in ('worker', 'reviewer'):
        raise ValueError('recipes support only worker and reviewer roles')
    return ('Use current supplied evidence; perform missing steps and required checks. '
            + _RECIPES[name][role])


def render_recipe_brief(config: ProjectConfig, job: dict) -> str:
    """Render one role's steps once, counting them against the brief character limit."""
    brief = build_brief(config, job['brief'])
    if 'recipe' not in job:
        return brief
    instruction = recipe_instructions(job['recipe'], job['role'])
    result = brief + f"\nRECIPE ({job['recipe']}, {job['role']})\n{instruction}\n"
    if len(result) > config.context.max_brief_chars:
        raise ValueError('recipe exceeds brief budget; narrow evidence, never drop constraints')
    return result


def compile_recipe(project: Path, kit: Path, task: Any) -> dict:
    """Build an ordinary bounded job without writing files or dispatching a model."""
    if not isinstance(task, dict) or set(task) - (_REQUIRED | _OPTIONAL) or _REQUIRED - set(task):
        raise ValueError('recipe task requires recipe, goal, files, scope, acceptance and checks')
    recipe_instructions(task['recipe'], 'worker')
    for key in ('goal', 'scope'):
        if not isinstance(task[key], str) or not task[key].strip():
            raise ValueError(f'{key} must be nonempty text')
    files = task['files']
    if (not isinstance(files, list) or not files
            or not all(isinstance(p, str) for p in files) or len(set(files)) != len(files)):
        raise ValueError('files must be a nonempty list of unique relative file names')
    fingerprint_paths(project, files)
    criteria = task['acceptance']
    if not isinstance(criteria, list) or not criteria or not all(isinstance(c, str) and c.strip() for c in criteria):
        raise ValueError('acceptance must be a nonempty list of criteria')
    data = copy.deepcopy(task)
    job = {
        'recipe': data['recipe'], 'role': 'worker',
        'complexity': data.get('complexity', 'standard'), 'risk': data.get('risk', 'medium'),
        'size': data.get('size', 'small'), 'independent': data.get('independent', False),
        'evidence_files': data['files'], 'checks': data['checks'],
        'brief': {
            'GOAL': data['goal'], 'SOURCES OF TRUTH': data['files'], 'SCOPE': data['scope'],
            'ACCEPTANCE CRITERIA': data['acceptance'],
            'VERIFICATION': {'checks': data['checks'], 'rule': 'Retain actual results; report unavailable verification.'},
            'CONTEXT': data.get('context', 'No additional context.'),
        },
    }
    for key in ('task_id', 'required_capabilities'):
        if key in data:
            job[key] = data[key]
    from .execution import _validate_job
    job = _validate_job(job)
    config = load_config(project, kit)
    route_task(config, **{key: job[key] for key in ('role', 'complexity', 'risk', 'size', 'independent', 'required_capabilities')})
    render_recipe_brief(config, job)
    return job
