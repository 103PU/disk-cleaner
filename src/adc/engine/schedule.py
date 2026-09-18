"""Task Scheduler integration over COM (docs/02-SPEC.md 6.5 & docs/03-PLAN.md P5).

Creates, updates, queries and removes the Disk CleanUp automated maintenance task
using the Windows Task Scheduler 2.0 COM API (Schedule.Service) without shelling
out to schtasks.exe.

Key Invariants:
1. SAFE-tier only: Unattended runs MUST NEVER include CAUTION or DANGEROUS targets.
   Attempting to schedule any non-safe target or preset raises NonSafeTargetError.
2. Headless execution: Scheduled runs execute with `--headless`, write JSON audit
   reports, and display Windows toast notifications on completion.
3. Threshold gating: When configured with a free space threshold, the headless
   runner evaluates drive space and skips cleaning if free space is sufficient.
4. Clean uninstall: The task can be cleanly removed via schedule_delete() or installer.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Final

from .audit import get as get_logger
from .cleaner import new_job, run_clean
from .cleaner import plan as build_plan
from .paths import roaming_dir
from .settings import load as load_settings
from .targets import Risk, Target, find, schedulable
from .volumes import fixed_volumes

_log = get_logger("schedule")

TASK_NAME: Final[str] = "DiskCleanUp_AutoClean"
TASK_FOLDER: Final[str] = "\\"
TASK_DESCRIPTION: Final[str] = (
    "Disk CleanUp automated maintenance task. Periodically cleans safe temporary "
    "caches to reclaim disk space."
)


class NonSafeTargetError(ValueError):
    """Raised when a non-safe target or preset is submitted for scheduled execution.

    docs/03-PLAN.md:223: test_schedule_rejects_non_safe_targets -- adding a CAUTION
    or DANGEROUS target to an unattended schedule must raise.
    """


class Frequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    THRESHOLD = "threshold"


# Bitmask flags for Weekly triggers in Windows Task Scheduler:
# Sunday=1, Monday=2, Tuesday=4, Wednesday=8, Thursday=16, Friday=32, Saturday=64
DAYS_OF_WEEK_MAP: Final[dict[int, int]] = {
    0: 1,   # Sun
    1: 2,   # Mon
    2: 4,   # Tue
    3: 8,   # Wed
    4: 16,  # Thu
    5: 32,  # Fri
    6: 64,  # Sat
}


@dataclass(frozen=True)
class ScheduleConfig:
    """Configuration for automated cleaning."""

    enabled: bool = False
    frequency: str = "daily"
    time_of_day: str = "09:00"
    days_of_week: tuple[int, ...] = (1,)  # Monday default (1)
    threshold_pct: int = 15
    preset: str = "safe"
    targets: tuple[str, ...] = ()
    notify: bool = True

    FIELDS: ClassVar[tuple[str, ...]] = (
        "enabled",
        "frequency",
        "time_of_day",
        "days_of_week",
        "threshold_pct",
        "preset",
        "targets",
        "notify",
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "frequency": self.frequency,
            "time_of_day": self.time_of_day,
            "days_of_week": list(self.days_of_week),
            "threshold_pct": self.threshold_pct,
            "preset": self.preset,
            "targets": list(self.targets),
            "notify": self.notify,
        }

    @classmethod
    def from_dict(cls, raw: object) -> ScheduleConfig:
        if not isinstance(raw, dict):
            return cls()

        enabled = raw.get("enabled") is True
        freq = str(raw.get("frequency", "daily")).lower()
        if freq not in ("daily", "weekly", "threshold"):
            freq = "daily"

        time_str = str(raw.get("time_of_day", "09:00")).strip()
        if not _is_valid_time(time_str):
            time_str = "09:00"

        days_raw = raw.get("days_of_week")
        days: list[int] = []
        if isinstance(days_raw, list | tuple):
            for d in days_raw:
                if isinstance(d, int) and 0 <= d <= 6:
                    days.append(d)
        if not days:
            days = [1]  # Monday

        try:
            thresh = int(raw.get("threshold_pct", 15))
            thresh = max(1, min(90, thresh))
        except (TypeError, ValueError):
            thresh = 15

        preset = str(raw.get("preset", "safe")).strip().lower()
        if preset != "safe":
            preset = "safe"

        targets_raw = raw.get("targets")
        target_list: list[str] = []
        if isinstance(targets_raw, list | tuple):
            for t in targets_raw:
                if isinstance(t, str) and t.strip():
                    target_list.append(t.strip())

        notify = raw.get("notify") is not False

        return cls(
            enabled=enabled,
            frequency=freq,
            time_of_day=time_str,
            days_of_week=tuple(sorted(set(days))),
            threshold_pct=thresh,
            preset=preset,
            targets=tuple(target_list),
            notify=notify,
        )


@dataclass(frozen=True)
class ScheduleStatus:
    """Live status of the schedule combining config and Task Scheduler service state."""

    enabled: bool
    frequency: str
    time_of_day: str
    days_of_week: tuple[int, ...]
    threshold_pct: int
    preset: str
    targets: tuple[str, ...]
    notify: bool
    installed: bool
    task_name: str
    state: str
    last_run_time: str | None
    next_run_time: str | None
    last_result: int | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "frequency": self.frequency,
            "time_of_day": self.time_of_day,
            "days_of_week": list(self.days_of_week),
            "threshold_pct": self.threshold_pct,
            "preset": self.preset,
            "targets": list(self.targets),
            "notify": self.notify,
            "installed": self.installed,
            "task_name": self.task_name,
            "state": self.state,
            "last_run_time": self.last_run_time,
            "next_run_time": self.next_run_time,
            "last_result": self.last_result,
            "error": self.error,
        }


def _is_valid_time(s: str) -> bool:
    try:
        parts = s.split(":")
        if len(parts) != 2:
            return False
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, TypeError):
        return False


def schedule_file() -> Path:
    return roaming_dir() / "schedule.json"


def load_config() -> ScheduleConfig:
    path = schedule_file()
    if not path.is_file():
        return ScheduleConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ScheduleConfig.from_dict(data)
    except Exception as e:
        _log.warning("Failed to load schedule.json: %s", e)
        return ScheduleConfig()


def save_config(config: ScheduleConfig) -> None:
    path = schedule_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.as_dict(), indent=2), encoding="utf-8")


def validate_scheduled_targets(target_ids: Sequence[str]) -> tuple[Target, ...]:
    """Validate that every target is in the SAFE tier and schedulable unattended.

    SPEC 6.5 & PLAN P5:
    Attempting to schedule any non-SAFE or standalone target MUST raise NonSafeTargetError.
    """
    valid_map = {t.id: t for t in schedulable()}
    resolved: list[Target] = []
    for tid in target_ids:
        target = find(tid)
        if target is None:
            raise NonSafeTargetError(f"Unknown target id {tid!r}")
        if target.risk is not Risk.SAFE:
            raise NonSafeTargetError(
                f"Target {tid!r} has risk tier {target.risk.value.upper()!r}; "
                "only SAFE tier targets may be scheduled unattended (SPEC 6.5)."
            )
        if target.standalone:
            raise NonSafeTargetError(
                f"Target {tid!r} is a standalone operation and cannot be run unattended."
            )
        resolved.append(valid_map[tid])
    return tuple(resolved)


# ---------------------------------------------------------------------------
# Task Scheduler 2.0 COM Interface via pythonnet (Schedule.Service)
# ---------------------------------------------------------------------------

class TaskSchedulerClient:
    """Wrapper for Schedule.Service COM interface."""

    def __init__(self) -> None:
        self._connected = False
        self._service: Any = None
        self._root_folder: Any = None

    def connect(self) -> None:
        if self._connected:
            return
        if sys.platform != "win32":
            raise RuntimeError("Task Scheduler COM is only available on Windows")

        __import__("clr")
        sys_mod = __import__("System")
        Activator = sys_mod.Activator
        Type = sys_mod.Type
        refl_mod = __import__("System.Reflection", fromlist=["BindingFlags"])
        bf = refl_mod.BindingFlags
        self._BindingFlags = bf
        service_type = Type.GetTypeFromProgID("Schedule.Service")
        if service_type is None:
            raise RuntimeError("Schedule.Service COM class is not registered on this system")
        self._service = Activator.CreateInstance(service_type)
        service_type.InvokeMember("Connect", bf.InvokeMethod, None, self._service, None)
        self._root_folder = service_type.InvokeMember(
            "GetFolder", bf.InvokeMethod, None, self._service, [TASK_FOLDER]
        )
        self._connected = True

    def get_task(self, name: str = TASK_NAME) -> Any | None:
        self.connect()
        try:
            return self._root_folder.GetType().InvokeMember(
                "GetTask", self._BindingFlags.InvokeMethod, None, self._root_folder, [name]
            )
        except Exception:
            return None

    def delete_task(self, name: str = TASK_NAME) -> bool:
        self.connect()
        try:
            self._root_folder.GetType().InvokeMember(
                "DeleteTask", self._BindingFlags.InvokeMethod, None, self._root_folder, [name, 0]
            )
            return True
        except Exception as e:
            _log.info("Task %s deletion returned: %s", name, e)
            return False

    def run_task(self, name: str = TASK_NAME) -> bool:
        task = self.get_task(name)
        if task is None:
            return False
        try:
            task.GetType().InvokeMember(
                "Run", self._BindingFlags.InvokeMethod, None, task, [None]
            )
            return True
        except Exception as e:
            _log.error("Failed to run task %s: %s", name, e)
            return False

    def register_task(
        self,
        config: ScheduleConfig,
        name: str = TASK_NAME,
    ) -> Any:
        self.connect()
        bf = self._BindingFlags

        # New Task definition
        task_def = self._service.GetType().InvokeMember(
            "NewTask", bf.InvokeMethod, None, self._service, [0]
        )

        # Registration Info
        reg_info = task_def.GetType().InvokeMember(
            "RegistrationInfo", bf.GetProperty, None, task_def, None
        )
        reg_info.GetType().InvokeMember(
            "Description", bf.SetProperty, None, reg_info, [TASK_DESCRIPTION]
        )
        reg_info.GetType().InvokeMember(
            "Author", bf.SetProperty, None, reg_info, ["Disk CleanUp"]
        )

        # Triggers
        triggers = task_def.GetType().InvokeMember(
            "Triggers", bf.GetProperty, None, task_def, None
        )

        today_str = datetime.now().strftime("%Y-%m-%d")
        start_boundary = f"{today_str}T{config.time_of_day}:00"

        if config.frequency == Frequency.WEEKLY.value:
            # TASK_TRIGGER_WEEKLY = 3
            trig = triggers.GetType().InvokeMember("Create", bf.InvokeMethod, None, triggers, [3])
            trig.GetType().InvokeMember(
                "StartBoundary", bf.SetProperty, None, trig, [start_boundary]
            )
            trig.GetType().InvokeMember("WeeksInterval", bf.SetProperty, None, trig, [1])
            # DaysOfWeek bitmask
            mask = 0
            for d in config.days_of_week:
                mask |= DAYS_OF_WEEK_MAP.get(d, 0)
            if mask == 0:
                mask = 2  # Monday default
            trig.GetType().InvokeMember("DaysOfWeek", bf.SetProperty, None, trig, [mask])
        else:
            # Default or Daily / Threshold: TASK_TRIGGER_DAILY = 2
            trig = triggers.GetType().InvokeMember("Create", bf.InvokeMethod, None, triggers, [2])
            trig.GetType().InvokeMember(
                "StartBoundary", bf.SetProperty, None, trig, [start_boundary]
            )
            trig.GetType().InvokeMember("DaysInterval", bf.SetProperty, None, trig, [1])

        # Action: execute adc with --headless
        actions = task_def.GetType().InvokeMember(
            "Actions", bf.GetProperty, None, task_def, None
        )
        # TASK_ACTION_EXEC = 0
        exec_action = actions.GetType().InvokeMember(
            "Create", bf.InvokeMethod, None, actions, [0]
        )

        exe_path, args = _build_execution_command(config)
        exec_action.GetType().InvokeMember(
            "Path", bf.SetProperty, None, exec_action, [exe_path]
        )
        exec_action.GetType().InvokeMember(
            "Arguments", bf.SetProperty, None, exec_action, [args]
        )

        # Settings
        settings = task_def.GetType().InvokeMember(
            "Settings", bf.GetProperty, None, task_def, None
        )
        settings.GetType().InvokeMember(
            "DisallowStartIfOnBatteries", bf.SetProperty, None, settings, [False]
        )
        settings.GetType().InvokeMember(
            "StopIfGoingOnBatteries", bf.SetProperty, None, settings, [False]
        )
        settings.GetType().InvokeMember(
            "ExecutionTimeLimit", bf.SetProperty, None, settings, ["PT2H"]
        )
        settings.GetType().InvokeMember(
            "AllowDemandStart", bf.SetProperty, None, settings, [True]
        )
        settings.GetType().InvokeMember(
            "StartWhenAvailable", bf.SetProperty, None, settings, [True]
        )

        # Register: TASK_CREATE_OR_UPDATE = 6, TASK_LOGON_INTERACTIVE_TOKEN = 3
        return self._root_folder.GetType().InvokeMember(
            "RegisterTaskDefinition",
            bf.InvokeMethod,
            None,
            self._root_folder,
            [name, task_def, 6, None, None, 3, None],
        )


def _build_execution_command(config: ScheduleConfig) -> tuple[str, str]:
    """Construct (executable_path, arguments) for the unattended task."""
    args_list = ["--headless", "--preset=safe"]
    if config.frequency == Frequency.THRESHOLD.value or config.threshold_pct:
        args_list.append(f"--threshold-pct={config.threshold_pct}")

    if config.targets:
        args_list.append(f"--targets={','.join(config.targets)}")

    if getattr(sys, "frozen", False):
        exe_path = str(Path(sys.executable).resolve())
        return exe_path, " ".join(args_list)

    # In development mode, invoke python -m adc
    return sys.executable, f"-m adc {' '.join(args_list)}"


# Singleton COM client for process lifetime
_client: TaskSchedulerClient | None = None


def get_client() -> TaskSchedulerClient:
    global _client
    if _client is None:
        _client = TaskSchedulerClient()
    return _client


# ---------------------------------------------------------------------------
# Public Engine API
# ---------------------------------------------------------------------------

def get_schedule_status() -> ScheduleStatus:
    """Return live status of the scheduled maintenance task."""
    config = load_config()
    installed = False
    state_str = "not_configured"
    last_run: str | None = None
    next_run: str | None = None
    last_result: int | None = None
    err: str | None = None

    if sys.platform == "win32":
        try:
            client = get_client()
            task = client.get_task(TASK_NAME)
            if task is not None:
                installed = True
                bf = client._BindingFlags
                raw_enabled = task.GetType().InvokeMember(
                    "Enabled", bf.GetProperty, None, task, None
                )
                raw_state = task.GetType().InvokeMember(
                    "State", bf.GetProperty, None, task, None
                )
                raw_last_run = task.GetType().InvokeMember(
                    "LastRunTime", bf.GetProperty, None, task, None
                )
                raw_next_run = task.GetType().InvokeMember(
                    "NextRunTime", bf.GetProperty, None, task, None
                )
                raw_last_result = task.GetType().InvokeMember(
                    "LastTaskResult", bf.GetProperty, None, task, None
                )

                # State mapping: 0=Unknown, 1=Disabled, 2=Queued, 3=Ready, 4=Running
                state_map = {1: "disabled", 2: "queued", 3: "ready", 4: "running"}
                state_str = state_map.get(raw_state, "unknown")
                if not raw_enabled:
                    state_str = "disabled"

                last_run_str = str(raw_last_run) if raw_last_run is not None else None
                next_run_str = str(raw_next_run) if raw_next_run is not None else None

                # Windows default sentinel 1999-11-30 means "never run"
                if last_run_str and "1999" not in last_run_str:
                    last_run = last_run_str
                if next_run_str and "1999" not in next_run_str:
                    next_run = next_run_str

                last_result = int(raw_last_result) if raw_last_result is not None else None
        except Exception as e:
            _log.info("Could not read live Task Scheduler state: %s", e)
            err = str(e)

    return ScheduleStatus(
        enabled=config.enabled and (installed or sys.platform != "win32"),
        frequency=config.frequency,
        time_of_day=config.time_of_day,
        days_of_week=config.days_of_week,
        threshold_pct=config.threshold_pct,
        preset=config.preset,
        targets=config.targets,
        notify=config.notify,
        installed=installed,
        task_name=TASK_NAME,
        state=state_str,
        last_run_time=last_run,
        next_run_time=next_run,
        last_result=last_result,
        error=err,
    )


def set_schedule(config: ScheduleConfig) -> ScheduleStatus:
    """Register or update the scheduled task with strictly validated safe targets."""
    # 1. Enforce safety invariants
    if config.targets:
        validate_scheduled_targets(config.targets)
    if config.preset != "safe":
        raise NonSafeTargetError(
            f"Preset {config.preset!r} cannot be scheduled; only 'safe' preset allowed."
        )

    # 2. Persist configuration
    save_config(config)

    # 3. Apply to Windows Task Scheduler if enabled
    if sys.platform == "win32":
        client = get_client()
        if config.enabled:
            client.register_task(config, TASK_NAME)
            _log.info(
                "Registered scheduled task %s (%s at %s)",
                TASK_NAME,
                config.frequency,
                config.time_of_day,
            )
        else:
            client.delete_task(TASK_NAME)
            _log.info("Removed scheduled task %s because schedule was disabled", TASK_NAME)

    return get_schedule_status()


def delete_schedule() -> ScheduleStatus:
    """Delete the scheduled task and mark configuration disabled."""
    config = load_config()
    new_config = ScheduleConfig(
        enabled=False,
        frequency=config.frequency,
        time_of_day=config.time_of_day,
        days_of_week=config.days_of_week,
        threshold_pct=config.threshold_pct,
        preset="safe",
        targets=config.targets,
        notify=config.notify,
    )
    save_config(new_config)

    if sys.platform == "win32":
        try:
            get_client().delete_task(TASK_NAME)
            _log.info("Deleted scheduled task %s", TASK_NAME)
        except Exception as e:
            _log.warning("Failed to delete scheduled task %s: %s", TASK_NAME, e)

    return get_schedule_status()


def run_schedule_now() -> bool:
    """Trigger the scheduled task immediately."""
    if sys.platform == "win32":
        client = get_client()
        if client.run_task(TASK_NAME):
            return True

    # Fallback to in-process execution if task trigger not available
    config = load_config()
    code = run_headless(
        preset=config.preset,
        threshold_pct=None,  # Manual trigger ignores threshold
        target_ids=config.targets if config.targets else None,
    )
    return code == 0


# ---------------------------------------------------------------------------
# Toast Notification
# ---------------------------------------------------------------------------

def show_toast(title: str, message: str) -> None:
    """Show balloon toast notification via Windows Forms NotifyIcon."""
    if sys.platform != "win32":
        return
    try:
        clr = __import__("clr")
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        forms = __import__("System.Windows.Forms", fromlist=["NotifyIcon", "ToolTipIcon"])
        drawing = __import__("System.Drawing", fromlist=["SystemIcons"])
        NotifyIcon = forms.NotifyIcon
        ToolTipIcon = forms.ToolTipIcon
        SystemIcons = drawing.SystemIcons

        notify = NotifyIcon()
        notify.Icon = SystemIcons.Information
        notify.Visible = True
        notify.ShowBalloonTip(5000, title, message, ToolTipIcon.Info)

        def _cleanup() -> None:
            try:
                notify.Visible = False
                notify.Dispose()
            except Exception:  # noqa: S110
                pass

        threading.Timer(6.0, _cleanup).start()
    except Exception as e:
        _log.warning("Could not show toast notification: %s", e)


# ---------------------------------------------------------------------------
# Headless Execution Runner (--headless)
# ---------------------------------------------------------------------------

def run_headless(
    preset: str = "safe",
    threshold_pct: int | None = None,
    target_ids: Sequence[str] | None = None,
) -> int:
    """Unattended cleanup worker called by __main__.py on --headless invocation."""
    _log.info(
        "Starting unattended headless cleanup: preset=%s threshold=%s targets=%s",
        preset,
        threshold_pct,
        target_ids,
    )

    # 1. Evaluate threshold if requested
    if threshold_pct is not None and threshold_pct > 0:
        vols = fixed_volumes()
        # If no volume has free space percentage below threshold, skip
        needs_clean = any(v.free_pct < threshold_pct for v in vols)
        if not needs_clean:
            _log.info(
                "All fixed volumes have free space >= %d%%; skipping automated cleanup.",
                threshold_pct,
            )
            return 0

    # 2. Select and validate targets
    if target_ids:
        sched_targets = validate_scheduled_targets(target_ids)
        ids = [t.id for t in sched_targets]
    else:
        if preset != "safe":
            raise NonSafeTargetError(
                f"Preset {preset!r} is not permitted in unattended cleanup; only 'safe' allowed."
            )
        ids = [t.id for t in schedulable()]

    _log.info("Running unattended cleanup over %d safe targets", len(ids))

    # 3. Dry-run plan and execute
    settings = load_settings()
    plan = build_plan(ids, settings=settings, allow_dangerous=False)
    job = new_job()

    report_data = run_clean(plan, job, write=True)
    reclaimed = report_data.get("reclaimed_bytes", 0)
    files = report_data.get("files_deleted", 0)

    _log.info("Unattended cleanup complete: reclaimed %d bytes, deleted %d files", reclaimed, files)

    # 4. Display Toast notification
    config = load_config()
    if config.notify:
        mb = reclaimed / (1024 * 1024)
        size_str = f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"

        title = "Disk CleanUp"
        msg = f"Đã tự động thu hồi {size_str} từ {files:,} tệp."
        show_toast(title, msg)

    return 0
