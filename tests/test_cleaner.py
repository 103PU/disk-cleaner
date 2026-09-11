r"""Cleaner tests: the plan is the promise, the clean is the receipt.

Four rules shape this module.

* **Nothing real is ever deleted.** Every target here is synthetic and resolves
  into ``tmp_path``. ``cleaner.find`` is monkeypatched, so no test can reach the
  fifty-row catalogue and therefore no test can reach a real cache directory.
* **The token is the only door.** ``submit_clean`` takes a token and nothing
  else, so these tests spend tokens rather than passing target lists, and the
  three failure modes -- unknown, expired, spent -- are asserted apart, because
  the UI has something different to say for each.
* **A number is either measured or absent.** A row that never ran must not
  report its zero as a measured size. That is the BUG-12 family of lie, and
  ``measurable`` is the field that keeps it out of the payload.
* **The receipt survives.** A cancel and a crash both still write a report: a
  clean that deleted files and then failed must leave a record of what it did.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pytest

from adc.engine import cleaner
from adc.engine.cleaner import (
    PLAN_TTL_S,
    SAMPLE_PATHS,
    ExpiredPlanError,
    Plan,
    PlanItem,
    PlanStore,
    SpentPlanError,
    UnknownPlanError,
    min_age_for,
    plan,
    plan_store,
    run_clean,
    submit_clean,
)
from adc.engine.guard import GuardError
from adc.engine.jobs import CancelledError, JobRunner, JobState
from adc.engine.resolvers import Resolution
from adc.engine.settings import Settings
from adc.engine.strategies import CleanContext, StrategyResult
from adc.engine.targets import Category, Risk, Target
from adc.engine.volumes import volume_letter
from tests.fixtures.make_tree import write_file


@dataclass(eq=False)
class FakeResolver:
    """Resolves wherever the test says.

    ``eq=False`` rather than ``frozen=True``: :class:`Target` is frozen and
    therefore hashes its fields, and identity hashing is enough here.
    """

    paths: tuple[str, ...] = ()
    available: bool = True
    reason: str | None = None
    kind: ClassVar[str] = "fake"

    @property
    def source(self) -> str:
        return "fake:test"

    def resolve(self) -> Resolution:
        if not self.available:
            return Resolution.unavailable(self.reason or "not present", self.source)
        if not self.paths:
            # Available with nothing to walk: the hibernation/DISM shape.
            return Resolution.systemic(self.source)
        return Resolution.found(list(self.paths), self.source)


@dataclass(eq=False)
class FakeStrategy:
    """Deletes for real, but only under ``tmp_path``; records every context.

    ``reversible`` and ``needs_admin`` are instance fields where the protocol
    declares class variables, so that one test can vary them without a second
    class. Structurally identical at runtime, which is all the engine asks.
    """

    kind: ClassVar[str] = "fake"
    reversible: bool = True
    needs_admin: bool = False
    raises: BaseException | None = None
    ok: bool = True
    skipped_reason: str | None = None
    notes: tuple[str, ...] = ()
    # Sets the flag on the job's own token, which is how a user pressing stop
    # arrives from a strategy's point of view.
    cancels: bool = False
    # Misbehave during the clean only. Without this a row meant to fail at clean
    # time fails its dry run instead, and the plan skips it before the clean can
    # ever reach it -- which is a different test than the one intended.
    clean_only: bool = False
    calls: list[CleanContext] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind}

    def execute(self, ctx: CleanContext) -> StrategyResult:
        self.calls.append(ctx)
        misbehave = not (self.clean_only and ctx.dry_run)
        if self.cancels and misbehave:
            ctx.cancel.cancel()
        if self.raises is not None and misbehave:
            raise self.raises
        files = [f for root in ctx.paths for f in Path(root).rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in files)
        if not ctx.dry_run:
            for path in files:
                path.unlink()
        return StrategyResult(
            files_deleted=len(files),
            bytes_deleted=total,
            ok=self.ok,
            skipped_reason=self.skipped_reason,
            dry_run=ctx.dry_run,
            notes=list(self.notes),
        )


def _target(
    target_id: str = "t_fake",
    *,
    paths: tuple[Path, ...] = (),
    risk: Risk = Risk.SAFE,
    admin_required: bool = False,
    standalone: bool = False,
    min_age_hours: int = 0,
    available: bool = True,
    strategy: FakeStrategy | None = None,
) -> Target:
    """A synthetic catalogue row pointing at *paths* and nothing else."""
    return Target(
        id=target_id,
        name_vi=f"Mục {target_id}",
        name_en=f"Target {target_id}",
        desc_vi="Chỉ dùng trong test.",
        desc_en="Test-only row.",
        category=Category.TEMP,
        risk=risk,
        resolver=FakeResolver(paths=tuple(str(p) for p in paths), available=available),
        strategy=strategy or FakeStrategy(),
        admin_required=admin_required,
        min_age_hours=min_age_hours,
        standalone=standalone,
    )


@pytest.fixture
def catalogue(monkeypatch: pytest.MonkeyPatch) -> dict[str, Target]:
    """Replace the catalogue lookup with a dict the test fills.

    Both ``plan`` and ``_clean_one`` call ``find`` in this module's namespace, so
    patching it here is what guarantees a test cannot reach a real cache
    directory even if it names ``npm_cache``.
    """
    rows: dict[str, Target] = {}
    monkeypatch.setattr(cleaner, "find", rows.get)
    return rows


@pytest.fixture
def store() -> PlanStore:
    """A private store, so one test's tokens cannot be seen by another."""
    return PlanStore()


