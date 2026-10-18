"""User-owned macOS launch agent for the shared loopback dashboard."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .project import _atomic_write

LABEL = 'io.orchestrakit.ui'


def _paths(home: Path):
    home = home.expanduser().resolve()
    directory = home / 'Library/Application Support/OrchestraKit'
    paths = (home / 'Library/LaunchAgents' / (LABEL + '.plist'),
             directory / 'ui-service.json', directory / 'ui.log')
    for path in (*paths, directory / 'install.lock'):
        for component in (path, *path.parents):
            if component == home:
                break
            if component.is_symlink():
                raise ValueError(f'refusing symlink in UI service path: {component}')
        if path.exists() and not path.is_file():
            raise ValueError(f'UI service path is not a regular file: {path}')
    return paths


def _owned(plist: Path, manifest: Path):
    if not plist.exists() and not manifest.exists():
        return None
    try:
        if plist.stat().st_size > 32768 or manifest.stat().st_size > 32768:
            raise ValueError('oversized service configuration')
        data = plist.read_bytes()
        owner = json.loads(manifest.read_text())
        spec = plistlib.loads(data)
        if (owner != {'generated_by': 'OrchestraKit', 'format_version': 1,
                      'plist_sha256': hashlib.sha256(data).hexdigest()}
                or spec.get('Label') != LABEL):
            raise ValueError('service configuration modified')
        argv = spec.get('ProgramArguments')
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv) or argv.count('--port') != 1:
            raise ValueError('invalid service arguments')
        port_index = argv.index('--port') + 1
        if port_index >= len(argv) or not 1 <= int(argv[port_index]) <= 65535:
            raise ValueError('invalid service port')
        return spec
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException) as exc:
        raise ValueError('UI service ownership missing or configuration modified; preserve and inspect the files') from exc


def _target():
    return f'gui/{os.getuid()}/{LABEL}'


def _launchctl(*args, required=True):
    try:
        result = subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('launchctl timed out; inspect ui-autostart status') from exc
    if required and result.returncode:
        raise ValueError(f'launchctl {args[0]} failed: {result.stderr.strip()[:500]}')
    return result


def _listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


def _healthy(port, pid):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
    try:
        connection.request('GET', '/api/health')
        response = connection.getresponse()
        data = json.loads(response.read(4096))
        return response.status == 200 and data == {'service': 'orchestra-ui', 'pid': pid} and type(pid) is int
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


@contextmanager
def _lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'install.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError(f'UI service installer is locked; inspect {path}') from exc
    try:
        os.close(fd)
        yield
    finally:
        path.unlink()


def service_status(*, home: Path | None = None):
    if sys.platform != 'darwin':
        return {'supported': False, 'installed': False, 'running': False, 'healthy': False}
    plist, manifest, log = _paths(home or Path.home())
    spec = _owned(plist, manifest)
    if spec is None:
        return {'supported': True, 'installed': False, 'running': False, 'healthy': False}
    result = _launchctl('print', _target(), required=False)
    running = result.returncode == 0 and bool(re.search(r'\bstate = running\b', result.stdout))
    argv = spec['ProgramArguments']
    port = int(argv[argv.index('--port') + 1])
    pid = re.search(r'\bpid = (\d+)', result.stdout) if running else None
    pid = int(pid[1]) if pid else None
    return {'supported': True, 'installed': True, 'running': running,
            'healthy': running and _healthy(port, pid), 'pid': pid,
            'url': f'http://127.0.0.1:{port}', 'plist': str(plist), 'log': str(log)}


def install_service(skill: Path, codex_home: Path, *, home: Path | None = None, port: int = 8731):
    if sys.platform != 'darwin':
        raise ValueError('UI autostart service requires macOS; use orchestra ui on this platform')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('UI service port must be an integer from 1 to 65535')
    home = (home or Path.home()).expanduser().resolve()
    if skill.expanduser().is_symlink():
        raise ValueError('refusing symlinked Orchestra skill')
    launcher = skill.expanduser().resolve() / 'scripts/orchestra.py'
    if launcher.is_symlink() or not launcher.is_file():
        raise ValueError('install the Orchestra skill with setup before enabling UI autostart')
    plist, manifest, log = _paths(home)
    with _lock(manifest.parent):
        existing = _owned(plist, manifest)
        spec = {'Label': LABEL, 'ProgramArguments': [sys.executable, str(launcher), 'ui',
                '--all-projects', '--no-browser', '--host', '127.0.0.1', '--port', str(port),
                '--codex-home', str(codex_home.expanduser().resolve())],
                'WorkingDirectory': str(home), 'RunAtLoad': True, 'KeepAlive': True,
                'ThrottleInterval': 10, 'StandardOutPath': str(log), 'StandardErrorPath': str(log)}
        if existing is not None and existing != spec:
            raise ValueError('UI autostart settings differ; uninstall the owned service before reinstalling')
        current = _launchctl('print', _target(), required=False)
        loaded = current.returncode == 0
        if loaded and existing is None:
            raise ValueError('UI launchd label is already loaded without Orchestra ownership')
        if not loaded and _listening(port):
            raise ValueError(f'UI port {port} is occupied; stop the manual server or choose another --port')
        if not loaded:
            # launchd opens an already private log; no shell, cwd project, or browser.
            fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            os.close(fd)
            data = plistlib.dumps(spec).decode('utf-8')
            _atomic_write(plist, data)
            owner = {'generated_by': 'OrchestraKit', 'format_version': 1,
                     'plist_sha256': hashlib.sha256(data.encode('utf-8')).hexdigest()}
            _atomic_write(manifest, json.dumps(owner) + '\n')
            _launchctl('enable', _target())
            _launchctl('bootstrap', f'gui/{os.getuid()}', str(plist))
        deadline = time.monotonic() + 5
        while True:
            status = service_status(home=home)
            if status['healthy']:
                return status
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        raise ValueError(f'UI service is installed but not healthy; inspect {log} and ui-autostart status')


def uninstall_service(*, home: Path | None = None):
    if sys.platform != 'darwin':
        raise ValueError('UI autostart service requires macOS')
    home = home or Path.home()
    plist, manifest, log = _paths(home)
    with _lock(manifest.parent):
        if _owned(plist, manifest) is not None:
            if _launchctl('print', _target(), required=False).returncode == 0:
                _launchctl('bootout', _target())
            plist.unlink()
            manifest.unlink()
    return {'supported': True, 'installed': False, 'running': False, 'healthy': False,
            'log': str(log)}
