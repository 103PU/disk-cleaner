"""Uninstaller Leftovers Hunter engine (Phase 3 v2.6.0).

Scans %APPDATA%, %LOCALAPPDATA%, and %ProgramData% for orphaned directories left behind
by uninstalled applications. Verifies against Windows Registry uninstall keys and
existing program binaries in Program Files / Local Programs.
"""

from __future__ import annotations

import contextlib
import os
import stat
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from .fsutil import is_reparse
from .jobs import CancelToken

try:
    import winreg
except ImportError:
    winreg = None  # type: ignore[assignment]


# Folders that belong to Windows, hardware drivers, runtime engines or core system components
SYSTEM_EXCLUDES: Final[frozenset[str]] = frozenset(
    {
        "microsoft",
        "windows",
        "packages",
        "temp",
        "google",
        "nvidia",
        "intel",
        "amd",
        "system",
        "common files",
        "desktop",
        "start menu",
        "programs",
        "oracle",
        "java",
        "powershell",
        "terminal",
        "git",
        "github",
        "antigravity-cli",
        "pip",
        "pipx",
        "virtualstore",
        "crashdumps",
        "connecteddevicesplatform",
        "publishers",
        "cryptneturlcache",
        "fontcache",
        "group-policy",
        "assembly",
        "cache",
    }
)


def _node_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class LeftoverItem:
    node_id: str
    name: str
    path: str
    location: str
    size: int
    files: int
    mtime: float
    reason_vi: str
    reason_en: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "path": self.path,
            "location": self.location,
            "size": self.size,
            "files": self.files,
            "mtime": self.mtime,
            "reason_vi": self.reason_vi,
            "reason_en": self.reason_en,
        }


@dataclass
class LeftoversResult:
    items: list[LeftoverItem] = field(default_factory=list)
    total_bytes: int = 0
    scanned_folders: int = 0
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.as_dict() for item in self.items],
            "total_bytes": self.total_bytes,
            "total_items": len(self.items),
            "scanned_folders": self.scanned_folders,
            "truncated": self.truncated,
        }


def get_installed_software_names() -> set[str]:
    """Collect lowercase names and identifiers of currently installed software."""
    names: set[str] = set()

    if winreg is not None:
        hives = [
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
            ),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ),
            (
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
            ),
        ]
        for hive, subkey in hives:
            with contextlib.suppress(OSError), winreg.OpenKey(hive, subkey) as k:
                count, _, _ = winreg.QueryInfoKey(k)
                for i in range(count):
                    with contextlib.suppress(OSError):
                        sub = winreg.EnumKey(k, i)
                        names.add(sub.lower())
                        with winreg.OpenKey(k, sub) as sk:
                            with contextlib.suppress(OSError):
                                dn, _ = winreg.QueryValueEx(sk, "DisplayName")
                                if dn:
                                    names.add(str(dn).lower())
                            with contextlib.suppress(OSError):
                                il, _ = winreg.QueryValueEx(sk, "InstallLocation")
                                if il:
                                    names.add(
                                        os.path.basename(
                                            str(il).rstrip(r"\/")
                                        ).lower()
                                    )

    # Check active Program Files directories
    prog_dirs = [
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ]
    for pdir in prog_dirs:
        if pdir and os.path.isdir(pdir):
            with contextlib.suppress(OSError):
                for entry in os.listdir(pdir):
                    names.add(entry.lower())

    return names


def _dir_size_and_files(
    folder: str, token: CancelToken | None
) -> tuple[int, int, float]:
    """Measure total size, file count, and latest mtime of a directory tree."""
    total_size = 0
    total_files = 0
    latest_mtime = 0.0

    stack = [folder]
    while stack:
        if token is not None and token.cancelled:
            break
        curr = stack.pop()
        try:
            with os.scandir(curr) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = getattr(st, "st_file_attributes", 0)
                    if is_reparse(entry, attrs):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total_size += st.st_size
                        total_files += 1
                        if st.st_mtime > latest_mtime:
                            latest_mtime = st.st_mtime
        except OSError:
            continue

    return total_size, total_files, latest_mtime


def find_uninstaller_leftovers(
    *,
    custom_roots: Sequence[tuple[str, str]] | None = None,
    installed_names_override: set[str] | None = None,
    min_age_days: int = 0,
    token: CancelToken | None = None,
    budget_s: float | None = None,
) -> LeftoversResult:
    """Find orphaned application leftover directories."""
    result = LeftoversResult()
    t0 = time.monotonic()

    installed = (
        installed_names_override
        if installed_names_override is not None
        else get_installed_software_names()
    )

    roots: list[tuple[str, str]] = []
    if custom_roots is not None:
        roots = list(custom_roots)
    else:
        app_local = os.environ.get("LOCALAPPDATA", "")
        app_roaming = os.environ.get("APPDATA", "")
        prog_data = os.environ.get("PROGRAMDATA", "")
        if app_local and os.path.isdir(app_local):
            roots.append(("Local", app_local))
        if app_roaming and os.path.isdir(app_roaming):
            roots.append(("Roaming", app_roaming))
        if prog_data and os.path.isdir(prog_data):
            roots.append(("ProgramData", prog_data))

    cutoff = time.time() - (min_age_days * 86400.0) if min_age_days > 0 else None

    for loc_name, root_dir in roots:
        if token is not None and token.cancelled:
            result.truncated = True
            break
        if budget_s is not None and (time.monotonic() - t0) >= budget_s:
            result.truncated = True
            break

        try:
            with os.scandir(root_dir) as scanner:
                for entry in scanner:
                    if token is not None and token.cancelled:
                        result.truncated = True
                        break
                    if budget_s is not None and (time.monotonic() - t0) >= budget_s:
                        result.truncated = True
                        break

                    if entry.name.startswith("."):
                        continue

                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue

                    if not stat.S_ISDIR(st.st_mode):
                        continue

                    attrs = getattr(st, "st_file_attributes", 0)
                    if is_reparse(entry, attrs):
                        continue

                    result.scanned_folders += 1
                    lower_name = entry.name.lower()
                    if lower_name in SYSTEM_EXCLUDES or any(
                        lower_name.startswith(ex) for ex in SYSTEM_EXCLUDES
                    ):
                        continue

                    # Check if this folder corresponds to any active installed software
                    matched = False
                    for inst in installed:
                        if len(inst) >= 4 and (lower_name in inst or inst in lower_name):
                            matched = True
                            break
                    if matched:
                        continue

                    # Measure directory size and files
                    size, files, latest_m = _dir_size_and_files(entry.path, token)
                    if cutoff is not None and latest_m > cutoff:
                        continue
                    if size == 0 and files == 0:
                        continue

                    result.items.append(
                        LeftoverItem(
                            node_id=_node_id(),
                            name=entry.name,
                            path=entry.path,
                            location=loc_name,
                            size=size,
                            files=files,
                            mtime=latest_m if latest_m > 0 else st.st_mtime,
                            reason_vi="Không tìm thấy phần mềm tương ứng đã cài đặt trên hệ thống",
                            reason_en="No matching installed software found on the system",
                        )
                    )
                    result.total_bytes += size
        except OSError:
            continue

    # Sort largest first
    result.items.sort(key=lambda x: x.size, reverse=True)
    return result
