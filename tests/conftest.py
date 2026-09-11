"""Shared fixtures.

Two jobs here. The first is to make the Windows-only tests *skip* rather than
fail on a host that cannot build a junction, so a red suite always means a real
defect. The second is to keep every test out of the real profile: the engine
writes ``cache/scan.sqlite`` and ``reports/*.json`` under ``%LOCALAPPDATA%``, and
a test that touched those would be editing the user's actual history.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

IS_WINDOWS = sys.platform == "win32"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Turn the ``windows_only`` marker into a skip off-Windows."""
    if IS_WINDOWS:
        return
    skip = pytest.mark.skip(reason="needs real Win32 behaviour (junction, sparse, shell API)")
    for item in items:
        if "windows_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``ADC_DATA_DIR`` at a tmp tree for the duration of one test."""
    root = tmp_path / "adc-data"
    monkeypatch.setenv("ADC_DATA_DIR", str(root))
    yield root


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """An empty directory to build a fixture shape inside.

    Named so the assertions read as ``walk_size(tree)``; ``tmp_path`` itself is
    left alone so a test can put a second, unrelated shape beside it -- which is
    how the escape-via-junction tests get somewhere outside the root to point at.
    """
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the scan cache from the equation.

    ``walk_size`` only caches when handed a ``ScanCache``, so this is belt and
    braces for any future default -- and it documents the intent that walker
    tests measure the walker, never a cache hit.
    """
    monkeypatch.setenv("ADC_NO_SCAN_CACHE", "1")
    monkeypatch.delenv("ADC_SCAN_BUDGET", raising=False)
    assert os.environ.get("ADC_NO_SCAN_CACHE") == "1"
