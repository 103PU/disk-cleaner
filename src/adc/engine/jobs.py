"""Job model: one long-running operation, cancellable and observable.

The UI never blocks. Every scan, plan, or clean becomes a ``Job`` running on its
own thread; the bridge is request/response, so the UI polls ``snapshot()`` every
250 ms (docs/02-SPEC.md 4.8).

Cancellation is the part v1 got wrong (BUG-13): its "cancel" only stopped the UI
from polling while the walker kept running. Here a ``CancelToken`` is checked at
every entry in the walker and every file in the deleter, so cancel is real.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobKind(str, Enum):
    SCAN = "scan"
    PLAN = "plan"
    CLEAN = "clean"
    EXPLORE = "explore"
    SWEEP = "sweep"


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Level(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    SUCCESS = "success"


class CancelledError(Exception):
    """Raised inside a worker when the token has been tripped."""


class CancelToken:
    """A one-way flag plus an optional wall-clock budget.

    ``threading.Event`` on its own would do for the flag, but the budget belongs
    here too: the walker must stop at *either* signal and the call sites should
    only have to ask one question.
    """

    __slots__ = ("_deadline", "_event")

    def __init__(self, budget_s: float | None = None) -> None:
        self._event = threading.Event()
        self._deadline: float | None = None if budget_s is None else time.monotonic() + budget_s

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """True once cancel() was called. Does not consider the budget."""
        return self._event.is_set()

    @property
    def expired(self) -> bool:
        """True once the wall-clock budget has run out."""
        return self._deadline is not None and time.monotonic() >= self._deadline

    @property
    def stopped(self) -> bool:
        """The question the hot loops actually ask."""
        return self._event.is_set() or self.expired

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError

    def wait(self, timeout: float) -> bool:
        """Sleep up to *timeout*, waking early on cancel. Returns True if cancelled."""
        return self._event.wait(timeout)


@dataclass
class Event:
    """One line for the UI console. Bilingual because the UI is (docs/02-SPEC.md 7)."""

    ts: float
    level: Level
    message_vi: str
    message_en: str
    target_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "level": self.level.value,
            "target_id": self.target_id,
            "message_vi": self.message_vi,
            "message_en": self.message_en,
        }


@dataclass
class TargetOutcome:
    """Per-target bookkeeping. ``reclaimed`` is measured, never estimated.

    ``before``/``after`` are two real measurements taken around the operation,
    which is what makes the final report trustworthy and what silently fixes
    BUG-07: for ``docker_compact`` they are VHDX file sizes, so the number shown
    is the space the compaction actually returned, not the size of the disk.
    """

    target_id: str
    before: int = 0
    after: int = 0
    files_deleted: int = 0
    files_locked: int = 0
    denied: int = 0
    skipped_reason: str | None = None
    locked_by: list[str] = field(default_factory=list)

    @property
    def reclaimed(self) -> int:
        return max(0, self.before - self.after)

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "before": self.before,
            "after": self.after,
            "reclaimed": self.reclaimed,
            "files_deleted": self.files_deleted,
            "files_locked": self.files_locked,
            "denied": self.denied,
            "skipped_reason": self.skipped_reason,
            "locked_by": list(self.locked_by),
        }


@dataclass
class Phase:
    """A named step with its own 0..1 progress, so the UI can show two levels."""

    key: str
    label_vi: str
    label_en: str
    pct: float = 0.0
    done: bool = False


class Job:
    """A unit of long work. Thread-safe: the worker writes, the UI thread reads.

    Every mutator takes the same lock, and ``snapshot()`` builds a plain dict
    under that lock so the bridge can serialise it without holding a reference to
    live objects. The UI must never see a half-updated job.
    """

    def __init__(self, kind: JobKind, phases: list[Phase] | None = None) -> None:
        self.id = f"{kind.value}-{uuid.uuid4().hex[:12]}"
        self.kind = kind
        self.cancel_token = CancelToken()
        self._lock = threading.RLock()
        self._state = JobState.PENDING
        self._phases: list[Phase] = list(phases or [])
        self._phase_index = 0
        self._events: list[Event] = []
        self._per_target: dict[str, TargetOutcome] = {}
        self._error: str | None = None
        self._detail_vi = ""
        self._detail_en = ""
        self.started_at: float | None = None
        self.finished_at: float | None = None
        # Bounded so a multi-million-file walk cannot grow the log without limit;
        # the report on disk keeps everything, the in-memory tail is for the UI.
        self.max_events = 2000

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self._state = JobState.RUNNING
            self.started_at = time.time()

    def finish(self, state: JobState = JobState.DONE, error: str | None = None) -> None:
        with self._lock:
            self._state = state
            self._error = error
            self.finished_at = time.time()
            for phase in self._phases:
                if state is JobState.DONE:
                    phase.done = True
                    phase.pct = 1.0

    def request_cancel(self) -> None:
        self.cancel_token.cancel()
        self.log(Level.WARN, "Đã yêu cầu dừng…", "Cancellation requested…")

    @property
    def state(self) -> JobState:
        with self._lock:
            return self._state

    # -- progress -----------------------------------------------------------
    def log(
        self,
        level: Level,
        message_vi: str,
        message_en: str,
        target_id: str | None = None,
    ) -> None:
        with self._lock:
            self._events.append(
                Event(
                    ts=time.time(),
                    level=level,
                    message_vi=message_vi,
                    message_en=message_en,
                    target_id=target_id,
                )
            )
            if len(self._events) > self.max_events:
                del self._events[: len(self._events) - self.max_events]

    def enter_phase(self, key: str) -> None:
        with self._lock:
            for i, phase in enumerate(self._phases):
                if phase.key == key:
                    for earlier in self._phases[:i]:
                        earlier.done = True
                        earlier.pct = 1.0
                    self._phase_index = i
                    return
            raise KeyError(f"unknown phase: {key}")

    def set_phase_progress(self, pct: float, detail_vi: str = "", detail_en: str = "") -> None:
        with self._lock:
            if self._phases:
                self._phases[self._phase_index].pct = min(1.0, max(0.0, pct))
            if detail_vi:
                self._detail_vi = detail_vi
            if detail_en:
                self._detail_en = detail_en

    def outcome(self, target_id: str) -> TargetOutcome:
        """Get-or-create the bookkeeping row for *target_id*."""
        with self._lock:
            row = self._per_target.get(target_id)
            if row is None:
                row = TargetOutcome(target_id=target_id)
                self._per_target[target_id] = row
            return row

    # -- read side ----------------------------------------------------------
    @property
    def overall_pct(self) -> float:
        """Whole-job progress: completed phases plus the current phase's share."""
        with self._lock:
            if not self._phases:
                return 1.0 if self._state in _TERMINAL else 0.0
            share = 1.0 / len(self._phases)
            done = sum(1 for p in self._phases if p.done)
            current = self._phases[self._phase_index].pct if not self._phases[
                self._phase_index
            ].done else 0.0
            return min(1.0, done * share + current * share)

    def snapshot(self, since_event: int = 0) -> dict[str, Any]:
        """A JSON-ready view. *since_event* lets the UI ask only for new lines."""
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind.value,
                "state": self._state.value,
                "pct": self.overall_pct,
                "phases": [
                    {
                        "key": p.key,
                        "label_vi": p.label_vi,
                        "label_en": p.label_en,
                        "pct": p.pct,
                        "done": p.done,
                    }
                    for p in self._phases
                ],
                "current_phase": (
                    self._phases[self._phase_index].key if self._phases else None
                ),
                "detail_vi": self._detail_vi,
                "detail_en": self._detail_en,
                "event_count": len(self._events),
                "events": [e.as_dict() for e in self._events[since_event:]],
                "per_target": {k: v.as_dict() for k, v in self._per_target.items()},
                "reclaimed_total": sum(v.reclaimed for v in self._per_target.values()),
                "cancel_requested": self.cancel_token.cancelled,
                "error": self._error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


