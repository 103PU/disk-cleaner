r"""Catalogue tests. The v1 defects here were all *data* defects.

v1's targets were code -- one function each, holding its own path, its own size
math and its own risk label -- so a wrong path (BUG-02: a guessed pnpm store), a
missing multiplication (BUG-03: ``Default`` only) and a wrong tier (BUG-08:
``C:\Windows\Logs`` ticked as safe) were all the same kind of edit and none of
them could be checked without deleting something.

``targets.py`` is a table, so this file checks the table. Two habits it keeps
throughout:

* **Nothing here deletes.** Every test resolves, inspects and measures. The only
  filesystem writes go into ``tmp_path``.
* **No frozen sizes.** The audit measured this machine on 2026-08-23 and those
  numbers drift by the hour -- a VS Code update rewrites ``CachedExtensionVSIXs``
  and Prefetch churns on every boot. Asserting them would produce a red suite
  that means "time passed". What is asserted instead is the invariant the numbers
  were evidence *for*: the resolver names paths that exist, and a measurement of
  one agrees with an independent walk of the same tree.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from adc.engine import targets as cat
from adc.engine.fsutil import walk_size
from adc.engine.jobs import CancelToken
from adc.engine.resolvers import ChromiumProfiles, Resolution
from adc.engine.strategies import Advise, RecycleDelete
from adc.engine.targets import Category, Risk

# The rows whose loss the user cannot undo by rebuilding anything. Not "every
# CAUTION row": Prefetch and the thumbnail cache are CAUTION because rebuilding
# them costs time, which is a different claim from holding irreplaceable state.
USER_STATE_IDS = frozenset({
    "vscode_workspacestorage",   # editor layout, undo history, extension state
    "claude_projects",           # conversation transcripts; nothing regenerates them
    "windows_logs",              # the only record of a failed servicing operation
    "crash_dumps",               # the evidence for a crash that already happened
})


# ---------------------------------------------------------------------------
# Shape of the table
# ---------------------------------------------------------------------------
def test_catalogue_has_the_spec_rows() -> None:
    """SPEC 5 lists 49 rows across seven sections."""
    assert len(cat.catalog()) == 49


def test_ids_are_unique() -> None:
    ids = [target.id for target in cat.catalog()]

    assert len(set(ids)) == len(ids)
    assert set(cat.ids()) == set(ids)


def test_a_duplicate_id_is_refused_rather_than_dropped() -> None:
    """A dict comprehension would keep the last row and lose the first silently."""
    row = cat.by_id("user_temp")

    with pytest.raises(ValueError, match="duplicate target id"):
        cat._index((row, row))


def test_every_category_is_used() -> None:
    used = {target.category for target in cat.catalog()}

    assert used == set(Category)


def test_every_row_is_bilingual() -> None:
    """A half-translated row is a P4 defect that would surface as a blank label."""
    for target in cat.catalog():
        assert target.name_vi and target.name_en, target.id
        assert target.desc_vi and target.desc_en, target.id


def test_by_id_and_find_disagree_only_about_unknown_ids() -> None:
    assert cat.by_id("user_temp").id == "user_temp"
    assert cat.find("user_temp") is cat.by_id("user_temp")
    assert cat.find("no-such-target") is None
    with pytest.raises(KeyError):
        cat.by_id("no-such-target")


# ---------------------------------------------------------------------------
# Resolution honesty -- the BUG-02 / BUG-03 rule
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def resolved(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Resolution]]:
    """Every row resolved once against this machine.

    Module-scoped because a few rows shell out (``npm config get cache`` is ~6 s
    cold) and because several tests want to look at the same answers. Resolving
    reads the filesystem and asks tools where their caches are; it deletes
    nothing. ``ADC_DATA_DIR`` is redirected for the same reason ``conftest`` does
    it elsewhere: ``ToolQuery`` caches its answers, and the cache belongs to the
    user's profile, not to a test run.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ADC_DATA_DIR", str(tmp_path_factory.mktemp("adc-data")))
        yield {target.id: target.resolve() for target in cat.catalog()}


