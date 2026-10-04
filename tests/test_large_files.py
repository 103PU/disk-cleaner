"""Tests for the Large Files Hunter engine (SPEC 6.2 extension / v2.5.0)."""

from __future__ import annotations

from pathlib import Path

from adc.engine.jobs import CancelToken
from adc.engine.large_files import (
    LargeFilesResult,
    categorize_file,
    find_large_files,
)


def test_categorize_file_by_extension() -> None:
    assert categorize_file("movie.mp4") == "media"
    assert categorize_file("track.mkv") == "media"
    assert categorize_file("backup.iso") == "archive"
    assert categorize_file("archive.zip") == "archive"
    assert categorize_file("setup.exe") == "installer"
    assert categorize_file("installer.msi") == "installer"
    assert categorize_file("document.pdf") == "document"
    assert categorize_file("random.bin") == "other"
    assert categorize_file("no_extension") == "other"


def test_find_large_files_finds_and_sorts_by_size(tmp_path: Path) -> None:
    # Create test files
    sub1 = tmp_path / "sub1"
    sub1.mkdir()
    f1 = sub1 / "small.txt"
    f1.write_bytes(b"x" * 100)

    f2 = sub1 / "medium.bin"
    f2.write_bytes(b"y" * 1000)

    f3 = tmp_path / "large.iso"
    f3.write_bytes(b"z" * 5000)

    f4 = tmp_path / "giant.mp4"
    f4.write_bytes(b"w" * 10000)

    # Search with min_size = 500
    res: LargeFilesResult = find_large_files(str(tmp_path), min_size_bytes=500, limit=10)

    assert not res.truncated
    assert len(res.files) == 3
    # Sorted descending by size
    assert res.files[0].name == "giant.mp4"
    assert res.files[0].size == 10000
    assert res.files[0].category == "media"

    assert res.files[1].name == "large.iso"
    assert res.files[1].size == 5000
    assert res.files[1].category == "archive"

    assert res.files[2].name == "medium.bin"
    assert res.files[2].size == 1000


def test_find_large_files_respects_limit(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"file_{i}.bin").write_bytes(b"x" * (1000 + i * 100))

    res = find_large_files(str(tmp_path), min_size_bytes=1000, limit=3)
    assert len(res.files) == 3
    assert res.files[0].size == 1900
    assert res.files[1].size == 1800
    assert res.files[2].size == 1700


def test_find_large_files_respects_cancel_token(tmp_path: Path) -> None:
    for i in range(50):
        d = tmp_path / f"dir_{i}"
        d.mkdir()
        (d / "file.bin").write_bytes(b"x" * 2000)

    token = CancelToken()
    token.cancel()

    res = find_large_files(str(tmp_path), min_size_bytes=1000, limit=100, token=token)
    assert res.truncated or len(res.files) < 50


def test_find_large_files_as_dict(tmp_path: Path) -> None:
    f = tmp_path / "big.zip"
    f.write_bytes(b"a" * 2048)

    res = find_large_files(str(tmp_path), min_size_bytes=1000)
    d = res.as_dict()
    assert d["root"] == str(tmp_path)
    assert len(d["files"]) == 1
    file_dict = d["files"][0]
    assert file_dict["name"] == "big.zip"
    assert file_dict["size"] == 2048
    assert file_dict["category"] == "archive"
    assert file_dict["node_id"]
