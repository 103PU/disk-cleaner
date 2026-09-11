r"""The walker. Every number the UI shows about "before" comes from here.

Replaces v1's ``get_folder_size()`` (``src/cleaner_backend.py:167``), which had
four defects this module is built around:

* **BUG-01** it followed junctions, so ``%LOCALAPPDATA%\Application Data`` --
  a junction pointing at its own parent -- made the walk recurse forever, and
  any other junction double-counted its target.
* **BUG-12** it reported ``st_size``, which for a sparse or NTFS-compressed file
  is not the space a delete would return. WSL2's ``ext4.vhdx`` is the case that
  matters on this machine.
* **BUG-13** it called ``stat`` twice per entry (once via ``is_file()``, once
  for the size) and could not be cancelled.
* It recursed, so a deep node_modules tree could hit the recursion limit.

The fixes: one ``entry.stat(follow_symlinks=False)`` per entry and nothing else,
the reparse-point *attribute* as the link test, an explicit stack, a cancel and
budget check inside the hot loop, and ``GetCompressedFileSizeW`` for the files
whose attributes say allocation differs from length.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import platform_win as win
from .jobs import CancelToken
from .paths import ensure, scan_db

# Not in the stat module on non-Windows hosts, so they are spelled out.
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_ATTRIBUTE_SPARSE_FILE = 0x0200
FILE_ATTRIBUTE_COMPRESSED = 0x0800
_ALLOCATION_DIFFERS = FILE_ATTRIBUTE_SPARSE_FILE | FILE_ATTRIBUTE_COMPRESSED

# How often the hot loop looks at the wall clock. Cancel is checked on *every*
# entry (a flag read); time.monotonic() is a real call, so it is sampled.
CLOCK_EVERY = 256
PROGRESS_EVERY = 2048

# Above this the two size numbers are reported separately to the user.
DIVERGENCE_THRESHOLD = 0.05

ProgressFn = Callable[[int, int, str], None]


@dataclass
class WalkResult:
    """What one walk found. ``size`` is the number to show; the rest is evidence.

    ``logical`` and ``on_disk`` are kept apart on purpose. They are equal for
    ordinary files, and when they are not the difference is the whole point --
    reporting a 20 GB sparse VHDX that occupies 4 GB as "20 GB reclaimable" is
    exactly the lie BUG-12 told.
    """

    root: str
    logical: int = 0
    on_disk: int = 0
    files: int = 0
    dirs: int = 0
    truncated: bool = False
    measured_on_disk: bool = False
    cached: bool = False
    elapsed_s: float = 0.0
    denied: list[str] = field(default_factory=list)
    skipped_links: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        """The reclaimable estimate: allocated size when we measured it."""
        return self.on_disk if self.measured_on_disk else self.logical

    @property
    def divergent(self) -> bool:
        """True when the two numbers differ enough that the UI must show both."""
        if not self.measured_on_disk or self.logical == 0:
            return False
        return abs(self.logical - self.on_disk) / self.logical > DIVERGENCE_THRESHOLD

    @property
    def denied_count(self) -> int:
        return len(self.denied)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "size": self.size,
            "logical": self.logical,
            "on_disk": self.on_disk if self.measured_on_disk else None,
            "divergent": self.divergent,
            "files": self.files,
            "dirs": self.dirs,
            "truncated": self.truncated,
            "cached": self.cached,
            "elapsed_s": round(self.elapsed_s, 3),
            "denied_count": len(self.denied),
            # Bounded: a walk over a locked-down tree can deny tens of
            # thousands of paths and the UI only ever shows the first few.
            "denied": self.denied[:64],
            "skipped_links": self.skipped_links[:64],
        }


class ScanCache:
    """``(root, mtime_ns) -> (size, files)`` in ``cache/scan.sqlite``.

    Honest about what this can and cannot do. A directory's ``mtime_ns`` changes
    when its *immediate* children change, not when a grandchild does, so a
    recursive size keyed on the root's mtime can go stale without the key
    moving. That is tolerable here and nowhere else: the cached number is only
    ever the pre-clean *estimate*, while the number reported after a clean is
    two fresh measurements taken around the operation (docs/02-SPEC.md 4.8).

    ``max_age_s`` bounds the staleness anyway, and ``ADC_NO_SCAN_CACHE=1``
    switches the whole thing off.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS scan (
            root       TEXT PRIMARY KEY,
            mtime_ns   INTEGER NOT NULL,
            size       INTEGER NOT NULL,
            files      INTEGER NOT NULL,
            scanned_at REAL    NOT NULL
        )
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None,
                 *, max_age_s: float = 86_400.0) -> None:
        self.path = os.fspath(db_path) if db_path is not None else os.fspath(scan_db())
        self.max_age_s = max_age_s
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._broken = False

    def _connect(self) -> sqlite3.Connection | None:
        """Open on first use. A cache that cannot open is not an error."""
        if self._conn is not None or self._broken:
            return self._conn
        try:
            if self.path != ":memory:":
                ensure(Path(self.path).parent)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=2.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(self._SCHEMA)
            conn.commit()
        except (sqlite3.Error, OSError):
            self._broken = True
            return None
        self._conn = conn
        return conn

    def lookup(self, root: str, mtime_ns: int) -> tuple[int, int] | None:
        """``(size, files)`` for an unchanged, not-yet-stale root."""
        with self._lock:
            conn = self._connect()
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT mtime_ns, size, files, scanned_at FROM scan WHERE root = ?",
                    (root,),
                ).fetchone()
            except sqlite3.Error:
                return None
        if row is None or row[0] != mtime_ns:
            return None
        if time.time() - row[3] > self.max_age_s:
            return None
        return int(row[1]), int(row[2])

    def store(self, root: str, mtime_ns: int, size: int, files: int) -> None:
        with self._lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(
                    "INSERT INTO scan (root, mtime_ns, size, files, scanned_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(root) DO UPDATE SET "
                    "mtime_ns=excluded.mtime_ns, size=excluded.size, "
                    "files=excluded.files, scanned_at=excluded.scanned_at",
                    (root, mtime_ns, size, files, time.time()),
                )
                conn.commit()
            except sqlite3.Error:
                return

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def is_reparse(entry: os.DirEntry[str], attrs: int) -> bool:
    r"""The link test. ``os.path.islink()`` alone is wrong and that is BUG-01.

    ``islink()`` returns **False** for an NTFS junction, which is what
    ``%LOCALAPPDATA%\Application Data`` is. The attribute is the primary signal;
    ``isjunction()``/``is_symlink()`` is the fallback for a host that does not
    populate ``st_file_attributes`` at all (any non-Windows test runner).

    Public because :mod:`adc.engine.explorer` classifies a folder's children from
    its own ``scandir`` pass and has to reach the same verdict this walk does --
    two link tests that disagree would be BUG-01 back in a second place.
    """
    if attrs:
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    try:
        # entry.is_symlink() is served from the scandir buffer: no extra stat.
        return entry.is_symlink() or os.path.isjunction(entry.path)
    except OSError:
        return True  # cannot tell -> do not descend


