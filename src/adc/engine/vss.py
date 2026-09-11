r"""Shadow copies, and the storage limit that decides whether they survive.

``vss_manage`` is the one row in the catalogue that refuses to be a checkbox
(docs/02-SPEC.md 5). v1 made it one: its "Deep Preset" ran
``vssadmin delete shadows /all /quiet`` and then pinned the store at 2 GB
(docs/01-AUDIT.md BUG-09). On this machine that left System Restore alive in name
and useless in fact -- one checkpoint fills the whole 2 GB, so the next one
evicts it -- and nothing in v1 could put the limit back.

So this module exposes three operations, separately, each with its own answer:

* :func:`state` -- read only. Which volumes have a shadow store, how large it is
  allowed to grow, how much is in use, and which copies exist right now.
* :func:`delete` -- destroy shadow copies. Irreversible, and it takes every
  restore point on that volume with it.
* :func:`resize` -- move the ceiling. This is the way back from BUG-09, and the
  only one of the three that can hand the user something rather than take it.

All three shell out to ``vssadmin``, all three need Administrator, and every
command goes through an injected *runner*, so the tests drive real captured
output without a real Volume Shadow Copy service anywhere near them.

Neither mutation can be reached in one call. :func:`plan_resize` and
:func:`plan_delete` mint a single-use, TTL-bounded receipt holding the exact argv
they previewed, and :meth:`PendingAction.run` will only execute that argv --
the same shape as the clean plan token (docs/02-SPEC.md 6.1), for the same
reason: what the user reads in the dialog has to be what runs.

Parsing note: ``vssadmin`` localises its labels but not its layout. Inside one
association block the three figures always arrive in the order used, allocated,
maximum, so they are read by position and by numeric shape instead of by
matching English words that a Vietnamese Windows would never print. The figures
themselves arrive already rounded to two decimals, which is why
:attr:`Storage.approximate` is True and the UI must not present them as exact.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from . import platform_win as win

# A runner takes an argv -- never a shell string -- and returns the exit code and
# the combined output. ``None`` for the code means the process never finished:
# killed, timed out, or never started. Injected everywhere so tests stay offline.
Runner = Callable[[Sequence[str]], "tuple[int | None, str]"]

CREATE_NO_WINDOW: Final = 0x08000000 if sys.platform == "win32" else 0
LIST_TIMEOUT_S: Final = 60.0
RESIZE_TIMEOUT_S: Final = 120.0
# ``delete shadows /all`` walks every copy on the volume and can sit for a while
# on a store that was allowed to grow; it is still one command, not a job.
DELETE_TIMEOUT_S: Final = 600.0

# vssadmin's own documented floor for /maxsize. Anything smaller is refused by
# the tool, so it is refused here too -- with a sentence instead of exit code 1.
MIN_MAXSIZE_BYTES: Final = 320 * 1024 * 1024

_GUID = re.compile(r"\{[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}")
_LETTER = re.compile(r"\(([A-Za-z]):\)")
# "1.93 GB (1%)", "2,00 Go (1%)", "UNBOUNDED (100%)" -- the number and its unit,
# with the percentage deliberately ignored: it is a rounding of a rounding.
#
# The unit word has to be spelled out as well as abbreviated. Under about a
# megabyte vssadmin stops abbreviating and prints "0 bytes", which is the state
# this machine is in between checkpoints; a pattern that only matched "B" as the
# last letter of the token read that line as no size at all, dropped the whole
# association, and reported the most ordinary state a machine can be in as
# unparseable output.
_SIZE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*([KMGTP]?)(?:B(?:ytes?)?|o(?:ctets?)?)\b", re.IGNORECASE
)
_UNBOUNDED = re.compile(r"UNBOUNDED|UNLIMITED", re.IGNORECASE)
_SCALE: Final = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}


class VssUnavailableError(RuntimeError):
    """``vssadmin`` is not something this machine can be asked. Not a failure."""


@dataclass(frozen=True)
class Limit:
    r"""What to hand ``/maxsize``: a percentage, a byte count, or no ceiling.

    A value object rather than a string because the two interesting values --
    ``10%`` and ``UNBOUNDED`` -- are the SPEC's own wording for the way back from
    BUG-09, and because ``/maxsize`` silently means different things for ``10``
    (bytes) and ``10%``. Validation happens here, once, so neither the bridge nor
    the UI has to know vssadmin's floor.
    """

    kind: str            # "percent" | "bytes" | "unbounded"
    value: int = 0       # percent 1..100, or bytes; ignored when unbounded
    KINDS: ClassVar[tuple[str, ...]] = ("percent", "bytes", "unbounded")

    def __post_init__(self) -> None:
        if self.kind not in self.KINDS:
            raise ValueError(f"unknown limit kind: {self.kind!r}")
        if self.kind == "percent" and not 1 <= self.value <= 100:
            raise ValueError(f"a percentage limit must be 1..100, not {self.value}")
        if self.kind == "bytes" and self.value < MIN_MAXSIZE_BYTES:
            raise ValueError(
                f"vssadmin refuses a maximum below {MIN_MAXSIZE_BYTES} bytes (320 MB)"
            )

    @property
    def arg(self) -> str:
        """The literal ``/maxsize`` value. ``UNBOUNDED`` is vssadmin's own word."""
        if self.kind == "unbounded":
            return "UNBOUNDED"
        if self.kind == "percent":
            return f"{self.value}%"
        return str(self.value)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value, "arg": self.arg}

    @classmethod
    def parse(cls, raw: object) -> Limit:
        """``"10%"``, ``"unbounded"``, ``"2147483648"`` -- what the page can send.

        The page sends one of these three shapes and nothing else; a shape this
        does not recognise raises rather than guessing, because guessing here
        would guess at a system-wide storage ceiling.
        """
        if isinstance(raw, Limit):
            return raw
        text = str(raw or "").strip()
        if not text:
            raise ValueError("no limit given")
        if _UNBOUNDED.fullmatch(text):
            return cls("unbounded")
        if text.endswith("%"):
            head = text[:-1].strip()
            if not head.isdigit():
                raise ValueError(f"not a percentage: {text!r}")
            return cls("percent", int(head))
        if text.isdigit():
            return cls("bytes", int(text))
        raise ValueError(f"not a storage limit: {text!r}")


