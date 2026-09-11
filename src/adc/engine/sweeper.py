r"""Orphan build junk under a root the user picks (SPEC 6.3).

The catalogue is the right shape for ``%LOCALAPPDATA%\npm-cache``: one path, always
in the same place, always safe. It is the wrong shape for the tens of gigabytes a
dev machine keeps in ``node_modules`` directories, because those live wherever the
projects live and no fixed list can name them in advance. This module walks a root
the user chooses and reports what it finds there.

Three rules decide every row, and all three are refusals.

**A name is not enough.** ``bin`` is a build directory beside a ``.csproj`` and a
folder of checked-in scripts anywhere else, so an artifact directory is listed only
when the manifest that regenerates it sits next to it (:data:`RULES`). A name with
no manifest requirement -- ``node_modules``, ``__pycache__`` -- is one that only a
package installer or a compiler ever creates.

**The date that matters belongs to the project, not to the directory.**
``node_modules`` is written once by ``npm install`` and never touched again, so its
own mtime is months old in every project including the one open in the editor right
now; filtering on it would offer up every project on the disk. What is asked instead
is when anything *else* under the project last changed, and a project that changed
inside the window is not listed at all, in any category (docs/03-PLAN.md: "project
sửa trong 30 ngày không bị liệt kê"). ``__pycache__`` regenerates itself and would
be harmless to offer anyway; it is held back with the rest, because a screen that
lists a live project teaches the user to stop reading the paths.

**Nothing is ticked, and nothing is deleted without a token a dry run minted.**
:func:`plan_delete` re-enumerates the chosen rows through
:class:`~adc.engine.strategies.HardDelete` and mints one single-use token;
:func:`run_delete` spends it. The same handshake as :mod:`adc.engine.cleaner`, whose
three token errors are raised here on purpose -- one vocabulary for "preview again"
in the bridge and in the UI, not two.

Deleting is irreversible here, deliberately. ``HardDelete`` and not
``RecycleDelete``: a 2 GB ``node_modules`` moved to the Recycle Bin frees nothing
until the bin is emptied, which is the opposite of what this screen is for, and the
shell API cannot take the 300-character paths inside one. What makes that trade
acceptable is that every path listed is regenerable by a command the project itself
documents.

Not a size heuristic and not a disk scan: only the names in :data:`RULES` are ever
listed, a matched directory is never descended into, and a reparse point is never
followed (BUG-01).
"""

from __future__ import annotations

import contextlib
import os
import stat
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from .audit import get as get_logger
from .cleaner import ExpiredPlanError, SpentPlanError, UnknownPlanError
from .explorer import resolve_root
from .fsutil import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    ScanCache,
    WalkResult,
    is_reparse,
    walk_size,
)
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
from .report import build_report, prune_reports, snapshot_volumes, write_report
from .scanner import budget_from_env, cache_enabled
from .settings import Settings
from .settings import load as load_settings
from .strategies import CleanContext, HardDelete, StrategyResult, remove_dir
from .volumes import fixed_volumes, volume_letter

# SPEC 6.3's default. Thirty days is one sprint plus slack: long enough that a
# project you are between tasks on is not offered, short enough that last
# quarter's experiments are.
DEFAULT_MIN_AGE_DAYS: Final = 30
MAX_MIN_AGE_DAYS: Final = 3650

# How deep below the chosen root a project may sit. Twelve is far past any real
# layout (``E:\PROJECT\group\repo\packages\app\node_modules`` is six) and stops a
# pathological tree from turning the find phase into a full disk scan.
MAX_DEPTH: Final = 12

# Rows kept in memory while measuring, and rows shipped to the page. A machine
# with four hundred repos has that many ``node_modules``; the table is ranked by
# size, so the ones past the cap are the ones nobody would have ticked. Both
# totals still count everything (see :meth:`SweepRun.totals`).
MAX_TRACKED: Final = 4096
TOP_ROWS: Final = 250

# Wall clock for the whole sweep. Longer than a level of the explorer because
# this walks a tree rather than one folder, and because the date phase stats
# files the find phase never touched. ``ADC_SCAN_BUDGET`` overrides it.
DEFAULT_SWEEP_BUDGET_S: Final = 300.0

# Entries one project's activity check will stat before it gives up. A project
# bigger than this is not dated and therefore not listed -- see
# :func:`project_activity` for why that is a refusal and not a guess.
ACTIVITY_SAMPLE: Final = 60_000

# The delete handshake, matching :mod:`adc.engine.cleaner` value for value: the
# same ten minutes to read a list and press the button, the same bound on a
# synchronous preview, the same cap on live plans.
PLAN_TTL_S: Final = 600.0
PLAN_BUDGET_S: Final = 120.0
MAX_LIVE_PLANS: Final = 8

# One preview cannot cover more than this many directories. A "select all" on a
# machine with a thousand findings would otherwise dry-run for minutes.
MAX_PLAN_ITEMS: Final = 500

# Per finding, for the before/after measurements. Same figure and same reason as
# ``cleaner.MEASURE_BUDGET_S``: a receipt must not stall on measuring.
MEASURE_BUDGET_S: Final = 60.0

PHASES: Final[tuple[Phase, ...]] = (
    Phase("find", "Tìm thư mục", "Finding"),
    Phase("date", "Xét hoạt động", "Dating projects"),
    Phase("measure", "Đo dung lượng", "Measuring"),
)

DELETE_PHASES: Final[tuple[Phase, ...]] = (
    Phase("prepare", "Chuẩn bị", "Preparing"),
    Phase("delete", "Đang xoá", "Deleting"),
    Phase("verify", "Đo lại", "Verifying"),
)

_log = get_logger("sweep")


def _node_id() -> str:
    """An opaque handle for one finding, for the same reason the explorer has one.

    The page ticks these and :func:`plan_delete` resolves them; no path the page
    sent is ever deleted (SEC-02). Random rather than derived -- a handle that can
    be computed from a path is a path.
    """
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class Rule:
    """One directory name worth listing, and the proof required before it is.

    *manifests* and *suffixes* are proof from outside: a sibling file named exactly
    (a ``Cargo.toml`` beside ``target``) or ending in one of them (any ``.csproj``
    beside ``bin``). *contains* is proof from inside -- one file the directory
    cannot be what it claims without. Any one of the three is enough; empty means
    no proof is needed, which is only ever true for a name nothing but a package
    installer or a compiler creates.

    *always_safe* is a UI badge, not a filter. ``__pycache__`` regenerates itself
    on the next import, so deleting it costs nothing; the row still waits for the
    project's date like every other, because a screen that lists a project you are
    working in teaches you to stop reading it.
    """

    name: str
    category: str
    manifests: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()
    always_safe: bool = False

    @property
    def needs_proof(self) -> bool:
        return bool(self.manifests or self.suffixes or self.contains)


