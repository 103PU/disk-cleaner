r"""Strategy tests. Nothing outside ``tmp_path`` is ever deleted.

The strategies are the only code in this project that destroys data, so the
tests are built around three rules:

* **The blast radius is a tmp tree.** Every ``Guard`` here is rooted inside
  ``tmp_path``, which means an escape does not merely fail an assertion -- it
  raises, because that is what ``Guard`` does to a path outside its roots.
* **Dry run is proved, not trusted.** ``CleanContext.dry_run`` defaults to True
  precisely so a caller that forgets cannot delete, and the tests below check the
  tree is byte-identical afterwards rather than checking a counter.
* **The two irreversible operations are never executed.** ``dism`` and
  ``powercfg /hibernate off`` are exercised in dry run only; asserting on their
  real effect would mean reconfiguring the machine running the suite.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adc.engine.guard import Guard, OutsideRootError
from adc.engine.jobs import CancelledError, CancelToken
from adc.engine.strategies import (
    Advise,
    CleanContext,
    HardDelete,
    RecycleDelete,
    ToolCommand,
    WinNative,
    sweep,
)
from tests.fixtures.make_tree import FixtureUnavailable, make_junction, write_file

IS_WINDOWS = os.name == "nt"


def _ctx(
    root: Path,
    *,
    paths: tuple[Path, ...] | None = None,
    dry_run: bool = True,
    min_age_hours: int = 0,
    exclusions: tuple[Path, ...] = (),
) -> CleanContext:
    """A context rooted at *root*, dry by default -- the same default as the class."""
    guard = Guard(
        roots=(str(root),), exclusions=tuple(str(path) for path in exclusions)
    )
    return CleanContext(
        target_id="test_target",
        paths=tuple(str(path) for path in (paths or (root,))),
        guard=guard,
        cancel=CancelToken(),
        dry_run=dry_run,
        min_age_hours=min_age_hours,
    )


def _snapshot(root: Path) -> dict[str, int]:
    """Every file under *root* with its size: the evidence a dry run changed nothing."""
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _make_cache(root: Path) -> None:
    """A small cache-shaped tree: nested directories, a few files, one deep."""
    write_file(root / "a.bin", size=2048)
    write_file(root / "b.bin", size=1024)
    write_file(root / "sub" / "c.bin", size=512)
    write_file(root / "sub" / "deep" / "d.bin", size=256)


# ---------------------------------------------------------------------------
# sweep() -- what a delete would touch
# ---------------------------------------------------------------------------
def test_sweep_never_lists_the_root_itself(tree: Path) -> None:
    r"""``%LOCALAPPDATA%\Temp`` must still exist afterwards; Windows assumes it.

    The root is the one directory a clean may not remove, and the way that is
    guaranteed is that it never enters the list in the first place.
    """
    _make_cache(tree)

    found = sweep(str(tree), guard=Guard(roots=(str(tree),)), cancel=CancelToken())

    assert str(tree) not in found.dirs
    assert str(tree) not in found.files
    assert len(found.files) == 4
    assert found.total_bytes == 2048 + 1024 + 512 + 256
    assert sorted(os.path.basename(p) for p in found.dirs) == ["deep", "sub"]


def test_sweep_orders_directories_deepest_first(tree: Path) -> None:
    """``rmdir`` has to walk out of the tree, so the order is the contract."""
    _make_cache(tree)

    found = sweep(str(tree), guard=Guard(roots=(str(tree),)), cancel=CancelToken())
    depths = [path.count(os.sep) for path in found.dirs]

    assert depths == sorted(depths, reverse=True)


def test_sweep_files_by_child_adds_up(tree: Path) -> None:
    """The fast path reports counts from this tally, so it has to be exact."""
    _make_cache(tree)

    found = sweep(str(tree), guard=Guard(roots=(str(tree),)), cancel=CancelToken())

    assert sum(found.files_by_child.values()) == len(found.files)
    assert set(found.files_by_child) <= set(found.children)


def test_sweep_lists_a_file_root_as_itself(tree: Path) -> None:
    """``MEMORY.DMP`` and a VHDX are targets that *are* a file."""
    dump = write_file(tree / "MEMORY.DMP", size=4096)

    found = sweep(str(dump), guard=Guard(roots=(str(tree),)), cancel=CancelToken())

    assert found.root_is_file
    assert len(found.files) == 1
    assert found.total_bytes == 4096
    assert found.dirs == []


def test_sweep_holds_back_files_younger_than_min_age(tree: Path) -> None:
    """A file an installer is still writing must survive the clean.

    ``min_age_hours`` is the whole safety story for ``%TEMP%``: v1 deleted
    whatever it found, including the scratch file of a running installer.
    """
    old = write_file(tree / "old.bin", size=100)
    write_file(tree / "fresh.bin", size=100)
    long_ago = 48 * 3600
    os.utime(old, (old.stat().st_atime - long_ago, old.stat().st_mtime - long_ago))

    found = sweep(
        str(tree), guard=Guard(roots=(str(tree),)), cancel=CancelToken(),
        min_age_hours=24,
    )

    assert [os.path.basename(path) for path in found.files] == ["old.bin"]
    assert found.skipped_recent == 1
    assert found.total_bytes == 100


def test_sweep_reports_an_exclusion_instead_of_listing_the_root(tree: Path) -> None:
    """A user exclusion covering the root skips it; it does not abort the run."""
    _make_cache(tree)
    guard = Guard(roots=(str(tree),), exclusions=(str(tree),))

    found = sweep(str(tree), guard=guard, cancel=CancelToken())

    assert found.excluded == 1
    assert found.files == []
    assert found.total_bytes == 0


def test_sweep_aborts_the_target_when_a_path_escapes(tmp_path: Path) -> None:
    """SPEC 4.7: an escape is not a skip. It stops the target.

    An exclusion means "the user said not this one"; a path outside the root means
    the catalogue and the filesystem disagree about what is being cleaned, and
    continuing would be deleting somewhere nobody asked for.
    """
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    write_file(outside / "keep.bin", size=64)

    with pytest.raises(OutsideRootError):
        sweep(str(outside), guard=Guard(roots=(str(inside),)), cancel=CancelToken())

    assert (outside / "keep.bin").exists()


def test_sweep_stops_on_cancel(tree: Path) -> None:
    """A cancel during enumeration is felt immediately, not after the walk."""
    _make_cache(tree)
    cancel = CancelToken()
    cancel.cancel()

    with pytest.raises(CancelledError):
        sweep(str(tree), guard=Guard(roots=(str(tree),)), cancel=cancel)

    assert _snapshot(tree), "a cancelled sweep deletes nothing -- it never deletes"


@pytest.mark.windows_only
def test_sweep_skips_a_junction_rather_than_descending_it(tmp_path: Path) -> None:
    """BUG-01 in its dangerous form: here it decides what gets deleted.

    A junction dropped into a cache directory between the scan and the clean is
    the TOCTOU attack this rule exists for. The sweep must list it as skipped and
    leave the tree it points at untouched.
    """
    root = tmp_path / "cache"
    elsewhere = tmp_path / "elsewhere"
    root.mkdir()
    write_file(root / "own.bin", size=64)
    write_file(elsewhere / "precious.bin", size=64)
    try:
        make_junction(root / "link", elsewhere)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    found = sweep(str(root), guard=Guard(roots=(str(root),)), cancel=CancelToken())

    assert [os.path.basename(path) for path in found.skipped_links] == ["link"]
    assert [os.path.basename(path) for path in found.files] == ["own.bin"]
    assert not any("precious" in path for path in found.files)
    assert (elsewhere / "precious.bin").exists()


# ---------------------------------------------------------------------------
# Dry run -- the default, and provably inert
# ---------------------------------------------------------------------------
def test_clean_context_defaults_to_dry_run(tree: Path) -> None:
    """A caller that forgets the flag must not be able to delete."""
    context = CleanContext(
        target_id="t", paths=(str(tree),),
        guard=Guard(roots=(str(tree),)), cancel=CancelToken(),
    )

    assert context.dry_run is True


@pytest.mark.parametrize("strategy", [HardDelete(), RecycleDelete()])
def test_dry_run_forecasts_without_touching_anything(
    tree: Path, strategy: HardDelete | RecycleDelete
) -> None:
    """The plan preview the UI shows. Same enumeration, zero side effects.

    Checked against a byte-for-byte snapshot rather than against a counter,
    because a counter is exactly what a broken dry run would still get right.
    """
    _make_cache(tree)
    before = _snapshot(tree)

    result = strategy.execute(_ctx(tree, dry_run=True))

    assert _snapshot(tree) == before
    assert result.dry_run is True
    assert result.ok
    assert result.files_deleted == 4
    assert result.dirs_deleted == 2
    assert result.bytes_deleted == 2048 + 1024 + 512 + 256


def test_dry_run_credits_no_bytes_when_an_exclusion_holds_something_back(
    tree: Path,
) -> None:
    """A forecast that ignores exclusions over-reports, which is the BUG-12 lie.

    The exclusion is applied to the file list *after* the sweep totalled the
    bytes, so the total is only honest when nothing was held back. It reports 0
    rather than a number it cannot stand behind.
    """
    _make_cache(tree)

    result = HardDelete().execute(
        _ctx(tree, dry_run=True, exclusions=(tree / "sub",))
    )

    assert result.files_deleted == 2, "the two files under sub/ were held back"
    assert result.bytes_deleted == 0
    assert any("exclusion" in note for note in result.notes)


# ---------------------------------------------------------------------------
# HardDelete -- the one that really removes files, inside tmp_path only
# ---------------------------------------------------------------------------
def test_hard_delete_empties_the_tree_and_keeps_the_root(tree: Path) -> None:
    """What a cache clean has to look like afterwards: the directory still there."""
    _make_cache(tree)

    result = HardDelete().execute(_ctx(tree, dry_run=False))

    assert tree.is_dir(), "the root must survive: Windows assumes %TEMP% exists"
    assert _snapshot(tree) == {}
    assert result.ok
    assert result.dry_run is False
    assert result.files_deleted == 4
    assert result.dirs_deleted == 2
    assert result.bytes_deleted == 2048 + 1024 + 512 + 256
    assert result.files_locked == 0
    assert result.denied == 0


def test_hard_delete_keeps_what_the_age_filter_held_back(tree: Path) -> None:
    """The fresh file survives, and so does the directory holding it."""
    old = write_file(tree / "sub" / "old.bin", size=100)
    fresh = write_file(tree / "sub" / "fresh.bin", size=100)
    long_ago = 48 * 3600
    os.utime(old, (old.stat().st_atime - long_ago, old.stat().st_mtime - long_ago))

    result = HardDelete().execute(_ctx(tree, dry_run=False, min_age_hours=24))

    assert not old.exists()
    assert fresh.exists(), "a file younger than min_age_hours must survive"
    assert (tree / "sub").is_dir(), "a directory still holding a file is not pruned"
    assert result.files_deleted == 1
    assert result.skipped_recent == 1
    assert result.dirs_deleted == 0


def test_hard_delete_honours_a_user_exclusion(tree: Path) -> None:
    """The excluded subtree is still there, and the run is still a success."""
    _make_cache(tree)

    result = HardDelete().execute(_ctx(tree, dry_run=False, exclusions=(tree / "sub",)))

    assert not (tree / "a.bin").exists()
    assert (tree / "sub" / "c.bin").exists()
    assert (tree / "sub" / "deep" / "d.bin").exists()
    assert result.ok
    assert result.files_deleted == 2
    assert result.bytes_deleted == 0, "something was held back; the total is not honest"


def test_hard_delete_leaves_a_junction_and_its_target_alone(tmp_path: Path) -> None:
    """Invariant 2 of the module, checked against the filesystem after the fact."""
    if not IS_WINDOWS:
        pytest.skip("junctions are an NTFS feature")
    root = tmp_path / "cache"
    elsewhere = tmp_path / "elsewhere"
    root.mkdir()
    write_file(root / "own.bin", size=64)
    write_file(elsewhere / "precious.bin", size=64)
    try:
        make_junction(root / "link", elsewhere)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    result = HardDelete().execute(_ctx(root, dry_run=False))

    assert (elsewhere / "precious.bin").exists(), "a junction was followed"
    assert (root / "link").exists(), "the junction itself must not be deleted either"
    assert not (root / "own.bin").exists()
    assert result.files_deleted == 1
    assert any("junction" in note for note in result.notes)


def test_hard_delete_deletes_a_file_target(tree: Path) -> None:
    """``MEMORY.DMP`` is the row this path exists for."""
    dump = write_file(tree / "MEMORY.DMP", size=4096)

    result = HardDelete().execute(_ctx(tree, paths=(dump,), dry_run=False))

    assert not dump.exists()
    assert result.files_deleted == 1


@pytest.mark.windows_only
def test_hard_delete_reports_a_locked_file_as_locked_not_denied(tree: Path) -> None:
    """SPEC 4.6: "Chrome has it open" and "you are not allowed" are different.

    v1 reported both as a failure with no detail, so a user whose browser was
    running saw a clean that had silently done nothing. The file is held open by
    this test process, which is the same Win32 condition (``ERROR_SHARING_VIOLATION``)
    a running Chrome produces -- and nothing is ever killed to get around it.
    """
    held = write_file(tree / "held.bin", size=128)
    write_file(tree / "free.bin", size=128)

    with open(held, "rb"):
        result = HardDelete().execute(_ctx(tree, dry_run=False))

    assert held.exists(), "a locked file must survive, not be forced"
    assert not (tree / "free.bin").exists(), "one locked file must not stop the rest"
    assert result.files_locked == 1
    assert result.denied == 0
    assert result.files_deleted == 1
    assert result.bytes_deleted == 0, "a partial delete credits no bytes"


@pytest.mark.windows_only
def test_recycle_delete_moves_files_to_the_bin(tree: Path) -> None:
    """The only undo this app has, exercised for real once.

    It leaves the probe file in the user's Recycle Bin, which is the point: the
    test would pass against a strategy that deleted outright if it only checked
    that the file was gone, so it also checks the shell reported a *move* and that
    the containing root survived.
    """
    _make_cache(tree)

    result = RecycleDelete().execute(_ctx(tree, dry_run=False))

    assert tree.is_dir()
    assert _snapshot(tree) == {}
    assert result.ok
    assert result.dry_run is False
    assert result.files_deleted == 4
    assert result.files_locked == 0
    assert result.denied == 0
    assert RecycleDelete.reversible is True
    assert RecycleDelete().describe()["undo"] == "Recycle Bin"


# ---------------------------------------------------------------------------
# The three non-deleting strategies
# ---------------------------------------------------------------------------
def test_tool_command_says_so_when_the_tool_is_missing(tree: Path) -> None:
    """Same fail-safe rule as the resolvers: report it, never improvise."""
    strategy = ToolCommand(argv=("adc-no-such-tool", "cache", "prune"))

    result = strategy.execute(_ctx(tree, dry_run=False))

    assert not result.ok
    assert result.skipped_reason == "adc-no-such-tool is not on PATH"
    assert result.files_deleted == 0


def test_tool_command_in_dry_run_only_says_what_it_would_run(tree: Path) -> None:
    """The preview must not run a command that frees space (BUG-07's ``uv``)."""
    strategy = ToolCommand(argv=("cmd", "/c", "exit", "0"))

    result = strategy.execute(_ctx(tree, dry_run=True))

    assert result.ok
    assert result.dry_run is True
    assert result.notes == ["would run: cmd /c exit 0"]


def test_tool_command_runs_without_a_shell_and_reports_the_exit_code(tree: Path) -> None:
    """A non-zero exit is a failure with the command in the reason, not a crash."""
    ok = ToolCommand(argv=("cmd", "/c", "exit", "0")).execute(_ctx(tree, dry_run=False))
    bad = ToolCommand(argv=("cmd", "/c", "exit", "3")).execute(_ctx(tree, dry_run=False))

    assert ok.ok
    assert not bad.ok
    assert bad.skipped_reason is not None
    assert "exited 3" in bad.skipped_reason


def test_tool_command_accepts_the_exit_codes_it_was_told_to(tree: Path) -> None:
    """``dism`` returns 3010 for "done, reboot to finish", which is a success."""
    strategy = ToolCommand(argv=("cmd", "/c", "exit", "3010"), ok_codes=(0, 3010))

    assert strategy.execute(_ctx(tree, dry_run=False)).ok


def test_win_native_refuses_an_unknown_op() -> None:
    """A catalogue typo must fail at import, not half way through a clean."""
    with pytest.raises(ValueError, match="unknown WinNative op"):
        WinNative("empty_the_whole_disk")


def test_win_native_admin_claim_is_per_op() -> None:
    """Emptying the bin needs no elevation; the other two do.

    The class-level ``needs_admin`` is True because two of the three ops need it,
    which is why ``describe()`` -- not the ClassVar -- is what the UI reads.
    """
    assert WinNative("empty_recycle_bin").describe()["needs_admin"] is False
    assert WinNative("dism_component_cleanup").describe()["needs_admin"] is True
    assert WinNative("hibernate_off").describe()["needs_admin"] is True


@pytest.mark.windows_only
def test_win_native_dry_run_never_reconfigures_the_machine(tree: Path) -> None:
    """The two irreversible ops are exercised in dry run and nowhere else.

    Running them for real would empty the user's Recycle Bin, delete
    ``hiberfil.sys`` and turn Fast Startup off -- on the machine running the
    suite. So what is asserted is that a dry run only *describes* them.
    """
    for op in WinNative.OPS:
        result = WinNative(op).execute(_ctx(tree, dry_run=True))

        assert result.dry_run is True
        assert result.files_deleted == 0
        if result.skipped_reason:
            # dism and powercfg are refused outright without elevation, which is
            # itself the safe answer.
            assert "Administrator" in result.skipped_reason, op
            continue
        assert result.notes, op
        assert all("would" in note for note in result.notes), op


def test_advise_touches_nothing_and_explains_in_both_languages(tree: Path) -> None:
    """Never a silent 0 B: the reason is the whole deliverable of this strategy."""
    _make_cache(tree)
    before = _snapshot(tree)
    logged: list[tuple[str, str]] = []
    context = _ctx(tree, dry_run=False)
    context.log = lambda _level, vi, en: logged.append((vi, en))
    strategy = Advise(
        reason_vi="Đổi pagefile trong System Properties.",
        reason_en="Resize the pagefile in System Properties.",
        handled_by="docker_prune",
    )

    result = strategy.execute(context)

    assert _snapshot(tree) == before
    assert result.ok
    assert result.files_deleted == 0
    assert result.skipped_reason == "Resize the pagefile in System Properties."
    assert result.notes == ["handled by: docker_prune"]
    assert logged == [(strategy.reason_vi, strategy.reason_en)]
    assert Advise.reversible is True
