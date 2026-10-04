"""Unit tests for All-Drive Auto Dev Sweeper (Phase 3 v2.6.0)."""

from __future__ import annotations

from pathlib import Path

from adc.engine import sweeper
from adc.engine.jobs import JobRunner


def test_discover_project_roots_returns_list() -> None:
    roots = sweeper.discover_project_roots()
    assert isinstance(roots, list)
    for r in roots:
        assert isinstance(r, str)
        assert Path(r).is_dir()


def test_submit_sweep_with_all_roots(tmp_path: Path, monkeypatch) -> None:
    r1 = tmp_path / "dev1"
    r2 = tmp_path / "dev2"
    r1.mkdir()
    r2.mkdir()

    # Create dummy projects
    p1 = r1 / "proj1"
    p1.mkdir()
    (p1 / "package.json").write_text("{}", encoding="utf-8")
    (p1 / "node_modules").mkdir()
    (p1 / "node_modules" / "test.js").write_text("console.log()", encoding="utf-8")

    p2 = r2 / "proj2"
    p2.mkdir()
    (p2 / "Cargo.toml").write_text("[package]", encoding="utf-8")
    (p2 / "target").mkdir()
    (p2 / "target" / "bin").write_text("binary", encoding="utf-8")

    monkeypatch.setattr(sweeper, "discover_project_roots", lambda: [str(r1), str(r2)])

    runner = JobRunner()
    run = sweeper.submit_sweep(runner, root="ALL", min_age_days=0)
    assert run.root == "ALL"
    assert set(run.roots) == {str(r1), str(r2)}

    # Wait for job to complete
    import time

    from adc.engine.jobs import JobState
    deadline = time.monotonic() + 5.0
    while run.job.state not in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert run.job.state == JobState.DONE

    findings = run.findings()
    assert len(findings) == 2
    names = {f.name for f in findings}
    assert "node_modules" in names
    assert "target" in names
