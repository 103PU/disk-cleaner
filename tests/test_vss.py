r"""Shadow-copy tests. The subject is BUG-09: the 2 GB ceiling, and the way back.

v1's "Deep Preset" deleted every shadow copy and then pinned the store at 2 GB
(docs/01-AUDIT.md BUG-09), and had no way to undo either. docs/02-SPEC.md 5 asks
for three separate operations instead of one checkbox, and docs/03-PLAN.md:212
makes the acceptance row "**Đặt lại được** max shadow storage từ 2.00 GB về
10 %/UNBOUNDED".

Every command goes through an injected ``Runner``, so nothing here touches the
real Volume Shadow Copy service: the tests drive this machine's own captured
``vssadmin`` output through the parsers, and check the mutations by the argv they
would have run. ``resize`` and ``delete`` are asserted, never executed.

The two ``STORAGE_*``/``SHADOWS_*`` captures marked *verbatim* are real output
from this machine on 2026-09-03 and 2026-09-04, with the machine name replaced;
the others are hand-built and say so, because no machine here has an unbounded
store or a second association to capture.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from adc.engine import vss
from adc.engine.vss import (
    PERCENT_10,
    UNBOUNDED,
    ActionResult,
    ActionStore,
    ExpiredActionError,
    Limit,
    PendingAction,
    ShadowCopy,
    SpentActionError,
    Storage,
    UnknownActionError,
    VssState,
    VssUnavailableError,
    plan_delete,
    plan_resize,
)

GB = 1024**3
MB = 1024**2

# ---------------------------------------------------------------------------
# Captured output
# ---------------------------------------------------------------------------
# Verbatim, 2026-09-03: the store full to its own ceiling. One checkpoint had
# taken 1.93 GB of the 2.00 GB it was allowed, which is BUG-09 in one line.
STORAGE_FULL = r"""vssadmin 1.1 - Volume Shadow Copy Service administrative command-line tool
(C) Copyright 2001-2013 Microsoft Corp.

Shadow Copy Storage association
   For volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
   Shadow Copy Storage volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
   Used Shadow Copy Storage space: 1.93 GB (1%)
   Allocated Shadow Copy Storage space: 2.00 GB (1%)
   Maximum Shadow Copy Storage space: 2.00 GB (1%)
"""

# Verbatim, 2026-09-04, the next day: the copy above is gone and the store holds
# nothing. Nobody deleted it -- it was evicted to fit the ceiling, which is what
# the memory note predicted and what makes System Restore useless here.
STORAGE_EMPTY = r"""vssadmin 1.1 - Volume Shadow Copy Service administrative command-line tool
(C) Copyright 2001-2013 Microsoft Corp.

Shadow Copy Storage association
   For volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
   Shadow Copy Storage volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
   Used Shadow Copy Storage space: 0 bytes (0%)
   Allocated Shadow Copy Storage space: 0 bytes (0%)
   Maximum Shadow Copy Storage space: 2.00 GB (1%)
"""

# Hand-built from the shape above: no machine here has an unbounded store to
# capture, and it is the value the acceptance row exists to be able to set.
STORAGE_UNBOUNDED = r"""Shadow Copy Storage association
   For volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
   Shadow Copy Storage volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
   Used Shadow Copy Storage space: 12.4 GB (9%)
   Allocated Shadow Copy Storage space: 12.4 GB (9%)
   Maximum Shadow Copy Storage space: UNBOUNDED (100%)
"""

# Hand-built: two associations, and C:'s copies kept on D:. That arrangement is
# why ``/on=`` exists as a separate argument -- point it at the wrong volume and
# vssadmin makes a second association instead of editing the first.
STORAGE_TWO = r"""Shadow Copy Storage association
   For volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
   Shadow Copy Storage volume: (D:)\\?\Volume{7ac1d461-2f0e-4f5b-9d2a-1c4be8d0f911}\
   Used Shadow Copy Storage space: 4.00 GB (3%)
   Allocated Shadow Copy Storage space: 4.50 GB (4%)
   Maximum Shadow Copy Storage space: 20.0 GB (16%)

Shadow Copy Storage association
   For volume: (E:)\\?\Volume{9b0c7d18-4e6a-4a11-8c3d-55f2a7e14c02}\
   Shadow Copy Storage volume: (E:)\\?\Volume{9b0c7d18-4e6a-4a11-8c3d-55f2a7e14c02}\
   Used Shadow Copy Storage space: 512 MB (1%)
   Allocated Shadow Copy Storage space: 640 MB (1%)
   Maximum Shadow Copy Storage space: 8.00 GB (2%)
"""

# Verbatim, 2026-09-03, machine name replaced. The single restore point that the
# 2 GB ceiling then evicted.
SHADOWS_ONE = r"""vssadmin 1.1 - Volume Shadow Copy Service administrative command-line tool
(C) Copyright 2001-2013 Microsoft Corp.

Contents of shadow copy set ID: {da4b9974-8ca0-4602-ac7e-d1c5f23fd25d}
   Contained 1 shadow copies at creation time: 3/09/2026 1:40:44 AM
      Shadow Copy ID: {7c13d002-0627-4c14-9e7d-682f93ea9d32}
         Original Volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
         Shadow Copy Volume: \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1
         Originating Machine: WORKSTATION
         Service Machine: WORKSTATION
         Provider: 'Microsoft Software Shadow Copy provider 1.0'
         Type: ClientAccessibleWriters
"""

# Verbatim, 2026-09-04: what the tool says when there is nothing. It exits 1 for
# this, which is why the exit code cannot be what decides whether it failed.
SHADOWS_NONE = r"""vssadmin 1.1 - Volume Shadow Copy Service administrative command-line tool
(C) Copyright 2001-2013 Microsoft Corp.

No items found that satisfy the query.
"""

# Hand-built, and the point of the module: the same output with every label
# translated and the layout untouched. If the parsers ever go back to matching
# English words, this is the fixture that fails.
SHADOWS_VI = r"""vssadmin 1.1 - Cong cu dong lenh quan tri Volume Shadow Copy Service
(C) Ban quyen 2001-2013 Microsoft Corp.

