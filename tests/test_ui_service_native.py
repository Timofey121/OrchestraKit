"""Opt-in real user-service lifecycle; run only on disposable CI hosts."""
from __future__ import annotations

import os
import signal
import socket
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from orchestra_kit.install import install_user_setup
from orchestra_kit import ui_service


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
                    print(ui_service._launchctl('print', ui_service._target(), required=False).stdout, flush=True)
                elif sys.platform == 'linux':
                    from orchestra_kit.ui_service_linux import _paths
                    log = _paths(Path.home())[2]
                else:
                    from orchestra_kit.ui_service_windows import _paths
                    log = _paths().log
                if log.exists():
                    print(log.read_text(encoding='utf-8', errors='replace')[-8000:], flush=True)
                raise
            finally:
                ui_service.uninstall_service()
            self.assertFalse(ui_service.service_status()['installed'])


if __name__ == '__main__':
    unittest.main()
