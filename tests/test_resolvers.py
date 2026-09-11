r"""Resolver tests -- the "I will not guess" rule, one case per way of guessing.

BUG-02 and BUG-03 were both the same mistake made twice: v1 had a fallback. When
it could not find the pnpm store it used ``%LOCALAPPDATA%\pnpm``; when a tool was
not installed it used a path shaped like the one that tool usually has. Both then
appeared in the UI as findings.

So the invariant under test throughout is negative. A resolver either returns
paths it observed on disk, or it returns ``unavailable`` with a reason naming what
was missing -- and the tests are written to catch the third answer appearing:

* an unset variable produces no path at all, not a relative one;
* a tool that prints garbage, exits non-zero, or names a directory it has not
  created yet produces ``unavailable``, not its own guess;
* ``AnyOf`` with every part missing reports every reason rather than an empty find.

Nothing here deletes. The only writes are into ``tmp_path``, and the tool cache is
redirected so a test run never edits the user's ``cache/tools.json``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from adc.engine.resolvers import (
    CACHE_OFF_ENV,
    MAX_REPORTED_PATHS,
    AnyOf,
    GlobPath,
    RecycleBins,
    Resolution,
    Resolver,
    StaticPath,
    Systemic,
    ToolPresence,
    ToolQuery,
    expand,
    narrow_to_volumes,
    tool_cache,
)
from adc.engine.volumes import fixed_volumes, volume_letter

IS_WINDOWS = os.name == "nt"

# Long enough that no test can be made to fail by being slow; the TTL itself is
# exercised by backdating an entry rather than by waiting.
TTL_GENEROUS = 3_600.0


# ---------------------------------------------------------------------------
# expand() -- the empty string is the refusal
# ---------------------------------------------------------------------------
def test_expand_resolves_a_variable_and_normalises() -> None:
    assert expand("%SystemRoot%") == os.path.normpath(os.environ["SYSTEMROOT"])
    assert expand(r"%SystemRoot%\Temp\..\Temp") == os.path.join(
        os.path.normpath(os.environ["SYSTEMROOT"]), "Temp"
    )


def test_expand_refuses_rather_than_dropping_an_unset_variable() -> None:
    r"""``%NOPE%\Cache`` must not become the relative path ``Cache``.

    A relative path would be resolved against the process's working directory,
    which is how a "clean the cache" run becomes a delete inside the app's own
    install tree.
    """
    assert expand(r"%ADC_DEFINITELY_UNSET%\Cache") == ""
    assert expand("%ADC_DEFINITELY_UNSET%") == ""
    assert expand("") == ""
    assert expand("   ") == ""

def test_expand_leaves_a_dollar_literal_alone() -> None:
    r"""``$PatchCache$`` is a real directory name under ``%SystemRoot%``.

    ``os.path.expandvars`` would read ``$PatchCache`` as a POSIX variable and eat
    it. That is why this module has its own ``%NAME%``-only regex rather than
    calling the stdlib.
    """
    got = expand(r"%SystemRoot%\$PatchCache$")
    assert got == os.path.join(os.path.normpath(os.environ["SYSTEMROOT"]), "$PatchCache$")
    assert got.endswith("$PatchCache$")


def test_expand_resolves_a_tilde_and_strips_surrounding_space() -> None:
    assert expand("~") == os.path.normpath(os.path.expanduser("~"))
    assert expand(r"~\Downloads") == os.path.join(
        os.path.normpath(os.path.expanduser("~")), "Downloads"
    )
    assert expand("  %SystemRoot%  ") == os.path.normpath(os.environ["SYSTEMROOT"])


# ---------------------------------------------------------------------------
# Resolution -- what a caller is allowed to trust
# ---------------------------------------------------------------------------
def test_a_bare_resolution_is_unavailable() -> None:
    """The default must be the safe answer, so a forgotten field cannot offer
    a target that was never resolved."""
    blank = Resolution()
    assert not blank.available
    assert blank.paths == ()
    assert blank.measurable is True
    assert blank.cached is False


def test_found_dedupes_case_insensitively_and_keeps_order() -> None:
    r"""``C:\A`` and ``c:\a`` are one directory; counting both doubles the estimate."""
    found = Resolution.found([r"C:\A", r"c:\a", r"C:\B", r"C:\A"], "test")
    assert found.paths == (r"C:\A", r"C:\B")
    assert found.available


def test_unavailable_carries_a_reason_and_never_a_path() -> None:
    out = Resolution.unavailable("pnpm is not on PATH", "tool:pnpm store path")
    assert not out.available
    assert out.reason == "pnpm is not on PATH"
    assert out.paths == ()


def test_systemic_is_available_but_not_measurable() -> None:
    """Hibernation frees bytes and has no directory. Reporting it as 0 B would be
    BUG-12 again, so ``measurable`` carries the distinction instead."""
    out = Resolution.systemic("system:hibernation")
    assert out.available
    assert out.measurable is False
    assert out.paths == ()


def test_as_dict_caps_the_path_list_but_not_the_count() -> None:
    """53 Chrome cache directories is normal; a UI row does not need all of them,
    but the count must stay honest or the estimate looks wrong."""
    many = [rf"C:\p{index}" for index in range(MAX_REPORTED_PATHS + 10)]
    payload = Resolution.found(many, "test").as_dict()
    assert payload["path_count"] == MAX_REPORTED_PATHS + 10
    assert len(payload["paths"]) == MAX_REPORTED_PATHS
    assert payload["paths"] == many[:MAX_REPORTED_PATHS]
    assert payload["available"] is True and payload["reason"] is None


def test_as_dict_is_json_shaped() -> None:
    payload = Resolution.unavailable("nope", "static:%X%").as_dict()
    assert json.loads(json.dumps(payload)) == payload


def test_every_concrete_resolver_satisfies_the_protocol() -> None:
    """``Resolver`` is structural, so this is the only place the shape is checked.

    A new resolver that forgets ``source`` would otherwise fail at scan time, in
    the middle of a job, on whichever machine happened to have that tool.
    """
    for resolver in (
        StaticPath("%SystemRoot%"),
        GlobPath(r"%SystemRoot%\*"),
        ToolQuery(("cmd", "/c", "echo", ".")),
        ToolPresence("cmd"),
        RecycleBins(),
        Systemic("hibernation"),
        AnyOf((StaticPath("%SystemRoot%"),)),
    ):
        assert isinstance(resolver, Resolver), type(resolver).__name__
        assert resolver.source
        assert resolver.kind


# ---------------------------------------------------------------------------
# StaticPath -- the two ways one fixed location can fail
# ---------------------------------------------------------------------------
def test_static_path_names_the_variable_it_could_not_expand() -> None:
    out = StaticPath(r"%ADC_DEFINITELY_UNSET%\Cache").resolve()
    assert not out.available
    assert "environment variable unset" in (out.reason or "")
    assert "%ADC_DEFINITELY_UNSET%" in (out.reason or "")
    assert out.paths == ()
    assert out.source == r"static:%ADC_DEFINITELY_UNSET%\Cache"


def test_static_path_distinguishes_absent_from_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"the variable is unset" and "the directory is not there" are different
    problems for the user, and v1 reported neither."""
    monkeypatch.setenv("ADC_TEST_ROOT", str(tmp_path))
    out = StaticPath(r"%ADC_TEST_ROOT%\nothing-here").resolve()
    assert not out.available
    assert (out.reason or "").startswith("not present: ")
    assert str(tmp_path) in (out.reason or "")


