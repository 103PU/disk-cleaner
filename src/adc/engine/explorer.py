r"""One folder at a time: where the space actually went (SPEC 6.2).

The catalogue answers "is there anything to clean". It cannot answer the question
docs/01-AUDIT.md opens with -- C: sits at 9.2 % free and 49 known targets account
for a fraction of it, so what is the *rest*? This module answers that, by walking
one level at a time and letting the user drill.

Deliberately not a whole-tree scan held in memory. A level is one ``scandir`` of
the folder plus one :func:`~adc.engine.fsutil.walk_size` per child directory, and
that choice buys four things:

* memory is O(children of this folder), not O(directories on the disk) -- a full
  tree of C: on this machine is 300k-600k nodes;
* progress is honest -- the child count is known before any measuring starts, so
  the bar moves per child and names the one in flight, instead of crawling
  towards a total nobody can compute yet;
* cancel lands quickly, because it lands inside a child's walk where the audited
  walker already checks the token every entry;
* drilling is a new job on a new root, so no stale tree has to be invalidated --
  and walking back up is nearly free, since every child is a ``ScanCache`` hit.

Nothing here deletes, and like :mod:`adc.engine.scanner` nothing here needs a
:class:`~adc.engine.guard.Guard`: a walk that follows no reparse point cannot
leave the tree it was handed.

Two honesty rules the UI is built on:

* a reparse point is listed and never followed (BUG-01), as ``kind="link"`` with
  no size. A junction is not 0 B, it is somewhere else's bytes, and adding them
  here would double-count the target they point at;
* when a folder has more children than a level keeps, the ones left out are still
  counted in ``total_size`` and reported as ``omitted`` / ``other_size``. A
  top-40 that silently drops 900 folders is BUG-12's class of lie in a new medium.
"""

from __future__ import annotations

import os
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from .audit import get as get_logger
from .fsutil import (
    DIVERGENCE_THRESHOLD,
    ScanCache,
    WalkResult,
    allocated_size,
    is_reparse,
    walk_size,
)
from .jobs import CancelToken, Job, JobKind, JobRunner, Level, Phase
from .scanner import budget_from_env, cache_enabled
from .settings import Settings
from .volumes import volume_letter

# Rows one level hands to the UI, per kind. ``job_poll`` re-sends the level a few
# times a second, so this is a payload budget as much as a screen budget: the
# treemap is unreadable past a few dozen tiles and the table filters rather than
# pages. Everything past the cap is counted in ``other_size``, never dropped.
TOP_DIRS: Final = 40
TOP_FILES: Final = 40

# How many rows a level keeps in memory while it measures. A cache folder with a
# hundred thousand files must not become a hundred thousand row objects; pruning
# keeps the biggest, which is exactly what the level is ranked by.
MAX_TRACKED: Final = 4096

# Wall clock for the whole level, not per child: a folder with 200 subdirectories
# should give a partial answer in two minutes rather than a perfect one in twenty.
# ``ADC_SCAN_BUDGET`` and ``Settings.scan_budget_s`` both override it.
DEFAULT_LEVEL_BUDGET_S: Final = 120.0

PHASES: Final[tuple[Phase, ...]] = (
    Phase("list", "Liệt kê", "Listing"),
    Phase("measure", "Đo dung lượng", "Measuring"),
    Phase("rank", "Xếp hạng", "Ranking"),
)

_log = get_logger("explore")


def _node_id() -> str:
    r"""An opaque handle for one path.

    The renderer navigates by these and never by paths: the bridge keeps the
    id -> path table, and ``bridge.explore_start`` accepts no path from the page
    (SEC-02). Random rather than derived, because a *derivable* handle is a path in
    disguise -- a page that can compute the id of ``C:\Windows\System32`` can reach
    it, and not being able to is the whole point.

    ``uuid4`` and not ``secrets``: ``tests/test_layering.py`` allows ``uuid`` in the
    engine, and this is a lookup key inside one process, not a credential.
    """
    return uuid.uuid4().hex[:12]


def _by_size(row: ExploreRow) -> tuple[int, str]:
    """Biggest first, then by name so equal sizes do not shuffle between polls."""
    return (-row.size, row.name.lower())