Noi dung cua bo ban sao bong ID: {da4b9974-8ca0-4602-ac7e-d1c5f23fd25d}
   Chua 1 ban sao bong tai thoi diem tao: 3/09/2026 1:40:44 AM
      ID ban sao bong: {7c13d002-0627-4c14-9e7d-682f93ea9d32}
         O dia goc: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
         Nha cung cap: 'Microsoft Software Shadow Copy provider 1.0'
         Loai: ClientAccessibleWriters
"""

# Hand-built: two copies inside one set, which is what a multi-volume checkpoint
# looks like. Both must come back carrying the set id, because deleting is done
# per volume and the set is how the UI groups them.
SHADOWS_TWO = r"""Contents of shadow copy set ID: {11111111-2222-3333-4444-555555555555}
   Contained 2 shadow copies at creation time: 4/09/2026 3:15:00 AM
      Shadow Copy ID: {aaaaaaaa-0000-0000-0000-000000000001}
         Original Volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
         Provider: 'Microsoft Software Shadow Copy provider 1.0'
      Shadow Copy ID: {bbbbbbbb-0000-0000-0000-000000000002}
         Original Volume: (E:)\\?\Volume{9b0c7d18-4e6a-4a11-8c3d-55f2a7e14c02}\
         Provider: 'Microsoft Software Shadow Copy provider 1.0'
"""

EXE = r"C:\Windows\System32\vssadmin.exe"


class Recorder:
    """A runner that answers from a table and remembers every argv it was given.

    Keyed on a substring of the joined argv rather than the exact list, so a test
    says ``"list shadows"`` and stays readable when the argv grows a flag.
    """

    def __init__(self, answers: dict[str, tuple[int | None, str]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int | None, str]:
        self.calls.append(tuple(argv))
        joined = " ".join(argv)
        for needle, answer in self.answers.items():
            if needle in joined:
                return answer
        raise AssertionError(f"unexpected command: {joined}")

    @property
    def commands(self) -> list[str]:
        """Each recorded argv joined, minus the executable: what was asked for."""
        return [" ".join(call[1:]) for call in self.calls]


def refuse(argv: Sequence[str]) -> tuple[int | None, str]:
    """A runner that must never be reached. Used where nothing may run."""
    raise AssertionError(f"this must not run: {' '.join(argv)}")


def listing(storage: str, shadows: str) -> Recorder:
    return Recorder({"list shadowstorage": (0, storage), "list shadows": (0, shadows)})


def _as_admin(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Pin elevation. Every operation here is admin-gated, on any machine."""
    monkeypatch.setattr(vss.win, "is_user_an_admin", lambda: value)


def _with_vssadmin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the tool is on PATH, so the tests run off-Windows too."""
    monkeypatch.setattr(vss, "_vssadmin", lambda: EXE)


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary case: elevated, with vssadmin present."""
    _as_admin(monkeypatch, True)
    _with_vssadmin(monkeypatch)


# ---------------------------------------------------------------------------
# Limit -- the difference between "10" and "10%" is a system-wide ceiling
# ---------------------------------------------------------------------------
def test_the_two_spec_limits_render_vssadmins_own_words() -> None:
    """docs/02-SPEC.md 5 names both: 10 % and UNBOUNDED."""
    assert PERCENT_10.arg == "10%"
    assert UNBOUNDED.arg == "UNBOUNDED"
    assert Limit("bytes", 2 * GB).arg == "2147483648"


def test_a_bare_number_means_bytes_so_the_ui_must_send_the_percent_sign() -> None:
    """``/maxsize=10`` is ten bytes, not ten percent. Parsing must not soften that.

    Ten bytes is below vssadmin's floor, so the mistake raises here instead of
    reaching the service -- which is the whole reason the floor is in this class.
    """
    with pytest.raises(ValueError, match="320 MB"):
        Limit.parse(10)

    assert Limit.parse("10%") == Limit("percent", 10)


def test_parse_accepts_the_three_shapes_the_page_can_send() -> None:
    assert Limit.parse("10%") == PERCENT_10
    assert Limit.parse("  UNBOUNDED  ") == UNBOUNDED
    assert Limit.parse("unbounded") == UNBOUNDED
    assert Limit.parse(str(2 * GB)) == Limit("bytes", 2 * GB)
    assert Limit.parse("10 %") == PERCENT_10          # a stray space is tolerated
    assert Limit.parse(PERCENT_10) is PERCENT_10


@pytest.mark.parametrize("raw", ["", "   ", None, "abc", "-5%", "1.5%", "10GB", "%"])
def test_parse_refuses_to_guess_at_a_storage_ceiling(raw: object) -> None:
    with pytest.raises(ValueError, match="limit|percentage"):
        Limit.parse(raw)


@pytest.mark.parametrize("pct", [0, -1, 101, 1000])
def test_a_percentage_outside_one_to_a_hundred_is_not_a_percentage(pct: int) -> None:
    with pytest.raises(ValueError, match="1..100"):
        Limit("percent", pct)


def test_a_maximum_below_vssadmins_floor_is_refused_here_not_there() -> None:
    """The tool wants 320 MB at least; below it, it exits 1 with a bare code."""
    with pytest.raises(ValueError, match="320 MB"):
        Limit("bytes", 100 * MB)

    assert Limit("bytes", 320 * MB).value == 335544320


def test_an_unknown_kind_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="unknown limit kind"):
        Limit("gigabytes", 4)


def test_limit_as_dict_carries_the_argument_it_will_become() -> None:
    assert PERCENT_10.as_dict() == {"kind": "percent", "value": 10, "arg": "10%"}
    assert UNBOUNDED.as_dict() == {"kind": "unbounded", "value": 0, "arg": "UNBOUNDED"}


# ---------------------------------------------------------------------------
# target_bytes / would_shrink -- what the dialog has to know before it asks
# ---------------------------------------------------------------------------
def test_a_percentage_is_worked_out_against_the_volume() -> None:
    assert vss.target_bytes(PERCENT_10, 200 * GB) == 20 * GB
    assert vss.target_bytes(Limit("bytes", 4 * GB), 200 * GB) == 4 * GB


def test_unbounded_has_no_byte_count_and_says_so() -> None:
    """``None`` rather than 0: the UI prints "no ceiling", not "no space"."""
    assert vss.target_bytes(UNBOUNDED, 200 * GB) is None