PERCENT_10: Final = Limit("percent", 10)
UNBOUNDED: Final = Limit("unbounded")


@dataclass(frozen=True)
class ShadowCopy:
    """One shadow copy, as much of it as can be read without guessing at labels.

    ``created`` is kept as the string the service printed. Reformatting it would
    mean parsing a localised date, and a restore point shown under the wrong day
    is worse than one shown in Windows' own wording.
    """

    copy_id: str
    set_id: str
    letter: str          # "C", or "" when the block did not name a volume
    created: str
    provider: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "copy_id": self.copy_id,
            "set_id": self.set_id,
            "letter": self.letter,
            "created": self.created,
            "provider": self.provider,
        }


@dataclass(frozen=True)
class Storage:
    """One volume's shadow-copy storage association.

    ``maximum`` is ``None`` exactly when the store is unbounded, which is a
    different fact from "0 bytes" and has to survive the trip to the UI as one:
    the whole point of this screen is the difference between a ceiling and no
    ceiling. ``diff_letter`` is where the copies actually live, and it is what
    ``/on=`` needs -- usually the same volume, not always.
    """

    letter: str
    diff_letter: str
    used: int | None
    allocated: int | None
    maximum: int | None
    unbounded: bool
    approximate: ClassVar[bool] = True

    @property
    def volume(self) -> str:
        return f"{self.letter}:" if self.letter else ""

    @property
    def diff_volume(self) -> str:
        return f"{self.diff_letter}:" if self.diff_letter else self.volume

    def as_dict(self) -> dict[str, Any]:
        return {
            "letter": self.letter,
            "diff_letter": self.diff_letter,
            "volume": self.volume,
            "diff_volume": self.diff_volume,
            "used": self.used,
            "allocated": self.allocated,
            "maximum": self.maximum,
            "unbounded": self.unbounded,
            "approximate": self.approximate,
        }


def target_bytes(limit: Limit, volume_total: int) -> int | None:
    """What *limit* works out to on a volume of *volume_total* bytes.

    ``None`` for unbounded, and for a percentage of a volume whose size is not
    known -- a caller that cannot size the disk must not be handed a number it
    would then present as the new ceiling.
    """
    if limit.kind == "unbounded":
        return None
    if limit.kind == "bytes":
        return limit.value
    if volume_total <= 0:
        return None
    return int(volume_total * limit.value / 100)


