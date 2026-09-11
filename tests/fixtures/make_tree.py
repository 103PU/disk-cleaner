r"""Build the on-disk shapes the walker has to survive.

These are real NTFS objects, not mocks: the defects in v1 (BUG-01, BUG-12) were
defects in how Windows reports junctions and sparse files, so a fake would have
happily passed the buggy code too. Junctions are created with ``mklink /J``,
which needs no elevation, and sparse files with ``fsutil sparse setflag``.

Every helper returns the path it made and raises ``FixtureUnavailable`` when the
host cannot provide the shape, so a test can skip rather than fail red on a
machine without NTFS.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


class FixtureUnavailable(RuntimeError):
    """The host cannot build this shape (no NTFS, no mklink, no privilege)."""


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a console tool with no window and no shell.

    ``shell=False`` matters even in a fixture: the paths below come from
    ``tmp_path`` and would otherwise be re-parsed by cmd's quoting rules.
    """
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def write_file(path: Path, size: int = 1024, *, fill: bytes = b"a") -> Path:
    """A file of exactly *size* bytes, so a test can assert on the total."""
    path.parent.mkdir(parents=True, exist_ok=True)
    block = fill * 4096
    with path.open("wb") as fh:
        remaining = size
        while remaining > 0:
            chunk = min(remaining, len(block))
            fh.write(block[:chunk])
            remaining -= chunk
    return path


def make_junction(link: Path, target: Path) -> Path:
    r"""``mklink /J`` -- an NTFS directory junction.

    This is the object ``os.path.islink()`` lies about: it returns **False** for
    a junction, which is precisely why v1 walked into
    ``%LOCALAPPDATA%\Application Data`` forever.

    ``mklink`` is a cmd builtin, so it has to be invoked through ``cmd /c``;
    there is no ``mklink.exe``. Junctions -- unlike symlinks -- need no
    Developer Mode and no elevation.
    """
    if not IS_WINDOWS:
        raise FixtureUnavailable("junctions need Windows")
    link.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    proc = _run(["cmd", "/c", "mklink", "/J", str(link), str(target)])
    if proc.returncode != 0 or not link.exists():
        raise FixtureUnavailable(f"mklink /J failed: {proc.stdout.strip()} {proc.stderr.strip()}")
    return link


def make_sparse_file(path: Path, *, logical: int = 64 * 1024 * 1024) -> Path:
    """A file whose length is *logical* but whose allocation is ~one cluster.

    The model for WSL2's ``ext4.vhdx``: ``st_size`` says 64 MB, the space a
    delete would return is a few KB. ``GetCompressedFileSizeW`` is the only API
    that tells the truth, and BUG-12 was reporting ``st_size`` instead.

    Order matters -- the sparse flag has to be set on an empty file, before any
    data is written, or the already-allocated ranges stay allocated.
    """
    if not IS_WINDOWS:
        raise FixtureUnavailable("sparse files need Windows/NTFS")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    proc = _run(["fsutil", "sparse", "setflag", str(path)])
    if proc.returncode != 0:
        raise FixtureUnavailable(f"fsutil sparse setflag failed: {proc.stdout.strip()}")
    with path.open("r+b") as fh:
        fh.truncate(logical)
        fh.seek(logical - 1)
        fh.write(b"\0")
    if os.stat(path).st_size != logical:
        raise FixtureUnavailable("sparse file did not reach its logical size")
    return path


def make_plain_tree(root: Path, *, files_per_dir: int = 3, depth: int = 3,
                    file_size: int = 1024) -> tuple[Path, int, int]:
    """A predictable tree. Returns ``(root, total_bytes, total_files)``.

    Used by the cancel and single-stat tests, which need a known entry count
    rather than a known shape.
    """
    total_bytes = 0
    total_files = 0
    current = root
    for level in range(depth):
        current.mkdir(parents=True, exist_ok=True)
        for i in range(files_per_dir):
            write_file(current / f"f{level}_{i}.bin", file_size)
            total_bytes += file_size
            total_files += 1
        current = current / f"d{level}"
    return root, total_bytes, total_files


def make_wide_tree(root: Path, *, count: int = 400, file_size: int = 64) -> Path:
    """One directory with *count* files: enough entries to cancel mid-walk."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        write_file(root / f"w{i:04d}.bin", file_size)
    return root


def make_junction_pair(root: Path, *, file_size: int = 2048) -> tuple[Path, Path, int]:
    r"""``root/real/`` with one file, plus ``root/link -> root/real``.

    Walking *root* must count ``file_size`` once. Counting it twice is BUG-01.
    Returns ``(real, link, expected_bytes)``.
    """
    real = root / "real"
    write_file(real / "payload.bin", file_size)
    link = make_junction(root / "link", real)
    return real, link, file_size


def make_self_referential_junction(root: Path, *, file_size: int = 512) -> tuple[Path, int]:
    r"""``root/inner/`` containing a junction back to *root*.

    The exact shape of ``%LOCALAPPDATA%\Application Data``. A walker that
    follows junctions never terminates here. Returns ``(root, expected_bytes)``.
    """
    inner = root / "inner"
    write_file(inner / "leaf.bin", file_size)
    make_junction(inner / "up", root)
    return root, file_size
