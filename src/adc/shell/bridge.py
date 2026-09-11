"""The only surface JavaScript can reach. Everything crosses here, or not at all.

v1 put a Flask server on ``127.0.0.1`` and let the page POST it a path to delete
(docs/01-AUDIT.md SEC-01..03). v2 has no server and no port: pywebview hands this
object to the page as ``window.pywebview.api``, so the transport is an in-process
call and there is nothing on the network to find. What is left to defend is the
*argument*, and the rule for that is one sentence: **the page sends ids, never
paths.** An id is looked up in the catalogue, and the catalogue decides what it
means on this machine. No method here accepts a path from the renderer.

Three further invariants this module owns:

* **Nothing leaks.** Every public method returns ``{"ok": bool, ...}`` and never
  raises. A traceback reaching the renderer would print internal paths and module
  names into a web page; :func:`guarded` turns one into a logged ``internal``
  error instead.
* **Nothing deletes without a token.** :meth:`Bridge.clean_execute` takes a plan
  token and nothing else -- no ids, no flags, no paths. Everything that decides
  what gets deleted was fixed when the dry run was taken.
* **The engine stays unaware.** This module imports the engine; the engine
  imports nothing from here.

Errors are bilingual at the boundary rather than in the page, because the engine
already logs every event in both languages and a code-to-string table in JS would
be a second place for the wording to drift.
"""

from __future__ import annotations

import functools
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, ParamSpec

from adc.engine import audit, explorer, paths, report, scanner, sweeper, targets, vss
from adc.engine import platform_win as win
from adc.engine import settings as settings_mod
from adc.engine import volumes as volumes_mod
from adc.engine.audit import get as get_logger
from adc.engine.cleaner import (
    ExpiredPlanError,
    Plan,
    PlanError,
    PlanStore,
    SpentPlanError,
    UnknownPlanError,
    plan_store,
    submit_clean,
)
from adc.engine.cleaner import plan as build_plan
from adc.engine.guard import GuardError
from adc.engine.jobs import Job, JobRunner, JobState
from adc.engine.settings import Settings
from adc.engine.vss import ActionStore, PendingAction

_log = get_logger(__name__)

#: Hard ceiling on a selection arriving from JS. The catalogue is forty-nine rows,
#: so anything past this is a UI bug or a hostile page, and either way the cost of
#: dry-running it should not be unbounded.
MAX_SELECTION: Final = 200

#: How many log lines one poll may carry. A cancelled walk over a million files
#: can produce thousands; the UI shows a scrolling list and does not need them in
#: one frame. ``since_event`` means the rest arrive on the next tick.
MAX_EVENTS: Final = 200

#: Default rows for :meth:`Bridge.history`. ``report.prune_reports`` keeps a hundred
#: files; a page that wants the older ones asks for them, and the ceiling in
#: :func:`_as_limit` is what stops it asking for a million.
HISTORY_LIMIT: Final = 30

Reply = dict[str, Any]


class BridgeError(Exception):
    """An expected failure, with the code and both strings the UI will show."""

    def __init__(self, code: str, vi: str, en: str) -> None:
        super().__init__(en)
        self.code = code
        self.vi = vi
        self.en = en


def _ok(data: Any = None) -> Reply:
    return {"ok": True, "data": data}


def _fail(code: str, vi: str, en: str) -> Reply:
    return {"ok": False, "error": {"code": code, "message_vi": vi, "message_en": en}}


_P = ParamSpec("_P")

# Token failures are separate classes because the UI says different things: an
# unknown token means the engine restarted, an expired one asks for a fresh
# preview, and a spent one means the clean already started -- which is what a
# double-clicked confirm button looks like from here.
_PLAN_ERRORS: Final[dict[type[Exception], tuple[str, str, str]]] = {
    UnknownPlanError: (
        "plan_unknown",
        "Không tìm thấy bản xem trước. Hãy quét và xem trước lại.",
        "No such preview. Run a scan and preview again.",
    ),
    ExpiredPlanError: (
        "plan_expired",
        "Bản xem trước đã quá hạn. Hãy xem trước lại để lấy số liệu mới.",
        "This preview has expired. Preview again for current figures.",
    ),
    SpentPlanError: (
        "plan_spent",
        "Bản xem trước này đã được dùng. Lần dọn đó đã chạy.",
        "This preview was already used. That clean has run.",
    ),
}

# The same three failures for a shadow-copy receipt, kept apart for the same
# reason and worded differently because the stakes are: an expired VSS preview was
# measured against a store Windows has been quietly changing since (BUG-09 --
# this machine lost its only restore point between two readings a day apart).
_ACTION_ERRORS: Final[dict[type[Exception], tuple[str, str, str]]] = {
    vss.UnknownActionError: (
        "vss_unknown",
        "Không tìm thấy yêu cầu này. Hãy xem trước lại.",
        "No such pending action. Preview it again.",
    ),
    vss.ExpiredActionError: (
        "vss_expired",
        "Yêu cầu đã quá hạn. Hãy xem trước lại để đọc số liệu hiện tại.",
        "This action has expired. Preview again for current figures.",
    ),
    vss.SpentActionError: (
        "vss_spent",
        "Yêu cầu này đã chạy rồi.",
        "This action has already run.",
    ),
}


def guarded(method: Callable[_P, Any]) -> Callable[_P, Reply]:
    """Wrap a bridge method so it always answers, and answers in one shape.

    The bare ``except Exception`` is the point of the decorator, not an oversight.
    This is the outermost frame of a call that started in a web page, and the
    alternative is not "the exception propagates" -- pywebview catches it and
    assigns ``{isError: true, value: {message, name, stack}}`` into the page
    (webview/util.py:244-248). That ``stack`` is a full traceback: absolute paths,
    module names, local variable names, in a document the renderer can read. The
    traceback belongs in the audit log, where it is useful and not reachable.
    """

    @functools.wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> Reply:
        try:
            return _ok(method(*args, **kwargs))
        except BridgeError as exc:
            _log.info("bridge %s refused: %s (%s)", method.__name__, exc.en, exc.code)
            return _fail(exc.code, exc.vi, exc.en)
        except PlanError as exc:
            code, vi, en = _PLAN_ERRORS.get(type(exc), _PLAN_ERRORS[UnknownPlanError])
            _log.info("bridge %s refused: %s", method.__name__, en)
            return _fail(code, vi, en)
        except vss.TokenError as exc:
            code, vi, en = _ACTION_ERRORS.get(type(exc), _ACTION_ERRORS[vss.UnknownActionError])
            _log.info("bridge %s refused: %s", method.__name__, en)
            return _fail(code, vi, en)
        except win.UnsupportedPlatformError as exc:
            return _fail("unsupported", f"Không hỗ trợ trên hệ này: {exc}", str(exc))
        except Exception as exc:
            _log.exception("bridge %s failed", method.__name__)
            return _fail(
                "internal",
                f"Lỗi nội bộ: {type(exc).__name__}. Chi tiết đã ghi vào log.",
                f"Internal error: {type(exc).__name__}. Details are in the log.",
            )

    return wrapper