def _as_admin(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Pin elevation, so the admin-gated rows behave the same on any machine."""
    monkeypatch.setattr(cleaner, "is_user_an_admin", lambda: value)


def _register(rows: dict[str, Target], target: Target) -> Target:
    rows[target.id] = target
    return target


# ---------------------------------------------------------------------------
# min_age_for() -- a setting may narrow, never widen
# ---------------------------------------------------------------------------
def test_min_age_takes_the_stricter_of_the_two() -> None:
    """A 24h setting must not lower the catalogue's 72h floor on crash dumps."""
    target = _target(min_age_hours=72)

    assert min_age_for(target, Settings(min_age_hours=24)) == 72
    assert min_age_for(target, Settings(min_age_hours=96)) == 96


def test_min_age_never_goes_negative() -> None:
    """A hand-edited config cannot buy a negative floor, which would mean 'any age'."""
    assert min_age_for(_target(), Settings(min_age_hours=-5)) == 0


# ---------------------------------------------------------------------------
# PlanItem -- what leaves the process
# ---------------------------------------------------------------------------
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


def test_item_is_not_measurable_until_a_dry_run_says_so() -> None:
    """The default is False: a row that never ran cannot claim a measured zero."""
    assert _item().measurable is False


def test_as_dict_ships_a_bounded_sample_not_the_path_list() -> None:
    """Fifty thousand paths must not cross the bridge because a row was expanded."""
    paths = tuple(f"C:\\x\\{i}" for i in range(SAMPLE_PATHS * 4))

    payload = _item(paths=paths).as_dict()

    assert payload["path_count"] == len(paths)
    assert len(payload["sample_paths"]) == SAMPLE_PATHS
    assert payload["sample_paths"] == list(paths[:SAMPLE_PATHS])
    assert "paths" not in payload


def test_as_dict_is_json_shaped() -> None:
    """Tuples become lists; the bridge serialises this straight to JS."""
    payload = _item(locked_by=("chrome.exe",), notes=("a note",)).as_dict()

    assert payload["locked_by"] == ["chrome.exe"]
    assert payload["notes"] == ["a note"]


# ---------------------------------------------------------------------------
# Plan -- the aggregate the user approves
# ---------------------------------------------------------------------------
def _plan(*items: PlanItem, **kwargs: Any) -> Plan:
    return Plan(token="tok", created_at=time.time(), items=items, **kwargs)


def test_a_skipped_rows_estimate_is_not_part_of_the_total() -> None:
    """The headline number is a promise about what will happen, not a sum of rows."""
    made = _plan(
        _item(target_id="runs", est_bytes=1000, will_run=True),
        _item(target_id="skipped", est_bytes=9_000_000, skipped_reason="nope"),
    )

    assert made.est_total == 1000
    assert [i.target_id for i in made.runnable] == ["runs"]


def test_admin_and_irreversible_are_asked_of_runnable_rows_only() -> None:
    """A skipped admin row must not make the UI demand elevation for nothing."""
    made = _plan(
        _item(target_id="a", admin_required=True, reversible=False, skipped_reason="skip"),
        _item(target_id="b", will_run=True),
    )

    assert made.needs_admin is False
    assert made.has_irreversible is False


def test_dangerous_ids_lists_what_would_actually_run() -> None:
    made = _plan(
        _item(target_id="d", risk=Risk.DANGEROUS.value, will_run=True),
        _item(target_id="s", risk=Risk.SAFE.value, will_run=True),
    )

    assert made.dangerous_ids == ("d",)


def test_truncation_counts_every_row_not_just_runnable_ones() -> None:
    """An incomplete estimate is a caveat on the whole preview, however it ended up."""
    made = _plan(_item(target_id="t", truncated=True, skipped_reason="skip"))

    assert made.truncated is True


def test_expiry_is_measured_from_when_the_preview_was_taken() -> None:
    made = _plan(_item())

    assert made.expired() is False
    assert made.expired(made.created_at + PLAN_TTL_S - 1) is False
    assert made.expired(made.created_at + PLAN_TTL_S + 1) is True


def test_plan_payload_carries_the_settings_it_was_measured_under() -> None:
    """Exclusions and the age floor decide what a clean touches, so they ship too."""
    made = _plan(
        _item(target_id="a", will_run=True),
        _item(target_id="b", skipped_reason="skip"),
        settings=Settings(min_age_hours=48, exclusions=("C:\\keep",)),
    )

    payload = made.as_dict()

    assert payload["count"] == 1
    assert payload["skipped"] == 1
    assert payload["target_ids"] == ["a"]
    assert payload["min_age_hours"] == 48
    assert payload["exclusion_count"] == 1
    assert payload["expires_at"] == pytest.approx(made.created_at + PLAN_TTL_S)


# ---------------------------------------------------------------------------
# PlanStore -- single use, TTL bounded, capacity bounded
# ---------------------------------------------------------------------------
def test_a_token_can_be_spent_exactly_once(store: PlanStore) -> None:
    """The second press of a double-clicked confirm button must not clean twice."""
    made = _plan(_item())
    store.put(made)

    assert store.spend(made.token) is made
    with pytest.raises(SpentPlanError):
        store.spend(made.token)


def test_peek_does_not_spend(store: PlanStore) -> None:
    """The UI re-reads a preview while the user reads it; that must stay free."""
    made = _plan(_item())
    store.put(made)

    assert store.peek(made.token) is made
    assert store.spend(made.token) is made


@pytest.mark.parametrize("token", [None, 0, 1.5, "", [], {}, True])
def test_a_token_that_is_not_a_string_is_unknown(store: PlanStore, token: object) -> None:
    """The token arrives from JavaScript, so every non-string is refused by type."""
    with pytest.raises(UnknownPlanError):
        store.spend(token)
    assert store.peek(token) is None


def test_an_expired_token_is_refused_and_dropped(store: PlanStore) -> None:
    """A preview the user walked away from must not stay spendable."""
    stale = Plan(token="old", created_at=time.time() - PLAN_TTL_S - 10, items=(_item(),))
    store.put(stale)

    with pytest.raises(ExpiredPlanError):
        store.spend(stale.token)
    assert store.peek(stale.token) is None


def test_the_store_evicts_the_oldest_rather_than_growing(store: PlanStore) -> None:
    """A UI bug minting a plan per keystroke costs a re-preview, not the process."""
    now = time.time()
    made = [Plan(token=f"t{i}", created_at=now + i, items=(_item(),)) for i in range(12)]
    for one in made:
        store.put(one)

    assert store.peek("t0") is None
    assert store.peek("t11") is not None
    assert len(store._plans) <= store.capacity


def test_plan_store_is_a_process_singleton() -> None:
    assert plan_store() is plan_store()


# ---------------------------------------------------------------------------
# plan() -- input from JavaScript, and the preflight gates
# ---------------------------------------------------------------------------
def _only(made: Plan) -> PlanItem:
    assert len(made.items) == 1
    return made.items[0]


def test_an_unknown_id_is_shown_never_silently_dropped(
    catalogue: dict[str, Target], store: PlanStore
) -> None:
    """A selection that quietly loses a row is how a user stops trusting the preview."""
    made = plan(["no_such_row"], settings=Settings(), store=store)

    item = _only(made)
    assert item.target_id == "no_such_row"
    assert item.skipped_reason == "unknown target id"
    assert item.will_run is False
    assert item.measurable is False


def test_a_repeated_id_is_measured_once(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """Two checkboxes bound to the same row must not double the estimate."""
    write_file(tree / "a.bin", 512)
    _register(catalogue, _target("dup", paths=(tree,)))

    made = plan(["dup", "dup", " dup "], settings=Settings(), store=store)

    assert len(made.items) == 1
    assert made.est_total == 512


@pytest.mark.parametrize("junk", [None, 3, 1.5, True, ["nested"], {"k": "v"}])
def test_a_non_string_in_the_selection_is_dropped(
    catalogue: dict[str, Target], store: PlanStore, junk: object
) -> None:
    """Ids are the only thing accepted from outside, and only as strings (SEC-02)."""
    made = plan([junk], settings=Settings(), store=store)

    assert made.items == ()


def test_a_bare_string_is_one_id_not_a_sequence_of_letters(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """``str`` is itself a Sequence, so ``"abc"`` would otherwise become a, b, c."""
    _register(catalogue, _target("abc", paths=(tree,)))

    made = plan("abc", settings=Settings(), store=store)

    assert [i.target_id for i in made.items] == ["abc"]


def test_a_standalone_row_is_not_part_of_a_bulk_clean(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """``vss_manage`` and the VHDX compactors get their own screen and prechecks."""
    _register(catalogue, _target("alone", paths=(tree,), standalone=True))

    item = _only(plan(["alone"], settings=Settings(), store=store))

    assert item.will_run is False
    assert item.skipped_reason == "handled on its own screen, not in a bulk clean"


def test_a_dangerous_row_needs_an_explicit_confirmation(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """Hibernation and the component store are opt-in per preview, never by default."""
    write_file(tree / "a.bin", 256)
    _register(catalogue, _target("danger", paths=(tree,), risk=Risk.DANGEROUS))

    refused = _only(plan(["danger"], settings=Settings(), store=store))
    allowed = _only(
        plan(["danger"], settings=Settings(), allow_dangerous=True, store=store)
    )

    assert refused.will_run is False
    assert refused.skipped_reason == "dangerous: needs an explicit confirmation"
    assert allowed.will_run is True


def test_an_admin_row_without_elevation_says_so_once(
    catalogue: dict[str, Target],
    store: PlanStore,
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Better one clear reason than a thousand per-file access-denied errors."""
    _as_admin(monkeypatch, False)
    _register(catalogue, _target("needs_admin", paths=(tree,), admin_required=True))

    item = _only(plan(["needs_admin"], settings=Settings(), store=store))

    assert item.will_run is False
    assert item.skipped_reason == "requires Administrator"


def test_an_unavailable_target_reports_the_resolver_s_reason(
    catalogue: dict[str, Target], store: PlanStore
) -> None:
    """"Docker is not installed" is a useful row; a silent zero is not."""
    _register(catalogue, _target("absent", available=False))

    item = _only(plan(["absent"], settings=Settings(), store=store))

    assert item.will_run is False
    assert item.skipped_reason == "not present"


def test_a_dry_run_deletes_nothing(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """The preview is an enumeration. The tree must be byte-identical afterwards."""
    write_file(tree / "a.bin", 2048)
    write_file(tree / "sub" / "b.bin", 1024)
    before = {p: p.stat().st_size for p in sorted(tree.rglob("*")) if p.is_file()}
    _register(catalogue, _target("dry", paths=(tree,)))

    item = _only(plan(["dry"], settings=Settings(), store=store))

    assert {p: p.stat().st_size for p in sorted(tree.rglob("*")) if p.is_file()} == before
    assert item.est_bytes == 3072
    assert item.measurable is True
    assert item.will_run is True


def test_a_row_with_no_bytes_but_a_note_still_runs_and_is_not_measurable(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """A tool command cannot be forecast, so it runs on its note and claims no size.

    This is the ``npm cache clean`` shape: real work to do, no honest number to
    put beside it. Reporting 0 B as if measured is the BUG-12 lie.
    """
    strategy = FakeStrategy(notes=("would run: npm cache clean --force",))
    _register(catalogue, _target("tool", paths=(tree,), strategy=strategy))

    item = _only(plan(["tool"], settings=Settings(), store=store))

    assert item.will_run is True
    assert item.est_bytes == 0
    assert item.measurable is False


def test_an_empty_target_says_nothing_to_remove(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    _register(catalogue, _target("empty", paths=(tree,)))

    item = _only(plan(["empty"], settings=Settings(), store=store))

    assert item.will_run is False
    assert item.skipped_reason == "nothing to remove"


def test_a_resolver_that_raises_is_a_skipped_row_not_a_failed_preview(
    catalogue: dict[str, Target], store: PlanStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One broken resolver must not cost the other forty-nine rows."""
    broken = _target("broken")
    monkeypatch.setattr(
        broken.resolver, "resolve", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    _register(catalogue, broken)

    item = _only(plan(["broken"], settings=Settings(), store=store))

    assert item.will_run is False
    assert item.skipped_reason is not None
    assert "could not locate" in item.skipped_reason


def test_a_user_cancel_stops_the_whole_preview(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """Stop means stop: a cancelled preview propagates rather than half-reporting."""
    _register(
        catalogue,
        _target(
            "cancels",
            paths=(tree,),
            # The flag *and* the raise: a bare raise with no flag is a budget
            # expiry, which is the next test, and truncates rather than stops.
            strategy=FakeStrategy(cancels=True, raises=CancelledError()),
        ),
    )

    with pytest.raises(CancelledError):
        plan(["cancels"], settings=Settings(), budget_s=None, store=store)


def test_an_expired_budget_truncates_the_row_and_keeps_the_rest(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """A slow row comes back marked incomplete; the preview still comes back.

    ``budget_s=0`` makes the token ``expired`` but not ``cancelled``, which is
    exactly the distinction ``_dry_run_item`` is built around.
    """
    write_file(tree / "a.bin", 128)
    _register(
        catalogue,
        _target("slow", paths=(tree,), strategy=FakeStrategy(raises=CancelledError())),
    )
    _register(catalogue, _target("fine", paths=(tree,)))

    made = plan(["slow", "fine"], settings=Settings(), budget_s=0, store=store)

    slow, fine = made.items
    assert slow.truncated is True
    assert made.truncated is True
    assert fine.target_id == "fine"


def test_plan_puts_the_token_in_the_store_it_was_given(
    catalogue: dict[str, Target], store: PlanStore
) -> None:
    made = plan([], settings=Settings(), store=store)

    assert store.peek(made.token) is made
    assert plan_store().peek(made.token) is None


# ---------------------------------------------------------------------------
# run_clean() -- the receipt
# ---------------------------------------------------------------------------
def _run(approved: Plan, *, write: bool = False) -> dict[str, Any]:
    """Clean synchronously. ``job.start()`` first, as ``JobRunner`` would."""
    job = cleaner.new_job()
    job.start()
    return run_clean(approved, job, write=write)


def test_a_clean_deletes_and_the_receipt_is_measured_not_forecast(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """The number shown comes from measuring before and after, never from a counter."""
    write_file(tree / "a.bin", 4096)
    write_file(tree / "sub" / "b.bin", 2048)
    _register(catalogue, _target("real", paths=(tree,)))
    approved = plan(["real"], settings=Settings(), store=store)

    body = _run(approved)

    assert list(tree.rglob("*.bin")) == []
    assert tree.exists(), "the root itself is the one directory a clean may not remove"
    assert body["per_target"]["real"]["before"] == 6144
    assert body["per_target"]["real"]["after"] == 0
    assert body["reclaimed_total"] == 6144
    assert body["state"] == JobState.DONE.value


def test_the_clean_re_resolves_and_never_deletes_the_plan_s_stored_paths(
    catalogue: dict[str, Target], store: PlanStore, tmp_path: Path
) -> None:
    """Paths ten minutes old are evidence for an estimate, never authority to delete.

    The resolver is moved between the preview and the clean. What gets deleted
    must be what the catalogue says *now*, and the stale directory must survive.
    """
    stale = tmp_path / "stale"
    fresh = tmp_path / "fresh"
    write_file(stale / "old.bin", 1024)
    write_file(fresh / "new.bin", 512)
    target = _register(catalogue, _target("moves", paths=(stale,)))
    approved = plan(["moves"], settings=Settings(), store=store)
    assert approved.items[0].paths == (str(stale),)

    target.resolver.paths = (str(fresh),)  # type: ignore[attr-defined]
    _run(approved)

    assert (stale / "old.bin").exists(), "the plan's stale path was deleted"
    assert not (fresh / "new.bin").exists(), "the re-resolved path was not cleaned"


def test_a_row_skipped_at_plan_time_is_not_cleaned(
    catalogue: dict[str, Target], store: PlanStore, tmp_path: Path
) -> None:
    """Only ``runnable`` rows are executed, and the report records why the rest were not."""
    keep = tmp_path / "keep"
    write_file(keep / "a.bin", 256)
    strategy = FakeStrategy()
    _register(catalogue, _target("skipped", paths=(keep,), standalone=True, strategy=strategy))
    approved = plan(["skipped"], settings=Settings(), store=store)

    body = _run(approved)

    assert (keep / "a.bin").exists()
    assert strategy.calls == [], "a skipped row's strategy was executed anyway"
    assert body["selection"] == []
    assert body["notes"]["skipped_at_plan"]["skipped"] == (
        "handled on its own screen, not in a bulk clean"
    )


def test_a_guard_refusal_aborts_that_target_and_the_run_continues(
    catalogue: dict[str, Target], store: PlanStore, tmp_path: Path
) -> None:
    """An escape aborts the target rather than the clean (SPEC 4.7)."""
    bad, good = tmp_path / "bad", tmp_path / "good"
    write_file(bad / "a.bin", 128)
    write_file(good / "b.bin", 256)
    _register(
        catalogue,
        _target(
            "bad",
            paths=(bad,),
            strategy=FakeStrategy(raises=GuardError("escape"), clean_only=True),
        ),
    )
    _register(catalogue, _target("good", paths=(good,)))
    approved = plan(["bad", "good"], settings=Settings(), store=store)

    body = _run(approved)

    assert body["state"] == JobState.DONE.value
    assert "refused by the guard" in body["per_target"]["bad"]["skipped_reason"]
    assert not (good / "b.bin").exists(), "the second target did not run"


def test_an_os_error_is_a_warned_row_not_a_failed_run(
    catalogue: dict[str, Target], store: PlanStore, tmp_path: Path
) -> None:
    """A locked cache is a fact about one target, not a reason to lose the clean."""
    locked, fine = tmp_path / "locked", tmp_path / "fine"
    write_file(locked / "a.bin", 128)
    write_file(fine / "b.bin", 256)
    _register(
        catalogue,
        _target(
            "locked",
            paths=(locked,),
            strategy=FakeStrategy(
                raises=OSError(32, "in use by another process"), clean_only=True
            ),
        ),
    )
    _register(catalogue, _target("fine", paths=(fine,)))
    approved = plan(["locked", "fine"], settings=Settings(), store=store)

    body = _run(approved)

    assert body["state"] == JobState.DONE.value
    assert body["per_target"]["locked"]["skipped_reason"] == "in use by another process"
    assert not (fine / "b.bin").exists()


def test_a_cancel_between_rows_stops_the_clean_and_still_writes_a_report(
    catalogue: dict[str, Target], store: PlanStore, tmp_path: Path, data_dir: Path
) -> None:
    """A clean that deleted files and then stopped must still leave a receipt.

    The first row cancels the job token and returns normally; the loop's
    ``raise_if_cancelled`` then stops before the second row is touched.
    """
    first, second = tmp_path / "first", tmp_path / "second"
    write_file(first / "a.bin", 1024)
    write_file(second / "b.bin", 2048)
    _register(
        catalogue,
        _target("first", paths=(first,), strategy=FakeStrategy(cancels=True, clean_only=True)),
    )
    _register(catalogue, _target("second", paths=(second,)))
    approved = plan(["first", "second"], settings=Settings(), store=store)
    job = cleaner.new_job()
    job.start()

    with pytest.raises(CancelledError):
        run_clean(approved, job)

    assert not (first / "a.bin").exists(), "the first row's work was rolled back"
    assert (second / "b.bin").exists(), "the clean continued past the stop"
    assert job.state is JobState.CANCELLED
    written = list((data_dir / "reports").glob("*.json"))
    assert len(written) == 1, "a cancelled clean left no receipt"


def test_a_cancel_inside_a_row_still_measures_what_that_row_did(
    catalogue: dict[str, Target], store: PlanStore, tmp_path: Path
) -> None:
    """The receipt must say what happened before the stop, not omit the row."""
    root = tmp_path / "half"
    write_file(root / "a.bin", 512)
    _register(
        catalogue,
        _target(
            "half",
            paths=(root,),
            strategy=FakeStrategy(cancels=True, raises=CancelledError(), clean_only=True),
        ),
    )
    approved = plan(["half"], settings=Settings(), store=store)
    job = cleaner.new_job()
    job.start()

    with pytest.raises(CancelledError):
        run_clean(approved, job, write=False)

    row = job.snapshot()["per_target"]["half"]
    assert row["before"] == 512
    assert row["after"] == 512, "nothing was deleted, so the measurement must say so"
    assert row["skipped_reason"] == "cancelled part-way"


def test_a_worker_failure_still_leaves_a_receipt(
    catalogue: dict[str, Target], store: PlanStore, tree: Path, data_dir: Path
) -> None:
    """Two hundred thousand files deleted and then a crash must still be recorded."""
    write_file(tree / "a.bin", 64)
    _register(
        catalogue,
        _target(
            "boom",
            paths=(tree,),
            strategy=FakeStrategy(raises=RuntimeError("boom"), clean_only=True),
        ),
    )
    approved = plan(["boom"], settings=Settings(), store=store)
    job = cleaner.new_job()
    job.start()

    with pytest.raises(RuntimeError):
        run_clean(approved, job)

    assert job.state is JobState.FAILED
    assert len(list((data_dir / "reports").glob("*.json"))) == 1


def test_the_report_records_the_promise_beside_the_outcome(
    catalogue: dict[str, Target], store: PlanStore, tree: Path, data_dir: Path
) -> None:
    """The estimate and the paths are the audit trail: 'which directories did it empty'."""
    write_file(tree / "a.bin", 2048)
    _register(catalogue, _target("audited", paths=(tree,)))
    approved = plan(["audited"], settings=Settings(exclusions=("C:\\keep",)), store=store)

    body = _run(approved, write=True)

    notes = body["notes"]
    assert notes["plan_token"] == approved.token
    assert notes["est_total"] == 2048
    assert notes["exclusions"] == ["C:\\keep"]
    assert notes["per_target_estimate"]["audited"]["paths"] == [str(tree)]
    assert Path(body["report_path"]).is_file()


def test_write_false_builds_the_body_without_touching_the_disk(
    catalogue: dict[str, Target], store: PlanStore, tree: Path, data_dir: Path
) -> None:
    """The bridge hands the summary view the same structure with no round trip."""
    _register(catalogue, _target("nowrite", paths=(tree,)))
    approved = plan(["nowrite"], settings=Settings(), store=store)

    body = _run(approved, write=False)

    assert "report_path" not in body
    assert not (data_dir / "reports").exists()


# ---------------------------------------------------------------------------
# submit_clean() -- a token and nothing else
# ---------------------------------------------------------------------------
_LIVE = (JobState.PENDING, JobState.RUNNING)


def _await(job: Any, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state not in _LIVE:
            return
        time.sleep(0.02)
    raise AssertionError(f"job {job.id} did not finish within {timeout}s")


def test_submit_clean_spends_the_token_and_runs(
    catalogue: dict[str, Target], store: PlanStore, tree: Path, data_dir: Path
) -> None:
    write_file(tree / "a.bin", 1024)
    _register(catalogue, _target("submitted", paths=(tree,)))
    approved = plan(["submitted"], settings=Settings(), store=store)

    job = submit_clean(JobRunner(), token=approved.token, store=store)
    _await(job)

    assert job.state is JobState.DONE
    assert list(tree.rglob("*.bin")) == []
    assert approved.spent_at is not None


def test_a_double_click_on_confirm_cannot_clean_twice(
    catalogue: dict[str, Target], store: PlanStore, tree: Path, data_dir: Path
) -> None:
    """The second submit finds a spent token, and the UI can say exactly that."""
    write_file(tree / "a.bin", 512)
    _register(catalogue, _target("once", paths=(tree,)))
    approved = plan(["once"], settings=Settings(), store=store)
    runner = JobRunner()

    _await(submit_clean(runner, token=approved.token, store=store))
    with pytest.raises(SpentPlanError):
        submit_clean(runner, token=approved.token, store=store)


def test_a_busy_runner_refuses_before_the_token_is_spent(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """A click landing while a scan finishes must not burn the preview.

    Otherwise the user has to re-run the dry run to find out why nothing happened.
    """
    _register(catalogue, _target("queued", paths=(tree,)))
    approved = plan(["queued"], settings=Settings(), store=store)
    runner = JobRunner()
    blocker = cleaner.new_job()
    release = threading.Event()
    runner.submit(blocker, lambda _job: release.wait(10))
    while blocker.state is not JobState.RUNNING:
        time.sleep(0.01)

    try:
        with pytest.raises(RuntimeError, match="still running"):
            submit_clean(runner, token=approved.token, store=store)
        assert approved.spent_at is None, "the token was burned on a job that never ran"
        assert store.peek(approved.token) is approved
    finally:
        release.set()
        _await(blocker)


@pytest.mark.parametrize("bad", [None, "", "deadbeef", 42])
def test_submit_clean_refuses_a_token_it_never_minted(
    store: PlanStore, bad: object
) -> None:
    with pytest.raises(UnknownPlanError):
        submit_clean(JobRunner(), token=bad, store=store)


# ---------------------------------------------------------------------------
# Volume filter -- the tick list is part of the promise (SPEC 6.6)
# ---------------------------------------------------------------------------
def _elsewhere(path: Path) -> str:
    """A drive letter that is certainly not the one *path* is on."""
    return "Y" if volume_letter(path) == "Z" else "Z"


def test_a_plan_skips_a_target_on_a_volume_the_user_unticked(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """The preview says why, and reports no bytes for a disk nobody looked at."""
    write_file(tree / "a.bin", 2048)
    _register(catalogue, _target("off_disk", paths=(tree,)))

    item = _only(
        plan(["off_disk"], settings=Settings(volume_ids=(_elsewhere(tree),)), store=store)
    )

    assert item.will_run is False
    assert item.skipped_reason == f"excluded by the volume filter ({volume_letter(tree)}:)"
    assert item.est_bytes == 0
    assert item.measurable is False
    assert item.paths == ()


def test_a_clean_never_touches_a_volume_the_plan_excluded(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """The safety half: an unticked disk survives the clean byte for byte.

    Asserted against a snapshot and against the strategy's own call list, because
    a strategy that ran and found nothing to do would produce the same totals as
    one that was never reached.
    """
    write_file(tree / "a.bin", 4096)
    write_file(tree / "sub" / "b.bin", 2048)
    before = {p: p.stat().st_size for p in sorted(tree.rglob("*")) if p.is_file()}
    strategy = FakeStrategy()
    _register(catalogue, _target("off_disk", paths=(tree,), strategy=strategy))
    approved = plan(
        ["off_disk"], settings=Settings(volume_ids=(_elsewhere(tree),)), store=store
    )

    body = _run(approved)

    assert {p: p.stat().st_size for p in sorted(tree.rglob("*")) if p.is_file()} == before
    assert strategy.calls == [], "the excluded row's strategy was executed anyway"
    assert body["selection"] == []
    assert body["reclaimed_total"] == 0
    assert body["notes"]["skipped_at_plan"]["off_disk"] == (
        f"excluded by the volume filter ({volume_letter(tree)}:)"
    )


def test_a_plan_keeps_the_ticked_half_of_a_target_that_spans_disks(
    catalogue: dict[str, Target], store: PlanStore, tree: Path
) -> None:
    """One row, one path per volume: the ``RecycleBins`` shape, half of it ticked.

    The excluded path is dropped before the dry run enumerates anything, which is
    why it can name a location that does not exist.
    """
    here = tree / "here"
    write_file(here / "f.bin", 512)
    gone = Path(_elsewhere(tree) + ":\nope")
    _register(catalogue, _target("split", paths=(here, gone)))

    item = _only(
        plan(["split"], settings=Settings(volume_ids=(volume_letter(tree),)), store=store)
    )

    assert item.will_run is True
    assert item.skipped_reason is None
    assert item.paths == (str(here),)
    assert item.est_bytes == 512
