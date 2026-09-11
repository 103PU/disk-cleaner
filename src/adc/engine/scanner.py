r"""Scan orchestration: catalogue rows in, measured rows out (SPEC 4.6-4.8).

The scan is the honest half of the app. It answers three questions per target and
keeps them apart, because collapsing them is what BUG-12 did:

* **Is it here?** -- the resolver's job. Absent is ``available=False`` with a
  reason, never 0 B.
* **Can it be measured?** -- ``measurable=False`` for hibernation, DISM component
  cleanup, the Recycle Bin API. Also not 0 B.
* **How big is it?** -- the walker's job, reporting size-on-disk and logical size
  separately whenever they diverge.

Nothing here deletes, and nothing here needs a :class:`~adc.engine.guard.Guard`:
a walk that follows no reparse point cannot leave the tree it was given. The
guard belongs to :mod:`adc.engine.cleaner`, one layer up, where paths turn into
deletes.

One deliberate inaccuracy, documented because the UI has to explain it: a target
with ``min_age_hours`` set is measured in full here, while the clean will skip
files younger than that. The scan is an estimate; ``clean_plan`` is the number to
trust.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from .audit import get as get_logger
from .fsutil import ScanCache, WalkResult, walk_size
from .jobs import CancelToken, Job, JobKind, JobRunner, Level, Phase
from .resolvers import Resolution, narrow_to_volumes
from .settings import Settings
from .targets import Target, find
from .volumes import volume_letter

ENV_NO_CACHE: Final = "ADC_NO_SCAN_CACHE"
ENV_BUDGET: Final = "ADC_SCAN_BUDGET"

# Per *path*, not per target: a Chromium row can hold 53 cache directories and a
# budget that stopped the whole target at the first slow one would report a
# fraction of it as if that were the total.
DEFAULT_PATH_BUDGET_S: Final = 90.0

PHASES: Final[tuple[Phase, ...]] = (
    Phase("resolve", "Xác định vị trí", "Locating"),
    Phase("measure", "Đo dung lượng", "Measuring"),
    Phase("summarise", "Tổng hợp", "Summarising"),
)

_log = get_logger("scan")


def cache_enabled() -> bool:
    """``ADC_NO_SCAN_CACHE=1`` switches the scan cache off (fsutil's contract)."""
    return os.environ.get(ENV_NO_CACHE, "").strip().lower() not in ("1", "true", "yes")


def budget_from_env(fallback: float | None) -> float | None:
    """``ADC_SCAN_BUDGET`` in seconds, else *fallback*. Zero means no budget."""
    raw = os.environ.get(ENV_BUDGET, "").strip()
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    return None if value <= 0 else value


@dataclass
class TargetScan:
    """One measured catalogue row.

    Carries no catalogue metadata beyond the id on purpose: ``job_poll`` returns
    these several times a second, and the bilingual names, descriptions and notes
    are constants the UI already has from one ``catalog()`` call. Shipping them
    again on every poll would be most of the payload.
    """

    target_id: str
    available: bool = False
    measurable: bool = True
    reason: str | None = None
    source: str = ""
    resolved_cached: bool = False
    path_count: int = 0
    size: int = 0
    logical: int = 0
    on_disk: int | None = None
    divergent: bool = False
    files: int = 0
    dirs: int = 0
    truncated: bool = False
    cached: bool = False
    denied_count: int = 0
    elapsed_s: float = 0.0
    by_volume: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def scanned(self) -> bool:
        """Available, measurable, and actually walked -- the rows that have a size."""
        return self.available and self.measurable and self.error is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "available": self.available,
            "measurable": self.measurable,
            "reason": self.reason,
            "source": self.source,
            "resolved_cached": self.resolved_cached,
            "path_count": self.path_count,
            "size": self.size,
            "logical": self.logical,
            "on_disk": self.on_disk,
            "divergent": self.divergent,
            "files": self.files,
            "dirs": self.dirs,
            "truncated": self.truncated,
            "cached": self.cached,
            "denied_count": self.denied_count,
            "elapsed_s": round(self.elapsed_s, 3),
            "by_volume": dict(self.by_volume),
            "error": self.error,
        }

    def absorb(self, walk: WalkResult) -> None:
        """Add one walked path's numbers to this row.

        Additive because one target is many paths: 53 Chromium directories, two
        Docker VHDX files, a Recycle Bin per volume. ``truncated`` and ``cached``
        are ORed rather than replaced -- if any path hit the budget, the row's
        total is a floor, and the UI must say so.
        """
        self.logical += walk.logical
        if walk.measured_on_disk:
            self.on_disk = (self.on_disk or 0) + walk.on_disk
        self.size += walk.size
        self.files += walk.files
        self.dirs += walk.dirs
        self.denied_count += walk.denied_count
        self.elapsed_s += walk.elapsed_s
        self.truncated = self.truncated or walk.truncated
        self.cached = self.cached or walk.cached
        self.divergent = self.divergent or walk.divergent
        letter = volume_letter(walk.root)
        if letter:
            self.by_volume[letter] = self.by_volume.get(letter, 0) + walk.size