# The manifests, grouped by what regenerates the directory. Named here rather
# than inline so that adding an ecosystem is one tuple and one rule, and so the
# same list cannot drift between ``dist`` and ``build``.
_JS: Final[tuple[str, ...]] = ("package.json",)
_PY: Final[tuple[str, ...]] = ("pyproject.toml", "setup.py", "setup.cfg")
_CMAKE: Final[tuple[str, ...]] = ("CMakeLists.txt", "Makefile", "meson.build")
_JVM: Final[tuple[str, ...]] = ("pom.xml", "build.gradle", "build.gradle.kts")
_DOTNET_FILES: Final[tuple[str, ...]] = ("Directory.Build.props",)
_DOTNET_SUFFIXES: Final[tuple[str, ...]] = (".csproj", ".vbproj", ".fsproj", ".sln")

RULES: Final[tuple[Rule, ...]] = (
    # No proof: nothing but a package installer writes a directory with this name.
    Rule("node_modules", "node_modules"),
    # Proof from inside, and it is definitive: ``pyvenv.cfg`` is written by
    # ``python -m venv``, ``virtualenv`` and ``uv venv`` and by nothing else. A
    # sibling manifest would have been the weaker test -- plenty of venvs sit in a
    # scratch folder with no project file anywhere near them.
    Rule(".venv", "venv", contains=("pyvenv.cfg",)),
    Rule("venv", "venv", contains=("pyvenv.cfg",)),
    # Regenerated on the next import or the next test run.
    Rule("__pycache__", "cache", always_safe=True),
    Rule(".pytest_cache", "cache", always_safe=True),
    Rule(".mypy_cache", "cache", always_safe=True),
    Rule(".ruff_cache", "cache", always_safe=True),
    # Framework caches: unambiguous names, but only inside a JS project.
    Rule(".next", "framework", manifests=_JS),
    Rule(".nuxt", "framework", manifests=_JS),
    Rule(".turbo", "framework", manifests=_JS),
    Rule(".parcel-cache", "framework", manifests=_JS),
    # Output directories, where the name alone proves nothing at all.
    Rule("dist", "build", manifests=_JS + _PY),
    Rule("out", "build", manifests=_JS + _PY),
    Rule("build", "build", manifests=_JS + _PY + _CMAKE + _JVM),
    Rule("target", "build", manifests=("Cargo.toml", "pom.xml")),
    Rule("bin", "build", manifests=_DOTNET_FILES, suffixes=_DOTNET_SUFFIXES),
    Rule("obj", "build", manifests=_DOTNET_FILES, suffixes=_DOTNET_SUFFIXES),
)

# The order the UI groups by, and the keys ``per_target`` uses in the report.
# Verified against ``targets.ids()``: none of these collides with a catalogue id,
# so the history view renders them through its own fallback without a prefix.
CATEGORIES: Final[tuple[str, ...]] = ("node_modules", "venv", "build", "cache", "framework")

_BY_NAME: Final[dict[str, Rule]] = {rule.name: rule for rule in RULES}

# Any of these beside a directory makes that directory a project root, which is
# what the date phase measures activity over. ``.git`` counts: a fetch or a
# commit is the user working, and this list must err towards "active".
_PROJECT_MARKERS: Final[frozenset[str]] = frozenset(
    name.lower()
    for rule in RULES
    for name in rule.manifests
) | {"cargo.toml", "package.json", "pyproject.toml", "requirements.txt", "pipfile", "go.mod"}

_MARKER_DIRS: Final[frozenset[str]] = frozenset({".git", ".hg", ".svn"})


def rule_for(name: str) -> Rule | None:
    """The rule for a directory name, case-folded. ``None`` means never listed."""
    return _BY_NAME.get(name.lower())


def _prove(rule: Rule, path: str, siblings: Mapping[str, str]) -> str | None:
    """The name of the file that proves *path* is what its name claims.

    Returns ``None`` when the rule needs proof and there is none -- the caller
    drops the candidate and counts it. For a rule that needs no proof the answer
    is also ``None``, which is why callers ask :attr:`Rule.needs_proof` first and
    never treat ``None`` alone as a refusal.

    *siblings* maps each lower-cased file name in the parent directory to the name
    as it is spelled on disk, taken from the same ``scandir`` that found the
    candidate: matching is case-insensitive because NTFS is, and the answer is the
    real spelling because the UI shows it next to the row. Proof costs no extra
    syscall except for :attr:`Rule.contains`, which is one ``exists`` per candidate.
    """
    for name in rule.contains:
        if os.path.exists(os.path.join(path, name)):
            return name
    for name in rule.manifests:
        actual = siblings.get(name.lower())
        if actual is not None:
            return actual
    for suffix in rule.suffixes:
        for folded, actual in siblings.items():
            if folded.endswith(suffix):
                return actual
    return None


@dataclass
class Finding:
    """One directory the sweep is willing to offer, and everything it can prove.

    Unlike :class:`~adc.engine.explorer.ExploreRow`, :meth:`as_dict` ships the full
    ``path``: SPEC 6.3 requires the screen to show it, because "delete 4.2 GB of
    node_modules" is not a decision anyone can make and "delete
    ``E:\\PROJECT\\old-api\\node_modules``" is. The path travelling *out* is not the
    hole SEC-02 closes -- what must never be accepted is a path travelling *in*,
    and :func:`plan_delete` takes only ``node_id``.
    """

    name: str
    path: str
    category: str
    project: str
    project_name: str
    node_id: str = field(default_factory=_node_id)
    always_safe: bool = False
    manifest: str | None = None
    idle_days: int | None = None
    project_mtime: float | None = None
    mtime: float | None = None
    size: int = 0
    logical: int = 0
    on_disk: int | None = None
    divergent: bool = False
    files: int = 0
    dirs: int = 0
    truncated: bool = False
    cached: bool = False
    denied_count: int = 0
    error: str | None = None

    def absorb(self, walk: WalkResult) -> None:
        """Attach this directory's walk. Assignment, not addition: one row, one walk."""
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
            "path": self.path,
            "category": self.category,
            "project": self.project,
            "project_name": self.project_name,
            "always_safe": self.always_safe,
            "manifest": self.manifest,
            "idle_days": self.idle_days,
            "project_mtime": self.project_mtime,
            "mtime": self.mtime,
            "size": self.size,
            "logical": self.logical,
            "on_disk": self.on_disk,
            "divergent": self.divergent,
            "files": self.files,
            "dirs": self.dirs,
            "truncated": self.truncated,
            "cached": self.cached,
            "denied_count": self.denied_count,
            "error": self.error,
        }


def _by_size(row: Finding) -> tuple[int, str]:
    """Biggest first, then by path so equal sizes do not shuffle between polls."""
    return (-row.size, row.path.lower())


