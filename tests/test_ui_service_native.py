"""Opt-in real user-service lifecycle; run only on disposable CI hosts."""
from __future__ import annotations

import os
import signal
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from orchestra_kit.install import install_user_setup
from orchestra_kit import ui_service


@unittest.skipUnless(os.environ.get('ORCHESTRA_NATIVE_UI_TEST') == '1',
                     'requires explicit opt-in on a disposable OS account')
class NativeUiServiceTests(unittest.TestCase):
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
            finally:
                ui_service.uninstall_service()
            self.assertFalse(ui_service.service_status()['installed'])


if __name__ == '__main__':
    unittest.main()
