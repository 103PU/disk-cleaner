r"""Project Sweeper tests: three refusals, and a delete that needs a token (SPEC 6.3).

The module under test is mostly made of things it declines to do, so most of these
tests assert an absence. Four rules shape them.

* **The date belongs to the project, not to the directory.** Every fixture project
  is aged with :func:`os.utime` rather than by waiting, and the mandatory
  acceptance test -- ``test_sweeper_never_touches_active_project`` -- pins the
  strongest reading of docs/03-PLAN.md: a project touched inside the window is not
  listed in *any* category, including the ``always_safe`` python caches.
* **A name is not enough.** ``bin`` with no ``.csproj`` beside it and ``dist`` with
  no ``package.json`` must not be offered, and the refusal has to be *counted* in
  ``totals()["unproven"]`` rather than silently dropped.
* **Nothing is ticked and nothing is deleted without a token.** The row dicts carry
  no selection field at all, and the three token failures are the three distinct
  errors the UI needs to tell apart.
* **Real trees, never the real disk.** Everything runs inside ``tmp_path``.
  Sweeping the actual ``E:\PROJECT`` is a manual acceptance step in
  docs/03-PLAN.md, not a unit test.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from adc.engine import sweeper
from adc.engine.cleaner import ExpiredPlanError, SpentPlanError, UnknownPlanError
from adc.engine.guard import GuardError
from adc.engine.jobs import CancelledError, CancelToken, Job, JobKind, JobRunner, JobState
from adc.engine.settings import Settings
from tests.fixtures.make_tree import FixtureUnavailable, make_junction, write_file

# The sweep measures with ``walk_size``; none of these tests may read or write the
# user's real scan cache.
pytestmark = pytest.mark.usefixtures("clean_env")

DAY = 86400.0


def _age(path: Path, days: float) -> None:
    """Backdate a whole tree, deepest entry first.

    Deepest first because writing a child updates the parent's mtime: doing this
    the other way round would leave every directory looking freshly touched, which
    is exactly the mistake the module refuses to make about ``node_modules``.
    """
    when = time.time() - days * DAY
    for target in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        os.utime(target, (when, when))
    os.utime(path, (when, when))


def _project(
    path: Path,
    *,
    days: float,
    manifest: str = "package.json",
    junk: Sequence[str] = ("node_modules",),
    junk_bytes: int = 2048,
    extra: Sequence[str] = (),
) -> Path:
    """A project: one manifest, one source file, and one build directory per name.

    *junk_bytes* lands in a single file inside each junk directory, so a row's
    measured size is a number the assertions can name exactly.
    """
    write_file(path / manifest, 128)
    write_file(path / "src" / "main.js", 256)
    for name in junk:
        write_file(path / name / "pkg" / "index.js", junk_bytes)
    for rel in extra:
        write_file(path / rel, 64)
    _age(path, days)
    return path


def _sweep(
    root: Path,
    *,
    min_age_days: int = sweeper.DEFAULT_MIN_AGE_DAYS,
    settings: Settings | None = None,
) -> sweeper.SweepRun:
    """One sweep, synchronously: no runner, no thread, same code path as the job."""
    active = settings or Settings()
    run = sweeper.SweepRun(
        sweeper.new_job(),
        sweeper.resolve_root(root),
        min_age_days=min_age_days,
        measured_on_disk=active.size_on_disk,
    )
    return sweeper.run_sweep(run, settings=active)


def _rows(run: sweeper.SweepRun) -> dict[str, dict[str, Any]]:
    """The rows the table would draw, keyed by full path."""
    return {row["path"]: row for row in run.rows()}


def _paths(run: sweeper.SweepRun) -> set[str]:
    return {row["path"] for row in run.rows()}


def _finish(job: Job, timeout: float = 20.0) -> JobState:
    """Wait for a runner-driven job to reach a terminal state."""
    deadline = time.monotonic() + timeout
    terminal = (JobState.DONE, JobState.FAILED, JobState.CANCELLED)
    while job.state not in terminal:
        if time.monotonic() > deadline:
            raise AssertionError(f"job {job.id} never finished (state={job.state})")
        time.sleep(0.005)
    return job.state


@pytest.fixture
def workspace(tree: Path) -> Path:
    """Two projects side by side: one idle for ninety days, one touched just now."""
    _project(tree / "old-api", days=90, junk_bytes=4096)
    _project(tree / "live-app", days=0, junk_bytes=4096)
    return tree


# -- 1. what the sweep offers ---------------------------------------------------
def test_an_idle_project_offers_its_node_modules(workspace: Path) -> None:
    run = _sweep(workspace)
    rows = _rows(run)
    wanted = str(workspace / "old-api" / "node_modules")

    assert wanted in rows
    row = rows[wanted]
    assert row["name"] == "node_modules"
    assert row["category"] == "node_modules"
    assert row["project"] == str(workspace / "old-api")
    assert row["project_name"] == "old-api"
    assert row["size"] == 4096
    assert row["files"] == 1
    assert row["idle_days"] >= 89


def test_every_row_shows_a_full_path_and_a_last_modified_date(workspace: Path) -> None:
    """SPEC 6.3: "luôn hiện đường dẫn đầy đủ và ngày sửa cuối"."""
    for row in _sweep(workspace).rows():
        assert os.path.isabs(row["path"])
        assert row["idle_days"] is not None
        assert row["project_mtime"] is not None


def test_nothing_is_pre_ticked(workspace: Path) -> None:
    """The row carries no selection at all, so there is nothing to tick by default.

    A ``"selected": False`` field would be a UI default the engine could later get
    wrong; the absence of one means the page has to hold the selection itself.
    """
    rows = _sweep(workspace).rows()
    assert rows
    for row in rows:
        assert "selected" not in row
        assert "checked" not in row
        assert "default" not in row


# -- 2. the mandatory acceptance test ------------------------------------------
def test_sweeper_never_touches_active_project(workspace: Path) -> None:
    """docs/03-PLAN.md: "project sửa trong 30 ngày không bị liệt kê".

    Not one row of the live project, and the refusal is counted rather than
    silent: a screen that shows a short list without saying what it held back is
    BUG-12's class of lie.
    """
    run = _sweep(workspace)
    live = str(workspace / "live-app")

    assert not [path for path in _paths(run) if path.startswith(live)]
    totals = run.totals()
    assert totals["projects"] == 2
    assert totals["active_projects"] == 1
    assert totals["found"] == 1


def test_an_active_project_keeps_even_its_regenerable_caches(tree: Path) -> None:
    """``__pycache__`` is ``always_safe`` and is still held back. Deliberately.

    Deleting it costs nothing, so the filter is not protecting the cache -- it is
    protecting the habit of reading the list. A screen that lists the project you
    have open teaches you to stop looking at the paths.
    """
    _project(tree / "live-lib", days=0, manifest="pyproject.toml",
             junk=("__pycache__", ".pytest_cache"))
    run = _sweep(tree)

    assert run.rows() == []
    assert run.totals()["active_projects"] == 1


def test_the_window_is_a_setting_not_a_constant(tree: Path) -> None:
    """A project idle for ten days appears at ``min_age_days=7`` and not at 30."""
    _project(tree / "paused", days=10)

    assert _sweep(tree, min_age_days=30).rows() == []
    assert len(_sweep(tree, min_age_days=7).rows()) == 1


# -- 3. a name is not enough ----------------------------------------------------
def test_a_name_without_its_manifest_is_refused_and_counted(tree: Path) -> None:
    """A ``bin`` full of checked-in scripts is not a build directory."""
    write_file(tree / "scripts" / "bin" / "deploy.ps1", 512)
    _age(tree / "scripts", 200)

    run = _sweep(tree)
    assert run.rows() == []
    assert run.totals()["unproven"] == 1


def test_bin_and_obj_beside_a_csproj_are_offered(tree: Path) -> None:
    """Proof by sibling suffix: any ``*.csproj`` next to the directory."""
    write_file(tree / "svc" / "Svc.csproj", 400)
    write_file(tree / "svc" / "bin" / "Debug" / "svc.dll", 8192)
    write_file(tree / "svc" / "obj" / "project.assets.json", 1024)
    _age(tree / "svc", 120)

    rows = _rows(_sweep(tree))
    assert set(rows) == {str(tree / "svc" / "bin"), str(tree / "svc" / "obj")}
    assert {row["category"] for row in rows.values()} == {"build"}
    assert rows[str(tree / "svc" / "bin")]["manifest"] == "Svc.csproj"


def test_a_venv_is_proved_from_inside(tree: Path) -> None:
    """``pyvenv.cfg`` is written by ``venv``, ``virtualenv`` and ``uv`` and nothing else.

    Proof from inside rather than a sibling manifest, because a scratch venv with
    no project around it is still a venv -- and a directory merely *named* ``venv``
    is not.
    """
    write_file(tree / "pyapp" / "pyproject.toml", 128)
    write_file(tree / "pyapp" / ".venv" / "pyvenv.cfg", 64)
    write_file(tree / "pyapp" / ".venv" / "Lib" / "site.py", 4096)
    write_file(tree / "notes" / "venv" / "readme.md", 128)
    _age(tree, 90)

    rows = _rows(_sweep(tree))
    assert str(tree / "pyapp" / ".venv") in rows
    assert str(tree / "notes" / "venv") not in rows
    assert rows[str(tree / "pyapp" / ".venv")]["manifest"] == "pyvenv.cfg"
    assert rows[str(tree / "pyapp" / ".venv")]["category"] == "venv"


# -- 4. what the walk refuses to enter ------------------------------------------
def test_a_matched_directory_is_never_descended_into(tree: Path) -> None:
    """The nested ``node_modules`` npm writes is inside the row, not beside it."""
    _project(tree / "app", days=60)
    write_file(tree / "app" / "node_modules" / "pkg" / "node_modules" / "dep" / "i.js", 512)
    _age(tree / "app", 60)

    rows = _rows(_sweep(tree))
    assert set(rows) == {str(tree / "app" / "node_modules")}
    assert rows[str(tree / "app" / "node_modules")]["files"] == 2


def test_the_walk_is_depth_bounded(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pathological tree cannot turn the find phase into a full disk scan."""
    monkeypatch.setattr(sweeper, "MAX_DEPTH", 1)
    _project(tree / "a" / "b" / "deep-app", days=90)

    assert _sweep(tree).rows() == []
    monkeypatch.setattr(sweeper, "MAX_DEPTH", 12)
    assert len(_sweep(tree).rows()) == 1


