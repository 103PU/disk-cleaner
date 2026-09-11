r"""From a catalogue entry to real paths on *this* machine (docs/02-SPEC.md 4.4).

Two v1 defects live in this file:

* **BUG-02** v1 hardcoded ``%LOCALAPPDATA%\pnpm`` as the pnpm store. The real
  store here is ``E:\.pnpm-store\v11`` -- a different volume -- so v1 measured an
  empty directory and reported 0 B for a store that exists.
* **BUG-03** when a tool was missing, v1 fell back to a guessed path and then
  reported the guess as a finding.

One rule fixes both: **a resolver never invents a path.** Every ``Resolution``
either carries paths that were observed to exist, or is ``unavailable`` with a
reason naming what was missing. There is no third answer.
"""

from __future__ import annotations

import glob as globlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Protocol, runtime_checkable

from .paths import cache_dir, ensure
from .volumes import fixed_volumes, volume_letter

# A console window flashing up every time we ask npm where its cache lives was one
# of v1's visible annoyances. This is the flag that stops it.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Measured here: `npm config get cache` takes ~6 s, `pip cache dir` ~3 s. That is
# why the answer is cached for a day and why the timeout is generous.
TOOL_TIMEOUT_S = 30.0
TOOL_TTL_S = 86_400.0
CACHE_OFF_ENV = "ADC_NO_TOOL_CACHE"

# docs/02-SPEC.md 4.4, exactly. Every Chromium profile multiplies these.
CHROMIUM_CACHE_SUBPATHS: tuple[str, ...] = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    os.path.join("Service Worker", "CacheStorage"),
)

# Only %NAME% is expanded here. os.path.expandvars() would also try $NAME, which
# would eat the literal `$PatchCache$` in one of the SYSTEM targets.
_VAR = re.compile(r"%([A-Za-z_][A-Za-z0-9_()#]*)%")

MAX_REPORTED_PATHS = 64


