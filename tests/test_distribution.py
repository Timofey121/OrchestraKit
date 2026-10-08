from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]

class DistributionTests(unittest.TestCase):
    def test_installed_package_and_reexported_skill_work_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / 'site-packages'
            package = site / 'orchestra_kit'
            shutil.copytree(KIT / 'src/orchestra_kit', package, ignore=shutil.ignore_patterns('__pycache__'))
            assets = package / '_kit_data'
            shutil.copytree(KIT / 'templates', assets / 'templates')
            shutil.copytree(KIT / 'skills/orchestra', assets / 'skills/orchestra')
            project = root / 'project'; project.mkdir()
            environment = dict(os.environ); environment.pop('PYTHONPATH', None)
            code = 'import sys; sys.path.insert(0, sys.argv.pop(1)); from orchestra_kit.cli import main; raise SystemExit(main())'
            command = [sys.executable, '-I', '-c', code, str(site)]
            for args in [('init', str(project)), ('doctor', str(project)),
                         ('setup', '--skills-dir', str(root / 'skills'),
                          '--codex-home', str(root / 'codex'))]:
                result = subprocess.run(command + list(args), cwd=root, env=environment, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            launcher = root / 'skills/orchestra/scripts/orchestra.py'
            self.assertTrue((launcher.parent / 'lib/orchestra_kit/web/app.js').is_file())
            self.assertTrue((launcher.parent / 'lib/orchestra_kit/web/THIRD_PARTY_NOTICES.txt').is_file())
            self.assertFalse((launcher.parent / 'lib/orchestra_kit/_kit_data').exists())
            result = subprocess.run([sys.executable, str(launcher), 'doctor', str(project)], cwd=root, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_archive_contains_newcomer_docs_and_working_launcher(self):
        import importlib.util
        import tarfile
        if importlib.util.find_spec('setuptools') is None:
            self.skipTest('setuptools is a build-time dependency')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir()
            for name in ['src','templates','skills','bin','frontend','tests','.github']:
                shutil.copytree(KIT/name,source/name,ignore=shutil.ignore_patterns('__pycache__','*.egg-info','node_modules'))
            for name in ['README.md','CONTRIBUTING.md','LICENSE','DESIGN.md','pyproject.toml','setup.py','MANIFEST.in','docs/getting-started.md','docs/local-dashboard.md','docs/project-config.md','docs/hooks-and-orchestration.md','docs/architecture.md','docs/features.md','docs/providers/deepseek.md']:
                target=source/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(KIT/name,target)
            result=subprocess.run([sys.executable,'setup.py','sdist','--dist-dir',str(root/'dist')],cwd=source,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            unpacked=root/'unpacked';unpacked.mkdir()
            with tarfile.open(next((root/'dist').glob('*.tar.gz'))) as archive:
                for member in archive.getmembers():
                    self.assertFalse(Path(member.name).is_absolute());self.assertNotIn('..',Path(member.name).parts)
                    path=unpacked/member.name
                    if member.isdir():path.mkdir(parents=True,exist_ok=True)
                    elif member.isfile():
                        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive.extractfile(member).read());path.chmod(member.mode)
                    else:self.fail('unexpected archive link or special file')
            received=next(unpacked.iterdir())
            for relative in ['docs/getting-started.md', 'docs/architecture.md', 'docs/features.md', 'LICENSE', 'CONTRIBUTING.md', 'tests/test_execution.py', '.github/pull_request_template.md']:
                self.assertTrue((received/relative).is_file(), relative)
            self.assertFalse((received/'.orchestra/archive').exists())
            self.assertFalse((received/'.idea').exists())
            self.assertTrue((received/'frontend/build.mjs').is_file())
            self.assertTrue((received/'frontend/package.json').is_file())
            project=root/'recipient project';project.mkdir()
            env=dict(os.environ);env.pop('PYTHONPATH',None)
            for args in [('init',str(project)),('doctor',str(project))]:
                result=subprocess.run([str(received/'bin/orchestra'),*args],cwd=root,env=env,text=True,capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)