def test_every_target_resolves_or_reports_unavailable(
    resolved: dict[str, Resolution],
) -> None:
    """P2 acceptance: no row raises, and an absent one says what was missing.

    This is the whole of BUG-03 in one assertion. v1's answer for a tool it could
    not find was a guessed path, which then appeared in the UI as a finding.
    """
    assert set(resolved) == set(cat.ids())
    for target_id, outcome in resolved.items():
        if outcome.available:
            continue
        assert outcome.reason, f"{target_id} is unavailable without saying why"
        assert outcome.paths == (), f"{target_id} is unavailable yet carries paths"


def test_available_rows_name_paths_that_exist(resolved: dict[str, Resolution]) -> None:
    """The other half: nothing invented. Every reported path is on disk now."""
    for target_id, outcome in resolved.items():
        if not outcome.available:
            continue
        if not outcome.measurable:
            assert outcome.paths == (), f"{target_id} is systemic yet carries paths"
            continue
        assert outcome.paths, f"{target_id} is available and measurable with no path"
        for path in outcome.paths:
            assert os.path.exists(path), f"{target_id} named a missing path: {path}"
            assert os.path.isabs(path), f"{target_id} named a relative path: {path}"


def test_a_missing_tool_does_not_become_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """With PATH emptied, every tool-backed row must go unavailable, not guess.

    Resolving with no PATH is the cheap simulation of the machine v1 got wrong:
    the tool is absent, so there is no authoritative answer, so the only honest
    output is ``unavailable``.
    """
    monkeypatch.setenv("PATH", "")
    tool_rows = [
        target for target in cat.catalog()
        if target.resolver.kind in ("tool", "presence")
    ]

    assert tool_rows, "the catalogue is meant to contain tool-backed rows"
    for target in tool_rows:
        outcome = target.resolve()
        assert not outcome.available, f"{target.id} answered available with no PATH"
        assert outcome.reason and "PATH" in outcome.reason, target.id


@pytest.mark.skipif(shutil.which("pnpm") is None, reason="pnpm is not installed here")
def test_toolquery_finds_the_real_pnpm_store() -> None:
    r"""BUG-02: v1 hardcoded ``%LOCALAPPDATA%\pnpm`` and measured an empty dir.

    The store on this machine is on another volume entirely. The assertion is not
    "it equals E:\.pnpm-store\v11" -- that is this machine's answer, and a config
    change would make it stale. It is the invariant v1 broke: the path came from
    pnpm, exists, and is not the guess.
    """
    outcome = cat.by_id("pnpm_store").resolve()
    guess = os.path.join(os.environ.get("LOCALAPPDATA", ""), "pnpm")

    assert outcome.available, outcome.reason
    assert outcome.source.startswith("tool:")
    assert len(outcome.paths) == 1
    assert os.path.isdir(outcome.paths[0])
    assert os.path.normcase(outcome.paths[0]) != os.path.normcase(guess)


# ---------------------------------------------------------------------------
# Risk tiers -- SPEC 4.3, and BUG-08
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("target_id", "risk"),
    [
        # BUG-08: v1 offered this with a green tick. It is the only record of a
        # failed servicing operation, so losing it costs the user a diagnosis.
        ("windows_logs", Risk.CAUTION),
        # Deleting a shadow copy destroys every restore point that used it.
        ("vss_manage", Risk.DANGEROUS),
        # Turning hibernation off is a configuration change, not a cache purge.
        ("hibernation", Risk.DANGEROUS),
        # A re-download of packages a build needs: bandwidth, not data loss.
        ("nuget_global", Risk.REBUILDABLE),
        # HTTP metadata cache. Rebuilt on the next restore, nothing depends on it.
        ("nuget_http", Risk.SAFE),
        ("user_temp", Risk.SAFE),
        ("chrome_caches", Risk.SAFE),
        # SPEC 5 called this SAFE; SPEC 4.3's own definition makes a ~4 GB model
        # re-download a bandwidth cost, which is REBUILDABLE by definition.
        ("chrome_ai_weights", Risk.REBUILDABLE),
    ],
)
def test_risk_tiers_follow_the_spec_definitions(target_id: str, risk: Risk) -> None:
    assert cat.by_id(target_id).risk is risk


