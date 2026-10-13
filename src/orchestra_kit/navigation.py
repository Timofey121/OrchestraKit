"""A small, local, advisory index of explicitly selected source files.

The index never discovers files and never stores source text.  Callers must
still read the referenced source before relying on a result.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable

from .log_capture import redacted_text, sensitive_values


SCHEMA = 1
MAX_PATHS = 256
MAX_FILE_BYTES = 1_000_000
MAX_SYMBOLS_PER_FILE = 1_000
MAX_NAME_CHARS = 256
MAX_INDEX_BYTES = 1_000_000
_CHUNK_BYTES = 128 * 1024
_INDEX_RELATIVE = Path(".orchestra/navigation/index.json")
_LANGUAGES = {".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".rb": "ruby", ".php": "php", ".sh": "shell"}
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:export\s+)?(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY|AWS_SECRET_ACCESS_KEY|AZURE_OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN|SLACK_TOKEN|STRIPE_SECRET_KEY|API_KEY|SECRET_KEY|PASSWORD)\s*[:=]\s*['\"][^'\"]+['\"]"
)
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|AIza[A-Za-z0-9_-]{8,})\b")
_MAX_PATH_CHARS = 512
_MAX_ERROR_CHARS = 512


class _Unreadable(Exception):
    pass


class _EncodingError(UnicodeError):
    def __init__(self, digest: str, offset: int) -> None:
        self.digest = digest
        self.offset = offset
        super().__init__(f"invalid UTF-8 at byte {offset}")


def _root(project: Path) -> Path:
    if not isinstance(project, Path):
        raise ValueError("project must be a Path")
    try:
        root = project.resolve(strict=True)
    except OSError as exc:
        raise ValueError("project cannot be resolved") from exc
    if not root.is_dir():
        raise ValueError("project must be a directory")
    return root


def _paths(paths: Iterable[str]) -> tuple[str, ...]:
    if isinstance(paths, (str, bytes)):
        raise ValueError("paths must be an iterable of path strings")
    try:
        iterator = iter(paths)
    except TypeError as exc:
        raise ValueError("paths must be an iterable of path strings") from exc
    values: list[str] = []
    for _ in range(MAX_PATHS + 1):
        try:
            value = next(iterator)
        except StopIteration:
            break
        if not isinstance(value, str) or not value or "\x00" in value or len(value) > _MAX_PATH_CHARS:
            raise ValueError("paths must be nonempty strings")
        candidate = Path(value)
        if candidate.is_absolute() or candidate.as_posix() != value or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("paths must be normalized project-relative file names")
        if value.endswith(("/", "\\")):
            raise ValueError("paths must name files")
        values.append(value)
    else:
        raise ValueError(f"paths may contain at most {MAX_PATHS} entries")
    if not values:
        raise ValueError("paths must not be empty")
    return tuple(sorted(set(values)))


def _source_path(root: Path, relative: str) -> Path:
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise _Unreadable from exc
        if stat.S_ISLNK(info.st_mode):
            raise _Unreadable
    try:
        parent = path.parent.resolve(strict=False)
    except OSError as exc:
        raise _Unreadable from exc
    if parent != root and root not in parent.parents:
        raise ValueError("path escapes project")
    return path


def _metadata(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_source(root: Path, relative: str) -> tuple[str, str] | None:
    path = _source_path(root, relative)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Unreadable from exc
    if not stat.S_ISREG(before.st_mode):
        raise _Unreadable
    if before.st_size > MAX_FILE_BYTES:
        raise ValueError(f"file exceeds {MAX_FILE_BYTES} byte limit: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _Unreadable from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _metadata(opened) != _metadata(before):
            raise _Unreadable
        digest = hashlib.sha256()
        parts: list[bytes] = []
        remaining = MAX_FILE_BYTES
        while remaining:
            chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            parts.append(chunk)
            remaining -= len(chunk)
        if not remaining and os.read(descriptor, 1):
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} byte limit: {relative}")
        after_opened = os.fstat(descriptor)
        after = path.lstat()
    except OSError as exc:
        raise _Unreadable from exc
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(after.st_mode) or _metadata(before) != _metadata(after_opened) or _metadata(before) != _metadata(after):
        raise _Unreadable
    try:
        text = b"".join(parts).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _EncodingError(digest.hexdigest(), exc.start) from exc
    values = sensitive_values()
    if _SECRET_ASSIGNMENT.search(text) or _TOKEN.search(text) or redacted_text(text, values) != text:
        raise ValueError(f"known secret material refused: {relative}")
    return digest.hexdigest(), text


def _symbols(text: str, relative: str) -> list[dict[str, Any]]:
    tree = ast.parse(text, filename=relative)
    found: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "function"
        else:
            continue
        if len(node.name) <= MAX_NAME_CHARS:
            found.append({"name": node.name, "kind": kind, "line": node.lineno})
    found.sort(key=lambda item: (item["line"], item["kind"], item["name"]))
    if len(found) > MAX_SYMBOLS_PER_FILE:
        raise ValueError(f"too many symbols in file: {relative}")
    return found


def _language(relative: str) -> str:
    return "python" if Path(relative).suffix == ".py" else _LANGUAGES.get(Path(relative).suffix, "unknown")


def _entry_from_source(relative: str, digest: str, text: str) -> dict[str, Any]:
    language = _language(relative)
    if language != "python":
        return {"path": relative, "sha256": digest, "status": "unsupported", "language": language, "parser": "unsupported", "symbols": []}
    try:
        symbols = _symbols(text, relative)
    except SyntaxError as exc:
        line = exc.lineno or 0
        message = exc.msg or "invalid syntax"
        return {"path": relative, "sha256": digest, "status": "syntax_error", "language": language, "symbols": [], "error": f"line {line}: {message}"}
    return {"path": relative, "sha256": digest, "status": "indexed", "language": language, "symbols": symbols}


def _source_or_error(root: Path, relative: str) -> tuple[str, str] | dict[str, Any]:
    language = _language(relative)
    try:
        source = _read_source(root, relative)
    except _Unreadable as exc:
        raise ValueError(f"unreadable file: {relative}") from exc
    except _EncodingError as exc:
        return {"path": relative, "sha256": exc.digest, "status": "encoding_error", "language": language, "symbols": [], "error": str(exc)}
    if source is None:
        return {"path": relative, "status": "missing", "language": language, "symbols": []}
    return source


def _entry(root: Path, relative: str) -> dict[str, Any]:
    source = _source_or_error(root, relative)
    return source if isinstance(source, dict) else _entry_from_source(relative, *source)


def _validate_index(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "files"}:
        raise ValueError("navigation index is corrupt")
    if type(value["schema"]) is not int or value["schema"] != SCHEMA or not isinstance(value["files"], list):
        raise ValueError("navigation index is corrupt")
    if len(value["files"]) > MAX_PATHS or any(not isinstance(entry, dict) for entry in value["files"]):
        raise ValueError("navigation index is corrupt")
    names = [entry.get("path") for entry in value["files"]]
    if not all(isinstance(name, str) for name in names) or tuple(names) != _paths(names):
        raise ValueError("navigation index is corrupt")
    languages = {"python", "unknown", *_LANGUAGES.values()}
    layouts = {
        "indexed": {"path", "sha256", "status", "language", "symbols"},
        "unsupported": {"path", "sha256", "status", "language", "parser", "symbols"},
        "syntax_error": {"path", "sha256", "status", "language", "symbols", "error"},
        "encoding_error": {"path", "sha256", "status", "language", "symbols", "error"},
        "missing": {"path", "status", "language", "symbols"},
    }
    for entry in value["files"]:
        status = entry.get("status")
        if status not in layouts or set(entry) != layouts[status] or not isinstance(entry["language"], str) or entry["language"] not in languages or not isinstance(entry["symbols"], list):
            raise ValueError("navigation index is corrupt")
        if "sha256" in entry and (not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("navigation index is corrupt")
        if status == "indexed" and entry["language"] != "python":
            raise ValueError("navigation index is corrupt")
        if status == "unsupported" and (entry["language"] == "python" or entry["parser"] != "unsupported" or entry["symbols"]):
            raise ValueError("navigation index is corrupt")
        if status == "syntax_error" and entry["language"] != "python":
            raise ValueError("navigation index is corrupt")
        if status in {"syntax_error", "encoding_error"} and (not isinstance(entry["error"], str) or not entry["error"] or len(entry["error"]) > _MAX_ERROR_CHARS):
            raise ValueError("navigation index is corrupt")
        if status == "missing" and entry["symbols"]:
            raise ValueError("navigation index is corrupt")
        if len(entry["symbols"]) > MAX_SYMBOLS_PER_FILE:
            raise ValueError("navigation index is corrupt")
        for symbol in entry["symbols"]:
            if not isinstance(symbol, dict) or set(symbol) != {"name", "kind", "line"} or not isinstance(symbol["name"], str) or not symbol["name"] or len(symbol["name"]) > MAX_NAME_CHARS or symbol["kind"] not in {"function", "class"} or type(symbol["line"]) is not int or symbol["line"] < 1:
                raise ValueError("navigation index is corrupt")
    return value


def _index_path(root: Path) -> Path:
    return root / _INDEX_RELATIVE


def _index_directory(root: Path, *, create: bool) -> Path:
    orchestra = root / ".orchestra"
    directory = orchestra / "navigation"
    for candidate in (orchestra, directory):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if not create:
                continue
            try:
                candidate.mkdir(mode=0o700)
            except OSError as exc:
                raise ValueError("navigation index cannot be written") from exc
            continue
        except OSError as exc:
            raise ValueError("navigation index cannot be read") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("navigation index directory is unsafe")
    return directory


def _private_mode(path: Path, mode: int) -> None:
    """Apply POSIX privacy bits when the host exposes them."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except (NotImplementedError, OSError) as exc:
        raise ValueError("navigation index permissions cannot be set") from exc