class ScanRun:
    """The live result set for one scan job: the worker writes, the UI reads.

    A separate object rather than more state on :class:`~adc.engine.jobs.Job`
    because ``Job`` is deliberately generic -- it knows phases, events and
    before/after byte counts, and a scan row is none of those. The bridge holds
    one of these per job id and hands its rows to ``job_poll`` as
    ``partial_results``.

    Every read returns a copy under the lock, so the UI thread can never observe a
    row mid-``absorb``.
    """

    def __init__(self, job: Job, target_ids: Sequence[str]) -> None:
        self.job = job
        self.target_ids = tuple(target_ids)
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._rows: dict[str, TargetScan] = {}

    def row(self, target_id: str) -> TargetScan:
        with self._lock:
            found = self._rows.get(target_id)
            if found is None:
                found = TargetScan(target_id=target_id)
                self._rows[target_id] = found
            return found

    def rows(self) -> list[dict[str, Any]]:
        """Catalogue order is the caller's business; this is completion order."""
        with self._lock:
            return [row.as_dict() for row in self._rows.values()]

    def totals(self) -> dict[str, Any]:
        """The four numbers the summary bar shows, plus the per-volume split.

        ``total_size`` counts only rows that were actually walked. An unavailable
        row contributes nothing -- not zero-with-an-asterisk, nothing -- and the
        counts beside it are how the UI says why.
        """
        with self._lock:
            rows = list(self._rows.values())
        by_volume: dict[str, int] = {}
        for row in rows:
            for letter, size in row.by_volume.items():
                by_volume[letter] = by_volume.get(letter, 0) + size
        scanned = [row for row in rows if row.scanned]
        return {
            "total_size": sum(row.size for row in scanned),
            "scanned": len(scanned),
            "unavailable": sum(1 for row in rows if not row.available),
            "not_measurable": sum(1 for row in rows if row.available and not row.measurable),
            "failed": sum(1 for row in rows if row.error is not None),
            "truncated": any(row.truncated for row in scanned),
            "by_volume": by_volume,
            "requested": len(self.target_ids),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"rows": self.rows(), "totals": self.totals()}


def new_job() -> Job:
    """A SCAN job with this module's three phases already attached."""
    return Job(JobKind.SCAN, [Phase(p.key, p.label_vi, p.label_en) for p in PHASES])


def _resolve_row(
    run: ScanRun, target_id: str, *, settings: Settings
) -> tuple[Target | None, Resolution | None]:
    """Fill in the resolve half of one row. Returns what the measure half needs.

    An unknown id lands as ``error`` rather than an exception: the bridge already
    validates ids against the catalogue, so reaching here means the two got out of
    step -- a bug worth showing in the report, not worth losing the other 48 rows
    over.

    ``settings.volume_ids`` is applied here, on the resolved paths, rather than on
    the target: a row can span disks -- ``RecycleBins`` returns one path per fixed
    volume -- so "scan C: only" has to be able to keep half a row.
    """
    row = run.row(target_id)
    target = find(target_id)
    if target is None:
        row.error = f"unknown target id: {target_id}"
        run.job.log(
            Level.ERROR,
            f"Không có mục nào tên {target_id}",
            f"No such target: {target_id}",
            target_id=target_id,
        )
        return None, None

    try:
        found = target.resolve()
    except Exception as exc:  # a resolver is third-party-ish: a tool, a glob, a scandir
        row.error = f"resolve failed: {exc}"
        run.job.log(
            Level.ERROR,
            f"Không xác định được vị trí: {exc}",
            f"Could not locate: {exc}",
            target_id=target_id,
        )
        return None, None

    resolution = narrow_to_volumes(found, settings.volume_ids)
    dropped = len(found.paths) - len(resolution.paths)
    if dropped and resolution.available:
        run.job.log(
            Level.INFO,
            f"{target.name_vi}: bỏ qua {dropped} vị trí nằm ngoài các ổ đã chọn",
            f"{target.name_en}: skipped {dropped} path(s) outside the chosen volumes",
            target_id=target_id,
        )

    row.available = resolution.available
    row.measurable = resolution.measurable
    row.reason = resolution.reason
    row.source = resolution.source
    row.resolved_cached = resolution.cached
    row.path_count = len(resolution.paths)

    if not resolution.available:
        run.job.log(
            Level.INFO,
            f"Bỏ qua {target.name_vi}: {resolution.reason}",
            f"Skipping {target.name_en}: {resolution.reason}",
            target_id=target_id,
        )
    elif not resolution.measurable:
        run.job.log(
            Level.INFO,
            f"{target.name_vi}: có, nhưng không đo được bằng cách quét",
            f"{target.name_en}: present, but not measurable by walking",
            target_id=target_id,
        )
    return target, resolution


