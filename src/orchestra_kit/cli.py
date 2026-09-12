from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError
from .project import ProjectError, doctor_project, init_project, sync_project


def _kit_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestra",
        description="Compile project model profiles into Codex orchestration files.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Initialize and synchronize a project")
    init.add_argument("project", nargs="?", default=".", help="Project directory (default: current directory)")
    init.add_argument("--name", help="Human-readable project name (default: directory name)")

    sync = commands.add_parser("sync", help="Regenerate managed project files")
    sync.add_argument("project", nargs="?", default=".", help="Project directory (default: current directory)")

    doctor = commands.add_parser("doctor", help="Validate configuration, credentials, and generated files")
    doctor.add_argument("project", nargs="?", default=".", help="Project directory (default: current directory)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project = Path(args.project).expanduser()
    kit = _kit_root()
    try:
        if args.command == "init":
            result = init_project(project, kit, args.name)
            print(f"initialized {project.resolve()}")
            print(f"managed files written: {len(result.written)}")
            return 0
        if args.command == "sync":
            result = sync_project(project, kit)
            print(f"synchronized {project.resolve()}")
            print(f"written: {len(result.written)}; removed: {len(result.removed)}")
            return 0
        if args.command == "doctor":
            result = doctor_project(project, kit)
            for warning in result.warnings:
                print(f"warning: {warning}", file=sys.stderr)
            if not result.ok:
                for error in result.errors:
                    print(f"error: {error}", file=sys.stderr)
                return 1
            print(f"healthy: {project.resolve()}")
            return 0
    except (ConfigError, ProjectError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {args.command}")