def _load(root: Path, *, required: bool) -> dict[str, Any] | None:
    _index_directory(root, create=False)
    path = _index_path(root)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise ValueError("navigation index is missing; run build_index first")
        return None
    except OSError as exc:
        raise ValueError("navigation index cannot be read") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > MAX_INDEX_BYTES:
        raise ValueError("navigation index is corrupt")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _metadata(opened) != _metadata(info):
                raise ValueError("navigation index is corrupt")
            chunks: list[bytes] = []
            remaining = MAX_INDEX_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError("navigation index cannot be read") from exc
    if len(raw) > MAX_INDEX_BYTES:
        raise ValueError("navigation index is corrupt")
    try:
        return _validate_index(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("navigation index is corrupt") from exc


def _write(root: Path, index: dict[str, Any]) -> None:
    encoded = json.dumps(index, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(encoded) > MAX_INDEX_BYTES:
        raise ValueError("navigation index exceeds size limit")
    directory = _index_directory(root, create=True)
    _private_mode(directory, 0o700)
    destination = directory / "index.json"
    temporary = directory / f".index.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("could not write navigation index")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        _private_mode(destination, 0o600)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError("navigation index cannot be written") from exc


def build_index(project: Path, paths: list[str]) -> dict[str, Any]:
    """Build a deterministic index for exactly the requested source paths."""
    root = _root(project)
    names = _paths(paths)
    previous = _load(root, required=False)
    cached = {entry["path"]: entry for entry in previous["files"]} if previous else {}
    files: list[dict[str, Any]] = []
    for relative in names:
        source = _source_or_error(root, relative)
        old = cached.get(relative)
        if not isinstance(source, dict) and old is not None and old.get("sha256") == source[0]:
            files.append(old)
            continue
        current = source if isinstance(source, dict) else _entry_from_source(relative, *source)
        if old is not None and old.get("sha256") == current.get("sha256") and current.get("sha256") is not None:
            files.append(old)
        else:
            files.append(current)
    index = {"schema": SCHEMA, "files": files}
    _write(root, index)
    return index


def _current(root: Path, entry: dict[str, Any]) -> str | None:
    try:
        source = _read_source(root, entry["path"])
    except _EncodingError as exc:
        return None if exc.digest == entry.get("sha256") else "changed"
    except (ValueError, _Unreadable, UnicodeError):
        return "unreadable"
    if source is None:
        return "missing"
    digest, _ = source
    return None if digest == entry.get("sha256") else "changed"


def search_index(project: Path, query: str, *, limit: int = 10, max_chars: int = 6000) -> dict[str, Any]:
    """Find literal symbol or path matches whose source digest is still current."""
    if not isinstance(query, str) or not query or len(query) > MAX_NAME_CHARS:
        raise ValueError("query must be a nonempty string up to 256 characters")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    if type(max_chars) is not int or not 128 <= max_chars <= 60_000:
        raise ValueError("max_chars must be an integer from 128 to 60000")
    root = _root(project)
    index = _load(root, required=True)
    needle = query.casefold()
    stale: list[str] = []
    candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for entry in index["files"]:
        state = _current(root, entry)
        if state is not None:
            stale.append(f"{state}:{entry['path']}")
            continue
        path = entry["path"]
        if entry.get("sha256") is not None and entry["status"] != "missing" and needle in path.casefold():
            candidates.append(((3, path, 0, path), {"path": path, "name": path, "kind": "path", "line": 0, "language": entry["language"], "parser_status": entry["status"]}))
        if entry["status"] != "indexed":
            continue
        for symbol in entry["symbols"]:
            name = symbol["name"]
            folded = name.casefold()
            if needle not in folded and needle not in path.casefold():
                continue
            rank = 0 if folded == needle else 1 if folded.startswith(needle) else 2 if needle in folded else 3
            candidates.append(((rank, path, symbol["line"], name), {"path": path, "name": name, "kind": symbol["kind"], "line": symbol["line"], "language": entry["language"]}))
    candidates.sort(key=lambda candidate: candidate[0])
    response: dict[str, Any] = {"schema": SCHEMA, "query": query, "results": [], "stale": [], "stale_total": len(stale), "more": False, "advisory": True}
    if stale:
        response["rebuild_hint"] = "Run build_index(project, paths) before using stale navigation results."
    if len(json.dumps(response, ensure_ascii=True, separators=(",", ":"))) > max_chars:
        raise ValueError("max_chars is too small for this query")
    for clue in stale:
        response["stale"].append(clue)
        if len(json.dumps(response, ensure_ascii=True, separators=(",", ":"))) > max_chars:
            response["stale"].pop()
            response["more"] = True
            break
    for _, item in candidates[:limit]:
        response["results"].append(item)
        if len(json.dumps(response, ensure_ascii=True, separators=(",", ":"))) > max_chars:
            response["results"].pop()
            response["more"] = True
            break
    if len(candidates) > limit:
        response["more"] = True
    return response