class SweepRun:
    """The live result set for one sweep: the worker writes, the UI reads.

    Same division of labour as :class:`~adc.engine.explorer.ExploreRun`, including
    being the id -> path table that :func:`plan_delete` resolves against. One
    difference matters: rows are added *after* their project has been dated, not
    when they are found. An active project's directories must never appear on
    screen at all, and a poll landing between "found" and "dated" would show them
    for a quarter of a second -- which is exactly long enough to tick.
    """

    def __init__(self, job: Job, root: str, *, min_age_days: int, measured_on_disk: bool = True):
        self.job = job
        self.root = root
        self.min_age_days = min_age_days
        self.started_at = time.time()
        self._measured_on_disk = measured_on_disk
        self._lock = threading.Lock()
        self._rows: list[Finding] = []
        self._by_category: dict[str, dict[str, int]] = {
            name: {"count": 0, "size": 0} for name in CATEGORIES
        }
        self._total_size = 0
        self._total_logical = 0
        self._found = 0
        self._measured = 0
        self._to_measure = 0
        self._scanned_dirs = 0
        self._projects = 0
        self._active_projects = 0
        self._unscanned_projects = 0
        self._unproven = 0
        self._denied: list[str] = []
        self._truncated = False
        self._error: str | None = None

    def path_for(self, node_id: object) -> str | None:
        """The path a handle stands for, or ``None`` if this run never minted it.

        The run *is* the table, with exactly the right lifetime: an id reaches the
        page as part of a result set and lives as long as the job does. Typed
        loosely because the value came from JavaScript.
        """
        if not isinstance(node_id, str) or not node_id:
            return None
        with self._lock:
            for row in self._rows:
                if row.node_id == node_id:
                    return row.path
        return None

    def finding_for(self, node_id: object) -> Finding | None:
        """The row a handle stands for. :func:`plan_delete` needs the whole row."""
        if not isinstance(node_id, str) or not node_id:
            return None
        with self._lock:
            for row in self._rows:
                if row.node_id == node_id:
                    return row
        return None

    def add(self, row: Finding) -> Finding:
        """Record one finding at size 0; :meth:`measured` grows it.

        Counting happens here rather than at rank time, so a row pruned for memory
        is still in the totals and therefore in ``other_size``.
        """
        with self._lock:
            self._found += 1
            bucket = self._by_category.setdefault(row.category, {"count": 0, "size": 0})
            bucket["count"] += 1
            self._rows.append(row)
            if len(self._rows) > MAX_TRACKED * 2:
                self._prune_locked()
        return row

    def measured(self, row: Finding, walk: WalkResult) -> None:
        """Attach one finding's walk. The totals move by the difference."""
        with self._lock:
            before_size, before_logical = row.size, row.logical
            row.absorb(walk)
            self._total_size += row.size - before_size
            self._total_logical += row.logical - before_logical
            bucket = self._by_category.setdefault(row.category, {"count": 0, "size": 0})
            bucket["size"] += row.size - before_size
            self._measured += 1
            self._truncated = self._truncated or row.truncated

    def expect(self, count: int) -> None:
        """How many findings the measure phase is about to walk."""
        with self._lock:
            self._to_measure = count

    def note_scanned(self, count: int = 1) -> None:
        with self._lock:
            self._scanned_dirs += count

    def note_project(self, *, active: bool = False, unscanned: bool = False) -> None:
        """One project dated. ``active`` and ``unscanned`` are both refusals."""
        with self._lock:
            self._projects += 1
            if active:
                self._active_projects += 1
            if unscanned:
                self._unscanned_projects += 1

    def note_unproven(self, count: int = 1) -> None:
        """A name matched a rule and the manifest that proves it was not there."""
        with self._lock:
            self._unproven += count

    def note_denied(self, what: str) -> None:
        with self._lock:
            self._denied.append(what)

    def mark_truncated(self) -> None:
        """The sweep ran out of budget: every total below is a floor."""
        with self._lock:
            self._truncated = True

    def fail(self, message: str) -> None:
        """The root itself could not be walked. Not a row error -- the run's."""
        with self._lock:
            self._error = message

    def _prune_locked(self) -> None:
        """Keep the biggest ``MAX_TRACKED`` rows; the rest live on in the totals.

        A pruned row becomes unresolvable by :meth:`finding_for`, which is
        harmless: pruning drops the smallest, the page can only tick what it was
        shown, and what it was shown is the biggest.
        """
        self._rows.sort(key=_by_size)
        del self._rows[MAX_TRACKED:]

    def _top_locked(self) -> list[Finding]:
        rows = sorted(self._rows, key=_by_size)
        return rows[:TOP_ROWS]

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [row.as_dict() for row in self._top_locked()]

    def totals(self) -> dict[str, Any]:
        """The summary, including everything the sweep decided *not* to show.

        ``active_projects``, ``unscanned_projects`` and ``unproven`` are the three
        refusals, counted. A screen that says "12 project(s) still active" is
        telling the truth about a short list; one that just shows a short list is
        not (BUG-12's class of lie).
        """
        with self._lock:
            shown = self._top_locked()
            shown_size = sum(row.size for row in shown)
            return {
                "root": self.root,
                "min_age_days": self.min_age_days,
                "total_size": self._total_size,
                "total_logical": self._total_logical,
                "other_size": max(0, self._total_size - shown_size),
                "found": self._found,
                "shown": len(shown),
                "omitted": max(0, self._found - len(shown)),
                "measured": self._measured,
                "to_measure": self._to_measure,
                "scanned_dirs": self._scanned_dirs,
                "projects": self._projects,
                "active_projects": self._active_projects,
                "unscanned_projects": self._unscanned_projects,
                "unproven": self._unproven,
                "by_category": {k: dict(v) for k, v in self._by_category.items()},
                "truncated": self._truncated,
                "measured_on_disk": self._measured_on_disk,
                "denied_count": len(self._denied),
                "denied": self._denied[:32],
                "elapsed_s": round(time.time() - self.started_at, 3),
                "error": self._error,
            }

    def as_dict(self) -> dict[str, Any]:
        """What ``job_poll`` ships as ``partial_results`` for a sweep job."""
        return {
            "root": self.root,
            "name": os.path.basename(self.root) or self.root,
            "volume": volume_letter(self.root),
            "rows": self.rows(),
            "totals": self.totals(),
        }


def new_job() -> Job:
    """A SWEEP job with this module's three scan phases already attached."""
    return Job(JobKind.SWEEP, [Phase(p.key, p.label_vi, p.label_en) for p in PHASES])


def new_delete_job() -> Job:
    """A SWEEP job for the delete side. Same kind, different phases.

    Deliberately not ``JobKind.CLEAN``: the history view groups by kind, and a
    sweep delete belongs beside the sweep that found the rows, not among the
    catalogue cleans it shares no selection vocabulary with.
    """
    return Job(JobKind.SWEEP, [Phase(p.key, p.label_vi, p.label_en) for p in DELETE_PHASES])


# Folder names a dev machine keeps its projects in, checked in this order under
# every fixed volume and then under the profile. Only ever a *suggestion* the
# user can overrule -- the sweep runs on the root the UI sends, and if none of
# these exists the UI asks rather than guessing.
_ROOT_CANDIDATES: Final[tuple[str, ...]] = (
    "PROJECT",
    "Projects",
    "projects",
    "dev",
    "code",
    "src",
    "repos",
    "git",
)


