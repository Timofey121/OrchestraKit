"""Launch the bundled kit without depending on its source checkout or PYTHONPATH."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from orchestra_kit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
