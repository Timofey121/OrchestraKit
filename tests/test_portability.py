"""Recipient installation smoke tests, run on every supported desktop OS."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen


KIT = Path(__file__).resolve().parents[1]


class PortabilityTests(unittest.TestCase):
    def test_managed_text_keeps_hashes_when_host_defaults_to_crlf(self):
        from orchestra_kit.project import _atomic_write, _digest
        original = tempfile.NamedTemporaryFile

        def windows_default(*args, **kwargs):
            kwargs.setdefault('newline', '\r\n')
            return original(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'managed.md'
            text = 'Первая строка\nSecond line\n'
            with patch('orchestra_kit.project.tempfile.NamedTemporaryFile', windows_default):
                _atomic_write(path, text)
            self.assertEqual(path.read_bytes(), text.encode('utf-8'))
            import hashlib
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), _digest(text))

    def test_installed_bundle_with_spaces_and_unicode_without_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / 'Проверка установки'
            root.mkdir()
            home = root / 'codex home'
            skills = root / 'user skills'
            project = root / 'Проект с пробелами'
            project.mkdir()
            environment = dict(os.environ, PYTHONUTF8='1')
            environment['PYTHONPATH'] = str(KIT / 'src')
            source = [sys.executable, '-m', 'orchestra_kit']

            def run(command, *arguments, **kwargs):
                result = subprocess.run(command + list(arguments), cwd=root,
                                        env=environment, text=True, encoding='utf-8',
                                        capture_output=True, timeout=30, **kwargs)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            run(source, 'setup', '--skills-dir', str(skills), '--codex-home', str(home))
            run(source, 'setup', '--skills-dir', str(skills), '--codex-home', str(home), '--check')
            environment.pop('PYTHONPATH', None)
            skill = skills / 'orchestra'
            installed = [sys.executable, str(skill / 'scripts/orchestra.py')]
            (project / 'AGENTS.md').write_text('Мои инструкции\n', encoding='utf-8')
            run(installed, 'init', str(project), '--name', 'Тестовый проект')
            run(installed, 'sync', str(project))
            run(installed, 'doctor', str(project))
            self.assertIn('Мои инструкции', (project / 'AGENTS.md').read_text(encoding='utf-8'))
            import hashlib
            digest = hashlib.sha256((skill / 'SKILL.md').read_bytes()).hexdigest()
            payload = json.loads(run(installed, 'hook', '--skill-path', str(skill),
                                     '--expected-sha256', digest,
                                     input=json.dumps({'hook_event_name': 'UserPromptSubmit'})))
            self.assertEqual(payload['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')

            process = subprocess.Popen(installed + ['ui', str(project), '--port', '0',
                                       '--no-codex', '--no-browser'], cwd=root,
                                       env=environment, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding='utf-8')
            try:
                line = process.stdout.readline()
                self.assertIn('http://127.0.0.1:', line)
                address = line.strip().split()[-1]
                with urlopen(address + '/api/overview', timeout=5) as response:
                    overview = json.load(response)
                self.assertIn(str(project.resolve()), [p['path'] for p in overview['projects']])
                for asset in ('/', '/app.js', '/style.css', '/room.png'):
                    with urlopen(address + asset, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertTrue(response.read())
            finally:
                process.terminate()
                process.communicate(timeout=10)
