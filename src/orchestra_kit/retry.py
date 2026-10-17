"""Conservative duplicate-failure detection; never turns a failure into a pass."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .fingerprint import fingerprint_paths


def failure_state(project: Path, files: list[str], checks: list[dict[str, Any]]) -> str | None:
    """Hash declared sources and complete, exact check outcomes; unknown means abstain.

    No message normalization: different diagnostics may be evidence of progress.
    Paths in retained log names vary by attempt, so only their contents are hashed.
    """
    if not files or not checks or not any(type(c.get('returncode')) is int and c['returncode'] != 0 for c in checks):
        return None
    try:
        sources = fingerprint_paths(project, files)['aggregate_sha256']
        outcomes = []
        for check in checks:
            if (type(check.get('returncode')) is not int or check.get('output_truncated')
                    or check.get('reason')
                    or (check['returncode'] != 0 and check.get('failure_cause') != 'implementation')):
                return None
            log = Path(check['log'])
            if not log.is_absolute():
                return None
            relative = log.relative_to(project).as_posix()
            if log.stat().st_size > 1_000_000:
                return None
            state = fingerprint_paths(project, [relative])['files'][relative]
            if state['state'] != 'regular':
                return None
            outcomes.append([check['argv'], check['returncode'], check['failure_cause'], state['sha256']])
        payload = json.dumps([sources, outcomes], sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(payload.encode()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def repeated_failure(attempts: list[dict[str, Any]], route: dict[str, Any], current: str | None) -> dict | None:
    """Keep one feedback repair and all distinct model/provider/effort escalations."""
    if len(attempts) < 2 or current is None:
        return None
    previous = attempts[-2:]
    identity = (route['model'], route['effort'], route['provider'])
    if any((a['requested_model'], a['requested_effort'], a.get('requested_provider')) != identity
           or a.get('retry_state') != current for a in previous):
        return None
    return {'reason': 'repeated-failure-without-progress',
            'matching_attempts': [a['number'] for a in previous],
            'skipped_profile': route['profile'], 'next_action': 'root-reassessment'}