def would_shrink(storage: Storage | None, limit: Limit, volume_total: int = 0) -> bool:
    """True when applying *limit* lowers the ceiling, so copies may be evicted.

    Shrinking the store is how BUG-09 destroyed the restore points in the first
    place: Windows drops copies immediately to fit the new maximum, without
    asking. The UI needs to know before it asks, which is why this is a function
    and not something the confirmation dialog infers.
    """
    if storage is None or limit.kind == "unbounded":
        return False
    if storage.unbounded or storage.maximum is None:
        return True                     # anything at all is lower than no ceiling
    wanted = target_bytes(limit, volume_total)
    return wanted is not None and wanted < storage.maximum


@dataclass(frozen=True)
class VssState:
    """Everything the screen needs to draw itself, including why it cannot.

    ``supported`` and ``is_admin`` are separate refusals: a non-Windows box has no
    VSS at all, while an unelevated one has VSS and no permission to look. The UI
    says something different for each, and "no data" would say neither.
    """

    supported: bool
    is_admin: bool
    storages: tuple[Storage, ...] = ()
    copies: tuple[ShadowCopy, ...] = ()
    error: str | None = None

    def storage_for(self, letter: str) -> Storage | None:
        wanted = (letter or "").strip(":\\/").upper()
        for storage in self.storages:
            if storage.letter.upper() == wanted:
                return storage
        return None

    def copies_for(self, letter: str) -> tuple[ShadowCopy, ...]:
        wanted = (letter or "").strip(":\\/").upper()
        return tuple(copy for copy in self.copies if copy.letter.upper() == wanted)

    def as_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "is_admin": self.is_admin,
            "storages": [storage.as_dict() for storage in self.storages],
            "copies": [copy.as_dict() for copy in self.copies],
            "error": self.error,
        }


@dataclass(frozen=True)
class ActionResult:
    """The outcome of one ``vssadmin`` mutation, or of refusing to run it.

    ``argv`` is carried so the report and the log can say exactly what ran --
    these are the two commands v1 ran silently, and a user who has just lost every
    restore point deserves to read the command that did it. Nothing here is
    sensitive: a drive letter and a size.
    """

    action: str                     # "resize" | "delete"
    ok: bool
    dry_run: bool = False
    argv: tuple[str, ...] = ()
    code: int | None = None
    lines: tuple[str, ...] = ()
    reason: str | None = None       # why it did not run, when ok is False

    @property
    def printable(self) -> str:
        return " ".join(self.argv)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "argv": list(self.argv),
            "command": self.printable,
            "code": self.code,
            "lines": list(self.lines),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Parsing -- by layout and numeric shape, never by an English label
# ---------------------------------------------------------------------------
def _to_bytes(token: str) -> int | None:
    """``"1.93 GB"`` -> 2072321720, ``"0 bytes"`` -> 0. ``None`` when not a size."""
    match = _SIZE.search(token)
    if match is None:
        return None
    number = match.group(1).replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return None
    return int(value * _SCALE[match.group(2).upper()])


def _sizes_in(line: str) -> list[int | None]:
    """Every size on one line, with ``None`` standing in for UNBOUNDED."""
    if _UNBOUNDED.search(line):
        return [None]
    found = _to_bytes(line)
    return [found] if found is not None else []


def parse_storages(text: str) -> tuple[tuple[Storage, ...], str | None]:
    r"""Read ``vssadmin list shadowstorage`` output. Returns rows and a complaint.

    One association is one blank-line-separated block holding two ``(X:)`` volumes
    -- the protected one, then the one the copies live on -- and three figures in
    the order used, allocated, maximum.

    Sizes are only read from lines carrying neither a volume nor a GUID. That is
    not fussiness: a GUID such as ``{...-b800-...}`` contains substrings that look
    exactly like a byte count, and a stray 2-byte match would shift all three
    figures one place to the left.
    """
    rows: list[Storage] = []
    unread = 0
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        letters = _LETTER.findall(block)
        if len(letters) < 2:
            continue
        sizes: list[int | None] = []
        for line in block.splitlines():
            if _LETTER.search(line) or _GUID.search(line):
                continue
            sizes.extend(_sizes_in(line))
        if len(sizes) < 3:
            unread += 1
            continue
        used, allocated, maximum = sizes[0], sizes[1], sizes[2]
        rows.append(Storage(
            letter=letters[0].upper(),
            diff_letter=letters[1].upper(),
            used=used,
            allocated=allocated,
            maximum=maximum,
            unbounded=maximum is None,
        ))
    complaint = f"{unread} storage block(s) could not be read" if unread else None
    return tuple(rows), complaint


