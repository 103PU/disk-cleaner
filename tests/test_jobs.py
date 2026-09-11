"""Job model tests. The one that matters most is that cancel is real (BUG-13).

v1's cancel button stopped the UI polling while the work ran to completion. Here
the proof is behavioural: a worker that watches the token stops, and the runner
records CANCELLED rather than DONE.
"""

from __future__ import annotations

import threading
import time

import pytest

from adc.engine.jobs import (
    CancelledError,
    CancelToken,
    Event,
    Job,
    JobKind,
    JobRunner,
    JobState,
    Level,
    Phase,
    TargetOutcome,
)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll *predicate* instead of sleeping a guessed interval."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _phases() -> list[Phase]:
    return [
        Phase(key="scan", label_vi="Quét", label_en="Scan"),
        Phase(key="clean", label_vi="Dọn", label_en="Clean"),
    ]


# ---------------------------------------------------------------------------
# CancelToken
# ---------------------------------------------------------------------------
def test_cancel_token_starts_clear_and_latches() -> None:
    token = CancelToken()

    assert (token.cancelled, token.expired, token.stopped) == (False, False, False)
    token.cancel()
    assert token.cancelled is True
    token.cancel()  # idempotent: one-way flag, never un-set
    assert token.cancelled is True


def test_cancel_token_budget_expires_without_being_cancelled() -> None:
    """``expired`` and ``cancelled`` are separate facts; ``stopped`` is the union.

    The walker needs the union, but the runner reports CANCELLED only for a real
    user cancel -- a job that ran out of its time budget is not the same event.
    """
    token = CancelToken(budget_s=0.0)

    assert token.expired is True
    assert token.cancelled is False
    assert token.stopped is True


def test_cancel_token_without_budget_never_expires() -> None:
    token = CancelToken()

    assert token.expired is False
    token.cancel()
    assert token.stopped is True


def test_cancel_token_wait_returns_early_on_cancel() -> None:
    """A worker sleeping between retries must wake the instant cancel lands."""
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()

    started = time.monotonic()
    woke_for_cancel = token.wait(5.0)
    elapsed = time.monotonic() - started

    assert woke_for_cancel is True
    assert elapsed < 1.0, "slept through the cancel"


def test_cancel_token_wait_times_out_returning_false() -> None:
    assert CancelToken().wait(0.01) is False


def test_cancel_token_raise_if_cancelled() -> None:
    token = CancelToken()
    token.raise_if_cancelled()  # no-op while clear

    token.cancel()
    with pytest.raises(CancelledError):
        token.raise_if_cancelled()


# ---------------------------------------------------------------------------
# Job: phases and progress
# ---------------------------------------------------------------------------
def test_job_id_names_its_kind() -> None:
    """The id is read in log lines and report filenames, so it carries the kind."""
    job = Job(JobKind.CLEAN)

    assert job.id.startswith("clean-")
    assert job.state is JobState.PENDING


def test_job_phases_drive_overall_pct() -> None:
    """Two levels of progress: within a phase, and across phases."""
    job = Job(JobKind.SCAN, _phases())

    assert job.overall_pct == 0.0
    job.set_phase_progress(0.5)
    assert job.overall_pct == pytest.approx(0.25)

    job.enter_phase("clean")
    assert job.overall_pct == pytest.approx(0.5), "earlier phase not banked"
    job.set_phase_progress(1.0)
    assert job.overall_pct == pytest.approx(1.0)


def test_job_enter_phase_marks_every_earlier_phase_done() -> None:
    """Skipping a phase (nothing to clean) must not strand the bar at 33%."""
    job = Job(JobKind.CLEAN, [
        Phase(key="a", label_vi="A", label_en="A"),
        Phase(key="b", label_vi="B", label_en="B"),
        Phase(key="c", label_vi="C", label_en="C"),
    ])

    job.enter_phase("c")
    snap = job.snapshot()

    assert [p["done"] for p in snap["phases"]] == [True, True, False]
    assert snap["current_phase"] == "c"
    assert job.overall_pct == pytest.approx(2 / 3)


def test_job_enter_unknown_phase_raises() -> None:
    """A typo in a phase key is a programming error, not a silent no-op."""
    job = Job(JobKind.SCAN, _phases())

    with pytest.raises(KeyError):
        job.enter_phase("nope")


def test_job_phase_progress_is_clamped() -> None:
    """A ratio computed from a moving denominator can overshoot; the bar cannot."""
    job = Job(JobKind.SCAN, _phases())

    job.set_phase_progress(9.0)
    assert job.snapshot()["phases"][0]["pct"] == 1.0
    job.set_phase_progress(-3.0)
    assert job.snapshot()["phases"][0]["pct"] == 0.0


def test_job_without_phases_reports_binary_progress() -> None:
    job = Job(JobKind.EXPLORE)

    assert job.overall_pct == 0.0
    job.finish(JobState.DONE)
    assert job.overall_pct == 1.0


def test_job_finish_completes_phases_only_when_done() -> None:
    """A cancelled job must not show a full bar -- the work did not happen."""
    cancelled = Job(JobKind.CLEAN, _phases())
    cancelled.finish(JobState.CANCELLED)

    assert [p["done"] for p in cancelled.snapshot()["phases"]] == [False, False]
    assert cancelled.overall_pct < 1.0

    done = Job(JobKind.CLEAN, _phases())
    done.finish(JobState.DONE)
    assert done.overall_pct == 1.0


# ---------------------------------------------------------------------------
# Job: the event log the UI console reads
# ---------------------------------------------------------------------------
def test_job_events_are_bilingual() -> None:
    """The UI is Vietnamese and English, so every line carries both (SPEC 7)."""
    job = Job(JobKind.SCAN)
    job.log(Level.WARN, "Bị khoá", "Locked", target_id="chrome_cache")

    line = job.snapshot()["events"][0]

    assert line["message_vi"] == "Bị khoá"
    assert line["message_en"] == "Locked"
    assert line["level"] == "warn"
    assert line["target_id"] == "chrome_cache"
    assert isinstance(line["ts"], float)


def test_job_snapshot_since_event_returns_only_new_lines() -> None:
    """The UI polls every 250 ms and must not re-download the whole log.

    ``event_count`` is the cursor the UI keeps; passing it back yields exactly
    the lines added since.
    """
    job = Job(JobKind.SCAN)
    for i in range(5):
        job.log(Level.INFO, f"vi {i}", f"en {i}")

    first = job.snapshot()
    assert first["event_count"] == 5
    assert len(first["events"]) == 5

    job.log(Level.SUCCESS, "xong", "done")
    delta = job.snapshot(since_event=first["event_count"])

    assert [e["message_en"] for e in delta["events"]] == ["done"]
    assert delta["event_count"] == 6


def test_job_event_log_is_bounded() -> None:
    """A million-file walk cannot be allowed to grow the log without limit.

    The trim keeps the newest lines: the tail is what the console shows, and the
    full history lives in the report on disk.
    """
    job = Job(JobKind.CLEAN)
    job.max_events = 10
    for i in range(50):
        job.log(Level.INFO, str(i), str(i))

    snap = job.snapshot()

    assert snap["event_count"] == 10
    assert [e["message_en"] for e in snap["events"]] == [str(i) for i in range(40, 50)]


def test_event_as_dict_is_json_ready() -> None:
    """The bridge serialises this dict directly; enums would not survive."""
    body = Event(ts=1.5, level=Level.ERROR, message_vi="v", message_en="e").as_dict()

    assert body["level"] == "error"
    assert isinstance(body["level"], str)
    assert body["target_id"] is None


# ---------------------------------------------------------------------------
# TargetOutcome: reclaimed is measured, never estimated
# ---------------------------------------------------------------------------
def test_target_outcome_reclaimed_is_the_measured_difference() -> None:
    row = TargetOutcome(target_id="pnpm_cache", before=1000, after=250)

    assert row.reclaimed == 750


def test_target_outcome_reclaimed_never_goes_negative() -> None:
    """A tree that grew during the clean (a live app writing) reports 0, not -N.

    Chrome re-creating its cache while we delete it is the ordinary case, and a
    negative "reclaimed" in the report would be nonsense.
    """
    row = TargetOutcome(target_id="chrome_cache", before=100, after=400)

    assert row.reclaimed == 0


def test_job_outcome_is_get_or_create_and_totals_up() -> None:
    job = Job(JobKind.CLEAN)
    job.outcome("a").before = 500
    job.outcome("a").after = 100
    job.outcome("b").before = 200
    job.outcome("b").after = 200
    job.outcome("b").skipped_reason = "locked"

    snap = job.snapshot()

    assert snap["reclaimed_total"] == 400
    assert snap["per_target"]["a"]["reclaimed"] == 400
    assert snap["per_target"]["b"]["skipped_reason"] == "locked"
    assert job.outcome("a") is job.outcome("a"), "created a second row for one target"


# ---------------------------------------------------------------------------
# JobRunner
# ---------------------------------------------------------------------------
def test_runner_runs_the_work_and_marks_it_done() -> None:
    runner = JobRunner()
    job = Job(JobKind.SCAN)
    ran = threading.Event()

    runner.submit(job, lambda j: ran.set())

    assert _wait_until(lambda: job.state is JobState.DONE)
    assert ran.is_set()
    assert job.started_at is not None and job.finished_at is not None
    assert runner.get(job.id) is job


