from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from urllib.request import urlopen

KIT = Path(__file__).resolve().parents[1]


class DashboardCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        (self.project / '.orchestra').mkdir(parents=True)
        (self.project / '.orchestra/project.toml').write_text((KIT / 'templates/project.toml').read_text())
        self.home = Path(self.temp.name) / 'codex'
        self.home.mkdir()
        self.env = dict(os.environ, PYTHONPATH=str(KIT / 'src'))

    def test_cli_binds_task_to_actual_chat_and_explicit_override(self):
        chat = '01a10742-1409-7882-a0f7-ca5ec3c8325f'
        other = '01a10742-1409-7882-a0f7-ca5ec3c8325e'
        self.env['CODEX_THREAD_ID'] = chat
        for arguments, expected in [([], chat), (['--chat-id', other], other)]:
            result = subprocess.run([sys.executable, '-m', 'orchestra_kit', 'task', 'start', str(self.project), '--goal', 'Dashboard', *arguments], env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['chat_id'], expected)

    def test_ui_command_serves_real_data_and_installed_assets(self):
        process = subprocess.Popen([sys.executable, '-m', 'orchestra_kit', 'ui', str(self.project), '--port', '0', '--codex-home', str(self.home), '--no-browser'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = process.stdout.readline()
            self.assertIn('http://127.0.0.1:', line)
            url = line.strip().split()[-1]
            with urlopen(url + '/api/overview', timeout=3) as response:
                payload = json.load(response)
            self.assertIn(str(self.project), [p['path'] for p in payload['projects']])
            for asset, expected in [('/', 'Orchestra'), ('/app.js', 'fetch'), ('/style.css', '--')]:
                with urlopen(url + asset, timeout=3) as response:
                    self.assertIn(expected, response.read().decode())
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_portable_bundle_contains_dashboard_assets(self):
        from orchestra_kit.install import install_skill, check_skill
        skills = Path(self.temp.name) / 'skills'
        skill = install_skill(KIT, skills)
        for name in ('index.html', 'app.js', 'style.css'):
            self.assertTrue((skill / 'scripts/lib/orchestra_kit/web' / name).is_file(), name)
        self.assertEqual(check_skill(KIT, skills), ())

    def test_portable_bundle_preserves_binary_room_asset_and_detects_drift(self):
        from orchestra_kit.install import install_skill, check_skill
        kit = Path(self.temp.name) / 'kit'
        for source in ('skills/orchestra', 'src/orchestra_kit', 'templates'):
            shutil.copytree(KIT / source, kit / source, ignore=shutil.ignore_patterns('__pycache__'))
        asset = kit / 'src/orchestra_kit/web/room.webp'
        image = b'RIFF\x00\xff\x80WEBP\x00'
        asset.write_bytes(image)
        skills = Path(self.temp.name) / 'binary-skills'
        skill = install_skill(kit, skills)
        target = skill / 'scripts/lib/orchestra_kit/web/room.webp'
        self.assertEqual(target.read_bytes(), image)
        self.assertEqual(check_skill(kit, skills), ())
        target.write_bytes(b'changed')
        self.assertTrue(check_skill(kit, skills))
        install_skill(kit, skills)
        self.assertEqual(target.read_bytes(), image)
