"""Opt-in real user-service lifecycle; run only on disposable CI hosts."""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from orchestra_kit.install import install_user_setup
from orchestra_kit import ui_service


class NativeUiDiagnosticTests(unittest.TestCase):
    def test_sample_output_keeps_call_graph_and_drops_binary_images(self):
        sample = ('Sampling process\nCall graph:\n  blocked frame\n'
                  'Binary Images:\n  thousands of irrelevant images')
        self.assertEqual(
            _bounded_result(sample, before='Binary Images:'),
            'Sampling process\nCall graph:\n  blocked frame',
        )


def _bounded_result(output, *, before=None, limit=12000):
    output = output.strip()
    if before is not None:
        output = output.split(before, 1)[0].rstrip()
        return output[:limit]
    return output[-limit:]


def _print_command_diagnostic(label, argv, *, timeout, before=None):
    """Print bounded process evidence without hiding the original test failure."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        output = _bounded_result(result.stdout + '\n' + result.stderr, before=before)
        print(f'{label}: exit={result.returncode}', flush=True)
        print(output if output else '<no output>', flush=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f'{label}: unavailable: {exc}', flush=True)


def _diagnose_macos_startup(started_at, launcher, codex, failed_port):
    """Distinguish a slow launchd start from a process blocked before the CLI."""
    launchd = ui_service._launchctl('print', ui_service._target(), required=False)
    print('macOS launchd state after installer failure:', flush=True)
    print(launchd.stdout[-12000:] or launchd.stderr[-12000:] or '<no output>', flush=True)
    pid_match = re.search(r'\bpid = (\d+)', launchd.stdout)

    deadline = time.monotonic() + 20
    launchd_healthy = False
    while time.monotonic() < deadline:
        status = ui_service.service_status()
        if status['healthy']:
            launchd_healthy = True
            elapsed = time.monotonic() - started_at
            print(f'macOS launchd service became healthy after {elapsed:.1f}s: {status}', flush=True)
            break
        time.sleep(0.5)
    else:
        elapsed = time.monotonic() - started_at
        print(f'macOS launchd service still unhealthy after {elapsed:.1f}s', flush=True)

    if pid_match:
        pid = pid_match.group(1)
        _print_command_diagnostic(
            'macOS launchd process',
            ['/bin/ps', '-ww', '-o', 'pid,ppid,state,etime,time,wchan,command', '-p', pid],
            timeout=5,
        )
        if launchd_healthy:
            return
        _print_command_diagnostic(
            'macOS launchd process sample', ['/usr/bin/sample', pid, '3', '1'], timeout=8,
            before='Binary Images:',
        )
    else:
        print('macOS launchd process: no PID reported', flush=True)

    dns_started = time.monotonic()
    try:
        fqdn = socket.getfqdn('127.0.0.1')
        dns_result = repr(fqdn)
    except OSError as exc:
        dns_result = f'error={exc}'
    dns_elapsed = time.monotonic() - dns_started
    post_dns_status = ui_service.service_status()
    print(f'macOS getfqdn(127.0.0.1) result={dns_result} elapsed={dns_elapsed:.1f}s; '
          f'launchd healthy={post_dns_status["healthy"]} '
          f'total_elapsed={time.monotonic() - started_at:.1f}s', flush=True)

    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        direct_port = probe.getsockname()[1]
    command = [sys.executable, str(launcher), 'ui', '--all-projects', '--no-browser',
               '--host', '127.0.0.1', '--port', str(direct_port),
               '--codex-home', str(codex)]
    direct_started = time.monotonic()
    direct = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    direct_healthy = False
    try:
        direct_deadline = time.monotonic() + 10
        while direct.poll() is None and time.monotonic() < direct_deadline:
            if ui_service._healthy(direct_port, direct.pid):
                direct_healthy = True
                break
            time.sleep(0.2)
    finally:
        if direct.poll() is None:
            direct.terminate()
        try:
            output = direct.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            direct.kill()
            output = direct.communicate(timeout=5)[0]
    direct_elapsed = time.monotonic() - direct_started
    print(f'macOS direct launcher healthy={direct_healthy} exit={direct.returncode} '
          f'elapsed={direct_elapsed:.1f}s launchd_port={failed_port} direct_port={direct_port}', flush=True)
    print(_bounded_result(output) if output else '<no output>', flush=True)
    comparison_status = ui_service.service_status()
    print(f'macOS launchd after direct comparison: {comparison_status}; '
          f'total_elapsed={time.monotonic() - started_at:.1f}s', flush=True)


@unittest.skipUnless(os.environ.get('ORCHESTRA_NATIVE_UI_TEST') == '1',
                     'requires explicit opt-in on a disposable OS account')
class NativeUiServiceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', 'systemd path directive probe')
    def test_systemd_literal_backslashes_in_directory_and_log(self):
        from orchestra_kit import ui_service_linux as linux
        from orchestra_kit.project import _atomic_write
        unit_directory = linux._paths(Path.home().resolve())[0].parent
        unit = unit_directory / ('io.orchestrakit.path-probe-' + uuid.uuid4().hex + '.service')
        with tempfile.TemporaryDirectory(prefix='Orchestra path probe ') as temporary:
            root = Path(temporary).resolve()
            work = root / 'working\\backslash % $ пробел'
            work.mkdir()
            log = root / 'log\\backslash % $ пробел.txt'
            data = linux._unit([sys.executable, '-c', 'import os; print(os.getcwd(), flush=True)'],
                               str(work), log)
            self.assertFalse(unit.exists())
            try:
                _atomic_write(unit, data)
                linux.run_systemctl('daemon-reload')
                linux.run_systemctl('start', unit.name)
                deadline = time.monotonic() + 5
                while (not log.exists() or not log.read_text(encoding='utf-8').strip()) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(log.read_text(encoding='utf-8').strip(), str(work))
                print('Native systemd WorkingDirectory and log literal backslashes: PASS', flush=True)
            finally:
                linux.run_systemctl('stop', unit.name)
                unit.unlink(missing_ok=True)
                linux.run_systemctl('daemon-reload')

    def test_install_health_recovery_and_uninstall(self):
        kit = Path(__file__).resolve().parents[1]
        self.assertFalse(ui_service.service_status()['installed'], 'refuse to replace an existing service')
        with tempfile.TemporaryDirectory(prefix='Orchestra проверка % $ ') as temporary:
            root = Path(temporary).resolve()
            skill = install_user_setup(kit, root / 'skills with spaces', root / 'codex')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            started_at = time.monotonic()
            try:
                first = ui_service.install_service(skill, root / 'codex', port=port)
                self.assertTrue(first['healthy'], first)
                second = ui_service.install_service(skill, root / 'codex', port=port)
                self.assertEqual(first['pid'], second['pid'], 'idempotent install must not restart')
                os.kill(first['pid'], signal.SIGTERM)
                # Task Scheduler's shortest supported restart interval is one minute.
                deadline = time.monotonic() + 100
                while time.monotonic() < deadline:
                    recovered = ui_service.service_status()
                    if recovered['healthy'] and recovered['pid'] != first['pid']:
                        break
                    time.sleep(0.5)
                else:
                    self.fail(f'service did not recover: {recovered}')
                print(f'Native {sys.platform} UI recovery: {first["pid"]} -> {recovered["pid"]}', flush=True)
            except Exception:
                # These are disposable test services with synthetic data only.
                if sys.platform == 'darwin':
                    log = ui_service._paths(Path.home())[2]
                    try:
                        _diagnose_macos_startup(started_at, skill / 'scripts/orchestra.py',
                                                root / 'codex', port)
                    except Exception as diagnostic_error:
                        print(f'macOS failure diagnostics failed: {diagnostic_error}', flush=True)
                elif sys.platform == 'linux':
                    from orchestra_kit.ui_service_linux import _paths
                    log = _paths(Path.home())[2]
                else:
                    from orchestra_kit import ui_service_windows as windows
                    log = windows._paths().log
                    print(windows._query_task(windows._task_name(windows._current_user_sid())), flush=True)
                if log.exists():
                    print(log.read_text(encoding='utf-8', errors='replace')[-8000:], flush=True)
                raise
            finally:
                ui_service.uninstall_service()
            self.assertFalse(ui_service.service_status()['installed'])


if __name__ == '__main__':
    unittest.main()
