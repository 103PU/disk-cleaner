r"""Disk Explorer tests: one level, measured honestly (SPEC 6.2).

Four rules shape this module, mirroring ``tests/test_scanner.py``.

* **Real trees, never the real disk.** Every level is explored inside ``tmp_path``.
  Walking an actual ``C:\`` is a manual acceptance step in docs/03-PLAN.md, not a
  unit test -- it would take minutes and give a different answer every run.
* **A cap is not a loss.** Rows a level does not show must still be inside
  ``total_size`` and must be named by ``omitted`` / ``other_size``. That is BUG-12's
  lie (a floor presented as a total) in a new medium, so the caps are turned down to
  two rows and the arithmetic is pinned exactly.
* **Cancel and out-of-time are different things.** An explicit ``cancel()`` raises
  out of :func:`~adc.engine.explorer.run_explore` and the runner marks the job
  CANCELLED; a budget running out marks the level ``truncated`` and still returns
  every row it managed to measure.
* **A junction is listed and never followed.** BUG-01's rule has to be visible in
  the rows, not only inside the walker.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from adc.engine import explorer
from adc.engine.fsutil import ScanCache
from adc.engine.jobs import CancelledError, Job, JobKind, JobRunner, JobState
from adc.engine.settings import Settings
from tests.fixtures.make_tree import FixtureUnavailable, make_junction, make_sparse_file, write_file

# No test here may touch the user's real scan cache; the two that care about
# caching build their own sqlite file under ``tmp_path``.
pytestmark = pytest.mark.usefixtures("clean_env")


def _explore(
    root: Path, *, settings: Settings | None = None, cache: ScanCache | None = None
) -> explorer.ExploreRun:
    """One level, synchronously: no runner, no thread, same code path as the job."""
    active = settings or Settings()
    run = explorer.ExploreRun(
        explorer.new_job(),
        explorer.resolve_root(root),
        measured_on_disk=active.size_on_disk,
    )
    return explorer.run_explore(run, settings=active, cache=cache)


def _by_name(run: explorer.ExploreRun) -> dict[str, dict[str, Any]]:
    """The rows the level would draw, keyed by name."""
    return {row["name"]: row for row in run.rows()}


def _finish(job: Job, timeout: float = 15.0) -> JobState:
    """Wait for a runner-driven job to reach a terminal state."""
    deadline = time.monotonic() + timeout
    terminal = (JobState.DONE, JobState.FAILED, JobState.CANCELLED)
    while job.state not in terminal:
        if time.monotonic() > deadline:
            raise AssertionError(f"job {job.id} never finished (state={job.state})")
        time.sleep(0.005)
    return job.state


@pytest.fixture
def level(tree: Path) -> Path:
    r"""A known shape, so every byte in the assertions below is accounted for.

    ``big`` is 14336 B across two levels, ``small`` is 1024, and ``loose.bin`` is
    5000 -- 20360 for the level. Plain files, so on-disk equals logical:
    :func:`~adc.engine.fsutil.allocated_size` deliberately does not model
    cluster-slack rounding, which is what keeps these numbers exact.
    """
    write_file(tree / "big" / "a.bin", 4096)
    write_file(tree / "big" / "b.bin", 4096)
    write_file(tree / "big" / "c.bin", 4096)
    write_file(tree / "big" / "sub" / "d.bin", 2048)
    write_file(tree / "small" / "e.bin", 1024)
    write_file(tree / "loose.bin", 5000)
    return tree


# ---------------------------------------------------------------------------
# Breadcrumbs and the door: what the UI navigates by
# ---------------------------------------------------------------------------


def test_crumbs_walk_up_to_the_volume_root(tmp_path: Path) -> None:
    """The chain ends at the folder on screen and starts at the drive.

    The crumb before the last one is what "up" means, which is why navigating
    upwards needs no extra bridge method -- every crumb already carries a handle.
    """
    deep = tmp_path / "one" / "two" / "three"
    deep.mkdir(parents=True)

    crumbs = explorer.crumbs_for(str(deep))

    assert [crumb.label for crumb in crumbs[-3:]] == ["one", "two", "three"]
    assert crumbs[-1].path == str(deep)
    assert crumbs[0].path == os.path.splitdrive(str(deep))[0] + os.sep


def test_crumbs_of_a_root_are_a_single_step(tmp_path: Path) -> None:
    """A volume root has nothing above it, and its label is the root itself.

    ``os.path.basename`` is empty for a root, so the label falls back to the path --
    a breadcrumb reading ``C:\\`` rather than a blank first tab.
    """
    root = os.path.splitdrive(str(tmp_path))[0] + os.sep

    crumbs = explorer.crumbs_for(root)

    assert len(crumbs) == 1
    assert crumbs[0].label == root
    assert crumbs[0].path == root


def test_node_ids_are_random_not_derived_from_the_path(tmp_path: Path) -> None:
    """SEC-02: a handle the page could *compute* would be a path in disguise.

    Two crumb chains over the same folder must share no id. If ids were a hash of
    the path, a renderer that guessed one would have reached an arbitrary folder
    without ever sending a path -- which is the rule this pins.
    """
    first = explorer.crumbs_for(str(tmp_path))
    second = explorer.crumbs_for(str(tmp_path))

    assert [c.path for c in first] == [c.path for c in second]
    assert not {c.node_id for c in first} & {c.node_id for c in second}


def test_resolve_root_rejects_a_file_and_a_missing_path(tmp_path: Path) -> None:
    """The door fails loudly and synchronously; the bridge turns this into an error.

    A job that starts and immediately dies would show the user a failed scan where
    "that folder is gone" is the honest answer.
    """
    a_file = write_file(tmp_path / "not-a-folder.bin", 16)

    with pytest.raises(NotADirectoryError):
        explorer.resolve_root(a_file)
    with pytest.raises(FileNotFoundError):
        explorer.resolve_root(tmp_path / "nope")


def test_resolve_root_returns_an_absolute_path(level: Path) -> None:
    """Relative input is resolved once, at the door, so no crumb is ever relative."""
    assert explorer.resolve_root(level) == os.path.abspath(str(level))


# ---------------------------------------------------------------------------
# One level: folders measured recursively, files priced from their own stat
# ---------------------------------------------------------------------------


def test_a_level_reports_every_child_with_its_own_size(level: Path) -> None:
    """Folders carry their whole subtree; a file carries what the ``scandir`` said."""
    run = _explore(level)
    rows = _by_name(run)

    assert set(rows) == {"big", "small", "loose.bin"}
    assert rows["big"]["kind"] == "dir"
    assert rows["big"]["size"] == 4096 * 3 + 2048
    assert rows["big"]["files"] == 4
    assert rows["big"]["dirs"] == 1
    assert rows["small"]["size"] == 1024
    assert rows["loose.bin"]["kind"] == "file"
    assert rows["loose.bin"]["size"] == 5000
    assert rows["loose.bin"]["files"] == 1


def test_the_level_total_is_the_sum_of_its_children(level: Path) -> None:
    """One folder's answer to "where did the space go", to the byte."""
    totals = _explore(level).totals()

    assert totals["total_size"] == 14336 + 1024 + 5000
    assert totals["total_logical"] == totals["total_size"]
    assert (totals["dirs"], totals["files"], totals["links"]) == (2, 1, 0)
    assert totals["children"] == 3
    assert totals["omitted"] == 0
    assert totals["other_size"] == 0
    assert totals["measured"] == totals["to_measure"] == 2
    assert totals["truncated"] is False
    assert totals["error"] is None