def test_dangerous_rows_are_the_expected_five() -> None:
    """A row drifting into DANGEROUS must be a deliberate edit, not a surprise."""
    assert {target.id for target in cat.by_risk(Risk.DANGEROUS)} == {
        "component_store",
        "hibernation",
        "docker_vhdx_compact",
        "wsl_distro_vhdx",
        "vss_manage",
    }


def test_caution_and_dangerous_rows_explain_themselves() -> None:
    """The tier is not the warning. A CAUTION row owes the user a sentence.

    v1's failure was not only mislabelling ``C:\\Windows\\Logs`` -- it never said
    what the row held, so a user had nothing to judge with. Every row above
    REBUILDABLE must carry either an estimate note or a rebuild cost, in both
    languages, because that text is what the confirm dialog shows.
    """
    for target in cat.catalog():
        if target.risk not in (Risk.CAUTION, Risk.DANGEROUS):
            continue
        vi = target.est_note_vi or target.rebuild_cost_vi
        en = target.est_note_en or target.rebuild_cost_en
        assert vi, f"{target.id} is {target.risk.value} with no Vietnamese warning"
        assert en, f"{target.id} is {target.risk.value} with no English warning"


def test_rebuildable_rows_say_what_rebuilding_costs() -> None:
    """REBUILDABLE is a promise about *cost*, so the cost has to be stated."""
    for target in cat.by_risk(Risk.REBUILDABLE):
        assert target.rebuild_cost_vi, target.id
        assert target.rebuild_cost_en, target.id


def test_user_state_targets_are_reversible() -> None:
    """The rows holding irreplaceable state must go through the Recycle Bin.

    Deliberately not "every CAUTION row": ``thumbnail_cache`` is CAUTION because
    Explorer redraws thumbnails slowly, and recycling a cache of millions of tiny
    files would be worse than deleting it. The invariant is about what cannot be
    rebuilt, not about the label.
    """
    for target_id in USER_STATE_IDS:
        target = cat.by_id(target_id)
        assert target.reversible, f"{target_id} holds user state and deletes hard"
        assert isinstance(target.strategy, RecycleDelete), target_id


def test_admin_required_is_never_understated() -> None:
    """If the strategy needs elevation, the row must have said so.

    The implication runs one way only. ``admin_required`` is also True on rows
    whose *paths* live outside the user's profile -- ``%SystemRoot%\\Temp`` needs
    elevation with an ordinary ``HardDelete`` -- and the strategy class cannot
    know that. What must never happen is the reverse: a row that quietly needs
    admin, so the UI offers it un-elevated and every delete fails as denied.
    """
    for target in cat.catalog():
        if target.strategy.describe().get("needs_admin"):
            assert target.admin_required, f"{target.id} needs admin but does not say so"


def test_temp_targets_hold_back_the_last_day() -> None:
    """An installer's ``%TEMP%`` scratch file is in use for as long as it runs."""
    for target_id in ("user_temp", "system_temp"):
        assert cat.by_id(target_id).min_age_hours >= 24


def test_min_age_is_never_negative() -> None:
    for target in cat.catalog():
        assert target.min_age_hours >= 0, target.id


# ---------------------------------------------------------------------------
# Presets -- what a one-click run is allowed to touch
# ---------------------------------------------------------------------------
def test_safe_preset_is_only_safe_rows() -> None:
    """The claim behind the Safe button. If this fails the button is a lie."""
    rows = cat.preset("safe")

    assert rows
    assert {target.risk for target in rows} == {Risk.SAFE}


