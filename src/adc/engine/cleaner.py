r"""Plan then clean: the dry run, the token, and the only code that deletes.

Two ideas carry the whole module.

**The plan is the promise, the clean is the receipt.** ``plan()`` runs every
selected target's strategy with ``dry_run=True``, which walks exactly what a real
clean would touch -- through the same guard, the same exclusions, the same age
filter -- and reports what it would remove. ``execute()`` then measures each
target before and after and reports what actually went. The two numbers are
allowed to differ, and when they do the report says so; that gap is honest,
whereas v1's single estimated number was not (BUG-12).

**Nothing is deleted without a token that a dry run minted.** ``clean_execute``
takes a token and nothing else: no target list, no paths, no flags. A selection
the user never previewed therefore cannot be executed, and neither can a
selection that was previewed and then edited -- the edit mints a new plan and the
old token is refused. Tokens are single-use and expire.

The re-resolve in :func:`execute` is deliberate. A plan carries the paths its dry
run measured, but the clean asks the catalogue again: between preview and confirm
a directory can vanish, and a junction can appear where one did not exist. Paths
that are ten minutes old are evidence for an estimate, never an authority to
delete.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from .audit import get as get_logger
from .fsutil import walk_size
from .guard import Guard, GuardError
from .jobs import (
    CancelledError,
    CancelToken,
    Job,
    JobKind,
    JobRunner,
    JobState,
    Level,
    Phase,
)
from .platform_win import is_user_an_admin
from .report import build_report, prune_reports, snapshot_volumes, write_report
from .resolvers import Resolution, narrow_to_volumes
from .settings import DEFAULTS as DEFAULT_SETTINGS
from .settings import Settings
from .settings import load as load_settings
from .strategies import CleanContext, StrategyResult
from .targets import Risk, Target, find

# A preview the user walked away from must not stay spendable. Ten minutes is
# long enough to read a list of fifty rows and short enough that the machine has
# not meaningfully changed.
PLAN_TTL_S: Final = 600.0

# The dry run is a real enumeration, and ``clean_plan`` is a synchronous bridge
# call. This bounds it: a target whose sweep runs out of budget is reported as an
# incomplete estimate rather than hanging the window.
PLAN_BUDGET_S: Final = 120.0

# Per target, for the before/after measurements. Bounded because a clean must not
# stall on measuring, and deliberately *not* the job's own cancel token: a cancel
# arriving mid-clean must still let the target that already ran be measured, or
# the receipt would be missing the work that was actually done.
MEASURE_BUDGET_S: Final = 60.0

# Enough to see what a row means without turning the payload into a file listing.
SAMPLE_PATHS: Final = 8

MAX_LIVE_PLANS: Final = 8

PHASES: Final[tuple[Phase, ...]] = (
    Phase("prepare", "Chuẩn bị", "Preparing"),
    Phase("clean", "Đang dọn", "Cleaning"),
    Phase("verify", "Đo lại", "Verifying"),
)

_log = get_logger("clean")


@dataclass
class PlanItem:
    """One target's dry-run result.

    ``paths`` stays in the process: it is what the estimate was measured over and
    what the audit report records. ``as_dict`` ships a bounded sample instead, so
    the preview can be inspected without the payload becoming a file listing.
    """

    target_id: str
    risk: str
    strategy_kind: str
    reversible: bool
    admin_required: bool
    min_age_hours: int
    paths: tuple[str, ...] = ()
    est_bytes: int = 0
    files: int = 0
    dirs: int = 0
    skipped_recent: int = 0
    files_locked: int = 0
    denied: int = 0
    locked_by: tuple[str, ...] = ()
    # False by default so that a row which never ran cannot claim its zero is a
    # measured size; ``_item_from`` sets it True only when the dry run produced
    # bytes it is willing to stand behind.
    measurable: bool = False
    will_run: bool = False
    skipped_reason: str | None = None
    truncated: bool = False
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "risk": self.risk,
            "strategy": self.strategy_kind,
            "reversible": self.reversible,
            "admin_required": self.admin_required,
            "min_age_hours": self.min_age_hours,
            "path_count": len(self.paths),
            "sample_paths": list(self.paths[:SAMPLE_PATHS]),
            "est_bytes": self.est_bytes,
            "files": self.files,
            "dirs": self.dirs,
            "skipped_recent": self.skipped_recent,
            "files_locked": self.files_locked,
            "denied": self.denied,
            "locked_by": list(self.locked_by),
            "measurable": self.measurable,
            "will_run": self.will_run,
            "skipped_reason": self.skipped_reason,
            "truncated": self.truncated,
            "notes": list(self.notes),
        }


class PlanError(Exception):
    """A token that cannot be spent. The bridge turns this into ``{ok: false}``."""


class UnknownPlanError(PlanError):
    pass


class ExpiredPlanError(PlanError):
    pass


class SpentPlanError(PlanError):
    pass


@dataclass
class Plan:
    """A dry run, its token, and the settings it was measured under.

    The ``settings`` snapshot is part of the promise. Exclusions,
    ``min_age_hours`` and ``volume_ids`` decide what a clean touches, so a plan
    measured under one set of settings must not be executed under another -- the
    number the user approved would no longer describe what happens. Executing uses
    this copy and ignores whatever the config file says by the time the button is
    pressed.
    """

    token: str
    created_at: float
    items: tuple[PlanItem, ...] = ()
    settings: Settings = DEFAULT_SETTINGS
    allow_dangerous: bool = False
    spent_at: float | None = None

    @property
    def runnable(self) -> tuple[PlanItem, ...]:
        return tuple(item for item in self.items if item.will_run)

    @property
    def est_total(self) -> int:
        """Only what will actually run. A skipped row's estimate is not a promise."""
        return sum(item.est_bytes for item in self.runnable)

    @property
    def needs_admin(self) -> bool:
        return any(item.admin_required for item in self.runnable)

    @property
    def has_irreversible(self) -> bool:
        return any(not item.reversible for item in self.runnable)

    @property
    def dangerous_ids(self) -> tuple[str, ...]:
        return tuple(i.target_id for i in self.runnable if i.risk == Risk.DANGEROUS.value)

    @property
    def truncated(self) -> bool:
        return any(item.truncated for item in self.items)

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created_at > PLAN_TTL_S

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "created_at": self.created_at,
            "expires_at": self.created_at + PLAN_TTL_S,
            "items": [item.as_dict() for item in self.items],
            "target_ids": [i.target_id for i in self.runnable],
            "est_total": self.est_total,
            "count": len(self.runnable),
            "skipped": len(self.items) - len(self.runnable),
            "needs_admin": self.needs_admin,
            "is_admin": is_user_an_admin(),
            "has_irreversible": self.has_irreversible,
            "dangerous_ids": list(self.dangerous_ids),
            "truncated": self.truncated,
            "min_age_hours": self.settings.min_age_hours,
            "exclusion_count": len(self.settings.exclusions),
        }


