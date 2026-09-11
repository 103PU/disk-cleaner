r"""How a target is actually cleaned (docs/02-SPEC.md 4.5).

Five strategies, and the choice between them is a safety decision, not a
performance one:

* ``RecycleDelete`` is the default for anything inside the user profile, because
  the Recycle Bin is the only undo this app has.
* ``HardDelete`` is for pure caches when the user has turned the Recycle Bin off,
  and for the paths that are too long for the shell API.
* ``ToolCommand`` is preferred over raw deletion wherever the tool knows something
  we do not -- ``uv cache prune`` frees space that deleting the cache directory
  does **not**, because a live venv hardlinks into those files (BUG-07).
* ``WinNative`` is for the operations that have no path: emptying the Recycle Bin
  per volume, DISM component cleanup, ``powercfg /h off``.
* ``Advise`` reports and explains without touching anything -- ``pagefile.sys``, and
  the three targets that have their own screen with their own prechecks.

Two invariants hold for every strategy that deletes:

1. **Every path goes through the guard immediately before it is used.** An escape
   out of the target root aborts the whole target (SPEC 4.7); a user exclusion
   only skips that path.
2. **Reparse points are never descended into and never deleted.** The walker
   established that rule for measuring (BUG-01); here it also stops a junction
   swapped in mid-clean from redirecting a delete outside the root.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

from . import platform_win as win
from .fsutil import FILE_ATTRIBUTE_REPARSE_POINT
from .guard import ExcludedPathError, Guard
from .jobs import CancelledError, CancelToken, Level, TargetOutcome
from .volumes import fixed_volumes

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Win32 error numbers we have to tell apart: a file held open by Chrome is a
# different report to the user than a file we are not allowed to touch.
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33

# SHFileOperationW takes one double-NUL list; batching keeps a single failure from
# costing the whole target and gives cancel somewhere to land.
RECYCLE_BATCH = 512

# Restart Manager is a per-call session; asking about every locked file in a
# 50k-file cache would cost more than the clean. The first few name the culprit.
LOCKED_PROBE_MAX = 32

# Tool commands can legitimately run for minutes (`npm cache clean --force`).
DEFAULT_TOOL_TIMEOUT_S = 900.0
PROC_POLL_S = 0.1

LogFn = Callable[[Level, str, str], None]


@dataclass
class StrategyResult:
    """What one strategy did. Maps onto ``TargetOutcome`` and nothing else.

    ``bytes_deleted`` is for progress only. The number the user is shown comes
    from measuring the target before and after (SPEC 4.8), never from adding up
    what the deleter thought it removed.
    """

    files_deleted: int = 0
    dirs_deleted: int = 0
    files_locked: int = 0
    denied: int = 0
    bytes_deleted: int = 0
    skipped_recent: int = 0
    ok: bool = True
    skipped_reason: str | None = None
    dry_run: bool = False
    locked_by: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def apply_to(self, outcome: TargetOutcome) -> None:
        outcome.files_deleted += self.files_deleted
        outcome.files_locked += self.files_locked
        outcome.denied += self.denied
        if self.skipped_reason and not outcome.skipped_reason:
            outcome.skipped_reason = self.skipped_reason
        for name in self.locked_by:
            if name not in outcome.locked_by:
                outcome.locked_by.append(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_deleted": self.files_deleted,
            "dirs_deleted": self.dirs_deleted,
            "files_locked": self.files_locked,
            "denied": self.denied,
            "bytes_deleted": self.bytes_deleted,
            "skipped_recent": self.skipped_recent,
            "ok": self.ok,
            "skipped_reason": self.skipped_reason,
            "dry_run": self.dry_run,
            "locked_by": self.locked_by[:16],
            "notes": self.notes[:16],
        }

@dataclass
class CleanContext:
    """Everything a strategy is allowed to know.

    The guard is built by the cleaner from the catalogue, never from anything the
    UI sent (SEC-02), and ``dry_run`` defaults to True so that a caller which
    forgets to pass it cannot delete.
    """

    target_id: str
    paths: tuple[str, ...]
    guard: Guard
    cancel: CancelToken
    dry_run: bool = True
    min_age_hours: int = 0
    log: LogFn | None = None

    def emit(self, level: Level, message_vi: str, message_en: str) -> None:
        if self.log is not None:
            self.log(level, message_vi, message_en)


class Strategy(Protocol):
    """Structural. ``reversible`` is what the plan preview shows the user."""

    kind: ClassVar[str]
    reversible: ClassVar[bool]
    needs_admin: ClassVar[bool]

    def describe(self) -> dict[str, Any]: ...

    def execute(self, ctx: CleanContext) -> StrategyResult: ...


@dataclass
class Sweep:
    """One enumeration of what a delete would touch, reused by two strategies.

    ``children`` exists for the fast path: handing the shell one directory is
    orders of magnitude quicker than handing it every file inside, and
    ``files_by_child`` is what keeps the reported file count exact when we do.
    """

    files: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    files_by_child: dict[str, int] = field(default_factory=dict)
    total_bytes: int = 0
    skipped_recent: int = 0
    skipped_links: list[str] = field(default_factory=list)
    excluded: int = 0
    denied: int = 0
    root_is_file: bool = False

def sweep(root: str, *, guard: Guard, cancel: CancelToken, min_age_hours: int = 0) -> Sweep:
    r"""Enumerate what deleting *root*'s contents would touch.

    The root directory itself is never listed: ``%LOCALAPPDATA%\Temp`` must still
    exist afterwards, and half of Windows assumes it does. A root that is a file
    (``MEMORY.DMP``, a VHDX) is listed as itself.

    Raises ``GuardError`` other than ``ExcludedPathError`` -- an escape aborts the
    target rather than skipping a path, per SPEC 4.7. ``CancelledError`` if the
    token trips, so a cancel during a long enumeration is felt immediately.
    """
    out = Sweep()
    try:
        checked_root = guard.check(root)
    except ExcludedPathError:
        out.excluded += 1
        return out

    try:
        root_st = os.stat(checked_root, follow_symlinks=False)
    except OSError:
        out.denied += 1
        return out

    if not stat.S_ISDIR(root_st.st_mode):
        out.root_is_file = True
        out.files.append(checked_root)
        out.children.append(checked_root)
        out.files_by_child[checked_root] = 1
        out.total_bytes = root_st.st_size
        return out

    cutoff = time.time() - min_age_hours * 3600 if min_age_hours > 0 else None
    # (directory, the top-level child it belongs to). The root's own entries are
    # their own child, which is what makes files_by_child add up.
    stack: list[tuple[str, str | None]] = [(checked_root, None)]

    while stack:
        current, child = stack.pop()
        if cancel.stopped:
            raise CancelledError(f"cancelled while listing {current}")
        try:
            scanner = os.scandir(current)
        except OSError:
            out.denied += 1
            continue

        with scanner:
            try:
                for entry in scanner:
                    if cancel.stopped:
                        raise CancelledError(f"cancelled while listing {current}")
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        out.denied += 1
                        continue

                    attrs = getattr(st, "st_file_attributes", 0)
                    if attrs & FILE_ATTRIBUTE_REPARSE_POINT or entry.is_symlink():
                        # BUG-01 again, and here it is also the TOCTOU defence: a
                        # junction dropped in mid-clean cannot redirect a delete.
                        out.skipped_links.append(entry.path)
                        continue

                    owner = child if child is not None else entry.path
                    if child is None:
                        out.children.append(entry.path)

                    if stat.S_ISDIR(st.st_mode):
                        out.dirs.append(entry.path)
                        stack.append((entry.path, owner))
                        continue

                    if cutoff is not None and st.st_mtime > cutoff:
                        out.skipped_recent += 1
                        continue
                    out.files.append(entry.path)
                    out.total_bytes += st.st_size
                    out.files_by_child[owner] = out.files_by_child.get(owner, 0) + 1
            except OSError:
                out.denied += 1

    # Deepest first, so rmdir walks out of the tree instead of into it.
    out.dirs.sort(key=lambda path: path.count(os.sep), reverse=True)
    return out

# ---------------------------------------------------------------------------
# Delete primitives
# ---------------------------------------------------------------------------
Outcome = Literal["deleted", "missing", "locked", "denied", "error"]


def _clear_readonly(path: str) -> bool:
    """Drop the read-only bit. npm and pip set it on thousands of cache files."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        return False
    return True