def test_deep_preset_excludes_dangerous() -> None:
    """Deep may cost time to undo; it may not cost data or reconfigure Windows."""
    risks = {target.risk for target in cat.preset("deep")}

    assert Risk.DANGEROUS not in risks
    assert risks == {Risk.SAFE, Risk.REBUILDABLE, Risk.CAUTION}


def test_no_preset_can_reach_a_dangerous_row() -> None:
    """Structural, not a per-preset rule: no batch run may include these."""
    for name in cat.PRESETS:
        for target in cat.preset(name):
            assert target.risk is not Risk.DANGEROUS, f"{name} includes {target.id}"


def test_standalone_rows_are_out_of_every_preset() -> None:
    """A row needing its own prechecked screen must never arrive inside a batch.

    ``vss_manage`` and the two VHDX rows each need their own confirmation, their
    own precheck and their own explanation of what cannot be undone. Sweeping
    them into a preset would put that decision behind a single click.
    """
    standalone = {target.id for target in cat.catalog() if target.standalone}

    assert standalone == {"docker_vhdx_compact", "wsl_distro_vhdx", "vss_manage"}
    for name in cat.PRESETS:
        assert not standalone & {target.id for target in cat.preset(name)}


def test_preset_counts_are_the_arithmetic_of_the_table() -> None:
    """49 rows, minus the five DANGEROUS, is what ``deep`` may offer."""
    dangerous = len(cat.by_risk(Risk.DANGEROUS))

    assert len(cat.preset("deep")) == len(cat.catalog()) - dangerous


def test_unknown_preset_names_the_valid_ones() -> None:
    with pytest.raises(KeyError, match="safe"):
        cat.preset("everything")


def test_in_preset_agrees_with_preset() -> None:
    for name in cat.PRESETS:
        members = set(cat.preset(name))
        for target in cat.catalog():
            assert cat.in_preset(target, name) is (target in members), target.id


def test_schedulable_is_safe_and_never_standalone() -> None:
    """SPEC 6.5: an unattended run gets the SAFE tier and nothing else."""
    rows = cat.schedulable()

    assert rows
    for target in rows:
        assert target.risk is Risk.SAFE, target.id
        assert not target.standalone, target.id


# ---------------------------------------------------------------------------
# What crosses the bridge (SEC-02)
# ---------------------------------------------------------------------------
def test_as_dict_carries_no_paths() -> None:
    """The UI names a target; Python owns the path.

    SEC-02 is the rule that a path never travels in either direction across the
    bridge, so a compromised renderer cannot ask for an arbitrary delete. Two
    things are checked: no key hands the UI a path or a resolver spec to edit,
    and no value has the shape of an absolute Windows path. Prose may still quote
    one -- ``pnpm_store`` explains BUG-02 by naming the directory v1 guessed --
    which is text in a warning, not an instruction the bridge would act on.
    """
    for row in cat.catalog_as_dicts():
        assert not {"paths", "path", "resolver_source", "spec"} & set(row)
        flat = repr(row)
        assert ":\\" not in flat and ":/" not in flat, f"{row['id']} leaked a path"


def test_as_dict_never_echoes_a_resolved_path(resolved: dict[str, Resolution]) -> None:
    """The stronger form: what a resolve found stays on the Python side."""
    rows = {row["id"]: repr(row) for row in cat.catalog_as_dicts()}

    for target_id, outcome in resolved.items():
        for path in outcome.paths:
            assert path not in rows[target_id], f"{target_id} serialised {path}"


def test_as_dict_is_json_shaped() -> None:
    """Enums must arrive as strings: ``json.dumps`` is what the bridge calls."""
    import json

    payload = json.dumps(cat.catalog_as_dicts())
    reloaded = json.loads(payload)

    assert len(reloaded) == len(cat.catalog())
    first = reloaded[0]
    assert isinstance(first["category"], str)
    assert isinstance(first["risk"], str)
    assert set(first["name"]) == {"vi", "en"}