def default_root() -> str | None:
    r"""A plausible project root, or ``None`` when nothing obvious exists.

    ``ADC_PROJECT_ROOT`` wins outright -- it is how the tests pin a root and how a
    user with an unusual layout stops being asked. After that: the non-system
    fixed volumes first (a dev machine that has a D: or an E: almost always keeps
    its code there, and this machine's own root is ``E:\PROJECT``), then the
    profile. Never the system volume's root itself, and never the profile itself:
    the guard would refuse both, and offering a root that cannot be swept is
    worse than offering none.
    """
    pinned = os.environ.get("ADC_PROJECT_ROOT", "").strip()
    if pinned and os.path.isdir(pinned):
        return os.path.abspath(pinned)

    system = os.path.normcase(os.environ.get("SYSTEMDRIVE", "C:") + os.sep)
    try:
        volumes = [v.root for v in fixed_volumes()]
    except OSError:
        volumes = []
    ordered = [v for v in volumes if os.path.normcase(v) != system]
    ordered += [v for v in volumes if os.path.normcase(v) == system]
    profile = os.environ.get("USERPROFILE", "")
    for base in [*ordered, profile] if profile else ordered:
        for name in _ROOT_CANDIDATES:
            candidate = os.path.join(base, name)
            if os.path.isdir(candidate):
                return candidate
    return None