def _classify(exc: OSError) -> Outcome:
    """"Chrome has it open" and "you are not allowed" are different reports."""
    code = int(getattr(exc, "winerror", 0) or 0)
    if code in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
        return "locked"
    if code == ERROR_ACCESS_DENIED or isinstance(exc, PermissionError):
        return "denied"
    return "error"


def _remove_file(path: str) -> tuple[Outcome, str | None]:
    r"""Delete one file, retrying once after clearing the read-only bit.

    ``\\?\`` is applied here and nowhere in ``RecycleDelete``: this is the
    strategy that has to cope with the 300-character paths inside node_modules,
    and the shell API cannot take the prefix at all.
    """
    target = win.long_path(path) if win.IS_WINDOWS else path
    retried = False
    while True:
        try:
            os.remove(target)
            return "deleted", None
        except FileNotFoundError:
            return "missing", None  # something else got there first; not an error
        except OSError as exc:
            outcome = _classify(exc)
            if outcome == "denied" and not retried:
                retried = True
                if _clear_readonly(target):
                    continue
            return outcome, exc.strerror or str(exc)

def remove_dir(path: str) -> tuple[Outcome, str | None]:
    """``rmdir`` one directory. It must already be empty; callers work upwards.

    Public because :mod:`adc.engine.sweeper` needs exactly this and a second copy
    would be a second place to get it wrong: the long-path prefix, the read-only
    retry, and "already gone is not an error".
    """
    target = win.long_path(path) if win.IS_WINDOWS else path
    retried = False
    while True:
        try:
            os.rmdir(target)
            return "deleted", None
        except FileNotFoundError:
            return "missing", None
        except OSError as exc:
            outcome = _classify(exc)
            if outcome == "denied" and not retried:
                retried = True
                if _clear_readonly(target):
                    continue
            return outcome, exc.strerror or str(exc)


