r"""Walker tests. Each one pins a defect v1 shipped (docs/01-AUDIT.md).

The junction and sparse-file cases build real NTFS objects. A mock would have
passed the buggy code too -- the bugs were in what Windows reports, not in the
Python around it -- so those tests are marked ``windows_only`` and skip
elsewhere rather than pretending to cover anything.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from adc.engine.fsutil import ScanCache, walk_size
from adc.engine.jobs import CancelToken
from tests.fixtures.make_tree import (
    FixtureUnavailable,
    make_junction,
    make_junction_pair,
    make_plain_tree,
    make_self_referential_junction,
    make_sparse_file,
    make_wide_tree,
    write_file,
)

IS_WINDOWS = os.name == "nt"

# Captured before any monkeypatch: the proxies below have to reach the genuine
# scandir, and patching ``adc.engine.fsutil.os.scandir`` patches the one and only
# ``os`` module -- so a proxy that called ``os.scandir`` would call itself.
_REAL_SCANDIR = os.scandir


class _CountingEntry:
    """Proxy over ``os.DirEntry`` that records which probe the walker used.

    ``os.DirEntry`` is a C type and cannot be patched, so the count has to be
    taken one level up, at ``os.scandir``. Everything the walker is allowed to
    touch is forwarded; anything else would raise ``AttributeError`` and fail the
    test loudly, which is the point.
    """

    def __init__(self, entry: os.DirEntry[str], tally: dict[str, int]) -> None:
        self._entry = entry
        self._tally = tally

    @property
    def name(self) -> str:
        return self._entry.name

    @property
    def path(self) -> str:
        return self._entry.path

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        self._tally["stat"] += 1
        return self._entry.stat(follow_symlinks=follow_symlinks)

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        self._tally["is_dir"] += 1
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        self._tally["is_file"] += 1
        return self._entry.is_file(follow_symlinks=follow_symlinks)

    def is_symlink(self) -> bool:
        self._tally["is_symlink"] += 1
        return self._entry.is_symlink()


class _CountingScanner:
    """Context-manager iterator standing in for ``os.scandir``.

    ``walk_size`` uses ``with os.scandir(d) as it: for entry in it`` so the
    replacement has to be both, and an optional ``after`` hook lets a test fire
    something -- a cancel -- once N entries have been handed out.
    """

    def __init__(self, path: str, tally: dict[str, int], hook: Any = None) -> None:
        self._inner = _REAL_SCANDIR(path)
        self._tally = tally
        self._hook = hook

    def __enter__(self) -> _CountingScanner:
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.close()

    def __iter__(self) -> _CountingScanner:
        return self

    def __next__(self) -> _CountingEntry:
        entry = next(self._inner)
        self._tally["entries"] += 1
        if self._hook is not None:
            self._hook(self._tally["entries"])
        return _CountingEntry(entry, self._tally)


def _patch_scandir(monkeypatch: pytest.MonkeyPatch, hook: Any = None) -> dict[str, int]:
    tally = {"entries": 0, "stat": 0, "is_dir": 0, "is_file": 0, "is_symlink": 0}

    def fake_scandir(path: str) -> _CountingScanner:
        return _CountingScanner(path, tally, hook)

    monkeypatch.setattr("adc.engine.fsutil.os.scandir", fake_scandir)
    return tally


# ---------------------------------------------------------------------------
# BUG-01 -- junctions
# ---------------------------------------------------------------------------
@pytest.mark.windows_only
def test_fsutil_skips_junction(tree: Path) -> None:
    r"""``link -> real`` must not make ``real``'s bytes count twice.

    v1 used ``os.path.islink()``, which is **False** for a junction, so it
    descended and double-counted every junctioned tree.
    """
    try:
        _real, link, expected = make_junction_pair(tree, file_size=4096)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    result = walk_size(tree, cancel=CancelToken())

    assert result.logical == expected, "junction target counted more than once"
    assert result.files == 1
    assert os.path.normcase(str(link)) in [os.path.normcase(p) for p in result.skipped_links]
    assert not result.truncated


@pytest.mark.windows_only
def test_fsutil_self_referential_junction_terminates(tree: Path) -> None:
    r"""A junction pointing at its own ancestor must not loop.

    The exact shape of ``%LOCALAPPDATA%\Application Data``, which is what hung
    v1's scan. The walk must finish, count the payload once, and finish because
    it ran out of tree -- not because a budget cut it off, which would hide the
    bug behind a timeout.
    """
    try:
        _root, expected = make_self_referential_junction(tree, file_size=1024)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    started = time.monotonic()
    result = walk_size(tree, cancel=CancelToken(), budget_s=10.0)
    elapsed = time.monotonic() - started

    assert result.logical == expected
    assert not result.truncated, "terminated by the budget, not by the reparse check"
    assert elapsed < 5.0
    assert len(result.skipped_links) == 1


@pytest.mark.windows_only
def test_fsutil_escape_junction_not_counted(tree: Path, tmp_path: Path) -> None:
    """A junction out of the tree contributes nothing to the total."""
    outside = tmp_path / "outside"
    write_file(outside / "big.bin", 8192)
    write_file(tree / "own.bin", 100)
    try:
        make_junction(tree / "escape", outside)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    result = walk_size(tree, cancel=CancelToken())

    assert result.logical == 100, "counted bytes that live outside the target"


# ---------------------------------------------------------------------------
# BUG-13 -- one stat per entry, and real cancellation
# ---------------------------------------------------------------------------
def test_fsutil_single_stat_per_entry(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly one ``stat`` per entry, and no ``is_dir``/``is_file`` at all.

    v1 called ``is_file()`` and then ``stat()``, doubling the syscalls on the
    hot loop. Directory-ness here comes from ``st_mode`` on the single stat.
    """
    _root, _total_bytes, _files = make_plain_tree(tree, files_per_dir=4, depth=3)
    tally = _patch_scandir(monkeypatch)

    result = walk_size(tree, cancel=CancelToken(), size_on_disk=False)

    assert tally["entries"] > 0
    assert tally["stat"] == tally["entries"], "more than one stat per entry"
    assert tally["is_dir"] == 0
    assert tally["is_file"] == 0
    assert result.files == 12