# ---------------------------------------------------------------------------
# Coercion. Everything below assumes the argument came from JavaScript and is
# therefore whatever the page felt like sending.
# ---------------------------------------------------------------------------
def _as_ids(raw: object, *, what: str, limit: int = MAX_SELECTION) -> list[str]:
    """A list of non-empty strings, deduplicated, order kept, length bounded.

    A bare string is one id, not a list of characters -- ``"npm_cache"`` from a
    single-row action must not become sixty-three ids. Non-strings are dropped
    rather than stringified: ``str(None)`` would become a plausible-looking id.

    *limit* is the caller's, because "too many" depends on what the ids are for:
    the catalogue is forty-nine rows, while a sweep shows up to
    :data:`adc.engine.sweeper.TOP_ROWS` of them and a Select-all on that table is
    a legitimate click.
    """
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list | tuple):
        raise BridgeError(
            "bad_input",
            f"{what} phải là một danh sách id.",
            f"{what} must be a list of ids.",
        )
    seen: dict[str, None] = {}
    for item in items:
        if isinstance(item, str) and item:
            seen[item] = None
    if len(seen) > limit:
        raise BridgeError(
            "bad_input",
            f"Chọn quá nhiều mục ({len(seen)}); tối đa {limit}.",
            f"Too many items selected ({len(seen)}); the maximum is {limit}.",
        )
    return list(seen)


def _as_flag(raw: object) -> bool:
    """Strictly ``True``. ``"false"``, ``0`` and ``[]`` are all not-true here.

    Deliberately not ``bool(raw)``: the strings a page can produce include
    ``"false"``, which is truthy, and this flag gates the dangerous rows.
    """
    return raw is True