def _prune_empty_dirs(dirs: list[str]) -> int:
    """Remove the directories a file-by-file delete left empty.

    Deepest first -- ``sweep`` already sorted them that way -- and failures are
    silent: a directory still holding a file the age filter kept is *supposed* to
    survive. An empty directory holds no data, so this is not the kind of delete
    the Recycle Bin exists to undo.
    """
    removed = 0
    for path in dirs:
        outcome, _ = remove_dir(path)
        if outcome == "deleted":
            removed += 1
    return removed


def _guarded(ctx: CleanContext, paths: list[str]) -> tuple[list[tuple[str, str]], int]:
    r"""Re-check every path immediately before use: invariant 1 of this module.

    Returns ``(original, checked)`` pairs and the number of paths a user
    exclusion removed. Both halves are needed: ``checked`` is what gets deleted
    (resolved, so a junction swapped in after the sweep cannot redirect it),
    while ``original`` is the key the sweep's per-child tallies are stored under
    -- ``Guard.check`` case-folds, and ``C:\Users\x`` is not ``c:\users\x`` to a
    dictionary even though it is to the filesystem.

    An escape out of the root or a blocked path is *not* caught here: it
    propagates and aborts the whole target (SPEC 4.7).
    """
    kept: list[tuple[str, str]] = []
    excluded = 0
    for path in paths:
        try:
            kept.append((path, ctx.guard.check(path)))
        except ExcludedPathError:
            excluded += 1
    return kept, excluded