class PlanStore:
    """The live plans, keyed by token. Single-use, TTL-bounded, capacity-bounded.

    A dict with a lock rather than anything cleverer, because the access pattern
    is one write per preview and one read per confirm. The cap exists so that a UI
    bug that mints a plan per keystroke cannot grow this without limit; the oldest
    plan is dropped, which at worst costs the user a second preview.
    """

    def __init__(self, capacity: int = MAX_LIVE_PLANS) -> None:
        self.capacity = capacity
        self._lock = threading.Lock()
        self._plans: dict[str, Plan] = {}

    def put(self, plan: Plan) -> None:
        with self._lock:
            self._purge_locked()
            while len(self._plans) >= self.capacity:
                oldest = min(self._plans.values(), key=lambda p: p.created_at)
                del self._plans[oldest.token]
            self._plans[plan.token] = plan

    def peek(self, token: object) -> Plan | None:
        if not isinstance(token, str):
            return None
        with self._lock:
            return self._plans.get(token)

    def spend(self, token: object) -> Plan:
        """Redeem a token exactly once. Every failure mode is a distinct error.

        Distinct because the UI says different things: an unknown token is a bug
        or a restarted engine, an expired one asks for a fresh preview, and a
        spent one means the clean already started -- and a double-click on the
        confirm button must not run a clean twice.
        """
        if not isinstance(token, str) or not token:
            raise UnknownPlanError("no plan token")
        now = time.time()
        with self._lock:
            found = self._plans.get(token)
            if found is None:
                raise UnknownPlanError("no such plan; run a dry run first")
            if found.spent_at is not None:
                raise SpentPlanError("this plan has already been executed")
            if found.expired(now):
                del self._plans[token]
                raise ExpiredPlanError("the preview has expired; run a dry run again")
            found.spent_at = now
            return found

    def _purge_locked(self) -> None:
        now = time.time()
        for token in [t for t, p in self._plans.items() if p.expired(now)]:
            del self._plans[token]

    def clear(self) -> None:
        with self._lock:
            self._plans.clear()