def test_advise_rows_point_at_whoever_does_the_work() -> None:
    """An advisory row that offers no next step is a dead end in the UI."""
    for target in cat.catalog():
        if not isinstance(target.strategy, Advise):
            continue
        described = target.strategy.describe()
        assert described["kind"] == "advise"
        assert described["reversible"] is True
        if target.strategy.handled_by:
            assert cat.find(target.strategy.handled_by), target.id


# ---------------------------------------------------------------------------
# ChromiumProfiles -- every profile, not just Default
# ---------------------------------------------------------------------------
def _make_user_data(root: Path, profiles: tuple[str, ...]) -> Path:
    """A miniature ``User Data`` tree: cache subdirs under each profile."""
    user_data = root / "User Data"
    for name in profiles:
        for sub in ("Cache", "Code Cache", "GPUCache"):
            (user_data / name / sub).mkdir(parents=True)
        # Not a cache directory: it must not be swept in with the rest.
        (user_data / name / "Local Storage").mkdir(parents=True)
    (user_data / "System Profile").mkdir(exist_ok=True)  # not a user profile
    return user_data


def test_chromium_enumerates_every_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-03: v1 cleaned ``Default`` only -- one of nine profiles on this machine.

    Hermetic, so it holds on a machine with no Chrome at all. Three profiles times
    three cache subdirectories is nine paths; anything less means the resolver is
    back to reading one profile.
    """
    _make_user_data(tmp_path, ("Default", "Profile 1", "Profile 2"))
    monkeypatch.setenv("ADC_TEST_USER_DATA", str(tmp_path / "User Data"))
    resolver = ChromiumProfiles(
        "%ADC_TEST_USER_DATA%", subpaths=("Cache", "Code Cache", "GPUCache")
    )

    outcome = resolver.resolve()

    assert outcome.available
    assert len(outcome.paths) == 9
    assert [os.path.basename(p) for p in resolver.profile_dirs()] == [
        "Default", "Profile 1", "Profile 2",
    ]
    assert not any("Local Storage" in path for path in outcome.paths)
    assert not any("System Profile" in path for path in outcome.paths)


def test_chromium_reports_a_profileless_root_rather_than_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty ``User Data`` is unavailable-with-a-reason, never an empty find."""
    (tmp_path / "User Data").mkdir()
    monkeypatch.setenv("ADC_TEST_USER_DATA", str(tmp_path / "User Data"))

    outcome = ChromiumProfiles("%ADC_TEST_USER_DATA%").resolve()

    assert not outcome.available
    assert outcome.reason and "no browser profile" in outcome.reason


def test_chromium_row_matches_an_independent_listing() -> None:
    """The real-machine half: agree with a directory listing done by hand.

    Skipped where Chrome is not installed, so the suite stays green on a clean
    machine, and written as a recount rather than a fixed number because a new
    profile appears the moment the user signs another account in.
    """
    target = cat.by_id("chrome_caches")
    outcome = target.resolve()
    if not outcome.available:
        pytest.skip(f"no Chrome cache here: {outcome.reason}")

    resolver = target.resolver
    assert isinstance(resolver, ChromiumProfiles)
    root = resolver.user_data_dir()
    by_hand = sorted(
        name for name in os.listdir(root)
        if (name == "Default" or name.startswith("Profile "))
        and os.path.isdir(os.path.join(root, name))
    )

    assert sorted(os.path.basename(p) for p in resolver.profile_dirs()) == by_hand
    expected = sorted(
        name for name in by_hand
        if any(
            os.path.isdir(os.path.join(root, name, sub)) for sub in resolver.subpaths
        )
    )
    represented = sorted({
        os.path.relpath(path, root).split(os.sep)[0] for path in outcome.paths
    })

    assert represented == expected
    for path in outcome.paths:
        assert os.path.isdir(path)


