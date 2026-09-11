r"""``reports/<job_id>.json`` -- the record of what a run actually did.

Required by docs/02-SPEC.md 4.8, and the answer to the question v1 could not
answer: *did it work?* v1 printed a running total to a console pane that was
gone the moment the window closed, and the total was the sum of pre-clean
estimates, so it stayed cheerfully wrong when a delete silently failed (BUG-07).

A report carries two independent numbers and never reconciles them for you:

* ``reclaimed_total`` -- the sum of per-target ``before - after`` measurements;
* ``free_delta`` -- how much free space the volume actually gained.

They disagree for real reasons, and the disagreement is information. A uv cache
whose files are hardlinked into a live venv reports bytes removed from the cache
while free space barely moves, because the data survives until the last link
goes; that is exactly why the catalogue prefers ``uv cache prune`` over a raw
delete (docs/02-SPEC.md 4.5). Averaging the two numbers, or showing only the
flattering one, would hide that.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jobs import Job
from .paths import ensure, reports_dir
from .volumes import fixed_volumes

SCHEMA_VERSION = 1

# A run is a few KB of JSON, so the cap is about tidiness, not space. Enough
# history for the UI's History view to be useful and for a regression to be
# traceable a couple of weeks back.
KEEP_REPORTS = 100


def _safe_stem(job_id: str) -> str:
    """*job_id* reduced to what may appear in a filename.

    ``Job`` generates ``<kind>-<uuid hex>``, so in practice this strips nothing.
    It exists because a report id crosses two boundaries this module does not
    control -- the JSON body handed to :func:`write_report`, and whatever the UI
    asks :func:`load_report` for -- and a filename is the one place where a stray
    ``..`` or a separator stops being text and starts being a different directory.
    """
    return "".join(c for c in job_id if c.isalnum() or c in "-_")[:64]


def is_safe_job_id(job_id: str) -> bool:
    """True when *job_id* already survives :func:`_safe_stem` unchanged.

    The two callers want opposite things from a bad id, on purpose.
    :func:`write_report` sanitises rather than refusing: the id it gets is
    engine-generated, and renaming a report is better than losing one.
    :func:`load_report` refuses rather than sanitising, because sanitising a
    *lookup* answers a question nobody asked -- ``"../settings"`` would reduce to
    ``settings`` and read the settings file next door.
    """
    return bool(job_id) and _safe_stem(job_id) == job_id


@dataclass(frozen=True)
class VolumeDelta:
    """Free space on one volume, before and after, plus the difference."""

    root: str
    free_before: int
    free_after: int

    @property
    def free_delta(self) -> int:
        """Positive means space was returned. Negative is possible and real:
        another process can write more than the clean removed."""
        return self.free_after - self.free_before

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "free_before": self.free_before,
            "free_after": self.free_after,
            "free_delta": self.free_delta,
        }


def snapshot_volumes() -> dict[str, int]:
    """``{root: free_bytes}`` right now -- call once before and once after."""
    return {v.root: v.free for v in fixed_volumes()}


def _deltas(before: dict[str, int], after: dict[str, int]) -> list[VolumeDelta]:
    roots = sorted(set(before) | set(after))
    return [
        VolumeDelta(root=r, free_before=before.get(r, 0), free_after=after.get(r, 0))
        for r in roots
    ]


def _volume_table() -> list[dict[str, Any]]:
    return [v.as_dict() for v in fixed_volumes()]


def build_report(
    job: Job,
    *,
    free_before: dict[str, int] | None = None,
    free_after: dict[str, int] | None = None,
    selection: list[str] | None = None,
    notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the report body without touching the disk.

    Separate from :func:`write_report` so the bridge can hand the same structure
    straight to the UI's summary view without a round trip through the file.
    """
    snap = job.snapshot()
    before = free_before or {}
    after = free_after or {}
    deltas = _deltas(before, after)
    return {
        "schema": SCHEMA_VERSION,
        "job_id": job.id,
        "kind": job.kind.value,
        "state": snap["state"],
        "started_at": snap["started_at"],
        "finished_at": snap["finished_at"],
        "duration_s": (
            round(snap["finished_at"] - snap["started_at"], 3)
            if snap["started_at"] and snap["finished_at"] else None
        ),
        "cancelled": snap["cancel_requested"],
        "error": snap["error"],
        "selection": list(selection or []),
        # The two independent measurements. See the module docstring.
        "reclaimed_total": snap["reclaimed_total"],
        "free_delta_total": sum(d.free_delta for d in deltas),
        "volumes": [d.as_dict() for d in deltas],
        "volumes_now": _volume_table(),
        "per_target": snap["per_target"],
        "events": snap["events"],
        "notes": dict(notes or {}),
        "written_at": time.time(),
    }


def write_report(body: dict[str, Any], *, directory: Path | None = None) -> Path:
    """Write ``<job_id>.json`` atomically and return its path.

    Atomic because the History view lists this directory while a job is still
    running: a half-written file would show up as a corrupt entry. Write to a
    temporary name in the same directory, then ``Path.replace`` (``os.replace``
    underneath), which is atomic
    on NTFS.
    """
    target_dir = ensure(directory or reports_dir())
    job_id = str(body.get("job_id") or f"job-{int(time.time())}")
    # The id is generated by Job (kind + uuid hex) but this function is the last
    # gate before a filename, so it does not trust it.
    safe = _safe_stem(job_id) or "job"
    final = target_dir / f"{safe}.json"
    tmp = target_dir / f".{safe}.json.tmp"
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(final)
    return final


def save(job: Job, **kwargs: Any) -> Path:
    """Build and write in one call -- what the job runner uses on completion."""
    return write_report(build_report(job, **kwargs))


def load_report(job_id: str, *, directory: Path | None = None) -> dict[str, Any] | None:
    """Read one report back. ``None`` when missing or unreadable.

    An id that is not already filename-safe counts as unreadable: it cannot name
    a file this module wrote (:func:`is_safe_job_id`), and it *can* name one this
    module never wrote, so it gets the same ``None`` a pruned report gets rather
    than a second failure mode for callers to learn. That check is the reason the
    UI can pass an id straight through from the page.
    """
    if not is_safe_job_id(job_id):
        return None
    path = (directory or reports_dir()) / f"{job_id}.json"
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_reports(*, limit: int = 25, directory: Path | None = None) -> list[dict[str, Any]]:
    """Newest first, one summary row per run, for the History view.

    Deliberately does not load the ``events`` array: a cancelled walk over a
    million files can carry 2000 log lines and the list view needs none of them.
    """
    target_dir = directory or reports_dir()
    if not target_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(target_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        body = load_report(path.stem, directory=target_dir)
        if body is None:
            continue
        rows.append({
            "job_id": body.get("job_id"),
            "kind": body.get("kind"),
            "state": body.get("state"),
            "finished_at": body.get("finished_at"),
            "duration_s": body.get("duration_s"),
            "reclaimed_total": body.get("reclaimed_total", 0),
            "free_delta_total": body.get("free_delta_total", 0),
            "targets": len(body.get("per_target") or {}),
        })
        if len(rows) >= limit:
            break
    return rows


def prune_reports(*, keep: int = KEEP_REPORTS, directory: Path | None = None) -> int:
    """Delete the oldest reports beyond *keep*. Returns how many went.

    The only place in the engine that deletes without going through ``guard``,
    and it is confined to files this module wrote, in a directory it owns, by
    exact glob. Anything wider belongs in the cleaner, behind the guard.
    """
    target_dir = directory or reports_dir()
    if not target_dir.is_dir():
        return 0
    files = sorted(target_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for path in files[keep:]:
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed
