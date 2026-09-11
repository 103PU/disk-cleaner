r"""The allowlist. Nothing is deleted anywhere in this app without passing here.

This module is the reason the rest of the engine can be written without paranoia
(docs/02-SPEC.md 4.7). Four rules, checked in this order:

1. the path must ``realpath`` **into** one of the roots registered for the
   target being cleaned -- escaping through ``..``, a symlink or a junction
   aborts the whole target, not just that path;
2. a hard-block list is refused outright, no matter which root claims it;
3. a path that came from the UI is refused on principle -- the bridge takes a
   ``target_id`` and looks the path up on the Python side (SEC-02);
4. the user's own exclusion list is applied last and can veto anything the
   first three rules allowed.

``realpath`` is what makes rule 1 work on Windows: since CPython 3.8 it resolves
junctions, so ``root\evil -> C:\Windows`` resolves to ``C:\Windows`` and lands
outside the root. ``islink()`` never sees it; ``realpath()`` always does.

Everything raises. A guard that returns ``False`` invites a caller to forget the
check, and the one caller that forgets is the one that deletes ``C:\Windows``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .paths import install_dir


class GuardError(Exception):
    """Base: this path must not be touched. Always fatal for its target."""


class OutsideRootError(GuardError):
    """The path resolved outside every registered root (rule 1)."""


class BlockedPathError(GuardError):
    """The path is on the hard-block list (rule 2)."""


class UnsafeInputError(GuardError):
    """A path arrived from outside the engine (rule 3)."""


class ExcludedPathError(GuardError):
    """The user's exclusion list vetoed it (rule 4)."""


def _norm(path: str | os.PathLike[str]) -> str:
    r"""Absolute, symlink/junction-resolved, case-folded, no trailing separator.

    Case folding is not cosmetic on Windows: ``C:\WINDOWS\System32`` and
    ``c:\windows\system32`` are the same directory and a block list that
    compares case-sensitively blocks neither.
    """
    resolved = os.path.realpath(os.path.abspath(os.fspath(path)))
    folded = os.path.normcase(resolved)
    # Keep the separator on a drive root ("c:\\"), strip it everywhere else, so
    # prefix comparisons below have exactly one form to handle.
    if len(folded) > 3 and folded.endswith(os.sep):
        folded = folded.rstrip(os.sep)
    return folded


def _is_within(candidate: str, root: str) -> bool:
    """True when *candidate* is *root* or lives underneath it. Both pre-normed.

    String prefixing with the separator appended is deliberate over
    ``commonpath``, which raises on paths from different drives -- a routine
    situation here, since the pnpm store is on E: and the profile on C:.
    """
    if candidate == root:
        return True
    if root.endswith(os.sep):          # drive root: "c:\\"
        return candidate.startswith(root)
    return candidate.startswith(root + os.sep)


def _env_dir(name: str) -> str | None:
    raw = os.environ.get(name)
    return _norm(raw) if raw else None


# Deleting the directory itself is forbidden; specific children may still be
# legal targets. %LOCALAPPDATA%\Temp is a target, %LOCALAPPDATA% is not.
_EXACT_ENV = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PUBLIC", "SystemRoot")

# The directory and everything below it. Nothing ADC cleans lives here, and a
# mistake here is unrecoverable without a reinstall.
_SUBTREE_UNDER_SYSTEMROOT = (
    "System32", "SysWOW64", "WinSxS", "servicing", "assembly", "Fonts",
    "Boot", "INF", "Microsoft.NET", "System", "Globalization", "security",
)
_SUBTREE_ENV = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles")

# Not in the SPEC's enumeration; added because these hold the only files in the
# profile that cannot be regenerated. A cleaner has no business inside them, and
# no catalogue target resolves into any of them.
_SUBTREE_UNDER_PROFILE = (
    "Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music",
    "OneDrive", "Favorites", "Links", "Saved Games", "Contacts", "Searches",
    ".ssh", ".gnupg",
)


def _blocked_exact() -> frozenset[str]:
    out: set[str] = set()
    for name in _EXACT_ENV:
        found = _env_dir(name)
        if found:
            out.add(found)
    return frozenset(out)


