"""Unit tests for the uninstaller leftovers hunter engine (Phase 3 v2.6.0)."""

from __future__ import annotations

from pathlib import Path

from adc.engine.jobs import CancelToken
from adc.engine.uninstaller_leftovers import (
    find_uninstaller_leftovers,
    get_installed_software_names,
)


def test_get_installed_software_names_returns_set() -> None:
    names = get_installed_software_names()
    assert isinstance(names, set)
    # On Windows, at least some software or empty set if non-windows
    assert all(isinstance(n, str) for n in names)


def test_find_uninstaller_leftovers_detects_orphaned_folders(tmp_path: Path) -> None:
    local_app_data = tmp_path / "AppData" / "Local"
    local_app_data.mkdir(parents=True)

    # 1. An installed app folder (matches installed software)
    installed_app = local_app_data / "ActiveGame"
    installed_app.mkdir()
    (installed_app / "save.dat").write_bytes(b"DATA" * 100)

    # 2. A system-excluded folder (e.g. Temp or Microsoft)
    sys_dir = local_app_data / "Temp"
    sys_dir.mkdir()
    (sys_dir / "t.tmp").write_bytes(b"TMP")

    # 3. An orphaned app folder (no corresponding software installed)
    orphaned_app = local_app_data / "UninstalledOldTool"
    orphaned_app.mkdir()
    (orphaned_app / "cache.bin").write_bytes(b"OLD_CACHE" * 200)
    (orphaned_app / "log.txt").write_bytes(b"OLD_LOG" * 50)

    custom_roots = [("Local", str(local_app_data))]
    installed_names = {"activegame", "python"}

    res = find_uninstaller_leftovers(
        custom_roots=custom_roots,
        installed_names_override=installed_names,
        min_age_days=0,
    )

    assert len(res.items) == 1
    item = res.items[0]
    assert item.name == "UninstalledOldTool"
    assert item.location == "Local"
    assert item.files == 2
    assert item.size == len(b"OLD_CACHE" * 200) + len(b"OLD_LOG" * 50)
    assert res.total_bytes == item.size
    assert item.as_dict()["name"] == "UninstalledOldTool"


def test_find_uninstaller_leftovers_cancellation(tmp_path: Path) -> None:
    local_app_data = tmp_path / "AppData" / "Local"
    local_app_data.mkdir(parents=True)
    for i in range(5):
        d = local_app_data / f"Orphan_{i}"
        d.mkdir()
        (d / "file.bin").write_bytes(b"x" * 100)

    token = CancelToken()
    token.cancel()

    res = find_uninstaller_leftovers(
        custom_roots=[("Local", str(local_app_data))],
        installed_names_override=set(),
        token=token,
    )

    assert res.truncated is True
