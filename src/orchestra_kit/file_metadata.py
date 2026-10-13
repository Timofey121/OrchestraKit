"""Compare file metadata without mixing Windows timestamp semantics."""
from __future__ import annotations

import os


def _is_windows() -> bool:
    return os.name == 'nt'


def metadata_signature(info) -> tuple[int, int, int, int, int]:
    """A same-API snapshot; ctime still detects changes during a read."""
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def stat_matches_fstat(path_info, descriptor_info) -> bool:
    """Match identity/content metadata across path and descriptor views.

    CPython on Windows can expose creation time for stat().st_ctime and
    change time for fstat().st_ctime (python/cpython#157671). Keep ctime
    comparisons within each view; compare birthtime across views when present.
    """
    if not _is_windows():
        return metadata_signature(path_info) == metadata_signature(descriptor_info)
    if metadata_signature(path_info)[:4] != metadata_signature(descriptor_info)[:4]:
        return False
    path_birth = getattr(path_info, 'st_birthtime_ns', None)
    descriptor_birth = getattr(descriptor_info, 'st_birthtime_ns', None)
    return path_birth == descriptor_birth
