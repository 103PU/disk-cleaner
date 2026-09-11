r"""Volume tests. The point of the module is BUG-02: the disk a target lives on.

v1 summed every target into one number and showed it against C:. The pnpm store
on this machine is on ``E:\``, so a clean that "freed 900 MB" freed nothing on
the drive the user was worried about. ``volume_for`` resolves first, so a target
reached through a junction is billed to the disk the bytes are really on.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from adc.engine import volumes as vol
from adc.engine.volumes import Volume
from tests.fixtures.make_tree import FixtureUnavailable, make_junction, write_file


# ---------------------------------------------------------------------------
# Volume arithmetic -- pure, so it is checked with numbers, not the real disk
# ---------------------------------------------------------------------------
def test_volume_derives_used_and_pct() -> None:
    v = Volume(root="C:" + os.sep, letter="C", label="PU", filesystem="NTFS",
               total=1000, free=250)

    assert v.used == 750
    assert v.free_pct == pytest.approx(25.0)
    assert v.is_ntfs is True
    assert v.display_name == "C: (PU)"


def test_volume_with_no_label_shows_just_the_letter() -> None:
    v = Volume(root="D:" + os.sep, letter="D", label="", filesystem="NTFS",
               total=10, free=1)

    assert v.display_name == "D:"


def test_volume_free_pct_of_a_zero_sized_volume_is_zero() -> None:
    """An unmounted or refusing volume must not divide by zero in the header."""
    v = Volume(root="Z:" + os.sep, letter="Z", label="", filesystem="", total=0, free=0)

    assert v.free_pct == 0.0
    assert v.used == 0
    assert v.is_ntfs is False


def test_volume_used_never_negative() -> None:
    """``free`` can exceed ``total`` on a quota'd or over-provisioned volume."""
    v = Volume(root="Q:" + os.sep, letter="Q", label="", filesystem="NTFS",
               total=100, free=500)

    assert v.used == 0


def test_volume_as_dict_is_json_ready() -> None:
    body = Volume(root="E:" + os.sep, letter="E", label="DATA", filesystem="ntfs",
                  total=3, free=1).as_dict()

    assert body["free_pct"] == 33.33, "rounded for display"
    assert body["is_ntfs"] is True, "filesystem comparison must be case-folded"
    assert body["display_name"] == "E: (DATA)"
    assert set(body) == {
        "root", "letter", "label", "filesystem", "total", "free",
        "used", "free_pct", "is_ntfs", "display_name",
    }


# ---------------------------------------------------------------------------
# Enumeration against the real machine
# ---------------------------------------------------------------------------
def test_fixed_volumes_finds_the_system_drive() -> None:
    """Whatever else is attached, the drive Windows booted from must be listed."""
    found = vol.fixed_volumes()

    assert found, "no fixed volume enumerated"
    roots = [v.root for v in found]
    assert roots == sorted(roots), "not ordered by drive letter"
    if os.name == "nt":
        system = os.path.splitdrive(os.environ["SYSTEMROOT"])[0].upper() + os.sep
        assert system in roots
        assert all(v.total > 0 for v in found)


@pytest.mark.windows_only
def test_fixed_volumes_excludes_non_fixed_drives() -> None:
    """Removable, network and optical drives are none of a cleaner's business."""
    from adc.engine import platform_win as win

    for v in vol.fixed_volumes():
        assert win.drive_type(v.root) == win.DRIVE_FIXED


def test_free_space_matches_disk_usage_for_a_real_root() -> None:
    root = vol.fixed_volumes()[0].root

    assert vol.free_space(root) == pytest.approx(shutil.disk_usage(root).free, rel=0.01)


def test_free_space_of_an_absent_volume_is_zero_not_an_error() -> None:
    """A volume can vanish between the plan and the report; 0 keeps the report."""
    assert vol.free_space("Q:" + os.sep + "nope") == 0


# ---------------------------------------------------------------------------
# volume_for / group_by_volume -- the BUG-02 fix
# ---------------------------------------------------------------------------
def test_volume_for_returns_the_drive_root(tmp_path: Path) -> None:
    expected = os.path.splitdrive(os.path.abspath(tmp_path))[0].upper() + os.sep

    assert vol.volume_for(tmp_path) == expected
    assert vol.volume_for(tmp_path / "deep" / "path.bin") == expected


def test_volume_for_upper_cases_the_letter(tmp_path: Path) -> None:
    """``e:`` and ``E:`` must land in one bucket, not two."""
    drive = os.path.splitdrive(os.path.abspath(tmp_path))[0]

    assert vol.volume_for(drive.lower() + os.sep) == drive.upper() + os.sep


@pytest.mark.windows_only
def test_volume_for_follows_a_junction_to_the_real_disk(tmp_path: Path) -> None:
    r"""The whole reason the function resolves before splitting the drive.

    A store reached through a junction has to be attributed to the volume its
    bytes occupy; otherwise the per-disk total repeats v1's mistake in a new
    place.
    """
    real = tmp_path / "real-store"
    write_file(real / "pkg.bin", 32)
    try:
        link = make_junction(tmp_path / "linked-store", real)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    assert vol.volume_for(link) == vol.volume_for(real)


def test_volume_letter_is_the_identifier_settings_stores(tmp_path: Path) -> None:
    """``volume_ids`` holds letters, so this is the bridge between a path and one."""
    expected = os.path.splitdrive(os.path.abspath(tmp_path))[0][0].upper()

    assert vol.volume_letter(tmp_path) == expected
    assert vol.volume_letter(tmp_path / "deep" / "file.bin") == expected
    assert vol.volume_letter(expected.lower() + ":" + os.sep) == expected


def test_volume_letter_of_something_with_no_drive_is_empty() -> None:
    """A UNC share has no letter, and an empty answer is how the filter says so."""
    assert vol.volume_letter(r"\\server\share\folder") == ""


def test_group_by_volume_buckets_and_preserves_order(tmp_path: Path) -> None:
    root = os.path.splitdrive(os.path.abspath(tmp_path))[0].upper() + os.sep
    paths = [str(tmp_path / "a"), str(tmp_path / "b"), "Q:" + os.sep + "elsewhere"]

    grouped = vol.group_by_volume(paths)

    assert grouped[root] == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert grouped["Q:" + os.sep] == ["Q:" + os.sep + "elsewhere"]
    assert len(grouped) == 2


def test_group_by_volume_of_nothing_is_empty() -> None:
    assert vol.group_by_volume([]) == {}