def test_a_percentage_of_an_unknown_volume_is_unknown() -> None:
    """A caller that could not size the disk must not be handed a number to show."""
    assert vss.target_bytes(PERCENT_10, 0) is None
    assert vss.target_bytes(PERCENT_10, -1) is None


def test_ten_percent_of_this_disk_raises_the_ceiling_and_is_not_a_shrink() -> None:
    """The acceptance row's own case, in numbers: 2.00 GB -> 10 % of 134.49 GB.

    This is the way back from BUG-09, and flagging it as destructive would put a
    "copies may be evicted" warning on the one operation that gives space back.
    """
    current = Storage("C", "C", used=0, allocated=0, maximum=2 * GB, unbounded=False)
    total = int(134.49 * GB)

    assert vss.target_bytes(PERCENT_10, total) == int(total * 10 / 100)
    assert vss.would_shrink(current, PERCENT_10, total) is False


def test_lowering_the_ceiling_is_flagged_because_windows_evicts_to_fit() -> None:
    current = Storage("C", "C", used=0, allocated=0, maximum=20 * GB, unbounded=False)

    assert vss.would_shrink(current, Limit("bytes", 2 * GB)) is True
    assert vss.would_shrink(current, PERCENT_10, 100 * GB) is True     # 10 GB < 20 GB


def test_any_ceiling_at_all_shrinks_a_store_that_had_none() -> None:
    """This is exactly what v1 did to this machine: UNBOUNDED -> 2 GB, silently."""
    current = Storage("C", "C", used=12 * GB, allocated=12 * GB, maximum=None, unbounded=True)

    assert vss.would_shrink(current, PERCENT_10, 200 * GB) is True
    assert vss.would_shrink(current, Limit("bytes", 500 * GB)) is True


def test_going_unbounded_never_shrinks_anything() -> None:
    current = Storage("C", "C", used=0, allocated=0, maximum=2 * GB, unbounded=False)

    assert vss.would_shrink(current, UNBOUNDED, 200 * GB) is False


def test_an_absent_association_cannot_shrink() -> None:
    """No association means no copies to lose; the first resize creates it."""
    assert vss.would_shrink(None, PERCENT_10, 200 * GB) is False


# ---------------------------------------------------------------------------
# parse_storages -- read by layout and numeric shape, never by an English label
# ---------------------------------------------------------------------------
def test_this_machines_full_store_parses_to_the_figures_it_printed() -> None:
    rows, complaint = vss.parse_storages(STORAGE_FULL)

    assert complaint is None
    assert len(rows) == 1
    row = rows[0]
    assert (row.letter, row.diff_letter) == ("C", "C")
    assert (row.volume, row.diff_volume) == ("C:", "C:")
    assert row.used == int(1.93 * GB)
    assert row.allocated == 2 * GB
    assert row.maximum == 2 * GB
    assert row.unbounded is False
    assert Storage.approximate is True          # vssadmin rounds to two decimals


def test_zero_bytes_is_a_size_not_a_parse_failure() -> None:
    """The state this machine is in the day after: the store holds nothing.

    ``vssadmin`` stops abbreviating below a megabyte and prints "0 bytes". A
    pattern that only accepted the abbreviation read three figures as one, then
    dropped the whole association and called ordinary output unreadable.
    """
    rows, complaint = vss.parse_storages(STORAGE_EMPTY)

    assert complaint is None
    assert len(rows) == 1
    assert (rows[0].used, rows[0].allocated) == (0, 0)
    assert rows[0].maximum == 2 * GB            # the cap survived the eviction
    assert rows[0].unbounded is False


def test_a_guid_in_the_block_is_never_read_as_a_byte_count() -> None:
    """``{cbd6257a-71bb-4473-b800-498f0a8e380c}`` contains "b800".

    Collect sizes from the volume lines too and that substring parses as a size,
    shifting used/allocated/maximum one place left -- so the ceiling this screen
    exists to show would be reported as the amount in use.
    """
    rows, _ = vss.parse_storages(STORAGE_FULL)

    assert rows[0].used == int(1.93 * GB)       # not 2048, not 0xb800
    assert rows[0].maximum == 2 * GB


def test_an_unbounded_maximum_is_none_and_flagged() -> None:
    rows, complaint = vss.parse_storages(STORAGE_UNBOUNDED)

    assert complaint is None
    assert rows[0].maximum is None
    assert rows[0].unbounded is True
    assert rows[0].used == int(12.4 * GB)


def test_two_associations_and_a_store_kept_on_another_volume() -> None:
    """``/on=`` names the diff volume, and getting it wrong makes a second row."""
    rows, complaint = vss.parse_storages(STORAGE_TWO)

    assert complaint is None
    assert [(r.letter, r.diff_letter) for r in rows] == [("C", "D"), ("E", "E")]
    assert rows[0].diff_volume == "D:"
    assert rows[0].maximum == 20 * GB
    assert rows[1].used == 512 * MB


def test_a_block_it_cannot_read_is_reported_not_silently_dropped() -> None:
    """Two figures where three belong. Better a complaint than a plausible row."""
    truncated = "\n".join(STORAGE_FULL.splitlines()[:-1]) + "\n"

    rows, complaint = vss.parse_storages(truncated)

    assert rows == ()
    assert complaint == "1 storage block(s) could not be read"


def test_the_header_alone_is_not_an_association() -> None:
    header = "\n".join(STORAGE_FULL.splitlines()[:2]) + "\n"

    assert vss.parse_storages(header) == ((), None)


def test_nothing_at_all_parses_to_nothing_at_all() -> None:
    assert vss.parse_storages("") == ((), None)


# ---------------------------------------------------------------------------
# parse_copies -- the same, by indentation
# ---------------------------------------------------------------------------
def test_the_one_copy_this_machine_had_parses_whole() -> None:
    copies = vss.parse_copies(SHADOWS_ONE)

    assert len(copies) == 1
    copy = copies[0]
    assert copy.copy_id == "{7c13d002-0627-4c14-9e7d-682f93ea9d32}"
    assert copy.set_id == "{da4b9974-8ca0-4602-ac7e-d1c5f23fd25d}"
    assert copy.letter == "C"
    assert copy.created == "3/09/2026 1:40:44 AM"
    assert copy.provider == "Microsoft Software Shadow Copy provider 1.0"