def test_runner_refuses_a_second_job_while_one_is_live() -> None:
    """Two concurrent walks over one disk are slower than one, and the UI has
    a single progress bar. The second submit must fail loudly, not queue.
    """
    runner = JobRunner()
    first = Job(JobKind.SCAN)
    release = threading.Event()
    runner.submit(first, lambda j: release.wait(5.0))

    assert _wait_until(lambda: first.state is JobState.RUNNING)
    with pytest.raises(RuntimeError):
        runner.submit(Job(JobKind.CLEAN), lambda j: None)

    release.set()
    assert _wait_until(lambda: first.state is JobState.DONE)
    # ...and once it is finished the runner accepts work again.
    second = Job(JobKind.CLEAN)
    runner.submit(second, lambda j: None)
    assert _wait_until(lambda: second.state is JobState.DONE)


def test_runner_records_a_failure_instead_of_losing_the_job() -> None:
    """An exception in the worker must still leave a readable job for the UI."""
    runner = JobRunner()
    job = Job(JobKind.CLEAN)

    def explode(j: Job) -> None:
        raise ValueError("disk went away")

    runner.submit(job, explode)

    assert _wait_until(lambda: job.state is JobState.FAILED)
    snap = job.snapshot()
    assert snap["error"] == "disk went away"
    assert any("disk went away" in e["message_en"] for e in snap["events"])
    assert snap["state"] == "failed"


def test_runner_maps_cancelled_error_to_cancelled_state() -> None:
    """``raise_if_cancelled`` is the deep-in-the-stack exit and is not a failure."""
    runner = JobRunner()
    job = Job(JobKind.CLEAN)

    def bail(j: Job) -> None:
        j.cancel_token.cancel()
        j.cancel_token.raise_if_cancelled()

    runner.submit(job, bail)

    assert _wait_until(lambda: job.state is JobState.CANCELLED)
    assert job.snapshot()["error"] is None


def test_runner_cancel_stops_a_running_worker() -> None:
    """BUG-13 end to end: cancel through the runner reaches the worker's loop.

    The worker here is the walker's shape -- a loop that consults the token every
    iteration -- and the assertion is that it stopped early, not that a flag was
    set somewhere.
    """
    runner = JobRunner()
    job = Job(JobKind.CLEAN)
    iterations = {"n": 0}

    def spin(j: Job) -> None:
        for _ in range(1_000_000):
            if j.cancel_token.cancelled:
                return
            iterations["n"] += 1
            time.sleep(0.001)

    runner.submit(job, spin)
    assert _wait_until(lambda: iterations["n"] > 3)

    assert runner.cancel(job.id) is True
    assert _wait_until(lambda: job.state is JobState.CANCELLED, timeout=2.0)
    assert iterations["n"] < 1_000_000, "worker ran to completion despite the cancel"
    assert job.snapshot()["cancel_requested"] is True


def test_runner_cancel_of_an_unknown_job_is_false_not_an_error() -> None:
    """The UI can poll with a stale job id after a restart."""
    assert JobRunner().cancel("scan-deadbeef") is False


def test_runner_active_tracks_the_live_job() -> None:
    runner = JobRunner()
    assert runner.active() is None

    job = Job(JobKind.SCAN)
    runner.submit(job, lambda j: None)

    assert _wait_until(lambda: job.state is JobState.DONE)
    assert runner.active() is job


def test_runner_evicts_the_oldest_finished_jobs() -> None:
    """A long session must not accumulate every job it ever ran."""
    runner = JobRunner(keep_last=2)
    ids: list[str] = []
    for _ in range(3):
        job = Job(JobKind.SCAN)
        runner.submit(job, lambda j: None)
        # Bound as a default: the closure must see this iteration's job, not the
        # last one the loop variable happened to hold.
        assert _wait_until(lambda j=job: j.state is JobState.DONE)
        ids.append(job.id)

    assert runner.get(ids[0]) is None, "oldest finished job was kept"
    assert runner.get(ids[1]) is not None
    assert runner.get(ids[2]) is not None
    assert [j.id for j in runner] == ids[1:]


def test_runner_never_evicts_a_running_job() -> None:
    """Eviction is a memory bound, and it must never drop the job being polled.

    Driven through ``_evict_locked`` directly: reaching this state through
    ``submit`` is impossible by construction, because ``submit`` refuses while a
    job is running -- which is exactly why the invariant needs its own test
    rather than being assumed from the public path.
    """
    runner = JobRunner(keep_last=1)
    live = Job(JobKind.CLEAN)
    live.start()
    later = Job(JobKind.SCAN)
    later.finish(JobState.DONE)

    with runner._lock:
        runner._jobs = {live.id: live, later.id: later}
        runner._order = [live.id, later.id]
        runner._evict_locked()

    assert runner.get(live.id) is live
    assert runner.get(later.id) is later, "evicted past a running job"