def clamp_min_age_days(raw: object, *, default: int = DEFAULT_MIN_AGE_DAYS) -> int:
    """Whatever the page sent, turned into a usable number of days.

    Typed loosely because the value came from JavaScript: a string, a null, a NaN
    or an infinity all fall back to the default rather than being trusted. Zero is
    allowed and means "no age filter" -- an explicit choice the user can make, and
    the reason the UI shows the count of active projects it would then include.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return default
    try:
        days = int(raw)
    except (OverflowError, ValueError):
        return default
    return max(0, min(MAX_MIN_AGE_DAYS, days))


def project_activity(
    project: str,
    *,
    cancel: CancelToken,
    cutoff: float,
    limit: int = ACTIVITY_SAMPLE,
) -> tuple[float | None, bool]:
    """When anything in *project* last changed, and whether the answer is complete.

    Returns ``(latest_mtime, complete)``. ``complete`` is False whenever the walk
    stopped early -- the entry limit, an unreadable subdirectory, or the answer
    already being decided -- and the caller treats an incomplete *idle* answer as a
    refusal to list, not as an idle project. That is the conservative direction: a
    project too big or too locked-down to date is one we cannot promise is dead.

    Two directories are treated specially and for opposite reasons. The names in
    :data:`RULES` are skipped, because their mtimes record the last build rather
    than the last time the user did anything. ``.git`` is *not* skipped, because a
    fetch, a commit or a checkout is the user working.

    Stops the moment it meets something newer than *cutoff*: the question is "is
    this project active", and one recent file settles it without walking the rest.
    A file with an mtime in the future therefore keeps its project off the list
    forever, which is the safe way for a clock skew to fail.
    """
    try:
        st = os.stat(project, follow_symlinks=False)
    except OSError:
        return None, False
    latest: float | None = st.st_mtime
    if st.st_mtime > cutoff:
        return st.st_mtime, False

    seen = 0
    complete = True
    stack: list[str] = [project]
    while stack:
        cancel.raise_if_cancelled()
        current = stack.pop()
        try:
            scanner = os.scandir(current)
        except OSError:
            complete = False
            continue
        with scanner:
            try:
                for entry in scanner:
                    seen += 1
                    if seen > limit:
                        return latest, False
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        complete = False
                        continue
                    if st.st_mtime > cutoff:
                        return st.st_mtime, False
                    if latest is None or st.st_mtime > latest:
                        latest = st.st_mtime
                    attrs = getattr(st, "st_file_attributes", 0)
                    if is_reparse(entry, attrs):
                        continue
                    if stat.S_ISDIR(st.st_mode) and entry.name.lower() not in _BY_NAME:
                        stack.append(entry.path)
            except OSError:
                complete = False
    return latest, complete


@dataclass
class _Candidate:
    """A directory that matched a rule, before its project has been dated."""

    name: str
    path: str
    rule: Rule
    project: str
    manifest: str | None = None
    mtime: float | None = None


def _find_candidates(
    run: SweepRun,
    *,
    cancel: CancelToken,
    deadline: float | None,
) -> list[_Candidate]:
    """Walk the root and collect every directory a rule is willing to name.

    One ``scandir`` per directory, then two passes over the entries it returned:
    the first decides whether this directory is a project root, the second decides
    which of its subdirectories are candidates. Both read the same in-memory list,
    so proving a manifest costs no extra syscall.

    A matched directory is never descended into. Its contents are what a delete
    would remove, and walking a ``node_modules`` looking for a ``node_modules``
    inside it would take longer than deleting it.
    """
    out: list[_Candidate] = []
    stack: list[tuple[str, int, str | None]] = [(run.root, 0, None)]
    while stack:
        cancel.raise_if_cancelled()
        if deadline is not None and time.monotonic() >= deadline:
            run.mark_truncated()
            break
        current, depth, inherited = stack.pop()
        try:
            scanner = os.scandir(current)
        except OSError as exc:
            detail = exc.strerror or exc
            if current == run.root:
                run.fail(f"{current}: {detail}")
            else:
                run.note_denied(f"{current}: {detail}")
            continue

        dirs: list[tuple[os.DirEntry[str], os.stat_result]] = []
        # Lower-cased name -> the spelling on disk. Folded for matching because
        # NTFS is case-insensitive; the real name is kept because _prove returns it.
        files: dict[str, str] = {}
        marker_dir = False
        with scanner:
            try:
                for entry in scanner:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        run.note_denied(f"{entry.path}: {exc.strerror or exc}")
                        continue
                    attrs = getattr(st, "st_file_attributes", 0)
                    if is_reparse(entry, attrs):
                        continue  # BUG-01, and the TOCTOU defence with it
                    if stat.S_ISDIR(st.st_mode):
                        dirs.append((entry, st))
                        marker_dir = marker_dir or entry.name.lower() in _MARKER_DIRS
                    else:
                        files[entry.name.lower()] = entry.name
            except OSError as exc:
                run.note_denied(f"{current}: {exc.strerror or exc}")
        run.note_scanned()

        # Decided before any candidate is judged, because every candidate below
        # inherits it: this directory is a project root if something here says so.
        is_project = marker_dir or bool(files.keys() & _PROJECT_MARKERS)
        project = current if is_project else inherited
        siblings = files
        for entry, st in dirs:
            rule = rule_for(entry.name)
            if rule is None:
                if depth < MAX_DEPTH:
                    stack.append((entry.path, depth + 1, project))
                continue
            manifest = _prove(rule, entry.path, siblings)
            if rule.needs_proof and manifest is None:
                run.note_unproven()
                continue
            out.append(
                _Candidate(
                    name=entry.name,
                    path=entry.path,
                    rule=rule,
                    project=project if project is not None else current,
                    manifest=manifest,
                    mtime=st.st_mtime,
                )
            )
    return out


def _group_by_project(candidates: list[_Candidate]) -> dict[str, list[_Candidate]]:
    """One activity walk per project, not one per candidate.

    A repo with a ``node_modules``, a ``dist`` and forty ``__pycache__`` is one
    project and must be dated once -- otherwise the date phase would walk it
    forty-two times to reach the same answer.
    """
    grouped: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.project, []).append(candidate)
    return grouped


def _finding_from(candidate: _Candidate, *, idle_days: int, project_mtime: float) -> Finding:
    return Finding(
        name=candidate.name,
        path=candidate.path,
        category=candidate.rule.category,
        project=candidate.project,
        project_name=os.path.basename(candidate.project) or candidate.project,
        always_safe=candidate.rule.always_safe,
        manifest=candidate.manifest,
        idle_days=idle_days,
        project_mtime=project_mtime,
        mtime=candidate.mtime,
    )


def run_sweep(
    run: SweepRun,
    *,
    settings: Settings | None = None,
    cache: ScanCache | None = None,
) -> SweepRun:
    """The worker body. Synchronous, so a test can call it without a thread.

    Lets :class:`~adc.engine.jobs.CancelledError` out, like the scanner and the
    explorer: the runner is what marks a job CANCELLED, and swallowing it here
    would present a half-dated sweep as a finished one.

    Three settings deliberately do not apply. ``min_age_hours`` is an hours-scale
    floor on individual cache files and this is a days-scale question about
    projects; ``volume_ids`` narrows a catalogue clean spread over several volumes
    and this runs on one root the user named; ``exclusions`` are enforced where
    they matter, by the guard, at delete time.
    """
    active = settings or Settings()
    job = run.job
    cancel = job.cancel_token
    owns_cache = cache is None and cache_enabled()
    scan_cache = cache if cache is not None else (ScanCache() if owns_cache else None)
    budget = budget_from_env(active.scan_budget_s or DEFAULT_SWEEP_BUDGET_S)
    deadline = None if budget is None else time.monotonic() + budget
    cutoff = time.time() - run.min_age_days * 86400.0

    try:
        job.enter_phase("find")
        candidates = _find_candidates(run, cancel=cancel, deadline=deadline)
        job.set_phase_progress(1.0)
        found = run.totals()
        if found["error"]:
            job.log(
                Level.ERROR,
                f"Không đọc được thư mục gốc: {found['error']}",
                f"Could not read the root: {found['error']}",
            )
        else:
            job.log(
                Level.INFO,
                f"{len(candidates)} thư mục ứng viên trong {found['scanned_dirs']} thư mục.",
                f"{len(candidates)} candidate(s) across {found['scanned_dirs']} folder(s).",
            )
        if found["unproven"]:
            job.log(
                Level.INFO,
                f"Bỏ qua {found['unproven']} thư mục không có manifest bên cạnh.",
                f"Skipped {found['unproven']} folder(s) with no manifest beside them.",
            )

        job.enter_phase("date")
        grouped = _group_by_project(candidates)
        kept: list[Finding] = []
        projects = max(1, len(grouped))
        now = time.time()
        for index, project in enumerate(sorted(grouped)):
            cancel.raise_if_cancelled()
            if deadline is not None and time.monotonic() >= deadline:
                run.mark_truncated()
                break
            label = os.path.basename(project) or project
            job.set_phase_progress(
                index / projects,
                detail_vi=f"Đang xét {label}…",
                detail_en=f"Dating {label}…",
            )
            latest, complete = project_activity(project, cancel=cancel, cutoff=cutoff)
            if latest is not None and latest > cutoff:
                # The one rule the mandatory test names: an active project is not
                # listed at all, in any category.
                run.note_project(active=True)
                continue
            if latest is None or not complete:
                run.note_project(unscanned=True)
                continue
            run.note_project()
            idle_days = max(0, int((now - latest) // 86400))
            for candidate in grouped[project]:
                kept.append(
                    run.add(_finding_from(candidate, idle_days=idle_days, project_mtime=latest))
                )
        job.set_phase_progress(1.0)
        run.expect(len(kept))
        dated = run.totals()
        job.log(
            Level.INFO,
            f"{dated['projects']} project đã xét, {dated['active_projects']} còn hoạt động"
            + (f", {dated['unscanned_projects']} quá lớn để xét" if dated["unscanned_projects"]
               else "")
            + ".",
            f"{dated['projects']} project(s) dated, {dated['active_projects']} still active"
            + (f", {dated['unscanned_projects']} too large to date"
               if dated["unscanned_projects"] else "")
            + ".",
        )

        job.enter_phase("measure")
        total = max(1, len(kept))
        for index, row in enumerate(kept):
            cancel.raise_if_cancelled()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                run.mark_truncated()
                job.log(
                    Level.WARN,
                    f"Hết thời gian: còn {len(kept) - index} thư mục chưa đo.",
                    f"Out of time: {len(kept) - index} folder(s) not measured.",
                )
                break
            job.set_phase_progress(
                index / total,
                detail_vi=f"Đang đo {row.project_name}\\{row.name}…",
                detail_en=f"Measuring {row.project_name}\\{row.name}…",
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
                # walk_size absorbs everything inside the tree; this is the
                # finding's own directory being unusable.
                row.error = f"{exc.strerror or exc}"
                run.note_denied(f"{row.path}: {exc.strerror or exc}")
                continue
            run.measured(row, walk)
            job.set_phase_progress((index + 1) / total)

        final = run.totals()
        if final["truncated"]:
            job.log(
                Level.WARN,
                "Chưa quét hết: các con số dưới đây là mức tối thiểu.",
                "Not fully scanned: the figures below are a floor.",
            )
        job.log(
            Level.SUCCESS,
            f"Xong: {final['found']} thư mục, {final['total_size']:,} B có thể thu hồi.",
            f"Done: {final['found']} folder(s), {final['total_size']:,} B reclaimable.",
        )
        _log.info(
            "sweep %s: root=%s days=%d scanned=%d found=%d projects=%d active=%d "
            "unscanned=%d unproven=%d total=%d truncated=%s denied=%d",
            job.id,
            run.root,
            run.min_age_days,
            final["scanned_dirs"],
            final["found"],
            final["projects"],
            final["active_projects"],
            final["unscanned_projects"],
            final["unproven"],
            final["total_size"],
            final["truncated"],
            final["denied_count"],
        )
    finally:
        if owns_cache and scan_cache is not None:
            scan_cache.close()
    return run


def submit_sweep(
    runner: JobRunner,
    *,
    root: str | os.PathLike[str],
    settings: Settings | None = None,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
) -> SweepRun:
    """Start one sweep on a worker thread and return the handle to poll.

    :func:`~adc.engine.explorer.resolve_root` runs *before* the job exists, the
    same as the explorer and for the same reason: a root that is missing or is a
    file should be a synchronous error the caller can phrase, not a job that
    starts and immediately fails. The bridge maps the ``OSError`` to
    ``not_present``.
    """
    active = settings or Settings()
    run = SweepRun(
        new_job(),
        resolve_root(root),
        min_age_days=max(0, min(MAX_MIN_AGE_DAYS, min_age_days)),
        measured_on_disk=active.size_on_disk,
    )
    _log.info("sweep %s: root=%s days=%d", run.job.id, run.root, run.min_age_days)
    runner.submit(run.job, lambda _job: run_sweep(run, settings=active))
    return run


# -- the delete half -------------------------------------------------------
#
# Everything below turns a scan result into a deletion, and it is deliberately
# the same shape as ``cleaner``: a dry run mints a single-use token, the token is
# the only way to start the real work, and the report is written in a ``finally``
# so a run that crashes half way still leaves a receipt. Nothing here accepts a
# path from the page.


def _measure(paths: Sequence[str], settings: Settings) -> int:
    """Size of the given paths right now. Best effort, bounded, never cached.

    The same two real measurements the cleaner takes around a target, taken for
    the same reason: the reclaimed figure in the report is a difference between
    two measurements, not a total of what the deleter believed it removed. A path
    that has just been deleted is not an error -- it is the expected state of the
    "after" measurement -- so a missing root contributes nothing and no warning.

    Never through the scan cache: a cached size is exactly the wrong answer for a
    directory we are about to empty.
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


