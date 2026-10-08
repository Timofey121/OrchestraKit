"""Audit trace metadata for evidence that a Codex skill file was actually read.

The report deliberately excludes conversation text, reasoning, command output, and
tool arguments other than the minimal public metadata required to identify a read.
"""
from __future__ import annotations

import json
import re
import shlex
from collections import defaultdict
from pathlib import Path
from typing import Iterable


MODEL_SOURCE = "turn_context (configured, not runtime-attested)"
TARGET_SKILL_NAME = "orchestra"
_RANGE = re.compile(r"^(\d+)(?:,(\d+))?p?$")
_READERS = {"cat", "sed", "head", "tail", "awk", "more", "less"}


def _output_info(value: object) -> tuple[str, int | None]:
    """Extract public display text and an explicit child exit code, if supplied."""
    if isinstance(value, str):
        try:
            wrapped = json.loads(value)
        except json.JSONDecodeError:
            return value, None
        if isinstance(wrapped, dict) and isinstance(wrapped.get("exit_code"), int) and not isinstance(wrapped["exit_code"], bool):
            text, _ = _output_info(wrapped.get("output", ""))
            return text, wrapped["exit_code"]
        return value, None
    if isinstance(value, dict):
        text, nested_exit = _output_info(value.get("output", value.get("text", "")))
        exit_code = value.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            return text, exit_code
        return text, nested_exit
    if isinstance(value, list):
        texts, exits = [], []
        for part in value:
            text, exit_code = _output_info(part)
            texts.append(text)
            if exit_code is not None:
                exits.append(exit_code)
        return "".join(texts), exits[-1] if exits else None
    return "", None


def _output_parts(value: object) -> list[tuple[str, int | None]]:
    """Keep individual native result blocks ordered for batched exec calls."""
    if isinstance(value, list):
        return [_output_info(part) for part in value]
    return [_output_info(value)]


def _json_object_after(source: str, start: int) -> dict | None:
    remainder = source[start:].lstrip()
    if not remainder.startswith("("):
        return None
    remainder = remainder[1:].lstrip()
    if not remainder.startswith("{"):
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(remainder)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _quoted_js_string(source: str, start: int) -> str | None:
    if start >= len(source) or source[start] not in {'"', "'"}:
        return None
    quote, index = source[start], start + 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
        elif source[index] == quote:
            literal = source[start:index + 1]
            try:
                if quote == '"':
                    value = json.loads(literal)
                else:
                    value = bytes(literal[1:-1], "utf-8").decode("unicode_escape")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return value if isinstance(value, str) else None
        else:
            index += 1
    return None


def _native_commands(source: object) -> tuple[list[str], int]:
    if not isinstance(source, str):
        return [], 0
    commands = []
    marker = "tools.exec_command"
    offset = 0
    unsupported = 0
    while (found := source.find(marker, offset)) >= 0:
        call_start = found + len(marker)
        payload = _json_object_after(source, call_start)
        offset = found + len(marker)
        if isinstance(payload, dict) and isinstance(payload.get("cmd"), str):
            commands.append(payload["cmd"])
            continue
        match = re.match(r"\s*\(\s*\{\s*cmd\s*:\s*", source[call_start:])
        if match:
            command = _quoted_js_string(source, call_start + match.end())
            if command is not None:
                commands.append(command)
                continue
        unsupported += 1
    return commands, unsupported


def _shell_words(command: str) -> list[list[str]]:
    """Return shell-like command words without executing or interpreting the command."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return []
    groups = []
    current: list[str] = []
    for word in words:
        if word in {"&&", "||", ";", "|"}:
            if current:
                groups.append(current)
            current = []
        else:
            current.append(word)
    if current:
        groups.append(current)
    for group in list(groups):
        for index, word in enumerate(group[:-1]):
            if word in {"-c", "-lc", "-cl"}:
                groups.extend(_shell_words(group[index + 1]))
    return groups


def _compound(command: str) -> bool:
    if '\n' in command or '\r' in command:
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&")
        lexer.whitespace_split = True
        words = list(lexer)
        if any(word in {";", "|", "||", "&", "&&"} for word in words):
            return True
        if words and Path(words[0]).name in {'sh', 'bash', 'zsh', 'dash', 'fish'}:
            for index, word in enumerate(words[:-1]):
                if word in {'-c', '-lc', '-cl'} and _compound(words[index + 1]):
                    return True
        return False
    except ValueError:
        return True


def _scope(reader: str, words: list[str], path_index: int) -> str:
    if reader == "cat":
        return "full"
    if reader == "sed":
        for word in words[1:path_index]:
            match = _RANGE.match(word)
            if match:
                end = match.group(2) or match.group(1)
                return f"partial: lines {match.group(1)}-{end}"
        return "partial: sed selection"
    return f"partial: {reader} selection"


def _reads(command: str) -> list[dict[str, str]]:
    reads = []
    for words in _shell_words(command):
        if not words:
            continue
        reader = Path(words[0]).name
        if reader not in _READERS:
            continue
        for index, word in enumerate(words[1:], 1):
            if word.endswith("/SKILL.md") or word == "SKILL.md":
                reads.append({"path": str(Path(word).expanduser()),
                              "read_scope": _scope(reader, words, index)})
    return reads


def _record(event_id: str, read: dict[str, str], source: str, evidence_level: str | None = None) -> dict:
    row = {"event_id": event_id, "path": read["path"], "source": source,
           "read_scope": read["read_scope"]}
    if evidence_level is not None:
        row["evidence_level"] = evidence_level
    return row


def _header(text: str, expected_name: str) -> bool:
    """A skill's public YAML header, without retaining the tool output itself."""
    return bool(re.search(rf"(?:^|\n)---\nname:[ \t]*{re.escape(expected_name)}(?:[ \t]+#[^\r\n]*)?[ \t]*(?:\r?\n|$)", text))


def _truncated(text: str) -> bool:
    return bool(re.search(r'(?:^|\n)\s*(?:Warning:\s*truncated output|\[?[^\n]*tokens truncated[^\n]*)',
                          text, re.IGNORECASE))


def _evidenced_read(read: dict[str, str], truncated: bool) -> dict[str, str]:
    result = dict(read)
    if truncated and result["read_scope"] == "full":
        result["read_scope"] = "partial: output truncated"
    return result


def audit_activation(paths: Iterable[Path], skill_path: Path) -> dict:
    """Return bounded evidence for native session or ``codex exec --json`` traces.

    Native custom tool calls can show that a tool emitted a completed-output header,
    but their outer result does not attest the shell command's exit status.  Legacy
    CLI command records require an explicit zero exit code plus the same header.
    """
    files = list(dict.fromkeys(Path(path).expanduser().resolve() for path in paths))
    target = str(Path(skill_path).expanduser().resolve())
    target_name = TARGET_SKILL_NAME
    attempted: list[dict] = []
    observed: list[dict] = []
    successful: list[dict] = []
    warnings: list[str] = []
    configured: list[dict] = []
    complete = True
    native_calls: dict[str, list[tuple[str, list[list[dict]], bool]]] = defaultdict(list)
    native_outputs: dict[str, list[tuple[str, int | None]]] = {}
    all_skill_reads: list[dict] = []
    activities = {"scripts": [], "code": []}
    uncertain_native = False

    for path in files:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    complete = False
                    warnings.append(f"incomplete JSON at {path}:{line_number}; report covers parsed records only")
                    continue
                if not isinstance(item, dict):
                    continue
                payload = item.get("payload")
                if item.get("type") == "turn_context" and isinstance(payload, dict):
                    model, effort = payload.get("model"), payload.get("effort")
                    if isinstance(model, str):
                        configured.append({"model": model, "effort": effort if isinstance(effort, str) else None,
                                           "source": MODEL_SOURCE})
                if item.get("type") == "response_item" and isinstance(payload, dict):
                    kind = payload.get("type")
                    if kind == "custom_tool_call" and payload.get("name") == "exec":
                        call_id, event_id = payload.get("call_id"), payload.get("id")
                        if not isinstance(call_id, str) or not isinstance(event_id, str):
                            continue
                        input_source = payload.get("input")
                        commands, unsupported = _native_commands(input_source)
                        command_reads = [_reads(command) for command in commands]
                        source_text = input_source if isinstance(input_source, str) else ""
                        target_commands = any(str(Path(read["path"]).expanduser().resolve()) == target
                                              for reads in command_reads for read in reads)
                        unsafe_target = target_commands and ("Promise.all" in source_text or any(
                            _compound(command) and any(str(Path(read["path"]).expanduser().resolve()) == target
                                                       for read in _reads(command))
                            for command in commands))
                        unsupported_target = unsupported > 0 and target in source_text
                        if any(command_reads) or unsupported_target:
                            native_calls[call_id].append((event_id, command_reads, unsupported_target or unsafe_target))
                        if unsafe_target:
                            uncertain_native = True
                            warnings.append(f"native read event {event_id} uses compound or parallel commands; child output cannot be attributed safely")
                        for command in commands:
                            words = _shell_words(command)
                            if any(group and Path(group[0]).name in {"python", "python3", "bash", "sh"} for group in words):
                                activities["scripts"].append({"event_id": event_id, "source": "native"})
                    elif kind == "custom_tool_call_output" and isinstance(payload.get("call_id"), str):
                        parts = _output_parts(payload.get("output"))
                        direct_exit = payload.get("exit_code")
                        if isinstance(direct_exit, int) and not isinstance(direct_exit, bool):
                            text = "".join(text for text, _ in parts)
                            parts = [(text, direct_exit)]
                        native_outputs[payload["call_id"]] = parts
                    elif kind == "custom_tool_call" and payload.get("name") == "exec":
                        pass
                    if kind == "custom_tool_call" and payload.get("name") == "exec" and isinstance(payload.get("input"), str) and "tools.apply_patch" in payload["input"]:
                        activities["code"].append({"event_id": payload.get("id"), "source": "native"})
                item_data = item.get("item")
                if item.get("type") == "item.completed" and isinstance(item_data, dict):
                    event_id, command = item_data.get("id"), item_data.get("command")
                    if item_data.get("type") == "file_change" and isinstance(event_id, str):
                        activities["code"].append({"event_id": event_id, "source": "cli"})
                    if not isinstance(event_id, str) or not isinstance(command, str):
                        continue
                    for read in _reads(command):
                        if str(Path(read["path"]).expanduser().resolve()) != target:
                            target_read = False
                        else:
                            target_read = True
                            attempted.append(_record(event_id, read, "cli"))
                            if _compound(command):
                                uncertain_native = True
                                warnings.append(f"CLI read event {event_id} uses a compound command; final exit cannot attest the read")
                        output = item_data.get("aggregated_output")
                        expected_name = target_name if target_read else Path(read["path"]).parent.name
                        if isinstance(output, str) and _header(output, expected_name):
                            evidenced = _evidenced_read(read, _truncated(output))
                            all_skill_reads.append(_record(event_id, evidenced, "cli"))
                            if target_read:
                                observed.append(_record(event_id, evidenced, "cli", "observed_header"))
                                if item_data.get("exit_code") == 0 and not _compound(command):
                                    successful.append(_record(event_id, evidenced, "cli", "exit_attested"))
                    if any(group and Path(group[0]).name in {"python", "python3", "bash", "sh"} for group in _shell_words(command)):
                        activities["scripts"].append({"event_id": event_id, "source": "cli"})

    for call_id, calls in native_calls.items():
        parts = native_outputs.get(call_id)
        for event_id, command_reads, unsupported_target in calls:
            if unsupported_target:
                uncertain_native = True
                warnings.append(f"native read event {event_id} has unsupported JavaScript arguments containing the target path")
            explicit_results = [part for part in parts or [] if part[1] is not None]
            if unsupported_target:
                results = []
            elif explicit_results:
                results = explicit_results
            elif len(command_reads) == 1 and parts:
                results = [("".join(text for text, _ in parts), None)]
            else:
                results = []
            for index, reads in enumerate(command_reads):
                output = results[index] if index < len(results) else None
                for read in reads:
                    target_read = str(Path(read["path"]).expanduser().resolve()) == target
                    if target_read:
                        attempted.append(_record(event_id, read, "native"))
                    if output is None:
                        if target_read:
                            warnings.append(f"native read event {event_id} has no associated output")
                        continue
                    text, child_exit = output
                    expected_name = target_name if target_read else Path(read["path"]).parent.name
                    if _header(text, expected_name):
                        evidenced = _evidenced_read(read, _truncated(text))
                        all_skill_reads.append(_record(event_id, evidenced, "native"))
                        if target_read:
                            observed.append(_record(event_id, evidenced, "native", "observed_header"))
                            if child_exit == 0:
                                successful.append(_record(event_id, evidenced, "native", "exit_attested"))
                            elif child_exit is None:
                                warnings.append(f"native read event {event_id} is observed from an output header, not exit-attested")
                            else:
                                warnings.append(f"native read event {event_id} has nonzero child exit metadata")
                    else:
                        if target_read:
                            warnings.append(f"native read event {event_id} has associated output without an exact skill header")

    grouped: dict[str, list[dict]] = defaultdict(list)
    for read in all_skill_reads:
        grouped[read["path"]].append(read)
    duplicates = []
    for path, rows in grouped.items():
        if len(rows) > 1:
            duplicates.append({"path": path, "event_ids": [row["event_id"] for row in rows],
                               "read_scopes": [row["read_scope"] for row in rows]})
    configured = list({(row["model"], row["effort"]): row for row in configured}.values())
    status = "unknown" if not complete or uncertain_native else ("pass" if successful else ("observed" if observed else "fail"))
    if not complete:
        warnings.append("partial or unparseable trace leaves activation evidence unknown")
    return {"files": [str(path) for path in files], "complete": complete, "status": status,
            "attempted": attempted, "observed": observed, "successful": successful,
            "configured_models": configured, "skill_read_duplicates": duplicates,
            "activities": activities, "warnings": list(dict.fromkeys(warnings))}