def test_static_path_returns_the_directory_it_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Temp").mkdir()
    monkeypatch.setenv("ADC_TEST_ROOT", str(tmp_path))
    out = StaticPath(r"%ADC_TEST_ROOT%\Temp").resolve()
    assert out.available
    assert out.paths == (os.path.normpath(str(tmp_path / "Temp")),)
    assert out.measurable and not out.cached


# ---------------------------------------------------------------------------
# GlobPath -- several matches, or a reason naming the pattern
# ---------------------------------------------------------------------------
def _packages(tmp_path: Path) -> Path:
    r"""``Packages\<app>\LocalState\ext4.vhdx`` in miniature, plus a decoy file."""
    root = tmp_path / "Packages"
    for app in ("AppB", "AppA"):
        state = root / app / "LocalState"
        state.mkdir(parents=True)
        (state / "ext4.vhdx").write_bytes(b"\0" * 16)
    (root / "loose.txt").write_text("not a package", encoding="utf-8")
    return root


def test_glob_path_lists_every_match_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _packages(tmp_path)
    monkeypatch.setenv("ADC_TEST_ROOT", str(tmp_path))
    out = GlobPath(r"%ADC_TEST_ROOT%\Packages\*\LocalState\ext4.vhdx").resolve()
    assert out.available
    assert [os.path.basename(os.path.dirname(os.path.dirname(p))) for p in out.paths] == [
        "AppA",
        "AppB",
    ]