def _probe_locks(paths: list[str], result: StrategyResult) -> None:
    """Name the processes holding the first few failed paths (SPEC 4.6).

    A diagnostic must never break a clean, so every failure mode here is
    swallowed -- and nothing is ever killed. The user is told "Chrome is holding
    142 files" and decides for themselves.
    """
    if not paths or not win.IS_WINDOWS:
        return
    try:
        holders = win.who_locks(paths[:LOCKED_PROBE_MAX], limit=16)
    except (OSError, win.UnsupportedPlatformError):
        return
    for holder in holders:
        label = holder.name or f"pid {holder.pid}"
        if label not in result.locked_by:
            result.locked_by.append(label)


def _run_process(
    argv: list[str],
    *,
    cancel: CancelToken,
    timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    cwd: str | None = None,
) -> tuple[int | None, str]:
    """Run *argv* -- never a shell -- so that a cancel lands within ``PROC_POLL_S``.

    ``communicate(timeout=...)`` in a loop rather than ``poll()`` plus a later
    ``read()``: the reader threads it starts keep draining the pipe, so a chatty
    tool cannot deadlock against a full 64 KB buffer. A ``None`` return code
    means the process was killed -- cancelled or out of time -- and is never
    reported as success.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            shell=False,
            creationflags=CREATE_NO_WINDOW,
            cwd=cwd,
        )
    except (OSError, ValueError) as exc:
        return None, str(exc)

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            out, _ = proc.communicate(timeout=PROC_POLL_S)
            return proc.returncode, out or ""
        except subprocess.TimeoutExpired:
            if not cancel.stopped and time.monotonic() < deadline:
                continue
            proc.kill()
            try:
                out, _ = proc.communicate(timeout=5.0)
            except (subprocess.SubprocessError, OSError):
                out = ""
            return None, out or ""

def _abort_if_cancelled(cancel: CancelToken, where: str) -> None:
    """``stopped``, not ``cancelled``: a budget that ran out stops a clean too."""
    if cancel.stopped:
        raise CancelledError(f"cancelled while {where}")


def _last_lines(text: str, limit: int = 3) -> list[str]:
    """The tail of a tool's output: that is where the verdict is."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