_store = PlanStore()


def plan_store() -> PlanStore:
    """The process-wide store, mirroring ``resolvers.tool_cache()``."""
    return _store


def min_age_for(target: Target, settings: Settings) -> int:
    """The stricter of the row's own floor and the user's global one.

    ``max`` rather than "settings wins", because a setting is only ever allowed to
    narrow what gets deleted (see :mod:`adc.engine.settings`). A user asking for
    "nothing younger than 24 hours" must not thereby *lower* the 72-hour floor the
    catalogue put on crash dumps.
    """
    return max(0, target.min_age_hours, settings.min_age_hours)


def _new_item(target: Target, settings: Settings, **kwargs: Any) -> PlanItem:
    return PlanItem(
        target_id=target.id,
        risk=target.risk.value,
        strategy_kind=target.strategy.kind,
        reversible=target.reversible,
        admin_required=target.admin_required,
        min_age_hours=min_age_for(target, settings),
        **kwargs,
    )


def _preflight(target: Target, settings: Settings, *, allow_dangerous: bool) -> str | None:
    """Why this row will not run, decided before anything is enumerated.

    Cheap refusals first: a standalone row is not part of a bulk clean at all, a
    DANGEROUS row needs the confirmation the UI is responsible for collecting, and
    an admin-only row without elevation would fail per-file with access denied
    instead of saying the one useful thing.
    """
    if target.standalone:
        return "handled on its own screen, not in a bulk clean"
    if target.risk is Risk.DANGEROUS and not allow_dangerous:
        return "dangerous: needs an explicit confirmation"
    if target.admin_required and not is_user_an_admin():
        return "requires Administrator"
    return None


# A root that is legal to construct, impossible to reach, and certain not to
# exist: a direct child of the system volume, so it lies inside none of the
# guard's protected subtrees, named so that nothing else will ever be there.
_NULL_ROOT: Final = "$adc-null-guard$"


def _null_guard(settings: Settings) -> Guard:
    r"""A guard for a target that has no paths at all.

    ``hibernation``, ``component_store`` and ``vss_manage`` resolve to *available
    but not measurable*: the operation exists, there is nothing to walk, and their
    strategies never touch :meth:`Guard.check`. :class:`CleanContext` still
    requires a guard, and the right one to hand it is one that permits nothing --
    if such a strategy ever did check a real path, refusing it is the safe answer.
    """
    drive = os.environ.get("SYSTEMDRIVE", "C:")
    return Guard(roots=(os.path.join(drive + os.sep, _NULL_ROOT),),
                 exclusions=settings.exclusions)


def _guard_for(resolution: Resolution, settings: Settings) -> Guard:
    if resolution.paths:
        return Guard(roots=resolution.paths, exclusions=settings.exclusions)
    return _null_guard(settings)


