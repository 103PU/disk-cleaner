"""Where the app keeps its state on disk (docs/02-SPEC.md 3.2).

One module because three others need the same four directories and none of them
should be the owner of that decision: ``fsutil`` writes ``cache/scan.sqlite``,
``report`` writes ``reports/<job_id>.json``, the logger writes ``logs/``, and
``guard`` has to hard-block ADC's own install directory.

Everything is derived from the environment, never hardcoded, and every getter
takes an ``ADC_DATA_DIR`` override so a test can point the whole tree at a tmp
path without touching the real profile.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "DiskCleanUp"
ENV_OVERRIDE = "ADC_DATA_DIR"



def _override() -> Path | None:
    raw = os.environ.get(ENV_OVERRIDE)
    return Path(raw) if raw else None


def _base(env_var: str, fallback: str) -> Path:
    """``%APPDATA%``-style root, or a POSIX-ish fallback off ``~``.

    The fallback exists so the engine can be unit-tested on any host; it is not
    a supported deployment target.
    """
    forced = _override()
    if forced is not None:
        return forced
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw) / APP_DIR_NAME
    return Path.home() / fallback / APP_DIR_NAME


def roaming_dir() -> Path:
    """Settings that should follow the user between machines."""
    return _base("APPDATA", ".config")


def local_dir() -> Path:
    """Machine-local state: logs, reports, scan cache."""
    return _base("LOCALAPPDATA", ".local/share")


def config_file() -> Path:
    return roaming_dir() / "config.json"


def logs_dir() -> Path:
    return local_dir() / "logs"


def reports_dir() -> Path:
    return local_dir() / "reports"


def cache_dir() -> Path:
    return local_dir() / "cache"


def scan_db() -> Path:
    return cache_dir() / "scan.sqlite"


def ensure(directory: Path) -> Path:
    """mkdir -p, returning the directory so callers can inline the call."""
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def install_dir() -> Path:
    """The directory ADC itself runs from.

    ``guard`` refuses to delete anything inside it: an installed build lives in
    ``%ProgramFiles%``, but a portable copy could sit anywhere -- including
    inside a folder the user then asks us to clean.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # src/adc/engine/paths.py -> repo root
    return Path(__file__).resolve().parents[3]
