"""Read observed Codex counters without reading messages into a report."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable


COUNTERS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
            'output_tokens', 'reasoning_output_tokens', 'total_tokens')
MODEL_SOURCE = 'turn_context (configured, not runtime-attested)'

def _bounded_lines(stream):
    while True:
        line=stream.readline(1_000_001)
        if not line:return
        if len(line)>1_000_000:
            while line and not line.endswith('\n'):line=stream.readline(1_000_001)
            yield 'OVERSIZED_EVENT'
        else:yield line


def _counters(value: object) -> dict[str, int | None]:
    if not isinstance(value, dict):
        raise ValueError('usage must contain numeric counters')
    result = {key: value.get(key) for key in COUNTERS}
    for key, number in result.items():
        if number is not None and (type(number) is not int or number < 0):
            raise ValueError(f'invalid usage counter: {key}')
    if result['input_tokens'] is None or result['output_tokens'] is None:
        raise ValueError('usage requires input_tokens and output_tokens')
    inp, out = result['input_tokens'], result['output_tokens']
    total = inp + out
    if result['total_tokens'] is not None and result['total_tokens'] != total:
        raise ValueError('inconsistent total_tokens usage counter')
    result['total_tokens'] = total
    for subset, whole in (('cached_input_tokens', inp), ('reasoning_output_tokens', out)):
        if result[subset] is not None and result[subset] > whole:
            raise ValueError(f'inconsistent usage subset: {subset}')
    return result


def _sum(rows: list[dict]) -> dict[str, int | None]:
    result = {}
    for key in COUNTERS:
        numbers = [row['usage'][key] for row in rows]
        result[key] = None if any(n is None for n in numbers) else sum(numbers)
    cached = result['cached_input_tokens']
    result['uncached_input_tokens'] = None if cached is None else result['input_tokens'] - cached
    return result


def summarize_usage(paths: Iterable[Path]) -> dict:
    """Deduplicate response records; cumulative events are fallback evidence only.

    Configured model context belongs to its own session, never a child's records.
    This reports supplied log coverage, not completeness of an entire agent tree.
    """
    files = list(dict.fromkeys(Path(p).expanduser().resolve() for p in paths))
    records: dict[str, dict] = {}
    fallback = []
    warnings = []
    complete = True
    for path in files:
        session = None
        model = effort = None
        contexts = set()
        native_count = 0
        cumulative = None
        cli_turns = []
        with path.open(encoding='utf-8') as stream:
            for line_number, line in enumerate(_bounded_lines(stream), 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    complete = False
                    warnings.append(f'incomplete JSON at {path}:{line_number}; report covers parsed records only')
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'turn.completed' and item.get('usage') is not None:
                    cli_turns.append({'usage': _counters(item['usage']), 'model': None,
                                      'effort': None, 'native': False})
                    continue
                payload = item.get('payload')
                if not isinstance(payload, dict):
                    continue
                kind = item.get('type')
                if kind == 'session_meta':
                    session = payload.get('id')
                elif kind == 'turn_context':
                    model = payload.get('model') if isinstance(payload.get('model'), str) else None
                    effort = payload.get('effort') if isinstance(payload.get('effort'), str) else None
                    contexts.add((model, effort))
                elif kind == 'token_usage_record':
                    native_count += 1
                    usage = _counters(payload.get('usage'))
                    response = payload.get('response_id')
                    if not isinstance(response, str) or not response:
                        response = f'{path}:{line_number}'
                        warnings.append(f'usage record lacks response_id at {path}:{line_number}; cannot deduplicate across logs')
                    own_context = bool(session) and payload.get('thread_id', session) == session
                    row = {'usage': usage, 'model': model if own_context else None,
                           'effort': effort if own_context else None, 'native': True}
                    previous = records.get(response)
                    if previous is not None:
                        if previous['usage'] != usage:
                            raise ValueError(f'conflicting usage for response_id: {response}')
                        if previous['model'] is None and row['model'] is not None:
                            records[response] = row
                        elif previous['model'] is not None and row['model'] is not None and (
                                previous['model'], previous['effort']) != (row['model'], row['effort']):
                            raise ValueError(f'conflicting configured context for response_id: {response}')
                    else:
                        records[response] = row
                elif kind == 'event_msg' and payload.get('type') == 'token_count':
                    info = payload.get('info')
                    if isinstance(info, dict) and info.get('total_token_usage') is not None:
                        cumulative = _counters(info['total_token_usage'])
        if not native_count and cli_turns:
            fallback.extend(cli_turns)
            warnings.append(f'CLI turn usage at {path}; model context, response count and cross-log deduplication unavailable')
        elif not native_count and cumulative is not None:
            single = len(contexts) == 1 and session is not None
            fallback.append({'usage': cumulative, 'model': model if single else None,
                             'effort': effort if single else None, 'native': False})
            warnings.append(f'cumulative-only usage at {path}; last event used, response count and cross-log deduplication unavailable')
        elif not native_count:
            complete = False
            warnings.append(f'no usage in supplied log: {path}')
    rows = list(records.values()) + fallback
    if not rows:
        raise ValueError('no observed usage records in supplied logs')
    groups = defaultdict(list)
    for row in rows:
        groups[row['model'], row['effort']].append(row)
    models = []
    for (model, effort), group in groups.items():
        models.append({'model': model, 'effort': effort,
                       'model_source': MODEL_SOURCE if model is not None else 'unknown',
                       'response_count': len(group) if all(r['native'] for r in group) else None,
                       'totals': _sum(group)})
    if any(row['model'] is None for row in rows):
        warnings.append('some usage lacks its own session model context; never attributed to the parent model')
    warnings.append('coverage is limited to supplied logs; counters do not prove billing cost or the effective inference model')
    return {'files': [str(p) for p in files], 'complete': complete,
            'response_count': len(records) if not fallback else None,
            'totals': _sum(rows), 'models': models, 'warnings': list(dict.fromkeys(warnings))}