@dataclass
class SweepItem:
    """One ticked row, as the dry run left it.

    ``path`` is stored because the delete needs it and because the report is the
    audit trail -- "which directories did it empty" is the first question anyone
    asks afterwards. It came out of the run's own id table, never off the page:
    the confirm call carries node ids, so there is no way to name a directory the
    sweep did not itself find and prove (SEC-02).
    """

    node_id: str
    path: str
    name: str
    category: str
    project: str
    idle_days: int | None = None
    est_bytes: int = 0
    files: int = 0
    dirs: int = 0
    files_locked: int = 0
    denied: int = 0
    locked_by: tuple[str, ...] = ()
    will_run: bool = False
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "path": self.path,
            "name": self.name,
            "category": self.category,
            "project": self.project,
            "idle_days": self.idle_days,
            "est_bytes": self.est_bytes,
            "files": self.files,
            "dirs": self.dirs,
            "files_locked": self.files_locked,
            "denied": self.denied,
            "locked_by": list(self.locked_by),
            "will_run": self.will_run,
            "skipped_reason": self.skipped_reason,
        }


@dataclass
class SweepPlan:
    """A dry run, frozen. The token is the only way to make it happen.

    ``has_irreversible`` is not computed from the rows the way the cleaner's plan
    computes it: every row here is a hard delete, so the answer is always yes and
    the confirm dialog must always say so.
    """

    token: str
    created_at: float
    root: str
    min_age_days: int
    items: tuple[SweepItem, ...] = ()
    settings: Settings = field(default_factory=Settings)
    truncated: bool = False
    spent_at: float | None = None

    @property
    def runnable(self) -> tuple[SweepItem, ...]:
        return tuple(item for item in self.items if item.will_run)

    @property
    def est_total(self) -> int:
        """Only what will actually run. A skipped row's estimate is not a promise."""
        return sum(item.est_bytes for item in self.runnable)

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({item.category for item in self.runnable}))

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created_at > PLAN_TTL_S

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "created_at": self.created_at,
            "expires_at": self.created_at + PLAN_TTL_S,
            "root": self.root,
            "min_age_days": self.min_age_days,
            "items": [item.as_dict() for item in self.items],
            "node_ids": [item.node_id for item in self.runnable],
            "categories": list(self.categories),
            "est_total": self.est_total,
            "count": len(self.runnable),
            "skipped": len(self.items) - len(self.runnable),
            "needs_admin": False,
            "has_irreversible": True,
            "reversible": False,
            "truncated": self.truncated,
            "exclusion_count": len(self.settings.exclusions),
        }


class SweepPlanStore:
    """The live sweep plans, keyed by token. Single-use, TTL- and capacity-bounded.

    Deliberately a second, small store rather than a generic one shared with
    ``cleaner.PlanStore``: making that class generic would mean editing a module
    whose token handling is already covered by tests, to save forty lines here.
    What is *not* duplicated is the vocabulary -- the three failure modes raise
    ``cleaner``'s own error types, so ``bridge.guarded`` maps them to
    ``plan_unknown`` / ``plan_expired`` / ``plan_spent`` with no new bridge code
    and the UI needs no new strings.
    """

    def __init__(self, capacity: int = MAX_LIVE_PLANS) -> None:
        self.capacity = capacity
        self._lock = threading.Lock()
        self._plans: dict[str, SweepPlan] = {}

    def put(self, plan: SweepPlan) -> None:
        with self._lock:
            self._purge_locked()
            while len(self._plans) >= self.capacity:
                oldest = min(self._plans.values(), key=lambda p: p.created_at)
                del self._plans[oldest.token]
            self._plans[plan.token] = plan

    def peek(self, token: object) -> SweepPlan | None:
        if not isinstance(token, str):
            return None
        with self._lock:
            return self._plans.get(token)

    def spend(self, token: object) -> SweepPlan:
        """Redeem a token exactly once. Every failure mode is a distinct error."""
        if not isinstance(token, str) or not token:
            raise UnknownPlanError("no plan token")
        now = time.time()
        with self._lock:
            found = self._plans.get(token)
            if found is None:
                raise UnknownPlanError("no such plan; run a preview first")
            if found.spent_at is not None:
                raise SpentPlanError("this plan has already been executed")
            if found.expired(now):
                del self._plans[token]
                raise ExpiredPlanError("the preview has expired; preview again")
            found.spent_at = now
            return found

    def _purge_locked(self) -> None:
        now = time.time()
        for token in [t for t, p in self._plans.items() if p.expired(now)]:
            del self._plans[token]

    def clear(self) -> None:
        with self._lock:
            self._plans.clear()


_store = SweepPlanStore()


def sweep_plan_store() -> SweepPlanStore:
    """The process-wide store. A parameter everywhere else, so tests stay isolated."""
    return _store


def _guard_for(root: str, settings: Settings) -> Guard:
    r"""One guard for the whole plan, rooted at the swept directory.

    The root is the *sweep* root rather than each finding, which is the stronger
    statement: every path the deleter touches has to resolve inside the tree the
    user pointed at, so a junction or a symlink that appeared since the preview
    cannot walk the delete out of it. The user's exclusion list is layered on top.

    A blocked root -- someone typing ``C:\Windows`` into the box -- raises out of
    :class:`~adc.engine.guard.Guard` here and refuses the entire plan, which is
    the intended answer: the sweeper has no business in a system directory even if
    it managed to find a ``node_modules`` inside one.
    """
    return Guard(roots=(root,), exclusions=settings.exclusions)


def guard_for_root(root: str, settings: Settings | None = None) -> Guard:
    """The guard a delete under *root* would be checked against, built early.

    Public because the bridge wants it *before* the walk. A root that can be
    listed but never deleted from -- a volume root, a system directory, the
    profile itself -- should be refused in the second it takes to type it, rather
    than five minutes later when the preview raises with a table already on
    screen. One definition, so the answer given at the door and the answer given
    at confirm time cannot differ.
    """
    return _guard_for(root, settings if settings is not None else load_settings())


def _item_from(finding: Finding, *, skipped_reason: str | None = None) -> SweepItem:
    """The row shape both outcomes share: everything from the finding, nothing new."""
    return SweepItem(
        node_id=finding.node_id,
        path=finding.path,
        name=finding.name,
        category=finding.category,
        project=finding.project,
        idle_days=finding.idle_days,
        skipped_reason=skipped_reason,
    )


_TIMED_OUT: Final = "the preview ran out of time"