def test_fsutil_cancel_mid_walk(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel at entry 100 stops the walk, not just the UI's polling.

    BUG-13: v1's cancel button stopped the front end from asking for progress
    while the walker kept running to completion in the background. The proof
    that cancellation is real is that the walker returns having seen far fewer
    files than the tree contains.
    """
    make_wide_tree(tree, count=2000, file_size=32)
    token = CancelToken()

    def trip(seen: int) -> None:
        if seen == 100:
            token.cancel()

    tally = _patch_scandir(monkeypatch, hook=trip)

    started = time.monotonic()
    result = walk_size(tree, cancel=token, size_on_disk=False)
    elapsed = time.monotonic() - started

    assert result.truncated is True
    assert elapsed < 0.2, f"cancel took {elapsed:.3f}s to land"
    assert tally["entries"] == 100, "kept scanning after the cancel"
    assert result.files < 2000, "walked the whole tree despite the cancel"


def test_fsutil_budget_truncates(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wall-clock budget sets ``truncated`` instead of hanging.

    The clock is sampled every ``CLOCK_EVERY`` entries, so the tree has to be
    wide enough to reach a sample point.
    """
    make_wide_tree(tree, count=1200, file_size=16)
    monkeypatch.setattr("adc.engine.fsutil.CLOCK_EVERY", 1)

    result = walk_size(tree, cancel=CancelToken(), budget_s=0.0)

    assert result.truncated is True


def test_fsutil_no_budget_completes(tree: Path) -> None:
    """The default is no budget, and a small tree is never marked truncated."""
    _root, total_bytes, files = make_plain_tree(tree, files_per_dir=2, depth=2)

    result = walk_size(tree, cancel=CancelToken(), size_on_disk=False)

    assert result.logical == total_bytes
    assert result.files == files
    assert result.truncated is False
    assert result.denied == []


# ---------------------------------------------------------------------------
# BUG-12 -- size on disk
# ---------------------------------------------------------------------------
@pytest.mark.windows_only
def test_fsutil_size_on_disk_sparse(tmp_path: Path) -> None:
    r"""A sparse file's allocation is far below its length, and we report both.

    The model for WSL2's ``ext4.vhdx``. v1 reported ``st_size``, so it promised
    tens of gigabytes that deleting the file would never return.
    """
    try:
        sparse = make_sparse_file(tmp_path / "ext4.vhdx", logical=64 * 1024 * 1024)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    result = walk_size(sparse, cancel=CancelToken(), size_on_disk=True)

    assert result.logical == 64 * 1024 * 1024
    assert result.on_disk < result.logical, "reported the logical length as reclaimable"
    assert result.size == result.on_disk
    assert result.divergent is True
    assert result.as_dict()["on_disk"] == result.on_disk


def test_fsutil_size_on_disk_off_reports_logical(tree: Path) -> None:
    """``size_on_disk=False`` reports ``st_size`` and says so in ``as_dict``."""
    write_file(tree / "a.bin", 2048)

    result = walk_size(tree, cancel=CancelToken(), size_on_disk=False)

    assert result.size == result.logical == 2048
    assert result.divergent is False
    assert result.as_dict()["on_disk"] is None


def test_fsutil_ordinary_file_sizes_agree(tree: Path) -> None:
    """An ordinary file is not sent through ``GetCompressedFileSizeW`` at all.

    Only sparse and compressed files can allocate less than their length, so the
    syscall is skipped for everything else -- the difference between seconds and
    minutes on a million-file walk.
    """
    write_file(tree / "plain.bin", 5000)

    result = walk_size(tree, cancel=CancelToken(), size_on_disk=True)

    assert result.logical == result.on_disk == 5000
    assert result.divergent is False


# ---------------------------------------------------------------------------
# Robustness: nothing inside the tree may abort the walk
# ---------------------------------------------------------------------------
def test_fsutil_missing_root_is_reported_not_raised(tmp_path: Path) -> None:
    """A vanished root yields an empty result with a reason, never an exception.

    The catalogue holds targets that legitimately do not exist on a given
    machine; ``unavailable`` is a UI state, not a crash.
    """
    result = walk_size(tmp_path / "does-not-exist", cancel=CancelToken())

    assert result.files == 0
    assert result.logical == 0
    assert len(result.denied) == 1


def test_fsutil_unreadable_entry_is_collected(tree: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``PermissionError`` on one entry is recorded; the rest still counts.

    The UI turns this into "N items need Administrator" instead of silently
    under-reporting.
    """
    write_file(tree / "ok.bin", 700)
    write_file(tree / "locked.bin", 700)

    class Blocked(_CountingEntry):
        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            if self.name == "locked.bin":
                raise PermissionError(5, "Access is denied")
            return super().stat(follow_symlinks=follow_symlinks)

    tally = {"entries": 0, "stat": 0, "is_dir": 0, "is_file": 0, "is_symlink": 0}

    class Scanner(_CountingScanner):
        def __next__(self) -> _CountingEntry:
            return Blocked(next(self._inner), self._tally)

    monkeypatch.setattr("adc.engine.fsutil.os.scandir",
                        lambda p: Scanner(p, tally))

    result = walk_size(tree, cancel=CancelToken(), size_on_disk=False)

    assert result.logical == 700
    assert result.denied_count == 1
    assert result.denied[0].endswith("locked.bin")


def test_fsutil_empty_directory(tree: Path) -> None:
    result = walk_size(tree, cancel=CancelToken())
    assert (result.files, result.dirs, result.logical, result.size) == (0, 0, 0, 0)
    assert result.divergent is False


# ---------------------------------------------------------------------------
# Scan cache
# ---------------------------------------------------------------------------
def test_scan_cache_hit_and_invalidation(tree: Path) -> None:
    """A second walk is served from cache; touching the tree invalidates it.

    The key is the root's ``mtime_ns``, which moves when a direct child is
    added -- the case this test covers and the only case the key is sound for.
    """
    write_file(tree / "a.bin", 1000)
    cache = ScanCache(":memory:")

    first = walk_size(tree, cancel=CancelToken(), cache=cache, size_on_disk=False)
    second = walk_size(tree, cancel=CancelToken(), cache=cache, size_on_disk=False)

    assert first.cached is False
    assert second.cached is True
    assert second.size == first.size

    mtime = os.stat(tree).st_mtime + 2.0
    os.utime(tree, (mtime, mtime))
    write_file(tree / "b.bin", 1000)
    third = walk_size(tree, cancel=CancelToken(), cache=cache, size_on_disk=False)

    assert third.cached is False
    assert third.size == 2000



def test_scan_cache_does_not_store_truncated(tree: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled walk must never poison the cache with a partial total."""
    make_wide_tree(tree, count=300, file_size=16)
    cache = ScanCache(":memory:")
    token = CancelToken()
    _patch_scandir(monkeypatch, hook=lambda seen: token.cancel() if seen == 10 else None)

    partial = walk_size(tree, cancel=token, cache=cache, size_on_disk=False)
    assert partial.truncated is True

    monkeypatch.undo()
    full = walk_size(tree, cancel=CancelToken(), cache=cache, size_on_disk=False)

    assert full.cached is False
    assert full.files == 300


def test_scan_cache_survives_an_unusable_database(tmp_path: Path) -> None:
    """A cache that cannot open degrades to no caching, never to an error."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    cache = ScanCache(str(blocker / "sub" / "scan.sqlite"))

    assert cache.lookup("whatever", 1) is None
    cache.store("whatever", 1, 10, 1)
    assert cache.lookup("whatever", 1) is None
    cache.close()