@dataclass
class ExploreRow:
    """One child of the folder being explored.

    ``size`` is the number the level ranks and draws: on-disk when the settings ask
    for it and the platform could supply it, logical otherwise. ``logical`` and
    ``on_disk`` stay beside it so the UI can show both when they diverge, the same
    contract :class:`~adc.engine.scanner.TargetScan` carries.
    """

    name: str
    path: str
    kind: str  # "dir" | "file" | "link"
    node_id: str = field(default_factory=_node_id)
    size: int = 0
    logical: int = 0
    on_disk: int | None = None
    divergent: bool = False
    files: int = 0
    dirs: int = 0
    mtime: float | None = None
    truncated: bool = False
    cached: bool = False
    denied_count: int = 0
    error: str | None = None

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir"

    def absorb(self, walk: WalkResult) -> None:
        """Attach this child's walk. Assignment, not addition: one row, one walk.

        (:class:`~adc.engine.scanner.TargetScan` adds, because one catalogue target
        can be 53 paths. A folder is one path by construction.)
        """
        self.logical = walk.logical
        self.on_disk = walk.on_disk if walk.measured_on_disk else None
        self.size = walk.size
        self.files = walk.files
        self.dirs = walk.dirs
        self.truncated = walk.truncated
        self.cached = walk.cached
        self.divergent = walk.divergent
        self.denied_count = walk.denied_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "logical": self.logical,
            "on_disk": self.on_disk,
            "divergent": self.divergent,
            "files": self.files,
            "dirs": self.dirs,
            "mtime": self.mtime,
            "truncated": self.truncated,
            "cached": self.cached,
            "denied_count": self.denied_count,
            "error": self.error,
        }


@dataclass(frozen=True)
class Crumb:
    """One step of the breadcrumb: a label to draw and a handle to navigate by."""

    node_id: str
    label: str
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "label": self.label}


def crumbs_for(root: str) -> tuple[Crumb, ...]:
    r"""``C:\Users\PU`` -> ``C:\``, ``Users``, ``PU``, each with its own handle.

    The last crumb is the folder on screen; the one before it is what "up" means,
    which is why going up needs no new bridge method. A UNC root stops at
    ``\\server\share`` because ``os.path.dirname`` does -- the right floor, since
    there is nothing above a share to explore.
    """
    chain: list[str] = []
    current = os.path.abspath(root)
    while True:
        chain.append(current)
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent
    return tuple(
        Crumb(node_id=_node_id(), label=os.path.basename(path) or path, path=path)
        for path in reversed(chain)
    )


def resolve_root(raw: str | os.PathLike[str]) -> str:
    r"""Absolute, present, and a directory -- or an ``OSError`` naming which it isn't.

    ``os.stat`` follows links here on purpose. A junction the *user* pointed at is a
    place they meant to look, and refusing it would make ``C:\Users\All Users``
    unexplorable. What must never be followed is a link found *inside* a walk, and
    that rule belongs to :func:`~adc.engine.fsutil.walk_size`, not to this door.
    """
    path = os.path.abspath(os.fspath(raw))
    st = os.stat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(path)
    return path