def _blocked_subtrees() -> frozenset[str]:
    out: set[str] = set()
    for name in _SUBTREE_ENV:
        found = _env_dir(name)
        if found:
            out.add(found)
    windir = _env_dir("SystemRoot")
    if windir:
        out.update(os.path.normcase(os.path.join(windir, leaf))
                   for leaf in _SUBTREE_UNDER_SYSTEMROOT)
    profile = _env_dir("USERPROFILE")
    if profile:
        out.update(os.path.normcase(os.path.join(profile, leaf))
                   for leaf in _SUBTREE_UNDER_PROFILE)
    # ADC's own files. A portable copy can sit anywhere, including inside a
    # directory the user then asks us to clean.
    out.add(_norm(install_dir()))
    return frozenset(out)


def _is_volume_root(normed: str) -> bool:
    r"""``C:\``, ``\server\share``, and bare ``\?\C:\`` all count."""
    drive, rest = os.path.splitdrive(normed)
    return bool(drive) and rest in ("", os.sep, "/")


def reject_external_path(value: object) -> None:
    """Rule 3, as something the bridge can actually call.

    v1's ``/api/open-folder`` took a path from JavaScript and handed it to
    ``os.startfile`` -- ShellExecute -- which is arbitrary local execution
    (SEC-02). The v2 bridge accepts ``target_id`` only; this exists so that a
    stray ``path`` key in a payload fails loudly instead of being honoured.
    """
    raise UnsafeInputError(
        f"paths are never accepted from outside the engine (got {type(value).__name__})"
    )


@dataclass
class Guard:
    """Roots for one target, plus the user's exclusions. Immutable in practice.

    Built by the cleaner from the catalogue -- never from anything the UI sent.
    Construct once per target and call :meth:`check` on every single path before
    it is deleted; the walker is not a substitute, because between the walk and
    the delete a junction can appear.
    """

    roots: tuple[str, ...]
    exclusions: tuple[str, ...] = ()
    _blocked_exact: frozenset[str] = field(default_factory=frozenset, repr=False)
    _blocked_subtrees: frozenset[str] = field(default_factory=frozenset, repr=False)

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("a Guard with no roots would allow nothing; refusing to build")
        object.__setattr__(self, "roots", tuple(_norm(r) for r in self.roots))
        object.__setattr__(self, "exclusions", tuple(_norm(e) for e in self.exclusions))
        object.__setattr__(self, "_blocked_exact", _blocked_exact())
        object.__setattr__(self, "_blocked_subtrees", _blocked_subtrees())
        # A root that is itself blocked is a catalogue bug, and finding it at
        # construction is far better than finding it per file.
        for root in self.roots:
            self._assert_not_blocked(root, is_root=True)

    # -- rule 2 -------------------------------------------------------------
    def _assert_not_blocked(self, normed: str, *, is_root: bool = False) -> None:
        if _is_volume_root(normed):
            raise BlockedPathError(f"volume root is never deletable: {normed}")
        if normed in self._blocked_exact:
            raise BlockedPathError(f"protected directory itself: {normed}")
        for blocked in self._blocked_subtrees:
            if _is_within(normed, blocked):
                where = "target root" if is_root else "path"
                raise BlockedPathError(f"{where} inside a protected subtree: {normed}")

    # -- rules 1 and 4 ------------------------------------------------------
    def check(self, path: str | os.PathLike[str]) -> str:
        """Return the resolved path, or raise. The only sanctioned entry point.

        The return value is the path the caller should delete: already resolved,
        so a caller that uses it cannot be redirected by a junction swapped in
        between this check and the delete.
        """
        raw = os.fspath(path)
        if not raw or not raw.strip():
            raise BlockedPathError("empty path")
        if "*" in raw or "?" in raw:
            # A wildcard means someone passed a pattern where a path was due;
            # expanding it here would silently widen the blast radius.
            raise BlockedPathError(f"wildcards are not paths: {raw}")

        normed = _norm(raw)
        self._assert_not_blocked(normed)

        if not any(_is_within(normed, root) for root in self.roots):
            raise OutsideRootError(
                f"{normed} resolves outside every registered root {self.roots}"
            )

        for excluded in self.exclusions:
            if _is_within(normed, excluded):
                raise ExcludedPathError(f"user exclusion covers {normed}")

        return normed

    def allows(self, path: str | os.PathLike[str]) -> bool:
        """Non-raising form, for planning and for the dry run only.

        Never use this to decide a delete: the deleter must call :meth:`check`
        so that the reason lands in the job log.
        """
        try:
            self.check(path)
        except GuardError:
            return False
        return True

    def why_not(self, path: str | os.PathLike[str]) -> str | None:
        """The refusal reason, for the UI. ``None`` when the path is allowed."""
        try:
            self.check(path)
        except GuardError as exc:
            return str(exc)
        return None
