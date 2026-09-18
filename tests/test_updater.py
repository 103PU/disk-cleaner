"""Tests for adc.engine.updater: version parsing, discovery, downloading, and execution."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from adc.engine import paths
from adc.engine.updater import (
    UpdateError,
    UpdateInfo,
    UpdateManager,
    find_installer_asset,
    is_newer_version,
    parse_version,
)

SAMPLE_RELEASE_JSON = {
    "tag_name": "v2.1.0",
    "name": "Disk CleanUp v2.1.0",
    "body": "## Release Notes v2.1.0\n- Faster scan engine\n- New UI features",
    "published_at": "2026-09-18T12:00:00Z",
    "html_url": "https://github.com/103PU/disk-cleaner/releases/tag/v2.1.0",
    "assets": [
        {
            "name": "DiskCleanUp-Setup-2.1.0-x64.exe",
            "browser_download_url": "https://example.com/download/DiskCleanUp-Setup-2.1.0-x64.exe",
            "size": 1024,
            "digest": "sha256:d4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35",
        },
        {
            "name": "DiskCleanUp-v2.1.0-portable.zip",
            "browser_download_url": "https://example.com/download/DiskCleanUp-v2.1.0-portable.zip",
            "size": 2048,
        },
    ],
}


def test_parse_version() -> None:
    assert parse_version("2.0.0") == (2, 0, 0)
    assert parse_version("v2.1.0") == (2, 1, 0)
    assert parse_version("v3.0.0-rc1") == (3, 0, 0, 1)
    assert parse_version("2.0.0.123") == (2, 0, 0, 123)
    assert parse_version("") == (0,)
    assert parse_version(None) == (0,)  # type: ignore[arg-type]


def test_is_newer_version() -> None:
    assert is_newer_version("v2.1.0", "2.0.0") is True
    assert is_newer_version("v2.0.1", "2.0.0") is True
    assert is_newer_version("v2.0.0.1", "2.0.0") is True
    assert is_newer_version("v2.0.0", "2.0.0") is False
    assert is_newer_version("v1.9.9", "2.0.0") is False


def test_find_installer_asset() -> None:
    assets = SAMPLE_RELEASE_JSON["assets"]
    asset, sha = find_installer_asset(assets)  # type: ignore[arg-type]

    assert asset is not None
    assert asset["name"] == "DiskCleanUp-Setup-2.1.0-x64.exe"
    assert sha == "d4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35"

    no_exe = [{"name": "readme.txt"}, {"name": "source.zip"}]
    asset_none, sha_none = find_installer_asset(no_exe)
    assert asset_none is None
    assert sha_none is None


def test_check_update_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.ENV_OVERRIDE, str(tmp_path))

    mgr = UpdateManager(repo="test/repo")

    body_bytes = json.dumps(SAMPLE_RELEASE_JSON).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body_bytes
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        info = mgr.check_update(current_version="2.0.0")

    assert info.available is True
    assert info.latest_version == "v2.1.0"
    assert info.current_version == "2.0.0"
    assert info.asset_name == "DiskCleanUp-Setup-2.1.0-x64.exe"
    assert info.asset_size == 1024
    assert info.sha256 == "d4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35"

    # Test caching: should return cached without calling urlopen
    with patch("urllib.request.urlopen", side_effect=AssertionError("should not be called")):
        cached = mgr.check_update(current_version="2.0.0")
        assert cached == info

    # Test force=True bypasses cache
    with patch("urllib.request.urlopen", return_value=mock_resp):
        fresh = mgr.check_update(force=True, current_version="2.0.0")
        assert fresh.latest_version == "v2.1.0"


def test_check_update_errors() -> None:
    mgr = UpdateManager(repo="test/repo")

    # 404
    http_404 = urllib.error.HTTPError("url", 404, "Not Found", {}, None)  # type: ignore[arg-type]
    with patch("urllib.request.urlopen", side_effect=http_404):
        with pytest.raises(UpdateError) as exc_info:
            mgr.check_update(force=True)
        assert exc_info.value.code == "no_release"

    # 403
    http_403 = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)  # type: ignore[arg-type]
    with patch("urllib.request.urlopen", side_effect=http_403):
        with pytest.raises(UpdateError) as exc_info:
            mgr.check_update(force=True)
        assert exc_info.value.code == "rate_limited"

    # URLError / Offline
    url_err = urllib.error.URLError("Network unreachable")
    with patch("urllib.request.urlopen", side_effect=url_err):
        with pytest.raises(UpdateError) as exc_info:
            mgr.check_update(force=True)
        assert exc_info.value.code == "offline"


def test_download_workflow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.ENV_OVERRIDE, str(tmp_path))

    mgr = UpdateManager(repo="test/repo")

    data = b"Installer Binary Content"
    import hashlib
    content_sha256 = hashlib.sha256(data).hexdigest()

    info = UpdateInfo(
        available=True,
        current_version="2.0.0",
        latest_version="v2.1.0",
        release_name="Disk CleanUp v2.1.0",
        release_notes="Notes",
        published_at="2026-09-18T12:00:00Z",
        asset_name="DiskCleanUp-Setup-2.1.0-x64.exe",
        asset_url="https://example.com/installer.exe",
        asset_size=len(data),
        sha256=content_sha256,
        html_url="https://example.com",
    )

    class MockStream:
        def __init__(self, raw: bytes) -> None:
            self._buf = io.BytesIO(raw)
            self.headers = {"Content-Length": str(len(raw))}

        def read(self, n: int = -1) -> bytes:
            return self._buf.read(n)

        def __enter__(self) -> MockStream:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    with patch("urllib.request.urlopen", return_value=MockStream(data)):
        prog = mgr.start_download(info)
        assert prog.status == "downloading"

        # Wait for worker thread to finish
        if mgr._worker_thread:
            mgr._worker_thread.join(timeout=5.0)

    final_prog = mgr.get_progress()
    assert final_prog.status == "completed"
    assert final_prog.pct == 100.0
    assert final_prog.target_path is not None
    assert Path(final_prog.target_path).is_file()
    assert Path(final_prog.target_path).read_bytes() == data


def test_download_sha_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.ENV_OVERRIDE, str(tmp_path))
    mgr = UpdateManager(repo="test/repo")

    data = b"Corrupted Data"
    info = UpdateInfo(
        available=True,
        current_version="2.0.0",
        latest_version="v2.1.0",
        release_name="v2.1.0",
        release_notes="Notes",
        published_at="now",
        asset_name="DiskCleanUp-Setup-2.1.0-x64.exe",
        asset_url="https://example.com/installer.exe",
        asset_size=len(data),
        sha256="0000000000000000000000000000000000000000000000000000000000000000",
        html_url="https://example.com",
    )

    class MockStream:
        def __init__(self, raw: bytes) -> None:
            self._buf = io.BytesIO(raw)
            self.headers = {"Content-Length": str(len(raw))}

        def read(self, n: int = -1) -> bytes:
            return self._buf.read(n)

        def __enter__(self) -> MockStream:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    with patch("urllib.request.urlopen", return_value=MockStream(data)):
        mgr.start_download(info)
        if mgr._worker_thread:
            mgr._worker_thread.join(timeout=5.0)

    final_prog = mgr.get_progress()
    assert final_prog.status == "failed"
    assert "SHA-256 mismatch" in (final_prog.error or "")


def test_launch_installer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.ENV_OVERRIDE, str(tmp_path))
    mgr = UpdateManager(repo="test/repo")

    # When no file exists
    with pytest.raises(UpdateError) as exc_info:
        mgr.launch_installer()
    assert exc_info.value.code == "no_installer_found"

    # When file exists
    up_dir = paths.ensure(paths.updates_dir())
    installer_file = up_dir / "DiskCleanUp-Setup-2.1.0-x64.exe"
    installer_file.write_bytes(b"dummy exe")

    with (
        patch("os.startfile", create=True) as mock_startfile,
        patch("subprocess.Popen") as mock_popen,
    ):
        import sys
        if sys.platform == "win32":
            launched = mgr.launch_installer(installer_file)
            assert launched is True
            mock_startfile.assert_called_once_with(str(installer_file))
        else:
            launched = mgr.launch_installer(installer_file)
            assert launched is True
            mock_popen.assert_called_once()