class ExploreRun:
    """The live result set for one explore job: the worker writes, the UI reads.

    Same division of labour as :class:`~adc.engine.scanner.ScanRun`, and the same
    reason for existing separately from :class:`~adc.engine.jobs.Job`. Every read
    copies under the lock, so the UI thread cannot observe a row mid-:meth:`absorb`.

    Rows are added the moment they are listed, before any measuring: the folder
    list appears at once and the sizes fill in, which is also what makes a cancel
    half-way through still worth showing.
    """

    def __init__(self, job: Job, root: str, *, measured_on_disk: bool = True) -> None:
        self.job = job
        self.root = root
        self.crumbs = crumbs_for(root)
        self.started_at = time.time()
        self._measured_on_disk = measured_on_disk
        self._lock = threading.Lock()
        self._rows: list[ExploreRow] = []
        self._dirs = 0
        self._files = 0
        self._links = 0
        self._total_size = 0
        self._total_logical = 0
        self._measured = 0
        self._to_measure = 0
        self._denied: list[str] = []
        self._truncated = False
        self._error: str | None = None

    @property
    def node_id(self) -> str:
        """This level's own handle -- the last crumb."""
        return self.crumbs[-1].node_id

    def path_for(self, node_id: str) -> str | None:
        """The path a handle stands for, or ``None`` if this level never minted it.

        The run *is* the id -> path table. Nothing else has to be kept in sync, and
        the lifetime is exactly right: an id only ever reaches the page as part of a
        level, and that level lives as long as its job does.

        A row pruned by :meth:`_prune_locked` becomes unresolvable, which is
        harmless -- pruning drops the smallest rows and the page can only click what
        it was shown, which is the biggest. The bridge answers ``unknown_node``.
        """
        for crumb in self.crumbs:
            if crumb.node_id == node_id:
                return crumb.path
        with self._lock:
            for row in self._rows:
                if row.node_id == node_id:
                    return row.path
        return None

    def add(self, row: ExploreRow) -> ExploreRow:
        """Record one child with whatever size it already has.

        A file arrives complete -- its length was in the ``scandir`` stat. A
        directory arrives at 0 and grows when :meth:`measured` lands. Counting
        happens here rather than at rank time so that a child pruned for memory is
        still in the totals.
        """
        with self._lock:
            if row.kind == "dir":
                self._dirs += 1
            elif row.kind == "file":
                self._files += 1
            else:
                self._links += 1
            self._total_size += row.size
            self._total_logical += row.logical
            self._rows.append(row)
            if len(self._rows) > MAX_TRACKED * 2:
                self._prune_locked()
        return row

    def measured(self, row: ExploreRow, walk: WalkResult) -> None:
        """Attach a child directory's walk. The totals move by the difference.

        The row may already have been pruned out of ``_rows``; the size still lands
        in ``total_size`` and therefore in ``other_size``, which is the whole point
        of pruning by size rather than by arrival.
        """
        with self._lock:
            before_size, before_logical = row.size, row.logical
            row.absorb(walk)
            self._total_size += row.size - before_size
            self._total_logical += row.logical - before_logical
            self._measured += 1
            self._truncated = self._truncated or row.truncated

    def expect(self, count: int) -> None:
        """How many child directories the measure phase is about to walk."""
        with self._lock:
            self._to_measure = count

    def note_denied(self, what: str) -> None:
        with self._lock:
            self._denied.append(what)

    def mark_truncated(self) -> None:
        """The level ran out of budget: every total below is a floor."""
        with self._lock:
            self._truncated = True

    def fail(self, message: str) -> None:
        """The level's own folder could not be listed. Not a row error -- the level's."""
        with self._lock:
            self._error = message

    def _prune_locked(self) -> None:
        """Keep the biggest ``MAX_TRACKED`` rows; the rest live on in the totals.

        Called at twice the cap so the sort is amortised over ``MAX_TRACKED``
        insertions instead of running on every one.
        """
        self._rows.sort(key=_by_size)
        del self._rows[MAX_TRACKED:]

    def _top_locked(self) -> list[ExploreRow]:
        """Top-N directories and top-N non-directories, merged biggest-first.

        Links share the file cap rather than getting one of their own: they are
        sizeless, so they sort last and a folder full of big files will show none of
        them. That is fine -- ``totals()['links']`` still says how many there were,
        and the level's job is to explain size.
        """
        dirs = [row for row in self._rows if row.kind == "dir"]
        rest = [row for row in self._rows if row.kind != "dir"]
        dirs.sort(key=_by_size)
        rest.sort(key=_by_size)
        merged = dirs[:TOP_DIRS] + rest[:TOP_FILES]
        merged.sort(key=_by_size)
        return merged

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [row.as_dict() for row in self._top_locked()]

    def totals(self) -> dict[str, Any]:
        """The level's summary, including what it is *not* showing.

        ``other_size`` is derived (total minus shown) rather than accumulated, so it
        cannot drift out of step with the rows: whatever the caps leave out is in it
        by construction, and it can never double-count what is on screen.
        """
        with self._lock:
            shown = self._top_locked()
            shown_size = sum(row.size for row in shown)
            children = self._dirs + self._files + self._links
            return {
                "total_size": self._total_size,
                "total_logical": self._total_logical,
                "other_size": max(0, self._total_size - shown_size),
                "dirs": self._dirs,
                "files": self._files,
                "links": self._links,
                "children": children,
                "shown": len(shown),
                "omitted": max(0, children - len(shown)),
                "measured": self._measured,
                "to_measure": self._to_measure,
                "truncated": self._truncated,
                "measured_on_disk": self._measured_on_disk,
                "denied_count": len(self._denied),
                "denied": self._denied[:32],
                "elapsed_s": round(time.time() - self.started_at, 3),
                "error": self._error,
            }

    def as_dict(self) -> dict[str, Any]:
        """What ``job_poll`` ships as ``partial_results`` for an explore job.

        ``parent_id`` is ``None`` at a volume root, which is how the UI knows to
        disable "up" without having to parse a path it was never given.
        """
        crumbs = list(self.crumbs)
        return {
            "root": self.root,
            "name": crumbs[-1].label,
            "volume": volume_letter(self.root),
            "node_id": self.node_id,
            "parent_id": crumbs[-2].node_id if len(crumbs) > 1 else None,
            "crumbs": [crumb.as_dict() for crumb in crumbs],
            "rows": self.rows(),
            "totals": self.totals(),
        }


