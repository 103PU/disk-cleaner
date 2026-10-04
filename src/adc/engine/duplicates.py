r"""Smart Duplicate File Finder (v2.5.0 / SPEC 6.2 extension).

Uses a 3-phase minimal-I/O algorithm:
1. Exact file size matching (discards unique file sizes with zero extra I/O).
2. 4 KB header SHA-256 fingerprinting (discards different headers with 1 block read).
3. Full streaming SHA-256 validation only for hash-colliding candidates.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .fsutil import is_reparse
from .jobs import CancelToken

HEADER_CHUNK_SIZE: Final = 4096
HASH_CHUNK_SIZE: Final = 65536


def _node_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class DuplicateItem:
    """One file in a duplicate cluster."""

    name: str
    path: str
    size: int
    mtime: float
    suggested_keep: bool
    node_id: str = field(default_factory=_node_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "suggested_keep": self.suggested_keep,
        }


@dataclass
class DuplicateGroup:
    """A cluster of identical files sharing the exact same SHA-256 checksum."""

    group_id: str
    size: int
    wasted_size: int
    hash: str
    items: list[DuplicateItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "size": self.size,
            "wasted_size": self.wasted_size,
            "hash": self.hash,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass
class DuplicatesResult:
    """Summary result of a duplicate scan."""

    root: str
    groups: list[DuplicateGroup] = field(default_factory=list)
    scanned_files: int = 0
    scanned_dirs: int = 0
    total_wasted: int = 0
    truncated: bool = False
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "groups": [g.as_dict() for g in self.groups],
            "scanned_files": self.scanned_files,
            "scanned_dirs": self.scanned_dirs,
            "total_wasted": self.total_wasted,
            "truncated": self.truncated,
            "elapsed_s": self.elapsed_s,
        }


def _read_header_hash(path: str) -> bytes:
    with open(path, "rb") as f:
        chunk = f.read(HEADER_CHUNK_SIZE)
        return hashlib.sha256(chunk).digest()


def _compute_full_hash(path: str, token: CancelToken | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK_SIZE):
            if token is not None and token.cancelled:
                return ""
            h.update(chunk)
    return h.hexdigest()


def find_duplicates(
    root: str,
    *,
    min_size_bytes: int = 1024,
    limit_groups: int = 100,
    token: CancelToken | None = None,
    budget_s: float | None = None,
    progress_fn: Callable[[int, int, str], None] | None = None,
) -> DuplicatesResult:
    """Scan root using 3-phase duplicate detection."""
    t0 = time.monotonic()
    abs_root = os.path.abspath(root)
    result = DuplicatesResult(root=abs_root)

    # Phase 1: Size grouping
    # size -> list of (path, name, mtime)
    by_size: dict[int, list[tuple[str, str, float]]] = defaultdict(list)
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
                        if is_reparse(entry, attrs):
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            result.scanned_files += 1
                            size = st.st_size
                            if size >= min_size_bytes and size > 0:
                                by_size[size].append((entry.path, entry.name, st.st_mtime))
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue

    # Discard non-colliding sizes
    candidate_size_groups = [files for size, files in by_size.items() if len(files) >= 2]

    # Phase 2: Header hash
    # (size, header_hash) -> list of (path, name, mtime)
    by_header: dict[tuple[int, bytes], list[tuple[str, str, float]]] = defaultdict(list)
    for file_list in candidate_size_groups:
        if token is not None and token.cancelled:
            result.truncated = True
            break
        if budget_s is not None and (time.monotonic() - t0) >= budget_s:
            result.truncated = True
            break

        for path, name, mtime in file_list:
            try:
                st_size = Path(path).stat().st_size
                h_digest = _read_header_hash(path)
                by_header[(st_size, h_digest)].append((path, name, mtime))
            except (OSError, PermissionError):
                continue

    # Discard non-colliding header groups
    candidate_header_groups = [files for key, files in by_header.items() if len(files) >= 2]

    # Phase 3: Full SHA-256 hash
    # (size, full_hash) -> list of (path, name, mtime)
    by_full_hash: dict[tuple[int, str], list[tuple[str, str, float]]] = defaultdict(list)
    for file_list in candidate_header_groups:
        if token is not None and token.cancelled:
            result.truncated = True
            break
        if budget_s is not None and (time.monotonic() - t0) >= budget_s:
            result.truncated = True
            break

        for path, name, mtime in file_list:
            try:
                full_h = _compute_full_hash(path, token)
                if not full_h:
                    continue
                size = Path(path).stat().st_size
                by_full_hash[(size, full_h)].append((path, name, mtime))
            except (OSError, PermissionError):
                continue

    # Build DuplicateGroups
    groups: list[DuplicateGroup] = []
    for (size, f_hash), files in by_full_hash.items():
        if len(files) < 2:
            continue

        # Sort items: earliest modified time first (older = candidate to keep)
        # If mtimes match, sort by shortest path length
        sorted_files = sorted(files, key=lambda x: (x[2], len(x[0]), x[0]))

        items: list[DuplicateItem] = []
        for i, (path, name, mtime) in enumerate(sorted_files):
            # First item (oldest) is suggested to keep
            suggested_keep = (i == 0)
            items.append(DuplicateItem(
                name=name,
                path=path,
                size=size,
                mtime=mtime,
                suggested_keep=suggested_keep,
            ))

        wasted = size * (len(items) - 1)
        groups.append(DuplicateGroup(
            group_id=_node_id(),
            size=size,
            wasted_size=wasted,
            hash=f_hash,
            items=items,
        ))

    # Sort groups descending by wasted space
    groups.sort(key=lambda g: -g.wasted_size)
    result.groups = groups[:limit_groups]
    result.total_wasted = sum(g.wasted_size for g in result.groups)
    result.elapsed_s = time.monotonic() - t0
    return result
