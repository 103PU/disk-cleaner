"""Tests for automated maintenance schedule (Task Scheduler COM integration).

Covers docs/02-SPEC.md 6.5 & docs/03-PLAN.md P5 requirements:
- test_schedule_rejects_non_safe_targets: adding a CAUTION or DANGEROUS target
  raises NonSafeTargetError.
- Safe-tier only execution.
- Task configuration roundtrip and validation.
- Headless execution with threshold logic.
- Bridge integration and error reporting.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from adc.engine import schedule, targets
from adc.engine.schedule import (
    TASK_NAME,
    NonSafeTargetError,
    ScheduleConfig,
    ScheduleStatus,
    load_config,
    run_headless,
    save_config,
    set_schedule,
    validate_scheduled_targets,
)
from adc.shell.bridge import Bridge


def test_schedule_rejects_non_safe_targets() -> None:
    """PLAN P5 acceptance test: adding a CAUTION/DANGEROUS target to schedule must raise."""
    # Find a CAUTION target in the catalog
    caution_targets = [t for t in targets.catalog() if t.risk is targets.Risk.CAUTION]
    assert len(caution_targets) > 0, "expected at least one CAUTION target"
    caution_id = caution_targets[0].id

    with pytest.raises(NonSafeTargetError) as exc_info:
        validate_scheduled_targets([caution_id])
    assert "SAFE" in str(exc_info.value)
    assert caution_id in str(exc_info.value)

    # Find a DANGEROUS target in the catalog
    dangerous_targets = [t for t in targets.catalog() if t.risk is targets.Risk.DANGEROUS]
    assert len(dangerous_targets) > 0, "expected at least one DANGEROUS target"
    dangerous_id = dangerous_targets[0].id

    with pytest.raises(NonSafeTargetError) as exc_info2:
        validate_scheduled_targets([dangerous_id])
    assert "SAFE" in str(exc_info2.value)


def test_schedule_rejects_unknown_target() -> None:
    with pytest.raises(NonSafeTargetError) as exc_info:
        validate_scheduled_targets(["non_existent_target_id_12345"])
    assert "Unknown target id" in str(exc_info.value)


def test_schedule_rejects_non_safe_preset() -> None:
    config = ScheduleConfig(enabled=True, preset="deep")
    with pytest.raises(NonSafeTargetError):
        set_schedule(config)


def test_schedule_accepts_safe_targets() -> None:
    safe = [t.id for t in targets.schedulable()]
    assert len(safe) > 0
    # First 3 safe targets
    sample = safe[:3]
    resolved = validate_scheduled_targets(sample)
    assert [t.id for t in resolved] == sample


def test_schedule_config_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADC_DATA_DIR", str(tmp_path))

    cfg = ScheduleConfig(
        enabled=True,
        frequency="weekly",
        time_of_day="14:30",
        days_of_week=(1, 3, 5),
        threshold_pct=20,
        preset="safe",
        targets=("npm_cache", "pip_cache"),
        notify=False,
    )
    save_config(cfg)
    loaded = load_config()

    assert loaded.enabled is True
    assert loaded.frequency == "weekly"
    assert loaded.time_of_day == "14:30"
    assert loaded.days_of_week == (1, 3, 5)
    assert loaded.threshold_pct == 20
    assert loaded.targets == ("npm_cache", "pip_cache")
    assert loaded.notify is False


def test_schedule_config_clamping_and_defaults() -> None:
    raw = {
        "enabled": "not_a_bool",
        "frequency": "invalid_freq",
        "time_of_day": "99:99",
        "days_of_week": [-1, 10, "bad"],
        "threshold_pct": 999,
        "preset": "deep",
        "targets": ["pip_cache", None, ""],
    }
    cfg = ScheduleConfig.from_dict(raw)
    assert cfg.enabled is False
    assert cfg.frequency == "daily"
    assert cfg.time_of_day == "09:00"
    assert cfg.days_of_week == (1,)
    assert cfg.threshold_pct == 90  # clamped to 90
    assert cfg.preset == "safe"
    assert cfg.targets == ("pip_cache",)


def test_schedule_status_serialization() -> None:
    status = ScheduleStatus(
        enabled=True,
        frequency="daily",
        time_of_day="09:00",
        days_of_week=(1,),
        threshold_pct=15,
        preset="safe",
        targets=("pip_cache",),
        notify=True,
        installed=True,
        task_name=TASK_NAME,
        state="ready",
        last_run_time="2026-09-18 10:00:00",
        next_run_time="2026-09-19 09:00:00",
        last_result=0,
        error=None,
    )
    d = status.as_dict()
    assert d["enabled"] is True
    assert d["state"] == "ready"
    assert d["task_name"] == TASK_NAME
    assert d["last_result"] == 0


def test_headless_skips_when_free_space_sufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mock fixed_volumes so all volumes report 50% free
    fake_vol = MagicMock(free_pct=50)
    monkeypatch.setattr("adc.engine.schedule.fixed_volumes", lambda: [fake_vol])

    # Threshold is 20%, free is 50% -> should skip and return 0
    exit_code = run_headless(preset="safe", threshold_pct=20)
    assert exit_code == 0


def test_headless_runs_when_free_space_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADC_DATA_DIR", str(tmp_path))
    # Mock fixed_volumes so one volume reports 8% free
    fake_vol = MagicMock(free_pct=8)
    monkeypatch.setattr("adc.engine.schedule.fixed_volumes", lambda: [fake_vol])

    # Mock cleaner.run_clean to avoid touching real files
    fake_report = {"reclaimed_bytes": 1024 * 1024 * 10, "files_deleted": 42}
    monkeypatch.setattr("adc.engine.schedule.run_clean", lambda plan, job, write=True: fake_report)
    monkeypatch.setattr("adc.engine.schedule.show_toast", lambda title, msg: None)

    exit_code = run_headless(preset="safe", threshold_pct=15)
    assert exit_code == 0


def test_bridge_schedule_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADC_DATA_DIR", str(tmp_path))
    bridge = Bridge()

    # 1. schedule_get
    get_res = bridge.schedule_get()
    assert get_res["ok"] is True
    assert "status" in get_res["data"]

    # 2. schedule_set with non-safe target -> bridge error
    caution_id = next(t.id for t in targets.catalog() if t.risk is targets.Risk.CAUTION)
    bad_res = bridge.schedule_set({"enabled": True, "targets": [caution_id]})
    assert bad_res["ok"] is False
    assert bad_res["error"]["code"] == "schedule_unsafe_target"

    # 3. schedule_set with safe target -> success
    good_res = bridge.schedule_set({
        "enabled": False,  # disabled so it doesn't try to register real COM task during unit test
        "frequency": "daily",
        "time_of_day": "10:00",
        "targets": ["pip_cache"],
    })
    assert good_res["ok"] is True
    assert good_res["data"]["status"]["time_of_day"] == "10:00"
    assert good_res["data"]["status"]["targets"] == ["pip_cache"]

    # 4. schedule_delete
    del_res = bridge.schedule_delete()
    assert del_res["ok"] is True
    assert del_res["data"]["status"]["enabled"] is False


@pytest.mark.windows_only
def test_windows_com_task_registration_and_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test actual Task Scheduler registration on Windows host."""
    if sys.platform != "win32":
        pytest.skip("Windows only")

    test_task_name = "DiskCleanUp_PytestScheduleTest"
    monkeypatch.setenv("ADC_DATA_DIR", str(tmp_path))

    client = schedule.get_client()
    config = ScheduleConfig(
        enabled=True,
        frequency="daily",
        time_of_day="23:45",
        days_of_week=(1,),
        preset="safe",
    )

    try:
        # Register test task
        client.register_task(config, name=test_task_name)
        task = client.get_task(name=test_task_name)
        assert task is not None, "Task should exist in Task Scheduler"

        bf = client._BindingFlags
        enabled = task.GetType().InvokeMember("Enabled", bf.GetProperty, None, task, None)
        assert enabled is True
    finally:
        # Clean up test task
        deleted = client.delete_task(name=test_task_name)
        assert deleted is True
        task_after = client.get_task(name=test_task_name)
        assert task_after is None
