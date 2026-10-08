"""Redact known credentials while draining child output into durable artifacts."""
from __future__ import annotations

import json
import os
import select
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence


MAX_RETAINED_BYTES = 10 * 1024 * 1024
TRUNCATION_MARKER = b'\n[ORCHESTRA OUTPUT TRUNCATED]\n'


class CapturedOutput:
    """Writable child pipe with capture state available after context exit."""

    def __init__(self, writer: BinaryIO) -> None:
        self._writer = writer
        self.overflowed = False
        self.retained_bytes = 0

    def fileno(self) -> int:
        return self._writer.fileno()

    def write(self, data: bytes) -> int:
        return self._writer.write(data)

    def flush(self) -> None:
        self._writer.flush()

    def close(self) -> None:
        self._writer.close()


def sensitive_values(extra: Mapping[str, str] | None = None) -> tuple[str, ...]:
    values = {value for name, value in os.environ.items()
              if name.endswith(('_API_KEY', '_ACCESS_KEY', '_PRIVATE_KEY', '_SIGNING_KEY', '_SECRET_KEY', '_TOKEN', '_SECRET', '_PASSWORD')) or name == 'API_KEY'}
    values.update((extra or {}).values())
    return tuple(value for value in values if isinstance(value, str) and value)


def _patterns(values: Sequence[str]) -> tuple[bytes, ...]:
    variants = set()
    for value in values:
        variants.add(value.encode('utf-8'))
        for ascii_only in (True, False):
            variants.add(json.dumps(value, ensure_ascii=ascii_only)[1:-1].encode('utf-8'))
    return tuple(sorted(variants, key=len, reverse=True))


def _replace(data: bytes, patterns: tuple[bytes, ...]) -> bytes:
    for pattern in patterns:
        data = data.replace(pattern, b'[REDACTED]')
    return data


def redacted_text(text: str, values: Sequence[str]) -> str:
    return _replace(text.encode('utf-8'), _patterns(values)).decode('utf-8')


@contextmanager
def sanitized_output(path: Path, values: Sequence[str]):
    patterns = _patterns(values)
    max_bytes = MAX_RETAINED_BYTES
    if max_bytes < len(TRUNCATION_MARKER):
        raise ValueError('retained output limit is smaller than its truncation marker')
    with ExitStack() as stack:
        output = stack.enter_context(path.open('wb'))
        reader_fd, writer_fd = os.pipe()
        reader = stack.enter_context(os.fdopen(reader_fd, 'rb', buffering=0))
        writer = stack.enter_context(os.fdopen(writer_fd, 'wb', buffering=0))
        capture = CapturedOutput(writer)
        stop = threading.Event()
        errors = []
        keep = max(map(len, patterns), default=1) - 1

        def retain(data: bytes) -> None:
            if not data or capture.overflowed:
                return
            retained = output.tell()
            if retained + len(data) <= max_bytes:
                output.write(data)
                capture.retained_bytes = retained + len(data)
                return
            content_limit = max_bytes - len(TRUNCATION_MARKER)
            if retained > content_limit:
                output.seek(content_limit)
                output.truncate()
            elif retained < content_limit:
                output.write(data[:content_limit - retained])
            output.write(TRUNCATION_MARKER)
            capture.retained_bytes = output.tell()
            capture.overflowed = True

        def drain():
            pending = b''
            stopping_at = None
            drained_after_stop = 0
            try:
                while True:
                    if stop.is_set():
                        stopping_at = stopping_at or time.monotonic() + .2
                        if time.monotonic() >= stopping_at or drained_after_stop >= 1024 * 1024:
                            break
                    ready, _, _ = select.select([reader], [], [], .1)
                    if not ready:
                        if stop.is_set():
                            break
                        continue
                    chunk = os.read(reader.fileno(), 65536)
                    if not chunk:
                        break
                    if stopping_at is not None:
                        drained_after_stop += len(chunk)
                    if capture.overflowed:
                        continue
                    pending += chunk
                    end = max(0, len(pending) - keep)
                    # Keep an entire match when it crosses the safe write boundary.
                    for pattern in patterns:
                        position = pending.rfind(pattern, 0, end + len(pattern))
                        if 0 <= position < end < position + len(pattern):
                            end = position
                    if end:
                        retain(_replace(pending[:end], patterns))
                        pending = pending[end:]
                retain(_replace(pending, patterns))
                output.flush()
            except Exception:
                errors.append(True)

        thread = threading.Thread(target=drain, name='orchestra-log-redaction', daemon=True)
        thread.start()
        try:
            yield capture
        finally:
            capture.close()
            stop.set()
            thread.join()
        if errors:
            raise OSError('could not retain sanitized child output')