def parse_copies(text: str) -> tuple[ShadowCopy, ...]:
    r"""Read ``vssadmin list shadows`` output by indentation, not by label.

    The tool nests: the set at the margin, its creation time one step in, each
    copy's id two steps in, and that copy's details three. Those steps survive
    translation, which the words around them do not, so the shape is what gets
    read -- id and set from their GUIDs, the volume from its ``(X:)``, the
    provider from its quotes, the creation time as the service's own text.
    """
    copies: list[ShadowCopy] = []
    set_id = created = copy_id = letter = provider = ""

    def flush() -> None:
        nonlocal copy_id, letter, provider
        if copy_id:
            copies.append(ShadowCopy(
                copy_id=copy_id, set_id=set_id, letter=letter,
                created=created, provider=provider,
            ))
        copy_id = letter = provider = ""

    for raw in text.replace("\r\n", "\n").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        guid = _GUID.search(line)
        if indent == 0:
            flush()
            if guid:
                set_id, created = guid.group(0), ""
            continue
        if indent <= 4:
            if not guid and any(char.isdigit() for char in line):
                created = line.split(":", 1)[1].strip() if ":" in line else line.strip()
            continue
        if indent <= 8 and guid:
            flush()
            copy_id = guid.group(0)
            continue
        found = _LETTER.search(line)
        if found and not letter:
            letter = found.group(1).upper()
        quoted = re.search(r"'([^']+)'", line)
        if quoted and not provider:
            provider = quoted.group(1)
    flush()
    return tuple(copies)


# ---------------------------------------------------------------------------
# Running the tool
# ---------------------------------------------------------------------------
def _vssadmin() -> str:
    """The tool's path, or a refusal. Never assembled from a shell string."""
    if not win.IS_WINDOWS:
        raise VssUnavailableError("shadow copies are a Windows feature")
    found = shutil.which("vssadmin")
    if found is None:
        raise VssUnavailableError("vssadmin is not on PATH")
    return found


def _default_run(argv: Sequence[str], timeout_s: float) -> tuple[int | None, str]:
    """One command, no shell, no console window, and a deadline.

    ``None`` for the code means it never finished, and callers treat that as
    failure -- a resize that was killed half way through has an unknown ceiling,
    which must not be reported as the requested one.
    """
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            shell=False,
            timeout=timeout_s,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout_s:.0f}s"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _run(argv: Sequence[str], *, runner: Runner | None, timeout_s: float) -> tuple[int | None, str]:
    if runner is not None:
        return runner(argv)
    return _default_run(argv, timeout_s)


