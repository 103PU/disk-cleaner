"""Report tests. The load-bearing assertion is that the two totals stay apart.

BUG-07 was v1 reporting the sum of its pre-clean *estimates* as the result, so a
delete that silently failed still showed up as space reclaimed. The v2 report
carries two measured numbers -- per-target before/after, and the volume's actual
free-space change -- and refuses to reconcile them, because when they disagree
the disagreement is the finding.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from adc.engine import report as rp
from adc.engine.jobs import Job, JobKind, JobState, Level


def _finished_job(*, before: int = 1_000_000, after: int = 250_000) -> Job:
    job = Job(JobKind.CLEAN)
    job.start()
    job.log(Level.INFO, "bắt đầu", "starting", target_id="pnpm_cache")
    row = job.outcome("pnpm_cache")
    row.before, row.after, row.files_deleted = before, after, 12
    job.finish(JobState.DONE)
    return job


# ---------------------------------------------------------------------------
# The two numbers
# ---------------------------------------------------------------------------
def test_report_keeps_reclaimed_and_free_delta_apart() -> None:
    """A uv cache hardlinked into a live venv: bytes gone, free space unmoved.

    ``reclaimed_total`` says 750 kB left the cache; ``free_delta_total`` says the
    volume gained 8 kB, because the data survives until the last hardlink does.
    Both are true and the report shows both.
    """
    job = _finished_job()

    body = rp.build_report(
        job,
        free_before={"C:" + os.sep: 1_000},
        free_after={"C:" + os.sep: 9_000},
        selection=["pnpm_cache"],
    )

    assert body["reclaimed_total"] == 750_000
    assert body["free_delta_total"] == 8_000
    assert body["volumes"][0]["free_delta"] == 8_000
    assert body["selection"] == ["pnpm_cache"]


def test_volume_delta_can_be_negative() -> None:
    """Another process writing during the clean is ordinary, not an error."""
    delta = rp.VolumeDelta(root="C:" + os.sep, free_before=5_000, free_after=1_000)

    assert delta.free_delta == -4_000
    assert delta.as_dict()["free_delta"] == -4_000


def test_report_without_measurements_totals_zero() -> None:
    """A scan job has no before/after, and must not invent one."""
    job = Job(JobKind.SCAN)
    job.start()
    job.finish(JobState.DONE)

    body = rp.build_report(job)

    assert body["free_delta_total"] == 0
    assert body["volumes"] == []
    assert body["reclaimed_total"] == 0


def test_snapshot_volumes_returns_free_bytes_per_root() -> None:
    snap = rp.snapshot_volumes()

    assert snap, "no fixed volume found on this host"
    for root, free in snap.items():
        assert isinstance(free, int) and free >= 0
        assert root.endswith(os.sep)


# ---------------------------------------------------------------------------
# Shape and provenance
# ---------------------------------------------------------------------------
def test_report_carries_schema_and_timing() -> None:
    job = _finished_job()

    body = rp.build_report(job, notes={"preset": "safe"})

    assert body["schema"] == rp.SCHEMA_VERSION
    assert body["job_id"] == job.id
    assert body["kind"] == "clean"
    assert body["state"] == "done"
    assert body["duration_s"] is not None and body["duration_s"] >= 0.0
    assert body["cancelled"] is False
    assert body["error"] is None
    assert body["notes"] == {"preset": "safe"}
    assert body["per_target"]["pnpm_cache"]["files_deleted"] == 12
    assert any(e["message_en"] == "starting" for e in body["events"])


def test_report_duration_is_none_for_an_unstarted_job() -> None:
    body = rp.build_report(Job(JobKind.SCAN))

    assert body["duration_s"] is None


# ---------------------------------------------------------------------------
# Writing, reading, listing, pruning
# ---------------------------------------------------------------------------
def test_write_report_round_trips_through_json(tmp_path: Path) -> None:
    """Written as UTF-8 with the Vietnamese text intact, not escaped away."""
    job = _finished_job()
    body = rp.build_report(job)

    path = rp.write_report(body, directory=tmp_path)

    assert path.name == f"{job.id}.json"
    reread = json.loads(path.read_text(encoding="utf-8"))
    assert reread["job_id"] == job.id
    assert "bắt đầu" in path.read_text(encoding="utf-8")
    assert rp.load_report(job.id, directory=tmp_path) == reread


def test_write_report_leaves_no_temporary_file(tmp_path: Path) -> None:
    """The History view lists this directory while a job is running.

    A half-written ``.json`` would show up as a corrupt row, hence tmp +
    ``os.replace``; what this checks is that the tmp name does not linger and
    that nothing hidden is left behind.
    """
    rp.write_report(rp.build_report(_finished_job()), directory=tmp_path)

    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_write_report_sanitises_the_filename(tmp_path: Path) -> None:
    r"""The last gate before a filename does not trust the id it was given.

    ``Job`` generates the id itself, so this can only fire if something upstream
    changes -- which is exactly when a path-traversing job id would matter.
    """
    path = rp.write_report({"job_id": r"../../etc/pas swd", "schema": 1}, directory=tmp_path)

    assert path.parent == tmp_path
    assert path.name == "etcpasswd.json"
    assert ".." not in path.name


def test_write_report_survives_an_empty_job_id(tmp_path: Path) -> None:
    path = rp.write_report({"job_id": "///"}, directory=tmp_path)

    assert path.name == "job.json"


def test_load_report_returns_none_for_missing_or_corrupt(tmp_path: Path) -> None:
    """A truncated report is a missing report, never an exception in the UI."""
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")

    assert rp.load_report("absent", directory=tmp_path) is None
    assert rp.load_report("broken", directory=tmp_path) is None
    assert rp.load_report("list", directory=tmp_path) is None, "a JSON array is not a report"


def test_load_report_refuses_an_id_that_is_not_already_a_filename(tmp_path: Path) -> None:
    """The id is joined onto a directory, so ``..`` must not be a working escape.

    ``reports/../settings.json`` is a real file on a real install -- the settings
    file lives exactly one directory up from the reports directory -- so this is not
    a theoretical join. The file is created here with that name on purpose: if the
    id ever stopped being validated, this is the test that would notice.

    Refused, not sanitised. Stripping ``"../settings"`` down to ``"settings"`` would
    read whatever ``settings.json`` happens to sit *inside* the reports directory,
    which is a different wrong answer rather than a fix.
    """
    (tmp_path / "reports").mkdir()
    rp.write_report({"job_id": "scan-real", "note": "wanted"}, directory=tmp_path / "reports")
    (tmp_path / "settings.json").write_text('{"language": "en"}', encoding="utf-8")
    (tmp_path / "reports" / "settings.json").write_text('{"decoy": true}', encoding="utf-8")

    reports = tmp_path / "reports"
    for attempt in ("../settings", r"..\settings", "/settings", r"C:\Windows\win.ini", ""):
        assert rp.load_report(attempt, directory=reports) is None, attempt
        assert rp.is_safe_job_id(attempt) is False, attempt

    # The escape is closed, not the door: a real id in the same directory still reads.
    body = rp.load_report("scan-real", directory=reports)
    assert body is not None and body["note"] == "wanted"
    assert rp.is_safe_job_id("scan-real") is True


def test_write_and_load_agree_on_the_filename(tmp_path: Path) -> None:
    """Whatever ``write_report`` accepts, ``load_report`` must be able to find again.

    The two functions treat a bad id differently on purpose -- write sanitises, load
    refuses -- and that asymmetry is only safe while every id write *keeps* is one
    load accepts. Both go through ``_safe_stem``, which is what makes that true.
    """
    for raw in ("scan-abc123", "clean_XYZ", "a" * 80):
        path = rp.write_report({"job_id": raw}, directory=tmp_path)
        assert rp.is_safe_job_id(path.stem), path.stem
        assert rp.load_report(path.stem, directory=tmp_path) is not None


def test_list_reports_is_newest_first_and_omits_events(tmp_path: Path) -> None:
    """The History view needs summary rows; 2000 log lines per row would be absurd."""
    for i in range(3):
        job = _finished_job(before=1000 * (i + 1), after=0)
        rp.write_report(rp.build_report(job), directory=tmp_path)
        os.utime(tmp_path / f"{job.id}.json", (time.time() + i, time.time() + i))

    rows = rp.list_reports(directory=tmp_path)

    assert len(rows) == 3
    assert [r["reclaimed_total"] for r in rows] == [3000, 2000, 1000]
    assert "events" not in rows[0]
    assert rows[0]["targets"] == 1
    assert rows[0]["kind"] == "clean"


def test_list_reports_honours_limit_and_skips_junk(tmp_path: Path) -> None:
    for _ in range(4):
        rp.write_report(rp.build_report(_finished_job()), directory=tmp_path)
    (tmp_path / "garbage.json").write_text("nope", encoding="utf-8")

    assert len(rp.list_reports(limit=2, directory=tmp_path)) == 2
    assert len(rp.list_reports(directory=tmp_path)) == 4, "junk file became a row"


def test_list_reports_on_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert rp.list_reports(directory=tmp_path / "never-created") == []


def test_prune_reports_keeps_the_newest(tmp_path: Path) -> None:
    """Deletes only files it wrote, in a directory it owns, by exact glob."""
    kept: list[str] = []
    for i in range(5):
        job = _finished_job()
        rp.write_report(rp.build_report(job), directory=tmp_path)
        os.utime(tmp_path / f"{job.id}.json", (time.time() + i, time.time() + i))
        kept.append(f"{job.id}.json")
    (tmp_path / "keep-me.txt").write_text("not a report", encoding="utf-8")

    removed = rp.prune_reports(keep=2, directory=tmp_path)

    assert removed == 3
    assert sorted(p.name for p in tmp_path.glob("*.json")) == sorted(kept[-2:])
    assert (tmp_path / "keep-me.txt").exists(), "pruned outside its own glob"


def test_prune_reports_on_a_missing_directory_is_zero(tmp_path: Path) -> None:
    assert rp.prune_reports(directory=tmp_path / "never-created") == 0


def test_save_writes_into_the_configured_data_dir(data_dir: Path) -> None:
    """``ADC_DATA_DIR`` is honoured, so tests never touch the real profile."""
    path = rp.save(_finished_job())

    assert data_dir in path.parents
    assert path.parent.name == "reports"
    assert path.is_file()
