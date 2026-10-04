"""Unit tests for Bridge large_files and duplicates integration (v2.5.0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from adc.shell.bridge import Bridge


@pytest.fixture
def test_bridge() -> Bridge:
    return Bridge()


def test_bridge_large_files_find(test_bridge: Bridge, tmp_path: Path) -> None:
    f1 = tmp_path / "big1.mp4"
    f1.write_bytes(b"x" * 2000)

    f2 = tmp_path / "big2.iso"
    f2.write_bytes(b"y" * 5000)

    res = test_bridge.large_files_find(str(tmp_path), min_size_mb=0, limit=10)
    assert res["ok"] is True
    data = res["data"]
    assert len(data["files"]) == 2
    assert data["files"][0]["name"] == "big2.iso"
    assert data["files"][0]["category"] == "archive"
    assert data["files"][1]["name"] == "big1.mp4"
    assert data["files"][1]["category"] == "media"

    # Test reveal uses _node_path successfully
    node_id = data["files"][0]["node_id"]
    rev = test_bridge.explore_reveal(node_id)
    assert rev["ok"] is True
    assert rev["data"]["node_id"] == node_id


def test_bridge_duplicates_find_and_delete(test_bridge: Bridge, tmp_path: Path) -> None:
    content = b"IDENTICAL DUPLICATE DATA CONTENT 123" * 50
    f1 = tmp_path / "f1.bin"
    f2 = tmp_path / "f2.bin"
    f1.write_bytes(content)
    f2.write_bytes(content)

    res = test_bridge.duplicates_find(str(tmp_path), min_size_kb=0, limit=10)
    assert res["ok"] is True
    groups = res["data"]["groups"]
    assert len(groups) == 1
    items = groups[0]["items"]
    assert len(items) == 2

    # Find the one not suggested to keep and delete it
    dupe = next(it for it in items if not it["suggested_keep"])
    del_res = test_bridge.duplicates_delete([dupe["node_id"]])
    assert del_res["ok"] is True
    assert del_res["data"]["deleted"] == 1
    assert del_res["data"]["freed"] == len(content)
    assert not Path(dupe["path"]).exists()
    # The original file is still intact!
    orig = next(it for it in items if it["suggested_keep"])
    assert Path(orig["path"]).exists()
