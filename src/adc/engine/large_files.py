r"""Large Files Hunter (v2.5.0 / SPEC 6.2 extension).

Scans directories or whole volumes for files exceeding a size threshold (e.g. >100 MB,
>500 MB, >1 GB), categorized into media, archives, installers, and documents.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .fsutil import allocated_size, is_reparse
from .jobs import CancelToken

MEDIA_EXTS: Final[frozenset[str]] = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma",
})

ARCHIVE_EXTS: Final[frozenset[str]] = frozenset({
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz",
    ".iso", ".img", ".vhd", ".vhdx", ".vmdk", ".qcow2",
})

INSTALLER_EXTS: Final[frozenset[str]] = frozenset({
    ".exe", ".msi", ".pkg", ".deb", ".rpm", ".dmg", ".appx", ".msix",
})

DOCUMENT_EXTS: Final[frozenset[str]] = frozenset({
    ".pdf", ".docx", ".xlsx", ".pptx", ".psd", ".ai", ".sketch", ".fig",
})


def categorize_file(name: str) -> str:
    """Classify a file name into a human-readable storage category."""
    ext = Path(name).suffix.lower()
    if ext in MEDIA_EXTS:
        return "media"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in INSTALLER_EXTS:
        return "installer"
    if ext in DOCUMENT_EXTS:
        return "document"
    return "other"


def _node_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class LargeFile:
    """One file identified by the Large Files Hunter."""

    name: str
    path: str
    size: int
    category: str
    ext: str
    node_id: str = field(default_factory=_node_id)
    allocated_size: int | None = None
    mtime: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "allocated_size": self.allocated_size,
            "category": self.category,
            "ext": self.ext,
            "mtime": self.mtime,
        }


@dataclass
class LargeFilesResult:
    """Result of a Large Files scan."""

    root: str
    files: list[LargeFile] = field(default_factory=list)
    scanned_files: int = 0
    scanned_dirs: int = 0
    total_size: int = 0
    truncated: bool = False
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files": [f.as_dict() for f in self.files],
            "scanned_files": self.scanned_files,
            "scanned_dirs": self.scanned_dirs,
            "total_size": self.total_size,
            "truncated": self.truncated,
            "elapsed_s": self.elapsed_s,
        }


def find_large_files(
    root: str,
    *,
    min_size_bytes: int = 100 * 1024 * 1024,
    limit: int = 100,
    token: CancelToken | None = None,
    budget_s: float | None = None,
    measure_on_disk: bool = True,
    progress_fn: Callable[[int, int, str], None] | None = None,
) -> LargeFilesResult:
    """Walk root iteratively and find the top largest files exceeding min_size_bytes."""
    t0 = time.monotonic()
    abs_root = os.path.abspath(root)
    result = LargeFilesResult(root=abs_root)

    found_files: list[LargeFile] = []
    stack: list[str] = [abs_root]

    while stack:
        if token is not None and token.cancelled:
            result.truncated = True
            break
        if budget_s is not None and (time.monotonic() - t0) >= budget_s:
            result.truncated = True
            break

        current_dir = stack.pop()
        result.scanned_dirs += 1

        if progress_fn is not None and (result.scanned_dirs % 64 == 0):
            progress_fn(result.scanned_dirs, result.scanned_files, current_dir)

        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if token is not None and token.cancelled:
                        result.truncated = True
                        break

                    try:
                        st = entry.stat(follow_symlinks=False)
                        attrs = getattr(st, "st_file_attributes", 0)
                        # Skip reparse points (junctions, symlinks - BUG-01)
                        if is_reparse(entry, attrs):
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            result.scanned_files += 1
                            size = st.st_size

                            if size >= min_size_bytes:
                                alloc = (
                                    allocated_size(entry.path, attrs, size)
                                    if measure_on_disk
                                    else size
                                )
                                cat = categorize_file(entry.name)
                                lf = LargeFile(
                                    name=entry.name,
                                    path=entry.path,
                                    size=size,
                                    allocated_size=alloc,
                                    category=cat,
                                    ext=Path(entry.name).suffix.lower(),
                                    mtime=st.st_mtime,
                                )
                                found_files.append(lf)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue

    # Sort descending by size, then alphabetically by name
    found_files.sort(key=lambda f: (-f.size, f.name.lower()))
    result.files = found_files[:limit]
    result.total_size = sum(f.size for f in result.files)
    result.elapsed_s = time.monotonic() - t0
    return result
