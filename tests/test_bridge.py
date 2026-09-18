"""Bridge tests. Four invariants, and the rest is coercion.

The bridge is the whole attack surface of v2 -- pywebview hands the instance to
the page as ``window.pywebview.api``, so every public method here is callable by
anything running in the renderer. These tests are written from that side of the
boundary: the argument is whatever JavaScript felt like sending.

* **The envelope never breaks.** Every method answers ``{"ok": bool, ...}`` and
  never raises. A raise would not propagate anywhere useful -- pywebview catches
  it and assigns the formatted traceback into the page (webview/util.py:244-248),
  which is a disclosure, not an error report.
* **Ids in, never paths.** ``reveal`` is the method v1 got wrong (SEC-02). A path
  arriving from the page must be refused *because it is not a catalogue id*, not
  because of a blocklist -- so the test hands it a real Windows directory.
* **The token is the only door to a delete.** ``clean_execute`` takes a token and
  nothing else, and the three failure modes stay apart because the UI says
  something different for each.
* **No pywebview.** Nothing in this file imports it, and nothing it imports does
  either; that is what makes these tests runnable in a plain process. The
  structural half of that guarantee lives in ``tests/test_layering.py``.

No test here touches a real cache directory. The two that reach the catalogue
(``catalog``, and ``reveal``'s refusals) only read metadata, and the ones that
would resolve a path monkeypatch the resolution.
"""

from __future__ import annotations

import inspect
import os
import re
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from adc.engine import report as report_mod
from adc.engine import settings as settings_mod
from adc.engine import sweeper as sweeper_mod
from adc.engine import targets
from adc.engine import volumes as volumes_mod
from adc.engine import vss as vss_mod
from adc.engine.cleaner import Plan, PlanItem, PlanStore
from adc.engine.jobs import Job, JobKind, JobRunner, JobState, Level
from adc.engine.resolvers import Resolution
from adc.engine.vss import ActionStore
from adc.shell import bridge as bridge_mod
from adc.shell.bridge import (
    MAX_EVENTS,
    MAX_SELECTION,
    Bridge,
    BridgeError,
    guarded,
)

# A real id, so a test that means "unknown id" cannot accidentally pass because
# the id it chose was also absent from the catalogue for some other reason.
KNOWN = "uv_cache"


def _no_vss(argv: Sequence[str]) -> tuple[int, str]:
    """A vssadmin that has nothing to report, for the sweeps.

    The bridge's shadow-copy methods are on the same surface as everything else,
    so the envelope sweep calls them -- and a suite that shelled out to a system
    service to prove a dict has two keys would be one hang away from a mystery.
    The tests that are actually about VSS build their own runner below.
    """
    return 0, "No items found that satisfy the query."


@pytest.fixture
def api(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bridge:
    """A bridge on a private runner and stores, writing to a tmp profile.

    ``data_dir`` is not optional: ``settings_set`` and the report reader both go
    through ``%LOCALAPPDATA%`` otherwise, and a test that persisted settings
    would be editing the user's real configuration. The action and sweep stores
    are private for the same reason -- ``vss.action_store()`` and
    ``sweeper.sweep_plan_store()`` are process-wide, and a receipt minted in one
    test must not be redeemable in the next.

    ``ADC_PROJECT_ROOT`` is pinned for a sharper reason. ``sweeper.default_root()``
    answers with a real folder on this machine, so ``sweep_start()`` with no
    argument -- which the envelope sweep below calls, because it calls every
    method -- would walk the developer's own project tree on a daemon thread. The
    directory is created rather than merely named so that call is a real, empty,
    millisecond sweep instead of a refusal that proves nothing.
    """
    root = tmp_path / "projects"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("ADC_PROJECT_ROOT", str(root))
    return Bridge(
        runner=JobRunner(),
        store=PlanStore(),
        actions=ActionStore(),
        vss_runner=_no_vss,
        sweeps=sweeper_mod.SweepPlanStore(),
    )


def _unwrap(reply: dict[str, Any]) -> Any:
    """The data half of a successful envelope, asserting it succeeded first."""
    assert reply["ok"] is True, reply
    return reply["data"]


def _code(reply: dict[str, Any]) -> str:
    """The error code of a failed envelope, asserting it failed first."""
    assert reply["ok"] is False, reply
    return str(reply["error"]["code"])


def _item(**kwargs: Any) -> PlanItem:
    base: dict[str, Any] = {
        "target_id": "t",
        "risk": "safe",
        "strategy_kind": "fake",
        "reversible": True,
        "admin_required": False,
        "min_age_hours": 0,
    }
    return PlanItem(**{**base, **kwargs})


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------
def test_every_public_method_answers_the_same_shape(
    api: Bridge, no_explorer: list[Path]
) -> None:
    """Called with no arguments at all -- the shape must survive that too.

    A page bug that calls ``job_poll()`` with nothing gets a refusal, not an
    exception. This iterates the surface rather than listing it, so a method
    added later without ``@guarded`` fails here instead of in production.

    ``no_explorer`` is taken because two methods on this surface reach the shell
    when they succeed: sweeping them for their envelope must not put two Explorer
    windows on the developer's desktop.
    """
    surface = [
        name
        for name in dir(api)
        if not name.startswith("_") and callable(getattr(api, name))
    ]
    assert "reveal" in surface and "clean_execute" in surface

    for name in surface:
        if name in {"admin_relaunch"}:  # would spawn a UAC prompt
            continue
        reply = getattr(api, name)()
        assert isinstance(reply, dict), name
        assert reply["ok"] in (True, False), name
        if reply["ok"]:
            assert set(reply) == {"ok", "data"}, name
        else:
            assert set(reply) == {"ok", "error"}, name
            assert set(reply["error"]) == {"code", "message_vi", "message_en"}, name
            assert reply["error"]["message_vi"] != reply["error"]["message_en"], name


def test_an_unexpected_exception_becomes_internal_and_keeps_the_traceback_out() -> None:
    """The disclosure test. A traceback in the page would print absolute paths.

    ``guarded`` is applied to a function that raises something it has no case
    for, and the assertion is about what the caller *cannot* see: no file path,
    no module name, no exception message.
    """
    secret = "C:\\Users\\Administrator\\AppData\\Local\\adc\\cache\\scan.sqlite"

    @guarded
    def boom() -> None:
        raise OSError(secret)

    reply = boom()

    assert _code(reply) == "internal"
    blob = repr(reply)
    assert secret not in blob
    assert "Traceback" not in blob
    assert "adc" not in reply["error"]["message_en"]
    # The class name is deliberately kept: it is the one thing that helps a user
    # describe the failure, and it names no path.
    assert "OSError" in reply["error"]["message_en"]


def test_a_bridge_error_keeps_its_code_and_both_strings() -> None:
    @guarded
    def refuse() -> None:
        raise BridgeError("teapot", "Ấm trà", "Teapot")

    reply = refuse()

    assert reply == {
        "ok": False,
        "error": {"code": "teapot", "message_vi": "Ấm trà", "message_en": "Teapot"},
    }


# ---------------------------------------------------------------------------
# Coercion -- the argument came from JavaScript
# ---------------------------------------------------------------------------
def test_a_bare_string_is_one_id_not_a_list_of_characters() -> None:
    """The single-row action case. ``"npm_cache"`` must not become nine ids."""
    assert bridge_mod._as_ids("npm_cache", what="x") == ["npm_cache"]


def test_ids_are_deduplicated_in_the_order_they_arrived() -> None:
    """Order matters: the page's row order is what the progress list follows."""
    assert bridge_mod._as_ids(["b", "a", "b", "c"], what="x") == ["b", "a", "c"]


@pytest.mark.parametrize(
    "junk",
    [None, [], (), [None, 0, "", False, {}, []]],
    ids=["none", "empty-list", "empty-tuple", "all-droppable"],
)
def test_nothing_usable_is_an_empty_selection_not_a_crash(junk: object) -> None:
    """Non-strings are dropped rather than stringified.

    ``str(None)`` would become ``"None"``, which is a plausible-looking id and
    would reach ``_known_ids`` as an unknown-target error -- a confusing message
    for what is really an empty selection.
    """
    assert bridge_mod._as_ids(junk, what="x") == []


@pytest.mark.parametrize("junk", [42, {"a": 1}, True], ids=["int", "dict", "bool"])
def test_a_non_list_is_refused_by_shape(junk: object) -> None:
    with pytest.raises(BridgeError) as caught:
        bridge_mod._as_ids(junk, what="selection")
    assert caught.value.code == "bad_input"
    assert "selection" in caught.value.en


def test_a_selection_past_the_ceiling_is_refused_before_anything_walks() -> None:
    """The catalogue is 49 rows, so this is a UI bug or a hostile page.

    Either way the cost of dry-running it should not be unbounded -- the refusal
    happens here, before ``clean_plan`` starts enumerating anything.
    """
    too_many = [f"id{i}" for i in range(MAX_SELECTION + 1)]

    with pytest.raises(BridgeError) as caught:
        bridge_mod._as_ids(too_many, what="selection")
    assert caught.value.code == "bad_input"
    assert str(MAX_SELECTION) in caught.value.en


@pytest.mark.parametrize(
    "raw",
    ["false", "true", 1, 0, [], "1", None],
    ids=["str-false", "str-true", "one", "zero", "empty-list", "str-one", "none"],
)
def test_only_the_boolean_true_unlocks_the_dangerous_rows(raw: object) -> None:
    """``bool("false")`` is True, and this flag gates the irreversible targets.

    JSON from a page can carry a string where a boolean was meant; strict
    identity is the only reading that cannot be surprised by one.
    """
    assert bridge_mod._as_flag(raw) is False


def test_the_boolean_true_does_unlock_them() -> None:
    assert bridge_mod._as_flag(True) is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, 0), (5, 5), (-1, 0), ("7", 0), (None, 0), (True, 0), (1.5, 0)],
    ids=["zero", "five", "negative", "string", "none", "bool", "float"],
)
def test_an_event_cursor_is_a_non_negative_int_or_it_is_zero(
    raw: object, expected: int
) -> None:
    """Junk means "send me everything" -- a resync, not an error.

    ``True`` is excluded explicitly because ``isinstance(True, int)`` holds, and
    a cursor of 1 from a page that meant "yes" would silently skip a log line.
    """
    assert bridge_mod._as_since(raw) == expected