# ---------------------------------------------------------------------------
# No shell, anywhere (SEC-01)
# ---------------------------------------------------------------------------
def test_no_target_uses_shell_true(repo_root: Path) -> None:
    """P2 acceptance: grep the engine, expect zero hits.

    Every command in the catalogue is a fixed argv tuple run with ``shell=False``,
    so there is no command line for a path or a target name to be interpolated
    into. A single ``shell=True`` would reopen that, hence a grep rather than a
    review note.
    """
    offenders = [
        f"{path.relative_to(repo_root)}:{number}"
        for path in sorted((repo_root / "src" / "adc").rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "shell=True" in line
    ]

    assert offenders == []


def test_every_command_is_an_argv_tuple() -> None:
    """The positive form: no row hands a strategy a string to be parsed."""
    for target in cat.catalog():
        argv = getattr(target.strategy, "argv", None)
        if argv is None:
            continue
        assert isinstance(argv, tuple), target.id
        assert argv and all(isinstance(part, str) and part for part in argv), target.id


# ---------------------------------------------------------------------------
# Measurement -- the invariant the audit's numbers were evidence for
# ---------------------------------------------------------------------------
def _independent_logical_size(root: str) -> tuple[int, int]:
    r"""``os.walk`` sum written the naive way, on purpose.

    Deliberately not sharing a line of code with ``fsutil``: an independent
    measurement is only independent if it can disagree. It does copy one rule --
    do not descend a reparse point -- because following the ``AppData\Local``
    junctions would double-count a tree rather than measure it.
    """
    total = files = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [
            name for name in dirs
            if not os.path.isjunction(os.path.join(base, name))
        ]
        for name in names:
            path = os.path.join(base, name)
            try:
                stat = os.stat(path, follow_symlinks=False)
            except OSError:
                continue  # vanished or denied: walk_size skips it too
            total += stat.st_size
            files += 1
    return total, files


# Small, quiet, and present on a developer machine. Ordered by how unlikely they
# are to be written to while the test runs: a log directory nobody has open beats
# a browser cache being appended to by a live process.
MEASURABLE_CANDIDATES = ("vscode_logs", "jetbrains_caches", "vscode_vsixs", "inetcache")


@pytest.mark.slow
def test_measured_size_agrees_with_an_independent_walk() -> None:
    r"""What ``01-AUDIT.md`` 6 was really evidence for.

    The plan's acceptance row asks for agreement with the audit within +-5 %. Those
    figures were measured on 2026-08-23 and are already wrong: an extension update
    rewrites ``CachedExtensionVSIXs`` and Prefetch churns on every boot. Asserting
    them would give a red suite that means "time passed", which trains everyone to
    ignore it.

    So the number is re-derived instead. ``walk_size`` and a naive ``os.walk`` are
    pointed at the same live directory and must land within 5 % of each other --
    the same tolerance, against a measurement taken now. A real defect (following
    a junction, counting a directory entry as a file, BUG-12's logical-for-
    allocated substitution) moves the two apart by far more than 5 %.
    """
    for target_id in MEASURABLE_CANDIDATES:
        outcome = cat.by_id(target_id).resolve()
        if outcome.available and outcome.paths:
            break
    else:
        pytest.skip("none of the small measurable targets is present here")

    root = outcome.paths[0]
    measured = walk_size(root, cancel=CancelToken(), size_on_disk=False)
    independent, files = _independent_logical_size(root)

    assert not measured.truncated, "no budget was set; a truncation is a defect"
    assert measured.size == measured.logical, "size_on_disk=False reports logical"
    assert not measured.measured_on_disk
    if independent == 0:
        assert measured.logical == 0
        return
    drift = abs(measured.logical - independent) / independent
    assert drift <= 0.05, (
        f"{target_id} at {root}: walk_size {measured.logical} B in {measured.files} "
        f"files vs os.walk {independent} B in {files} files ({drift:.1%} apart)"
    )