def _item_from(
    target: Target,
    settings: Settings,
    resolution: Resolution,
    result: StrategyResult,
    *,
    truncated: bool,
) -> PlanItem:
    """Turn one dry-run :class:`StrategyResult` into the row the preview shows.

    Two judgements live here.

    *Will it run?* A dry run that produced any evidence -- a forecast count, a
    byte figure, or a note describing the command it would run -- will run. One
    that produced nothing and reported no error has nothing to do, and saying so
    is more useful than offering a row that would be a no-op.

    *Is the estimate a size?* ``measurable`` is False whenever the dry run came
    back with no bytes, which covers both cases where a number would be a lie: a
    tool command or a native operation, which cannot be forecast at all, and a
    sweep that held files back for an exclusion or the age filter, where
    :mod:`adc.engine.strategies` deliberately credits zero rather than over-report.
    """
    units = result.files_deleted + result.dirs_deleted
    evidence = units > 0 or result.bytes_deleted > 0 or bool(result.notes)
    reason = result.skipped_reason
    if reason is None and not result.ok:
        reason = "the dry run reported a failure"
    if reason is None and not evidence:
        reason = "nothing to remove"
    return _new_item(
        target,
        settings,
        paths=resolution.paths,
        est_bytes=result.bytes_deleted,
        files=result.files_deleted,
        dirs=result.dirs_deleted,
        skipped_recent=result.skipped_recent,
        denied=result.denied,
        locked_by=tuple(result.locked_by[:16]),
        measurable=result.bytes_deleted > 0,
        will_run=reason is None,
        skipped_reason=reason,
        truncated=truncated,
        notes=tuple(result.notes[:16]),
    )


def _dry_run_item(
    target: Target,
    settings: Settings,
    *,
    cancel: CancelToken,
    allow_dangerous: bool,
) -> PlanItem:
    """One target's dry run. Never raises except on a real user cancel.

    The two ways out of a :class:`~adc.engine.jobs.CancelledError` are the point:
    ``cancel.cancelled`` means the user pressed stop and the whole preview should
    stop with them, while a bare ``expired`` means the budget ran out and this row
    is an incomplete estimate -- which is a fact to report, not a reason to lose
    the other forty-nine rows.
    """
    reason = _preflight(target, settings, allow_dangerous=allow_dangerous)
    if reason is not None:
        return _new_item(target, settings, skipped_reason=reason)
    try:
        resolution = narrow_to_volumes(target.resolve(), settings.volume_ids)
    except Exception as exc:  # a resolver runs a tool, a glob, a scandir
        return _new_item(target, settings, skipped_reason=f"could not locate: {exc}")
    if not resolution.available:
        return _new_item(target, settings, skipped_reason=resolution.reason or "not present")
    try:
        guard = _guard_for(resolution, settings)
    except (GuardError, ValueError) as exc:
        # A catalogue root the guard refuses is a bug in the catalogue, and the
        # plan is exactly where it should become visible.
        return _new_item(target, settings, skipped_reason=f"refused by the guard: {exc}")

    context = CleanContext(
        target_id=target.id,
        paths=resolution.paths,
        guard=guard,
        cancel=cancel,
        dry_run=True,
        min_age_hours=min_age_for(target, settings),
    )
    truncated = False
    try:
        result = target.strategy.execute(context)
    except CancelledError:
        if cancel.cancelled:
            raise
        truncated = True
        result = StrategyResult(dry_run=True, notes=["the preview ran out of time"])
    except GuardError as exc:
        return _new_item(target, settings, skipped_reason=f"refused by the guard: {exc}")
    except OSError as exc:
        return _new_item(target, settings, skipped_reason=str(exc.strerror or exc))
    return _item_from(target, settings, resolution, result, truncated=truncated)


def _unknown_item(target_id: str) -> PlanItem:
    """A requested id that is not in the catalogue: shown, never silently dropped."""
    return PlanItem(
        target_id=target_id,
        risk="unknown",
        strategy_kind="none",
        reversible=True,
        admin_required=False,
        min_age_hours=0,
        skipped_reason="unknown target id",
    )


