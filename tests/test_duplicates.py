"""Tests for the Smart Duplicate File Finder engine (v2.5.0)."""

from __future__ import annotations

import os
from pathlib import Path

from adc.engine.duplicates import (
    DuplicateGroup,
    DuplicatesResult,
    find_duplicates,
)
from adc.engine.jobs import CancelToken


def test_find_duplicates_detects_identical_files(tmp_path: Path) -> None:
    # 2 identical files with same content
    content_a = b"Exact identical content test data 12345" * 100
    (tmp_path / "orig.dat").write_bytes(content_a)
    (tmp_path / "copy.dat").write_bytes(content_a)

    # 1 file with same size but different middle content
    content_b = b"Exact identical content test data 99999" * 100
    assert len(content_a) == len(content_b)
    (tmp_path / "diff.dat").write_bytes(content_b)

    # 1 file with different size
    (tmp_path / "other.dat").write_bytes(b"short")

    res: DuplicatesResult = find_duplicates(str(tmp_path), min_size_bytes=10)

    assert not res.truncated
    assert len(res.groups) == 1
    group: DuplicateGroup = res.groups[0]
    assert len(group.items) == 2
    names = {item.name for item in group.items}
    assert names == {"orig.dat", "copy.dat"}
    assert group.size == len(content_a)
    assert group.wasted_size == len(content_a)  # 1 duplicate = 1 * size


def test_find_duplicates_ignores_empty_files(tmp_path: Path) -> None:
    (tmp_path / "empty1.txt").write_bytes(b"")
    (tmp_path / "empty2.txt").write_bytes(b"")

    res = find_duplicates(str(tmp_path), min_size_bytes=0)
    # Zero-byte files must not be considered reclaimable duplicates
    assert len(res.groups) == 0


def test_find_duplicates_suggests_keep_based_on_mtime(tmp_path: Path) -> None:
    content = b"Some duplicate payload" * 50
    f1 = tmp_path / "older.txt"
    f2 = tmp_path / "newer.txt"
    f1.write_bytes(content)
    f2.write_bytes(content)

    os.utime(f1, (1000, 1000))
    os.utime(f2, (2000, 2000))

    res = find_duplicates(str(tmp_path), min_size_bytes=10)
    assert len(res.groups) == 1
    items = res.groups[0].items
    older_item = next(it for it in items if it.name == "older.txt")
    newer_item = next(it for it in items if it.name == "newer.txt")

    # Older file should be suggested to keep (original)
    assert older_item.suggested_keep is True
    assert newer_item.suggested_keep is False


def test_find_duplicates_respects_cancel_token(tmp_path: Path) -> None:
    token = CancelToken()
    token.cancel()

    res = find_duplicates(str(tmp_path), min_size_bytes=10, token=token)
    assert res.truncated or len(res.groups) == 0