def new_job() -> Job:
    """An EXPLORE job with this module's three phases already attached."""
    return Job(JobKind.EXPLORE, [Phase(p.key, p.label_vi, p.label_en) for p in PHASES])


def _file_row(entry: os.DirEntry[str], st: os.stat_result, *, size_on_disk: bool) -> ExploreRow:
    """A file is complete from the ``scandir`` stat -- no walk, at most one syscall.

    Routing it through :func:`~adc.engine.fsutil.walk_size` would re-``stat`` every
    file in the folder; :func:`~adc.engine.fsutil.allocated_size` reuses the
    attributes we already hold and only calls Windows for the sparse and compressed
    ones. Same threshold as ``WalkResult.divergent`` so a row means the same thing
    here as it does on the Clean view.
    """
    attrs = getattr(st, "st_file_attributes", 0)
    logical = st.st_size
    on_disk = allocated_size(entry.path, attrs, logical) if size_on_disk else None
    row = ExploreRow(
        name=entry.name,
        path=entry.path,
        kind="file",
        logical=logical,
        on_disk=on_disk,
        size=logical if on_disk is None else on_disk,
        mtime=st.st_mtime,
        files=1,
    )
    if on_disk is not None and logical:
        row.divergent = abs(logical - on_disk) / logical > DIVERGENCE_THRESHOLD
    return row


def _list_children(run: ExploreRun, *, cancel: CancelToken, size_on_disk: bool) -> list[ExploreRow]:
    """One ``scandir`` of the level's folder. Returns the directories left to measure.

    Files and reparse points are finished here -- a file from its stat, a link
    because it is deliberately not measured at all -- so the measure phase only has
    directories to walk, and its "n of m" is a count the UI can trust.

    A child whose ``stat`` fails is recorded as denied rather than dropped: on C:
    that is ``System Volume Information`` and a handful of others, and a folder
    listing that quietly omits them looks complete when it isn't.
    """
    pending: list[ExploreRow] = []
    try:
        scandir = os.scandir(run.root)
    except OSError as exc:
        run.fail(f"{run.root}: {exc.strerror or exc}")
        return pending

    with scandir:
        while True:
            cancel.raise_if_cancelled()
            try:
                entry = next(scandir)
            except StopIteration:
                break
            except OSError as exc:
                # The iteration itself failed part-way; keep what we have.
                run.note_denied(f"{run.root}: {exc.strerror or exc}")
                break
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                run.note_denied(f"{entry.path}: {exc.strerror or exc}")
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            if is_reparse(entry, attrs):
                run.add(
                    ExploreRow(name=entry.name, path=entry.path, kind="link", mtime=st.st_mtime)
                )
            elif stat.S_ISDIR(st.st_mode):
                pending.append(
                    run.add(
                        ExploreRow(name=entry.name, path=entry.path, kind="dir", mtime=st.st_mtime)
                    )
                )
            else:
                run.add(_file_row(entry, st, size_on_disk=size_on_disk))
    return pending