@pytest.mark.windows_only
def test_a_junction_named_node_modules_is_not_offered(tree: Path, tmp_path: Path) -> None:
    """BUG-01. A link is not a directory to delete, and its target is not ours.

    The explorer *lists* junctions because a size screen has to explain where the
    bytes went. A screen whose button deletes must not: the row would name a link
    and the delete would either do nothing or reach outside the swept root.
    """
    outside = tmp_path / "outside"
    write_file(outside / "payload.bin", 4096)
    write_file(tree / "app" / "package.json", 128)
    try:
        make_junction(tree / "app" / "node_modules", outside)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))
    _age(tree / "app", 90)

    assert _sweep(tree).rows() == []
    assert (outside / "payload.bin").exists()


# -- 5. a cap is not a loss -----------------------------------------------------
def test_rows_the_table_omits_are_still_in_the_totals(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-12's lie in a new medium: a floor must never be presented as a total."""
    monkeypatch.setattr(sweeper, "TOP_ROWS", 2)
    for name, size in (("a", 8192), ("b", 4096), ("c", 1024)):
        _project(tree / name, days=90, junk_bytes=size)

    run = _sweep(tree)
    totals = run.totals()
    shown = run.rows()

    assert [row["size"] for row in shown] == [8192, 4096]
    assert totals["found"] == 3
    assert totals["shown"] == 2
    assert totals["omitted"] == 1
    assert totals["total_size"] == 8192 + 4096 + 1024
    assert totals["other_size"] == 1024
    assert totals["by_category"]["node_modules"] == {"count": 3, "size": 13312}


def test_the_totals_count_every_refusal(tree: Path) -> None:
    """One listed, one active, one unproven -- and the summary says all three.

    ``projects`` counts projects *dated*, not directories walked: the unproven
    ``tools`` never produced a candidate, so the date phase never saw it and it is
    reported under ``unproven`` alone. Two different refusals, two different
    counters, and no directory silently absent from both.
    """
    _project(tree / "idle", days=90)
    _project(tree / "live", days=0)
    write_file(tree / "tools" / "obj" / "stale.o", 128)
    _age(tree / "tools", 400)

    totals = _sweep(tree).totals()
    assert totals["found"] == 1
    assert totals["projects"] == 2
    assert totals["active_projects"] == 1
    assert totals["unproven"] == 1
    assert totals["truncated"] is False
    assert totals["error"] is None
    assert totals["scanned_dirs"] >= 4


# -- 6. the job it runs as -------------------------------------------------------
def test_a_sweep_is_a_sweep_job_with_three_phases(workspace: Path) -> None:
    """``JobKind.SWEEP`` on both constructors: the history view groups by kind.

    A sweep delete is deliberately not ``JobKind.CLEAN`` -- it belongs beside the
    sweep that found the rows, not among the catalogue cleans whose selection
    vocabulary it does not share.
    """
    assert sweeper.new_job().kind is JobKind.SWEEP
    assert sweeper.new_delete_job().kind is JobKind.SWEEP

    scan = sweeper.new_job().snapshot()
    assert [p["key"] for p in scan["phases"]] == ["find", "date", "measure"]
    delete = sweeper.new_delete_job().snapshot()
    assert [p["key"] for p in delete["phases"]] == ["prepare", "delete", "verify"]


def test_the_runner_drives_a_sweep_to_done(workspace: Path) -> None:
    """The bridge's path: ``submit_sweep`` returns a handle, the thread does the work."""
    runner = JobRunner()
    run = sweeper.submit_sweep(runner, root=workspace)

    assert _finish(run.job) is JobState.DONE
    assert run.min_age_days == sweeper.DEFAULT_MIN_AGE_DAYS
    assert len(run.rows()) == 1
    snapshot = run.job.snapshot()
    assert snapshot["pct"] == 1.0
    assert all(phase["done"] for phase in snapshot["phases"])


def test_a_root_that_is_not_a_directory_fails_before_the_job_exists(tree: Path) -> None:
    """A missing root is a synchronous error the caller can phrase, not a dead job."""
    runner = JobRunner()
    with pytest.raises(OSError):
        sweeper.submit_sweep(runner, root=tree / "nope")
    assert runner.active() is None


def test_cancel_stops_the_walk_and_the_job_says_cancelled(workspace: Path) -> None:
    """BUG-13: v1's cancel only stopped the UI polling. This one reaches the walker.

    The token is tripped before the worker starts so the outcome cannot depend on
    how fast the fixture tree walks; what is under test is that ``run_sweep`` lets
    :class:`CancelledError` out and the runner is what marks the job.
    """
    runner = JobRunner()
    run = sweeper.SweepRun(
        sweeper.new_job(), sweeper.resolve_root(workspace), min_age_days=30, measured_on_disk=False
    )
    run.job.cancel_token.cancel()
    runner.submit(run.job, lambda _job: sweeper.run_sweep(run))

    assert _finish(run.job) is JobState.CANCELLED
    assert run.rows() == []
    assert run.job.snapshot()["cancel_requested"] is True


def test_a_spent_budget_marks_the_totals_a_floor(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out of time is not an error, and it is not silence either: ``truncated`` is True.

    The budget is forced through the seam that reads it -- ``budget_from_env`` --
    with a deadline already in the past, because a small positive budget would race
    ``time.monotonic``, whose resolution on Windows is coarser than a fixture walk.
    """
    monkeypatch.setattr(sweeper, "budget_from_env", lambda fallback: -1.0)
    run = _sweep(workspace)

    totals = run.totals()
    assert totals["truncated"] is True
    assert totals["error"] is None
    assert run.job.state is not JobState.FAILED


# -- 7. dating a project ---------------------------------------------------------
def test_a_build_directorys_own_contents_do_not_count_as_activity(tree: Path) -> None:
    """A fresh file deep inside ``node_modules`` is a build, not the user working."""
    project = _project(tree / "app", days=90)
    now = time.time()
    os.utime(project / "node_modules" / "pkg" / "index.js", (now, now))

    rows = _rows(_sweep(tree))
    assert str(project / "node_modules") in rows


def test_activity_inside_dot_git_does_count(tree: Path) -> None:
    """The opposite rule, and the reason ``.git`` is not in the skip list.

    A fetch, a commit or a checkout is the user working, so a repository whose
    ``.git`` moved today is active even when every tracked file is months old.
    """
    project = _project(tree / "repo", days=90, extra=(".git/index",))
    now = time.time()
    os.utime(project / ".git" / "index", (now, now))

    run = _sweep(tree)
    assert run.rows() == []
    assert run.totals()["active_projects"] == 1


def test_a_future_mtime_keeps_a_project_off_the_list(tree: Path) -> None:
    """Clock skew must fail towards keeping files, never towards deleting them."""
    project = _project(tree / "skewed", days=90)
    ahead = time.time() + 10 * DAY
    os.utime(project / "src" / "main.js", (ahead, ahead))

    run = _sweep(tree)
    assert run.rows() == []
    assert run.totals()["active_projects"] == 1


def test_a_project_too_big_to_date_is_counted_and_listed_nowhere(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incomplete answer is a refusal to list, not a licence to list.

    ``project_activity`` gives up on a project past its entry limit; a project we
    cannot promise is dead does not get offered, and the count says how many.
    """
    monkeypatch.setattr(sweeper, "project_activity", lambda *a, **k: (None, False))
    run = _sweep(workspace)

    assert run.rows() == []
    totals = run.totals()
    assert totals["projects"] == 2
    assert totals["unscanned_projects"] == 2
    assert totals["active_projects"] == 0


def test_project_activity_reports_its_own_incompleteness(tree: Path) -> None:
    """The limit is the mechanism behind the count above, tested on its own."""
    project = _project(tree / "wide", days=90)
    cutoff = time.time() - 30 * DAY

    latest, complete = sweeper.project_activity(
        str(project), cancel=CancelToken(), cutoff=cutoff
    )
    assert complete is True
    assert latest is not None and latest < cutoff

    _, partial = sweeper.project_activity(
        str(project), cancel=CancelToken(), cutoff=cutoff, limit=1
    )
    assert partial is False


def test_dating_a_project_is_cancellable(tree: Path) -> None:
    """The date phase checks the token per directory, not per sweep."""
    project = _project(tree / "app", days=90)
    cancel = CancelToken()
    cancel.cancel()

    with pytest.raises(CancelledError):
        sweeper.project_activity(str(project), cancel=cancel, cutoff=time.time())


# -- 8. the two values the page sends ------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (7, 7),
        (0, 0),
        (7.9, 7),
        (-5, 0),
        (10**6, sweeper.MAX_MIN_AGE_DAYS),
        (True, sweeper.DEFAULT_MIN_AGE_DAYS),
        (False, sweeper.DEFAULT_MIN_AGE_DAYS),
        ("30", sweeper.DEFAULT_MIN_AGE_DAYS),
        (None, sweeper.DEFAULT_MIN_AGE_DAYS),
        (float("nan"), sweeper.DEFAULT_MIN_AGE_DAYS),
        (float("inf"), sweeper.DEFAULT_MIN_AGE_DAYS),
    ],
)
def test_the_age_filter_survives_whatever_javascript_sends(raw: object, expected: int) -> None:
    """Zero is a real choice ("no age filter"); a bool is not a number of days."""
    assert sweeper.clamp_min_age_days(raw) == expected