# ---------------------------------------------------------------------------
# 1. RecycleDelete -- the default
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecycleDelete:
    r"""Move the contents of each path to the Recycle Bin.

    The default for anything under the user profile, because it is the only
    strategy the user can undo. Two costs, both accepted deliberately: the shell
    API refuses ``\\?\`` so MAX_PATH applies here (``HardDelete`` takes the long
    paths), and the bytes still occupy the volume until the bin is emptied -- so
    the target shrinks while volume free space does not move. SPEC 4.8's two
    independent numbers exist so that shows up as information rather than as a
    contradiction.

    Frozen and field-free: every catalogue row holds one of these as
    configuration, and a mutable strategy would make ``Target`` unhashable.
    """

    kind: ClassVar[str] = "recycle"
    reversible: ClassVar[bool] = True
    needs_admin: ClassVar[bool] = False

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "reversible": True, "needs_admin": False,
                "undo": "Recycle Bin"}

    def execute(self, ctx: CleanContext) -> StrategyResult:
        result = StrategyResult(dry_run=ctx.dry_run)
        if not win.IS_WINDOWS:
            result.ok = False
            result.skipped_reason = "the Recycle Bin API is Windows-only"
            return result

        excluded_total = 0
        for root in ctx.paths:
            _abort_if_cancelled(ctx.cancel, f"cleaning {ctx.target_id}")
            found = sweep(root, guard=ctx.guard, cancel=ctx.cancel,
                          min_age_hours=ctx.min_age_hours)
            result.skipped_recent += found.skipped_recent
            result.denied += found.denied
            excluded_total += found.excluded

            files, files_excluded = _guarded(ctx, found.files)
            dirs, dirs_excluded = _guarded(ctx, found.dirs)
            excluded_total += files_excluded + dirs_excluded
            # The sweep totalled the bytes before exclusions were applied, so it
            # is only an honest figure when nothing was held back. Progress that
            # over-reports is the BUG-12 family of lie, so it stays at zero.
            held_back = files_excluded + dirs_excluded + found.excluded
            byte_credit = 0 if held_back else found.total_bytes

            if ctx.dry_run:
                # A forecast, not a tally: the caller marks it with dry_run.
                result.files_deleted += len(files)
                result.dirs_deleted += len(dirs)
                result.bytes_deleted += byte_credit
                continue

            # Handing the shell one directory instead of its 50k files is orders
            # of magnitude faster, but only sound when nothing inside has to be
            # kept back -- an age-filtered file, an exclusion, or a junction the
            # shell might follow.
            fast = (
                found.skipped_recent == 0
                and files_excluded + dirs_excluded + found.excluded == 0
                and not found.skipped_links
                and not found.root_is_file
            )
            if fast:
                children, _ = _guarded(ctx, found.children)
                refused = self._recycle(ctx, children, found, result, whole_children=True)
            else:
                refused = self._recycle(ctx, files, found, result, whole_children=False)
                result.dirs_deleted += _prune_empty_dirs([checked for _, checked in dirs])
            if refused == 0:
                # Progress only: the number the user is shown is the before/after
                # measurement (SPEC 4.8).
                result.bytes_deleted += byte_credit

            if found.skipped_links:
                result.notes.append(
                    f"{len(found.skipped_links)} junction(s)/symlink(s) left in place"
                )

        if excluded_total:
            result.notes.append(f"{excluded_total} path(s) skipped by a user exclusion")
        return result

    def _recycle(
        self,
        ctx: CleanContext,
        items: list[tuple[str, str]],
        found: Sweep,
        result: StrategyResult,
        *,
        whole_children: bool,
    ) -> int:
        """Batch through ``SHFileOperationW``; returns how many items it refused."""
        failed: list[str] = []
        dir_set = {os.path.normcase(path) for path in found.dirs}
        for start in range(0, len(items), RECYCLE_BATCH):
            _abort_if_cancelled(ctx.cancel, f"recycling {ctx.target_id}")
            batch = items[start:start + RECYCLE_BATCH]
            shell = win.recycle_delete([checked for _, checked in batch])
            if shell.ok:
                self._credit(batch, found, result, dir_set, whole_children=whole_children)
                continue
            # One unreachable path fails the whole batch, so retry item by item:
            # a single locked file must not cost the other 511.
            for pair in batch:
                if win.recycle_delete([pair[1]]).ok:
                    self._credit([pair], found, result, dir_set,
                                 whole_children=whole_children)
                else:
                    failed.append(pair[1])
            if shell.aborted:
                result.notes.append("the shell reported the operation was aborted")

        if failed:
            _probe_locks(failed, result)
            if result.locked_by:
                result.files_locked += len(failed)
            else:
                result.denied += len(failed)
            result.notes.append(f"{len(failed)} item(s) the Recycle Bin refused")
        return len(failed)

    @staticmethod
    def _credit(
        items: list[tuple[str, str]],
        found: Sweep,
        result: StrategyResult,
        dir_set: set[str],
        *,
        whole_children: bool,
    ) -> None:
        """Turn one successful shell op into file and directory counts.

        On the fast path a single op removed a whole subtree, so the number of
        files comes from the sweep's per-child tally -- which is the reason
        ``files_by_child`` is collected at all.
        """
        for original, _checked in items:
            if not whole_children:
                result.files_deleted += 1
                continue
            result.files_deleted += found.files_by_child.get(original, 0)
            if os.path.normcase(original) in dir_set:
                result.dirs_deleted += 1