def _measure_row(
    run: ScanRun,
    target: Target,
    paths: Iterable[str],
    *,
    cancel: CancelToken,
    settings: Settings,
    cache: ScanCache | None,
) -> None:
    """Walk every path of one target into its row."""
    row = run.row(target.id)
    budget = budget_from_env(settings.scan_budget_s or DEFAULT_PATH_BUDGET_S)
    for path in paths:
        cancel.raise_if_cancelled()
        try:
            walk = walk_size(
                path,
                cancel=cancel,
                budget_s=budget,
                size_on_disk=settings.size_on_disk,
                cache=cache,
            )
        except OSError as exc:
            # walk_size swallows everything it finds *inside* the tree; this is
            # the root itself being unusable.
            row.error = f"{path}: {exc.strerror or exc}"
            run.job.log(
                Level.WARN,
                f"Không đọc được {path}: {exc.strerror or exc}",
                f"Unreadable: {path}: {exc.strerror or exc}",
                target_id=target.id,
            )
            continue
        row.absorb(walk)
    if row.truncated:
        run.job.log(
            Level.WARN,
            f"{target.name_vi}: hết thời gian quét, con số là mức tối thiểu",
            f"{target.name_en}: scan budget reached, the figure is a floor",
            target_id=target.id,
        )


def run_scan(
    run: ScanRun,
    *,
    settings: Settings | None = None,
    cache: ScanCache | None = None,
) -> ScanRun:
    """The worker body. Synchronous, so a test can call it without a thread.

    Raises :class:`~adc.engine.jobs.CancelledError` on cancel and lets it out:
    :class:`~adc.engine.jobs.JobRunner` is what turns that into a CANCELLED job,
    and swallowing it here would report a partial scan as a complete one.
    """
    active = settings or Settings()
    job = run.job
    cancel = job.cancel_token
    owns_cache = cache is None and cache_enabled()
    scan_cache = cache if cache is not None else (ScanCache() if owns_cache else None)

    try:
        job.enter_phase("resolve")
        pending: list[tuple[Target, Resolution]] = []
        total = max(1, len(run.target_ids))
        for index, target_id in enumerate(run.target_ids):
            cancel.raise_if_cancelled()
            target, resolution = _resolve_row(run, target_id, settings=active)
            if (
                target is not None
                and resolution is not None
                and resolution.available
                and resolution.measurable
                and resolution.paths
            ):
                pending.append((target, resolution))
            job.set_phase_progress((index + 1) / total)

        job.enter_phase("measure")
        measured = max(1, len(pending))
        for index, (target, resolution) in enumerate(pending):
            cancel.raise_if_cancelled()
            job.set_phase_progress(
                index / measured,
                detail_vi=f"Đang đo {target.name_vi}…",
                detail_en=f"Measuring {target.name_en}…",
            )
            _measure_row(
                run,
                target,
                resolution.paths,
                cancel=cancel,
                settings=active,
                cache=scan_cache,
            )
            job.set_phase_progress((index + 1) / measured)

        job.enter_phase("summarise")
        totals = run.totals()
        job.set_phase_progress(1.0)
        job.log(
            Level.SUCCESS,
            f"Quét xong: {totals['scanned']} mục có dữ liệu, "
            f"{totals['unavailable']} không có trên máy này.",
            f"Scan complete: {totals['scanned']} target(s) with data, "
            f"{totals['unavailable']} not present on this machine.",
        )
        _log.info(
            "scan %s: requested=%d scanned=%d unavailable=%d not_measurable=%d "
            "failed=%d total=%d truncated=%s",
            job.id,
            totals["requested"],
            totals["scanned"],
            totals["unavailable"],
            totals["not_measurable"],
            totals["failed"],
            totals["total_size"],
            totals["truncated"],
        )
    finally:
        if owns_cache and scan_cache is not None:
            scan_cache.close()
    return run


def submit_scan(
    runner: JobRunner,
    *,
    target_ids: Sequence[str],
    settings: Settings | None = None,
) -> ScanRun:
    """Start a scan on a worker thread and return the handle to poll.

    The caller keeps the :class:`ScanRun` against ``run.job.id``; ``job_poll``
    reads ``run.as_dict()`` for its ``partial_results``.
    """
    job = new_job()
    run = ScanRun(job, target_ids)
    _log.info("scan %s: %d target(s) requested", job.id, len(run.target_ids))
    runner.submit(job, lambda _job: run_scan(run, settings=settings))
    return run
