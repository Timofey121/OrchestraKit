from __future__ import annotations

import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra_kit import ui_service as service


class UiServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.skill = self.home / 'skills with spaces/orchestra'
        (self.skill / 'scripts').mkdir(parents=True)
        (self.skill / 'scripts/orchestra.py').write_text('# launcher\n')
        self.codex = self.home / 'custom codex'
        self.calls = []
        self.loaded = False
        self.platform = patch.object(service.sys, 'platform', 'darwin')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.real_healthy = service._healthy
        mock = patch.object(service.subprocess, 'run', side_effect=self.launchctl)
        mock.start()
        self.addCleanup(mock.stop)
        for name, value in [('_listening', False), ('_healthy', True)]:
            mock = patch.object(service, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def launchctl(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == 'print':
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 113,
                                               'state = running\n pid = 123\n' if self.loaded else '', '')
        if argv[1] == 'bootstrap':
            self.loaded = True
        elif argv[1] == 'bootout':
            self.loaded = False
        return subprocess.CompletedProcess(argv, 0, '', '')

    def install(self, **kwargs):
        return service.install_service(self.skill, self.codex, home=self.home, **kwargs)

    def test_install_uses_login_service_restart_and_global_projects_without_browser(self):
        result = self.install()
        self.assertTrue(result['running'])
        self.assertTrue(result['healthy'])
        spec = plistlib.loads(Path(result['plist']).read_bytes())
        self.assertTrue(spec['RunAtLoad'])
        self.assertTrue(spec['KeepAlive'])
        argv = spec['ProgramArguments']
        self.assertIn(str(self.skill / 'scripts/orchestra.py'), argv)
        self.assertIn('--all-projects', argv)
        self.assertIn('--no-browser', argv)
        self.assertEqual(argv[argv.index('--codex-home') + 1], str(self.codex))
        self.assertEqual(spec['WorkingDirectory'], str(self.home))
        self.assertEqual(Path(result['plist']).stat().st_mode & 0o777, 0o600)
        self.assertEqual([x[1] for x in self.calls if x[1] != 'print'], ['enable', 'bootstrap'])

    def test_repeated_install_does_not_restart_or_duplicate_service(self):
        first = self.install()
        original = Path(first['plist']).read_bytes()
        self.calls.clear()
        self.install()
        self.assertEqual(Path(first['plist']).read_bytes(), original)
        self.assertTrue(all(x[1] == 'print' for x in self.calls))

    def test_uninstall_only_removes_owned_service_and_preserves_logs(self):
        first = self.install()
        log = Path(first['log'])
        log.write_text('diagnostics')
        result = service.uninstall_service(home=self.home)
        self.assertFalse(result['installed'])
        self.assertFalse(Path(first['plist']).exists())
        self.assertEqual(log.read_text(), 'diagnostics')
        self.assertIn('bootout', [x[1] for x in self.calls])

    def test_modified_service_is_not_overwritten_or_unloaded(self):
        first = self.install()
        path = Path(first['plist'])
        path.write_bytes(path.read_bytes() + b'\n<!-- personal edit -->')
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'modified|ownership'):
            self.install()
        with self.assertRaisesRegex(ValueError, 'modified|ownership'):
            service.uninstall_service(home=self.home)
        self.assertTrue(path.read_bytes().endswith(b'<!-- personal edit -->'))
        self.assertEqual(self.calls, [])

    def test_existing_foreign_port_does_not_install_or_kill_any_process(self):
        with patch.object(service, '_listening', return_value=True):
            with self.assertRaisesRegex(ValueError, 'occupied'):
                self.install()
        self.assertFalse((self.home / 'Library/LaunchAgents' / (service.LABEL + '.plist')).exists())
        self.assertFalse(any(x[1] in ('bootstrap', 'bootout', 'enable') for x in self.calls))

    def test_symlinked_launchagents_or_log_is_refused(self):
        (self.home / 'Library').mkdir()
        other = self.home / 'other'
        other.mkdir()
        (self.home / 'Library/LaunchAgents').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.install()
        self.assertEqual(list(other.iterdir()), [])

    def test_failed_bootstrap_is_reported_and_can_be_uninstalled(self):
        original = self.launchctl
        def fail(argv, **kwargs):
            if argv[1] == 'bootstrap':
                return subprocess.CompletedProcess(argv, 5, '', 'bootstrap rejected')
            return original(argv, **kwargs)
        with patch.object(service.subprocess, 'run', side_effect=fail):
            with self.assertRaisesRegex(ValueError, 'bootstrap rejected'):
                self.install()
        service.uninstall_service(home=self.home)

    def test_unsupported_platform_is_explicit_without_writes(self):
        with patch.object(service.sys, 'platform', 'linux'):
            self.assertFalse(service.service_status(home=self.home)['supported'])
            with self.assertRaisesRegex(ValueError, 'macOS'):
                self.install()
        self.assertFalse((self.home / 'Library').exists())

    def test_invalid_port_and_concurrent_installer_are_refused(self):
        for port in (0, 65536, True):
            with self.assertRaises(ValueError):
                self.install(port=port)
        first = self.install()
        (Path(first['log']).parent / 'install.lock').touch()
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'locked'):
            self.install()
        self.assertEqual(self.calls, [])

    def test_unowned_matching_label_is_not_taken_over(self):
        path = self.home / 'Library/LaunchAgents' / (service.LABEL + '.plist')
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({'Label': service.LABEL}))
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.install()
        self.assertEqual(plistlib.loads(path.read_bytes()), {'Label': service.LABEL})

    def test_launchctl_timeout_is_a_clear_operational_error(self):
        with patch.object(service.subprocess, 'run', side_effect=subprocess.TimeoutExpired('launchctl', 10)):
            with self.assertRaisesRegex(ValueError, 'launchctl.*timed out'):
                self.install()

    def test_health_requires_this_job_pid_not_another_dashboard(self):
        response = unittest.mock.Mock(status=200)
        response.read.return_value = json.dumps({'service': 'orchestra-ui', 'pid': 456}).encode()
        connection = unittest.mock.Mock()
        connection.getresponse.return_value = response
        with patch.object(service.http.client, 'HTTPConnection', return_value=connection):
            self.assertFalse(self.real_healthy(8731, 123))
            self.assertTrue(self.real_healthy(8731, 456))
        self.assertEqual(connection.request.call_args.args, ('GET', '/api/health'))