def test_glob_path_with_dirs_only_drops_the_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""A pattern like ``Packages\*`` matches loose files too, and handing one to a
    directory walker would report it as an empty tree."""
    _packages(tmp_path)
    monkeypatch.setenv("ADC_TEST_ROOT", str(tmp_path))
    everything = GlobPath(r"%ADC_TEST_ROOT%\Packages\*").resolve()
    dirs = GlobPath(r"%ADC_TEST_ROOT%\Packages\*", dirs_only=True).resolve()
    assert len(everything.paths) == 3
    assert sorted(os.path.basename(p) for p in dirs.paths) == ["AppA", "AppB"]


def test_glob_path_says_what_it_looked_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADC_TEST_ROOT", str(tmp_path))
    out = GlobPath(r"%ADC_TEST_ROOT%\Packages\*\ext4.vhdx").resolve()
    assert not out.available
    assert (out.reason or "").startswith("no match for ")
    assert out.paths == ()


def test_glob_path_refuses_an_unset_variable() -> None:
    out = GlobPath(r"%ADC_DEFINITELY_UNSET%\*").resolve()
    assert not out.available
    assert "environment variable unset" in (out.reason or "")


# ---------------------------------------------------------------------------
# ToolQuery -- believing a tool only when it prints a real path
# ---------------------------------------------------------------------------
_QUERY = ToolQuery(("adc-parser-only",))


@pytest.mark.windows_only
@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("", None),
        ("   \n\n", None),
        ("cache", None),                              # relative: BUG-03's shape
        (r"..\..\pnpm-store", None),
        (r"C:\Store\*", None),                        # a pattern, not a path
        (r"C:\Store\?", None),
        ("C:/Store\0evil", None),
        ("C:" + os.sep + "a" * 5000, None),           # absurd line, refused on length
        ("npm WARN config\n" + r"E:\.pnpm-store\v11", r"E:\.pnpm-store\v11"),
        ('"' + r"E:\.pnpm-store\v11" + '"', r"E:\.pnpm-store\v11"),
        ("E:/.pnpm-store/v11", r"E:\.pnpm-store\v11"),
    ],
)
def test_first_path_accepts_only_something_shaped_like_a_path(
    stdout: str, expected: str | None
) -> None:
    """Parsing is where a fallback would sneak back in.

    Every rejected case here is output a real tool has been seen to produce -- a
    warning banner above the answer, a relative path, a quoted one -- and none of
    them may become a directory ADC then deletes.
    """
    assert _QUERY._first_path(stdout) == expected


def test_tool_query_refuses_a_tool_that_is_not_installed() -> None:
    out = ToolQuery(("adc-no-such-tool", "cache", "dir")).resolve()
    assert not out.available
    assert out.reason == "adc-no-such-tool is not on PATH"
    assert out.source == "tool:adc-no-such-tool cache dir"
    assert out.paths == ()


@pytest.mark.windows_only
def test_tool_query_believes_a_directory_the_tool_printed(
    tmp_path: Path, data_dir: Path
) -> None:
    r"""``cmd /c echo <dir>`` stands in for ``pnpm store path``.

    The point is the whole round trip: run without a shell, parse one line, check
    it exists, then cache it -- and the second call must come back ``cached``.
    """
    store = tmp_path / "store" / "v11"
    store.mkdir(parents=True)
    query = ToolQuery(("cmd", "/c", "echo", str(store)))

    first = query.resolve()
    assert first.available, first.reason
    assert first.paths == (os.path.normpath(str(store)),)
    assert first.source.startswith("tool:cmd /c echo ")
    assert not first.cached

    second = query.resolve()
    assert second.cached, "the 24 h cache exists because these calls cost seconds"
    assert second.paths == first.paths
    assert Path(tool_cache().path).is_file()
    assert str(data_dir) in tool_cache().path


@pytest.mark.windows_only
@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (("cmd", "/c", "echo", "not-a-path"), "did not print a path"),
        (("cmd", "/c", "exit", "3"), "exited 3"),
    ],
)
def test_tool_query_fails_safe_rather_than_guessing(
    argv: tuple[str, ...], fragment: str, data_dir: Path
) -> None:
    out = ToolQuery(argv).resolve()
    assert not out.available
    assert fragment in (out.reason or "")
    assert out.paths == ()


@pytest.mark.windows_only
def test_tool_query_refuses_a_path_the_tool_has_not_created(
    tmp_path: Path, data_dir: Path
) -> None:
    """npm prints its cache directory before creating it. Nothing to clean is not
    the same as "here is a directory", and it is certainly not a licence to guess."""
    out = ToolQuery(("cmd", "/c", "echo", str(tmp_path / "never-made"))).resolve()
    assert not out.available
    assert "named a missing path" in (out.reason or "")


# ---------------------------------------------------------------------------
# ToolCache -- a day-old answer is fine; a wrong one is not
# ---------------------------------------------------------------------------
def test_tool_cache_round_trips_through_the_data_dir(tmp_path: Path, data_dir: Path) -> None:
    cache = tool_cache()
    cache.put("probe argv", str(tmp_path))
    assert cache.get("probe argv", TTL_GENEROUS) == str(tmp_path)
    assert cache.path.startswith(str(data_dir)), "the singleton must honour ADC_DATA_DIR"


def test_tool_cache_forgets_an_entry_older_than_the_ttl(tmp_path: Path, data_dir: Path) -> None:
    """The TTL is why a store that moved from C: to E: is noticed within a day."""
    cache = tool_cache()
    cache.put("stale argv", str(tmp_path))
    with open(cache.path, encoding="utf-8") as handle:
        data = json.load(handle)
    data["stale argv"]["at"] = time.time() - 100.0
    with open(cache.path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    assert cache.get("stale argv", 10.0) is None
    assert cache.get("stale argv", 1_000.0) == str(tmp_path)


def test_tool_cache_treats_a_damaged_file_as_empty(data_dir: Path) -> None:
    """A half-written tools.json must not raise inside a scan; the cost of a miss
    is one slow subprocess call."""
    cache = tool_cache()
    cache.put("seed", "x")
    with open(cache.path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    assert cache.get("seed", TTL_GENEROUS) is None
    cache.put("seed", "y")          # must recover by overwriting
    assert cache.get("seed", TTL_GENEROUS) == "y"


def test_tool_cache_can_be_switched_off_entirely(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ADC_NO_TOOL_CACHE=1`` has to disable *both* halves. A read-only-off cache
    would keep serving whatever the last run wrote."""
    cache = tool_cache()
    cache.put("live", "value")
    monkeypatch.setenv(CACHE_OFF_ENV, "1")
    assert not cache.enabled
    assert cache.get("live", TTL_GENEROUS) is None
    cache.put("live", "other")
    monkeypatch.delenv(CACHE_OFF_ENV)
    assert cache.get("live", TTL_GENEROUS) == "value", "a disabled cache must not write"