def _as_job_id(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise BridgeError("bad_input", "Thiếu mã job.", "No job id.")
    return raw


def _as_since(raw: object) -> int:
    """A non-negative event cursor. Junk means "send me everything"."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(0, raw)


def _as_node_id(raw: object) -> str:
    """A handle the engine minted, checked for *shape* before it is looked up.

    The lookup in :meth:`Bridge._node_path` is what actually decides whether the
    handle is real; this only refuses the arguments that could never be one, so a
    page bug reads ``bad_input`` instead of ``unknown_node``. Hex digits and a
    bounded length -- :func:`adc.engine.explorer._node_id` mints twelve of them --
    which also means nothing that arrives here can look like a path.
    """
    if not isinstance(raw, str) or not raw:
        raise BridgeError("bad_input", "Thiếu mã thư mục.", "No folder handle.")
    if len(raw) > 32 or not all(char in "0123456789abcdef" for char in raw):
        raise BridgeError("bad_input", "Mã thư mục không hợp lệ.", "Malformed folder handle.")
    return raw


def _as_volume_id(raw: object) -> str:
    """A drive letter, normalised the same way :mod:`adc.engine.settings` does.

    ``"C"``, ``"c:"`` and ``"C:\\"`` are all the C volume; anything else is not a
    volume id at all. The caller still checks it against the volumes this machine
    actually has -- a well-formed letter for a drive that is not there is
    ``not_present``, not ``bad_input``.
    """
    if not isinstance(raw, str):
        raise BridgeError("bad_input", "Thiếu ổ đĩa.", "No volume id.")
    letter = raw.strip().rstrip(":\\/").upper()
    if len(letter) != 1 or not ("A" <= letter <= "Z"):
        raise BridgeError("bad_input", f"Ổ đĩa không hợp lệ: {raw!r}", f"Not a volume id: {raw!r}")
    return letter


#: Longest root path the sweep's folder box will accept. Windows' own limits are
#: 260 for a normal path and 32 767 for an extended one; this is here only so a
#: page bug cannot ask the engine to normalise a megabyte-long string.
MAX_ROOT_LEN: Final = 1024

#: Characters a Windows path cannot contain. ``*`` and ``?`` are on the list not
#: because they are dangerous but because they are a misunderstanding worth
#: naming: the sweep takes one directory, never a glob.
_BAD_IN_PATH: Final = frozenset('<>"|?*')


def _as_root(raw: object) -> str:
    r"""The one place a path is allowed *into* the engine, and what that costs.

    Every other method takes an id or a handle, because v1 accepted a path from
    the page and handed it to the shell (docs/01-AUDIT.md SEC-02). The sweep
    cannot: SPEC 6.3 has the user pick the folder their projects live in, and
    there is no handle for a folder the engine has never shown them.

    So the path is fenced rather than refused. This checks *shape* only --
    drive-qualified (``E:\PROJECT``) or UNC (``\\server\share``), no wildcards, no
    control characters, bounded length. Three things stand between the result and
    a deletion: :func:`~adc.engine.explorer.resolve_root` requires it to exist and
    be a directory, :func:`~adc.engine.sweeper.guard_for_root` refuses a volume
    root and every protected system subtree before the walk starts, and the delete
    still travels as handles the sweep itself minted, checked against a guard
    rooted here. A page can name a tree to look in; it still cannot name a folder
    to delete.

    Residual, stated rather than hidden: a page can learn whether a directory
    exists from the difference between ``not_present`` and a job starting. That is
    the whole of what this door buys it.
    """
    if not isinstance(raw, str):
        raise BridgeError("bad_input", "Chưa chọn thư mục gốc.", "No root folder given.")
    # Explorer's "Copy as path" quotes what it puts on the clipboard, and pasting
    # that in is the most likely way this box gets filled.
    value = raw.strip().strip('"').strip()
    if not value:
        raise BridgeError("bad_input", "Chưa chọn thư mục gốc.", "No root folder given.")
    if len(value) > MAX_ROOT_LEN:
        raise BridgeError(
            "bad_input",
            f"Đường dẫn quá dài (tối đa {MAX_ROOT_LEN} ký tự).",
            f"That path is too long (the maximum is {MAX_ROOT_LEN} characters).",
        )
    if any(char in _BAD_IN_PATH or ord(char) < 32 for char in value):
        raise BridgeError(
            "bad_input",
            "Đường dẫn chứa ký tự không hợp lệ. Hãy chọn đúng một thư mục.",
            "That path contains characters a folder name cannot. Pick one folder.",
        )
    if not os.path.splitdrive(value)[0]:
        raise BridgeError(
            "bad_input",
            "Cần đường dẫn đầy đủ, ví dụ E:\\PROJECT.",
            r"A full path is required, for example E:\PROJECT.",
        )
    return os.path.abspath(value)


def _as_limit(raw: object, *, default: int, ceiling: int) -> int:
    """A positive row count, bounded. Junk means *default*.

    JavaScript has one number type, so a page that means 25 may send ``25.0``;
    a float that is integral is accepted rather than refused, because refusing it
    would be an error the page cannot see the cause of.
    """
    if isinstance(raw, bool) or raw is None:
        return default
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if not isinstance(raw, int) or raw <= 0:
        return default
    return min(raw, ceiling)


def _open_folder(folder: Path) -> None:
    """Show *folder* in Explorer.

    A module-level function so a test can replace it without a real window
    appearing. ``os.startfile`` on a *directory* opens Explorer; the caller has
    already reduced a file to its parent, because ``startfile`` on a file would
    *run* it -- and several catalogue rows resolve to installers.
    """
    if not win.IS_WINDOWS:  # pragma: no cover - the shell is Windows-only
        raise BridgeError(
            "unsupported",
            "Chỉ mở được thư mục trên Windows.",
            "Opening a folder is Windows-only.",
        )
    os.startfile(folder)  # noqa: S606 - a directory, and startfile is the shell verb


class Bridge:
    """``window.pywebview.api`` on the JS side. One instance per window.

    Holds the job runner, the plan store, and the per-job payloads the poll
    endpoint merges into its answer -- :class:`~adc.engine.jobs.Job` carries
    progress and log lines but knows nothing about scan rows or plan items, and
    :class:`~adc.engine.jobs.JobRunner` discards the worker's return value.

    Every method that starts work goes through the runner, which permits one live
    job at a time. That is a UI fact as much as a disk one: there is a single
    progress bar, and two concurrent walks of the same disk are slower than one.
    """

    def __init__(
        self,
        *,
        runner: JobRunner | None = None,
        store: PlanStore | None = None,
        sweeps: sweeper.SweepPlanStore | None = None,
        actions: ActionStore | None = None,
        vss_runner: vss.Runner | None = None,
        on_relaunch: Callable[[], None] | None = None,
    ) -> None:
        self._runner = runner or JobRunner()
        self._store = store or plan_store()
        # The sweep's tokens live in their own store, not the cleaner's: they are
        # redeemed by a different function, they carry a different plan type, and a
        # sweep token accepted by ``clean_execute`` would be a category error the
        # type checker cannot see through a shared dict.
        self._sweep_store = sweeps or sweeper.sweep_plan_store()
        self._actions = actions or vss.action_store()
        # Every vssadmin command this bridge causes goes through here. ``None`` is
        # the real thing; a test passes its own so that proving "apply runs the
        # approved argv" does not resize this machine's shadow storage to prove it.
        self._vss_runner = vss_runner
        # Set by the window to its own close, so the bridge can ask for a
        # shutdown after a successful elevation without importing ``webview``.
        self._on_relaunch = on_relaunch
        self._lock = threading.Lock()
        self._scans: dict[str, scanner.ScanRun] = {}
        self._plans: dict[str, Plan] = {}
        self._reports: dict[str, dict[str, Any]] = {}
        # Explore levels, newest last. These double as the ``node_id`` -> path
        # table: a handle only reaches the page as part of a level, so the level
        # that minted it is the right thing to ask, and it lives exactly as long as
        # its job does. No separate registry to keep in step, and nothing to expire.
        self._explores: dict[str, explorer.ExploreRun] = {}
        # Sweep scans, and the previews their handles were ticked into. Both keyed
        # by job id and both dropped by ``_forget_locked`` when the runner evicts
        # the job, for the same reason the explore levels are: a handle is only
        # meaningful while the table it came from is still the one on screen.
        self._sweeps: dict[str, sweeper.SweepRun] = {}
        self._sweep_plans: dict[str, sweeper.SweepPlan] = {}

    # -- internals ---------------------------------------------------------
    @property
    def runner(self) -> JobRunner:
        """For the window's shutdown path and for tests. Not reachable from JS.

        pywebview only exposes attributes that do not start with an underscore
        *and* are callable, so a property is not part of the JS surface.
        """
        return self._runner

    def _busy(self) -> BridgeError:
        active = self._runner.active()
        which = active.kind.value if active is not None else "job"
        return BridgeError(
            "busy",
            f"Đang chạy một việc khác ({which}). Hãy đợi hoặc bấm Dừng.",
            f"Another job is running ({which}). Wait for it, or press Stop.",
        )

    def _job(self, job_id: str) -> Job:
        job = self._runner.get(job_id)
        if job is None:
            # The runner keeps the last twenty jobs, so this is a poll for
            # something evicted or from a previous process -- not a crash.
            raise BridgeError(
                "no_such_job",
                "Không còn dữ liệu cho việc này.",
                "That job is no longer available.",
            )
        return job

    def _known_ids(self, raw: object, *, what: str) -> list[str]:
        """Ids that exist in the catalogue. An unknown one is refused here.

        The engine would also survive an unknown id -- ``scanner`` marks the row
        ``error`` and ``cleaner`` emits a skipped row -- but a bad id at this
        boundary means the page and the catalogue are out of step, and saying so
        is more useful than a silent row.
        """
        wanted = _as_ids(raw, what=what)
        known = set(targets.ids())
        strays = [i for i in wanted if i not in known]
        if strays:
            raise BridgeError(
                "unknown_target",
                f"Không có mục: {', '.join(strays[:5])}",
                f"No such target: {', '.join(strays[:5])}",
            )
        return wanted

    def _remember(self, job_id: str, *, scan: scanner.ScanRun | None = None,
                  plan: Plan | None = None,
                  level: explorer.ExploreRun | None = None,
                  sweep: sweeper.SweepRun | None = None,
                  sweep_plan: sweeper.SweepPlan | None = None) -> None:
        with self._lock:
            if scan is not None:
                self._scans[job_id] = scan
            if plan is not None:
                self._plans[job_id] = plan
            if level is not None:
                self._explores[job_id] = level
            if sweep is not None:
                self._sweeps[job_id] = sweep
            if sweep_plan is not None:
                self._sweep_plans[job_id] = sweep_plan
            self._forget_locked()

    def _forget_locked(self) -> None:
        """Drop payloads whose job the runner has already evicted.

        Without this the dicts outlive the jobs they describe, and a long session
        would hold every scan's rows forever.
        """
        live = {job.id for job in self._runner}
        for holder in (
            self._scans,
            self._plans,
            self._reports,
            self._explores,
            self._sweeps,
            self._sweep_plans,
        ):
            for job_id in [k for k in holder if k not in live]:
                del holder[job_id]

    def _node_path(self, node_id: str) -> str:
        """Turn a handle from the page back into a path, or refuse it.

        Newest level first: drilling re-mints the crumb chain on every level, so the
        live level is the one whose handles the page is actually looking at, and an
        id from a level the runner has already evicted is genuinely gone rather than
        ambiguous.

        This lookup is the whole of SEC-02's fix for this view. The page cannot name
        a folder; it can only hand back something the engine minted for a folder it
        was already shown.
        """
        with self._lock:
            levels = list(self._explores.values())
        for level in reversed(levels):
            found = level.path_for(node_id)
            if found is not None:
                return found
        raise BridgeError(
            "unknown_node",
            "Không còn dữ liệu cho thư mục này. Hãy quét lại.",
            "That folder is no longer in view. Scan again.",
        )

    def _sweep_run(self, job_id: str) -> sweeper.SweepRun:
        """The sweep a page's table belongs to, named by its job id.

        Named rather than inferred, which is the difference that matters here: the
        explorer can resolve a handle against whichever level is newest because the
        answer is only ever "open this folder", but a sweep handle resolves into a
        *deletion*, and every plan is rooted at the run's own root. Guessing the
        run would mean a stale table's ticks could be re-resolved under a different
        root -- so the page says which sweep it is looking at, and a table older
        than the runner's memory is refused instead.
        """
        with self._lock:
            found = self._sweeps.get(job_id)
        if found is None:
            raise BridgeError(
                "no_such_sweep",
                "Không còn dữ liệu cho lần quét này. Hãy quét lại.",
                "That sweep is no longer available. Sweep again.",
            )
        return found

    def _sweep_path(self, node_id: str) -> str:
        """A sweep handle back to a path, for Explorer only. Newest run first.

        Separate from :meth:`_node_path` because the two registries mint their own
        handles: an explore level knows nothing about a sweep's rows, and a sweep
        knows nothing about a level's. Newest first is safe *here* -- unlike in
        :meth:`_sweep_run` -- because the outcome is a window, not a delete.
        """
        with self._lock:
            runs = list(self._sweeps.values())
        for run in reversed(runs):
            found = run.path_for(node_id)
            if found is not None:
                return found
        raise BridgeError(
            "unknown_node",
            "Không còn dữ liệu cho thư mục này. Hãy quét lại.",
            "That folder is no longer in view. Sweep again.",
        )

    def _root_refused(self, root: str, exc: GuardError) -> BridgeError:
        """A root the deleter would never accept, phrased for the folder box.

        The guard's own message is English-only and names internals, so it goes to
        the log and the page gets the reason in both languages. Both halves of the
        sweep raise this: the walk refuses it up front, and the preview refuses it
        again if the environment shifted underneath.
        """
        _log.info("bridge sweep: root refused: %s", exc)
        return BridgeError(
            "blocked_root",
            f"Không thể quét dọn trong {root}: đây là gốc ổ đĩa hoặc thư mục được bảo vệ.",
            f"{root} cannot be swept: it is a volume root or a protected folder.",
        )

    def _report_for(self, job: Job) -> dict[str, Any] | None:
        """The clean's receipt, once it exists on disk.

        ``JobRunner.submit`` discards the worker's return value, so the report
        body is read back from the file ``run_clean`` wrote rather than handed
        over in memory. A miss is not cached: the job is marked terminal a few
        lines *before* the report is written (cleaner.py:711-723), so a poll can
        legitimately land in that window and the next tick will find it.
        """
        if job.state not in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            return None
        with self._lock:
            cached = self._reports.get(job.id)
        if cached is not None:
            return cached
        body = report.load_report(job.id)
        if body is not None:
            with self._lock:
                self._reports[job.id] = body
        return body

    def _partial_for(self, job: Job) -> dict[str, Any] | None:
        """What this job has produced so far, in the shape its view expects.

        Scan rows are read live off the :class:`~adc.engine.scanner.ScanRun` the
        worker is still filling in, which is what makes the table grow during a
        scan instead of appearing at the end. An explore level is the same idea:
        its rows exist from the listing phase, so the folder list is on screen
        while the sizes are still arriving.

        The two sweep arms are distinguished by which registry holds the job, not
        by ``job.kind``: a sweep and its delete are both ``JobKind.SWEEP`` on
        purpose (the History view groups them together), so the kind alone cannot
        tell the page whether it is watching a walk or a deletion.
        """
        with self._lock:
            run = self._scans.get(job.id)
            approved = self._plans.get(job.id)
            level = self._explores.get(job.id)
            swept = self._sweeps.get(job.id)
            sweep_plan = self._sweep_plans.get(job.id)
        if run is not None:
            return {"kind": "scan", **run.as_dict()}
        if level is not None:
            return {"kind": "explore", **level.as_dict()}
        if swept is not None:
            return {"kind": "sweep", **swept.as_dict()}
        if sweep_plan is not None:
            return {
                "kind": "sweep_delete",
                "plan": sweep_plan.as_dict(),
                "report": self._report_for(job),
            }
        if approved is not None:
            return {"kind": "clean", "plan": approved.as_dict(), "report": self._report_for(job)}
        return None

    def _volume_total(self, letter: str) -> int:
        """The size of *letter*, or ``not_present`` if this machine has no such volume.

        Refusing early matters more here than elsewhere: a percentage limit cannot
        be turned into bytes without the volume's size, and a resize whose byte
        value is unknown cannot be compared with the current ceiling -- so it would
        be presented as "not a shrink" and skip the typed confirmation.
        """
        for vol in volumes_mod.fixed_volumes():
            if vol.letter.upper() == letter:
                return vol.total
        raise BridgeError(
            "not_present",
            f"Không có ổ {letter}: trên máy này.",
            f"There is no {letter}: volume on this machine.",
        )

    def _vss_reading(self) -> vss.VssState:
        """The live shadow-copy state, or a refusal the screen can act on.

        A preview has to be built from what the machine says now -- which volume
        the copies are really kept on, and what the ceiling currently is -- so this
        read is not a convenience for the page. When it cannot be taken there is
        nothing to preview, which is a different answer from
        :meth:`vss_status`, where "cannot read" is the thing being displayed.
        """
        reading = vss.state(runner=self._vss_runner)
        if not reading.supported:
            raise BridgeError(
                "unsupported",
                f"Không đọc được Volume Shadow Copy: {reading.error}",
                str(reading.error or "shadow copies are not available here"),
            )
        if not reading.is_admin:
            raise BridgeError(
                "need_admin",
                "Cần quyền Administrator để quản lý shadow copy.",
                "Managing shadow copies requires Administrator.",
            )
        return reading

    def _vss_check(self, pending: PendingAction) -> None:
        """Refuse a preview vssadmin would not accept, with its own reason.

        :meth:`_vss_reading` has already ruled out the two ordinary causes, so this
        is the narrow case where the tool itself refused between the read and the
        preview. The receipt is not stored in that case, so there is nothing to
        clean up -- but the page must not be handed a token-shaped dict either.
        """
        if pending.ok:
            return
        reason = pending.preview.reason or "vssadmin refused the command"
        raise BridgeError("vss_refused", f"Không thực hiện được: {reason}", reason)

    def _vss_phrase(self, pending: PendingAction, typed: object) -> None:
        """Refuse a wrong confirmation phrase, before the token is spent.

        SPEC 4.3 asks for a typed phrase on the DANGEROUS tier; the engine decides
        what it is and whether this receipt needs one. A typo has to cost a retry
        and not the preview, which is why this runs before
        :meth:`~adc.engine.vss.ActionStore.spend` and not inside it.
        """
        if pending.matches(typed):
            return
        raise BridgeError(
            "vss_phrase",
            f"Hãy gõ đúng {pending.phrase} để xác nhận.",
            f"Type {pending.phrase} exactly to confirm.",
        )

    # -- the JS surface ----------------------------------------------------
    @guarded
    def catalog(self) -> dict[str, Any]:
        """Every row and every preset, for the page to render. Resolves nothing.

        Called once at startup. Sizes are absent by design -- finding out what is
        on this machine is what a scan is for, and doing it during window creation
        would block the first paint on a disk walk.
        """
        return {
            "targets": [row.as_dict() for row in targets.catalog()],
            "presets": {
                name: [row.id for row in targets.preset(name)] for name in targets.PRESETS
            },
            "schedulable": [row.id for row in targets.schedulable()],
        }

    @guarded
    def volumes(self) -> dict[str, Any]:
        """Fixed volumes with live free space, for the disk cards."""
        return {"volumes": [vol.as_dict() for vol in volumes_mod.fixed_volumes()]}

    @guarded
    def settings_get(self) -> dict[str, Any]:
        """What is in force, plus the defaults so the page can offer a reset."""
        return {
            "settings": settings_mod.load().as_dict(),
            "defaults": settings_mod.DEFAULTS.as_dict(),
        }

    @guarded
    def settings_set(self, changes: object = None) -> dict[str, Any]:
        """Merge *changes*, write, and answer with what is now in force.

        Not :func:`adc.engine.settings.update`, which discards ``save``'s return
        value: a profile that is not writable is a real state on a managed machine
        and the page has to be able to say "this will not survive a restart".
        Validation is still the engine's -- :meth:`Settings.patched` ignores an
        unknown key so a newer page against an older engine degrades.
        """
        if not isinstance(changes, dict):
            raise BridgeError(
                "bad_input",
                "Thiếu thay đổi cần lưu.",
                "No settings changes were sent.",
            )
        merged = settings_mod.load().patched(changes)
        persisted = settings_mod.save(merged)
        if not persisted:
            _log.warning("settings not persisted: profile directory is not writable")
        return {"settings": merged.as_dict(), "persisted": persisted}

    @guarded
    def scan_start(self, volume_ids: object = None, target_ids: object = None) -> dict[str, Any]:
        """Measure *target_ids*, and answer with the job id to poll.

        *volume_ids* is recorded on the run's settings, reported back per row in
        ``by_volume``, and narrows the walk: a resolved path on a volume the user
        did not tick is dropped, and a row left with no paths at all is reported
        as unavailable with the filter as its reason. Ticking nothing means every
        volume, which is what the Settings page says a cleared group means.

        The filter lands on resolved paths rather than on targets because a row can
        span disks -- ``RecycleBins`` returns one ``$Recycle.Bin`` per fixed volume
        (BUG-02's neighbour), so "scan C: only" has to be able to keep half a row.

        ``volume_ids=None`` means "whatever the user saved", not "every volume": a
        page that omits the argument must not quietly widen the scan past the tick
        list on the Settings screen. Sending a list -- including an empty one --
        overrides the saved choice for this one scan.
        """
        wanted = self._known_ids(target_ids, what="target_ids")
        if not wanted:
            raise BridgeError(
                "empty_selection",
                "Chưa chọn mục nào để quét.",
                "Nothing selected to scan.",
            )
        override = {} if volume_ids is None else {"volume_ids": volume_ids}
        active: Settings = settings_mod.load().patched(override)
        try:
            run = scanner.submit_scan(self._runner, target_ids=wanted, settings=active)
        except win.UnsupportedPlatformError:
            raise
        except RuntimeError as exc:
            raise self._busy() from exc
        self._remember(run.job.id, scan=run)
        _log.info("bridge scan_start: job=%s targets=%d", run.job.id, len(wanted))
        return {"job_id": run.job.id, "target_ids": wanted, "volume_ids": list(active.volume_ids)}

    @guarded
    def job_poll(self, job_id: object = None, since_event: object = 0) -> dict[str, Any]:
        """Progress, new log lines, and whatever the job has produced so far.

        Polled every 250 ms (SPEC 3.1) -- there is no streaming, because a push
        channel from a worker thread into the renderer is a second concurrency
        problem for no gain at human timescales.

        *since_event* is a cursor, not a page number: pass back ``next_event`` and
        the same lines never arrive twice. ``event_count`` is the total the job
        holds, so the page can tell "caught up" from "more waiting".
        """
        job = self._job(_as_job_id(job_id))
        since = _as_since(since_event)
        snapshot = job.snapshot(since_event=since)
        events = snapshot["events"][:MAX_EVENTS]
        snapshot["events"] = events
        snapshot["next_event"] = since + len(events)
        snapshot["phase"] = snapshot["current_phase"]
        snapshot["done"] = job.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)
        snapshot["partial_results"] = self._partial_for(job)
        return snapshot

    @guarded
    def job_cancel(self, job_id: object = None) -> dict[str, Any]:
        """Ask a job to stop. Returns immediately; the job ends on its own thread.

        Cancelling is cooperative and idempotent: the worker checks the token
        between rows and inside its walks, so a cancel lands within a directory
        rather than instantly, and what was already deleted stays deleted -- the
        report says so.
        """
        target = _as_job_id(job_id)
        found = self._runner.cancel(target)
        if not found:
            raise BridgeError(
                "no_such_job",
                "Không còn dữ liệu cho việc này.",
                "That job is no longer available.",
            )
        _log.info("bridge job_cancel: job=%s", target)
        return {"job_id": target, "cancel_requested": True}

    @guarded
    def explore_start(self, volume_id: object = None, node_id: object = None) -> dict[str, Any]:
        r"""Scan one level of the disk: a volume root, or a folder already on screen.

        Exactly one argument. *volume_id* is a drive letter and starts a fresh
        chain at that volume's root; *node_id* is a handle from a level the page is
        already looking at -- a row to drill into, or a crumb to go back up to.
        There is no third way in, and in particular no path: the page can only ask
        for a folder the engine has already shown it (docs/01-AUDIT.md SEC-02).

        The answer carries the level's identity -- root, volume, crumbs -- before
        the first poll, so the header and breadcrumb bar draw immediately instead
        of after a 250 ms tick. The rows arrive through
        :meth:`job_poll`'s ``partial_results``.
        """
        if (volume_id is None) == (node_id is None):
            raise BridgeError(
                "bad_input",
                "Cần đúng một trong hai: ổ đĩa hoặc thư mục.",
                "Expected exactly one of volume_id or node_id.",
            )
        if volume_id is not None:
            letter = _as_volume_id(volume_id)
            found = next(
                (vol for vol in volumes_mod.fixed_volumes() if vol.letter == letter), None
            )
            if found is None:
                raise BridgeError(
                    "not_present",
                    f"Không có ổ {letter}: trên máy này.",
                    f"There is no {letter}: volume on this machine.",
                )
            root = found.root
        else:
            root = self._node_path(_as_node_id(node_id))

        active: Settings = settings_mod.load()
        try:
            level = explorer.submit_explore(self._runner, root=root, settings=active)
        except NotADirectoryError as exc:
            # A file's handle, which the UI should not have offered. Not a crash.
            raise BridgeError(
                "bad_input",
                "Đây là một file, không phải thư mục.",
                "That is a file, not a folder.",
            ) from exc
        except OSError as exc:
            raise BridgeError(
                "not_present",
                "Thư mục này không còn tồn tại.",
                "That folder no longer exists.",
            ) from exc
        except RuntimeError as exc:
            raise self._busy() from exc
        self._remember(level.job.id, level=level)
        _log.info("bridge explore_start: job=%s root=%s", level.job.id, level.root)
        body = level.as_dict()
        return {
            "job_id": level.job.id,
            "root": body["root"],
            "name": body["name"],
            "volume": body["volume"],
            "node_id": body["node_id"],
            "parent_id": body["parent_id"],
            "crumbs": body["crumbs"],
        }

    @guarded
    def explore_reveal(self, node_id: object = None) -> dict[str, Any]:
        """Open a row from the explorer in Explorer. Takes a handle, never a path.

        The same asymmetry :meth:`reveal` documents, one level down: the handle is
        resolved by the level that minted it, and a file is reduced to its parent
        because ``os.startfile`` on a file would *run* it.
        """
        found = Path(self._node_path(_as_node_id(node_id)))
        folder = found if found.is_dir() else found.parent
        if not folder.is_dir():
            raise BridgeError(
                "not_present",
                "Vị trí này không còn tồn tại.",
                "That location no longer exists.",
            )
        _open_folder(folder)
        _log.info("bridge explore_reveal: opened=%s", folder)
        return {"node_id": node_id, "opened": str(folder)}

    @guarded
    def clean_plan(
        self, selection: object = None, allow_dangerous: object = False
    ) -> dict[str, Any]:
        """Dry-run *selection* and answer with the preview and its token.

        Always before a clean, never optional: this is the call that turns a
        checkbox list into a number the user can approve, and the token it mints
        is the only thing :meth:`clean_execute` accepts.

        Runs on pywebview's own call thread (webview/util.py:239), so the 120 s
        worst case does not freeze the window. It still takes the busy check --
        two walks of one disk are slower than one, and a preview measured against
        a disk that a clean is actively changing would be out of date on arrival.

        *allow_dangerous* is the second half of the confirmation handshake: the
        first preview reports ``dangerous_ids`` and skips those rows, the page
        confirms them explicitly, and the second preview includes them.
        """
        wanted = self._known_ids(selection, what="selection")
        if not wanted:
            raise BridgeError(
                "empty_selection",
                "Chưa chọn mục nào để dọn.",
                "Nothing selected to clean.",
            )
        active = self._runner.active()
        if active is not None and active.state is JobState.RUNNING:
            raise self._busy()
        preview = build_plan(
            wanted,
            settings=settings_mod.load(),
            allow_dangerous=_as_flag(allow_dangerous),
            store=self._store,
        )
        _log.info(
            "bridge clean_plan: token=%s runnable=%d est=%d dangerous=%d",
            preview.token[:8],
            len(preview.runnable),
            preview.est_total,
            len(preview.dangerous_ids),
        )
        return preview.as_dict()

    @guarded
    def clean_execute(self, plan_token: object = None) -> dict[str, Any]:
        """Redeem a token and start the clean. The only call that deletes anything.

        Takes a token and nothing else -- no ids, no paths, no flags. The peek is
        for the poll payload only; the store is still what decides whether the
        token is good, so an unknown, expired or already-spent token is refused by
        :meth:`PlanStore.spend` rather than by a guess made here.
        """
        token = plan_token
        if not isinstance(token, str) or not token:
            raise UnknownPlanError("no plan token")
        approved = self._store.peek(token)
        try:
            job = submit_clean(self._runner, token=token, store=self._store)
        except win.UnsupportedPlatformError:
            raise
        except RuntimeError as exc:
            raise self._busy() from exc
        if approved is not None:
            self._remember(job.id, plan=approved)
        _log.info("bridge clean_execute: job=%s token=%s", job.id, token[:8])
        return {"job_id": job.id}

    @guarded
    def sweep_defaults(self) -> dict[str, Any]:
        r"""What the sweep page puts in its two controls before anything runs.

        A read, not a start. SPEC 6.3 wants the folder box pre-filled with a
        plausible root (``E:\PROJECT`` on this machine) and the age filter with
        thirty days, and the page should not hardcode either -- the suggestion
        depends on which volumes exist. ``root`` is ``None`` when nothing obvious is
        there, which the view shows as an empty box: offering a root that cannot be
        swept is worse than offering none.
        """
        return {
            "root": sweeper.default_root(),
            "min_age_days": sweeper.DEFAULT_MIN_AGE_DAYS,
            "max_min_age_days": sweeper.MAX_MIN_AGE_DAYS,
            "categories": list(sweeper.CATEGORIES),
            "top_rows": sweeper.TOP_ROWS,
            "max_selection": sweeper.MAX_PLAN_ITEMS,
        }

    @guarded
    def sweep_start(self, root: object = None, min_age_days: object = None) -> dict[str, Any]:
        """Walk *root* for regenerable project directories nobody has touched.

        The one method that accepts a path -- :func:`_as_root` is the fence around
        it and says why SPEC 6.3 leaves no alternative. ``None`` means "use the
        suggestion", so a page whose box the user never edited sends back exactly
        what :meth:`sweep_defaults` gave it.

        The guard is built here, before the job exists, rather than left to the
        preview: a root that can be listed but never deleted from is otherwise a
        five-minute walk ending in a refusal, and ``blocked_root`` now arrives while
        the user is still looking at the box they typed it into.

        The answer carries the root's identity so the header draws before the first
        poll. Rows arrive through :meth:`job_poll`'s ``partial_results``, project by
        project and only once each project has been dated -- never between the two,
        because a row on screen for a quarter of a second is long enough to tick.
        """
        resolved = sweeper.default_root() if root is None else _as_root(root)
        if not resolved:
            raise BridgeError(
                "no_root",
                "Chưa chọn thư mục gốc để quét.",
                "No root folder to sweep. Choose one.",
            )
        days = sweeper.clamp_min_age_days(min_age_days)
        active: Settings = settings_mod.load()
        try:
            sweeper.guard_for_root(resolved, active)
        except GuardError as exc:
            raise self._root_refused(resolved, exc) from exc
        try:
            run = sweeper.submit_sweep(
                self._runner, root=resolved, settings=active, min_age_days=days
            )
        except NotADirectoryError as exc:
            raise BridgeError(
                "bad_input",
                "Đây là một file, không phải thư mục.",
                "That is a file, not a folder.",
            ) from exc
        except OSError as exc:
            raise BridgeError(
                "not_present",
                "Thư mục này không tồn tại.",
                "That folder does not exist.",
            ) from exc
        except win.UnsupportedPlatformError:
            raise
        except RuntimeError as exc:
            raise self._busy() from exc
        self._remember(run.job.id, sweep=run)
        _log.info("bridge sweep_start: job=%s root=%s days=%d", run.job.id, run.root, days)
        body = run.as_dict()
        return {
            "job_id": run.job.id,
            "root": body["root"],
            "name": body["name"],
            "volume": body["volume"],
            "min_age_days": days,
        }

    @guarded
    def sweep_plan(self, job_id: object = None, node_ids: object = None) -> dict[str, Any]:
        """Dry-run one sweep's ticked rows; answer with the preview and its token.

        *job_id* names the sweep the ticks came from, *node_ids* are handles that
        sweep minted, and there is no third argument. No path, no category, no
        "everything" flag: a directory the engine did not itself find, prove and
        date cannot be reached from here at all.

        Runs on pywebview's call thread like :meth:`clean_plan`, and takes the same
        busy check for the same two reasons -- one disk, and a preview measured
        against a tree another job is changing is out of date on arrival.
        """
        run = self._sweep_run(_as_job_id(job_id))
        wanted = [
            _as_node_id(one)
            for one in _as_ids(node_ids, what="selection", limit=sweeper.MAX_PLAN_ITEMS)
        ]
        if not wanted:
            raise BridgeError(
                "empty_selection",
                "Chưa chọn thư mục nào để xoá.",
                "Nothing ticked to delete.",
            )
        active = self._runner.active()
        if active is not None and active.state is JobState.RUNNING:
            raise self._busy()
        try:
            preview = sweeper.plan_delete(
                run, wanted, settings=settings_mod.load(), store=self._sweep_store
            )
        except GuardError as exc:
            raise self._root_refused(run.root, exc) from exc
        _log.info(
            "bridge sweep_plan: token=%s runnable=%d est=%d skipped=%d",
            preview.token[:8],
            len(preview.runnable),
            preview.est_total,
            len(preview.items) - len(preview.runnable),
        )
        return preview.as_dict()

    @guarded
    def sweep_execute(self, plan_token: object = None) -> dict[str, Any]:
        """Redeem a sweep preview and start deleting. A token, and nothing else.

        The same handshake as :meth:`clean_execute` and for the same reason: what
        goes was decided when the preview was taken, so a page cannot widen the
        selection after the user has approved a number.

        There is no ``allow_dangerous`` counterpart, because there is nothing to
        grade -- every row is a directory tree removed outright, the Recycle Bin is
        not involved at this size, and the plan says so with ``has_irreversible``.
        Getting that confirmed is the dialog's job, not this method's.
        """
        token = plan_token
        if not isinstance(token, str) or not token:
            raise UnknownPlanError("no sweep plan token")
        approved = self._sweep_store.peek(token)
        try:
            job = sweeper.submit_delete(self._runner, token=token, store=self._sweep_store)
        except win.UnsupportedPlatformError:
            raise
        except RuntimeError as exc:
            raise self._busy() from exc
        if approved is not None:
            self._remember(job.id, sweep_plan=approved)
        _log.info("bridge sweep_execute: job=%s token=%s", job.id, token[:8])
        return {"job_id": job.id}

    @guarded
    def sweep_reveal(self, node_id: object = None) -> dict[str, Any]:
        """Open a sweep row in Explorer, so the user can look before they delete.

        The same asymmetry :meth:`reveal` documents: a handle goes in, a path comes
        back out. Resolved against the sweep runs rather than the explore levels,
        because the two mint their own handles and neither knows the other's.
        """
        found = Path(self._sweep_path(_as_node_id(node_id)))
        folder = found if found.is_dir() else found.parent
        if not folder.is_dir():
            raise BridgeError(
                "not_present",
                "Vị trí này không còn tồn tại.",
                "That location no longer exists.",
            )
        _open_folder(folder)
        _log.info("bridge sweep_reveal: opened=%s", folder)
        return {"node_id": node_id, "opened": str(folder)}

    @guarded
    def reveal(self, target_id: object = None) -> dict[str, Any]:
        """Open a target's folder in Explorer. Takes an id, never a path.

        This is the method v1 got wrong: it accepted a path from the page and
        handed it to the shell, which made every folder on the machine reachable
        from JavaScript (docs/01-AUDIT.md SEC-02). Here the page names a
        catalogue row, the row's own resolver says where it is, and the answer to
        an id that is not in the catalogue is no.

        A file is reduced to its parent directory on purpose: ``os.startfile`` on
        a file *runs* it, and catalogue rows include installer caches.
        """
        wanted = self._known_ids(target_id, what="target_id")
        if len(wanted) != 1:
            raise BridgeError("bad_input", "Cần đúng một mục.", "Expected exactly one target.")
        row = targets.by_id(wanted[0])
        resolution = row.resolve()
        if not resolution.available:
            raise BridgeError(
                "not_present",
                f"Không có trên máy này: {resolution.reason or 'không tìm thấy'}",
                f"Not present on this machine: {resolution.reason or 'not found'}",
            )
        folder = next(
            (
                path if path.is_dir() else path.parent
                for path in (Path(raw) for raw in resolution.paths)
                if path.exists()
            ),
            None,
        )
        if folder is None:
            raise BridgeError(
                "not_present",
                "Vị trí này không còn tồn tại.",
                "That location no longer exists.",
            )
        _open_folder(folder)
        _log.info("bridge reveal: target=%s", row.id)
        # The folder goes back so the page can show *which* one opened when a row
        # has several. It is engine-derived, never page-supplied -- the asymmetry
        # is the point: paths may leave, they may not enter.
        return {"target_id": row.id, "opened": str(folder)}

    @guarded
    def history(self, limit: object = HISTORY_LIMIT) -> dict[str, Any]:
        """Past runs, newest first, for the History view.

        Summary rows only -- :func:`adc.engine.report.list_reports` deliberately
        drops each run's ``events`` array, and a list of thirty runs carrying two
        thousand log lines each is not something to serialise into a web page for a
        table that shows a date and a number. :meth:`report_detail` fetches those
        lines for the one run the user opens.

        ``volumes_now`` is read live rather than from the newest report, because
        the trend chart's right-hand edge is *now*: a history list rendered from
        last week's report would show last week's free space as current.
        """
        rows = report.list_reports(limit=_as_limit(limit, default=HISTORY_LIMIT, ceiling=200))
        return {
            "reports": rows,
            "volumes": [vol.as_dict() for vol in volumes_mod.fixed_volumes()],
        }

    @guarded
    def report_detail(self, job_id: object = None) -> dict[str, Any]:
        """One past run in full, including its log lines.

        Reads by job id and nothing else. The id becomes a filename, so
        :func:`adc.engine.report.load_report` refuses any id that is not already
        filename-safe -- ``"../settings"`` reads nothing rather than the settings
        file one directory up. A missing report is a normal answer here: the pruner
        keeps the newest hundred and the History list can outlive the file it
        points at, so both cases land on the same code.
        """
        wanted = _as_job_id(job_id)
        body = report.load_report(wanted)
        if body is None:
            raise BridgeError(
                "no_such_report",
                "Không còn báo cáo cho lần chạy này.",
                "That run's report is no longer on disk.",
            )
        return {"report": body}

    @guarded
    def open_log(self) -> dict[str, Any]:
        """Show today's log file in Explorer. SPEC 7.5's "Mở file log" button.

        Takes no argument at all, which is what makes it safe to exist beside
        :meth:`reveal`'s refusal to accept a path: there is nothing for the page to
        choose. The directory is opened rather than the file, because ``startfile``
        on a ``.log`` would hand it to whatever the machine has associated with the
        extension.
        """
        path = audit.log_file()
        # Created rather than reported missing: on a first run the user may press
        # this before anything has been logged, and an empty folder answers the
        # question ("here is where they go") where a refusal would not.
        folder = paths.ensure(path.parent)
        _open_folder(folder)
        _log.info("bridge open_log: %s", folder)
        return {"opened": str(folder), "log_file": str(path), "exists": path.is_file()}

    @guarded
    def admin_state(self) -> dict[str, Any]:
        """Whether this process is elevated, and which rows that costs.

        v2 runs unelevated by design (SPEC 2.1): the admin-only rows are shown
        disabled with a reason rather than the app demanding UAC at startup for a
        session that may never touch them.
        """
        admin = win.is_user_an_admin()
        gated = [row.id for row in targets.catalog() if row.admin_required]
        return {
            "admin": admin,
            "is_windows": win.IS_WINDOWS,
            "can_relaunch": win.IS_WINDOWS and not admin,
            "frozen": bool(getattr(sys, "frozen", False)),
            "admin_required_ids": gated,
            "blocked_count": 0 if admin else len(gated),
        }

    @guarded
    def admin_relaunch(self) -> dict[str, Any]:
        """Restart elevated through UAC. ``launched`` is false if the user says no.

        A declined prompt is a normal answer, not an error: the app carries on
        unelevated with the admin rows still disabled.

        ``--relaunched`` is passed so the new process knows to wait for this one's
        single-instance mutex instead of finding it held, focusing this window and
        exiting -- which is what would otherwise happen to every elevation.
        """
        if win.is_user_an_admin():
            return {"launched": False, "already_admin": True}
        frozen = bool(getattr(sys, "frozen", False))
        exe = sys.executable
        params = "--relaunched" if frozen else "-m adc --relaunched"
        cwd = None if frozen else str(Path.cwd())
        launched = win.relaunch_as_admin(exe, params, cwd)
        _log.info("bridge admin_relaunch: launched=%s frozen=%s", launched, frozen)
        if launched and self._on_relaunch is not None:
            # The mutex is released as this window closes, which is what lets the
            # elevated process past its own single-instance check.
            self._on_relaunch()
        return {"launched": launched, "already_admin": False}

    # -- shadow copies (SPEC 5) --------------------------------------------
    # Three operations, three calls, and the two that change something are
    # unreachable without a receipt. ``vss_manage`` is the one catalogue row that
    # is not a checkbox (docs/02-SPEC.md 5) precisely because v1 made it one and
    # took every restore point on this machine with it (docs/01-AUDIT.md BUG-09).
    @guarded
    def vss_status(self) -> dict[str, Any]:
        """Read the shadow stores and the copies. Changes nothing, ever.

        Answers ``ok`` even when it could not read: ``supported``, ``is_admin`` and
        ``error`` are what the screen draws instead of a table, and a refusal
        envelope would leave it with nothing to say. ``totals`` is carried so the
        page can show what a percentage means in bytes on each volume without a
        second call, and ``min_maxsize`` so a custom figure is refused in the form
        rather than by vssadmin.
        """
        reading = vss.state(runner=self._vss_runner)
        totals = {vol.letter.upper(): vol.total for vol in volumes_mod.fixed_volumes()}
        return {
            **reading.as_dict(),
            "totals": totals,
            "suggested": [vss.PERCENT_10.as_dict(), vss.UNBOUNDED.as_dict()],
            "min_maxsize": vss.MIN_MAXSIZE_BYTES,
            "ttl_seconds": vss.ACTION_TTL_S,
        }

    @guarded
    def vss_resize_preview(self, volume_id: object = None, limit: object = None) -> dict[str, Any]:
        """Preview a new ceiling and mint the receipt that can apply it.

        This is the way back from BUG-09, so the limit the page may send is
        deliberately narrow -- ``"10%"``, ``"UNBOUNDED"``, or a byte count above
        vssadmin's floor -- and :class:`~adc.engine.vss.Limit` refuses the rest.
        Which volume the copies live on and whether this lowers the ceiling are
        read from the machine here, not accepted from the page.
        """
        letter = _as_volume_id(volume_id)
        total = self._volume_total(letter)
        reading = self._vss_reading()
        try:
            wanted = vss.Limit.parse(limit)
        except ValueError as exc:
            raise BridgeError(
                "bad_limit",
                f"Giới hạn không hợp lệ: {exc}",
                f"Not a usable storage limit: {exc}",
            ) from exc
        pending = vss.plan_resize(
            letter,
            wanted,
            storage=reading.storage_for(letter),
            volume_total=total,
            copies=len(reading.copies_for(letter)),
            store=self._actions,
        )
        self._vss_check(pending)
        _log.info("bridge vss_resize_preview: %s", pending.preview.printable)
        return pending.as_dict()

    @guarded
    def vss_delete_preview(self, volume_id: object = None, scope: object = None) -> dict[str, Any]:
        """Preview a delete and mint its receipt. Always the destructive path.

        ``scope`` defaults to ``oldest`` because v1's default was ``/all`` and that
        is the whole of BUG-09; ``all`` has to be asked for by name, and the count
        it would take comes from the live reading rather than from the page.
        """
        letter = _as_volume_id(volume_id)
        reading = self._vss_reading()
        wanted = "oldest" if scope is None else str(scope).strip().lower()
        if wanted not in vss.SCOPES:
            raise BridgeError(
                "bad_input",
                f"Phạm vi xoá không hợp lệ: {wanted!r}",
                f"Not a delete scope: {wanted!r}",
            )
        pending = vss.plan_delete(
            letter,
            scope=wanted,
            copies=len(reading.copies_for(letter)),
            store=self._actions,
        )
        self._vss_check(pending)
        _log.info("bridge vss_delete_preview: %s", pending.preview.printable)
        return pending.as_dict()

    @guarded
    def vss_apply(self, action_token: object = None, phrase: object = None) -> dict[str, Any]:
        """Redeem a receipt and run the command it holds. Nothing else runs one.

        Takes a token and, for a destructive receipt, the phrase the dialog showed.
        The phrase is checked before the token is spent: a typo must cost a retry,
        not the preview. What runs is the argv the receipt has held since the
        preview -- this method never builds a command.
        """
        token = action_token
        if not isinstance(token, str) or not token:
            raise vss.UnknownActionError("no action token")
        pending = self._actions.peek(token)
        if pending is not None and pending.spent_at is None and not pending.expired():
            self._vss_phrase(pending, phrase)
        approved = self._actions.spend(token)
        result = approved.run(runner=self._vss_runner)
        # warning, not info: these are the two commands v1 ran silently, and the
        # log is where a user finds out which one took their restore points.
        _log.warning(
            "bridge vss_apply: %s -> ok=%s code=%s",
            approved.preview.printable, result.ok, result.code,
        )
        return {"action": approved.as_dict(), "result": result.as_dict()}
