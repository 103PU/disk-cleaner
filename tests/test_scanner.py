r"""Scanner orchestration tests: resolve -> measure -> summarise, honestly.

Four rules shape this module, mirroring ``tests/test_cleaner.py``.

* **Nothing real is ever reached.** Every target is synthetic and resolves into
  ``tmp_path``. ``scanner.find`` is monkeypatched, so no test can touch the
  fifty-row catalogue or a real machine path.
* **Cancel and expiry are different things.** An explicit ``cancel()`` stops the
  whole scan (``CancelledError`` out of :func:`run_scan`); a budget running out
  only truncates the affected row and lets the rest of the scan continue. That
  distinction is the whole point of this module, so both are pinned apart.
* **A truncated number is a floor, never a total.** ``truncated`` must be set
  wherever the budget cut a walk short, and the row it belongs to must still be
  the best partial answer rather than being dropped.
* **One bad row does not sink the scan.** An unknown id, a resolver that raises,
  and a root that vanishes must each land as an ``error`` on their own row while
  every other requested target still produces a result.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar

import pytest

from adc.engine import fsutil, scanner
from adc.engine.jobs import CancelledError
from adc.engine.resolvers import Resolution
from adc.engine.settings import Settings
from adc.engine.strategies import Advise
from adc.engine.targets import Category, Risk, Target
from adc.engine.volumes import volume_letter
from tests.fixtures.make_tree import make_plain_tree, write_file


@dataclass(eq=False)
class FakeResolver:
    """Resolves wherever the test says.

    ``eq=False`` rather than ``frozen=True``: :class:`Target` is frozen and
    therefore hashes its fields, and identity hashing is enough here.
    """

    paths: tuple[str, ...] = ()
    available: bool = True
    measurable: bool = True
    reason: str | None = None
    raises: BaseException | None = None
    kind: ClassVar[str] = "fake"

    @property
    def source(self) -> str:
        return "fake:test"

    def resolve(self) -> Resolution:
        if self.raises is not None:
            raise self.raises
        if not self.available:
            return Resolution.unavailable(self.reason or "not present", self.source)
        if not self.measurable or not self.paths:
            # Available with nothing to walk: the hibernation/DISM shape.
            return Resolution.systemic(self.source)
        return Resolution.found(list(self.paths), self.source)


def _target(
    target_id: str = "t_fake",
    *,
    paths: tuple[str, ...] = (),
    available: bool = True,
    measurable: bool = True,
    reason: str | None = None,
    raises: BaseException | None = None,
) -> Target:
    """A synthetic catalogue row pointing at *paths* and nothing else."""
    return Target(
        id=target_id,
        name_vi=f"Mục {target_id}",
        name_en=f"Target {target_id}",
        desc_vi="Chỉ dùng trong test.",
        desc_en="Test-only row.",
        category=Category.TEMP,
        risk=Risk.SAFE,
        resolver=FakeResolver(
            paths=paths, available=available, measurable=measurable,
            reason=reason, raises=raises,
        ),
        strategy=Advise(reason_vi="Chỉ để đọc.", reason_en="Read-only."),
    )


@pytest.fixture
def catalogue(monkeypatch: pytest.MonkeyPatch) -> dict[str, Target]:
    """Replace the catalogue lookup with a dict the test fills.

    ``_resolve_row`` calls ``find`` in this module's namespace, so patching it
    here is what guarantees a test cannot reach the real catalogue even if it
    names a real target id.
    """
    rows: dict[str, Target] = {}
    monkeypatch.setattr(scanner, "find", rows.get)
    return rows


def _register(rows: dict[str, Target], target: Target) -> Target:
    rows[target.id] = target
    return target


def _run(target_ids: tuple[str, ...], **kwargs: object) -> scanner.ScanRun:
    """Build a fresh job and run it synchronously, per ``run_scan``'s own docstring."""
    run = scanner.ScanRun(scanner.new_job(), target_ids)
    return scanner.run_scan(run, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Ground truth: sizes and counts pin to what was actually built
# ---------------------------------------------------------------------------
def test_scan_totals_match_a_synthetic_tree(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """The number the summary bar shows is the number the fixture built, exactly."""
    root, total_bytes, total_files = make_plain_tree(tree / "one", files_per_dir=3, depth=2)
    _register(catalogue, _target("one", paths=(str(root),)))

    run = _run(("one",), settings=Settings())

    rows = {row["target_id"]: row for row in run.rows()}
    assert rows["one"]["size"] == total_bytes
    assert rows["one"]["files"] == total_files
    assert rows["one"]["available"] is True
    assert rows["one"]["error"] is None

    totals = run.totals()
    assert totals["total_size"] == total_bytes
    assert totals["scanned"] == 1
    assert totals["requested"] == 1
    assert totals["truncated"] is False


def test_a_target_with_two_paths_sums_both(tree, clean_env, catalogue: dict[str, Target]) -> None:
    """``TargetScan.absorb`` is additive: one row, many paths, one total."""
    write_file(tree / "a" / "f.bin", 1000)
    write_file(tree / "b" / "f.bin", 2000)
    _register(catalogue, _target("two_paths", paths=(str(tree / "a"), str(tree / "b"))))

    run = _run(("two_paths",), settings=Settings())

    row = run.rows()[0]
    assert row["size"] == 3000
    assert row["files"] == 2


# ---------------------------------------------------------------------------
# Cancellation: an explicit stop is not a budget running out
# ---------------------------------------------------------------------------
def test_explicit_cancel_raises_and_stops_the_whole_scan(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """A user's stop must abort ``run_scan`` itself, not just truncate a row.

    ``JobRunner`` is what turns this into a ``CANCELLED`` job; swallowing it
    inside ``run_scan`` would report a partial scan as a complete one.
    """
    write_file(tree / "a" / "f.bin", 100)
    _register(catalogue, _target("a", paths=(str(tree / "a"),)))
    _register(catalogue, _target("b", paths=(str(tree / "a"),)))

    run = scanner.ScanRun(scanner.new_job(), ("a", "b"))
    run.job.cancel_token.cancel()

    with pytest.raises(CancelledError):
        scanner.run_scan(run, settings=Settings())


def test_budget_expiry_truncates_but_does_not_raise(
    tree, clean_env, monkeypatch: pytest.MonkeyPatch, catalogue: dict[str, Target]
) -> None:
    """Running out of time is a truncation, never a raised exception.

    ``CLOCK_EVERY`` is pinned to 1 so the wall-clock check inside
    ``fsutil.walk_size`` fires on the very first entry instead of every 256 --
    the same technique ``test_fsutil_budget_truncates`` uses -- so a handful of
    files is enough to trip the budget deterministically and fast.
    """
    monkeypatch.setattr(fsutil, "CLOCK_EVERY", 1)
    # A budget of exactly 0.0 is what reliably expires on the very first entry:
    # the deadline then equals the walk's start time exactly, so the next clock
    # reading satisfies ">=" by equality alone, with no real elapsed time
    # needed -- which matters on this host's ~15ms GetTickCount64 resolution.
    # ``budget_from_env`` treats an env value of 0 as "no budget" by contract
    # ("Zero means no budget"), so the only way to hand ``walk_size`` a literal
    # 0.0 is to patch ``budget_from_env`` itself rather than go through the env
    # var or ``Settings.scan_budget_s`` (which scanner.py's own ``0.0 or
    # DEFAULT_PATH_BUDGET_S`` falsy-check would silently replace with 90s).
    monkeypatch.setattr(scanner, "budget_from_env", lambda fallback: 0.0)
    root, _total_bytes, _total_files = make_plain_tree(tree / "slow", files_per_dir=5, depth=2)
    _register(catalogue, _target("slow", paths=(str(root),)))

    run = _run(("slow",), settings=Settings())

    row = run.rows()[0]
    assert row["truncated"] is True
    assert run.totals()["truncated"] is True
    # A truncated row is still a scanned row -- a floor, not a failure.
    assert row["error"] is None


def test_budget_expiry_on_one_target_still_lets_the_others_finish(
    tree, clean_env, monkeypatch: pytest.MonkeyPatch, catalogue: dict[str, Target]
) -> None:
    """Truncation is per-path/target, not a scan-wide abort.

    ``budget_from_env`` hands every target in one ``run_scan`` call the same
    budget, so a shared tiny budget cannot deterministically truncate one
    target's walk while letting another's finish -- both would trip on their
    first entry. Instead, ``scanner.walk_size`` (the name scanner.py's own
    ``_measure_row`` calls) is monkeypatched to mark exactly one root's result
    truncated after a real walk, isolating the row-level bookkeeping from the
    walker's own timing.
    """
    slow_root, _slow_bytes, _slow_files = make_plain_tree(tree / "slow", files_per_dir=2, depth=1)
    fine_root, fine_bytes, fine_files = make_plain_tree(tree / "fine", files_per_dir=1, depth=1)
    _register(catalogue, _target("slow", paths=(str(slow_root),)))
    _register(catalogue, _target("fine", paths=(str(fine_root),)))

    real_walk_size = scanner.walk_size

    def flaky_walk_size(path: str, **kwargs: object) -> fsutil.WalkResult:
        result = real_walk_size(path, **kwargs)  # type: ignore[arg-type]
        if path == str(slow_root):
            result.truncated = True
        return result

    monkeypatch.setattr(scanner, "walk_size", flaky_walk_size)

    run = _run(("slow", "fine"), settings=Settings())

    rows = {row["target_id"]: row for row in run.rows()}
    assert rows["slow"]["truncated"] is True
    assert rows["fine"]["truncated"] is False
    assert rows["fine"]["error"] is None
    assert rows["fine"]["files"] == fine_files
    assert rows["fine"]["size"] == fine_bytes
    assert run.totals()["truncated"] is True


# ---------------------------------------------------------------------------
# Per-target isolation: one bad row must not sink the scan
# ---------------------------------------------------------------------------
def test_unknown_target_id_becomes_an_error_row_not_a_crash(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """The bridge and the catalogue getting out of step is a reportable bug, not a crash."""
    write_file(tree / "ok" / "f.bin", 10)
    _register(catalogue, _target("ok", paths=(str(tree / "ok"),)))

    run = _run(("ok", "does_not_exist"), settings=Settings())

    rows = {row["target_id"]: row for row in run.rows()}
    assert rows["does_not_exist"]["error"] == "unknown target id: does_not_exist"
    assert rows["does_not_exist"]["available"] is False
    # The other target still produced a result.
    assert rows["ok"]["error"] is None
    assert rows["ok"]["files"] == 1
    assert run.totals()["requested"] == 2


def test_resolver_that_raises_lands_on_its_own_row(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """A resolver is third-party-ish (a tool, a glob, a scandir); its failure is contained."""
    write_file(tree / "ok" / "f.bin", 10)
    _register(catalogue, _target("ok", paths=(str(tree / "ok"),)))
    _register(catalogue, _target("boom", raises=RuntimeError("tool not on PATH")))

    run = _run(("boom", "ok"), settings=Settings())

    rows = {row["target_id"]: row for row in run.rows()}
    assert rows["boom"]["error"] == "resolve failed: tool not on PATH"
    assert rows["ok"]["files"] == 1
    assert run.totals()["failed"] == 1
    assert run.totals()["scanned"] == 1


def test_vanished_root_is_reported_on_its_row_not_raised(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """A root deleted between resolve and measure must not abort the scan.

    ``fsutil.walk_size`` reports a missing root via ``denied`` rather than
    raising (see ``test_fsutil_missing_root_is_reported_not_raised``), so this
    row comes back scanned-but-empty, not errored -- pinning that behaviour one
    layer up, at the scanner.
    """
    missing = tree / "gone"
    write_file(tree / "ok" / "f.bin", 10)
    _register(catalogue, _target("gone", paths=(str(missing),)))
    _register(catalogue, _target("ok", paths=(str(tree / "ok"),)))

    run = _run(("gone", "ok"), settings=Settings())

    rows = {row["target_id"]: row for row in run.rows()}
    assert rows["gone"]["error"] is None
    assert rows["gone"]["size"] == 0
    assert rows["gone"]["denied_count"] == 1
    assert rows["ok"]["files"] == 1


def test_a_file_where_a_directory_was_expected_is_measured_as_one_file(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """``walk_size`` measures a single file directly; a target need not be a directory."""
    write_file(tree / "not_a_dir.bin", 500)
    write_file(tree / "ok" / "f.bin", 10)
    _register(catalogue, _target("single_file", paths=(str(tree / "not_a_dir.bin"),)))
    _register(catalogue, _target("ok", paths=(str(tree / "ok"),)))

    run = _run(("single_file", "ok"), settings=Settings())

    rows = {row["target_id"]: row for row in run.rows()}
    assert rows["single_file"]["size"] == 500
    assert rows["single_file"]["files"] == 1
    assert rows["ok"]["files"] == 1


# ---------------------------------------------------------------------------
# Availability and measurability: absent and not-measurable are not 0 B
# ---------------------------------------------------------------------------
def test_unavailable_target_is_a_row_with_a_reason_not_a_zero(
    clean_env, catalogue: dict[str, Target]
) -> None:
    """Absent is ``available=False`` with a reason, never a silent 0 B (BUG-12's class of lie)."""
    _register(catalogue, _target("absent", available=False, reason="not installed"))

    run = _run(("absent",), settings=Settings())

    row = run.rows()[0]
    assert row["available"] is False
    assert row["reason"] == "not installed"
    assert row["size"] == 0
    totals = run.totals()
    assert totals["unavailable"] == 1
    assert totals["scanned"] == 0
    assert totals["total_size"] == 0


def test_available_but_not_measurable_target_is_not_a_zero_either(
    clean_env, catalogue: dict[str, Target]
) -> None:
    """Hibernation/DISM-shaped targets: present, but nothing to walk."""
    _register(catalogue, _target("systemic", measurable=False))

    run = _run(("systemic",), settings=Settings())

    row = run.rows()[0]
    assert row["available"] is True
    assert row["measurable"] is False
    totals = run.totals()
    assert totals["not_measurable"] == 1
    assert totals["scanned"] == 0
    assert totals["total_size"] == 0


# ---------------------------------------------------------------------------
# Selection shape: empty and duplicate
# ---------------------------------------------------------------------------
def test_empty_selection_scans_nothing_and_does_not_crash(
    clean_env, catalogue: dict[str, Target]
) -> None:
    """Nothing requested, nothing measured, no division-by-zero in the progress math."""
    run = _run((), settings=Settings())

    assert run.rows() == []
    totals = run.totals()
    assert totals["requested"] == 0
    assert totals["scanned"] == 0
    assert totals["total_size"] == 0


def test_duplicate_id_in_the_selection_scans_the_same_row_twice(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """``ScanRun.row`` is get-or-create by id, so a duplicate id shares one row.

    The row is walked once per occurrence in ``target_ids`` and ``absorb`` is
    additive, so a duplicate silently doubles that row's counted size. This is
    the observed behaviour, pinned so a future change to it is a deliberate one.
    """
    root, total_bytes, total_files = make_plain_tree(tree / "dup", files_per_dir=2, depth=1)
    _register(catalogue, _target("dup", paths=(str(root),)))

    run = _run(("dup", "dup"), settings=Settings())

    assert run.totals()["requested"] == 2
    rows = run.rows()
    assert len(rows) == 1
    assert rows[0]["size"] == total_bytes * 2
    assert rows[0]["files"] == total_files * 2


# ---------------------------------------------------------------------------
# Phase order and bilingual events
# ---------------------------------------------------------------------------
def test_phases_advance_in_the_documented_order(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """resolve -> measure -> summarise, and earlier phases are marked done."""
    write_file(tree / "ok" / "f.bin", 10)
    _register(catalogue, _target("ok", paths=(str(tree / "ok"),)))

    run = _run(("ok",), settings=Settings())

    snapshot = run.job.snapshot()
    keys = [phase["key"] for phase in snapshot["phases"]]
    assert keys == ["resolve", "measure", "summarise"]
    # entering a later phase marks every *earlier* one done; the final phase is
    # only marked done by Job.finish(), which is JobRunner's job, not run_scan's.
    done_by_key = {phase["key"]: phase["done"] for phase in snapshot["phases"]}
    assert done_by_key["resolve"] is True
    assert done_by_key["measure"] is True
    assert snapshot["current_phase"] == "summarise"
    assert snapshot["pct"] == pytest.approx(1.0)


def test_log_events_carry_both_languages(tree, clean_env, catalogue: dict[str, Target]) -> None:
    """Every emitted line is bilingual, per SPEC 7 -- the UI has no fallback."""
    _register(catalogue, _target("absent", available=False, reason="not present"))

    run = _run(("absent",), settings=Settings())

    events = run.job.snapshot()["events"]
    assert events, "expected at least one log line for an unavailable target"
    for event in events:
        assert event["message_vi"], event
        assert event["message_en"], event


# ---------------------------------------------------------------------------
# Scan cache: off by default under clean_env, and reused when handed one
# ---------------------------------------------------------------------------
def test_no_scan_cache_env_var_means_no_cache_is_ever_consulted(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """``ADC_NO_SCAN_CACHE=1`` (``clean_env``) turns the whole cache off."""
    assert scanner.cache_enabled() is False
    root, total_bytes, total_files = make_plain_tree(tree / "one", files_per_dir=2, depth=1)
    _register(catalogue, _target("one", paths=(str(root),)))

    run = _run(("one",), settings=Settings())

    row = run.rows()[0]
    assert row["cached"] is False
    assert row["size"] == total_bytes
    assert row["files"] == total_files


def test_an_explicit_cache_is_reused_on_a_second_scan(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """Handing ``run_scan`` its own ``ScanCache`` lets a second, unchanged scan hit it.

    ``run_scan`` only owns (and closes) a cache it built itself
    (``cache_enabled()``); a caller-supplied cache is the caller's to keep open
    across calls, which is what makes a hit observable here.
    """
    root, total_bytes, total_files = make_plain_tree(tree / "one", files_per_dir=2, depth=1)
    _register(catalogue, _target("one", paths=(str(root),)))
    cache = fsutil.ScanCache(":memory:")

    first = _run(("one",), settings=Settings(), cache=cache)
    assert first.rows()[0]["cached"] is False

    second = _run(("one",), settings=Settings(), cache=cache)
    row = second.rows()[0]
    assert row["cached"] is True
    assert row["size"] == total_bytes
    assert row["files"] == total_files


def test_a_stale_cache_entry_is_invalidated_by_a_changed_mtime(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """A directory's mtime moving is what tells the cache its old entry is stale."""
    root = tree / "one"
    write_file(root / "f.bin", 100)
    _register(catalogue, _target("one", paths=(str(root),)))
    cache = fsutil.ScanCache(":memory:")

    first = _run(("one",), settings=Settings(), cache=cache)
    assert first.rows()[0]["cached"] is False

    write_file(root / "g.bin", 50)
    mtime = os.stat(root).st_mtime + 5.0
    os.utime(root, (mtime, mtime))

    second = _run(("one",), settings=Settings(), cache=cache)
    row = second.rows()[0]
    assert row["cached"] is False
    assert row["size"] == 150
    assert row["files"] == 2


def test_a_truncated_walk_is_never_stored_in_the_cache(
    tree, clean_env, monkeypatch: pytest.MonkeyPatch, catalogue: dict[str, Target]
) -> None:
    """Caching a floor as if it were a total would make the lie permanent."""
    monkeypatch.setattr(fsutil, "CLOCK_EVERY", 1)
    # See test_budget_expiry_truncates_but_does_not_raise: budget_from_env is
    # patched directly to hand walk_size a literal 0.0, the one value that
    # reliably expires on the very first entry regardless of clock resolution.
    monkeypatch.setattr(scanner, "budget_from_env", lambda fallback: 0.0)
    root, _, _ = make_plain_tree(tree / "slow", files_per_dir=5, depth=2)
    _register(catalogue, _target("slow", paths=(str(root),)))
    cache = fsutil.ScanCache(":memory:")

    first = _run(("slow",), settings=Settings(), cache=cache)
    assert first.rows()[0]["truncated"] is True

    # No budget this time: if the truncated walk had been cached, this second
    # pass would come back cached=True instead of walking for real.
    monkeypatch.setattr(scanner, "budget_from_env", lambda fallback: None)
    monkeypatch.setattr(fsutil, "CLOCK_EVERY", 256)
    second = _run(("slow",), settings=Settings(), cache=cache)
    assert second.rows()[0]["cached"] is False


# ---------------------------------------------------------------------------
# Volume filter: settings.volume_ids narrows the walk (SPEC 6.6)
# ---------------------------------------------------------------------------
def _letter_of(path: object) -> str:
    return volume_letter(str(path))


def _another_letter(letter: str) -> str:
    """A drive letter that is certainly not *letter*, for "the disk you unticked"."""
    return "Y" if letter == "Z" else "Z"


def test_a_row_outside_the_ticked_volumes_is_never_walked(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """Unticking a volume keeps it out of the scan, with the reason on the row.

    The row must read as unavailable rather than as 0 B: a total of zero for a
    directory nobody looked at is the same lie as BUG-12.
    """
    root, total_bytes, _ = make_plain_tree(tree / "one", files_per_dir=2, depth=1)
    assert total_bytes > 0
    _register(catalogue, _target("one", paths=(str(root),)))
    elsewhere = _another_letter(_letter_of(root))

    run = _run(("one",), settings=Settings(volume_ids=(elsewhere,)))

    row = run.rows()[0]
    assert row["available"] is False
    assert row["reason"] == f"excluded by the volume filter ({_letter_of(root)}:)"
    assert row["size"] == 0
    assert row["path_count"] == 0
    totals = run.totals()
    assert totals["unavailable"] == 1
    assert totals["scanned"] == 0
    assert totals["total_size"] == 0


def test_ticking_the_volume_a_row_lives_on_scans_it_normally(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """The other half of the pair: a filter that names this disk changes nothing."""
    root, total_bytes, total_files = make_plain_tree(tree / "one", files_per_dir=2, depth=1)
    _register(catalogue, _target("one", paths=(str(root),)))

    run = _run(("one",), settings=Settings(volume_ids=(_letter_of(root),)))

    row = run.rows()[0]
    assert row["available"] is True
    assert row["size"] == total_bytes
    assert row["files"] == total_files


def test_the_filter_keeps_half_a_row_when_a_target_spans_disks(
    tree, clean_env, catalogue: dict[str, Target]
) -> None:
    """The ``RecycleBins`` shape: one row, one path per volume, only some ticked.

    The excluded path is dropped before anything walks it, which is why it can be
    a location that does not exist -- the filter is not a measurement.
    """
    write_file(tree / "here" / "f.bin", 400)
    here = str(tree / "here")
    elsewhere = _another_letter(_letter_of(here)) + ":" + os.sep + "nope"
    _register(catalogue, _target("split", paths=(here, elsewhere)))

    run = _run(("split",), settings=Settings(volume_ids=(_letter_of(here),)))

    row = run.rows()[0]
    assert row["available"] is True
    assert row["path_count"] == 1
    assert row["size"] == 400
    assert row["error"] is None, "the excluded path must not be walked, so it cannot fail"
    dropped = [e for e in run.job.snapshot()["events"] if "outside the chosen volumes" in
               e["message_en"]]
    assert len(dropped) == 1, "a partial exclusion has to be visible in the log"
    assert dropped[0]["message_vi"], "bilingual, per SPEC 7"


def test_a_systemic_row_is_not_touched_by_the_volume_filter(
    clean_env, catalogue: dict[str, Target]
) -> None:
    """Hibernation and DISM have no path to attribute, so no tick list excludes them."""
    _register(catalogue, _target("systemic", measurable=False))

    run = _run(("systemic",), settings=Settings(volume_ids=("Z",)))

    row = run.rows()[0]
    assert row["available"] is True
    assert row["measurable"] is False
    assert row["reason"] is None
    assert run.totals()["not_measurable"] == 1
