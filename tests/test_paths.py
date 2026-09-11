"""State-location tests (docs/02-SPEC.md 3.2).

Small module, but everything the app writes lands where it says, and one of the
directories it computes is hard-blocked by ``guard``. The override is what keeps
the rest of the suite out of the user's real profile, so it gets checked here
rather than being assumed by every other test file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from adc.engine import paths


def test_roaming_and_local_are_derived_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(paths.ENV_OVERRIDE, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    assert paths.roaming_dir() == tmp_path / "Roaming" / paths.APP_DIR_NAME
    assert paths.local_dir() == tmp_path / "Local" / paths.APP_DIR_NAME


def test_the_four_locations_sit_under_local_except_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SPEC 3.2: config roams, logs/reports/cache stay on the machine."""
    monkeypatch.setenv(paths.ENV_OVERRIDE, str(tmp_path))

    assert paths.config_file() == tmp_path / "config.json"
    assert paths.logs_dir() == tmp_path / "logs"
    assert paths.reports_dir() == tmp_path / "reports"
    assert paths.cache_dir() == tmp_path / "cache"
    assert paths.scan_db() == tmp_path / "cache" / "scan.sqlite"


def test_override_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ADC_DATA_DIR`` wins, and does not append the app name a second time."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "ignored"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "ignored"))
    monkeypatch.setenv(paths.ENV_OVERRIDE, str(tmp_path / "forced"))

    assert paths.roaming_dir() == tmp_path / "forced"
    assert paths.local_dir() == tmp_path / "forced"
    assert paths.APP_DIR_NAME not in str(paths.local_dir())


def test_empty_override_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unset-but-present env var must not resolve the tree to the cwd."""
    monkeypatch.setenv(paths.ENV_OVERRIDE, "")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    assert paths.local_dir() == tmp_path / "Local" / paths.APP_DIR_NAME


def test_falls_back_under_home_when_the_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """So the engine imports and unit-tests on a host with no %LOCALAPPDATA%."""
    monkeypatch.delenv(paths.ENV_OVERRIDE, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    result = paths.local_dir()

    assert Path.home() in result.parents
    assert result.name == paths.APP_DIR_NAME


def test_ensure_creates_the_whole_chain_and_is_idempotent(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"

    assert paths.ensure(deep) == deep
    assert deep.is_dir()
    assert paths.ensure(deep) == deep, "second call must not raise"


def test_real_local_dir_is_under_localappdata() -> None:
    """Checked against the actual profile, without writing anything to it."""
    if os.name != "nt":
        pytest.skip("Windows profile layout")

    resolved = str(paths.local_dir())

    assert resolved.startswith(os.environ["LOCALAPPDATA"])
    assert resolved.endswith(paths.APP_DIR_NAME)


def test_install_dir_is_the_repo_root_when_not_frozen(repo_root: Path) -> None:
    assert paths.install_dir() == repo_root
    assert (paths.install_dir() / "pyproject.toml").is_file()


def test_install_dir_follows_the_executable_when_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The packaged build has no ``src/`` tree to walk up from.

    ``guard`` hard-blocks this directory, so getting it wrong in a PyInstaller
    bundle would leave the installed app deletable by itself.
    """
    fake = tmp_path / "app" / "AntigravityDiskCleaner.exe"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake))

    assert paths.install_dir() == fake.parent