def plan(
    target_ids: Sequence[object],
    *,
    settings: Settings | None = None,
    allow_dangerous: bool = False,
    budget_s: float | None = PLAN_BUDGET_S,
    store: PlanStore | None = None,
) -> Plan:
    """Dry-run the selection, mint a token, return the preview.

    *target_ids* is typed loosely because the caller is the bridge and the value
    came from JavaScript: a number, a null or a nested list in the array is
    dropped rather than trusted. Ids are the only thing accepted from outside --
    never a path (SEC-02).

    Synchronous: SPEC 3.1 has ``clean_plan(selection) -> Plan`` returning the plan
    itself rather than a job id, and :data:`PLAN_BUDGET_S` is what makes that safe
    to call from the bridge. A preview that runs out of budget comes back with the
    slow rows marked ``truncated`` rather than not coming back.
    """
    active = settings if settings is not None else load_settings()
    cancel = CancelToken(budget_s)
    items: list[PlanItem] = []
    seen: set[str] = set()
    requested: Sequence[object] = [target_ids] if isinstance(target_ids, str) else target_ids
    for raw in requested:
        if not isinstance(raw, str):
            continue
        target_id = raw.strip()
        if not target_id or target_id in seen:
            continue
        seen.add(target_id)
        target = find(target_id)
        if target is None:
            items.append(_unknown_item(target_id))
            continue
        items.append(
            _dry_run_item(target, active, cancel=cancel, allow_dangerous=allow_dangerous)
        )
    made = Plan(
        token=uuid.uuid4().hex,
        created_at=time.time(),
        items=tuple(items),
        settings=active,
        allow_dangerous=allow_dangerous,
    )
    (store or plan_store()).put(made)
    _log.info(
        "plan %s: %d selected, %d will run, est=%d bytes, admin=%s, dangerous=%s",
        made.token[:8],
        len(made.items),
        len(made.runnable),
        made.est_total,
        made.needs_admin,
        ",".join(made.dangerous_ids) or "-",
    )
    return made


def new_job() -> Job:
    """A CLEAN job with this module's three phases already attached."""
    return Job(JobKind.CLEAN, [Phase(p.key, p.label_vi, p.label_en) for p in PHASES])


def _measure(paths: Sequence[str], settings: Settings) -> int:
    """Size of every path of one target, right now. Best effort, bounded.

    A path that has just been deleted is not an error -- it is the expected state
    of the "after" measurement for most targets -- so a missing root contributes
    nothing and no warning. Never through the scan cache: a cached size is exactly
    the wrong answer for a directory we are about to empty.
    """
    total = 0
    cancel = CancelToken(MEASURE_BUDGET_S)
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            walk = walk_size(
                path,
                cancel=cancel,
                budget_s=None,
                size_on_disk=settings.size_on_disk,
                cache=None,
            )
        except OSError:
            continue
        total += walk.size
    return total