def test_the_creation_time_is_kept_in_the_services_own_wording() -> None:
    """``3/09/2026`` is 3 September here. Reparsing it risks showing 9 March."""
    assert vss.parse_copies(SHADOWS_ONE)[0].created == "3/09/2026 1:40:44 AM"


def test_a_translated_listing_parses_because_the_layout_is_what_is_read() -> None:
    """The module's central claim. English words appear nowhere in this fixture."""
    copies = vss.parse_copies(SHADOWS_VI)

    assert len(copies) == 1
    assert copies[0].copy_id == "{7c13d002-0627-4c14-9e7d-682f93ea9d32}"
    assert copies[0].letter == "C"
    assert copies[0].created == "3/09/2026 1:40:44 AM"
    assert copies[0].provider == "Microsoft Software Shadow Copy provider 1.0"


def test_two_copies_in_one_set_both_keep_the_set_id() -> None:
    """Deleting is per volume; the set is only how the UI groups what it shows."""
    copies = vss.parse_copies(SHADOWS_TWO)

    assert len(copies) == 2
    assert [c.letter for c in copies] == ["C", "E"]
    assert {c.set_id for c in copies} == {"{11111111-2222-3333-4444-555555555555}"}
    assert copies[0].copy_id != copies[1].copy_id
    assert all(c.created == "4/09/2026 3:15:00 AM" for c in copies)


def test_no_items_found_is_no_copies() -> None:
    assert vss.parse_copies(SHADOWS_NONE) == ()
    assert vss.parse_copies("") == ()


def test_a_copy_carries_no_device_path_out_of_the_engine() -> None:
    r"""``\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1`` stays in the engine.

    Same rule as the explorer's rows (SEC-02): the page gets ids and labels, and
    a device path it could not use is a path it should never have been handed.
    """
    body = json.dumps(vss.parse_copies(SHADOWS_ONE)[0].as_dict())

    assert "GLOBALROOT" not in body
    assert "?" + chr(92) + "Volume" not in body
    assert "WORKSTATION" not in body            # nor the machine's own name


# ---------------------------------------------------------------------------
# state() -- read only, and every refusal says which one it is
# ---------------------------------------------------------------------------
def test_state_reads_both_lists_and_asks_for_nothing_else(admin: None) -> None:
    rec = listing(STORAGE_FULL, SHADOWS_ONE)

    st = vss.state(runner=rec)

    assert rec.commands == ["list shadowstorage", "list shadows"]
    assert (st.supported, st.is_admin, st.error) == (True, True, None)
    assert len(st.storages) == 1
    assert len(st.copies) == 1


def test_state_finds_a_volume_however_the_caller_spells_it(admin: None) -> None:
    st = vss.state(runner=listing(STORAGE_FULL, SHADOWS_ONE))

    found = st.storage_for("c")
    assert found is not None
    assert found.maximum == 2 * GB
    assert st.storage_for("c:") is found
    assert st.storage_for("C:" + chr(92)) is found
    assert st.storage_for("Z") is None
    assert len(st.copies_for("c:")) == 1
    assert st.copies_for("E") == ()


def test_a_machine_without_vssadmin_is_unsupported_not_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off-Windows there is no VSS at all, which is a sentence, not a red banner."""

    def missing() -> str:
        raise VssUnavailableError("shadow copies are a Windows feature")

    monkeypatch.setattr(vss, "_vssadmin", missing)

    st = vss.state(runner=refuse)

    assert st.supported is False
    assert st.is_admin is False
    assert st.error == "shadow copies are a Windows feature"
    assert st.storages == () and st.copies == ()


def test_state_without_elevation_explains_itself_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vssadmin refuses a standard user, so an empty screen would say nothing."""
    _with_vssadmin(monkeypatch)
    _as_admin(monkeypatch, False)

    st = vss.state(runner=refuse)

    assert (st.supported, st.is_admin) == (True, False)
    assert st.error is not None
    assert "Administrator" in st.error


def test_no_shadow_copies_at_all_is_not_an_error(admin: None) -> None:
    """This machine's live state on 2026-09-04, exit code and all.

    ``list shadows`` exits 1 for "there are none", the most ordinary state a
    machine can be in, so the presence of a copy id decides -- not the code.
    """
    rec = Recorder({"list shadowstorage": (0, STORAGE_EMPTY), "list shadows": (1, SHADOWS_NONE)})

    st = vss.state(runner=rec)

    assert st.error is None
    assert st.copies == ()
    assert len(st.storages) == 1
    assert st.storages[0].used == 0


def test_a_failed_copy_list_still_leaves_the_ceiling_on_screen(admin: None) -> None:
    """The ceiling is what this screen exists to change; losing it to a second,
    unrelated failure would take away the fix along with the diagnosis."""
    rec = Recorder({
        "list shadowstorage": (0, STORAGE_FULL),
        "list shadows": (2, "Error: E_UNEXPECTED. Query failed."),
    })

    st = vss.state(runner=rec)

    assert len(st.storages) == 1
    assert st.storages[0].maximum == 2 * GB
    assert st.copies == ()
    assert st.error is not None
    assert "Query failed." in st.error


def test_when_the_first_list_fails_the_error_is_vssadmins_own(admin: None) -> None:
    rec = Recorder({
        "list shadowstorage": (1, "Error: You don't have sufficient privileges."),
        "list shadows": (1, SHADOWS_NONE),
    })

    st = vss.state(runner=rec)

    assert st.storages == ()
    assert st.error is not None
    assert "sufficient privileges" in st.error


def test_an_unreadable_block_is_carried_out_as_a_complaint(admin: None) -> None:
    truncated = "\n".join(STORAGE_FULL.splitlines()[:-1]) + "\n"
    rec = Recorder({"list shadowstorage": (0, truncated), "list shadows": (1, SHADOWS_NONE)})

    st = vss.state(runner=rec)

    assert st.error is not None
    assert "could not be read" in st.error