def allocated_size(path: str, attrs: int, logical: int) -> int:
    """Space this file occupies, asking Windows only when it can differ.

    ``GetCompressedFileSizeW`` is a syscall per file; on a million-file walk that
    is the difference between seconds and minutes. Only sparse and compressed
    files can have an allocation below their length, and the attribute already
    tells us which those are. Cluster-slack rounding *up* is deliberately not
    modelled: it would need the volume cluster size and it never causes the
    over-reporting that BUG-12 was about.

    Public for the same reason as :func:`is_reparse`: the explorer already holds a
    ``stat`` for each file it lists, and pricing it here costs one attribute test
    where routing it through :func:`walk_size` would cost a second ``os.stat``.
    """
    if not attrs & _ALLOCATION_DIFFERS:
        return logical
    try:
        measured = win.get_size_on_disk(path)
    except (OSError, win.UnsupportedPlatformError):
        return logical
    return logical if measured is None else measured


def _measure_single_file(path: str, size_on_disk: bool) -> WalkResult:
    """A target can be one file (``ext4.vhdx``), not only a directory."""
    result = WalkResult(root=path, measured_on_disk=size_on_disk)
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        result.denied.append(f"{path}: {exc.strerror or exc}")
        return result
    attrs = getattr(st, "st_file_attributes", 0)
    result.files = 1
    result.logical = st.st_size
    if size_on_disk:
        result.on_disk = allocated_size(path, attrs, st.st_size)
    return result