_TERMINAL = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED})


class JobRunner:
    """Owns the worker threads and the job registry the bridge polls.

    Deliberately not a thread pool: at most one heavy job runs at a time, because
    two concurrent walks over the same disk are slower than one and the progress
    UI has a single bar. ``submit`` refuses while a job is live.
    """

    def __init__(self, keep_last: int = 20) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._active: str | None = None
        self._keep_last = keep_last

    def submit(self, job: Job, work: Any) -> Job:
        """Run *work(job)* on a daemon thread. Raises if a job is already active."""
        with self._lock:
            if self._active is not None:
                active = self._jobs.get(self._active)
                if active is not None and active.state is JobState.RUNNING:
                    raise RuntimeError(f"job {self._active} is still running")
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._active = job.id
            self._evict_locked()

        def _run() -> None:
            job.start()
            try:
                work(job)
            except CancelledError:
                job.finish(JobState.CANCELLED)
            except Exception as exc:  # the report must survive any worker failure
                job.log(
                    Level.ERROR,
                    f"Job thất bại: {exc}",
                    f"Job failed: {exc}",
                )
                job.finish(JobState.FAILED, error=str(exc))
            else:
                if job.state is JobState.RUNNING:
                    job.finish(
                        JobState.CANCELLED if job.cancel_token.cancelled else JobState.DONE
                    )

        threading.Thread(target=_run, name=f"adc-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.request_cancel()
        return True

    def active(self) -> Job | None:
        with self._lock:
            if self._active is None:
                return None
            return self._jobs.get(self._active)

    def _evict_locked(self) -> None:
        """Drop the oldest finished jobs so a long session cannot grow forever."""
        while len(self._order) > self._keep_last:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job is not None and job.state is JobState.RUNNING:
                return
            self._order.pop(0)
            self._jobs.pop(oldest, None)

    def __iter__(self) -> Iterator[Job]:
        with self._lock:
            return iter([self._jobs[i] for i in self._order if i in self._jobs])