def run_explore(
    run: ExploreRun,
    *,
    settings: Settings | None = None,
    cache: ScanCache | None = None,
) -> ExploreRun:
    """The worker body. Synchronous, so a test can call it without a thread.

    Lets :class:`~adc.engine.jobs.CancelledError` out for the same reason
    :func:`~adc.engine.scanner.run_scan` does: the runner is what marks a job
    CANCELLED, and swallowing it here would present a half-measured level as a
    finished one.

    Only two settings apply. ``size_on_disk`` and ``scan_budget_s`` are about
    measuring; ``exclusions``, ``min_age_hours`` and ``volume_ids`` are about
    cleaning, and this view deletes nothing -- filtering a diagnostic that exists to
    answer "what is using my disk" would be answering a different question.
    """
    active = settings or Settings()
    job = run.job
    cancel = job.cancel_token
    owns_cache = cache is None and cache_enabled()
    scan_cache = cache if cache is not None else (ScanCache() if owns_cache else None)
    budget = budget_from_env(active.scan_budget_s or DEFAULT_LEVEL_BUDGET_S)
    deadline = None if budget is None else time.monotonic() + budget

    try:
        job.enter_phase("list")
        pending = _list_children(run, cancel=cancel, size_on_disk=active.size_on_disk)
        run.expect(len(pending))
        job.set_phase_progress(1.0)
        totals = run.totals()
        if totals["error"]:
            job.log(
                Level.ERROR,
                f"Không đọc được thư mục: {totals['error']}",
                f"Could not list the folder: {totals['error']}",
            )
        else:
            job.log(
                Level.INFO,
                f"{totals['children']} mục trong {run.root}",
                f"{totals['children']} item(s) in {run.root}",
            )

        job.enter_phase("measure")
        total = max(1, len(pending))
        for index, row in enumerate(pending):
            cancel.raise_if_cancelled()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                run.mark_truncated()
                job.log(
                    Level.WARN,
                    f"Hết thời gian: còn {len(pending) - index} thư mục chưa đo",
                    f"Out of time: {len(pending) - index} folder(s) not measured",
                )
                break
            job.set_phase_progress(
                index / total,
                detail_vi=f"Đang đo {row.name}…",
                detail_en=f"Measuring {row.name}…",
            )
            try:
                walk = walk_size(
                    row.path,
                    cancel=cancel,
                    budget_s=remaining,
                    size_on_disk=active.size_on_disk,
                    cache=scan_cache,
                )
            except OSError as exc:
                # walk_size absorbs everything it meets *inside* the tree; this is
                # the child folder itself being unusable.
                row.error = f"{exc.strerror or exc}"
                run.note_denied(f"{row.path}: {exc.strerror or exc}")
                job.log(
                    Level.WARN,
                    f"Không đọc được {row.name}: {exc.strerror or exc}",
                    f"Unreadable: {row.name}: {exc.strerror or exc}",
                )
                continue
            run.measured(row, walk)
            job.set_phase_progress((index + 1) / total)

        job.enter_phase("rank")
        totals = run.totals()
        job.set_phase_progress(1.0)
        if totals["truncated"]:
            job.log(
                Level.WARN,
                "Chưa đo hết: các con số dưới đây là mức tối thiểu.",
                "Not fully measured: the figures below are a floor.",
            )
        job.log(
            Level.SUCCESS,
            f"Xong: {totals['dirs']} thư mục, {totals['files']} tệp"
            + (f", {totals['links']} liên kết" if totals["links"] else ""),
            f"Done: {totals['dirs']} folder(s), {totals['files']} file(s)"
            + (f", {totals['links']} link(s)" if totals["links"] else ""),
        )
        _log.info(
            "explore %s: root=%s dirs=%d files=%d links=%d measured=%d/%d "
            "total=%d truncated=%s denied=%d",
            job.id,
            run.root,
            totals["dirs"],
            totals["files"],
            totals["links"],
            totals["measured"],
            totals["to_measure"],
            totals["total_size"],
            totals["truncated"],
            totals["denied_count"],
        )
    finally:
        if owns_cache and scan_cache is not None:
            scan_cache.close()
    return run


def submit_explore(
    runner: JobRunner,
    *,
    root: str | os.PathLike[str],
    settings: Settings | None = None,
) -> ExploreRun:
    """Start one level on a worker thread and return the handle to poll.

    :func:`resolve_root` runs *before* the job exists, on purpose: a missing folder
    should be a synchronous error the caller can phrase, not a job that starts and
    immediately fails. The bridge maps the ``OSError`` to ``not_present``.
    """
    active = settings or Settings()
    run = ExploreRun(new_job(), resolve_root(root), measured_on_disk=active.size_on_disk)
    _log.info("explore %s: root=%s", run.job.id, run.root)
    runner.submit(run.job, lambda _job: run_explore(run, settings=active))
    return run