# ---------------------------------------------------------------------------
# resize() -- the way back from BUG-09. Asserted by argv, never executed.
# ---------------------------------------------------------------------------
def test_the_dry_run_is_the_command_the_dialog_will_show(admin: None) -> None:
    """docs/03-PLAN.md:212, previewed: 2.00 GB back up to 10 %.

    A dry run exists so the confirmation dialog can quote the command it is asking
    about. v1 ran this silently; the whole difference is that the user reads it
    first, which means the string has to be the real one.
    """
    res = vss.resize("C", PERCENT_10, runner=refuse, dry_run=True)

    assert res.argv[1:] == (
        "resize", "shadowstorage", "/for=C:", "/on=C:", "/maxsize=10%",
    )
    assert (res.action, res.ok, res.dry_run) == ("resize", True, True)
    assert res.code is None and res.lines == ()
    assert res.printable.endswith("resize shadowstorage /for=C: /on=C: /maxsize=10%")


def test_the_other_spec_value_is_vssadmins_own_word(admin: None) -> None:
    res = vss.resize("C", UNBOUNDED, runner=refuse, dry_run=True)

    assert res.argv[-1] == "/maxsize=UNBOUNDED"


def test_the_page_may_send_the_limit_as_the_string_it_showed(admin: None) -> None:
    assert vss.resize("C", "unbounded", runner=refuse, dry_run=True).argv[-1] == (
        "/maxsize=UNBOUNDED"
    )
    assert vss.resize("C", "10%", runner=refuse, dry_run=True).argv[-1] == "/maxsize=10%"


def test_a_limit_it_cannot_read_stops_before_the_process(admin: None) -> None:
    with pytest.raises(ValueError, match="not a storage limit"):
        vss.resize("C", "as much as it needs", runner=refuse)


def test_a_store_kept_elsewhere_is_named_by_on(admin: None) -> None:
    """Point ``/on=`` at the wrong volume and vssadmin adds an association."""
    res = vss.resize("C", PERCENT_10, on="D", runner=refuse, dry_run=True)

    assert res.argv[1:5] == ("resize", "shadowstorage", "/for=C:", "/on=D:")


@pytest.mark.parametrize("spelling", ["c", "C", "c:", "C:", "C:" + chr(92), " c: "])
def test_every_spelling_of_a_drive_letter_reaches_vssadmin_as_one(
    admin: None, spelling: str
) -> None:
    res = vss.resize(spelling, PERCENT_10, runner=refuse, dry_run=True)

    assert res.argv[3] == "/for=C:"


@pytest.mark.parametrize(
    "volume", ["C:" + chr(92) + "Windows", "", None, "CC", "1", "?", chr(92) + chr(92) + "srv"]
)
def test_only_a_drive_letter_is_accepted_as_a_volume(admin: None, volume: object) -> None:
    """``/for=`` takes a volume spec, and a caller's spec is a command argument.

    Same rule as the bridge's: ids in, paths never. A path arriving here means a
    caller is confused about what it is asking for, and the answer is a refusal
    rather than a best guess at which volume it meant.
    """
    with pytest.raises(ValueError, match="not a drive letter"):
        vss.resize(volume, PERCENT_10, runner=refuse)