def test_tool_cache_clear_removes_the_file_and_tolerates_its_absence(data_dir: Path) -> None:
    """``clear()`` is what a user pressing "re-detect tools" calls, so it must be
    safe to press twice."""
    cache = tool_cache()
    cache.put("gone", "value")
    assert Path(cache.path).is_file()
    cache.clear()
    assert not Path(cache.path).exists()
    cache.clear()
    assert cache.get("gone", TTL_GENEROUS) is None


# ---------------------------------------------------------------------------
# AnyOf -- one target, several legitimate homes
# ---------------------------------------------------------------------------
def test_anyof_unions_what_resolved_and_ignores_what_did_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "here").mkdir()
    monkeypatch.setenv("ADC_TEST_ROOT", str(tmp_path))
    out = AnyOf(
        (
            StaticPath(r"%ADC_DEFINITELY_UNSET%\WER"),
            StaticPath(r"%ADC_TEST_ROOT%\here"),
            StaticPath(r"%ADC_TEST_ROOT%\not-here"),
        )
    ).resolve()
    assert out.available
    assert out.paths == (os.path.normpath(str(tmp_path / "here")),)
    assert out.reason is None
    assert out.source.startswith("any(static:")


def test_anyof_counts_one_directory_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r"""Two specs can point at the same place -- ``%TEMP%`` and ``%LOCALAPPDATA%\Temp``
    are the same directory -- and a doubled path doubles the estimate."""
    (tmp_path / "Temp").mkdir()
    monkeypatch.setenv("ADC_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("ADC_TEST_TEMP", str(tmp_path / "Temp"))
    out = AnyOf((StaticPath(r"%ADC_TEST_ROOT%\Temp"), StaticPath("%ADC_TEST_TEMP%"))).resolve()
    assert len(out.paths) == 1


def test_anyof_reports_every_reason_when_nothing_resolved() -> None:
    """A UI row saying "unavailable" with no reason is what sent v1's users
    guessing; with two homes the user needs to know both were checked."""
    out = AnyOf(
        (StaticPath("%ADC_DEFINITELY_UNSET%"), StaticPath("%ADC_ALSO_UNSET%"))
    ).resolve()
    assert not out.available
    assert "; " in (out.reason or "")
    assert "%ADC_DEFINITELY_UNSET%" in (out.reason or "")
    assert "%ADC_ALSO_UNSET%" in (out.reason or "")


def test_anyof_with_no_parts_is_unavailable_not_empty_success() -> None:
    out = AnyOf(()).resolve()
    assert not out.available
    assert out.reason == "nothing resolved"


# ---------------------------------------------------------------------------
# RecycleBins -- every fixed volume, not just C:
# ---------------------------------------------------------------------------
@pytest.mark.windows_only
def test_recycle_bins_names_a_bin_on_each_fixed_volume() -> None:
    r"""v1 measured ``C:\$Recycle.Bin`` alone, so a file deleted from D: or E: was
    invisible. Every path here must be a real bin sitting directly on a volume
    root -- these are measured, never deleted as a directory."""
    out = RecycleBins().resolve()
    assert out.source == "recyclebin:fixed-volumes"
    if not out.available:            # a machine with none is legal, if unlikely
        assert "no Recycle Bin directory" in (out.reason or "")
        return
    roots = {volume.root for volume in fixed_volumes()}
    for path in out.paths:
        assert os.path.basename(path) == "$Recycle.Bin"
        assert os.path.isdir(path)
        assert os.path.dirname(path) + os.sep in {r + os.sep for r in roots}
    assert len(out.paths) == len(set(map(os.path.normcase, out.paths)))


# ---------------------------------------------------------------------------
# narrow_to_volumes -- what "scan C: only" does to a row that spans disks
# ---------------------------------------------------------------------------
def test_narrowing_to_no_volumes_at_all_keeps_everything() -> None:
    """An empty tick list means every volume, so the resolution is not even copied."""
    out = Resolution.found(["C:" + os.sep + "a", "D:" + os.sep + "b"], "test")

    assert narrow_to_volumes(out, ()) is out


def test_narrowing_keeps_the_ticked_volumes_and_drops_the_rest() -> None:
    """Half a row survives: this is the ``RecycleBins`` shape, in miniature."""
    out = Resolution.found(
        ["C:" + os.sep + "a", "D:" + os.sep + "b", "E:" + os.sep + "c"], "test"
    )

    kept = narrow_to_volumes(out, ("c", "E"))          # lower case must still match

    assert kept.paths == ("C:" + os.sep + "a", "E:" + os.sep + "c")
    assert kept.available is True
    assert kept.reason is None
    assert kept.source == out.source


def test_narrowing_everything_away_is_unavailable_not_an_empty_find() -> None:
    """0 B and "not on a disk you ticked" are different answers (BUG-12's class).

    ``available=False`` is what makes the scanner and the cleaner take their
    existing skip branches, so the row shows a reason instead of a total of zero.
    """
    out = Resolution.found(["D:" + os.sep + "b", "Q:" + os.sep + "c"], "test")

    gone = narrow_to_volumes(out, ("C",))

    assert gone.available is False
    assert gone.paths == ()
    assert gone.reason == "excluded by the volume filter (D:, Q:)"


def test_narrowing_leaves_a_systemic_resolution_alone() -> None:
    """Hibernation and DISM have no path to attribute, so no filter can exclude them.

    Unticking C: must not silently disable them: the Settings page offers a list of
    volumes, and inventing a rule it never showed would be worse than not filtering.
    """
    out = Resolution.systemic("systemic:hibernation")

    assert narrow_to_volumes(out, ("Z",)) is out


@pytest.mark.windows_only
def test_narrowing_a_recycle_bin_row_drops_the_other_volumes() -> None:
    r"""The reason the filter is per path: one row, one ``$Recycle.Bin`` per disk.

    ``bridge.scan_start`` used to argue that ``volume_ids`` could not narrow
    anything because every catalogue row sits at a location Windows fixes.
    ``RecycleBins`` is the counter-example, so it is the one pinned here.
    """
    out = RecycleBins().resolve()
    if not out.available or len(out.paths) < 2:
        pytest.skip("this machine has fewer than two Recycle Bin directories")
    letter = volume_letter(out.paths[0])

    kept = narrow_to_volumes(out, (letter,))

    assert kept.paths, "the ticked volume's own bin must survive"
    assert {volume_letter(p) for p in kept.paths} == {letter}
    assert len(kept.paths) < len(out.paths)


# ---------------------------------------------------------------------------
# Systemic vs ToolPresence -- the BUG-03 distinction, in one pair of tests
# ---------------------------------------------------------------------------
def test_systemic_needs_nothing_from_the_machine() -> None:
    """Hibernation and DISM exist on every Windows install, so asking a tool
    whether they are there would only add a way to be wrong."""
    out = Systemic("hibernation").resolve()
    assert out.available and not out.measurable
    assert out.paths == () and out.reason is None
    assert out.source == "system:hibernation"


def test_tool_presence_is_not_systemic_when_the_tool_is_absent() -> None:
    """Docker's target is a command, not a directory -- but a machine without
    Docker must not be offered it. ``Systemic`` would have said yes."""
    out = ToolPresence("adc-no-such-tool").resolve()
    assert not out.available
    assert out.reason == "adc-no-such-tool is not on PATH"


@pytest.mark.windows_only
def test_tool_presence_of_an_installed_tool_is_available_and_unmeasurable() -> None:
    out = ToolPresence("cmd").resolve()
    assert out.available
    assert not out.measurable
    assert out.paths == ()
    assert out.source == "presence:cmd"


def test_an_empty_path_takes_the_tool_rows_down_and_leaves_systemic_standing(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """The two are deliberately different answers to "is this machine equipped",
    and this is the one test that puts them side by side."""
    monkeypatch.setenv("PATH", "")
    assert not ToolPresence("pnpm").resolve().available
    assert not ToolQuery(("pnpm", "store", "path")).resolve().available
    assert Systemic("hibernation").resolve().available