def test_the_suggested_root_is_pinnable_and_always_exists(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ADC_PROJECT_ROOT`` wins outright; otherwise the suggestion must be real.

    The fallback walks this machine's own volumes, so the assertion is about the
    property that matters -- a root that cannot be swept is worse than no
    suggestion -- rather than about which folder this particular box has.
    """
    monkeypatch.setenv("ADC_PROJECT_ROOT", str(tree))
    assert sweeper.default_root() == str(tree)

    monkeypatch.setenv("ADC_PROJECT_ROOT", str(tree / "missing"))
    fallback = sweeper.default_root()
    assert fallback is None or os.path.isdir(fallback)


# -- 9. the preview -------------------------------------------------------------
def _plan(
    run: sweeper.SweepRun,
    node_ids: object,
    *,
    settings: Settings | None = None,
    store: sweeper.SweepPlanStore | None = None,
) -> tuple[sweeper.SweepPlan, sweeper.SweepPlanStore]:
    """Dry-run *node_ids* against a store of this test's own.

    ``settings`` is always passed explicitly: ``plan_delete`` falls back to
    :func:`adc.engine.settings.load`, and a unit test must not depend on -- or be
    changed by -- the settings file of whoever is running it.
    """
    target = store if store is not None else sweeper.SweepPlanStore()
    plan = sweeper.plan_delete(
        run, node_ids, settings=settings or Settings(), store=target
    )
    return plan, target


def test_the_preview_measures_what_the_delete_will_take(workspace: Path) -> None:
    """A real dry run, not an estimate: the same sweep the live pass will make."""
    run = _sweep(workspace)
    row = run.rows()[0]
    plan, _ = _plan(run, [row["node_id"]])

    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.will_run is True
    assert item.skipped_reason is None
    assert item.path == row["path"]
    assert item.category == "node_modules"
    assert item.est_bytes == 4096
    assert item.files == 1
    assert plan.est_total == 4096
    assert plan.categories == ("node_modules",)


def test_the_preview_deletes_nothing(workspace: Path) -> None:
    """The obvious property, asserted because it is the whole point of a dry run."""
    run = _sweep(workspace)
    row = run.rows()[0]
    victim = Path(row["path"])

    _plan(run, [row["node_id"]])
    assert (victim / "pkg" / "index.js").exists()


def test_the_confirm_dialog_is_told_this_cannot_be_undone(workspace: Path) -> None:
    """Every sweep row is a hard delete, so the answer is not computed from the rows."""
    run = _sweep(workspace)
    plan, _ = _plan(run, [run.rows()[0]["node_id"]])
    shape = plan.as_dict()

    assert shape["has_irreversible"] is True
    assert shape["reversible"] is False
    assert shape["needs_admin"] is False
    assert shape["count"] == 1
    assert shape["skipped"] == 0
    assert shape["node_ids"] == [run.rows()[0]["node_id"]]
    assert shape["expires_at"] == plan.created_at + sweeper.PLAN_TTL_S


def test_only_handles_this_run_minted_are_accepted(workspace: Path) -> None:
    """SEC-02: the confirm path names handles, so no path can travel inwards.

    An unknown handle is not silently dropped either -- it becomes a visible row
    with a reason, because a preview that quietly plans less than was ticked is
    the same class of lie as a total that is really a floor.
    """
    run = _sweep(workspace)
    plan, _ = _plan(run, ["not-a-real-handle", 17, None, {"path": "C:\\Windows"}])

    assert plan.runnable == ()
    assert [item.skipped_reason for item in plan.items] == ["unknown selection; sweep again"]
    assert plan.items[0].category == "unknown"
    assert plan.items[0].path == ""


def test_a_selection_that_is_not_a_list_selects_nothing(workspace: Path) -> None:
    """Whatever the page sent, it is not a reason to delete something."""
    run = _sweep(workspace)
    for sent in ({"a": 1}, 42, None, True):
        plan, _ = _plan(run, sent)
        assert plan.items == ()
        assert plan.est_total == 0


def test_a_duplicated_handle_is_dry_run_once(workspace: Path) -> None:
    run = _sweep(workspace)
    handle = run.rows()[0]["node_id"]
    plan, _ = _plan(run, [handle, handle, handle])

    assert len(plan.items) == 1


def test_a_selection_larger_than_the_cap_says_so(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MAX_PLAN_ITEMS`` truncates the preview, and the preview admits it."""
    monkeypatch.setattr(sweeper, "MAX_PLAN_ITEMS", 1)
    _project(tree / "a", days=90)
    _project(tree / "b", days=90)
    run = _sweep(tree)
    handles = [row["node_id"] for row in run.rows()]
    assert len(handles) == 2

    plan, _ = _plan(run, handles)
    assert len(plan.items) == 1
    assert plan.truncated is True
    assert plan.as_dict()["truncated"] is True


def test_a_user_exclusion_holds_a_row_back_and_names_itself(workspace: Path) -> None:
    """The reason must point at the exclusion list, not at the directory.

    ``strategies.sweep`` swallows an ``ExcludedPathError`` on its own root and
    returns an empty result, which would otherwise be scored as "nothing left to
    remove" -- true of the sweep, useless to the user.
    """
    run = _sweep(workspace)
    row = run.rows()[0]
    guarded = Settings(exclusions=(str(Path(row["path"]).parent),))

    plan, _ = _plan(run, [row["node_id"]], settings=guarded)
    item = plan.items[0]

    assert item.will_run is False
    assert item.skipped_reason is not None
    assert "exclusion" in item.skipped_reason
    assert plan.est_total == 0
    assert plan.categories == ()


def test_a_blocked_root_refuses_the_whole_plan(workspace: Path) -> None:
    r"""``C:\Windows`` never becomes a sweep plan, however the root got there."""
    run = _sweep(workspace)
    handle = run.rows()[0]["node_id"]
    run.root = os.path.join(os.environ.get("SYSTEMROOT", "C:\\Windows"))

    with pytest.raises(GuardError):
        _plan(run, [handle])


# -- 10. the token ---------------------------------------------------------------
def test_a_token_works_exactly_once(workspace: Path) -> None:
    """Single-use is what makes a double-click harmless."""
    run = _sweep(workspace)
    plan, store = _plan(run, [run.rows()[0]["node_id"]])

    assert store.spend(plan.token) is plan
    with pytest.raises(SpentPlanError):
        store.spend(plan.token)


@pytest.mark.parametrize(
    ("token", "error"),
    [
        ("", UnknownPlanError),
        (None, UnknownPlanError),
        (42, UnknownPlanError),
        ("deadbeef" * 4, UnknownPlanError),
    ],
)
def test_a_token_that_was_never_minted_is_unknown(token: object, error: type) -> None:
    """The three failures are three distinct errors because the UI must tell them apart."""
    with pytest.raises(error):
        sweeper.SweepPlanStore().spend(token)


def test_an_old_preview_expires_rather_than_running(workspace: Path) -> None:
    """Ten minutes: long enough to read the list, short enough that the disk still matches."""
    run = _sweep(workspace)
    plan, store = _plan(run, [run.rows()[0]["node_id"]])
    plan.created_at -= sweeper.PLAN_TTL_S + 1

    assert plan.expired() is True
    with pytest.raises(ExpiredPlanError):
        store.spend(plan.token)
    assert store.peek(plan.token) is None


def test_the_delete_needs_a_token_and_nothing_else(workspace: Path) -> None:
    """``submit_delete`` takes a runner and a token: no path, no id list, no flag."""
    runner = JobRunner()
    with pytest.raises(UnknownPlanError):
        sweeper.submit_delete(runner, token="nope", store=sweeper.SweepPlanStore())
    assert runner.active() is None


def test_a_click_that_lands_during_a_scan_does_not_burn_the_token(workspace: Path) -> None:
    """The busy check comes before ``spend``, so the token survives to be used."""
    run = _sweep(workspace)
    plan, store = _plan(run, [run.rows()[0]["node_id"]])
    runner = JobRunner()
    busy = Job(JobKind.SCAN)
    runner.submit(busy, lambda _job: busy.cancel_token.wait(5.0))

    try:
        with pytest.raises(RuntimeError):
            sweeper.submit_delete(runner, token=plan.token, store=store)
        assert store.peek(plan.token) is not None
        assert plan.spent_at is None
    finally:
        busy.request_cancel()
        _finish(busy)


# -- 11. the delete --------------------------------------------------------------
def _delete(plan: sweeper.SweepPlan, *, write: bool = False) -> dict[str, Any]:
    """Delete synchronously. ``job.start()`` first, as ``JobRunner`` would."""
    job = sweeper.new_delete_job()
    job.start()
    return sweeper.run_delete(plan, job, write=write)


def test_the_directory_itself_goes_not_just_its_contents(workspace: Path) -> None:
    """``strategies.sweep`` never lists the root it was handed, so ``remove_dir`` must run.

    Without it every cleaned ``node_modules`` would survive as an empty shell and
    the next sweep would offer it again at zero bytes.
    """
    run = _sweep(workspace)
    row = run.rows()[0]
    victim = Path(row["path"])
    plan, _ = _plan(run, [row["node_id"]])

    _delete(plan)
    assert not victim.exists()
    assert (victim.parent / "package.json").exists()
    assert (victim.parent / "src" / "main.js").exists()


def test_an_unticked_row_survives_the_delete(tree: Path) -> None:
    """Two findings, one ticked. The other is still on disk afterwards."""
    project = _project(tree / "app", days=90, junk=("node_modules", "__pycache__"))
    run = _sweep(tree)
    rows = _rows(run)
    ticked = rows[str(project / "node_modules")]

    plan, _ = _plan(run, [ticked["node_id"]])
    _delete(plan)

    assert not (project / "node_modules").exists()
    assert (project / "__pycache__" / "pkg" / "index.js").exists()


def test_the_reclaimed_figure_is_measured_and_keyed_by_category(tree: Path) -> None:
    """Two projects, one category: ``per_target`` sums, because the rows share a row.

    ``before`` and ``after`` are two real measurements taken around the operation,
    so the number in the receipt is the space that actually came back rather than
    a counter the deleter incremented.
    """
    _project(tree / "a", days=90, junk_bytes=4096)
    _project(tree / "b", days=90, junk_bytes=2048)
    run = _sweep(tree)
    plan, _ = _plan(run, [row["node_id"] for row in run.rows()])
    assert len(plan.runnable) == 2

    body = _delete(plan)
    assert set(body["per_target"]) == {"node_modules"}
    outcome = body["per_target"]["node_modules"]
    assert outcome["before"] == 6144
    assert outcome["after"] == 0
    assert outcome["reclaimed"] == 6144
    assert outcome["files_deleted"] == 2
    assert body["reclaimed_total"] == 6144
    assert body["kind"] == "sweep"
    assert body["state"] == "done"


def test_the_receipt_records_every_path_it_deleted(workspace: Path) -> None:
    """For a hard delete of a directory the user picked, this is the only record left."""
    run = _sweep(workspace)
    row = run.rows()[0]
    plan, _ = _plan(run, [row["node_id"]])

    notes = _delete(plan)["notes"]
    assert notes["reversible"] is False
    assert notes["planned"] == 1
    assert notes["est_total"] == 4096
    assert notes["plan_token"] == plan.token
    assert notes["root"] == run.root
    assert notes["min_age_days"] == sweeper.DEFAULT_MIN_AGE_DAYS
    assert [entry["path"] for entry in notes["deleted_paths"]] == [row["path"]]
    assert notes["deleted_paths"][0]["category"] == "node_modules"
    assert notes["deleted_paths"][0]["idle_days"] == row["idle_days"]
    assert notes["skipped_at_plan"] == {}


def test_a_row_that_changed_since_the_preview_is_refused(workspace: Path) -> None:
    """A plan can be ten minutes old. What it names is re-resolved before use."""
    run = _sweep(workspace)
    row = run.rows()[0]
    victim = Path(row["path"])
    plan, _ = _plan(run, [row["node_id"]])

    # Same path, no longer a directory: the delete must not touch it.
    for child in sorted(victim.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    victim.rmdir()
    victim.write_text("not a directory any more", encoding="utf-8")

    body = _delete(plan)
    assert victim.is_file()
    assert body["per_target"]["node_modules"]["skipped_reason"] == "no longer a directory"
    assert body["reclaimed_total"] == 0


def test_a_row_that_vanished_since_the_preview_is_refused(workspace: Path) -> None:
    """The other half of the same check: gone is a refusal, not an error."""
    run = _sweep(workspace)
    row = run.rows()[0]
    plan, _ = _plan(run, [row["node_id"]])

    victim = Path(row["path"])
    for child in sorted(victim.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    victim.rmdir()

    body = _delete(plan)
    assert body["per_target"]["node_modules"]["skipped_reason"] is not None
    assert body["state"] == "done"


def test_a_cancelled_delete_still_leaves_a_receipt(workspace: Path) -> None:
    """The report is built in a ``finally``: nine directories emptied must be recorded."""
    run = _sweep(workspace)
    plan, store = _plan(run, [run.rows()[0]["node_id"]])
    runner = JobRunner()
    job = sweeper.new_delete_job()
    job.cancel_token.cancel()
    runner.submit(job, lambda started: sweeper.run_delete(plan, started, write=False))

    assert _finish(job) is JobState.CANCELLED
    assert Path(run.rows()[0]["path"]).exists()
    assert store.peek(plan.token) is plan


def test_the_runner_drives_a_delete_to_done(workspace: Path, data_dir: Path) -> None:
    """End to end on the bridge's own path: preview, token, job, receipt on disk."""
    run = _sweep(workspace)
    row = run.rows()[0]
    plan, store = _plan(run, [row["node_id"]])
    runner = JobRunner()

    job = sweeper.submit_delete(runner, token=plan.token, store=store)
    assert _finish(job) is JobState.DONE
    assert job.kind is JobKind.SWEEP
    assert not Path(row["path"]).exists()
    assert plan.spent_at is not None
    written: list[Path] = []
    for _ in range(200):
        written = list(data_dir.rglob(f"{job.id}.json"))
        if written:
            break
        time.sleep(0.01)
    assert len(written) == 1

