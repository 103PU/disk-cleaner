r"""Fixed volumes and how full they are.

Two jobs. The obvious one is the header the UI shows -- "C: 13.5 GB free of
125.9 GB (10.7 %)" -- which has to be re-read after a clean, because "you got
2.1 GB back" is only believable next to a free-space number that moved.

The less obvious one is deciding *which* disk a target lives on. v1 summed every
target into one number and showed it against C:, but the pnpm store on this
machine is on ``E:\`` (BUG-02): cleaning it frees nothing on C:. Grouping the
plan by volume is what makes the reported total honest.

Removable, network and optical drives are excluded on purpose. A cleaner has no
business on a USB stick it was never pointed at, and ``shutil.disk_usage`` on an
empty optical drive raises after spinning the disc up.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any

from . import platform_win as win


@dataclass(frozen=True)
class Volume:
    """One fixed volume. Sizes in bytes, measured at construction time."""

    root: str            # "C:\\"
    letter: str          # "C"
    label: str           # "PU", or "" when unlabelled
    filesystem: str      # "NTFS"
    total: int
    free: int

    @property
    def used(self) -> int:
        return max(0, self.total - self.free)

    @property
    def free_pct(self) -> float:
        return 0.0 if self.total == 0 else self.free / self.total * 100.0

    @property
    def is_ntfs(self) -> bool:
        """Sparse files, compression and junctions are all NTFS-only features."""
        return self.filesystem.upper() == "NTFS"

    @property
    def display_name(self) -> str:
        return f"{self.letter}: ({self.label})" if self.label else f"{self.letter}:"

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "letter": self.letter,
            "label": self.label,
            "filesystem": self.filesystem,
            "total": self.total,
            "free": self.free,
            "used": self.used,
            "free_pct": round(self.free_pct, 2),
            "is_ntfs": self.is_ntfs,
            "display_name": self.display_name,
        }


def _measure(root: str) -> Volume | None:
    """Build one :class:`Volume`, or ``None`` if the volume will not answer.

    A fixed drive can still refuse: BitLocker-locked, offline, or a mount point
    whose backing device has gone. ``None`` keeps it out of the list instead of
    killing the whole enumeration.
    """
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return None
    try:
        label, filesystem = win.volume_info(root)
    except (OSError, win.UnsupportedPlatformError):
        label, filesystem = "", ""
    return Volume(
        root=root,
        letter=root[0].upper(),
        label=label,
        filesystem=filesystem,
        total=usage.total,
        free=usage.free,
    )


def fixed_volumes() -> list[Volume]:
    """Every fixed volume, ordered by drive letter."""
    if not win.IS_WINDOWS:
        usage = shutil.disk_usage(os.sep)
        return [Volume(os.sep, os.sep, "", "", usage.total, usage.free)]

    out: list[Volume] = []
    for root in win.logical_drives():
        if win.drive_type(root) != win.DRIVE_FIXED:
            continue
        volume = _measure(root)
        if volume is not None:
            out.append(volume)
    out.sort(key=lambda v: v.letter)
    return out


def volume_for(path: str | os.PathLike[str]) -> str:
    r"""The volume root a path belongs to: ``E:\.pnpm-store\v11`` -> ``E:\``.

    Resolves first, so a target reached through a junction is attributed to the
    disk the data is really on -- the whole point of grouping by volume.
    """
    resolved = os.path.realpath(os.path.abspath(os.fspath(path)))
    drive, _ = os.path.splitdrive(resolved)
    if not drive:
        return os.sep
    if drive.startswith("\\\\"):
        return drive          # UNC share: "\\server\share"
    return drive.upper() + os.sep


def volume_letter(path: str | os.PathLike[str]) -> str:
    """``"C"`` for a path on C:, ``""`` for a UNC path or anything odd.

    Letters rather than roots because that is the identifier the UI and the config
    file use for a volume (:func:`adc.engine.settings.clean_exclusions` explains
    why an identifier that cannot be a path is worth the conversion).
    """
    try:
        root = volume_for(path)
    except (OSError, ValueError):
        return ""
    drive = os.path.splitdrive(root)[0]
    return drive[0].upper() if len(drive) == 2 and drive[1] == ":" else ""


def group_by_volume(paths: list[str]) -> dict[str, list[str]]:
    """Bucket *paths* by volume root, so a plan can report per-disk totals."""
    buckets: dict[str, list[str]] = {}
    for path in paths:
        buckets.setdefault(volume_for(path), []).append(path)
    return buckets


def free_space(root: str) -> int:
    """Free bytes on *root* right now. Used for the before/after measurement."""
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return 0