# ---------------------------------------------------------------------------
# 2. HardDelete -- pure caches, and the paths the shell cannot address
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HardDelete:
    """Delete outright: no Recycle Bin, no undo.

    Always a deliberate catalogue choice, never a fallback after a failed
    recycle. Two reasons an entry asks for it: the user has turned the Recycle
    Bin off, so moving a 4 GB cache into a bin that discards it immediately is
    pure waste; and paths past MAX_PATH, which the shell API cannot take.

    Frozen for the same reason as ``RecycleDelete``: it is configuration, not
    state.
    """

    kind: ClassVar[str] = "hard"
    reversible: ClassVar[bool] = False
    needs_admin: ClassVar[bool] = False

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "reversible": False, "needs_admin": False, "undo": None}

    def execute(self, ctx: CleanContext) -> StrategyResult:
        result = StrategyResult(dry_run=ctx.dry_run)
        excluded_total = 0
        locked: list[str] = []

        for root in ctx.paths:
            _abort_if_cancelled(ctx.cancel, f"cleaning {ctx.target_id}")
            found = sweep(root, guard=ctx.guard, cancel=ctx.cancel,
                          min_age_hours=ctx.min_age_hours)
            result.skipped_recent += found.skipped_recent
            result.denied += found.denied
            excluded_total += found.excluded

            files, files_excluded = _guarded(ctx, found.files)
            dirs, dirs_excluded = _guarded(ctx, found.dirs)
            excluded_total += files_excluded + dirs_excluded
            # Only honest when nothing was held back; see RecycleDelete.execute.
            held_back = files_excluded + dirs_excluded + found.excluded
            byte_credit = 0 if held_back else found.total_bytes

            if ctx.dry_run:
                result.files_deleted += len(files)
                result.dirs_deleted += len(dirs)
                result.bytes_deleted += byte_credit
                continue

            deleted = self._delete_files(ctx, files, result, locked)
            result.files_deleted += deleted
            result.dirs_deleted += _prune_empty_dirs([checked for _, checked in dirs])
            if deleted == len(files):
                result.bytes_deleted += byte_credit
            if found.skipped_links:
                result.notes.append(
                    f"{len(found.skipped_links)} junction(s)/symlink(s) left in place"
                )

        _probe_locks(locked, result)
        if excluded_total:
            result.notes.append(f"{excluded_total} path(s) skipped by a user exclusion")
        return result

    @staticmethod
    def _delete_files(
        ctx: CleanContext,
        files: list[tuple[str, str]],
        result: StrategyResult,
        locked: list[str],
    ) -> int:
        """One file at a time, sorting locked from denied. Returns the count gone.

        Cancel is checked every 256 files rather than every file: the check is
        cheap but the delete is a syscall, and 256 files is a few milliseconds.
        """
        deleted = 0
        for index, (_original, checked) in enumerate(files):
            if index % 256 == 0:
                _abort_if_cancelled(ctx.cancel, f"deleting for {ctx.target_id}")
            outcome, detail = _remove_file(checked)
            if outcome == "deleted":
                deleted += 1
            elif outcome == "locked":
                result.files_locked += 1
                locked.append(checked)
            elif outcome == "denied":
                result.denied += 1
            elif outcome == "error" and len(result.notes) < 16:
                result.notes.append(f"{os.path.basename(checked)}: {detail}")
        return deleted

# ---------------------------------------------------------------------------
# 3. ToolCommand -- let the tool clean its own cache
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolCommand:
    r"""Run the tool's own cache command instead of deleting its directory.

    Preferred wherever the tool knows something we do not, and BUG-07 is the
    case in point: ``uv cache prune`` frees space that deleting
    ``%LOCALAPPDATA%\uv\cache`` does not, because a live venv hardlinks into
    those files -- and deleting the directory outright would break every venv on
    the machine. Same argument for ``pnpm store prune``, ``npm cache clean
    --force`` and ``dotnet nuget locals``.

    ``argv`` is a fixed tuple from the catalogue, passed as a list with
    ``shell=False``. Nothing from the UI, the config file or the environment ever
    reaches it, so there is no argument to quote and no shell to interpret it.

    No file counts come back from here: only the tool knows what it removed, and
    the number the user is shown is the before/after measurement (SPEC 4.8).
    """

    argv: tuple[str, ...]
    timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    ok_codes: tuple[int, ...] = (0,)
    kind: ClassVar[str] = "tool"
    reversible: ClassVar[bool] = False
    needs_admin: ClassVar[bool] = False

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reversible": False,
            "needs_admin": False,
            "command": " ".join(self.argv),
            "timeout_s": self.timeout_s,
        }

    def execute(self, ctx: CleanContext) -> StrategyResult:
        result = StrategyResult(dry_run=ctx.dry_run)
        exe = shutil.which(self.argv[0])
        if exe is None:
            # Same fail-safe rule as the resolvers: say so, do not improvise.
            result.ok = False
            result.skipped_reason = f"{self.argv[0]} is not on PATH"
            return result

        printable = " ".join(self.argv)
        if ctx.dry_run:
            result.notes.append(f"would run: {printable}")
            return result

        ctx.emit(Level.INFO, f"Đang chạy: {printable}", f"Running: {printable}")
        code, out = _run_process(
            [exe, *self.argv[1:]], cancel=ctx.cancel, timeout_s=self.timeout_s
        )
        result.notes.extend(_last_lines(out))
        if code is None:
            if ctx.cancel.stopped:
                raise CancelledError(f"{printable} cancelled")
            result.ok = False
            result.skipped_reason = f"{printable} timed out after {self.timeout_s:.0f}s"
            return result
        if code not in self.ok_codes:
            result.ok = False
            result.skipped_reason = f"{printable} exited {code}"
        return result

