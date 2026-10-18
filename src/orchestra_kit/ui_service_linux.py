"""Per-user systemd dashboard service; no root or lingering configuration."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .project import _atomic_write

UNIT = 'io.orchestrakit.ui.service'


def _paths(home):
    def xdg(name, default):
        value = Path(os.environ.get(name) or default).expanduser()
        return value if value.is_absolute() else Path(default)
    directory = xdg('XDG_DATA_HOME', home / '.local/share') / 'OrchestraKit'
    paths = (xdg('XDG_CONFIG_HOME', home / '.config') / 'systemd/user' / UNIT,
             directory / 'ui-service.json', directory / 'ui.log')
    for path in (*paths, directory / 'install.lock'):
        for component in (path, *path.parents):
            if component.is_symlink():
                raise ValueError(f'refusing symlink in UI service path: {component}')
        if path.exists() and not path.is_file():
            raise ValueError(f'UI service path is not a regular file: {path}')
    return paths


def _owned(unit, manifest):
    if not unit.exists() and not manifest.exists():
        return None
    try:
        if unit.stat().st_size > 32768 or manifest.stat().st_size > 32768:
            raise ValueError('oversized configuration')
        owner = json.loads(manifest.read_text(encoding='utf-8'))
        if (set(owner) != {'generated_by', 'format_version', 'unit_sha256', 'argv', 'home'}
                or owner['generated_by'] != 'OrchestraKit' or owner['format_version'] != 1
                or owner['unit_sha256'] != hashlib.sha256(unit.read_bytes()).hexdigest()):
            raise ValueError('configuration modified')
        argv = owner['argv']
        if (not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv)
                or argv.count('--port') != 1 or not 1 <= int(argv[argv.index('--port') + 1]) <= 65535):
            raise ValueError('invalid port')
        # Bind metadata to the actual generated unit, not just its digest.
        if unit.read_text(encoding='utf-8') != _unit(argv, owner['home'], manifest.parent / 'ui.log'):
            raise ValueError('metadata modified')
        return owner
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise ValueError('UI service ownership missing or configuration modified; preserve and inspect the files') from exc


def run_systemctl(*args):
    try:
        result = subprocess.run(['systemctl', '--user', *args], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('systemd user manager unavailable or systemctl timed out; use orchestra ui') from exc
    # A missing unit has structured LoadState=not-found, unlike an unavailable bus.
    if result.returncode and not (args[0] == 'show' and 'LoadState=not-found' in result.stdout.splitlines()):
        raise ValueError(f'systemctl {args[0]} failed; systemd user manager required: {result.stderr.strip()[:500]}')
    return result.stdout


_systemctl = run_systemctl


def _query(unit, owned):
    text = _systemctl('show', UNIT, '--property=LoadState,ActiveState,MainPID,FragmentPath,DropInPaths,UnitFileState')
    state = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    if state.get('LoadState') not in ('loaded', 'not-found'):
        raise ValueError('systemd returned an invalid or masked UI service; inspect systemctl --user status ' + UNIT)
    if state['LoadState'] != 'not-found':
        if not owned:
            raise ValueError('UI systemd unit already exists without Orchestra ownership; preserve foreign service')
        if state.get('FragmentPath') != str(unit) or state.get('DropInPaths'):
            raise ValueError('UI systemd service modified or has an override; preserve and inspect it')
    return state


def _quote(value, *, exec_arg=False):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('control characters are not supported in UI service paths')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if exec_arg:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def _unit(argv, home, log):
    _quote(str(home))
    _quote(str(log))  # Reject control characters before emitting an unquoted path directive.
    if str(home).endswith((' ', '\\')):
        raise ValueError('UI service home cannot end with a space or backslash')
    return ('[Unit]\nDescription=OrchestraKit local dashboard\nStartLimitIntervalSec=0\n\n'
            '[Service]\nType=exec\nExecStart=' + ' '.join(_quote(arg, exec_arg=True) for arg in argv)
            + '\nWorkingDirectory=' + str(home).replace('%', '%%')
            + '\nRestart=always\nRestartSec=10\nUMask=0077\nStandardOutput=append:'
            + str(log).replace('%', '%%')
            + '\nStandardError=inherit\n\n[Install]\nWantedBy=default.target\n')


def service_status(*, home=None):
    from .ui_service import _healthy
    home = (home or Path.home()).expanduser().resolve()
    unit, manifest, log = _paths(home)
    owner = _owned(unit, manifest)
    if owner is None:
        return {'supported': True, 'installed': False, 'running': False, 'healthy': False}
    state = _query(unit, True)
    pid = int(state.get('MainPID', '0'))
    running = state.get('ActiveState') == 'active' and pid > 0
    port = int(owner['argv'][owner['argv'].index('--port') + 1])
    return {'supported': True, 'installed': True, 'running': running,
            'enabled': state.get('UnitFileState') == 'enabled',
            'healthy': running and _healthy(port, pid), 'pid': pid or None,
            'url': f'http://127.0.0.1:{port}', 'unit': str(unit), 'log': str(log)}


def install_service(skill, codex_home, *, home=None, port=8731):
    from .ui_service import _lock, _listening
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('UI service port must be an integer from 1 to 65535')
    home = (home or Path.home()).expanduser().resolve()
    if skill.expanduser().is_symlink():
        raise ValueError('refusing symlinked Orchestra skill')
    launcher = skill.expanduser().resolve() / 'scripts/orchestra.py'
    if launcher.is_symlink() or not launcher.is_file():
        raise ValueError('install the Orchestra skill with setup before enabling UI autostart')
    unit, manifest, log = _paths(home)
    argv = [sys.executable, str(launcher), 'ui', '--all-projects', '--no-browser',
            '--host', '127.0.0.1', '--port', str(port), '--codex-home', str(codex_home.expanduser().resolve())]
    data = _unit(argv, str(home), log)
    owner = {'generated_by': 'OrchestraKit', 'format_version': 1,
             'unit_sha256': hashlib.sha256(data.encode()).hexdigest(), 'argv': argv, 'home': str(home)}
    with _lock(manifest.parent):
        existing = _owned(unit, manifest)
        if existing is not None and existing != owner:
            raise ValueError('UI autostart settings differ; uninstall the owned service before reinstalling')
        state = _query(unit, existing is not None)
        running = state.get('ActiveState') in ('active', 'activating', 'reloading')
        if not running and _listening(port):
            raise ValueError(f'UI port {port} is occupied; stop the manual server or choose another --port')
        if existing is None:
            fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            os.close(fd)
            _atomic_write(unit, data)
            _atomic_write(manifest, json.dumps(owner, ensure_ascii=True) + '\n')
        if existing is None or state.get('LoadState') == 'not-found':
            _systemctl('daemon-reload')
        if state.get('UnitFileState') != 'enabled':
            _systemctl('enable', UNIT)
        if not running:
            _systemctl('start', UNIT)
        deadline = time.monotonic() + 5
        while True:
            status = service_status(home=home)
            if status['healthy']:
                return status
            if time.monotonic() >= deadline:
                raise ValueError(f'UI service is installed but not healthy; inspect {log} and ui-autostart status')
            time.sleep(0.1)


def uninstall_service(*, home=None):
    from .ui_service import _lock
    home = (home or Path.home()).expanduser().resolve()
    unit, manifest, log = _paths(home)
    with _lock(manifest.parent):
        if _owned(unit, manifest) is not None:
            _query(unit, True)
            _systemctl('disable', '--now', UNIT)
            unit.unlink()
            manifest.unlink()
            _systemctl('daemon-reload')
    return {'supported': True, 'installed': False, 'running': False, 'healthy': False, 'log': str(log)}