def test_a_missing_job_id_is_refused_by_shape() -> None:
    for junk in (None, "", 0, ["j"]):
        with pytest.raises(BridgeError) as caught:
            bridge_mod._as_job_id(junk)
        assert caught.value.code == "bad_input"


# ---------------------------------------------------------------------------
# reveal -- SEC-02, the method v1 got wrong
# ---------------------------------------------------------------------------
@pytest.fixture
def no_explorer(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record what would have been opened instead of opening it.

    Every test in this section asserts on *what reached the shell*, and the one
    that matters asserts nothing did.
    """
    opened: list[Path] = []
    monkeypatch.setattr(bridge_mod, "_open_folder", opened.append)
    return opened


@pytest.mark.parametrize(
    "path",
    [
        "C:\\Windows",
        "C:\\Users\\Administrator",
        "\\\\?\\C:\\Windows\\System32",
        "\\\\server\\share",
        "C:/Windows",
        "..\\..\\Windows",
        "%WINDIR%",
    ],
    ids=["windows", "profile", "unc-prefixed", "unc", "forward", "traversal", "envvar"],
)
def test_reveal_refuses_a_path_from_the_page(
    api: Bridge, no_explorer: list[Path], path: str
) -> None:
    """v1's hole, closed by shape rather than by a blocklist.

    None of these is refused for *being dangerous* -- they are refused for not
    being catalogue ids, which is the same reason ``"asdf"`` is refused. That is
    what makes the guarantee total: there is no path string that is also an id,
    so there is no path string that gets through.
    """
    reply = api.reveal(path)

    assert _code(reply) == "unknown_target"
    assert no_explorer == []


def test_reveal_refuses_an_unknown_id(api: Bridge, no_explorer: list[Path]) -> None:
    """Same refusal, same code: the page and the catalogue are out of step."""
    assert _code(api.reveal("not_a_real_target")) == "unknown_target"
    assert no_explorer == []


def test_reveal_refuses_a_list_of_ids(api: Bridge, no_explorer: list[Path]) -> None:
    """One folder per call. A list would make the button's meaning ambiguous."""
    assert _code(api.reveal([KNOWN, KNOWN + "x"])) == "unknown_target"
    assert no_explorer == []


def test_reveal_refuses_two_known_ids(api: Bridge, no_explorer: list[Path]) -> None:
    ids = list(targets.ids())[:2]
    assert _code(api.reveal(ids)) == "bad_input"
    assert no_explorer == []


def test_reveal_opens_the_folder_the_resolver_names(
    api: Bridge, no_explorer: list[Path], tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path: an id goes in, and the *engine's* path comes back out.

    The asymmetry is the point -- paths may leave, they may not enter. The page
    is told which folder opened so it can label a row that has several; it never
    gets to choose one.
    """
    row = targets.by_id(KNOWN)
    monkeypatch.setattr(
        type(row.resolver),
        "resolve",
        lambda self: Resolution.found([str(tree)], "test"),
    )

    data = _unwrap(api.reveal(KNOWN))

    assert data == {"target_id": KNOWN, "opened": str(tree)}
    assert no_explorer == [tree]


def test_reveal_reduces_a_file_to_its_parent(
    api: Bridge, no_explorer: list[Path], tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.startfile`` on a file *runs* it, and rows resolve to installer caches."""
    installer = tree / "setup.exe"
    installer.write_bytes(b"MZ")
    row = targets.by_id(KNOWN)
    monkeypatch.setattr(
        type(row.resolver),
        "resolve",
        lambda self: Resolution.found([str(installer)], "test"),
    )

    data = _unwrap(api.reveal(KNOWN))

    assert data["opened"] == str(tree)
    assert no_explorer == [tree]


def test_reveal_says_not_present_when_the_row_resolves_nowhere(
    api: Bridge, no_explorer: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different code from a refusal: this is a fact about the machine.

    ``unknown_target`` means the page is wrong; ``not_present`` means the page is
    right and the tool simply is not installed here.
    """
    row = targets.by_id(KNOWN)
    monkeypatch.setattr(
        type(row.resolver),
        "resolve",
        lambda self: Resolution.unavailable("uv is not installed", "test"),
    )

    reply = api.reveal(KNOWN)

    assert _code(reply) == "not_present"
    assert no_explorer == []


def test_reveal_says_not_present_when_the_resolved_path_is_gone(
    api: Bridge, no_explorer: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver can answer from a config file that outlived the directory."""
    row = targets.by_id(KNOWN)
    monkeypatch.setattr(
        type(row.resolver),
        "resolve",
        lambda self: Resolution.found([str(tmp_path / "vanished")], "test"),
    )

    assert _code(api.reveal(KNOWN)) == "not_present"
    assert no_explorer == []


# ---------------------------------------------------------------------------
# clean_execute -- the token is the only door
# ---------------------------------------------------------------------------
def _inert_plan(token: str = "tok", **kwargs: Any) -> Plan:
    """A spendable plan that deletes nothing when executed.

    Its single item is skipped, so ``runnable`` is empty and ``run_clean`` walks
    no paths. That is what lets the happy path be tested at all: a plan with a
    live item would delete real files, and no test in this file may do that.
    """
    return Plan(
        token=token,
        created_at=time.time(),
        items=(_item(target_id=KNOWN, skipped_reason="inert for tests"),),
        **kwargs,
    )


def _settle(api: Bridge, job_id: str, *, timeout_s: float = 5.0) -> dict[str, Any]:
    """Poll until the job is terminal, and answer with the last poll.

    Polling rather than joining the thread, because polling is what the UI does
    and this is the path that has to work.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        data = _unwrap(api.job_poll(job_id))
        if data["done"]:
            return dict(data)
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s}s")


@pytest.mark.parametrize(
    "junk", [None, "", 0, [], {"token": "t"}], ids=["none", "empty", "int", "list", "dict"]
)
def test_a_clean_without_a_token_is_refused(api: Bridge, junk: object) -> None:
    """No token, no delete -- and the refusal reads as a missing preview."""
    assert _code(api.clean_execute(junk)) == "plan_unknown"


def test_an_unknown_token_is_refused(api: Bridge) -> None:
    assert _code(api.clean_execute("no-such-token")) == "plan_unknown"


def test_an_expired_token_is_refused_with_its_own_code(api: Bridge) -> None:
    """A preview the user walked away from. The UI asks for a fresh one.

    Distinct from unknown on purpose: the numbers were real, they are just old,
    and "preview again" is a different instruction from "something went wrong".
    """
    stale = _inert_plan("stale")
    object.__setattr__(stale, "created_at", time.time() - 10_000)
    api._store.put(stale)

    assert _code(api.clean_execute("stale")) == "plan_expired"


def test_a_spent_token_is_refused_with_its_own_code(api: Bridge) -> None:
    """What a double-clicked confirm button looks like from here.

    The second click must not run the clean again, and the message must say the
    clean already started rather than implying the click failed.
    """
    api._store.put(_inert_plan("once"))
    first = _unwrap(api.clean_execute("once"))
    _settle(api, first["job_id"])

    assert _code(api.clean_execute("once")) == "plan_spent"


def test_the_three_token_failures_have_three_distinct_codes(api: Bridge) -> None:
    """Asserted together, because the UI branches on them separately."""
    api._store.put(_inert_plan("live"))
    stale = _inert_plan("old")
    object.__setattr__(stale, "created_at", time.time() - 10_000)
    api._store.put(stale)

    spent = _unwrap(api.clean_execute("live"))
    _settle(api, spent["job_id"])

    codes = {
        _code(api.clean_execute("absent")),
        _code(api.clean_execute("old")),
        _code(api.clean_execute("live")),
    }
    assert codes == {"plan_unknown", "plan_expired", "plan_spent"}


def test_a_clean_that_ran_reports_its_plan_and_receipt(api: Bridge, data_dir: Path) -> None:
    """The poll payload carries the plan the user approved and the report written.

    ``JobRunner.submit`` discards the worker's return value, so the receipt is
    read back off disk -- this is the test that the bridge's merge works and not
    just that the clean finished.
    """
    api._store.put(_inert_plan("receipt"))

    started = _unwrap(api.clean_execute("receipt"))
    final = _settle(api, started["job_id"])

    partial = final["partial_results"]
    assert partial["kind"] == "clean"
    assert partial["plan"]["token"] == "receipt"
    assert partial["report"] is not None
    assert partial["report"]["job_id"] == started["job_id"]
    # Nothing was runnable, so nothing was reclaimed. The number must be a
    # measured zero, not an estimate carried over from the plan.
    assert partial["report"]["reclaimed_total"] == 0
    # And it landed in the tmp profile. This is the assertion that proves the
    # isolation rather than trusting the fixture: a report in the real
    # %LOCALAPPDATA% would be a test writing into the user's own history.
    assert list((data_dir / "reports").glob("*.json"))


# ---------------------------------------------------------------------------
# One job at a time
# ---------------------------------------------------------------------------
@pytest.fixture
def blocked(api: Bridge) -> Iterator[Job]:
    """A job parked mid-run, so the busy path can be tested without a disk walk.

    Released in the teardown whatever the test does, because a daemon thread
    left waiting on an event would keep the runner busy for the rest of the
    session and every later test would see a spurious refusal.
    """
    gate = threading.Event()
    job = Job(JobKind.SCAN)

    def work(started: Job) -> None:
        gate.wait(timeout=30.0)

    api.runner.submit(job, work)
    while job.state is not JobState.RUNNING:
        time.sleep(0.005)
    try:
        yield job
    finally:
        gate.set()


def test_a_second_scan_is_refused_while_one_runs(api: Bridge, blocked: Job) -> None:
    """One progress bar, one job. Two walks of one disk are slower than one."""
    reply = api.scan_start(None, [KNOWN])

    assert _code(reply) == "busy"
    assert "scan" in reply["error"]["message_en"]


def test_a_preview_is_refused_while_a_job_runs(api: Bridge, blocked: Job) -> None:
    """A preview measured against a disk a clean is changing is stale on arrival."""
    assert _code(api.clean_plan([KNOWN])) == "busy"


def test_a_busy_refusal_does_not_burn_the_token(api: Bridge, blocked: Job) -> None:
    """The check comes before the spend (cleaner.py:800-807), and here is why.

    A click landing while a scan finishes must cost the user nothing. If the
    token were spent first, they would have to preview all over again to find
    out that the answer was "wait".
    """
    api._store.put(_inert_plan("kept"))

    assert _code(api.clean_execute("kept")) == "busy"
    assert api._store.peek("kept") is not None


def test_the_busy_message_names_what_is_running(api: Bridge, blocked: Job) -> None:
    """"Another job is running" alone would leave the user guessing which."""
    reply = api.clean_plan([KNOWN])

    assert blocked.kind.value in reply["error"]["message_en"]
    assert blocked.kind.value in reply["error"]["message_vi"]


# ---------------------------------------------------------------------------
# job_poll -- the cursor, and the merge
# ---------------------------------------------------------------------------
def _logged_job(api: Bridge, count: int = 5) -> Job:
    """A finished job carrying *count* log lines, for the cursor tests."""
    job = Job(JobKind.SCAN)

    def work(started: Job) -> None:
        for i in range(count):
            started.log(Level.INFO, f"dòng {i}", f"line {i}")

    api.runner.submit(job, work)
    _settle(api, job.id)
    return job


def test_polling_an_unknown_job_is_refused_not_a_crash(api: Bridge) -> None:
    """The runner keeps the last twenty jobs; this is a poll for an evicted one."""
    assert _code(api.job_poll("job-that-never-was")) == "no_such_job"


def test_a_poll_carries_the_keys_the_ui_binds_to(api: Bridge) -> None:
    """The SPEC 3.1 payload. ``phase`` and ``partial_results`` are added here.

    ``Job.snapshot`` has no ``partial_results`` key and names its phase
    ``current_phase``; the bridge merges both in, so a rename in the engine
    breaks this test rather than the UI.
    """
    job = _logged_job(api)

    data = _unwrap(api.job_poll(job.id))

    for key in ("phase", "pct", "events", "partial_results", "next_event", "done"):
        assert key in data, key
    assert data["phase"] == data["current_phase"]
    assert data["done"] is True


def test_the_cursor_never_sends_the_same_line_twice(api: Bridge) -> None:
    """``since_event`` is a cursor, not a page number.

    The UI appends to a scrolling list, so a line arriving twice would be
    visible as a duplicate. Feeding ``next_event`` back must drain exactly once.
    """
    job = _logged_job(api, count=5)

    first = _unwrap(api.job_poll(job.id, 0))
    assert len(first["events"]) == 5
    assert first["next_event"] == 5

    second = _unwrap(api.job_poll(job.id, first["next_event"]))
    assert second["events"] == []
    assert second["next_event"] == 5
    # The total is still reported, so the page can tell "caught up" from "more
    # waiting" without keeping its own count.
    assert second["event_count"] == 5


def test_a_junk_cursor_resyncs_from_the_beginning(api: Bridge) -> None:
    """Junk means "send me everything" -- a resync is safe, a silent skip is not."""
    job = _logged_job(api, count=3)

    assert len(_unwrap(api.job_poll(job.id, "nonsense"))["events"]) == 3
    assert len(_unwrap(api.job_poll(job.id, -5))["events"]) == 3


def test_one_poll_cannot_carry_an_unbounded_number_of_lines(api: Bridge) -> None:
    """A cancelled walk over a million files produces thousands of lines.

    The cap is per poll, not per job: ``next_event`` advances by what was
    actually sent, so the rest arrive on the following tick.
    """
    job = _logged_job(api, count=MAX_EVENTS + 25)

    data = _unwrap(api.job_poll(job.id, 0))

    assert len(data["events"]) == MAX_EVENTS
    assert data["next_event"] == MAX_EVENTS
    assert data["event_count"] == MAX_EVENTS + 25
    rest = _unwrap(api.job_poll(job.id, data["next_event"]))
    assert len(rest["events"]) == 25


def test_cancelling_an_unknown_job_is_refused(api: Bridge) -> None:
    assert _code(api.job_cancel("nope")) == "no_such_job"


def test_cancelling_a_running_job_is_acknowledged_immediately(
    api: Bridge, blocked: Job
) -> None:
    """Cancel is cooperative: it returns at once and the job ends on its thread."""
    data = _unwrap(api.job_cancel(blocked.id))

    assert data == {"job_id": blocked.id, "cancel_requested": True}
    assert _unwrap(api.job_poll(blocked.id))["cancel_requested"] is True


# ---------------------------------------------------------------------------
# The read-only surface
# ---------------------------------------------------------------------------
def test_the_catalogue_ships_no_paths(api: Bridge) -> None:
    """Startup call, and it resolves nothing.

    No row may carry a path: finding out what is on this machine is what a scan
    is for, and a catalogue that carried resolved paths would leak the profile
    layout into the page before the user has asked for anything.
    """
    data = _unwrap(api.catalog())

    assert len(data["targets"]) == len(targets.ids())
    assert set(data["presets"]) == set(targets.PRESETS)
    for row in data["targets"]:
        assert "paths" not in row
        assert ":\\" not in repr(row)


def test_settings_come_with_their_defaults(api: Bridge) -> None:
    """So the page can offer a reset without hardcoding a second copy of them."""
    data = _unwrap(api.settings_get())

    assert data["defaults"] == settings_mod.DEFAULTS.as_dict()
    assert set(data["settings"]) == set(data["defaults"])


def test_settings_set_reports_whether_the_write_survived(api: Bridge) -> None:
    """``persisted`` is a real state: a managed profile can be read-only.

    ``settings.update`` discards ``save``'s return value, which is why the bridge
    does the two steps itself -- the page has to be able to say "this will not
    survive a restart".
    """
    data = _unwrap(api.settings_set({"min_age_hours": 48}))

    assert data["settings"]["min_age_hours"] == 48
    assert data["persisted"] is True
    assert _unwrap(api.settings_get())["settings"]["min_age_hours"] == 48


def test_settings_set_ignores_a_key_it_does_not_know(api: Bridge) -> None:
    """A newer page against an older engine degrades instead of erroring."""
    data = _unwrap(api.settings_set({"min_age_hours": 12, "invented_key": "x"}))

    assert data["settings"]["min_age_hours"] == 12
    assert "invented_key" not in data["settings"]


@pytest.mark.parametrize("junk", [None, "x", 42, []], ids=["none", "str", "int", "list"])
def test_settings_set_needs_an_object(api: Bridge, junk: object) -> None:
    assert _code(api.settings_set(junk)) == "bad_input"


def test_volumes_answers_with_the_fixed_disks(api: Bridge) -> None:
    """Read-only and cheap -- this one is allowed to touch the real machine."""
    data = _unwrap(api.volumes())

    assert data["volumes"], "this machine has at least one fixed volume"
    for vol in data["volumes"]:
        assert "free" in vol


def test_admin_state_counts_what_being_unelevated_costs(api: Bridge) -> None:
    """The rows that need UAC are named, so the page can disable them with a reason.

    ``blocked_count`` is derived from ``admin`` rather than stored, so this test
    reads correctly whether or not the suite is running elevated -- which it is,
    on this machine.
    """
    data = _unwrap(api.admin_state())

    gated = {row.id for row in targets.catalog() if row.admin_required}
    assert set(data["admin_required_ids"]) == gated
    assert data["blocked_count"] == (0 if data["admin"] else len(gated))
    assert data["can_relaunch"] is (data["is_windows"] and not data["admin"])


# ---------------------------------------------------------------------------
# scan_start
# ---------------------------------------------------------------------------
def test_a_scan_of_nothing_is_refused(api: Bridge) -> None:
    """Distinct from a bad id: the user has simply not ticked anything yet."""
    assert _code(api.scan_start(None, [])) == "empty_selection"
    assert _code(api.scan_start(["C"], None)) == "empty_selection"


def test_a_scan_of_an_unknown_id_is_refused_before_it_starts(api: Bridge) -> None:
    """The engine would survive it -- the row would just be marked ``error``.

    Refusing here instead says something more useful: the page and the catalogue
    are out of step, which is a bug in one of them.
    """
    reply = api.scan_start(None, [KNOWN, "invented"])

    assert _code(reply) == "unknown_target"
    assert "invented" in reply["error"]["message_en"]
    assert api.runner.active() is None


def _stub_scan(monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any]) -> None:
    """Replace the real walk with a job that records the arguments it was handed.

    Shared by the volume-filter tests: what they are about is which ``Settings``
    the scan is started under, and a real disk walk would only add noise.
    """

    def fake_submit_scan(runner: JobRunner, **kwargs: Any) -> Any:
        seen.update(kwargs)
        job = Job(JobKind.SCAN)
        runner.submit(job, lambda started: None)
        return type("Run", (), {"job": job, "as_dict": lambda self: {"rows": [], "totals": {}}})()

    monkeypatch.setattr(bridge_mod.scanner, "submit_scan", fake_submit_scan)


def test_volume_ids_reach_the_scan_as_a_real_filter(
    api: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick list has to arrive on the run's ``Settings`` to narrow anything.

    The scan is stubbed, so this pins the argument handling only: the narrowing
    itself lives in ``resolvers.narrow_to_volumes`` and is pinned against real
    paths in ``tests/test_resolvers.py``, ``tests/test_scanner.py`` and
    ``tests/test_cleaner.py``. Before P5 this test asserted the opposite -- that
    ``volume_ids`` was recorded and ignored -- which is why it was written to fail
    loudly as a test edit rather than to drift into a silent behaviour change.
    """
    seen: dict[str, Any] = {}
    _stub_scan(monkeypatch, seen)

    data = _unwrap(api.scan_start(["C", "E"], [KNOWN]))

    assert data["volume_ids"] == ["C", "E"]
    assert seen["target_ids"] == [KNOWN]
    assert seen["settings"].volume_ids == ("C", "E")


def test_omitting_volume_ids_uses_the_saved_tick_list_not_every_volume(
    api: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` means "whatever the user saved", which is the fail-safe reading.

    Both pages send ``null`` (overview.js, clean.js) because the config file is
    fresher than their cached copy. Treating that as "every volume" would widen
    every scan past the Settings screen's tick list, so it is pinned here.
    """
    seen: dict[str, Any] = {}
    _stub_scan(monkeypatch, seen)
    _unwrap(api.settings_set({"volume_ids": ["D"]}))

    data = _unwrap(api.scan_start(None, [KNOWN]))

    assert seen["settings"].volume_ids == ("D",)
    assert data["volume_ids"] == ["D"]


def test_an_empty_volume_id_list_overrides_the_saved_one_for_this_scan(
    api: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list is an override, and an empty list is the "every volume" override."""
    seen: dict[str, Any] = {}
    _stub_scan(monkeypatch, seen)
    _unwrap(api.settings_set({"volume_ids": ["D"]}))

    data = _unwrap(api.scan_start([], [KNOWN]))

    assert seen["settings"].volume_ids == ()
    assert data["volume_ids"] == []
    assert _unwrap(api.settings_get())["settings"]["volume_ids"] == ["D"], (
        "an override for one scan must not rewrite what the user saved"
    )


def test_a_scan_records_its_run_so_the_poll_can_grow_the_table(
    api: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows are read live off the ScanRun, which is what makes the table fill in."""
    rows = [{"target_id": KNOWN, "bytes": 1}]

    def fake_submit_scan(runner: JobRunner, **kwargs: Any) -> Any:
        job = Job(JobKind.SCAN)
        runner.submit(job, lambda started: None)
        return type(
            "Run", (), {"job": job, "as_dict": lambda self: {"rows": rows, "totals": {"bytes": 1}}}
        )()

    monkeypatch.setattr(bridge_mod.scanner, "submit_scan", fake_submit_scan)
    started = _unwrap(api.scan_start(None, [KNOWN]))

    partial = _settle(api, started["job_id"])["partial_results"]

    assert partial["kind"] == "scan"
    assert partial["rows"] == rows


def test_payloads_do_not_outlive_the_jobs_they_describe(api: Bridge, blocked: Job) -> None:
    """A long session must not hold every scan's rows forever.

    The runner keeps the last twenty jobs; the bridge's dicts are pruned against
    that on every write, so an evicted job's payload goes with it.

    The live job is what makes this test mean something: pruning has to drop the
    ghost *and keep* the payload of a job that still exists. Without it the
    assertions would also pass if ``_forget_locked`` simply emptied the dicts.
    """
    api._plans["ghost"] = _inert_plan("ghost")
    api._reports["ghost"] = {"job_id": "ghost"}

    api._remember(blocked.id, plan=_inert_plan("live"))

    assert "ghost" not in api._plans
    assert "ghost" not in api._reports
    assert blocked.id in api._plans


# ---------------------------------------------------------------------------
# explore_start / explore_reveal -- SEC-02 one level down
#
# The Disk Explorer is the first view whose subject is a folder the catalogue has
# never heard of, so it is the first place "the page sends ids, never paths" had to
# be made to work for an arbitrary location. It works because the handle the page
# sends back was minted by the engine for a folder it had already shown:
# ``_node_path`` is a lookup, not a parse, and no argument reaches the filesystem
# without going through it.
# ---------------------------------------------------------------------------
@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> Path:
    """A tiny tree standing in for a fixed volume, so a level costs milliseconds.

    ``explore_start`` takes a drive letter and asks
    :func:`adc.engine.volumes.fixed_volumes` where that letter is; pointing one
    fake volume at a tmp tree means the drilling tests run the real engine over
    four files instead of over ``C:``.
    """
    root = tmp_path / "vol"
    (root / "big" / "sub").mkdir(parents=True)
    (root / "small").mkdir()
    (root / "big" / "a.bin").write_bytes(b"a" * 4096)
    (root / "big" / "sub" / "b.bin").write_bytes(b"b" * 2048)
    (root / "small" / "c.bin").write_bytes(b"c" * 1024)
    (root / "loose.bin").write_bytes(b"d" * 5000)
    fake = volumes_mod.Volume(
        root=str(root),
        letter="T",
        label="TMP",
        filesystem="NTFS",
        total=1 << 40,
        free=1 << 30,
    )
    monkeypatch.setattr(bridge_mod.volumes_mod, "fixed_volumes", lambda: [fake])
    return root


def _level(api: Bridge, **kwargs: Any) -> dict[str, Any]:
    """Start one level, poll it to the end, and answer with what the table binds to."""
    started = _unwrap(api.explore_start(**kwargs))
    partial = _settle(api, started["job_id"])["partial_results"]
    assert partial["kind"] == "explore", partial
    return dict(partial)


def _row(level: dict[str, Any], name: str) -> dict[str, Any]:
    return next(row for row in level["rows"] if row["name"] == name)


def test_explore_needs_exactly_one_of_a_volume_or_a_folder(api: Bridge) -> None:
    """Neither is a page bug and both is an ambiguous request; both read the same."""
    assert _code(api.explore_start()) == "bad_input"
    assert _code(api.explore_start("T", "0123456789ab")) == "bad_input"


@pytest.mark.parametrize(
    "path",
    [
        "C:\\Windows",
        "C:\\Users\\Administrator",
        "\\\\?\\C:\\Windows\\System32",
        "\\\\server\\share",
        "C:/Windows",
        "..\\..\\Windows",
        "%WINDIR%",
        "/etc/passwd",
    ],
    ids=["windows", "profile", "unc-prefixed", "unc", "forward", "traversal", "envvar", "posix"],
)
def test_explore_refuses_a_path_where_a_handle_belongs(
    api: Bridge, no_explorer: list[Path], path: str
) -> None:
    """Drilling takes a handle, and a path is not one. Refused on shape, first.

    ``bad_input`` rather than ``unknown_node`` on purpose: a path could never be a
    handle the engine minted, so this is a malformed call and not a stale one.
    Nothing reaches the filesystem either way, which is what ``no_explorer`` pins.
    """
    assert _code(api.explore_start(None, path)) == "bad_input"
    assert _code(api.explore_reveal(path)) == "bad_input"
    assert no_explorer == []


def test_an_unknown_but_well_formed_handle_is_unknown_node(api: Bridge) -> None:
    """A level the runner has evicted, or a row that pruning dropped. Not a crash.

    Kept apart from ``bad_input`` because the UI says something different: this one
    means "scan again", which the page can act on without a bug report.
    """
    assert _code(api.explore_start(None, "deadbeefcafe")) == "unknown_node"
    assert _code(api.explore_reveal("deadbeefcafe")) == "unknown_node"


def test_a_volume_that_is_not_on_this_machine_is_not_present(
    api: Bridge, volume: Path
) -> None:
    """A well-formed letter for a drive that is not there is a different answer."""
    assert _code(api.explore_start("Z")) == "not_present"
    assert api.runner.active() is None


def test_explore_start_answers_with_the_level_identity_before_the_first_poll(
    api: Bridge, volume: Path
) -> None:
    """The header and the breadcrumb bar draw from this reply, not from a 250 ms tick.

    The lowercase letter is deliberate: a volume id is normalised the same way
    :mod:`adc.engine.settings` normalises the Settings screen's tick list, so
    ``"t"`` and ``"T:"`` and ``"T:\\"`` are one volume.
    """
    data = _unwrap(api.explore_start("t"))

    assert data["root"] == str(volume)
    assert data["name"] == volume.name
    assert data["volume"] == volumes_mod.volume_letter(str(volume))
    assert data["crumbs"][-1]["node_id"] == data["node_id"]
    assert set(data["crumbs"][0]) == {"node_id", "label"}
    _settle(api, data["job_id"])


def test_the_poll_carries_the_level_and_no_paths_in_its_rows(
    api: Bridge, volume: Path
) -> None:
    """``partial_results`` is what the table binds to, so the leak test belongs here.

    The level's own root leaves, because the header shows it. A *row* never carries
    one: a row is a thing the user can click, and a path in a click handler is
    SEC-02 again.
    """
    level = _level(api, volume_id="T")

    assert level["root"] == str(volume)
    assert {row["name"] for row in level["rows"]} == {"big", "small", "loose.bin"}
    for row in level["rows"]:
        assert "path" not in row, row
        assert len(row["node_id"]) == 12
    assert level["totals"]["total_size"] == 4096 + 2048 + 1024 + 5000


def test_a_row_handle_drills_into_that_folder(api: Bridge, volume: Path) -> None:
    """The whole point of the view, and the acceptance row for "dao xuong".

    The page never learns where ``big`` is. It hands back the handle the level
    minted for that row and the bridge looks the path up, which is why drilling
    into an arbitrary folder does not reopen SEC-02.
    """
    top = _level(api, volume_id="T")

    inner = _level(api, node_id=_row(top, "big")["node_id"])

    assert inner["root"] == str(volume / "big")
    assert inner["name"] == "big"
    assert {row["name"] for row in inner["rows"]} == {"sub", "a.bin"}
    assert [crumb["label"] for crumb in inner["crumbs"]][-2:] == [volume.name, "big"]
    assert inner["parent_id"] is not None


def test_a_crumb_handle_walks_back_up(api: Bridge, volume: Path) -> None:
    """Going up needs no method of its own -- crumbs carry handles too.

    The id is a fresh one, minted for this level's own crumb chain rather than
    remembered from the level above: a level *is* the id -> path table, so climbing
    out is the same lookup as drilling in.
    """
    top = _level(api, volume_id="T")
    inner = _level(api, node_id=_row(top, "big")["node_id"])

    back = _level(api, node_id=inner["parent_id"])

    assert back["root"] == str(volume)
    assert {row["name"] for row in back["rows"]} == {"big", "small", "loose.bin"}


def test_a_file_handle_is_refused_as_a_folder(api: Bridge, volume: Path) -> None:
    """Rows include files, and a file is not somewhere to drill.

    ``bad_input`` because the UI should not have offered it -- the row says
    ``kind: "file"`` -- but the engine refuses it anyway rather than trusting the
    page to have read its own row correctly.
    """
    top = _level(api, volume_id="T")

    reply = api.explore_start(None, _row(top, "loose.bin")["node_id"])

    assert _code(reply) == "bad_input"
    assert "file" in reply["error"]["message_en"]


def test_a_folder_deleted_between_the_click_and_the_call_is_not_present(
    api: Bridge, volume: Path
) -> None:
    """The handle is good and the folder is gone. Not a bug on either side."""
    top = _level(api, volume_id="T")
    handle = _row(top, "small")["node_id"]
    (volume / "small" / "c.bin").unlink()
    (volume / "small").rmdir()

    assert _code(api.explore_start(None, handle)) == "not_present"


def test_explore_is_refused_while_another_job_runs(
    api: Bridge, blocked: Job, volume: Path
) -> None:
    """One progress bar, and a level is a disk walk like any other."""
    reply = api.explore_start("T")

    assert _code(reply) == "busy"
    assert blocked.kind.value in reply["error"]["message_en"]


def test_explore_reveal_opens_the_folder_a_handle_stands_for(
    api: Bridge, no_explorer: list[Path], volume: Path
) -> None:
    """Paths may leave, they may not enter -- ``reveal``'s asymmetry, one level down."""
    top = _level(api, volume_id="T")

    data = _unwrap(api.explore_reveal(_row(top, "big")["node_id"]))

    assert no_explorer == [volume / "big"]
    assert data["opened"] == str(volume / "big")


def test_explore_reveal_reduces_a_file_to_its_parent(
    api: Bridge, no_explorer: list[Path], volume: Path
) -> None:
    """``os.startfile`` on a file *runs* it, and every level lists files."""
    top = _level(api, volume_id="T")

    data = _unwrap(api.explore_reveal(_row(top, "loose.bin")["node_id"]))

    assert no_explorer == [volume]
    assert data["opened"] == str(volume)


def test_the_newest_level_answers_first_for_a_handle(api: Bridge, volume: Path) -> None:
    """Two live levels, and the one the page is looking at is the one asked first.

    Drilling re-mints the crumb chain on every level, so the same folder has a
    different handle in each of them. Newest-first means a handle from the level on
    screen resolves without a scan of history, and an older level's handles keep
    working until the runner evicts them -- which is what makes the browser's
    back button cheap.
    """
    top = _level(api, volume_id="T")
    inner = _level(api, node_id=_row(top, "big")["node_id"])

    assert api._node_path(top["node_id"]) == str(volume)
    assert api._node_path(inner["node_id"]) == str(volume / "big")
    assert len(api._explores) == 2


def test_a_level_does_not_outlive_the_job_it_describes(
    api: Bridge, blocked: Job, tmp_path: Path
) -> None:
    """The registry is the id -> path table, so its lifetime is the security boundary.

    A handle that kept resolving after its level was gone would be a path the page
    could hold indefinitely. ``_forget_locked`` prunes ``_explores`` against the
    runner on every write, exactly as it does the scan rows and the plans, so the
    table expires on its own and there is nothing to expire by hand.
    """
    ghost = bridge_mod.explorer.ExploreRun(
        bridge_mod.explorer.new_job(), str(tmp_path)
    )
    api._explores["ghost"] = ghost

    api._remember(blocked.id, plan=_inert_plan("live"))

    assert "ghost" not in api._explores
    assert _code(api.explore_reveal(ghost.node_id)) == "unknown_node"


# @@SWEEP_SECTION@@

# ---------------------------------------------------------------------------
# History and the log button. Both exist for P4's views, and both are on this
# surface only because neither takes a location from the page: `history` reads
# the reports directory the engine owns, and `open_log` takes no argument at all.
# ---------------------------------------------------------------------------
def test_history_reads_the_reports_directory_newest_first(api: Bridge, data_dir: Path) -> None:
    """Two receipts on disk come back in the order the History list shows them.

    Written through ``report.write_report`` rather than as hand-made files, so the
    test breaks if the summary row's keys change -- which is what the view binds to.
    """
    reports = data_dir / "reports"
    for index, job_id in enumerate(("scan-aaa", "clean-bbb")):
        report_mod.write_report(
            {
                "job_id": job_id,
                "kind": "clean",
                "state": "done",
                "finished_at": 1_700_000_000.0 + index,
                "duration_s": 1.5,
                "reclaimed_total": 4096 * (index + 1),
                "per_target": {KNOWN: {}},
            },
            directory=reports,
        )
        # mtime is what list_reports sorts on, and two files written in the same
        # millisecond would make the order a coin toss.
        os.utime(reports / f"{job_id}.json", (1_700_000_000 + index, 1_700_000_000 + index))

    data = _unwrap(api.history())

    assert [row["job_id"] for row in data["reports"]] == ["clean-bbb", "scan-aaa"]
    assert data["reports"][0]["reclaimed_total"] == 8192
    # Free space comes with the list because the trend chart's right edge is now,
    # not the newest report's snapshot of then.
    assert data["volumes"] and "free" in data["volumes"][0]


def test_history_never_carries_the_log_lines(api: Bridge, data_dir: Path) -> None:
    """A list of thirty runs must not serialise thirty event arrays into the page."""
    report_mod.write_report(
        {"job_id": "scan-ccc", "kind": "scan", "state": "done",
         "events": [{"level": "info", "message_vi": "x", "message_en": "x"}] * 50},
        directory=data_dir / "reports",
    )

    row = _unwrap(api.history())["reports"][0]

    assert "events" not in row


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 30),
        (True, 30),
        ("40", 30),
        (0, 30),
        (-5, 30),
        (7, 7),
        (7.0, 7),
        (7.5, 30),
        (999, 200),
    ],
    ids=[
        "none",
        "bool",
        "string",
        "zero",
        "negative",
        "int",
        "integral-float",
        "fraction",
        "ceiling",
    ],
)
def test_a_history_limit_from_js_is_coerced_or_defaulted(raw: object, expected: int) -> None:
    """JavaScript has one number type, so ``25`` may arrive as ``25.0``.

    An integral float is accepted rather than refused: refusing it would be a
    failure whose cause is invisible from the page. Everything else falls back to
    the default, and nothing exceeds the ceiling.
    """
    assert bridge_mod._as_limit(raw, default=30, ceiling=200) == expected


def test_report_detail_returns_one_run_in_full(api: Bridge, data_dir: Path) -> None:
    """The detail view wants the events the list deliberately dropped."""
    report_mod.write_report(
        {"job_id": "clean-ddd", "kind": "clean", "state": "done",
         "events": [{"level": "info", "message_vi": "xong", "message_en": "done"}]},
        directory=data_dir / "reports",
    )

    body = _unwrap(api.report_detail("clean-ddd"))["report"]

    assert body["job_id"] == "clean-ddd"
    assert body["events"][0]["message_vi"] == "xong"


def test_report_detail_cannot_reach_outside_the_reports_directory(
    api: Bridge, data_dir: Path
) -> None:
    """The id becomes a filename, so traversal is the thing to prove impossible.

    ``load_report`` joins the id onto the reports directory and appends ``.json``,
    so these resolve to nothing that exists -- the refusal is ``no_such_report``,
    not a file from somewhere else. The settings file next door is real and is
    named here on purpose: if the join were ever loosened, this is the test that
    would find it.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text('{"language": "en"}', encoding="utf-8")

    for attempt in ("../settings", r"..\settings", r"C:\Windows\win", "../../../../etc/passwd"):
        assert _code(api.report_detail(attempt)) == "no_such_report", attempt


def test_open_log_opens_the_directory_and_takes_no_argument(
    api: Bridge, no_explorer: list[Path], data_dir: Path
) -> None:
    """The folder, never the file: ``startfile`` on a ``.log`` would launch it.

    And nothing about *which* file is up to the page -- the method has no
    parameter, which is why it can sit beside ``reveal``'s refusal to accept one.
    """
    data = _unwrap(api.open_log())

    assert no_explorer == [Path(data["opened"])]
    assert Path(data["opened"]).is_dir()
    assert data["log_file"].endswith(".log")
    assert Path(data["log_file"]).parent == Path(data["opened"])
    assert inspect.signature(Bridge.open_log).parameters.keys() == {"self"}


# ---------------------------------------------------------------------------
# Shadow copies -- the second handshake
# ---------------------------------------------------------------------------
# ``vss_manage`` is the one catalogue row that is not a checkbox (docs/02-SPEC.md
# 5), and these four methods are why: a read, two previews, and one redemption.
# The captures are trimmed from this machine's own ``vssadmin`` output -- the
# parsers are tested against the full thing in ``tests/test_vss.py``; here they
# only need to give the bridge something real to join volumes against.
#
# Every test in this section injects a runner. Not one of them may reach the real
# Volume Shadow Copy service: this shell is elevated and ``vssadmin`` is on PATH,
# so a test that forgot would resize this machine's shadow storage for real.
STORAGE = r"""Shadow Copy Storage association
   For volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
   Shadow Copy Storage volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
   Used Shadow Copy Storage space: 1.93 GB (1%)
   Allocated Shadow Copy Storage space: 2.00 GB (1%)
   Maximum Shadow Copy Storage space: 2.00 GB (1%)
"""

# The same reading, with C:'s copies kept on D:. ``/on=`` has to follow this.
STORAGE_ELSEWHERE = STORAGE.replace(
    "Shadow Copy Storage volume: (C:)", "Shadow Copy Storage volume: (D:)"
)

SHADOWS = r"""Contents of shadow copy set ID: {da4b9974-8ca0-4602-ac7e-d1c5f23fd25d}
   Contained 1 shadow copies at creation time: 3/09/2026 1:40:44 AM
      Shadow Copy ID: {7c13d002-0627-4c14-9e7d-682f93ea9d32}
         Original Volume: (C:)\\?\Volume{cbd6257a-71bb-4473-b800-498f0a8e380c}\
         Shadow Copy Volume: \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1
         Provider: 'Microsoft Software Shadow Copy provider 1.0'
         Type: ClientAccessibleWriters
"""

C_TOTAL = int(134.49 * (1024**3))       # this machine's C:, to the same rounding


class VssRunner:
    """A vssadmin that answers the two list commands and records every argv.

    Anything else -- a resize, a delete -- answers *mutation* and is recorded, so
    a test can assert both that the right command ran and that no other did.
    """

    def __init__(
        self, storage: str = STORAGE, shadows: str = SHADOWS,
        mutation: tuple[int | None, str] = (0, "Successfully resized"),
    ) -> None:
        self.storage = storage
        self.shadows = shadows
        self.mutation = mutation
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int | None, str]:
        self.calls.append(tuple(argv))
        joined = " ".join(argv)
        if "list shadowstorage" in joined:
            return 0, self.storage
        if "list shadows" in joined:
            return 0, self.shadows
        return self.mutation

    @property
    def commands(self) -> list[str]:
        """Each argv joined, minus the executable path."""
        return [" ".join(call[1:]) for call in self.calls]

    @property
    def mutations(self) -> list[str]:
        return [cmd for cmd in self.commands if not cmd.startswith("list ")]


@pytest.fixture
def vss_api(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Bridge, VssRunner]]:
    """A bridge whose vssadmin is a recorder, on a machine with one C: volume.

    Elevation and the tool's presence are pinned rather than read, so the section
    behaves the same in an unelevated shell and on a machine with no VSS at all --
    what it is testing is the bridge, not this box.
    """
    runner = VssRunner()
    monkeypatch.setattr(vss_mod.win, "is_user_an_admin", lambda: True)
    monkeypatch.setattr(vss_mod, "_vssadmin", lambda: r"C:\Windows\System32\vssadmin.exe")
    fake = volumes_mod.Volume(
        root="C:\\", letter="C", label="", filesystem="NTFS",
        total=C_TOTAL, free=int(12.4 * (1024**3)),
    )
    monkeypatch.setattr(bridge_mod.volumes_mod, "fixed_volumes", lambda: [fake])
    yield Bridge(
        runner=JobRunner(), store=PlanStore(), actions=ActionStore(), vss_runner=runner,
    ), runner


def test_vss_status_reads_the_cap_and_says_what_a_percentage_would_mean(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    """The screen's whole job: show the 2 GB ceiling, and the two ways out of it.

    ``totals`` is carried so the page can render "10 % = 13.4 GB" without a second
    call, and ``suggested`` so the two values SPEC 5 names come from the engine
    rather than being spelled again in JavaScript.
    """
    api, runner = vss_api

    data = _unwrap(api.vss_status())

    assert runner.commands == ["list shadowstorage", "list shadows"]
    assert data["supported"] is True and data["is_admin"] is True
    assert data["storages"][0]["maximum"] == 2 * 1024**3
    assert data["storages"][0]["unbounded"] is False
    assert data["copies"][0]["set_id"].startswith("{da4b9974")
    assert data["totals"] == {"C": C_TOTAL}
    assert [limit["arg"] for limit in data["suggested"]] == ["10%", "UNBOUNDED"]
    assert data["min_maxsize"] == vss_mod.MIN_MAXSIZE_BYTES
    assert data["error"] is None


def test_vss_status_still_answers_ok_when_it_cannot_read(
    api: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Not elevated" is what the screen draws, not an error envelope it cannot
    render. A refusal here would leave the view with nothing to say."""
    monkeypatch.setattr(vss_mod.win, "is_user_an_admin", lambda: False)
    monkeypatch.setattr(vss_mod, "_vssadmin", lambda: r"C:\Windows\System32\vssadmin.exe")

    data = _unwrap(api.vss_status())

    assert data["supported"] is True and data["is_admin"] is False
    assert data["storages"] == [] and data["copies"] == []
    assert data["error"] is not None


def test_a_resize_preview_runs_nothing_and_hands_back_a_receipt(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    """The acceptance row's own call (docs/03-PLAN.md:212), one step short of doing it.

    10 % of this volume is 13.4 GB against a 2.00 GB ceiling, so nothing can be
    evicted -- and the way back from BUG-09 does not demand a typed phrase.
    """
    api, runner = vss_api

    data = _unwrap(api.vss_resize_preview("C", "10%"))

    assert runner.mutations == []
    assert data["preview"]["dry_run"] is True
    assert data["preview"]["command"].endswith(
        "resize shadowstorage /for=C: /on=C: /maxsize=10%"
    )
    assert data["destructive"] is False and data["phrase"] == ""
    assert data["limit"] == {"kind": "percent", "value": 10, "arg": "10%"}
    assert len(data["token"]) == 32


def test_a_preview_asks_the_machine_where_the_copies_live(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    """``/on=`` is read from the reading, never sent by the page: point it at the
    wrong volume and vssadmin makes a second association and leaves the real
    ceiling alone."""
    api, runner = vss_api
    runner.storage = STORAGE_ELSEWHERE

    data = _unwrap(api.vss_resize_preview("C", "UNBOUNDED"))

    assert data["preview"]["command"].endswith("/for=C: /on=D: /maxsize=UNBOUNDED")
    assert data["on_volume"] == "D:"


def test_lowering_the_ceiling_comes_back_with_a_phrase_to_type(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    """SPEC 4.3: the DANGEROUS tier types a confirmation. Below 2.00 GB, Windows
    evicts copies to fit, so this is the losing direction and the page is told the
    worst case -- the one copy the reading found."""
    api, _ = vss_api

    data = _unwrap(api.vss_resize_preview("C", str(400 * 1024**2)))

    assert data["destructive"] is True
    assert data["phrase"] == "C:"
    assert data["copies_at_risk"] == 1


@pytest.mark.parametrize("limit", ["10", "5 GB", "nonsense", "0%", "101%", "", None, "-1"])
def test_the_page_cannot_send_a_limit_the_tool_would_refuse(
    vss_api: tuple[Bridge, VssRunner], limit: object
) -> None:
    """``"10"`` is in the list on purpose: to vssadmin a bare number means *bytes*,
    so a page that dropped the percent sign would ask for a ten-byte ceiling.
    :class:`~adc.engine.vss.Limit` refuses it below the tool's own 320 MB floor."""
    api, runner = vss_api

    assert _code(api.vss_resize_preview("C", limit)) == "bad_limit"
    assert runner.mutations == []


@pytest.mark.parametrize("volume", [r"C:\Windows", "", "CC", "1", None, 3, "C: & del /f"])
def test_a_volume_that_is_not_a_letter_is_refused_before_anything_is_read(
    vss_api: tuple[Bridge, VssRunner], volume: object
) -> None:
    """The same rule as everywhere else on this surface: ids in, never paths.

    ``/for=`` takes a volume spec, so a spec from the page would be a command
    argument from the page. A letter is the only shape that gets through, and it is
    checked before vssadmin is asked anything at all.
    """
    api, runner = vss_api

    assert _code(api.vss_resize_preview(volume, "10%")) == "bad_input"
    assert _code(api.vss_delete_preview(volume)) == "bad_input"
    assert runner.calls == []


def test_a_volume_this_machine_does_not_have_reads_as_not_present(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    """A well-formed letter for a drive that is not there is a different answer --
    and refusing it is what keeps a percentage from being applied against a volume
    whose size nobody could measure."""
    api, runner = vss_api

    assert _code(api.vss_resize_preview("Z", "10%")) == "not_present"
    assert runner.mutations == []


def test_a_delete_preview_defaults_to_the_oldest_copy(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    """v1's only move was ``/all /quiet`` (docs/01-AUDIT.md BUG-09). The default
    here takes one copy, and every delete demands the phrase."""
    api, runner = vss_api

    data = _unwrap(api.vss_delete_preview("c:"))

    assert runner.mutations == []
    assert data["preview"]["command"].endswith("delete shadows /for=C: /oldest /quiet")
    assert data["destructive"] is True and data["phrase"] == "C:"
    assert data["scope"] == "oldest" and data["copies_at_risk"] == 1
    assert data["limit"] is None


def test_deleting_everything_has_to_be_asked_for_by_name(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    api, _ = vss_api

    data = _unwrap(api.vss_delete_preview("C", "all"))

    assert data["preview"]["command"].endswith("/for=C: /all /quiet")
    assert data["scope"] == "all"
    assert data["copies_at_risk"] == 1          # what the reading actually found


@pytest.mark.parametrize("scope", ["everything", "newest", "/all", "0", 7])
def test_an_unknown_delete_scope_is_a_page_bug(
    vss_api: tuple[Bridge, VssRunner], scope: object
) -> None:
    api, runner = vss_api

    assert _code(api.vss_delete_preview("C", scope)) == "bad_input"
    assert runner.mutations == []


def test_a_typo_in_the_phrase_costs_a_retry_and_not_the_preview(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    """The phrase is checked before the token is spent, so the second try works.

    Getting this the other way round would mean a user who mistyped had to
    re-preview -- and on this screen re-previewing means reading the disk again.
    """
    api, runner = vss_api
    token = _unwrap(api.vss_delete_preview("C"))["token"]

    assert _code(api.vss_apply(token, "yes")) == "vss_phrase"
    assert _code(api.vss_apply(token, "D:")) == "vss_phrase"
    assert runner.mutations == []

    data = _unwrap(api.vss_apply(token, "c:"))

    assert data["result"]["ok"] is True
    assert runner.mutations == ["delete shadows /for=C: /oldest /quiet"]


def test_apply_runs_the_argv_the_receipt_has_held_since_the_preview(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    """What the dialog showed is what runs. Nothing is rebuilt at redemption time,
    so no argument arriving later can travel under an approval given earlier."""
    api, runner = vss_api
    preview = _unwrap(api.vss_resize_preview("C", "10%"))

    data = _unwrap(api.vss_apply(preview["token"]))

    assert runner.mutations == ["resize shadowstorage /for=C: /on=C: /maxsize=10%"]
    assert data["result"]["argv"] == preview["preview"]["argv"]
    assert data["result"]["dry_run"] is False
    assert data["action"]["token"] == preview["token"]


def test_raising_the_ceiling_is_one_confirm_and_no_phrase(
    vss_api: tuple[Bridge, VssRunner],
) -> None:
    """The acceptance row end to end, against a recorded vssadmin: 2.00 GB -> 10 %.

    ``vss_apply`` is called with no phrase at all, because this is the direction
    that gives something back (docs/03-PLAN.md:212). Demanding a typed volume to
    undo BUG-09 would make the repair feel like the damage.
    """
    api, runner = vss_api

    token = _unwrap(api.vss_resize_preview("C", "10%"))["token"]
    data = _unwrap(api.vss_apply(token))

    assert data["result"]["ok"] is True
    assert runner.mutations == ["resize shadowstorage /for=C: /on=C: /maxsize=10%"]


def test_a_receipt_is_good_once(vss_api: tuple[Bridge, VssRunner]) -> None:
    """A double-clicked confirm button must not delete twice."""
    api, runner = vss_api
    token = _unwrap(api.vss_delete_preview("C", "all"))["token"]

    _unwrap(api.vss_apply(token, "C:"))

    assert _code(api.vss_apply(token, "C:")) == "vss_spent"
    assert len(runner.mutations) == 1


@pytest.mark.parametrize("token", ["", None, "nope", "0" * 32, 7, {"token": "x"}])
def test_apply_without_a_real_receipt_runs_nothing(
    vss_api: tuple[Bridge, VssRunner], token: object
) -> None:
    """No token, a made-up token, and a token-shaped object are the same answer.

    This is the door: everything else on this screen is a read or a preview, so a
    page that could talk its way past this line could resize the store by itself.
    """
    api, runner = vss_api

    assert _code(api.vss_apply(token, "C:")) == "vss_unknown"
    assert runner.mutations == []


def test_an_old_receipt_is_refused_and_says_so(vss_api: tuple[Bridge, VssRunner]) -> None:
    """After the TTL the numbers in the dialog are stale, so the receipt dies.

    Not pedantry: this machine's store held one copy and 1.93 GB on 2026-09-03 and
    nothing at all a day later (docs/01-AUDIT.md BUG-09). A confirmation measured
    against the old reading would be confirming something that no longer exists.
    """
    api, runner = vss_api
    token = _unwrap(api.vss_delete_preview("C"))["token"]
    pending = api._actions.peek(token)
    assert pending is not None
    pending.created_at -= vss_mod.ACTION_TTL_S + 1

    assert _code(api.vss_apply(token, "C:")) == "vss_expired"
    assert runner.mutations == []


def test_neither_preview_is_offered_to_a_standard_user(
    vss_api: tuple[Bridge, VssRunner], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without elevation vssadmin will not even list, so there is nothing to preview.

    ``need_admin`` is its own code because the page can act on it -- that is what
    ``admin_relaunch`` is for -- while ``unsupported`` is a dead end.
    """
    api, runner = vss_api
    monkeypatch.setattr(vss_mod.win, "is_user_an_admin", lambda: False)

    assert _code(api.vss_resize_preview("C", "10%")) == "need_admin"
    assert _code(api.vss_delete_preview("C")) == "need_admin"
    assert runner.mutations == []


def test_a_machine_without_vssadmin_refuses_the_previews(
    vss_api: tuple[Bridge, VssRunner], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows Home ships no vssadmin. The screen has to say that, not draw zeros."""
    api, runner = vss_api
    monkeypatch.setattr(vss_mod, "_vssadmin", _raise_unavailable)

    assert _code(api.vss_resize_preview("C", "10%")) == "unsupported"
    assert _code(api.vss_delete_preview("C")) == "unsupported"
    assert runner.calls == []


def _raise_unavailable() -> str:
    raise vss_mod.VssUnavailableError("vssadmin.exe not found on this machine")


# ---------------------------------------------------------------------------
# The other side of the boundary
# ---------------------------------------------------------------------------
def test_the_page_can_reach_every_method_the_bridge_exposes() -> None:
    """``ADC.api`` and ``Bridge`` are one surface described twice, so they drift.

    A Python method with no wrapper in bridge.js is unreachable -- and, worse, so is
    a wrapper that calls a name Python no longer has: ``call('vss_stats')`` would
    fail at runtime as an ``AttributeError`` inside pywebview, in a promise, in a
    view, which is three layers away from the typo. Neither direction is visible to
    any other test here: the Python suite never reads the JS, and the JS gate is
    ``node --check``, which cannot know what a string argument means.

    Both directions are asserted, and both are equalities: this file's own
    ``test_every_public_method_answers_the_same_shape`` already iterates the Python
    surface, so a method added without a wrapper cannot hide behind a subset check.
    """
    js = (
        Path(__file__).resolve().parents[1]
        / "src" / "adc" / "ui" / "js" / "bridge.js"
    ).read_text(encoding="utf-8")
    wired = set(re.findall(r"call\('([a-z_]+)'", js))
    surface = {
        name
        for name, member in inspect.getmembers(Bridge, callable)
        if not name.startswith("_") and getattr(member, "__module__", "") == Bridge.__module__
    }

    assert sorted(surface - wired) == [], "public Bridge methods with no ADC.api wrapper"
    assert sorted(wired - surface) == [], "ADC.api calls a method Bridge does not have"


def test_updater_bridge_endpoints(api: Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from adc.engine import updater

    mock_mgr = MagicMock()
    fake_info = updater.UpdateInfo(
        available=True,
        current_version="2.0.0",
        latest_version="v2.1.0",
        release_name="Disk CleanUp v2.1.0",
        release_notes="Notes",
        published_at="2026-09-18T12:00:00Z",
        asset_name="DiskCleanUp-Setup-2.1.0-x64.exe",
        asset_url="https://example.com/installer.exe",
        asset_size=1024,
        sha256="abc",
        html_url="https://example.com",
    )
    mock_mgr.check_update.return_value = fake_info
    mock_mgr.start_download.return_value = updater.DownloadProgress(
        status="downloading", total_bytes=1024
    )
    mock_mgr.get_progress.return_value = updater.DownloadProgress(status="downloading", pct=50.0)
    mock_mgr.cancel_download.return_value = updater.DownloadProgress(status="cancelled")
    mock_mgr.launch_installer.return_value = True

    monkeypatch.setattr(updater, "get_manager", lambda: mock_mgr)

    # updater_check
    res_check = api.updater_check(force=True)
    assert res_check["ok"] is True
    assert res_check["data"]["available"] is True
    assert res_check["data"]["info"]["latest_version"] == "v2.1.0"

    # updater_download_start
    res_start = api.updater_download_start()
    assert res_start["ok"] is True
    assert res_start["data"]["progress"]["status"] == "downloading"

    # updater_download_progress
    res_prog = api.updater_download_progress()
    assert res_prog["ok"] is True
    assert res_prog["data"]["progress"]["pct"] == 50.0

    # updater_download_cancel
    res_cancel = api.updater_download_cancel()
    assert res_cancel["ok"] is True
    assert res_cancel["data"]["progress"]["status"] == "cancelled"

    # updater_install
    res_inst = api.updater_install()
    assert res_inst["ok"] is True
    assert res_inst["data"]["launched"] is True