# ---------------------------------------------------------------------------
# 4. WinNative -- operations that have no path
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WinNative:
    """One named Windows operation with nothing to enumerate and nothing to walk.

    Emptying the Recycle Bin per volume, DISM component cleanup, turning
    hibernation off. ``needs_admin`` is True on the class because two of the
    three do; the per-target ``admin_required`` in the catalogue is what the UI
    reads (SPEC 4.2), and the Recycle Bin entry sets it False there.

    ``vssadmin``, ``diskpart`` and ``Optimize-VHD`` are deliberately absent:
    those get their own screen with their own prechecks, so their catalogue
    entries use ``Advise`` until that screen exists.
    """

    op: str
    timeout_s: float = 1_800.0
    kind: ClassVar[str] = "winnative"
    reversible: ClassVar[bool] = False
    needs_admin: ClassVar[bool] = True
    OPS: ClassVar[tuple[str, ...]] = (
        "empty_recycle_bin",
        "dism_component_cleanup",
        "hibernate_off",
    )

    def __post_init__(self) -> None:
        # A typo in the catalogue must fail at import, not half way through a run.
        if self.op not in self.OPS:
            raise ValueError(f"unknown WinNative op: {self.op}")

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reversible": False,
            "needs_admin": self.op != "empty_recycle_bin",
            "op": self.op,
        }

    def execute(self, ctx: CleanContext) -> StrategyResult:
        result = StrategyResult(dry_run=ctx.dry_run)
        if not win.IS_WINDOWS:
            result.ok = False
            result.skipped_reason = f"{self.op} is Windows-only"
            return result
        if self.op == "empty_recycle_bin":
            return self._empty_bins(ctx, result)
        if not win.is_user_an_admin():
            result.ok = False
            result.skipped_reason = f"{self.op} requires Administrator"
            return result
        if self.op == "dism_component_cleanup":
            return self._dism(ctx, result)
        return self._hibernate_off(ctx, result)

    @staticmethod
    def _empty_bins(ctx: CleanContext, result: StrategyResult) -> StrategyResult:
        r"""Per volume, because v1 only ever emptied C: (docs/01-AUDIT.md BUG-04).

        This is the one operation that ends the Recycle Bin's usefulness as an
        undo, so it is its own DANGEROUS-adjacent target rather than a step other
        strategies take. The API is used instead of ``Clear-RecycleBin``: no
        console window, and S_OK for an already empty bin.
        """
        for volume in fixed_volumes():
            _abort_if_cancelled(ctx.cancel, "emptying the Recycle Bin")
            if ctx.dry_run:
                result.notes.append(f"would empty the Recycle Bin on {volume.root}")
                continue
            try:
                win.empty_recycle_bin(volume.root)
            except (OSError, win.UnsupportedPlatformError) as exc:
                result.denied += 1
                result.notes.append(f"{volume.root}: {exc}")
                continue
            result.notes.append(f"emptied {volume.root}")
        result.ok = result.denied == 0
        return result

    def _dism(self, ctx: CleanContext, result: StrategyResult) -> StrategyResult:
        """``/StartComponentCleanup``. Exit 3010 means "done, reboot to finish"."""
        exe = shutil.which("dism")
        if exe is None:
            result.ok = False
            result.skipped_reason = "dism is not on PATH"
            return result
        argv = [exe, "/Online", "/Cleanup-Image", "/StartComponentCleanup"]
        if ctx.dry_run:
            # /AnalyzeComponentStore is the honest way to size this, and it takes
            # minutes -- far too slow for a plan preview.
            result.notes.append("would run: dism /Online /Cleanup-Image /StartComponentCleanup")
            return result

        ctx.emit(Level.INFO, "DISM đang dọn component store (có thể mất nhiều phút)",
                 "DISM is cleaning the component store (this can take minutes)")
        code, out = _run_process(argv, cancel=ctx.cancel, timeout_s=self.timeout_s)
        result.notes.extend(_last_lines(out))
        if code is None:
            if ctx.cancel.stopped:
                raise CancelledError("dism cancelled")
            result.ok = False
            result.skipped_reason = f"dism timed out after {self.timeout_s:.0f}s"
            return result
        if code == 3010:
            result.notes.append("a restart is needed to finish the cleanup")
        elif code != 0:
            result.ok = False
            result.skipped_reason = f"dism exited {code}"
        return result

    def _hibernate_off(self, ctx: CleanContext, result: StrategyResult) -> StrategyResult:
        r"""``powercfg /hibernate off`` -- deletes ``C:\hiberfil.sys`` wholesale.

        Worth several GB and trivially reversible with ``powercfg /hibernate on``,
        but it costs Fast Startup as well as hibernation, which is why the note
        says so rather than leaving the user to find out.
        """
        exe = shutil.which("powercfg")
        if exe is None:
            result.ok = False
            result.skipped_reason = "powercfg is not on PATH"
            return result
        if ctx.dry_run:
            result.notes.append("would run: powercfg /hibernate off")
            return result

        code, out = _run_process([exe, "/hibernate", "off"], cancel=ctx.cancel,
                                 timeout_s=120.0)
        result.notes.extend(_last_lines(out))
        if code is None:
            if ctx.cancel.stopped:
                raise CancelledError("powercfg cancelled")
            result.ok = False
            result.skipped_reason = "powercfg timed out"
            return result
        if code != 0:
            result.ok = False
            result.skipped_reason = f"powercfg exited {code}"
            return result
        result.notes.append("hibernation and Fast Startup are now off; "
                            "re-enable with: powercfg /hibernate on")
        return result