def _clean_one(job: Job, item: PlanItem, settings: Settings) -> None:
    r"""Clean one target: re-resolve, measure, delete, measure again.

    The re-resolve is the safety property. ``item.paths`` is what the dry run
    measured minutes ago; this asks the catalogue what is there *now* and builds
    the guard from that answer, so a directory that vanished is skipped and a
    junction that appeared since the preview is refused by a guard that knows
    about it. Nothing from the plan payload is ever used as a path to delete.

    ``allow_dangerous=True`` on the re-check because the plan already carries that
    decision -- a DANGEROUS row only became runnable if the confirmation happened
    -- while the standalone and Administrator checks are re-run because they are
    cheap and because being wrong about them means deleting the wrong thing or
    failing per file.
    """
    outcome = job.outcome(item.target_id)
    target = find(item.target_id)
    if target is None:
        outcome.skipped_reason = "unknown target id"
        return

    def emit(level: Level, message_vi: str, message_en: str) -> None:
        job.log(level, message_vi, message_en, target_id=target.id)

    reason = _preflight(target, settings, allow_dangerous=True)
    resolution: Resolution | None = None
    if reason is None:
        try:
            resolution = narrow_to_volumes(target.resolve(), settings.volume_ids)
        except Exception as exc:  # a resolver runs a tool, a glob, a scandir
            reason = f"could not locate: {exc}"
        if resolution is not None and not resolution.available:
            reason = resolution.reason or "not present"

    if reason is not None or resolution is None:
        outcome.skipped_reason = reason or "not present"
        emit(Level.WARN, f"Bỏ qua {target.name_vi}: {outcome.skipped_reason}",
             f"Skipped {target.name_en}: {outcome.skipped_reason}")
        return

    try:
        guard = _guard_for(resolution, settings)
    except (GuardError, ValueError) as exc:
        outcome.skipped_reason = f"refused by the guard: {exc}"
        emit(Level.ERROR, f"Guard từ chối {target.name_vi}: {exc}",
             f"The guard refused {target.name_en}: {exc}")
        return

    context = CleanContext(
        target_id=target.id,
        paths=resolution.paths,
        guard=guard,
        cancel=job.cancel_token,
        dry_run=False,
        min_age_hours=min_age_for(target, settings),
        log=emit,
    )
    outcome.before = _measure(resolution.paths, settings)
    try:
        result = target.strategy.execute(context)
    except CancelledError:
        # The receipt must still say what happened before the stop.
        outcome.after = _measure(resolution.paths, settings)
        outcome.skipped_reason = outcome.skipped_reason or "cancelled part-way"
        raise
    except GuardError as exc:
        # An escape aborts the target rather than skipping a path (SPEC 4.7).
        outcome.after = _measure(resolution.paths, settings)
        outcome.skipped_reason = f"refused by the guard: {exc}"
        emit(Level.ERROR, f"Dừng {target.name_vi}: {exc}",
             f"Aborted {target.name_en}: {exc}")
        return
    except OSError as exc:
        outcome.after = _measure(resolution.paths, settings)
        outcome.skipped_reason = str(exc.strerror or exc)
        emit(Level.WARN, f"Lỗi khi dọn {target.name_vi}: {outcome.skipped_reason}",
             f"Error cleaning {target.name_en}: {outcome.skipped_reason}")
        return

    result.apply_to(outcome)
    outcome.after = _measure(resolution.paths, settings)
    if result.ok:
        emit(Level.SUCCESS,
             f"{target.name_vi}: thu hồi {outcome.reclaimed:,} B, "
             f"{outcome.files_deleted:,} tệp",
             f"{target.name_en}: reclaimed {outcome.reclaimed:,} B "
             f"from {outcome.files_deleted:,} file(s)")
    else:
        emit(Level.WARN, f"{target.name_vi}: {result.skipped_reason}",
             f"{target.name_en}: {result.skipped_reason}")


def _notes_for(approved: Plan) -> dict[str, Any]:
    """What the report keeps about the promise, so the gap can be audited later.

    The full path list goes in here and nowhere else: the report is the audit
    trail, and "which directories did it empty" is the first question anyone asks
    of a cleaner after the fact.
    """
    return {
        "plan_token": approved.token,
        "est_total": approved.est_total,
        "planned": len(approved.runnable),
        "plan_truncated": approved.truncated,
        "allow_dangerous": approved.allow_dangerous,
        "min_age_hours": approved.settings.min_age_hours,
        "exclusions": list(approved.settings.exclusions),
        "size_on_disk": approved.settings.size_on_disk,
        "per_target_estimate": {
            item.target_id: {
                "est_bytes": item.est_bytes,
                "measurable": item.measurable,
                "paths": list(item.paths),
            }
            for item in approved.runnable
        },
        "skipped_at_plan": {
            item.target_id: item.skipped_reason
            for item in approved.items
            if not item.will_run
        },
    }