def test_resize_without_elevation_shows_the_command_but_does_not_run_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command is still worth showing: it is what the UAC prompt is for."""
    _with_vssadmin(monkeypatch)
    _as_admin(monkeypatch, False)

    res = vss.resize("C", PERCENT_10, runner=refuse)

    assert res.ok is False
    assert res.dry_run is False
    assert res.argv[-1] == "/maxsize=10%"
    assert res.reason is not None
    assert "Administrator" in res.reason


def test_resize_without_vssadmin_refuses_with_a_reason_and_no_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing() -> str:
        raise VssUnavailableError("vssadmin is not on PATH")

    monkeypatch.setattr(vss, "_vssadmin", missing)

    res = vss.resize("C", PERCENT_10, runner=refuse)

    assert (res.ok, res.argv, res.reason) == (False, (), "vssadmin is not on PATH")


def test_a_successful_resize_carries_the_code_and_the_last_words(admin: None) -> None:
    rec = Recorder({"resize": (0, "\nSuccessfully resized the shadow copy storage "
                                  "association\n")})

    res = vss.resize("C", PERCENT_10, runner=rec)

    assert (res.ok, res.code, res.dry_run) == (True, 0, False)
    assert res.lines == ("Successfully resized the shadow copy storage association",)
    assert res.reason is None
    assert rec.commands == ["resize shadowstorage /for=C: /on=C: /maxsize=10%"]


def test_a_failed_resize_reports_vssadmins_verdict_not_a_number(admin: None) -> None:
    """The last lines are where the tool puts the reason; the code is just 1."""
    rec = Recorder({"resize": (1, "vssadmin 1.1\n\nError: The specified maximum size "
                                  "is less than the minimum of 320MB.\n")})

    res = vss.resize("C", Limit("bytes", 320 * MB), runner=rec)

    assert res.ok is False
    assert res.code == 1
    assert res.reason is not None
    assert "minimum of 320MB" in res.reason


def test_a_resize_that_never_finished_is_never_reported_as_done(admin: None) -> None:
    """``None`` for the code means killed or timed out: the ceiling is unknown.

    Reporting that as success would tell the user their restore points are safe at
    a limit that was never applied.
    """
    rec = Recorder({"resize": (None, "timed out after 120s")})

    res = vss.resize("C", PERCENT_10, runner=rec)

    assert res.ok is False
    assert res.code is None
    assert res.reason == "timed out after 120s"


# ---------------------------------------------------------------------------
# delete() -- irreversible, and the default has to admit that
# ---------------------------------------------------------------------------
def test_delete_defaults_to_the_oldest_copy_and_not_to_all_of_them(admin: None) -> None:
    """v1's only move was ``delete shadows /all /quiet`` (docs/01-AUDIT.md BUG-09).

    A screen whose default destroys every restore point on the machine has learned
    nothing from that, so ``/all`` has to be asked for by name.
    """
    res = vss.delete("C", runner=refuse, dry_run=True)

    assert res.argv[1:] == ("delete", "shadows", "/for=C:", "/oldest", "/quiet")
    assert (res.action, res.ok, res.dry_run) == ("delete", True, True)


def test_deleting_everything_is_available_but_must_be_named(admin: None) -> None:
    res = vss.delete("C", scope="all", runner=refuse, dry_run=True)

    assert res.argv[4] == "/all"


def test_quiet_is_passed_because_no_console_can_answer_the_prompt(admin: None) -> None:
    """The confirmation happens in the UI, where the user can read what will go."""
    res = vss.delete("C", runner=refuse, dry_run=True)

    assert "/quiet" in res.argv


@pytest.mark.parametrize("scope", ["", "everything", "ALL", "newest", "oldest2", None])
def test_delete_refuses_a_scope_it_does_not_know(admin: None, scope: object) -> None:
    """An unrecognised scope must not fall through to whatever vssadmin defaults to."""
    with pytest.raises(ValueError, match="unknown delete scope"):
        vss.delete("C", scope=scope, runner=refuse)      # type: ignore[arg-type]


def test_delete_takes_a_drive_letter_and_nothing_else(admin: None) -> None:
    with pytest.raises(ValueError, match="not a drive letter"):
        vss.delete("C:" + chr(92) + "Users", runner=refuse)


def test_delete_without_elevation_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_vssadmin(monkeypatch)
    _as_admin(monkeypatch, False)

    res = vss.delete("C", scope="all", runner=refuse)

    assert res.ok is False
    assert res.argv[4] == "/all"
    assert res.reason is not None
    assert "Administrator" in res.reason


def test_a_successful_delete_says_how_many_went(admin: None) -> None:
    rec = Recorder({"delete shadows": (0, "Successfully deleted 1 shadow copies.\n")})

    res = vss.delete("C", scope="oldest", runner=rec)

    assert (res.ok, res.code) == (True, 0)
    assert res.lines == ("Successfully deleted 1 shadow copies.",)
    assert rec.commands == ["delete shadows /for=C: /oldest /quiet"]


def test_deleting_when_there_is_nothing_to_delete_is_a_failure_with_a_reason(
    admin: None,
) -> None:
    """vssadmin exits non-zero for "none found" here, and the screen should say so
    rather than claim it deleted something."""
    rec = Recorder({"delete shadows": (1, "Error: No items found that satisfy the query.")})

    res = vss.delete("C", runner=rec)

    assert res.ok is False
    assert res.reason is not None
    assert "No items found" in res.reason


def test_a_delete_that_never_finished_is_never_reported_as_done(admin: None) -> None:
    rec = Recorder({"delete shadows": (None, "timed out after 600s")})

    assert vss.delete("C", runner=rec).ok is False


# ---------------------------------------------------------------------------
# The shapes the bridge will serialise
# ---------------------------------------------------------------------------
def test_the_whole_state_survives_json_with_the_keys_the_page_reads(admin: None) -> None:
    st = vss.state(runner=listing(STORAGE_FULL, SHADOWS_ONE))

    body = json.loads(json.dumps(st.as_dict()))

    assert set(body) == {"supported", "is_admin", "storages", "copies", "error"}
    assert set(body["storages"][0]) == {
        "letter", "diff_letter", "volume", "diff_volume",
        "used", "allocated", "maximum", "unbounded", "approximate",
    }
    assert set(body["copies"][0]) == {"copy_id", "set_id", "letter", "created", "provider"}
    assert body["storages"][0]["approximate"] is True


def test_no_device_path_reaches_the_page_from_either_list(admin: None) -> None:
    r"""Both captures are full of ``\\?\Volume{...}``; none of it is in the answer."""
    body = json.dumps(vss.state(runner=listing(STORAGE_FULL, SHADOWS_ONE)).as_dict())

    assert "Volume{" not in body
    assert "GLOBALROOT" not in body
    assert chr(92) not in body                  # no backslash at all, escaped or not


def test_an_unbounded_maximum_serialises_as_null_not_zero(admin: None) -> None:
    """0 and "no ceiling" are opposite facts, and JSON has a word for one of them."""
    rec = Recorder({"list shadowstorage": (0, STORAGE_UNBOUNDED), "list shadows": (1, "")})

    body = json.loads(json.dumps(vss.state(runner=rec).as_dict()))

    assert body["storages"][0]["maximum"] is None
    assert body["storages"][0]["unbounded"] is True


def test_an_action_result_carries_the_command_as_text_for_the_report(admin: None) -> None:
    """The report has to be able to print what ran. These are the two commands v1
    ran silently, and the log is where a user finds out which one took their
    restore points."""
    res = vss.resize("C", PERCENT_10, runner=refuse, dry_run=True)

    body = json.loads(json.dumps(res.as_dict()))

    assert set(body) == {
        "action", "ok", "dry_run", "argv", "command", "code", "lines", "reason",
    }
    assert body["command"].endswith("/maxsize=10%")
    assert body["argv"][1:3] == ["resize", "shadowstorage"]


def test_the_state_types_are_frozen_so_a_view_cannot_edit_the_reading() -> None:
    with pytest.raises(AttributeError):
        Storage("C", "C", 0, 0, 0, False).letter = "D"       # type: ignore[misc]
    with pytest.raises(AttributeError):
        VssState(True, True).supported = False               # type: ignore[misc]
    with pytest.raises(AttributeError):
        ActionResult("resize", ok=True).ok = False           # type: ignore[misc]
    with pytest.raises(AttributeError):
        ShadowCopy("a", "b", "C", "d", "e").letter = "E"     # type: ignore[misc]


# ---------------------------------------------------------------------------
# Approval -- the receipt between "show me" and "do it"
# ---------------------------------------------------------------------------
# The 2 GB store this machine is stuck with, and the volume it sits on: the
# acceptance row's own numbers (docs/03-PLAN.md:212), reused here so the receipt
# is tested against the situation it exists to get out of.
CAPPED = Storage("C", "C", used=0, allocated=0, maximum=2 * GB, unbounded=False)
C_TOTAL = int(134.49 * GB)


def _aged(pending: PendingAction, seconds: float) -> PendingAction:
    """The same receipt, minted *seconds* ago. Cheaper than waiting five minutes."""
    pending.created_at -= seconds
    return pending


def test_a_preview_is_a_command_and_not_a_mutation(admin: None) -> None:
    """The whole point of the two-step: the first step runs nothing at all."""
    pending = plan_resize("C", PERCENT_10, storage=CAPPED, volume_total=C_TOTAL)

    assert pending.ok
    assert pending.preview.dry_run
    assert pending.argv[1:] == (
        "resize", "shadowstorage", "/for=C:", "/on=C:", "/maxsize=10%",
    )
    assert len(pending.token) == 32 and pending.token.isalnum()


def test_raising_the_ceiling_needs_no_typed_phrase(admin: None) -> None:
    """The way back from BUG-09 must not feel like the thing it undoes.

    10 % of 134.49 GB is 13.4 GB, well above the 2 GB cap, so nothing can be
    evicted and SPEC 4.3's typed confirmation is not asked for.
    """
    pending = plan_resize("C", PERCENT_10, storage=CAPPED, volume_total=C_TOTAL)

    assert pending.destructive is False
    assert pending.phrase == ""
    assert pending.copies_at_risk == 0
    assert pending.matches("") and pending.matches(None)


def test_going_unbounded_is_never_treated_as_a_loss(admin: None) -> None:
    pending = plan_resize("C", UNBOUNDED, storage=CAPPED, volume_total=C_TOTAL)

    assert pending.argv[-1] == "/maxsize=UNBOUNDED"
    assert pending.destructive is False


def test_lowering_the_ceiling_demands_the_volume_typed(admin: None) -> None:
    """Below the current maximum, Windows evicts to fit -- so the user types it.

    ``copies_at_risk`` is the worst case on purpose: vssadmin will not say how
    many it intends to drop, and a dialog that guesses low is the v1 mistake.
    """
    pending = plan_resize(
        "C", Limit("bytes", 400 * MB), storage=CAPPED, volume_total=C_TOTAL, copies=3,
    )

    assert pending.destructive is True
    assert pending.phrase == "C:"
    assert pending.copies_at_risk == 3
    assert not pending.matches("")


def test_a_ceiling_that_cannot_be_measured_is_treated_as_a_loss(admin: None) -> None:
    """Unknown must not read as safe. Ten percent of an unmeasured volume is a size
    nobody can compare with the 2 GB already there, and "no confirmation needed"
    is exactly how BUG-09 got past a user."""
    pending = plan_resize("C", PERCENT_10, storage=CAPPED)      # no volume_total

    assert pending.destructive is True
    assert pending.phrase == "C:"


def test_an_unmeasured_volume_with_no_store_is_still_not_a_loss(admin: None) -> None:
    """Nothing to lose: with no association there are no copies to evict."""
    assert plan_resize("C", PERCENT_10).destructive is False


def test_going_unbounded_is_never_a_loss_even_unmeasured(admin: None) -> None:
    assert plan_resize("C", UNBOUNDED, storage=CAPPED).destructive is False


def test_the_reading_says_where_the_copies_live_and_the_plan_believes_it(
    admin: None,
) -> None:
    """``/on=`` has to name the existing association or vssadmin makes a second.

    This is the two-association case captured in ``STORAGE_TWO``: C:'s store is
    kept on D:, so a resize aimed at C: with ``/on=C:`` would silently create a
    new association and leave the real ceiling where it was.
    """
    elsewhere = Storage("C", "D", used=0, allocated=0, maximum=2 * GB, unbounded=False)

    pending = plan_resize("C", PERCENT_10, storage=elsewhere, volume_total=C_TOTAL)

    assert pending.argv[3:5] == ("/for=C:", "/on=D:")
    assert pending.as_dict()["on_volume"] == "D:"


def test_an_explicit_on_overrides_the_reading(admin: None) -> None:
    pending = plan_resize("c", PERCENT_10, on="e:", storage=CAPPED, volume_total=C_TOTAL)

    assert pending.argv[3:5] == ("/for=C:", "/on=E:")


def test_a_delete_always_demands_the_phrase(admin: None) -> None:
    """Every delete loses a restore point. There is no gentle scope."""
    pending = plan_delete("C", copies=3)

    assert pending.argv[1:] == ("delete", "shadows", "/for=C:", "/oldest", "/quiet")
    assert pending.destructive is True
    assert pending.phrase == "C:"
    assert pending.copies_at_risk == 1          # /oldest takes one


def test_all_puts_every_copy_on_the_volume_at_risk(admin: None) -> None:
    """v1's only move, with the count in front of the user this time."""
    pending = plan_delete("C", scope="all", copies=3)

    assert pending.argv[4] == "/all"
    assert pending.copies_at_risk == 3