def walk_size(
    root: str | os.PathLike[str],
    *,
    cancel: CancelToken,
    on_progress: ProgressFn | None = None,
    budget_s: float | None = None,
    size_on_disk: bool = True,
    cache: ScanCache | None = None,
) -> WalkResult:
    """Total the tree at *root* without following reparse points.

    Contract (docs/02-SPEC.md 4.1):

    * exactly **one** ``entry.stat(follow_symlinks=False)`` per entry, and no
      ``is_dir()``/``is_file()`` calls -- directory-ness comes from ``st_mode``;
    * an explicit stack, so depth costs memory rather than C stack;
    * *cancel* is consulted on every entry, so a cancel lands in milliseconds;
    * ``budget_s`` caps wall-clock and sets ``truncated`` instead of hanging;
    * ``PermissionError`` paths are collected, never raised -- a locked-down
      subtree must not lose the rest of the walk.

    Never raises for anything it finds inside the tree. It can raise only if
    *root* itself is unusable in a way ``os.stat`` reports.
    """
    started = time.monotonic()
    root_abs = os.path.abspath(os.fspath(root))
    deadline = None if budget_s is None else started + budget_s

    try:
        root_st = os.stat(root_abs, follow_symlinks=False)
    except OSError as exc:
        result = WalkResult(root=root_abs, measured_on_disk=size_on_disk)
        result.denied.append(f"{root_abs}: {exc.strerror or exc}")
        result.elapsed_s = time.monotonic() - started
        return result

    if not stat.S_ISDIR(root_st.st_mode):
        result = _measure_single_file(root_abs, size_on_disk)
        result.elapsed_s = time.monotonic() - started
        return result

    if cache is not None:
        hit = cache.lookup(root_abs, root_st.st_mtime_ns)
        if hit is not None:
            size, files = hit
            return WalkResult(
                root=root_abs,
                logical=size,
                on_disk=size,
                files=files,
                measured_on_disk=size_on_disk,
                cached=True,
                elapsed_s=time.monotonic() - started,
            )

    result = WalkResult(root=root_abs, measured_on_disk=size_on_disk)
    stack: list[str] = [root_abs]
    seen = 0
    stop = False

    while stack and not stop:
        current = stack.pop()
        try:
            scanner = os.scandir(current)
        except PermissionError:
            result.denied.append(current)
            continue
        except FileNotFoundError:
            continue  # deleted from under us mid-walk; not an error
        except OSError:
            result.denied.append(current)
            continue

        with scanner:
            try:
                for entry in scanner:
                    seen += 1
                    # BUG-13: the check that makes cancel real, on every entry.
                    if cancel.cancelled:
                        stop = True
                        result.truncated = True
                        break
                    if seen % CLOCK_EVERY == 0:
                        out_of_time = deadline is not None and time.monotonic() >= deadline
                        if out_of_time or cancel.expired:
                            stop = True
                            result.truncated = True
                            break
                        if on_progress is not None and seen % PROGRESS_EVERY == 0:
                            on_progress(result.files, result.size, current)

                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        result.denied.append(entry.path)
                        continue

                    attrs = getattr(st, "st_file_attributes", 0)
                    if is_reparse(entry, attrs):
                        # BUG-01: do not descend, do not count. The target of a
                        # junction is either outside the tree (not ours to
                        # count) or inside it (counted on its own).
                        result.skipped_links.append(entry.path)
                        continue

                    if stat.S_ISDIR(st.st_mode):
                        result.dirs += 1
                        stack.append(entry.path)
                        continue

                    result.files += 1
                    result.logical += st.st_size
                    if size_on_disk:
                        result.on_disk += allocated_size(entry.path, attrs, st.st_size)
            except OSError:
                # scandir's iterator can fail part way through a directory.
                result.denied.append(current)

    result.elapsed_s = time.monotonic() - started
    if cache is not None and not result.truncated:
        cache.store(root_abs, root_st.st_mtime_ns, result.size, result.files)
    return result
