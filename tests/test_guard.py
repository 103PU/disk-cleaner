r"""Guard tests. Everything here is a path that must **not** be deleted.

These are the tests whose failure would mean the app can destroy a Windows
install, so they are written as "this raises" rather than "this returns False":
a guard that answers with a boolean can be ignored by a caller that forgets to
look, and the caller that forgets is the one that formats C:.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adc.engine import guard as g
from tests.fixtures.make_tree import FixtureUnavailable, make_junction, write_file


@pytest.fixture
def guarded(tree: Path) -> g.Guard:
    """A guard over one ordinary target root with a file in it."""
    write_file(tree / "cache" / "blob.bin", 128)
    return g.Guard(roots=(str(tree / "cache"),))


# ---------------------------------------------------------------------------
# Rule 2 -- hard blocks
# ---------------------------------------------------------------------------
def test_guard_rejects_volume_root(guarded: g.Guard) -> None:
    r"""``C:\`` and every other volume root, whatever the roots say."""
    for root in ("C:\\", "c:/", "D:\\", "E:\\"):
        with pytest.raises(g.BlockedPathError):
            guarded.check(root)


def test_guard_rejects_windir(guarded: g.Guard) -> None:
    """``%SystemRoot%``, ``System32`` and ``%ProgramFiles%`` are all refused."""
    windir = os.environ["SYSTEMROOT"]
    candidates = [
        windir,
        os.path.join(windir, "System32"),
        os.path.join(windir, "System32", "drivers", "etc", "hosts"),
        os.path.join(windir, "WinSxS"),
        os.environ["PROGRAMFILES"],
        os.path.join(os.environ["PROGRAMFILES"], "anything", "at", "all"),
    ]
    for candidate in candidates:
        with pytest.raises(g.BlockedPathError):
            guarded.check(candidate)


def test_guard_rejects_bare_profile_dirs(guarded: g.Guard) -> None:
    r"""The profile roots themselves, even though targets live inside them.

    ``%LOCALAPPDATA%\Temp`` is a legitimate target; ``%LOCALAPPDATA%`` is not.
    That distinction is the whole reason the block list has two kinds of entry.
    """
    for var in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        with pytest.raises(g.BlockedPathError):
            guarded.check(os.environ[var])


def test_guard_rejects_user_data_subtrees(guarded: g.Guard) -> None:
    """Documents, Downloads and friends: the files that cannot be regenerated."""
    profile = os.environ["USERPROFILE"]
    for leaf in ("Documents", "Downloads", "Desktop", "Pictures"):
        with pytest.raises(g.BlockedPathError):
            guarded.check(os.path.join(profile, leaf, "anything.txt"))


def test_guard_rejects_own_install_dir(guarded: g.Guard) -> None:
    """ADC never deletes ADC, even if a target root happens to contain it."""
    from adc.engine.paths import install_dir

    with pytest.raises(g.BlockedPathError):
        guarded.check(install_dir() / "adc.exe")


def test_guard_refuses_to_build_on_a_blocked_root() -> None:
    """A catalogue entry pointing at a protected tree fails at construction.

    Far better than discovering it per file: the error names the target root, so
    the fix is obvious, and no partial delete can have happened yet.
    """
    with pytest.raises(g.BlockedPathError):
        g.Guard(roots=(os.environ["PROGRAMFILES"],))
    with pytest.raises(g.BlockedPathError):
        g.Guard(roots=("C:\\",))


def test_guard_refuses_to_build_with_no_roots() -> None:
    """An empty root tuple would allow nothing, which hides a wiring bug."""
    with pytest.raises(ValueError, match="no roots"):
        g.Guard(roots=())


# ---------------------------------------------------------------------------
# Rule 1 -- stay inside the registered root
# ---------------------------------------------------------------------------
def test_guard_allows_inside_root(guarded: g.Guard, tree: Path) -> None:
    """The happy path, and the resolved path is what comes back."""
    resolved = guarded.check(tree / "cache" / "blob.bin")

    assert resolved.endswith(os.path.normcase(os.path.join("cache", "blob.bin")))
    assert guarded.allows(tree / "cache" / "blob.bin")
    assert guarded.why_not(tree / "cache" / "blob.bin") is None


def test_guard_rejects_sibling_of_root(guarded: g.Guard, tree: Path) -> None:
    """A directory next to the root is outside it, prefix similarity or not."""
    with pytest.raises(g.OutsideRootError):
        guarded.check(tree / "cache-other" / "x.bin")
    with pytest.raises(g.OutsideRootError):
        guarded.check(tree / "other")


def test_guard_rejects_dotdot_escape(guarded: g.Guard, tree: Path) -> None:
    """``..`` is resolved before the comparison, so it cannot climb out."""
    with pytest.raises(g.OutsideRootError):
        guarded.check(tree / "cache" / ".." / ".." / "escaped.bin")


@pytest.mark.windows_only
def test_guard_rejects_escape_via_junction(guarded: g.Guard, tree: Path,
                                          tmp_path: Path) -> None:
    r"""A junction inside the root that points outside it must not be followed.

    This is the attack the walker's reparse check does *not* cover: the walker
    refuses to descend, but a deleter handed the path directly would follow it,
    because the OS follows junctions transparently. ``realpath`` resolves
    junctions on Windows, which is what makes the escape visible here --
    ``islink()`` would report False and let it through.
    """
    outside = tmp_path / "outside"
    write_file(outside / "precious.bin", 64)
    try:
        make_junction(tree / "cache" / "escape", outside)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    with pytest.raises(g.OutsideRootError):
        guarded.check(tree / "cache" / "escape" / "precious.bin")
    with pytest.raises(g.OutsideRootError):
        guarded.check(tree / "cache" / "escape")


@pytest.mark.windows_only
def test_guard_allows_a_root_reached_through_a_junction(tmp_path: Path) -> None:
    r"""A junctioned *root* is fine -- the store really is somewhere else.

    ``E:\.pnpm-store\v11`` on this machine is reached that way (BUG-02). Both the
    root and the paths under it resolve to the same real location, so rule 1 is
    satisfied and cleaning it is legal.
    """
    real_store = tmp_path / "real-store"
    write_file(real_store / "pkg.bin", 256)
    try:
        link = make_junction(tmp_path / "linked-store", real_store)
    except FixtureUnavailable as exc:
        pytest.skip(str(exc))

    guard = g.Guard(roots=(str(link),))

    assert guard.allows(link / "pkg.bin")
    assert guard.allows(real_store / "pkg.bin"), "same real path, different spelling"


# ---------------------------------------------------------------------------
# Rule 3 -- no paths from outside the engine
# ---------------------------------------------------------------------------
def test_reject_external_path_always_raises() -> None:
    """SEC-02 at the root: the bridge takes a ``target_id``, never a path."""
    for value in (os.environ["SYSTEMROOT"], "", None, 42, Path("x")):
        with pytest.raises(g.UnsafeInputError):
            g.reject_external_path(value)


# ---------------------------------------------------------------------------
# Rule 4 -- user exclusions have the last word
# ---------------------------------------------------------------------------
def test_guard_exclusion_vetoes_an_allowed_path(tree: Path) -> None:
    """An exclusion beats rule 1: inside the root is not enough."""
    root = tree / "cache"
    write_file(root / "keep" / "k.bin", 32)
    write_file(root / "drop" / "d.bin", 32)
    guard = g.Guard(roots=(str(root),), exclusions=(str(root / "keep"),))

    assert guard.allows(root / "drop" / "d.bin")
    with pytest.raises(g.ExcludedPathError):
        guard.check(root / "keep" / "k.bin")
    with pytest.raises(g.ExcludedPathError):
        guard.check(root / "keep")


# ---------------------------------------------------------------------------
# Input hygiene
# ---------------------------------------------------------------------------
def test_guard_rejects_wildcards_and_blanks(guarded: g.Guard, tree: Path) -> None:
    """A pattern where a path was expected would silently widen the delete."""
    for bad in (str(tree / "cache" / "*.bin"), str(tree / "cache" / "?.bin"), "", "   "):
        with pytest.raises(g.BlockedPathError):
            guarded.check(bad)


def test_guard_is_case_insensitive(guarded: g.Guard, tree: Path) -> None:
    r"""``C:\WINDOWS`` and ``c:\windows`` are one directory on Windows."""
    windir = os.environ["SYSTEMROOT"]
    for spelling in (windir.upper(), windir.lower()):
        with pytest.raises(g.BlockedPathError):
            guarded.check(spelling)


def test_why_not_explains_the_refusal(guarded: g.Guard) -> None:
    """The UI needs a reason string, not just a rejection."""
    reason = guarded.why_not("C:\\")

    assert reason is not None
    assert "volume root" in reason