def _finish_and_write(
    job: Job,
    approved: Plan,
    *,
    free_before: dict[str, int],
    state: JobState,
    error: str | None,
    write: bool,
) -> dict[str, Any]:
    """Close the job, take the second volume snapshot, build and store the report.

    The job is finished here rather than left to :class:`JobRunner` so that the
    report records a terminal state and a real duration; the runner then sees a
    job that is no longer RUNNING and leaves it alone.
    """
    job.enter_phase("verify")
    free_after = snapshot_volumes()
    job.set_phase_progress(1.0)
    if job.state is JobState.RUNNING:
        job.finish(state, error=error)
    body = build_report(
        job,
        free_before=free_before,
        free_after=free_after,
        selection=[item.target_id for item in approved.runnable],
        notes=_notes_for(approved),
    )
    if not write:
        return body
    try:
        path = write_report(body)
    except OSError as exc:
        # A report that cannot be written is not worth losing the run over.
        _log.warning("clean %s: report not written: %s", job.id, exc)
        return body
    body["report_path"] = str(path)
    _log.info(
        "clean %s: state=%s reclaimed=%d free_delta=%d est=%d report=%s",
        job.id,
        body["state"],
        body["reclaimed_total"],
        body["free_delta_total"],
        approved.est_total,
        path.name,
    )
    with contextlib.suppress(OSError):
        prune_reports()
    return body


def run_clean(approved: Plan, job: Job, *, write: bool = True) -> dict[str, Any]:
    """The worker body: clean every runnable row, then write the report.

    Synchronous, so a test can call it without a thread. The report is built in a
    ``finally`` and therefore survives a cancel and a crash alike -- a clean that
    deleted two hundred thousand files and then failed on the two hundred
    thousand and first must still leave a receipt for what it did.

    Returns the report body, which is also what the bridge hands the summary view.
    """
    settings = approved.settings
    items = approved.runnable
    free_before = snapshot_volumes()
    state = JobState.DONE
    error: str | None = None
    try:
        job.enter_phase("prepare")
        job.log(
            Level.INFO,
            f"Sẽ dọn {len(items)} mục, dự kiến {approved.est_total:,} B.",
            f"Cleaning {len(items)} target(s), estimated {approved.est_total:,} B.",
        )
        job.set_phase_progress(1.0)

        job.enter_phase("clean")
        total = max(1, len(items))
        for index, item in enumerate(items):
            job.cancel_token.raise_if_cancelled()
            name = find(item.target_id)
            job.set_phase_progress(
                index / total,
                detail_vi=f"Đang dọn {name.name_vi if name else item.target_id}…",
                detail_en=f"Cleaning {name.name_en if name else item.target_id}…",
            )
            _clean_one(job, item, settings)
            job.set_phase_progress((index + 1) / total)
    except CancelledError:
        state = JobState.CANCELLED
        raise
    except Exception as exc:
        state, error = JobState.FAILED, str(exc)
        raise
    finally:
        body = _finish_and_write(
            job, approved, free_before=free_before, state=state, error=error, write=write
        )
    return body


def submit_clean(runner: JobRunner, *, token: object, store: PlanStore | None = None) -> Job:
    """Redeem a plan token and start the clean. The only way to delete anything.

    Takes a runner and a token, and nothing else. There is deliberately no
    parameter here for a target list, a path or a flag: everything that decides
    what gets deleted was fixed when the dry run was taken, and that is what makes
    the preview a promise instead of a suggestion.

    The busy check comes before the token is spent. Otherwise a click that lands
    while a scan is still finishing would burn the token on a job that never
    started, and the user would have to preview all over again to find out why.
    """
    active = runner.active()
    if active is not None and active.state is JobState.RUNNING:
        raise RuntimeError(f"job {active.id} is still running")
    approved = (store or plan_store()).spend(token)
    job = new_job()
    _log.info(
        "clean %s: plan %s, %d target(s), est=%d bytes",
        job.id,
        approved.token[:8],
        len(approved.runnable),
        approved.est_total,
    )
    runner.submit(job, lambda started: run_clean(approved, started))
    return job
