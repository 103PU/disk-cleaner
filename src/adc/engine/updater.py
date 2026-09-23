"""In-app update notification, verification, and downloader.

Queries GitHub Releases API for Disk CleanUp releases, discovers installer assets,
downloads them in a background worker thread with progress tracking, verifies
SHA-256 checksums, and launches the Windows installer.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Final

from adc.engine import paths

_log = logging.getLogger(__name__)

APP_VERSION: Final = "2.2.0"

DEFAULT_REPO: Final = "103PU/disk-cleaner"
GITHUB_API_LATEST: Final = "https://api.github.com/repos/{repo}/releases/latest"
DEFAULT_TIMEOUT_S: Final = 10.0
CHUNK_SIZE: Final = 65536  # 64 KB


class UpdateError(Exception):
    """Bilingual update failure."""

    def __init__(self, code: str, vi: str, en: str) -> None:
        super().__init__(en)
        self.code = code
        self.vi = vi
        self.en = en


def parse_version(v: object) -> tuple[int, ...]:
    """Extract numeric components from a SemVer string (e.g. 'v2.1.0' -> (2, 1, 0))."""
    if not isinstance(v, str):
        return (0,)
    nums = [int(n) for n in re.findall(r"\d+", v)]
    return tuple(nums) if nums else (0,)


def is_newer_version(latest_tag: str, current_version: str) -> bool:
    """Return True if latest_tag represents a strictly newer version than current_version."""
    return parse_version(latest_tag) > parse_version(current_version)


@dataclasses.dataclass(frozen=True)
class UpdateInfo:
    """Metadata describing the latest release from GitHub."""

    available: bool
    current_version: str
    latest_version: str
    release_name: str
    release_notes: str
    published_at: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str | None
    html_url: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class DownloadProgress:
    """Live state of an update download."""

    status: str = "idle"  # idle | downloading | completed | failed | cancelled
    bytes_downloaded: int = 0
    total_bytes: int = 0
    pct: float = 0.0
    speed_bps: float = 0.0
    target_path: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def find_installer_asset(
    assets: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """Find the Windows Setup .exe asset and optional SHA256 checksum."""
    installer_asset: dict[str, Any] | None = None
    sha256_checksum: str | None = None

    # Find installer asset: prefer *Setup*.exe
    for asset in assets:
        name = str(asset.get("name", ""))
        if name.endswith(".exe"):
            if "setup" in name.lower():
                installer_asset = asset
                break
            if installer_asset is None:
                installer_asset = asset

    # Extract SHA-256 digest if present on asset object
    if installer_asset:
        digest = str(installer_asset.get("digest", ""))
        if digest.lower().startswith("sha256:"):
            sha256_checksum = digest.split(":", 1)[1].strip().lower()

    return installer_asset, sha256_checksum


class UpdateManager:
    """Thread-safe background updater and installer launcher."""

    def __init__(self, repo: str = DEFAULT_REPO) -> None:
        self._repo = repo
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._progress = DownloadProgress()
        self._cached_info: UpdateInfo | None = None
        self._last_checked: float = 0.0
        self._cache_ttl_s = 600.0  # 10 minutes

    @property
    def repo(self) -> str:
        return self._repo

    def check_update(
        self,
        *,
        force: bool = False,
        current_version: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> UpdateInfo:
        """Query GitHub Releases API and return UpdateInfo."""
        cur_ver = current_version or APP_VERSION
        now = time.monotonic()

        with self._lock:
            if (
                not force
                and self._cached_info is not None
                and (now - self._last_checked) < self._cache_ttl_s
            ):
                return self._cached_info

        url = GITHUB_API_LATEST.format(repo=self._repo)
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "User-Agent": f"DiskCleanUp/{cur_ver}",
                "Accept": "application/vnd.github.v3+json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read()
                data = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            _log.warning("updater: GitHub API returned HTTP %s for %s", exc.code, url)
            if exc.code == 404:
                raise UpdateError(
                    "no_release",
                    "Chưa có bản phát hành nào trên GitHub.",
                    "No releases found on GitHub.",
                ) from exc
            if exc.code == 403:
                raise UpdateError(
                    "rate_limited",
                    "Đã vượt giới hạn truy vấn GitHub API. Hãy thử lại sau.",
                    "GitHub API rate limit exceeded. Please try again later.",
                ) from exc
            raise UpdateError(
                "http_error",
                f"Lỗi kết nối máy chủ ({exc.code}).",
                f"Server error ({exc.code}).",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            _log.warning("updater: connection failed: %s", exc)
            raise UpdateError(
                "offline",
                "Không thể kết nối đến máy chủ cập nhật. Hãy kiểm tra kết nối mạng.",
                "Cannot connect to update server. Please check your network connection.",
            ) from exc

        tag_name = str(data.get("tag_name", ""))
        rel_name = str(data.get("name", "") or tag_name)
        body = str(data.get("body", ""))
        published_at = str(data.get("published_at", ""))
        html_url = str(data.get("html_url", ""))
        assets = data.get("assets", [])

        asset, sha256_val = find_installer_asset(assets)
        if asset is None:
            raise UpdateError(
                "no_installer_asset",
                f"Bản phát hành {tag_name} không chứa file cài đặt .exe phù hợp.",
                f"Release {tag_name} does not contain a suitable .exe installer.",
            )

        asset_name = str(asset.get("name", ""))
        asset_url = str(asset.get("browser_download_url", ""))
        asset_size = int(asset.get("size", 0))

        available = is_newer_version(tag_name, cur_ver)

        info = UpdateInfo(
            available=available,
            current_version=cur_ver,
            latest_version=tag_name,
            release_name=rel_name,
            release_notes=body,
            published_at=published_at,
            asset_name=asset_name,
            asset_url=asset_url,
            asset_size=asset_size,
            sha256=sha256_val,
            html_url=html_url,
        )

        with self._lock:
            self._cached_info = info
            self._last_checked = now

        return info

    def get_progress(self) -> DownloadProgress:
        """Return a snapshot of current download progress."""
        with self._lock:
            return dataclasses.replace(self._progress)

    def start_download(self, info: UpdateInfo | None = None) -> DownloadProgress:
        """Begin downloading the update asset in a background worker."""
        with self._lock:
            if (
                self._progress.status == "downloading"
                and self._worker_thread
                and self._worker_thread.is_alive()
            ):
                return dataclasses.replace(self._progress)

            target_info = info or self._cached_info
            if target_info is None:
                raise UpdateError(
                    "no_update_info",
                    "Chưa có thông tin bản cập nhật. Hãy kiểm tra cập nhật trước.",
                    "No update information available. Please check for updates first.",
                )

            self._cancel_event.clear()
            self._progress = DownloadProgress(
                status="downloading",
                bytes_downloaded=0,
                total_bytes=target_info.asset_size,
                pct=0.0,
                speed_bps=0.0,
                target_path=None,
                error=None,
            )

            thread = threading.Thread(
                target=self._download_worker,
                args=(target_info,),
                name="adc-updater-download",
                daemon=True,
            )
            self._worker_thread = thread
            thread.start()

            return dataclasses.replace(self._progress)

    def cancel_download(self) -> DownloadProgress:
        """Cancel an ongoing download."""
        self._cancel_event.set()
        with self._lock:
            if self._progress.status == "downloading":
                self._progress.status = "cancelled"
            return dataclasses.replace(self._progress)

    def _download_worker(self, info: UpdateInfo) -> None:
        """Worker thread executing the chunked HTTP download and verification."""
        dest_dir = paths.ensure(paths.updates_dir())
        final_path = dest_dir / info.asset_name
        part_path = dest_dir / f"{info.asset_name}.part"

        # If already fully downloaded and valid, skip re-download
        if (
            final_path.is_file()
            and final_path.stat().st_size == info.asset_size
            and (not info.sha256 or self._verify_sha256(final_path, info.sha256))
        ):
            _log.info("updater: already downloaded and verified: %s", final_path)
            with self._lock:
                self._progress = DownloadProgress(
                    status="completed",
                    bytes_downloaded=info.asset_size,
                    total_bytes=info.asset_size,
                    pct=100.0,
                    speed_bps=0.0,
                    target_path=str(final_path),
                )
            return

        # Clean old temporary file if present
        if part_path.is_file():
            with contextlib.suppress(OSError):
                part_path.unlink()

        req = urllib.request.Request(  # noqa: S310
            info.asset_url,
            headers={
                "User-Agent": f"DiskCleanUp/{APP_VERSION}",
                "Accept": "application/octet-stream",
            },
        )

        hasher = hashlib.sha256() if info.sha256 else None
        bytes_done = 0
        start_time = time.monotonic()
        last_calc_time = start_time
        bytes_at_last_calc = 0

        try:
            with (
                urllib.request.urlopen(req, timeout=30.0) as resp,  # noqa: S310
                open(part_path, "wb") as out_f,
            ):
                total_len = int(resp.headers.get("Content-Length", 0)) or info.asset_size
                with self._lock:
                    self._progress.total_bytes = total_len

                while not self._cancel_event.is_set():
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    bytes_done += len(chunk)
                    if hasher:
                        hasher.update(chunk)

                    now = time.monotonic()
                    dt = now - last_calc_time
                    if dt >= 0.4:
                        speed = (bytes_done - bytes_at_last_calc) / dt if dt > 0 else 0.0
                        pct = (
                            round((bytes_done / total_len) * 100, 1)
                            if total_len > 0
                            else 0.0
                        )
                        with self._lock:
                            self._progress.bytes_downloaded = bytes_done
                            self._progress.pct = min(100.0, pct)
                            self._progress.speed_bps = speed
                        last_calc_time = now
                        bytes_at_last_calc = bytes_done

            if self._cancel_event.is_set():
                _log.info("updater: download cancelled by user")
                if part_path.is_file():
                    with contextlib.suppress(OSError):
                        part_path.unlink()
                with self._lock:
                    self._progress = DownloadProgress(
                        status="cancelled",
                        error="Cancelled by user",
                    )
                return

            # Check SHA-256 if available
            if hasher and info.sha256:
                computed = hasher.hexdigest().lower()
                expected = info.sha256.lower()
                if computed != expected:
                    _log.error(
                        "updater: SHA-256 mismatch! computed=%s expected=%s",
                        computed,
                        expected,
                    )
                    if part_path.is_file():
                        with contextlib.suppress(OSError):
                            part_path.unlink()
                    with self._lock:
                        self._progress = DownloadProgress(
                            status="failed",
                            error=f"SHA-256 mismatch: {computed} != {expected}",
                        )
                    return

            # Atomically replace final file
            if final_path.is_file():
                with contextlib.suppress(OSError):
                    final_path.unlink()
            part_path.replace(final_path)

            # Clean up other old .exe files in updates directory
            self._cleanup_old_updates(keep_path=final_path)

            _log.info("updater: download completed successfully: %s", final_path)
            with self._lock:
                self._progress = DownloadProgress(
                    status="completed",
                    bytes_downloaded=bytes_done,
                    total_bytes=bytes_done,
                    pct=100.0,
                    speed_bps=0.0,
                    target_path=str(final_path),
                )

        except Exception as exc:
            _log.exception("updater: download worker failed")
            if part_path.is_file():
                with contextlib.suppress(OSError):
                    part_path.unlink()
            with self._lock:
                self._progress = DownloadProgress(
                    status="failed",
                    bytes_downloaded=bytes_done,
                    total_bytes=info.asset_size,
                    error=str(exc),
                )

    def _verify_sha256(self, path: Path, expected: str) -> bool:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                hasher.update(chunk)
        return hasher.hexdigest().lower() == expected.lower()

    def _cleanup_old_updates(self, keep_path: Path) -> None:
        """Remove older installer executables to conserve disk space."""
        updates = paths.updates_dir()
        if not updates.is_dir():
            return
        for entry in updates.glob("*.exe"):
            if entry.resolve() != keep_path.resolve():
                with contextlib.suppress(OSError):
                    entry.unlink()

    def launch_installer(self, installer_path: Path | str | None = None) -> bool:
        """Launch the downloaded Windows installer executable."""
        target: Path | None = None
        if installer_path:
            target = Path(installer_path)
        else:
            with self._lock:
                if self._progress.target_path:
                    target = Path(self._progress.target_path)

        if target is None or not target.is_file():
            # Try to find any .exe in updates_dir
            updates = paths.updates_dir()
            if updates.is_dir():
                exes = sorted(
                    updates.glob("*.exe"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if exes:
                    target = exes[0]

        if target is None or not target.is_file() or not target.name.endswith(".exe"):
            raise UpdateError(
                "no_installer_found",
                "Không tìm thấy file cài đặt nào để khởi chạy.",
                "No installer file found to launch.",
            )

        _log.info("updater: launching installer %s", target)
        if sys.platform == "win32":
            os.startfile(str(target))  # noqa: S606
        else:
            subprocess.Popen([str(target)])
        return True


_GLOBAL_MANAGER: UpdateManager | None = None
_GLOBAL_LOCK = threading.Lock()


def get_manager() -> UpdateManager:
    """Return the global UpdateManager singleton."""
    global _GLOBAL_MANAGER
    with _GLOBAL_LOCK:
        if _GLOBAL_MANAGER is None:
            _GLOBAL_MANAGER = UpdateManager()
        return _GLOBAL_MANAGER