def test_rows_come_back_biggest_first(level: Path) -> None:
    """The ranking is the point of the view; the treemap draws it in this order."""
    assert [row["name"] for row in _explore(level).rows()] == [
        "big",
        "loose.bin",
        "small",
    ]


def test_the_level_carries_its_root_crumbs_and_handles(level: Path) -> None:
    """``as_dict`` is what ``job_poll`` ships: enough to draw and to navigate."""
    payload = _explore(level).as_dict()

    assert payload["root"] == str(level)
    assert payload["name"] == level.name
    assert payload["crumbs"][-1]["node_id"] == payload["node_id"]
    assert payload["parent_id"] == payload["crumbs"][-2]["node_id"]
    assert len({row["node_id"] for row in payload["rows"]}) == 3
    assert "path" not in payload["crumbs"][0], "a crumb hands the page a handle, not a path"


def test_rows_and_crumbs_hand_out_handles_never_paths(level: Path) -> None:
    """Paths may leave, they may not enter (bridge.py) -- so only one path leaves.

    The level's own ``root`` is shown, because the header has to say which folder
    this is. Every *navigable* thing is a ``node_id``: if a row carried its path the
    page could send it straight back and pick its own target.
    """
    payload = _explore(level).as_dict()

    assert payload["root"] == str(level)
    for row in payload["rows"]:
        assert "path" not in row
        assert str(level) not in repr(row)
    for crumb in payload["crumbs"]:
        assert set(crumb) == {"node_id", "label"}