def _tail(text: str, limit: int = 4) -> tuple[str, ...]:
    """The last few non-empty lines: where vssadmin puts its verdict."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return tuple(lines[-limit:])


def _letter_of(volume: object) -> str:
    r"""``"c"``, ``"C:"``, ``"C:\\"`` -> ``"C"``. Anything else raises.

    A drive letter is the only volume identifier this module accepts, for the same
    reason the bridge accepts no paths: ``/for=`` takes a volume spec, and a
    caller-supplied spec is a caller-supplied command argument.
    """
    text = str(volume or "").strip().strip("\\/")
    if text.endswith(":"):
        text = text[:-1]
    if len(text) != 1 or not text.isalpha():
        raise ValueError(f"not a drive letter: {volume!r}")
    return text.upper()


# ---------------------------------------------------------------------------
# The three operations
# ---------------------------------------------------------------------------
def state(*, runner: Runner | None = None) -> VssState:
    """Read the shadow-copy state of every volume that has one.

    Reads twice because vssadmin answers two different questions, and keeps going
    if the second one fails: knowing the ceiling with no list of copies is still
    worth drawing, and it is the ceiling this screen exists to change.
    """
    try:
        exe = _vssadmin()
    except VssUnavailableError as exc:
        return VssState(supported=False, is_admin=False, error=str(exc))
    admin = win.is_user_an_admin()
    if not admin:
        # vssadmin refuses to list for a standard user, so there is nothing to
        # parse -- and an empty screen with no explanation is the v1 mistake.
        return VssState(supported=True, is_admin=False,
                        error="listing shadow copies requires Administrator")

    code, out = _run([exe, "list", "shadowstorage"], runner=runner, timeout_s=LIST_TIMEOUT_S)
    storages, complaint = parse_storages(out)
    problems = [complaint] if complaint else []
    if code != 0 and not storages:
        problems.append("; ".join(_tail(out)) or f"vssadmin exited {code}")

    copies: tuple[ShadowCopy, ...] = ()
    code2, out2 = _run([exe, "list", "shadows"], runner=runner, timeout_s=LIST_TIMEOUT_S)
    if not _looks_empty(out2):
        copies = parse_copies(out2)
    elif code2 not in (0, 1):
        problems.append("; ".join(_tail(out2)) or f"vssadmin exited {code2}")

    return VssState(
        supported=True, is_admin=True, storages=storages, copies=copies,
        error="; ".join(problems) or None,
    )


def _looks_empty(text: str) -> bool:
    """No GUID anywhere means the tool listed nothing, which is not an error.

    ``list shadows`` exits 1 for "there are no shadow copies", the most ordinary
    state a machine can be in; a red banner over that would be a lie, so the
    presence of a copy id -- not the exit code -- is what decides.
    """
    return _GUID.search(text) is None


def resize(
    volume: object = "C",
    limit: object = PERCENT_10,
    *,
    on: object = None,
    runner: Runner | None = None,
    dry_run: bool = False,
) -> ActionResult:
    r"""Move the ceiling on *volume*'s shadow store. The way back from BUG-09.

    *on* is the volume the copies are stored on and defaults to *volume*, which is
    the normal arrangement; pass it when :attr:`Storage.diff_letter` says the store
    lives elsewhere, because ``/on=`` names an existing association and vssadmin
    creates a second one rather than editing the first if it is wrong.

    A dry run returns the exact argv without running it, so the confirmation
    dialog can show the command it is asking about.
    """
    letter = _letter_of(volume)
    on_letter = _letter_of(on) if on is not None else letter
    wanted = Limit.parse(limit)
    try:
        exe = _vssadmin()
    except VssUnavailableError as exc:
        return ActionResult("resize", ok=False, reason=str(exc))
    argv = (
        exe, "resize", "shadowstorage",
        f"/for={letter}:", f"/on={on_letter}:", f"/maxsize={wanted.arg}",
    )
    if not win.is_user_an_admin():
        return ActionResult("resize", ok=False, argv=argv,
                            reason="resizing shadow storage requires Administrator")
    if dry_run:
        return ActionResult("resize", ok=True, dry_run=True, argv=argv)
    code, out = _run(argv, runner=runner, timeout_s=RESIZE_TIMEOUT_S)
    return ActionResult(
        "resize", ok=code == 0, argv=argv, code=code, lines=_tail(out),
        reason=None if code == 0 else ("; ".join(_tail(out)) or f"vssadmin exited {code}"),
    )


SCOPES: Final = ("all", "oldest")


def delete(
    volume: object = "C",
    *,
    scope: str = "oldest",
    runner: Runner | None = None,
    dry_run: bool = False,
) -> ActionResult:
    r"""Destroy shadow copies on *volume*. Irreversible, and it is the point.

    The default is ``oldest`` rather than ``all`` on purpose: v1's one and only
    move was ``/all /quiet`` (docs/01-AUDIT.md BUG-09), and a screen whose default
    button destroys every restore point on the machine has learned nothing from
    that. ``/quiet`` is still passed, because there is no console attached to
    answer the prompt -- the confirmation happens in the UI, where the user can
    read what is about to go.
    """
    letter = _letter_of(volume)
    if scope not in SCOPES:
        raise ValueError(f"unknown delete scope: {scope!r}")
    try:
        exe = _vssadmin()
    except VssUnavailableError as exc:
        return ActionResult("delete", ok=False, reason=str(exc))
    argv = (exe, "delete", "shadows", f"/for={letter}:", f"/{scope}", "/quiet")
    if not win.is_user_an_admin():
        return ActionResult("delete", ok=False, argv=argv,
                            reason="deleting shadow copies requires Administrator")
    if dry_run:
        return ActionResult("delete", ok=True, dry_run=True, argv=argv)
    code, out = _run(argv, runner=runner, timeout_s=DELETE_TIMEOUT_S)
    return ActionResult(
        "delete", ok=code == 0, argv=argv, code=code, lines=_tail(out),
        reason=None if code == 0 else ("; ".join(_tail(out)) or f"vssadmin exited {code}"),
    )


# ---------------------------------------------------------------------------
# Approval -- a mutation is previewed, then redeemed. Never both in one call.
# ---------------------------------------------------------------------------
# Shorter than the clean plan's ten minutes: this receipt is measured against a
# store whose contents Windows changes on its own, and a ceiling approved against
# figures from five minutes ago is a ceiling approved against nothing.
ACTION_TTL_S: Final = 300.0
MAX_LIVE_ACTIONS: Final = 8


class TokenError(RuntimeError):
    """A receipt that cannot be redeemed. The bridge turns this into ``ok: false``."""


class UnknownActionError(TokenError):
    pass


class ExpiredActionError(TokenError):
    pass


class SpentActionError(TokenError):
    pass


@dataclass
class PendingAction:
    """One previewed mutation, waiting for a confirmation it may never get.

    Holds the argv the preview produced rather than the arguments that produced
    it, and :meth:`run` refuses to execute anything else. That is the difference
    between "the user approved a resize" and "the user approved this command".

    ``phrase`` is what SPEC 4.3 asks for: DANGEROUS operations "phải gõ chữ xác
    nhận". It is the volume with its colon -- short, and the same word in both
    languages, which a translated "yes, delete" would not be. It is only demanded
    when copies can actually be lost: raising the ceiling is the way back from
    BUG-09 and must not be made to feel like the thing it undoes.
    """

    token: str
    created_at: float
    action: str                          # "resize" | "delete"
    letter: str
    preview: ActionResult
    limit: Limit | None = None
    on_letter: str = ""
    scope: str = ""
    destructive: bool = False
    copies_at_risk: int = 0
    spent_at: float | None = None

    @property
    def ok(self) -> bool:
        """Whether the preview succeeded. A refused preview is never stored."""
        return self.preview.ok

    @property
    def argv(self) -> tuple[str, ...]:
        return self.preview.argv

    @property
    def phrase(self) -> str:
        """What the user must type, or ``""`` when nothing need be typed."""
        return f"{self.letter}:" if self.destructive else ""

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created_at > ACTION_TTL_S

    def matches(self, typed: object) -> bool:
        """Whether *typed* is the phrase. Case and surrounding space forgiven.

        The point of the phrase is that the user stopped and read which volume
        they were about to change, not that they can hit shift -- so this asks
        :func:`_letter_of` rather than comparing strings, and "however a drive
        letter may be written" stays defined in exactly one place. Someone who
        types ``C:\\`` has read the same volume as someone who types ``C:``.
        """
        if not self.destructive:
            return True
        try:
            return _letter_of(typed) == self.letter.upper()
        except ValueError:
            return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "created_at": self.created_at,
            "expires_at": self.created_at + ACTION_TTL_S,
            "action": self.action,
            "letter": self.letter,
            "volume": f"{self.letter}:",
            "on_volume": f"{self.on_letter}:" if self.on_letter else "",
            "limit": self.limit.as_dict() if self.limit is not None else None,
            "scope": self.scope,
            "destructive": self.destructive,
            "phrase": self.phrase,
            "copies_at_risk": self.copies_at_risk,
            "preview": self.preview.as_dict(),
        }

    def run(self, *, runner: Runner | None = None) -> ActionResult:
        """Execute the previewed argv, and only it.

        Re-deriving the command here would let a caller's later argument reach
        ``vssadmin`` under an approval given for something else, so nothing is
        re-derived: the stored argv runs, or nothing does.
        """
        if not self.ok or not self.argv:
            return self.preview
        timeout = DELETE_TIMEOUT_S if self.action == "delete" else RESIZE_TIMEOUT_S
        code, out = _run(self.argv, runner=runner, timeout_s=timeout)
        return ActionResult(
            self.action, ok=code == 0, argv=self.argv, code=code, lines=_tail(out),
            reason=None if code == 0 else ("; ".join(_tail(out)) or f"vssadmin exited {code}"),
        )


class ActionStore:
    """The live receipts, keyed by token. Single-use, TTL- and capacity-bounded.

    Deliberately the same shape as ``cleaner.PlanStore``: one write per preview,
    one read per confirm, a lock because pywebview answers each call on its own
    thread, and a cap so that a UI bug cannot mint receipts without limit.
    """

    def __init__(self, capacity: int = MAX_LIVE_ACTIONS) -> None:
        self.capacity = capacity
        self._lock = threading.Lock()
        self._pending: dict[str, PendingAction] = {}

    def put(self, pending: PendingAction) -> None:
        """Store a receipt. A preview that was refused does not get one.

        Without this check a caller could ask for a resize it has no rights to,
        be told no, and still be holding a token -- which is the one thing the
        token exists to prevent.
        """
        if not pending.ok:
            raise ValueError("a refused preview cannot be approved")
        with self._lock:
            self._purge_locked()
            while len(self._pending) >= self.capacity:
                oldest = min(self._pending.values(), key=lambda p: p.created_at)
                del self._pending[oldest.token]
            self._pending[pending.token] = pending

    def peek(self, token: object) -> PendingAction | None:
        if not isinstance(token, str):
            return None
        with self._lock:
            return self._pending.get(token)

    def spend(self, token: object) -> PendingAction:
        """Redeem a receipt exactly once. Each failure is a distinct error.

        A double-clicked confirm button must not delete twice, and an expired
        receipt must be re-previewed rather than run against figures that have
        moved -- so "spent" and "expired" cannot be the same answer.
        """
        if not isinstance(token, str) or not token:
            raise UnknownActionError("no action token")
        now = time.time()
        with self._lock:
            found = self._pending.get(token)
            if found is None:
                raise UnknownActionError("no such action; preview it first")
            if found.spent_at is not None:
                raise SpentActionError("this action has already run")
            if found.expired(now):
                del self._pending[token]
                raise ExpiredActionError("the preview has expired; preview again")
            found.spent_at = now
            return found

    def _purge_locked(self) -> None:
        now = time.time()
        for token in [t for t, p in self._pending.items() if p.expired(now)]:
            del self._pending[token]

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()


_actions = ActionStore()


def action_store() -> ActionStore:
    """The process-wide store, mirroring ``cleaner.plan_store()``."""
    return _actions


def plan_resize(
    volume: object = "C",
    limit: object = PERCENT_10,
    *,
    on: object = None,
    storage: Storage | None = None,
    volume_total: int = 0,
    copies: int = 0,
    store: ActionStore | None = None,
) -> PendingAction:
    """Preview a resize and mint the receipt that can run it.

    Pass the *storage* row this was measured from and the volume's *volume_total*
    and the receipt knows two things the UI cannot work out for itself: which
    volume the copies actually live on -- ``/on=`` has to name the existing
    association or vssadmin makes a second one -- and whether this lowers the
    ceiling, which is what decides whether a phrase is demanded.

    *copies* is how many copies the volume has now, used only as the worst case
    to report when shrinking: Windows evicts as many as it needs to fit.
    """
    wanted = Limit.parse(limit)
    letter = _letter_of(volume)
    if on is not None:
        on_letter = _letter_of(on)
    elif storage is not None and storage.diff_letter:
        on_letter = storage.diff_letter
    else:
        on_letter = letter
    shrinks = would_shrink(storage, wanted, volume_total)
    if storage is not None and target_bytes(wanted, volume_total) is None and not shrinks:
        # A percentage against a volume of unknown size cannot be compared with the
        # ceiling that is already there. The fail-open answer -- "not a shrink, no
        # confirmation needed" -- is how BUG-09 got past a user in the first place,
        # so an unmeasurable resize is treated as the losing kind.
        shrinks = wanted.kind != "unbounded"
    pending = PendingAction(
        token=uuid.uuid4().hex,
        created_at=time.time(),
        action="resize",
        letter=letter,
        preview=resize(letter, wanted, on=on_letter, dry_run=True),
        limit=wanted,
        on_letter=on_letter,
        destructive=shrinks,
        copies_at_risk=copies if shrinks else 0,
    )
    if store is not None and pending.ok:
        store.put(pending)
    return pending


def plan_delete(
    volume: object = "C",
    *,
    scope: str = "oldest",
    copies: int = 0,
    store: ActionStore | None = None,
) -> PendingAction:
    """Preview a delete and mint the receipt that can run it. Always destructive.

    ``/oldest`` takes one copy and ``/all`` takes every one on the volume, so the
    count the dialog shows comes from *copies* -- the number the state read found,
    not a guess made after the fact.
    """
    letter = _letter_of(volume)
    pending = PendingAction(
        token=uuid.uuid4().hex,
        created_at=time.time(),
        action="delete",
        letter=letter,
        preview=delete(letter, scope=scope, dry_run=True),
        scope=scope,
        destructive=True,
        copies_at_risk=copies if scope == "all" else min(1, copies),
    )
    if store is not None and pending.ok:
        store.put(pending)
    return pending