# ---------------------------------------------------------------------------
# 5. Advise -- report, explain, touch nothing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Advise:
    r"""Explain what could be done, and do nothing at all.

    Two kinds of target land here: the ones where deleting is the wrong answer at
    any size (``pagefile.sys`` -- the fix is a setting, not a delete), and the
    ones whose real operation needs its own screen with its own prechecks (VSS,
    Docker VHDX compaction, WSL distro shrink). Never a silent 0 B: the reason
    travels to the UI in both languages, which is the difference between "nothing
    found" and "found, and here is why it is being left alone".
    """

    reason_vi: str
    reason_en: str
    handled_by: str | None = None
    kind: ClassVar[str] = "advise"
    reversible: ClassVar[bool] = True  # nothing was touched, so there is nothing to undo
    needs_admin: ClassVar[bool] = False

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reversible": True,
            "needs_admin": False,
            "handled_by": self.handled_by,
            "reason_vi": self.reason_vi,
            "reason_en": self.reason_en,
        }

    def execute(self, ctx: CleanContext) -> StrategyResult:
        # skipped_reason is the log-facing field, so it carries the English text;
        # the console line is emitted in both languages.
        ctx.emit(Level.INFO, self.reason_vi, self.reason_en)
        result = StrategyResult(dry_run=ctx.dry_run, skipped_reason=self.reason_en)
        if self.handled_by:
            result.notes.append(f"handled by: {self.handled_by}")
        return result