# ---------------------------------------------------------------------------
# Caps and pruning: what a level leaves out, it still counts
# ---------------------------------------------------------------------------


def test_capped_rows_are_counted_not_dropped(level: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A top-N that hides bytes is BUG-12 in a new medium.

    Turned down to one row of each kind: the level then shows ``big`` and
    ``loose.bin`` and must still report ``small``'s 1024 B -- as ``omitted`` and
    inside both ``total_size`` and ``other_size``.
    """
    monkeypatch.setattr(explorer, "TOP_DIRS", 1)
    monkeypatch.setattr(explorer, "TOP_FILES", 1)

    run = _explore(level)
    totals = run.totals()

    assert [row["name"] for row in run.rows()] == ["big", "loose.bin"]
    assert totals["total_size"] == 20360
    assert totals["shown"] == 2
    assert totals["omitted"] == 1
    assert totals["other_size"] == 1024
    assert totals["other_size"] == totals["total_size"] - sum(row["size"] for row in run.rows())


def test_pruning_keeps_the_biggest_rows_and_the_whole_total(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory is bounded by ``MAX_TRACKED``; correctness of the total is not.

    A cache folder with a hundred thousand files must not become a hundred thousand
    row objects. Pruning by size is what makes that safe: the rows dropped are the
    ones the level would never have shown, and their bytes stay in ``total_size``.
    """
    monkeypatch.setattr(explorer, "MAX_TRACKED", 2)
    for index in range(10):
        write_file(tree / f"f{index}.bin", 1000 + index)

    run = _explore(tree)
    totals = run.totals()

    assert totals["files"] == 10
    assert totals["total_size"] == sum(1000 + index for index in range(10))
    assert len(run.rows()) <= 4, "pruning runs at twice the cap"
    assert run.rows()[0]["name"] == "f9.bin", "the biggest survived every prune"


# ---------------------------------------------------------------------------
# Cancel, budget, and the things that go wrong
# ---------------------------------------------------------------------------


def test_cancel_raises_out_of_run_explore(level: Path) -> None:
    """The runner is what marks a job CANCELLED; swallowing this would lie.

    Same contract as :func:`~adc.engine.scanner.run_scan` -- a half-measured level
    reported as a finished one is worse than no level at all.
    """
    run = explorer.ExploreRun(explorer.new_job(), explorer.resolve_root(level))
    run.job.cancel_token.cancel()

    with pytest.raises(CancelledError):
        explorer.run_explore(run)


def test_out_of_time_truncates_the_level_but_still_returns_it(
    level: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget is not a cancel: the listing survives, the sizes are a floor.

    A budget of exactly 0.0 is the deterministic way in (the technique
    ``test_budget_expiry_truncates_but_does_not_raise`` uses): ``budget_from_env``
    treats an env value of 0 as "no budget", so the only way to hand the level a
    literal zero is to patch the function the module imported.
    """
    monkeypatch.setattr(explorer, "budget_from_env", lambda fallback: 0.0)

    run = _explore(level)
    totals = run.totals()

    assert totals["truncated"] is True
    assert totals["measured"] == 0
    assert totals["to_measure"] == 2
    assert totals["children"] == 3, "the listing itself is not budgeted"
    assert totals["total_size"] == 5000, "only the file, and the folders are 0 so far"
    assert any("Out of time" in event["message_en"] for event in run.job.snapshot()["events"])


def test_an_unlistable_root_fails_the_level_not_the_job(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder we cannot open is an answer, not a crash.

    ``os.scandir`` is patched rather than a real ACL built: the point is what the
    level does with the ``OSError``, and the walker's own denied-path handling is
    already pinned in ``tests/test_fsutil.py``.
    """

    def boom(_path: Any) -> Any:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(explorer.os, "scandir", boom)

    run = _explore(tree)
    totals = run.totals()

    assert totals["error"] is not None
    assert "Access is denied" in totals["error"]
    assert totals["children"] == 0
    assert run.rows() == []


def test_a_child_that_cannot_be_walked_lands_as_a_row_error(
    level: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreadable folder must not cost the level its other rows.

    ``walk_size`` absorbs everything it meets *inside* a tree; an ``OSError`` out of
    it means the child itself became unusable between the listing and the walk --
    a folder deleted mid-scan, or one whose ACL denies opening it at all.
    """
    real_walk = explorer.walk_size

    def selective(path: str, **kwargs: Any) -> Any:
        if os.path.basename(path) == "big":
            raise PermissionError(13, "Access is denied")
        return real_walk(path, **kwargs)

    monkeypatch.setattr(explorer, "walk_size", selective)

    run = _explore(level)
    rows = _by_name(run)
    totals = run.totals()

    assert rows["big"]["error"] == "Access is denied"
    assert rows["big"]["size"] == 0
    assert rows["small"]["size"] == 1024, "the other folder still measured"
    assert totals["measured"] == 1
    assert totals["denied_count"] == 1
    assert totals["total_size"] == 1024 + 5000


# ---------------------------------------------------------------------------
# Windows shapes: a junction is listed, a sparse file shows both numbers
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
def test_a_junction_is_listed_and_never_followed(tree: Path, tmp_path: Path) -> None:
    r"""BUG-01, made visible in the rows.

    ``%LOCALAPPDATA%\Application Data`` points at its own parent, and v1 walked it
    until it ran out of path. Here the junction has to appear -- hiding it would be
    a folder listing that disagrees with Explorer -- with no size, because a
    junction is not 0 B, it is somewhere else's bytes. Adding them would count the
    target twice in the same level.
    """
    outside = tmp_path / "elsewhere"
    write_file(outside / "payload.bin", 8192)
    write_file(tree / "real" / "own.bin", 2048)
    try:
        make_junction(tree / "link", outside)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    run = _explore(tree)
    rows = _by_name(run)
    totals = run.totals()

    assert rows["link"]["kind"] == "link"
    assert rows["link"]["size"] == 0
    assert rows["link"]["files"] == 0
    assert totals["links"] == 1
    assert totals["total_size"] == 2048, "the junction's target is not this level's"


@pytest.mark.windows_only
def test_a_sparse_file_reports_on_disk_below_logical(tree: Path) -> None:
    """BUG-12's other half: the two numbers are kept apart, per row.

    A file row is priced from the ``stat`` the listing already holds, so this also
    pins that :func:`~adc.engine.fsutil.allocated_size` is reached on that path --
    a file measured logically would report 64 MB of space that is not being used.
    """
    try:
        make_sparse_file(tree / "sparse.dat", logical=64 * 1024 * 1024)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    rows = _by_name(_explore(tree))

    assert rows["sparse.dat"]["logical"] == 64 * 1024 * 1024
    assert rows["sparse.dat"]["on_disk"] < rows["sparse.dat"]["logical"]
    assert rows["sparse.dat"]["size"] == rows["sparse.dat"]["on_disk"]
    assert rows["sparse.dat"]["divergent"] is True


# ---------------------------------------------------------------------------
# Settings: two apply, the rest deliberately do not
# ---------------------------------------------------------------------------


def test_size_on_disk_off_reports_logical_only(level: Path) -> None:
    """With the setting off, no row claims an on-disk number it did not measure."""
    run = _explore(level, settings=Settings(size_on_disk=False))
    rows = _by_name(run)

    assert run.totals()["measured_on_disk"] is False
    assert all(row["on_disk"] is None for row in rows.values())
    assert all(row["divergent"] is False for row in rows.values())
    assert rows["big"]["size"] == rows["big"]["logical"] == 14336


def test_the_volume_filter_does_not_reach_the_explorer(level: Path) -> None:
    """Deliberate: this view answers "what is using my disk", not "what may I clean".

    ``volume_ids``, ``exclusions`` and ``min_age_hours`` are clean-time settings.
    Narrowing a diagnostic by them would answer a different question than the one
    the user asked by opening the folder -- and the folder they opened is the filter.
    """
    hostile = Settings(volume_ids=("Z",), exclusions=(str(level / "big"),), min_age_hours=9999)

    rows = _by_name(_explore(level, settings=hostile))

    assert set(rows) == {"big", "small", "loose.bin"}
    assert rows["big"]["size"] == 14336


def test_a_second_visit_is_served_from_the_scan_cache(level: Path, tmp_path: Path) -> None:
    """Walking back up a level is nearly free, and the row says so.

    This is what makes per-level exploring affordable: drilling down measures new
    folders, and going back up hits ``(root, mtime_ns)`` for every child. ``cached``
    is surfaced per row so the UI can mark a number as possibly stale rather than
    pretending it was just measured.
    """
    cache = ScanCache(tmp_path / "explore-cache.sqlite")
    try:
        first = _by_name(_explore(level, cache=cache))
        second = _by_name(_explore(level, cache=cache))
    finally:
        cache.close()

    assert first["big"]["cached"] is False
    assert second["big"]["cached"] is True
    assert second["big"]["size"] == first["big"]["size"]


# ---------------------------------------------------------------------------
# The runner path: what the bridge actually calls
# ---------------------------------------------------------------------------


def test_submit_explore_runs_an_explore_job_to_done(level: Path) -> None:
    """One level on a worker thread, with the phases the UI draws.

    ``JobKind.EXPLORE`` has existed in ``jobs.py`` since P1 and was unused until
    now; this is the first job that claims it, and ``job_poll`` keys its payload
    shape off ``kind``.
    """
    runner = JobRunner()

    run = explorer.submit_explore(runner, root=level)

    assert run.job.kind is JobKind.EXPLORE
    assert _finish(run.job) is JobState.DONE
    snapshot = run.job.snapshot()
    assert [phase["key"] for phase in snapshot["phases"]] == ["list", "measure", "rank"]
    assert snapshot["pct"] == 1.0
    assert run.totals()["total_size"] == 20360
    assert [row["name"] for row in run.rows()] == ["big", "loose.bin", "small"]


def test_submit_explore_rejects_a_missing_root_before_starting_a_job(
    tmp_path: Path,
) -> None:
    """The failure is synchronous, so the bridge can answer ``not_present``.

    A job that starts and dies would put a failed scan in the history for what is
    really "that folder is gone" -- and would occupy the runner while doing it.
    """
    runner = JobRunner()

    with pytest.raises(FileNotFoundError):
        explorer.submit_explore(runner, root=tmp_path / "gone")

    assert list(runner) == [], "no job was registered for a folder that is not there"


def test_a_cancelled_level_is_cancelled_not_failed(level: Path) -> None:
    """Through the runner this time: the state the UI shows has to be CANCELLED.

    A cancel reported as FAILED would put a red error in the console for something
    the user asked for -- and would hide a genuine failure among the noise.
    """
    runner = JobRunner()

    run = explorer.submit_explore(runner, root=level)
    run.job.request_cancel()

    assert _finish(run.job) is JobState.CANCELLED


def test_an_empty_folder_is_a_valid_answer(tree: Path) -> None:
    """Zero children is not an error, and the totals must say zero rather than fail."""
    totals = _explore(tree).totals()

    assert totals["children"] == 0
    assert totals["total_size"] == 0
    assert totals["other_size"] == 0
    assert totals["omitted"] == 0
    assert totals["error"] is None


def test_path_for_resolves_the_handles_this_level_minted(level: Path) -> None:
    """The run is the id -> path table the bridge navigates by.

    Nothing else in the process can turn a handle into a path, which is what keeps
    ``explore_start`` from having to accept one from the page.
    """
    run = _explore(level)
    rows = _by_name(run)

    assert run.path_for(rows["big"]["node_id"]) == str(level / "big")
    assert run.path_for(rows["loose.bin"]["node_id"]) == str(level / "loose.bin")
    assert run.path_for(run.node_id) == str(level)
    assert run.path_for(run.crumbs[-2].node_id) == str(level.parent)


def test_path_for_refuses_a_handle_it_never_minted(level: Path) -> None:
    """A guessed or stale token resolves to nothing, and the bridge says so."""
    run = _explore(level)

    assert run.path_for("deadbeefcafe") is None
    assert run.path_for("") is None
