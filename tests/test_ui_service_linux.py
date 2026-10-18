from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra_kit import ui_service as service
from orchestra_kit import ui_service_linux as linux


class LinuxUiServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.skill = self.home / 'skills с пробелами % $/orchestra'
        (self.skill / 'scripts').mkdir(parents=True)
        (self.skill / 'scripts/orchestra.py').write_text('# launcher\n')
        self.codex = self.home / 'codex data'
        self.calls = []
        self.loaded = False
        self.running = False
        self.enabled = False
        self.fragment = str(self.home / '.config/systemd/user' / linux.UNIT)
        self.dropins = ''
        for mock in (patch.dict(os.environ, {'XDG_CONFIG_HOME': '', 'XDG_DATA_HOME': ''}),
                     patch.object(service.sys, 'platform', 'linux'),
                     patch.object(linux, '_systemctl', side_effect=self.systemctl),
                     patch.object(service, '_listening', return_value=False),
                     patch.object(service, '_healthy', return_value=True)):
            mock.start()
            self.addCleanup(mock.stop)

    def systemctl(self, *args):
        self.calls.append(args)
        if args[0] == 'show':
            return '\n'.join([f'LoadState={"loaded" if self.loaded else "not-found"}',
                              f'ActiveState={"active" if self.running else "inactive"}',
                              f'FragmentPath={self.fragment if self.loaded else ""}',
                              f'DropInPaths={self.dropins}',
                              f'UnitFileState={"enabled" if self.enabled else "disabled"}',
                              f'MainPID={123 if self.running else 0}'])
        if args[0] == 'daemon-reload':
            self.loaded = Path(self.fragment).exists()
        elif args[0] == 'enable':
            self.enabled = True
        elif args[0] == 'start':
            self.running = True
        elif args[0] == 'disable':
            self.enabled = self.running = False
        return ''

    def install(self, **kwargs):
        return service.install_service(self.skill, self.codex, home=self.home, **kwargs)

    def test_install_and_idempotence(self):
        first = self.install()
        self.assertTrue(first['healthy'])
        unit = Path(first['unit']).read_text()
        self.assertIn('WantedBy=default.target', unit)
        self.assertIn('Restart=always', unit)
        # WorkingDirectory is a literal path directive, unlike ExecStart argv.
        self.assertIn(f'WorkingDirectory={self.home}\n', unit)
        self.assertIn('--all-projects', unit)
        self.assertIn('--no-browser', unit)
        self.assertIn('%% $$', unit)
        self.assertNotIn('/bin/sh', unit)
        self.calls.clear()
        self.install()
        self.assertTrue(all(call[0] == 'show' for call in self.calls))

    def test_uninstall_preserves_logs_and_is_idempotent(self):
        first = self.install()
        Path(first['log']).write_text('diagnostics')
        result = service.uninstall_service(home=self.home)
        self.assertFalse(result['installed'])
        self.assertFalse(Path(first['unit']).exists())
        self.assertEqual(Path(first['log']).read_text(), 'diagnostics')
        self.assertIn(('disable', '--now', linux.UNIT), self.calls)
        service.uninstall_service(home=self.home)

    def test_changed_unit_or_metadata_refused_before_os_mutations(self):
        first = self.install()
        Path(first['unit']).write_text('foreign')
        self.calls.clear()
        for action in (self.install, lambda: service.uninstall_service(home=self.home)):
            with self.assertRaisesRegex(ValueError, 'ownership|modified'):
                action()
        self.assertEqual(self.calls, [])

    def test_foreign_loaded_unit_and_dropins_are_preserved(self):
        self.loaded = True
        self.fragment = '/usr/lib/systemd/user/foreign.service'
        with self.assertRaisesRegex(ValueError, 'ownership|foreign'):
            self.install()
        self.assertTrue(all(call[0] == 'show' for call in self.calls))
        self.loaded = False
        self.fragment = str(self.home / '.config/systemd/user' / linux.UNIT)
        self.install()
        self.dropins = '/some/override.conf'
        with self.assertRaisesRegex(ValueError, 'modified|override'):
            service.uninstall_service(home=self.home)

    def test_port_collision_and_invalid_inputs(self):
        with patch.object(service, '_listening', return_value=True):
            with self.assertRaisesRegex(ValueError, 'occupied'):
                self.install()
        self.assertFalse(Path(self.fragment).exists())
        for port in (0, 65536, True):
            with self.assertRaises(ValueError):
                self.install(port=port)
        with self.assertRaisesRegex(ValueError, 'setup'):
            service.install_service(self.home / 'missing', self.codex, home=self.home)

    def test_symlink_and_configuration_change_refused(self):
        directory = self.home / '.config/systemd'
        directory.mkdir(parents=True)
        other = self.home / 'other'
        other.mkdir()
        (directory / 'user').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.install()
        self.assertEqual(list(other.iterdir()), [])

    def test_health_uses_manager_pid_and_inactive_is_unhealthy(self):
        self.install()
        with patch.object(service, '_healthy', return_value=False) as health:
            self.assertFalse(service.service_status(home=self.home)['healthy'])
            health.assert_called_once_with(8731, 123)
        self.running = False
        self.assertFalse(service.service_status(home=self.home)['healthy'])

    def test_xdg_paths_and_different_settings(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.home / 'config'),
                                     'XDG_DATA_HOME': str(self.home / 'data')}):
            self.fragment = str(self.home / 'config/systemd/user' / linux.UNIT)
            first = self.install()
            self.assertEqual(first['unit'], self.fragment)
            self.assertIn(str(self.home / 'data'), first['log'])
            with self.assertRaisesRegex(ValueError, 'settings differ'):
                self.install(port=8732)

    def test_control_characters_cannot_inject_unit_directives(self):
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.home / 'data\nExecStart=bad')}):
            with self.assertRaisesRegex(ValueError, 'control characters'):
                self.install()
        self.assertEqual(self.calls, [])

    def test_owned_configuration_after_interrupted_reload_can_be_started(self):
        self.install()
        self.loaded = self.running = False
        self.calls.clear()
        self.install()
        self.assertIn(('daemon-reload',), self.calls)
        self.assertIn(('start', linux.UNIT), self.calls)

    def test_loaded_unit_without_configuration_is_not_adopted(self):
        self.loaded = True
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.install()

    def test_manager_unavailable_is_clear_and_no_install_is_written(self):
        with patch.object(linux, '_systemctl', side_effect=ValueError('systemd user manager unavailable')):
            with self.assertRaisesRegex(ValueError, 'systemd'):
                self.install()
        self.assertFalse(Path(self.fragment).exists())

    def test_systemctl_command_has_timeout_no_shell_and_reports_transport_errors(self):
        # Exercise the real command wrapper separately from the fake manager.
        for output in (subprocess.CompletedProcess([], 1, '', 'Failed to connect to bus'),
                       subprocess.TimeoutExpired('systemctl', 10), FileNotFoundError('systemctl')):
            with patch.object(subprocess, 'run', **({'side_effect': output} if isinstance(output, Exception)
                                                   else {'return_value': output})):
                with self.assertRaisesRegex(ValueError, 'systemd|systemctl'):
                    linux.run_systemctl('show', linux.UNIT)


if __name__ == '__main__':
    unittest.main()
