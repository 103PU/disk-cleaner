r"""User settings in ``%APPDATA%\DiskCleanUp\config.json`` (SPEC 3.2).


Three rules shape this module.

**A damaged config never bricks the app.** Every field is validated on read and a
bad value is replaced by the default, not raised. The file is the least trusted
input the engine has: a half-written JSON from a power cut, a hand edit, an older
schema. ``load()`` therefore has no failure mode -- worst case it returns
:data:`DEFAULTS`.

**Settings can only ever narrow what gets deleted.** ``exclusions`` is the single
place where a path from outside the engine is accepted, and it is safe precisely
because :class:`~adc.engine.guard.Guard` uses it to *refuse* paths, never to
reach one. Nothing else in here widens a root, adds a target, or names a
directory to delete.

**No secrets, ever.** This file is written in the clear and shows up in a report
attachment, so nothing that looks like a credential belongs in it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Final

from .paths import config_file, ensure, roaming_dir
from .targets import PRESETS

SCHEMA_VERSION: Final = 1

LANGUAGES: Final[tuple[str, ...]] = ("vi", "en")

# A user with more than this many exclusions has a different problem, and an
# unbounded list is a way to make every guard check slow.
MAX_EXCLUSIONS: Final = 64
MAX_PATH_LEN: Final = 4096

# Hours. The upper bound is a year: past that the field is being misused.
MAX_MIN_AGE_HOURS: Final = 8_760

# Seconds. Zero means "no budget"; the walker takes ``None`` for that.
MAX_SCAN_BUDGET_S: Final = 3_600.0


def _as_bool(raw: object, fallback: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return fallback


def _as_int(raw: object, fallback: int, *, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is reachable: Python's json accepts the non-standard
        # ``Infinity`` token, and int(inf) raises before the clamp below can run.
        # A hand-edited config must not be able to break load()'s no-failure rule.
        return fallback
    return max(low, min(high, value))


def _as_choice(raw: object, fallback: str, allowed: tuple[str, ...]) -> str:
    if isinstance(raw, str) and raw in allowed:
        return raw
    return fallback


def _as_budget(raw: object, fallback: float | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if value <= 0:
        return None
    return min(MAX_SCAN_BUDGET_S, value)


def clean_exclusions(raw: object) -> tuple[str, ...]:
    """Absolute, wildcard-free, deduped, bounded. Anything else is dropped.

    Silently: an exclusion that cannot be honoured is not worth a modal, and the
    settings view shows the list back to the user, so a dropped entry is visible
    where it matters. A relative path is dropped rather than resolved, because
    resolving it against ADC's own working directory would mean something the
    user did not ask for.
    """
    if not isinstance(raw, list | tuple):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip().strip('"')
        if not text or len(text) > MAX_PATH_LEN:
            continue
        if "*" in text or "?" in text or "\0" in text:
            continue
        if not os.path.isabs(text):
            continue
        normalised = os.path.normpath(text)
        key = os.path.normcase(normalised)
        if key in seen:
            continue
        seen.add(key)
        out.append(normalised)
        if len(out) >= MAX_EXCLUSIONS:
            break
    return tuple(out)


def _clean_volume_ids(raw: object) -> tuple[str, ...]:
    """Drive letters, not paths -- ``("C", "E")``.

    The UI names a volume by its letter and the engine maps that to
    ``Volume.root`` itself. That is the whole point: an identifier from outside
    the engine that cannot be a path is an identifier that cannot be traversed
    (SEC-02).
    """
    if not isinstance(raw, list | tuple):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        letter = item.strip().rstrip(":\\/").upper()
        if len(letter) != 1 or not ("A" <= letter <= "Z") or letter in seen:
            continue
        seen.add(letter)
        out.append(letter)
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    """One validated snapshot of the config file.

    Frozen so that a caller holding a ``Settings`` cannot be surprised by a
    concurrent ``settings_set`` from the UI thread; :meth:`patched` returns a new
    one instead.
    """

    language: str = "vi"
    preset: str = "safe"
    exclusions: tuple[str, ...] = ()
    size_on_disk: bool = True
    min_age_hours: int = 0
    scan_budget_s: float | None = None
    confirm_dangerous: bool = True
    restore_point_before_dangerous: bool = True
    volume_ids: tuple[str, ...] = ()

    FIELDS: ClassVar[tuple[str, ...]] = (
        "language",
        "preset",
        "exclusions",
        "size_on_disk",
        "min_age_hours",
        "scan_budget_s",
        "confirm_dangerous",
        "restore_point_before_dangerous",
        "volume_ids",
    )

    def as_dict(self) -> dict[str, Any]:
        """JSON-shaped, and this is exactly what gets written to the file."""
        return {
            "schema_version": SCHEMA_VERSION,
            "language": self.language,
            "preset": self.preset,
            "exclusions": list(self.exclusions),
            "size_on_disk": self.size_on_disk,
            "min_age_hours": self.min_age_hours,
            "scan_budget_s": self.scan_budget_s,
            "confirm_dangerous": self.confirm_dangerous,
            "restore_point_before_dangerous": self.restore_point_before_dangerous,
            "volume_ids": list(self.volume_ids),
        }

    @classmethod
    def from_dict(cls, raw: object) -> Settings:
        """Every field validated independently, so one bad key costs one field."""
        if not isinstance(raw, Mapping):
            return cls()
        blank = cls()
        return cls(
            language=_as_choice(raw.get("language"), blank.language, LANGUAGES),
            preset=_as_choice(raw.get("preset"), blank.preset, tuple(PRESETS)),
            exclusions=clean_exclusions(raw.get("exclusions")),
            size_on_disk=_as_bool(raw.get("size_on_disk"), blank.size_on_disk),
            min_age_hours=_as_int(
                raw.get("min_age_hours"), blank.min_age_hours, low=0, high=MAX_MIN_AGE_HOURS
            ),
            scan_budget_s=_as_budget(raw.get("scan_budget_s"), blank.scan_budget_s),
            confirm_dangerous=_as_bool(
                raw.get("confirm_dangerous"), blank.confirm_dangerous
            ),
            restore_point_before_dangerous=_as_bool(
                raw.get("restore_point_before_dangerous"),
                blank.restore_point_before_dangerous,
            ),
            volume_ids=_clean_volume_ids(raw.get("volume_ids")),
        )

    def patched(self, changes: object) -> Settings:
        """A new ``Settings`` with only the recognised keys of *changes* applied.

        The bridge hands this whatever JavaScript sent, so an unknown key is
        ignored rather than an error: a newer UI talking to an older engine
        should degrade, not fail. Validation is the same code path as
        :meth:`from_dict`, because a value arriving from the UI deserves no more
        trust than one read off the disk.
        """
        if not isinstance(changes, Mapping):
            return self
        merged = self.as_dict()
        for key, value in changes.items():
            if key in self.FIELDS:
                merged[key] = value
        return replace(Settings.from_dict(merged))


DEFAULTS: Final = Settings()


def load() -> Settings:
    """The config file, or :data:`DEFAULTS`. Never raises.

    A first run has no file; a corrupt one is indistinguishable from that as far
    as the user's next click is concerned. Both answers are the defaults.
    """
    try:
        with open(config_file(), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return DEFAULTS
    return Settings.from_dict(raw)


def save(settings: Settings) -> bool:
    """Write atomically. ``False`` when the profile is not writable.

    Atomic because the alternative is a truncated file: ADC can be killed mid
    write by a reboot the user started from the cleanup screen itself. The temp
    name sits in the same directory so ``Path.replace`` stays on one volume and
    therefore stays atomic on NTFS -- the same idiom as
    :func:`adc.engine.report.write_report`.
    """
    target = config_file()
    tmp = target.parent / ".config.json.tmp"
    try:
        ensure(roaming_dir())
        tmp.write_text(
            json.dumps(settings.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def update(changes: object) -> Settings:
    """Read, validate-and-merge, write back, return what is now in force.

    The read is deliberately inside this call rather than cached: the config file
    is small, and a settings write is a human-speed event. Losing a concurrent
    change matters more than the microseconds.
    """
    merged = load().patched(changes)
    save(merged)
    return merged
