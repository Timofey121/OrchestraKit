"""Per-user Windows Task Scheduler service for the shared loopback dashboard."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import locale
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .project import _atomic_write


TASK_NAME_PREFIX = r"\OrchestraKit UI "
TASK_NOT_FOUND_HRESULT = 0x80070002
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
DESCRIPTION = "OrchestraKit per-user UI service"
RECOVERY_START_BOUNDARY = "2000-01-01T00:00:00"
_MAX_CONFIG_SIZE = 128 * 1024
_SCHED_E_TASK_NOT_RUNNING = 0x8004130B
_MISSING = object()


@dataclass(frozen=True)
class ServicePaths:
    directory: Path
    task_xml: Path
    runner: Path
    manifest: Path
    pid: Path
    log: Path


def _paths(home: Path | None = None) -> ServicePaths:
    """Return paths under the user's LocalAppData convention.

    An explicit ``home`` is treated as the Windows profile root. The default
    prefers LOCALAPPDATA and falls back to ``~/AppData/Local``.
    """
    if home is None:
        configured = os.environ.get("LOCALAPPDATA")
        local = Path(configured).expanduser().resolve() if configured else (
            Path.home().expanduser().resolve() / "AppData" / "Local"
        )
        boundary = local.parent
    else:
        profile = Path(home).expanduser().resolve()
        local = profile / "AppData" / "Local"
        boundary = profile
    directory = local / "OrchestraKit"
    paths = ServicePaths(
        directory=directory,
        task_xml=directory / "ui-task.xml",
        runner=directory / "ui-runner.py",
        manifest=directory / "ui-service.json",
        pid=directory / "ui.pid",
        log=directory / "ui.log",
    )
    for path in (directory, paths.task_xml, paths.runner, paths.manifest, paths.pid, paths.log,
                 directory / "install.lock"):
        for component in (path, *path.parents):
            if component == boundary:
                break
            if component.is_symlink():
                raise ValueError(f"refusing symlink in UI service path: {component}")
        if path == directory:
            if path.exists() and not path.is_dir():
                raise ValueError(f"UI service path is not a directory: {path}")
        elif path.exists() and not path.is_file():
            raise ValueError(f"UI service path is not a regular file: {path}")
    return paths


def _listening(port: int) -> bool:
    from .ui_service import _listening as check
    return check(port)


def _healthy(port: int, pid: int) -> bool:
    from .ui_service import _healthy as check
    return check(port, pid)


def _lock(directory: Path):
    from .ui_service import _lock as lock
    return lock(directory)


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        return value.decode("utf-16")
    if len(value) >= 4 and (value[:4] == b"<\x00?\x00" or value[:4] == b"\x00<\x00?"):
        return value.decode("utf-16-le" if value[0] else "utf-16-be")
    encoding = locale.getpreferredencoding(False) or "utf-8"
    try:
        return value.decode(encoding)
    except UnicodeDecodeError:
        return value.decode("utf-8", errors="replace")


def _schtasks(*args: str, required: bool = True) -> subprocess.CompletedProcess:
    argv = ["schtasks.exe", *args]
    try:
        result = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
    except FileNotFoundError as exc:
        raise ValueError("Windows Task Scheduler is unavailable: schtasks.exe was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Windows Task Scheduler timed out; inspect ui-autostart status") from exc
    if required and result.returncode:
        operation = args[0].lstrip("/").lower() if args else "operation"
        detail = _decode_output(result.stderr).strip() or _decode_output(result.stdout).strip()
        suffix = f": {detail[:500]}" if detail else ""
        raise ValueError(f"Windows Task Scheduler {operation} failed{suffix}")
    return result


def _task_name(sid: str) -> str:
    return TASK_NAME_PREFIX + sid


def _query_task(task_name: str) -> str | None:
    result = _schtasks("/query", "/tn", task_name, "/xml", "/hresult", required=False)
    if result.returncode == 0:
        xml = _decode_output(result.stdout).lstrip("\ufeff").strip()
        if not xml:
            raise ValueError("Windows Task Scheduler returned an empty task definition")
        return xml
    # /hresult gives stable machine-readable codes, avoiding localized stderr.
    # Only the documented Win32 and Task Scheduler not-found results mean absent.
    if (result.returncode & 0xFFFFFFFF) == TASK_NOT_FOUND_HRESULT:
        return None
    detail = _decode_output(result.stderr).strip() or "query failed"
    raise ValueError(f"Windows Task Scheduler is unavailable: {detail[:500]}")


def _current_user_sid() -> str:
    argv = ["whoami.exe", "/user", "/fo", "csv", "/nh"]
    try:
        result = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
    except FileNotFoundError as exc:
        raise ValueError("cannot determine the current Windows user SID: whoami.exe was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("determining the current Windows user SID timed out") from exc
    if result.returncode:
        detail = _decode_output(result.stderr).strip()
        raise ValueError(f"cannot determine the current Windows user SID: {detail[:500]}")
    try:
        row = next(csv.reader(io.StringIO(_decode_output(result.stdout).lstrip("\ufeff"))))
        sid = row[1].strip()
    except (IndexError, StopIteration, csv.Error) as exc:
        raise ValueError("cannot parse the current Windows user SID") from exc
    if not sid.startswith("S-") or not all(part.isdigit() for part in sid[2:].split("-")):
        raise ValueError("cannot parse the current Windows user SID")
    return sid


def _is_sid(value: str) -> bool:
    return value.startswith("S-") and all(part.isdigit() for part in value[2:].split("-"))


def _account_name_for_sid(sid: str) -> str:
    """Resolve an owned SID to its canonical local/domain account name."""
    if sys.platform != "win32" or not _is_sid(sid):
        raise ValueError("cannot resolve an invalid Windows user SID")
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        convert = advapi32.ConvertStringSidToSidW
        convert.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
        convert.restype = wintypes.BOOL
        lookup = advapi32.LookupAccountSidW
        lookup.argtypes = [
            wintypes.LPCWSTR, wintypes.LPVOID, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ]
        lookup.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.LPVOID]
        kernel32.LocalFree.restype = wintypes.LPVOID

        sid_pointer = wintypes.LPVOID()
        if not convert(sid, ctypes.byref(sid_pointer)):
            raise ValueError("cannot resolve the Windows user SID")
        try:
            name_size = wintypes.DWORD()
            domain_size = wintypes.DWORD()
            sid_type = wintypes.DWORD()
            ctypes.set_last_error(0)
            lookup(None, sid_pointer, None, ctypes.byref(name_size), None,
                   ctypes.byref(domain_size), ctypes.byref(sid_type))
            if ctypes.get_last_error() != 122 or not name_size.value:
                raise ValueError("cannot resolve the Windows user SID")
            name = ctypes.create_unicode_buffer(name_size.value)
            domain = ctypes.create_unicode_buffer(max(domain_size.value, 1))
            if not lookup(None, sid_pointer, name, ctypes.byref(name_size), domain,
                          ctypes.byref(domain_size), ctypes.byref(sid_type)):
                raise ValueError("cannot resolve the Windows user SID")
            return f"{domain.value}\\{name.value}" if domain.value else name.value
        finally:
            kernel32.LocalFree(sid_pointer)
    except (AttributeError, OSError) as exc:
        raise ValueError("cannot resolve the Windows user SID") from exc


def _element(parent: ET.Element, name: str, text: str | None = None, **attributes: str) -> ET.Element:
    child = ET.SubElement(parent, f"{{{TASK_NAMESPACE}}}{name}", attributes)
    child.text = text
    return child


def _task_python() -> Path:
    executable = Path(sys.executable).resolve()
    if executable.name.lower() == "python.exe":
        windowless = executable.with_name("pythonw.exe")
        if windowless.is_file():
            return windowless
    return executable


def _task_source(sid: str, runner: Path, working_directory: Path,
                 python_executable: Path) -> bytes:
    ET.register_namespace("", TASK_NAMESPACE)
    task = ET.Element(f"{{{TASK_NAMESPACE}}}Task", {"version": "1.2"})
    registration = _element(task, "RegistrationInfo")
    _element(registration, "Description", DESCRIPTION)
    triggers = _element(task, "Triggers")
    trigger = _element(triggers, "LogonTrigger")
    _element(trigger, "Enabled", "true")
    _element(trigger, "UserId", sid)
    # RestartOnFailure only covers scheduler/action start failures. This
    # indefinite one-minute trigger also relaunches an action that exited or
    # was terminated; IgnoreNew keeps a healthy instance untouched.
    recovery = _element(triggers, "TimeTrigger")
    _element(recovery, "StartBoundary", RECOVERY_START_BOUNDARY)
    repetition = _element(recovery, "Repetition")
    _element(repetition, "Interval", "PT1M")
    principals = _element(task, "Principals")
    principal = _element(principals, "Principal", id="OrchestraKitUser")
    _element(principal, "UserId", sid)
    _element(principal, "LogonType", "InteractiveToken")
    _element(principal, "RunLevel", "LeastPrivilege")
    settings = _element(task, "Settings")
    _element(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _element(settings, "DisallowStartIfOnBatteries", "false")
    _element(settings, "StopIfGoingOnBatteries", "false")
    _element(settings, "AllowHardTerminate", "true")
    _element(settings, "StartWhenAvailable", "true")
    _element(settings, "RunOnlyIfNetworkAvailable", "false")
    _element(settings, "AllowStartOnDemand", "true")
    _element(settings, "Enabled", "true")
    _element(settings, "Hidden", "false")
    _element(settings, "RunOnlyIfIdle", "false")
    _element(settings, "WakeToRun", "false")
    _element(settings, "ExecutionTimeLimit", "PT0S")
    _element(settings, "Priority", "7")
    restart = _element(settings, "RestartOnFailure")
    _element(restart, "Interval", "PT1M")
    # Count is an unsigned byte in the Task Scheduler schema. Its maximum keeps
    # restart recovery enabled for the longest supported run of failures.
    _element(restart, "Count", "255")
    actions = _element(task, "Actions", Context="OrchestraKitUser")
    execute = _element(actions, "Exec")
    _element(execute, "Command", str(python_executable))
    _element(execute, "Arguments", subprocess.list2cmdline([str(runner)]))
    _element(execute, "WorkingDirectory", str(working_directory))
    ET.indent(task, space="  ")
    return ET.tostring(task, encoding="utf-16", xml_declaration=True) + "\n".encode("utf-16-le")


def _runner_source(launcher: Path, codex_home: Path, paths: ServicePaths, port: int) -> str:
    argv = [str(launcher), "ui", "--all-projects", "--no-browser", "--host", "127.0.0.1",
            "--port", str(port), "--codex-home", str(codex_home)]
    return f'''# Generated by OrchestraKit. Do not edit.
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

LAUNCHER = Path({str(launcher)!r})
LOG = Path({str(paths.log)!r})
PID = Path({str(paths.pid)!r})
ARGV = {argv!r}


def _write_pid() -> None:
    temporary = PID.with_name(PID.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(str(os.getpid()) + "\\n", encoding="ascii")
    os.replace(temporary, PID)


def main() -> None:
    if LOG.is_symlink() or (LOG.exists() and not LOG.is_file()):
        raise RuntimeError("refusing unsafe OrchestraKit UI log path")
    descriptor = os.open(LOG, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
    finally:
        if descriptor > 2:
            os.close(descriptor)
    # pythonw starts without Python stream wrappers even though descriptors 1
    # and 2 can be redirected. Recreate them so print/logging reach the log.
    sys.stdout = open(1, "w", encoding="utf-8", errors="backslashreplace",
                      buffering=1, closefd=False)
    sys.stderr = open(2, "w", encoding="utf-8", errors="backslashreplace",
                      buffering=1, closefd=False)
    _write_pid()
    try:
        sys.argv = ARGV
        runpy.run_path(str(LAUNCHER), run_name="__main__")
    finally:
        try:
            if PID.read_text(encoding="ascii").strip() == str(os.getpid()):
                PID.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
'''


def _only(root: ET.Element, path: str) -> ET.Element:
    found = root.findall(path, {"t": TASK_NAMESPACE})
    if len(found) != 1:
        raise ValueError("invalid task structure")
    return found[0]


def _child_names(element: ET.Element) -> list[str]:
    prefix = f"{{{TASK_NAMESPACE}}}"
    names = []
    for child in element:
        if not child.tag.startswith(prefix):
            raise ValueError("unexpected task namespace")
        names.append(child.tag[len(prefix):])
    return names


def _check_children(element: ET.Element, required: set[str], optional: set[str] = frozenset()) -> None:
    names = _child_names(element)
    if len(names) != len(set(names)) or set(names) - required - optional or not required <= set(names):
        raise ValueError("unexpected task fields")


def _optional_default(root: ET.Element, path: str, expected: str) -> None:
    found = root.findall(path, {"t": TASK_NAMESPACE})
    if len(found) > 1:
        raise ValueError("duplicate task default")
    if found and (found[0].text or "").strip().lower() != expected.lower():
        raise ValueError("nondefault task constraint")


def _text(root: ET.Element, path: str, default: object = _MISSING) -> str:
    found = root.findall(path, {"t": TASK_NAMESPACE})
    if len(found) > 1:
        raise ValueError("duplicate task value")
    if not found:
        if default is _MISSING:
            raise ValueError("missing task value")
        return str(default)
    if found[0].text is None or not found[0].text.strip():
        raise ValueError("empty task value")
    return found[0].text.strip()


def _task_signature(source: str | bytes, *, allow_disabled: bool = False) -> dict[str, str]:
    try:
        root = ET.fromstring(source)
        if root.tag != f"{{{TASK_NAMESPACE}}}Task":
            raise ValueError("invalid task namespace")
        triggers = _only(root, "t:Triggers")
        principals = _only(root, "t:Principals")
        actions = _only(root, "t:Actions")
        trigger_names = _child_names(triggers)
        if (sorted(trigger_names) != ["LogonTrigger", "TimeTrigger"]
                or [child.tag for child in principals] != [f"{{{TASK_NAMESPACE}}}Principal"]
                or [child.tag for child in actions] != [f"{{{TASK_NAMESPACE}}}Exec"]):
            raise ValueError("unexpected task trigger, principal, or action")
        principal = _only(root, "t:Principals/t:Principal")
        trigger = _only(root, "t:Triggers/t:LogonTrigger")
        recovery = _only(root, "t:Triggers/t:TimeTrigger")
        repetition = _only(root, "t:Triggers/t:TimeTrigger/t:Repetition")
        execute = _only(root, "t:Actions/t:Exec")
        if (principal.get("id") != "OrchestraKitUser"
                or actions.get("Context") != "OrchestraKitUser"):
            raise ValueError("unexpected task security context")
        expected_exec_children = [
            f"{{{TASK_NAMESPACE}}}Command",
            f"{{{TASK_NAMESPACE}}}Arguments",
            f"{{{TASK_NAMESPACE}}}WorkingDirectory",
        ]
        if [child.tag for child in execute] != expected_exec_children:
            raise ValueError("unexpected task execution fields")
        registration = _only(root, "t:RegistrationInfo")
        settings = _only(root, "t:Settings")
        _check_children(root, {
            "RegistrationInfo", "Triggers", "Principals", "Settings", "Actions",
        })
        _check_children(registration, {"Description"}, {"URI"})
        _check_children(trigger, {"UserId"}, {"Enabled", "Delay", "ExecutionTimeLimit"})
        _check_children(recovery, {"StartBoundary", "Repetition"}, {
            "Enabled", "ExecutionTimeLimit",
        })
        _check_children(repetition, {"Interval"}, {"StopAtDurationEnd"})
        _check_children(principal, {"UserId", "LogonType"}, {
            "RunLevel", "ProcessTokenSidType",
        })
        _check_children(settings, {
            "DisallowStartIfOnBatteries", "StopIfGoingOnBatteries",
            "StartWhenAvailable", "ExecutionTimeLimit", "RestartOnFailure",
        }, {
            "MultipleInstancesPolicy", "AllowHardTerminate", "RunOnlyIfNetworkAvailable",
            "AllowStartOnDemand", "Enabled", "Hidden", "RunOnlyIfIdle", "WakeToRun",
            "Priority",
            "IdleSettings", "DeleteExpiredTaskAfter", "UseUnifiedSchedulingEngine",
            "DisallowStartOnRemoteAppSession", "Volatile",
        })
        restart = _only(root, "t:Settings/t:RestartOnFailure")
        _check_children(restart, {"Interval", "Count"})
        _optional_default(root, "t:Triggers/t:LogonTrigger/t:Delay", "PT0M")
        _optional_default(root, "t:Triggers/t:LogonTrigger/t:ExecutionTimeLimit", "PT72H")
        _optional_default(root, "t:Triggers/t:TimeTrigger/t:Enabled", "true")
        _optional_default(root, "t:Triggers/t:TimeTrigger/t:ExecutionTimeLimit", "PT72H")
        _optional_default(
            root, "t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd", "false"
        )
        _optional_default(root, "t:Principals/t:Principal/t:ProcessTokenSidType", "None")
        for field, value in (
            ("DeleteExpiredTaskAfter", "PT0S"),
            ("UseUnifiedSchedulingEngine", "false"),
            ("DisallowStartOnRemoteAppSession", "false"),
            ("Volatile", "false"),
        ):
            _optional_default(root, f"t:Settings/t:{field}", value)
        idle = root.findall("t:Settings/t:IdleSettings", {"t": TASK_NAMESPACE})
        if len(idle) > 1:
            raise ValueError("duplicate idle settings")
        if idle:
            _check_children(idle[0], set(), {
                "Duration", "WaitTimeout", "StopOnIdleEnd", "RestartOnIdle",
            })
            for field, value in (
                ("Duration", "PT10M"), ("WaitTimeout", "PT1H"),
                ("StopOnIdleEnd", "true"), ("RestartOnIdle", "false"),
            ):
                _optional_default(root, f"t:Settings/t:IdleSettings/t:{field}", value)
        values = {
            "description": ("t:RegistrationInfo/t:Description", _MISSING),
            "trigger_enabled": ("t:Triggers/t:LogonTrigger/t:Enabled", "true"),
            "trigger_user": ("t:Triggers/t:LogonTrigger/t:UserId", _MISSING),
            "recovery_start": ("t:Triggers/t:TimeTrigger/t:StartBoundary", _MISSING),
            "recovery_interval": (
                "t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", _MISSING,
            ),
            "principal_user": ("t:Principals/t:Principal/t:UserId", _MISSING),
            "logon_type": ("t:Principals/t:Principal/t:LogonType", _MISSING),
            # Task Scheduler omits LeastPrivilege from its canonical export.
            "run_level": ("t:Principals/t:Principal/t:RunLevel", "LeastPrivilege"),
            "multiple_instances": ("t:Settings/t:MultipleInstancesPolicy", "IgnoreNew"),
            "battery_start": ("t:Settings/t:DisallowStartIfOnBatteries", _MISSING),
            "battery_stop": ("t:Settings/t:StopIfGoingOnBatteries", _MISSING),
            "hard_terminate": ("t:Settings/t:AllowHardTerminate", "true"),
            "start_available": ("t:Settings/t:StartWhenAvailable", _MISSING),
            "network_required": ("t:Settings/t:RunOnlyIfNetworkAvailable", "false"),
            "start_demand": ("t:Settings/t:AllowStartOnDemand", "true"),
            "enabled": ("t:Settings/t:Enabled", "true"),
            "hidden": ("t:Settings/t:Hidden", "false"),
            "idle_required": ("t:Settings/t:RunOnlyIfIdle", "false"),
            "wake": ("t:Settings/t:WakeToRun", "false"),
            "execution_limit": ("t:Settings/t:ExecutionTimeLimit", _MISSING),
            "priority": ("t:Settings/t:Priority", "7"),
            "restart_interval": ("t:Settings/t:RestartOnFailure/t:Interval", _MISSING),
            "restart_count": ("t:Settings/t:RestartOnFailure/t:Count", _MISSING),
            "command": ("t:Actions/t:Exec/t:Command", _MISSING),
            "arguments": ("t:Actions/t:Exec/t:Arguments", _MISSING),
            "working_directory": ("t:Actions/t:Exec/t:WorkingDirectory", _MISSING),
        }
        signature = {key: _text(root, path, default) for key, (path, default) in values.items()}
        principal_sid = signature["principal_user"]
        trigger_user = signature["trigger_user"]
        if not _is_sid(principal_sid):
            raise ValueError("invalid task principal SID")
        if _is_sid(trigger_user):
            same_user = trigger_user.casefold() == principal_sid.casefold()
        else:
            same_user = (
                trigger_user.casefold() == _account_name_for_sid(principal_sid).casefold()
            )
        if not same_user:
            raise ValueError("task trigger belongs to a different user")
        # Scheduler may render the trigger identity as DOMAIN\user while
        # retaining the principal SID. Canonicalize both forms to that SID.
        signature["trigger_user"] = principal_sid
        if (signature["description"] != DESCRIPTION
                or signature["trigger_enabled"].lower() != "true"
                or signature["recovery_start"] != RECOVERY_START_BOUNDARY
                or signature["recovery_interval"] != "PT1M"
                or signature["logon_type"] != "InteractiveToken"
                or signature["run_level"] != "LeastPrivilege"
                or signature["multiple_instances"] != "IgnoreNew"
                or signature["battery_start"].lower() != "false"
                or signature["battery_stop"].lower() != "false"
                or signature["hard_terminate"].lower() != "true"
                or signature["start_available"].lower() != "true"
                or signature["network_required"].lower() != "false"
                or signature["start_demand"].lower() != "true"
                or signature["enabled"].lower()
                not in ({"true", "false"} if allow_disabled else {"true"})
                or signature["hidden"].lower() != "false"
                or signature["idle_required"].lower() != "false"
                or signature["wake"].lower() != "false"
                or signature["execution_limit"] != "PT0S"
                or signature["priority"] != "7"
                or signature["restart_interval"] != "PT1M"
                or signature["restart_count"] != "255"):
            raise ValueError("unsafe task settings")
        return signature
    except (ET.ParseError, TypeError, AttributeError) as exc:
        raise ValueError("invalid task XML") from exc


def _owned(paths: ServicePaths) -> dict[str, object] | None:
    required = (paths.task_xml, paths.runner, paths.manifest)
    present = [path.exists() for path in required]
    if not any(present):
        return None
    if not all(present):
        raise ValueError("UI service ownership missing or configuration modified; preserve and inspect the files")
    try:
        for path in required:
            if path.stat().st_size > _MAX_CONFIG_SIZE:
                raise ValueError("oversized service configuration")
        task_data = paths.task_xml.read_bytes()
        runner_data = paths.runner.read_bytes()
        owner = json.loads(paths.manifest.read_text(encoding="utf-8"))
        if not isinstance(owner, dict):
            raise ValueError("invalid manifest")
        sid = owner.get("sid")
        if not isinstance(sid, str) or not sid:
            raise ValueError("invalid manifest")
        expected = {
            "generated_by": "OrchestraKit",
            "format_version": 1,
            "task_name": _task_name(sid),
            "task_sha256": hashlib.sha256(task_data).hexdigest(),
            "runner_sha256": hashlib.sha256(runner_data).hexdigest(),
        }
        if any(owner.get(key) != value for key, value in expected.items()):
            raise ValueError("manifest mismatch")
        port = owner.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid service port")
        for key in ("launcher", "codex_home", "sid", "python_executable"):
            if not isinstance(owner.get(key), str) or not owner[key]:
                raise ValueError("invalid manifest")
        signature = _task_signature(task_data)
        if (signature["trigger_user"] != owner["sid"]
                or signature["command"] != owner["python_executable"]
                or signature["arguments"] != subprocess.list2cmdline([str(paths.runner)])):
            raise ValueError("task and manifest differ")
        return {**owner, "signature": signature}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "UI service ownership missing or configuration modified; preserve and inspect the files"
        ) from exc


def _validate_registered(source: str, paths: ServicePaths, *,
                         allow_disabled: bool = False) -> dict[str, str]:
    try:
        registered = _task_signature(source, allow_disabled=allow_disabled)
        expected = _task_signature(paths.task_xml.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError("registered task definition is modified; preserve and inspect it") from exc
    comparison = registered
    if allow_disabled and registered["enabled"].lower() == "false":
        comparison = {**registered, "enabled": expected["enabled"]}
    if comparison != expected:
        raise ValueError("registered task definition is modified; preserve and inspect it")
    return registered


def _read_pid(path: Path) -> int | None:
    try:
        if path.stat().st_size > 32:
            return None
        text = path.read_text(encoding="ascii").strip()
        pid = int(text)
        if pid <= 0 or str(pid) != text:
            return None
        return pid
    except (OSError, ValueError):
        return None


def _pid_running(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
                exit_code.value == still_active
            )
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return False


def _base_status() -> dict[str, bool]:
    return {"supported": True, "installed": False, "running": False, "healthy": False}


def _refuse_orphaned_process(paths: ServicePaths, port: int) -> None:
    pid = _read_pid(paths.pid)
    if pid is not None and _pid_running(pid):
        raise ValueError(
            f"UI task registration is missing but recorded PID {pid} is still active; "
            "preserving the owned files for inspection"
        )
    if _listening(port):
        raise ValueError(
            f"UI task registration is missing and port {port} is occupied; "
            "preserving the owned files for inspection"
        )


def service_status(*, home: Path | None = None):
    if sys.platform != "win32":
        return {"supported": False, "installed": False, "running": False, "healthy": False}
    paths = _paths(home)
    owner = _owned(paths)
    current_sid = _current_user_sid()
    task_name = str(owner["task_name"]) if owner is not None else _task_name(current_sid)
    if owner is not None and owner["sid"] != current_sid:
        raise ValueError("UI service ownership belongs to a different Windows user")
    registered = _query_task(task_name)
    if owner is None:
        if registered is not None:
            raise ValueError("UI Task Scheduler name already exists without Orchestra ownership")
        return _base_status()
    if registered is None:
        return {**_base_status(), "task": task_name, "task_xml": str(paths.task_xml),
                "runner": str(paths.runner), "log": str(paths.log)}
    _validate_registered(registered, paths)
    port = int(owner["port"])
    pid = _read_pid(paths.pid)
    running = pid is not None and _pid_running(pid)
    return {"supported": True, "installed": True, "running": running,
            "healthy": running and _healthy(port, pid), "pid": pid,
            "url": f"http://127.0.0.1:{port}", "task": task_name,
            "task_xml": str(paths.task_xml), "runner": str(paths.runner), "log": str(paths.log)}


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".orchestra-",
                                     delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            temporary.replace(path)
        finally:
            handle.close()
            temporary.unlink(missing_ok=True)


def _write_installation(paths: ServicePaths, task_source: bytes, runner_source: str,
                        launcher: Path, codex_home: Path, sid: str, port: int,
                        python_executable: Path) -> None:
    paths.directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(paths.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(descriptor)
    _atomic_write_bytes(paths.task_xml, task_source)
    _atomic_write(paths.runner, runner_source)
    task_data = task_source
    runner_data = runner_source.encode("utf-8")
    owner = {
        "generated_by": "OrchestraKit",
        "format_version": 1,
        "task_name": _task_name(sid),
        "task_sha256": hashlib.sha256(task_data).hexdigest(),
        "runner_sha256": hashlib.sha256(runner_data).hexdigest(),
        "launcher": str(launcher),
        "codex_home": str(codex_home),
        "sid": sid,
        "port": port,
        "python_executable": str(python_executable),
    }
    _atomic_write(paths.manifest, json.dumps(owner, sort_keys=True) + "\n")
    for path in (paths.task_xml, paths.runner, paths.manifest, paths.log):
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise ValueError(f"cannot restrict UI service file permissions: {path}") from exc


def install_service(skill: Path, codex_home: Path, *, home: Path | None = None, port: int = 8731):
    if sys.platform != "win32":
        raise ValueError("UI autostart service requires Windows; use orchestra ui on this platform")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("UI service port must be an integer from 1 to 65535")
    skill = Path(skill).expanduser()
    if skill.is_symlink():
        raise ValueError("refusing symlinked Orchestra skill")
    launcher = skill.resolve() / "scripts" / "orchestra.py"
    if launcher.is_symlink() or not launcher.is_file():
        raise ValueError("install the Orchestra skill with setup before enabling UI autostart")
    codex_home = Path(codex_home).expanduser().resolve()
    working_directory = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()
    paths = _paths(home)
    with _lock(paths.directory):
        owner = _owned(paths)
        current_sid = _current_user_sid()
        if owner is not None and owner["sid"] != current_sid:
            raise ValueError("UI service ownership belongs to a different Windows user")
        task_name = str(owner["task_name"]) if owner is not None else _task_name(current_sid)
        registered = _query_task(task_name)
        if owner is None:
            if registered is not None:
                raise ValueError("UI Task Scheduler name already exists without Orchestra ownership")
            if _listening(port):
                raise ValueError(f"UI port {port} is occupied; stop the manual server or choose another --port")
            sid = current_sid
            python_executable = _task_python()
            runner_source = _runner_source(launcher, codex_home, paths, port)
            task_source = _task_source(sid, paths.runner, working_directory, python_executable)
            _write_installation(paths, task_source, runner_source, launcher, codex_home, sid, port,
                                python_executable)
            owner = _owned(paths)
        else:
            if (owner["launcher"] != str(launcher) or owner["codex_home"] != str(codex_home)
                    or owner["port"] != port):
                raise ValueError(
                    "UI autostart settings differ; uninstall the owned service before reinstalling"
                )
            if registered is not None:
                _validate_registered(registered, paths)
                status = service_status(home=home)
                if status["healthy"]:
                    return status
                if not status["running"] and _listening(port):
                    raise ValueError(
                        f"UI port {port} is occupied; stop the manual server or choose another --port"
                    )
            else:
                _refuse_orphaned_process(paths, port)
        if registered is None:
            _schtasks("/create", "/tn", task_name, "/xml", str(paths.task_xml), "/hresult")
            registered = _query_task(task_name)
            if registered is None:
                raise ValueError("Windows Task Scheduler did not retain the registered UI task")
            _validate_registered(registered, paths)
        _schtasks("/run", "/tn", task_name, "/hresult")
        deadline = time.monotonic() + 5
        while True:
            status = service_status(home=home)
            if status["healthy"]:
                return status
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        raise ValueError(
            f"UI service is installed but not healthy; inspect {paths.log} and ui-autostart status"
        )


def uninstall_service(*, home: Path | None = None):
    if sys.platform != "win32":
        raise ValueError("UI autostart service requires Windows")
    paths = _paths(home)
    with _lock(paths.directory):
        owner = _owned(paths)
        current_sid = _current_user_sid()
        if owner is not None and owner["sid"] != current_sid:
            raise ValueError("UI service ownership belongs to a different Windows user")
        task_name = str(owner["task_name"]) if owner is not None else _task_name(current_sid)
        registered = _query_task(task_name)
        if owner is None:
            if registered is not None:
                raise ValueError("UI Task Scheduler name already exists without Orchestra ownership")
        else:
            if registered is not None:
                registered_signature = _validate_registered(
                    registered, paths, allow_disabled=True,
                )
                if registered_signature["enabled"].lower() == "true":
                    _schtasks("/change", "/tn", task_name, "/disable", "/hresult")
                    registered = _query_task(task_name)
                    if registered is None:
                        raise ValueError(
                            "Windows Task Scheduler lost the UI task while disabling it"
                        )
                    registered_signature = _validate_registered(
                        registered, paths, allow_disabled=True,
                    )
                    if registered_signature["enabled"].lower() != "false":
                        raise ValueError(
                            "Windows Task Scheduler did not disable the UI task; preserving it"
                        )
                pid = _read_pid(paths.pid)
                end = _schtasks("/end", "/tn", task_name, "/hresult", required=False)
                end_code = end.returncode & 0xFFFFFFFF
                if end_code not in (0, _SCHED_E_TASK_NOT_RUNNING):
                    detail = (_decode_output(end.stderr).strip()
                              or _decode_output(end.stdout).strip())
                    suffix = f": {detail[:500]}" if detail else ""
                    raise ValueError(f"Windows Task Scheduler end failed{suffix}")
                if pid is not None and _pid_running(pid):
                    if end_code == _SCHED_E_TASK_NOT_RUNNING:
                        raise ValueError(
                            "Task Scheduler reports no running task but its owned UI process is still active; "
                            "preserving the task and files"
                        )
                    deadline = time.monotonic() + 5
                    while _pid_running(pid) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if _pid_running(pid):
                        raise ValueError(
                            "owned UI process did not stop after Task Scheduler end; preserving the task and files"
                        )
                _schtasks("/delete", "/tn", task_name, "/f", "/hresult")
            else:
                _refuse_orphaned_process(paths, int(owner["port"]))
            for path in (paths.task_xml, paths.runner, paths.manifest, paths.pid):
                path.unlink(missing_ok=True)
    return {**_base_status(), "log": str(paths.log)}