def test_nothing_to_delete_is_reported_as_nothing_not_as_one(admin: None) -> None:
    pending = plan_delete("C", scope="oldest", copies=0)

    assert pending.copies_at_risk == 0


def test_an_unknown_scope_never_becomes_a_receipt(admin: None) -> None:
    with pytest.raises(ValueError, match="scope"):
        plan_delete("C", scope="everything")


def test_a_volume_that_is_not_a_letter_never_becomes_a_receipt(admin: None) -> None:
    for bad in (r"C:\Windows", "", None, "CC", r"\\srv\share"):
        with pytest.raises(ValueError, match="drive letter"):
            plan_delete(bad)
        with pytest.raises(ValueError, match="drive letter"):
            plan_resize(bad, PERCENT_10)


@pytest.mark.parametrize("typed", ["C:", "c:", " C: ", "c", "C", r"C:\ "])
def test_the_phrase_is_forgiving_about_everything_except_the_volume(
    admin: None, typed: str,
) -> None:
    """Case and space are not the point; having read which volume it is, is."""
    assert plan_delete("C").matches(typed)


@pytest.mark.parametrize("typed", ["D:", "d", "", "   ", None, "yes", "delete", "CC", ":"])
def test_the_wrong_phrase_is_the_wrong_phrase(admin: None, typed: object) -> None:
    assert not plan_delete("C").matches(typed)


def test_run_executes_the_previewed_argv_and_nothing_else(admin: None) -> None:
    """The receipt is the command. Nothing is re-derived at redemption time."""
    rec = Recorder({"resize shadowstorage": (0, "Successfully resized the shadow copy storage")})
    pending = plan_resize("C", PERCENT_10, storage=CAPPED, volume_total=C_TOTAL)

    res = pending.run(runner=rec)

    assert rec.calls == [pending.argv]
    assert res.ok and res.code == 0
    assert res.dry_run is False
    assert res.lines[-1].startswith("Successfully resized")


