"""Include the authored skill and templates in built Python distributions."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithKitData(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parent
        destination = Path(self.build_lib) / "orchestra_kit" / "_kit_data"
        for relative in ("templates", "skills/orchestra"):
            for path in (source / relative).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    target = destination / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)


setup(cmdclass={"build_py": BuildWithKitData})