def expand(spec: str) -> str:
    """``%VAR%``/``~`` expanded and normalised, or ``""`` if a variable is unset.

    The empty string is the "I will not guess" answer. A spec naming a variable
    that does not exist on this machine has no meaning, and substituting nothing
    would turn it into a relative path that could well point at something real.
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        value = os.environ.get(match.group(1))
        if not value:
            missing.append(match.group(1))
            return ""
        return value

    text = _VAR.sub(replace, spec.strip())
    if missing or not text:
        return ""
    if text.startswith("~"):
        text = os.path.expanduser(text)
    return os.path.normpath(text)


def _dedupe(paths: list[str]) -> tuple[str, ...]:
    """Order-preserving, case-insensitive dedupe.

    Two specs can legitimately land on the same directory -- ``AnyOf`` unions
    several of them -- and counting it twice would inflate the estimate.
    """
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return tuple(out)

@dataclass(frozen=True)
class Resolution:
    """What one resolver found. ``available`` is the only field callers may trust.

    ``measurable=False`` marks a target with nothing on disk to walk -- hibernation,
    DISM component cleanup, the Recycle Bin API. The scanner must report those as
    "not measurable" rather than as 0 B, which is the same class of lie as BUG-12.
    """

    paths: tuple[str, ...] = ()
    available: bool = False
    reason: str | None = None
    source: str = ""
    measurable: bool = True
    cached: bool = False

    @classmethod
    def found(cls, paths: list[str], source: str, *, cached: bool = False) -> Resolution:
        return cls(paths=_dedupe(paths), available=True, source=source, cached=cached)

    @classmethod
    def unavailable(cls, reason: str, source: str) -> Resolution:
        return cls(available=False, reason=reason, source=source)

    @classmethod
    def systemic(cls, source: str) -> Resolution:
        """Available, but there is no path to point at."""
        return cls(available=True, source=source, measurable=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "source": self.source,
            "measurable": self.measurable,
            "cached": self.cached,
            "path_count": len(self.paths),
            "paths": list(self.paths[:MAX_REPORTED_PATHS]),
        }


def narrow_to_volumes(resolution: Resolution, volume_ids: Sequence[str]) -> Resolution:
    """Drop the paths that live on a volume the user did not tick.

    An empty *volume_ids* means every volume: that is what a cleared checkbox
    group means on the Settings page, and it is what the default ``()`` has to go
    on meaning. A resolution with no paths comes back untouched -- hibernation,
    DISM and the Recycle Bin API have nothing to attribute to a disk, and making
    them unavailable because C: is unticked would invent a rule the UI never
    offered.

    When every path is filtered out the answer is ``available=False`` rather than
    an empty ``found``: the scanner and the cleaner both already branch on
    ``available``, so the row lands as skipped-with-a-reason instead of as a
    silent 0 B, which is the same class of lie as BUG-12.
    """
    if not volume_ids or not resolution.paths:
        return resolution
    wanted = {letter.strip().upper() for letter in volume_ids}
    kept: list[str] = []
    dropped: set[str] = set()
    for path in resolution.paths:
        letter = volume_letter(path)
        if letter in wanted:
            kept.append(path)
        else:
            dropped.add(letter or "?")
    if not dropped:
        return resolution
    if kept:
        return replace(resolution, paths=tuple(kept))
    where = ", ".join(f"{letter}:" for letter in sorted(dropped))
    return replace(
        resolution,
        paths=(),
        available=False,
        reason=f"excluded by the volume filter ({where})",
    )


@runtime_checkable
class Resolver(Protocol):
    """Structural: anything that can answer ``resolve()`` and name itself."""

    kind: ClassVar[str]

    @property
    def source(self) -> str: ...

    def resolve(self) -> Resolution: ...

@dataclass(frozen=True)
class StaticPath:
    r"""One fixed location, e.g. ``%LOCALAPPDATA%\Temp``."""

    spec: str
    kind: ClassVar[str] = "static"

    @property
    def source(self) -> str:
        return f"static:{self.spec}"

    def resolve(self) -> Resolution:
        path = expand(self.spec)
        if not path:
            return Resolution.unavailable(
                f"environment variable unset in {self.spec}", self.source
            )
        if not os.path.exists(path):
            return Resolution.unavailable(f"not present: {path}", self.source)
        return Resolution.found([path], self.source)


@dataclass(frozen=True)
class GlobPath:
    r"""Several matches, e.g. ``%LOCALAPPDATA%\Packages\*\LocalState\ext4.vhdx``.

    ``glob`` reads ``[`` in the *expanded* prefix as a character class. Escaping the
    fixed half would mean splitting every pattern into literal and wild parts, and
    no directory these patterns reach can contain one, so the prefix is passed
    through as-is and this comment stands in for the guard.
    """

    pattern: str
    dirs_only: bool = False
    kind: ClassVar[str] = "glob"

    @property
    def source(self) -> str:
        return f"glob:{self.pattern}"

    def resolve(self) -> Resolution:
        expanded = expand(self.pattern)
        if not expanded:
            return Resolution.unavailable(
                f"environment variable unset in {self.pattern}", self.source
            )
        matches = sorted(globlib.glob(expanded))
        if self.dirs_only:
            matches = [match for match in matches if os.path.isdir(match)]
        if not matches:
            return Resolution.unavailable(f"no match for {expanded}", self.source)
        return Resolution.found(matches, self.source)

class ToolCache:
    """``argv -> path`` in ``cache/tools.json``, good for 24 h (SPEC 4.4).

    A JSON file rather than the scan SQLite: five entries, written about once a
    day, and being readable by a human debugging a wrong pnpm store is worth more
    than the write speed. The path is recomputed on every access so that a test
    pointing ``ADC_DATA_DIR`` at a tmp tree is honoured even though this object is
    a module-level singleton.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return os.path.join(str(cache_dir()), "tools.json")

    @property
    def enabled(self) -> bool:
        return os.environ.get(CACHE_OFF_ENV, "").strip().lower() not in ("1", "true", "yes")

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def get(self, key: str, ttl_s: float) -> str | None:
        if not self.enabled:
            return None
        with self._lock:
            row = self._read().get(key)
        if not isinstance(row, dict):
            return None
        value, at = row.get("value"), row.get("at")
        if not isinstance(value, str) or not isinstance(at, int | float):
            return None
        if time.time() - float(at) > ttl_s:
            return None
        return value

    def put(self, key: str, value: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            data = self._read()
            data[key] = {"value": value, "at": time.time()}
            try:
                ensure(cache_dir())
                with open(self.path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=1)
            except OSError:
                return  # a cache that cannot be written is not an error

    def clear(self) -> None:
        with self._lock:
            try:
                os.remove(self.path)
            except OSError:
                return


_TOOL_CACHE = ToolCache()


def tool_cache() -> ToolCache:
    """The process-wide tool cache. A function so a test can clear it."""
    return _TOOL_CACHE

@dataclass(frozen=True)
class ToolQuery:
    """Ask the tool itself where its cache lives, and believe only a real path.

    Fail-safe is the contract (SPEC 4.4): not on PATH, a non-zero exit, a timeout,
    or output that is not an absolute path all produce ``unavailable``. None of
    them produces a guess -- that was BUG-03.

    ``argv`` is a fixed tuple from the catalogue, passed to ``subprocess.run`` as a
    list with ``shell=False``. Nothing from the UI, the config file or the
    environment ever reaches it, so there is no argument to quote and no shell to
    interpret it.
    """

    argv: tuple[str, ...]
    timeout_s: float = TOOL_TIMEOUT_S
    ttl_s: float = TOOL_TTL_S
    kind: ClassVar[str] = "tool"

    @property
    def source(self) -> str:
        return "tool:" + " ".join(self.argv)

    def _first_path(self, raw: str) -> str | None:
        """First line usable as an absolute path, or ``None`` for garbage."""
        for line in raw.splitlines():
            candidate = line.strip().strip('"')
            if not candidate or len(candidate) > 4096:
                continue
            if any(bad in candidate for bad in ("\0", "*", "?")):
                continue
            if os.path.isabs(candidate):
                return os.path.normpath(candidate)
        return None

    def resolve(self) -> Resolution:
        # which() gives the full name including the .CMD/.EXE extension, which is
        # what makes an argv list executable without a shell on Windows.
        exe = shutil.which(self.argv[0])
        if exe is None:
            return Resolution.unavailable(f"{self.argv[0]} is not on PATH", self.source)

        key = " ".join(self.argv)
        cached = tool_cache().get(key, self.ttl_s)
        if cached is not None and os.path.exists(cached):
            return Resolution.found([cached], self.source, cached=True)

        try:
            completed = subprocess.run(
                [exe, *self.argv[1:]],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                shell=False,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            return Resolution.unavailable(f"{key} failed: {exc}", self.source)

        if completed.returncode != 0:
            return Resolution.unavailable(f"{key} exited {completed.returncode}", self.source)
        path = self._first_path(completed.stdout or "")
        if path is None:
            return Resolution.unavailable(f"{key} did not print a path", self.source)
        if not os.path.exists(path):
            # The tool named a directory it has not created yet: nothing to clean,
            # and still no reason to substitute a guess.
            return Resolution.unavailable(f"{key} named a missing path: {path}", self.source)

        tool_cache().put(key, path)
        return Resolution.found([path], self.source)

@dataclass(frozen=True)
class ToolPresence:
    """The tool is installed; there is no directory of ours to measure.

    For a target whose entire operation is a command that reports its own reclaim
    -- ``docker system prune`` owns no path we may walk, and the bytes it frees
    live inside a VHDX. ``Systemic`` would be wrong here: it answers *available*
    unconditionally, so a machine with no Docker at all would be offered a Docker
    target. That is BUG-03 in a different coat, hence the ``which()``.
    """

    exe: str
    kind: ClassVar[str] = "presence"

    @property
    def source(self) -> str:
        return f"presence:{self.exe}"

    def resolve(self) -> Resolution:
        if shutil.which(self.exe) is None:
            return Resolution.unavailable(f"{self.exe} is not on PATH", self.source)
        return Resolution.systemic(self.source)


@dataclass(frozen=True)
class ChromiumProfiles:
    r"""``Default`` + ``Profile *`` crossed with the cache subdirectories.

    v1 cleaned ``Default`` only, which on this machine is one profile out of nine --
    the reason it saw a fraction of Chrome's 4.96 GB.
    """

    user_data_spec: str
    subpaths: tuple[str, ...] = CHROMIUM_CACHE_SUBPATHS
    kind: ClassVar[str] = "chromium"

    @property
    def source(self) -> str:
        return f"chromium:{self.user_data_spec}"

    def user_data_dir(self) -> str:
        return expand(self.user_data_spec)

    def profile_dirs(self) -> tuple[str, ...]:
        """Every profile directory, ``Default`` first then ``Profile *`` sorted."""
        root = self.user_data_dir()
        if not root or not os.path.isdir(root):
            return ()
        found: list[str] = []
        try:
            with os.scandir(root) as scanner:
                for entry in scanner:
                    if entry.name != "Default" and not entry.name.startswith("Profile "):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            found.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            return ()
        found.sort(key=lambda path: (os.path.basename(path) != "Default", os.path.basename(path)))
        return tuple(found)

    def resolve(self) -> Resolution:
        root = self.user_data_dir()
        if not root:
            return Resolution.unavailable(
                f"environment variable unset in {self.user_data_spec}", self.source
            )
        profiles = self.profile_dirs()
        if not profiles:
            return Resolution.unavailable(f"no browser profile under {root}", self.source)
        hits = [
            os.path.join(profile, sub)
            for profile in profiles
            for sub in self.subpaths
            if os.path.isdir(os.path.join(profile, sub))
        ]
        if not hits:
            return Resolution.unavailable(
                f"{len(profiles)} profile(s) under {root}, none holding a cache directory",
                self.source,
            )
        return Resolution.found(hits, self.source)

@dataclass(frozen=True)
class RecycleBins:
    r"""``<volume>\$Recycle.Bin`` on every fixed volume.

    v1 looked at ``C:\$Recycle.Bin`` alone, so a file deleted from D: or E: was
    invisible to it. These paths are for *measuring* only; emptying goes through
    ``SHEmptyRecycleBin`` per volume, never a raw delete of the directory.
    """

    kind: ClassVar[str] = "recyclebin"

    @property
    def source(self) -> str:
        return "recyclebin:fixed-volumes"

    def resolve(self) -> Resolution:
        hits = [
            os.path.join(volume.root, "$Recycle.Bin")
            for volume in fixed_volumes()
            if os.path.isdir(os.path.join(volume.root, "$Recycle.Bin"))
        ]
        if not hits:
            return Resolution.unavailable(
                "no Recycle Bin directory on any fixed volume", self.source
            )
        return Resolution.found(hits, self.source)


@dataclass(frozen=True)
class Systemic:
    """A target with no path: hibernation, DISM component cleanup, VSS.

    ``available`` is True because the *operation* exists; ``measurable`` is False
    because there is nothing to walk. The size for these comes from the operation
    itself (DISM reports it, ``powercfg`` implies it), never from a walk.
    """

    label: str
    kind: ClassVar[str] = "systemic"

    @property
    def source(self) -> str:
        return f"system:{self.label}"

    def resolve(self) -> Resolution:
        return Resolution.systemic(self.source)


@dataclass(frozen=True)
class AnyOf:
    """Union of several resolvers; available when at least one part is.

    For a target that legitimately lives in two places: WER reports under both
    ``%LOCALAPPDATA%`` and ``%PROGRAMDATA%``, the two Docker VHDX files, Discord's
    three cache directories.
    """

    parts: tuple[Resolver, ...]
    kind: ClassVar[str] = "anyof"

    @property
    def source(self) -> str:
        return "any(" + ", ".join(part.source for part in self.parts) + ")"

    def resolve(self) -> Resolution:
        found: list[str] = []
        reasons: list[str] = []
        for part in self.parts:
            outcome = part.resolve()
            if outcome.available:
                found.extend(outcome.paths)
            elif outcome.reason:
                reasons.append(outcome.reason)
        if not found:
            return Resolution.unavailable("; ".join(reasons) or "nothing resolved", self.source)
        return Resolution.found(found, self.source)