def test_editing_the_receipt_cannot_change_the_command_that_runs(admin: None) -> None:
    """A later argument must not reach vssadmin under an earlier approval.

    ``limit`` and ``letter`` are what the dialog *described*; ``argv`` is what was
    approved. If run() rebuilt the command from the fields, this would delete
    every copy on D: under a receipt the user read as "raise C:'s ceiling".
    """
    rec = Recorder({"resize shadowstorage": (0, "ok")})
    pending = plan_resize("C", PERCENT_10, storage=CAPPED, volume_total=C_TOTAL)
    pending.letter = "D"
    pending.limit = Limit("bytes", 400 * MB)
    pending.action = "resize"

    pending.run(runner=rec)

    assert rec.commands == ["resize shadowstorage /for=C: /on=C: /maxsize=10%"]


def test_a_receipt_for_a_refused_preview_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unelevated: the command is shown, the receipt is dead, and refuse() proves it."""
    _as_admin(monkeypatch, False)
    _with_vssadmin(monkeypatch)

    pending = plan_resize("C", PERCENT_10, storage=CAPPED, volume_total=C_TOTAL)

    assert not pending.ok
    assert pending.argv[-1] == "/maxsize=10%"        # still shown to the user
    res = pending.run(runner=refuse)
    assert res is pending.preview
    assert res.reason is not None and "Administrator" in res.reason


def test_a_receipt_without_vssadmin_holds_no_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tool, no argv: there is nothing to approve, and nothing to show."""
    _as_admin(monkeypatch, True)
    monkeypatch.setattr(vss.win, "IS_WINDOWS", True)
    monkeypatch.setattr(vss.shutil, "which", lambda _name: None)

    pending = plan_delete("C")

    assert not pending.ok
    assert pending.argv == ()
    assert pending.run(runner=refuse) is pending.preview


def test_a_refused_preview_cannot_be_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one thing the token exists to prevent: being told no and holding one."""
    _as_admin(monkeypatch, False)
    _with_vssadmin(monkeypatch)
    store = ActionStore()

    pending = plan_resize("C", PERCENT_10, store=store)

    with pytest.raises(ValueError, match="refused"):
        store.put(pending)
    assert store.peek(pending.token) is None


def test_a_plan_can_be_handed_straight_to_the_store(admin: None) -> None:
    store = ActionStore()

    pending = plan_delete("C", store=store)

    assert store.peek(pending.token) is pending
    assert store.spend(pending.token) is pending


def test_a_token_is_good_once(admin: None) -> None:
    """A double-clicked confirm button must not delete twice."""
    store = ActionStore()
    pending = plan_delete("C", scope="all", store=store)

    store.spend(pending.token)

    with pytest.raises(SpentActionError, match="already run"):
        store.spend(pending.token)


def test_an_unknown_token_is_an_answer_not_a_crash(admin: None) -> None:
    store = ActionStore()

    assert store.peek("nope") is None
    assert store.peek(None) is None
    for bad in ("nope", "", None, 7):
        with pytest.raises(UnknownActionError):
            store.spend(bad)


def test_a_stale_receipt_is_refused_because_the_store_moves(admin: None) -> None:
    """Windows evicts copies on its own: figures from five minutes ago are gone.

    This machine is the proof -- one day the store held a copy, the next it held
    nothing, with nobody touching it. A ceiling approved against the first reading
    must not be applied against the second.
    """
    store = ActionStore()
    pending = plan_delete("C", store=store)
    _aged(pending, vss.ACTION_TTL_S + 1)

    with pytest.raises(ExpiredActionError, match="preview again"):
        store.spend(pending.token)
    assert store.peek(pending.token) is None        # and it is gone, not retryable


def test_a_receipt_inside_its_window_still_works(admin: None) -> None:
    store = ActionStore()
    pending = plan_delete("C", store=store)

    assert store.spend(_aged(pending, vss.ACTION_TTL_S - 30).token) is pending


def test_the_store_keeps_only_the_last_few_receipts(admin: None) -> None:
    """A UI bug cannot mint tokens without limit; the oldest goes first."""
    store = ActionStore(capacity=3)
    minted = []
    for i in range(5):
        pending = plan_delete("C", store=store)
        _aged(pending, 100 - i)                     # oldest first, deterministically
        minted.append(pending)

    assert [p for p in minted if store.peek(p.token)] == minted[-3:]


def test_clear_drops_every_receipt(admin: None) -> None:
    store = ActionStore()
    pending = plan_delete("C", store=store)

    store.clear()

    assert store.peek(pending.token) is None


def test_the_module_store_is_one_store() -> None:
    """Mirrors ``cleaner.plan_store()``: the bridge and the page share one."""
    assert vss.action_store() is vss.action_store()


def test_the_receipt_serialises_into_everything_the_dialog_has_to_say(
    admin: None,
) -> None:
    """The dialog is built from this dict alone -- so it has to hold all of it.

    SPEC 4.3 wants the user to read what will happen and type a phrase; that means
    the command, the volume, the count at risk, the phrase itself, and a deadline
    the page can count down.
    """
    pending = plan_resize(
        "C", Limit("bytes", 400 * MB), storage=CAPPED, volume_total=C_TOTAL, copies=2,
    )

    body = json.loads(json.dumps(pending.as_dict()))

    assert set(body) == {
        "token", "created_at", "expires_at", "action", "letter", "volume",
        "on_volume", "limit", "scope", "destructive", "phrase", "copies_at_risk",
        "preview",
    }
    assert body["expires_at"] - body["created_at"] == pytest.approx(vss.ACTION_TTL_S)
    assert body["volume"] == "C:" and body["phrase"] == "C:"
    assert body["limit"] == {"kind": "bytes", "value": 400 * MB, "arg": str(400 * MB)}
    assert body["preview"]["command"].endswith(f"/maxsize={400 * MB}")
    assert body["copies_at_risk"] == 2


def test_a_delete_receipt_carries_a_scope_and_no_limit(admin: None) -> None:
    """The two actions serialise into one shape, with the other's field empty."""
    body = plan_delete("C", scope="all").as_dict()

    assert body["scope"] == "all"
    assert body["limit"] is None
    assert body["action"] == "delete"