def _dry_run_item(
    finding: Finding,
    *,
    strategy: HardDelete,
    guard: Guard,
    cancel: CancelToken,
) -> SweepItem:
    """Walk one ticked directory the way the delete will, and count what it would take.

    A real dry run, not an estimate. ``HardDelete.execute`` with ``dry_run=True``
    runs the same sweep, the same per-path guard check, the same reparse-point rule
    and the same user exclusions the live pass will use, so the figure under the
    confirm button was produced by the code that does the work.

    ``min_age_hours=0`` is deliberate, and it is not a hole in the age filter. The
    age question was already answered once, at the project level, and answered
    better: applying an hours cutoff to the files *inside* a ``node_modules``
    would hold back whatever the last install wrote and leave a half-deleted tree
    that neither builds nor reinstalls cleanly.

    The exclusion check is made here rather than left to the sweep because
    ``strategies.sweep`` swallows an ``ExcludedPathError`` on the root it was
    handed and returns an empty result: the row would then be skipped with
    "nothing left to remove", which sends the user looking at the directory
    instead of at their own exclusion list.
    """
    item = _item_from(finding)
    context = CleanContext(
        target_id=finding.category,
        paths=(finding.path,),
        guard=guard,
        cancel=cancel,
        dry_run=True,
        min_age_hours=0,
    )
    try:
        guard.check(finding.path)
        result = strategy.execute(context)
    except CancelledError:
        # A tripped flag is the user; a spent budget is this row's bad luck.
        if cancel.cancelled:
            raise
        item.skipped_reason = _TIMED_OUT
        return item
    except GuardError as exc:
        item.skipped_reason = f"refused by the guard: {exc}"
        return item
    except OSError as exc:
        item.skipped_reason = str(exc.strerror or exc)
        return item
    return _score(item, result)


def _score(item: SweepItem, result: StrategyResult) -> SweepItem:
    """Copy the dry run's evidence onto the row and decide whether it will run.

    A dry run that produced any evidence -- a file count, a directory count or a
    byte figure -- will run. One that produced none and reported no error has
    nothing to do, and saying so is more useful than offering a row that would
    delete nothing. Note that a byte figure of zero is not on its own a reason to
    skip: ``HardDelete`` withholds the byte credit whenever a user exclusion held
    something back, so the files count is the honest signal there.
    """
    item.est_bytes = result.bytes_deleted
    item.files = result.files_deleted
    item.dirs = result.dirs_deleted
    item.files_locked = result.files_locked
    item.denied = result.denied
    item.locked_by = tuple(result.locked_by)
    reason = result.skipped_reason
    if reason is None and not result.ok:
        reason = "the preview reported a failure"
    if reason is None and not (item.files or item.dirs or item.est_bytes):
        reason = "nothing left to remove"
    item.skipped_reason = reason
    item.will_run = reason is None
    return item


def plan_delete(
    run: SweepRun,
    node_ids: object,
    *,
    settings: Settings | None = None,
    store: SweepPlanStore | None = None,
    budget_s: float | None = PLAN_BUDGET_S,
) -> SweepPlan:
    """Dry-run the ticked rows, mint a single-use token, return the preview.

    *node_ids* is typed ``object`` because the caller is the bridge and the values
    came from JavaScript: anything that is not a string this run actually minted is
    dropped rather than guessed at. That is the whole of SEC-02 for this feature --
    the confirm path names *handles*, so there is no way to ask the sweeper to
    delete a directory it did not itself find, prove and date.

    The cap is on rows, not bytes: ``MAX_PLAN_ITEMS`` dry runs is already a long
    wait, and a selection larger than that is a mis-click or a "select all" on a
    tree of thousands.
    """
    active = settings if settings is not None else load_settings()
    target = store if store is not None else sweep_plan_store()
    guard = _guard_for(run.root, active)
    strategy = HardDelete()
    cancel = CancelToken(budget_s)
    requested: Sequence[object]
    requested = [node_ids] if isinstance(node_ids, str) else list(_as_sequence(node_ids))

    items: list[SweepItem] = []
    seen: set[str] = set()
    truncated = False
    for raw in requested:
        if not isinstance(raw, str) or raw in seen:
            continue
        seen.add(raw)
        if len(items) >= MAX_PLAN_ITEMS:
            truncated = True
            break
        finding = run.finding_for(raw)
        if finding is None:
            # A handle from a previous run, or a row that has since been pruned.
            items.append(
                SweepItem(node_id=raw, path="", name=raw, category="unknown", project="",
                          skipped_reason="unknown selection; sweep again")
            )
            continue
        items.append(_dry_run_item(finding, strategy=strategy, guard=guard, cancel=cancel))
    return _mint(run, items, active, target, truncated=truncated)


def _as_sequence(value: object) -> Sequence[object]:
    """A JS array arrives as a list. Anything else selects nothing, quietly."""
    if isinstance(value, list | tuple):
        return value
    return ()


def _mint(
    run: SweepRun,
    items: list[SweepItem],
    settings: Settings,
    store: SweepPlanStore,
    *,
    truncated: bool,
) -> SweepPlan:
    """Freeze the rows into a plan, store it, log what was promised."""
    made = SweepPlan(
        token=uuid.uuid4().hex,
        created_at=time.time(),
        root=run.root,
        min_age_days=run.min_age_days,
        items=tuple(items),
        settings=settings,
        truncated=truncated or any(item.skipped_reason == _TIMED_OUT for item in items),
    )
    store.put(made)
    _log.info(
        "sweep plan %s: %d row(s), %d runnable, est=%d bytes, root=%s",
        made.token[:8],
        len(made.items),
        len(made.runnable),
        made.est_total,
        made.root,
    )
    return made


def _notes_for(approved: SweepPlan) -> dict[str, Any]:
    """What the report keeps about the promise, so the gap can be audited later.

    The full path list goes in here and nowhere else. For a hard delete of a
    directory the user picked off a tree, "which paths went" is not a detail --
    it is the only record that exists afterwards.
    """
    return {
        "plan_token": approved.token,
        "root": approved.root,
        "min_age_days": approved.min_age_days,
        "est_total": approved.est_total,
        "planned": len(approved.runnable),
        "plan_truncated": approved.truncated,
        "reversible": False,
        "exclusions": list(approved.settings.exclusions),
        "size_on_disk": approved.settings.size_on_disk,
        "deleted_paths": [
            {
                "path": item.path,
                "category": item.category,
                "project": item.project,
                "idle_days": item.idle_days,
                "est_bytes": item.est_bytes,
                "files": item.files,
            }
            for item in approved.runnable
        ],
        "skipped_at_plan": {
            item.node_id: item.skipped_reason
            for item in approved.items
            if not item.will_run
        },
    }


def _finish_and_write(
    job: Job,
    approved: SweepPlan,
    *,
    free_before: dict[str, int],
    state: JobState,
    error: str | None,
    write: bool,
) -> dict[str, Any]:
    """Close the job, take the second volume snapshot, build and store the report.

    The job is finished here rather than left to :class:`JobRunner` so the report
    records a terminal state and a real duration; the runner then sees a job that
    is no longer RUNNING and leaves it alone. ``selection`` is the category list
    because that is what ``per_target`` is keyed by for a sweep -- five names, not
    forty-nine catalogue ids.
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
        selection=list(approved.categories),
        notes=_notes_for(approved),
    )
    if not write:
        return body
    try:
        path = write_report(body)
    except OSError as exc:
        # A report that cannot be written is not worth losing the run over.
        _log.warning("sweep %s: report not written: %s", job.id, exc)
        return body
    body["report_path"] = str(path)
    _log.info(
        "sweep-delete %s: state=%s reclaimed=%d free_delta=%d est=%d report=%s",
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


def _delete_one(job: Job, item: SweepItem, settings: Settings, *, guard: Guard) -> None:
    """Delete one directory: measure, empty it through ``HardDelete``, measure again.

    Two measurements around the operation is what makes the reclaimed figure in
    the report a fact rather than a running total the deleter kept about itself.

    ``HardDelete`` empties the directory but cannot remove it: ``strategies.sweep``
    never lists the root it was handed, by design, so without the ``remove_dir``
    below every cleaned ``node_modules`` would survive as an empty shell and the
    next sweep would list it again at zero bytes. The rmdir is skipped when
    anything was left behind -- a locked file means the directory is genuinely not
    empty, and an ``ENOTEMPTY`` in the log would be noise rather than news.
    """
    outcome = job.outcome(item.category)

    def emit(level: Level, message_vi: str, message_en: str) -> None:
        job.log(level, message_vi, message_en, target_id=item.category)

    paths = (item.path,)
    outcome.before += _measure(paths, settings)
    context = CleanContext(
        target_id=item.category,
        paths=paths,
        guard=guard,
        cancel=job.cancel_token,
        dry_run=False,
        min_age_hours=0,
        log=emit,
    )
    strategy = HardDelete()
    try:
        result = strategy.execute(context)
    except CancelledError:
        # The receipt must still say what happened before the stop.
        outcome.after += _measure(paths, settings)
        outcome.skipped_reason = outcome.skipped_reason or "cancelled part-way"
        raise
    except GuardError as exc:
        # An escape aborts this directory rather than skipping one file (SPEC 4.7).
        outcome.after += _measure(paths, settings)
        outcome.skipped_reason = outcome.skipped_reason or f"refused by the guard: {exc}"
        emit(Level.ERROR, f"Guard từ chối {item.path}: {exc}",
             f"The guard refused {item.path}: {exc}")
        return
    except OSError as exc:
        detail = str(exc.strerror or exc)
        outcome.after += _measure(paths, settings)
        outcome.skipped_reason = outcome.skipped_reason or detail
        emit(Level.WARN, f"Lỗi khi xoá {item.path}: {detail}",
             f"Error deleting {item.path}: {detail}")
        return

    result.apply_to(outcome)
    if result.files_locked or result.denied:
        emit(Level.WARN,
             f"Còn tệp bị giữ trong {item.name}, để lại thư mục.",
             f"Files still held in {item.name}; the folder is left in place.")
    else:
        removed, why = remove_dir(item.path)
        if removed not in {"deleted", "missing"}:
            emit(Level.WARN,
                 f"Đã xoá xong nội dung nhưng không xoá được {item.path}: {why}",
                 f"Emptied but could not remove {item.path}: {why}")
    outcome.after += _measure(paths, settings)
    emit(Level.SUCCESS,
         f"{item.name}: {result.files_deleted:,} tệp, {result.bytes_deleted:,} B — {item.path}",
         f"{item.name}: {result.files_deleted:,} file(s), {result.bytes_deleted:,} B "
         f"— {item.path}")


def _still_valid(item: SweepItem) -> str | None:
    """Re-check the row against the disk. The plan is up to ten minutes old.

    This is the cleaner's "re-resolve before deleting" property in the shape this
    feature needs: the preview proved a directory, and what gets deleted must be
    that directory *now*. A path that vanished, stopped being a directory, turned
    into a junction, or no longer carries a name the catalogue recognises is
    refused here rather than handed to the deleter.

    The link test reads the attribute off the ``lstat`` this call already took --
    the same primary signal :func:`~adc.engine.fsutil.is_reparse` uses, since that
    one needs a ``DirEntry`` this call does not have -- with ``isjunction`` as the
    fallback for a host that does not populate the attribute. The guard's
    ``realpath`` resolution, which every path inside still goes through, is what
    actually stops an escape; this is the earlier and clearer refusal.
    """
    try:
        entry = os.lstat(item.path)
    except OSError as exc:
        return str(exc.strerror or exc)
    attrs = getattr(entry, "st_file_attributes", 0)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT or os.path.isjunction(item.path):
        return "now a junction or a symlink"
    if not stat.S_ISDIR(entry.st_mode):
        return "no longer a directory"
    if rule_for(os.path.basename(item.path)) is None:
        return "no longer a recognised build or cache directory"
    return None


def run_delete(approved: SweepPlan, job: Job, *, write: bool = True) -> dict[str, Any]:
    """The worker body: delete every runnable row, then write the report.

    Synchronous, so a test can call it without a thread. The report is built in a
    ``finally`` and therefore survives a cancel and a crash alike -- a run that
    emptied nine directories and then failed on the tenth must still leave a
    receipt saying which nine.
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
            f"Sẽ xoá {len(items)} thư mục, dự kiến {approved.est_total:,} B.",
            f"Deleting {len(items)} folder(s), estimated {approved.est_total:,} B.",
        )
        job.log(
            Level.WARN,
            "Xoá thẳng, không qua Thùng rác. Không thể hoàn tác.",
            "Deleted outright, not to the Recycle Bin. This cannot be undone.",
        )
        guard = _guard_for(approved.root, settings)
        job.set_phase_progress(1.0)

        job.enter_phase("delete")
        total = max(1, len(items))
        for index, item in enumerate(items):
            job.cancel_token.raise_if_cancelled()
            job.set_phase_progress(
                index / total,
                detail_vi=f"Đang xoá {item.name} trong {item.project}…",
                detail_en=f"Deleting {item.name} in {item.project}…",
            )
            refusal = _still_valid(item)
            if refusal is not None:
                outcome = job.outcome(item.category)
                outcome.skipped_reason = outcome.skipped_reason or refusal
                job.log(Level.WARN, f"Bỏ qua {item.path}: {refusal}",
                        f"Skipped {item.path}: {refusal}", target_id=item.category)
            else:
                _delete_one(job, item, settings, guard=guard)
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


def submit_delete(
    runner: JobRunner,
    *,
    token: object,
    store: SweepPlanStore | None = None,
) -> Job:
    """Redeem a preview token and start the delete. The only way to remove anything.

    Takes a runner and a token, and nothing else -- no path, no id list, no flag.
    Everything that decides what gets deleted was fixed when the preview was
    taken, which is what makes the preview a promise instead of a suggestion, and
    it is why a page that has been tampered with cannot widen the selection.

    The busy check comes before the token is spent, so a click that lands while a
    scan is still finishing does not burn the token on a job that never started.
    """
    active = runner.active()
    if active is not None and active.state is JobState.RUNNING:
        raise RuntimeError(f"job {active.id} is still running")
    approved = (store if store is not None else sweep_plan_store()).spend(token)
    job = new_delete_job()
    _log.info(
        "sweep-delete %s: plan %s, %d folder(s), est=%d bytes, root=%s",
        job.id,
        approved.token[:8],
        len(approved.runnable),
        approved.est_total,
        approved.root,
    )
    runner.submit(job, lambda started: run_delete(approved, started))
    return job
