"""Host-captured, bounded review context; never substitutes for passing evidence."""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

from .evidence import verify_evidence
from .fingerprint import check_fingerprint, fingerprint_paths
from .log_capture import redacted_text, sensitive_values
from .repair_checkpoint import (_create_private_directory, _exclusive_bytes,
                                _reject_symlink_components, _stable_regular)

_FILE_BYTES = 64 * 1024
_BASE_BYTES = 128 * 1024


def _redact(value):
    if isinstance(value, str):
        return redacted_text(value, sensitive_values())
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def _write(project: Path, path: Path, value: dict) -> str:
    relative = path.relative_to(project).as_posix()
    _reject_symlink_components(project, relative)
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > 512 * 1024:
        raise ValueError('review material exceeds retained size limit')
    _exclusive_bytes(path, raw, 0o600)
    return relative


def _text(project: Path, name: str, state: dict) -> dict:
    if state['state'] == 'missing':
        return {**state, 'text': ''}
    path = _reject_symlink_components(project, name)
    if path.stat().st_size > _FILE_BYTES:
        return {**state, 'unavailable': 'size-limit'}
    raw, _ = _stable_regular(path, limit=_FILE_BYTES, label='review source')
    if hashlib.sha256(raw).hexdigest() != state['sha256']:
        raise ValueError('source changed during review capture')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return {**state, 'unavailable': 'binary'}
    if '\0' in text:
        return {**state, 'unavailable': 'binary'}
    safe = redacted_text(text, sensitive_values())
    return {**state, 'text': safe, 'redacted': safe != text}


def capture_baseline(project: Path, files: list[str], directory: Path) -> dict:
    """Capture only declared source files before the first worker, without Git."""
    _create_private_directory(project/'.orchestra/workflows', directory)
    snapshot = fingerprint_paths(project, files)
    entries = {}
    remaining = _BASE_BYTES
    for name, state in snapshot['files'].items():
        entry = _text(project, name, state)
        size = len(entry.get('text', '').encode('utf-8'))
        if size > remaining:
            entry = {**state, 'unavailable': 'total-size-limit'}
        else:
            remaining -= size
        entries[name] = entry
    if check_fingerprint(project, snapshot):
        raise ValueError('sources changed during baseline capture')
    name = _write(project, directory/'baseline.json', {
        'schema': 1, 'captured': 'before-first-worker', 'untrusted': True,
        'hashes_cover': 'original bytes; text may be redacted', 'files': entries})
    return {'path': name, 'snapshot': fingerprint_paths(project, [name])}


def review_materials(project: Path, files: list[str], baseline: dict,
                     worker: dict, directory: Path) -> dict:
    """Reference actual validated checks and selective changes, not chat history."""
    if check_fingerprint(project, baseline['snapshot']):
        raise ValueError('review baseline changed')
    cert = verify_evidence(project, worker['evidence_ref'], kind='check', required_files=files)
    before_raw, _ = _stable_regular(project/baseline['path'], limit=512*1024, label='baseline')
    before = json.loads(before_raw)
    current = fingerprint_paths(project, list(before['files']))
    changes = []
    limited = False
    room = 32_000
    for name, entry in before['files'].items():
        after = _text(project, name, current['files'][name])
        if 'text' not in entry or 'text' not in after:
            limited = True
            continue
        diff = ''.join(difflib.unified_diff(entry['text'].splitlines(True),
                       after['text'].splitlines(True), fromfile='before/'+name, tofile='after/'+name))
        if len(diff) > room:
            limited = True
        shown = diff[:room]
        changes.append(shown)
        room -= len(shown)
    report = redacted_text(str(worker.get('final') or ''), sensitive_values())
    checks = [{**_redact(check), 'log': artifact['path'], 'sha256': artifact['sha256']}
              for check, artifact in zip(cert['checks'], cert['artifacts'])]
    references = [baseline['path'], worker['evidence_ref'], *(a['path'] for a in cert['artifacts'])]
    if any(_redact(name) != name for name in references):
        raise ValueError('secret review artifact paths are refused')
    # All output remains advisory; only original requirements authorize work.
    packet = {'schema': 1, 'untrusted': True, 'baseline': baseline['path'],
              'worker_evidence': worker['evidence_ref'], 'checks': checks,
              'worker_report': report[:6000], 'worker_report_limited': len(report) > 6000,
              'diff': ''.join(changes), 'diff_limited': limited,
              'limits': 'Missing/binary/large/redacted content is not full equivalence evidence. Read current sources and baseline when needed; report gaps.'}
    manifest = _write(project, directory/f'review-{len(list(directory.glob("review-*.json")))}.json', packet)
    subjects = sorted({manifest, baseline['path'], worker['evidence_ref'],
                       *(a['path'] for a in cert['artifacts'])})
    return {'manifest': manifest, 'files': subjects,
            'snapshot': fingerprint_paths(project, subjects)}
