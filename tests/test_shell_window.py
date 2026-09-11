"""Window tests. The load-bearing one is that no HTTP server can start.

v1 served the UI from Flask on ``127.0.0.1`` and took commands over it, which is
SEC-01/02/03 in docs/01-AUDIT.md: any local process, and any page in any browser
on the machine, could reach it. v2's answer is not authentication -- it is that
there is nothing listening.

That guarantee rests on one detail of pywebview, and this file is where it is
pinned. ``webview.start`` starts its global Bottle server when ``http_server`` is
true *or* any window URL is "local" (webview/__init__.py:191), and
``is_local_url`` counts a bare filesystem path as local while explicitly
excluding ``file://`` (webview/util.py:76-78). So the URL must be a ``file://``
URI. The distinction is invisible at a glance -- ``str(path)`` and
``path.as_uri()`` look equally reasonable in a call to ``create_window`` -- which
is exactly why it needs a test rather than a comment.

The tests assert against pywebview's *own* predicate rather than a copy of it, so
an upgrade that changes the rule fails here instead of silently opening a port.

These tests import ``webview``, which pulls in pythonnet. They are marked
``windows_only`` for that reason; the engine and bridge suites stay importable in
a plain process, which ``tests/test_layering.py`` checks.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from adc.engine import platform_win as win

pytestmark = pytest.mark.windows_only

# Skipped rather than failed off-Windows: pywebview's Windows backend needs
# pythonnet, and a host without it should report "not applicable", not "broken".
window = pytest.importorskip("adc.shell.window", reason="needs pywebview + pythonnet")


# ---------------------------------------------------------------------------
# SEC-01: nothing listens
# ---------------------------------------------------------------------------
def test_the_ui_url_is_a_file_uri_and_starts_no_server() -> None:
    """The single assertion the no-network claim rests on.

    Asked through ``would_start_server``, which calls pywebview's own
    ``is_local_url``. If a future pywebview changes what counts as local, this
    goes red -- which is the whole point of not reimplementing the predicate.
    """
    url = window.index_url()

    assert url.startswith("file:///")
    assert window.would_start_server(url) is False


def test_the_same_page_as_a_bare_path_would_start_a_server() -> None:
    """The negative control, and the reason ``index_url`` returns a URI.

    Without this test the one above could pass for the wrong reason -- a
    ``would_start_server`` that always answered False would satisfy it. This
    proves the predicate discriminates, and it documents the mistake that is easy
    to make: ``create_window(url=str(path))`` opens a port,
    ``create_window(url=path.as_uri())`` does not.
    """
    as_path = str(window.asset_dir() / "index.html")

    assert window.would_start_server(as_path) is True


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8080/", "https://example.invalid/", "file:///C:/x/index.html"],
    ids=["http", "https", "file"],
)
def test_only_a_scheme_free_path_is_treated_as_local(url: str) -> None:
    """Documents pywebview's rule, so a reader need not go and read the source."""
    assert window.would_start_server(url) is False


def test_no_server_is_running_in_this_process() -> None:
    """``assert_no_server`` is the on-the-way-out check; here it runs on a cold process.

    Nothing has called ``webview.start``, so the global server must be None. This
    is a smoke test of the assertion itself: if it could not tell, the check
    inside ``run`` would be decorative.
    """
    window.assert_no_server()


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
def test_the_asset_directory_holds_the_page() -> None:
    assert (window.asset_dir() / "index.html").is_file()


def test_the_asset_directory_follows_a_frozen_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a PyInstaller onedir build the UI ships under ``sys._MEIPASS``.

    P6 has to pass ``--add-data`` that matches this. Asserted now so that the
    packaging step has something to satisfy rather than a convention to guess at.
    """
    monkeypatch.setattr(sys, "_MEIPASS", "C:\\bundle", raising=False)

    assert window.asset_dir() == Path("C:\\bundle") / "ui"


def test_a_missing_page_is_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken install must say which file is missing, not fail inside WebView2."""
    monkeypatch.setattr(window, "asset_dir", lambda: tmp_path)

    with pytest.raises(FileNotFoundError, match="index.html"):
        window.index_url()


def test_the_icon_is_optional() -> None:
    """``adc.ico`` is a P6 deliverable; until then the window uses the default.

    Answering None rather than raising is what lets P3 run before P6 exists.
    """
    icon = window.icon_path()

    assert icon is None or Path(icon).is_file()


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------
def test_the_second_caller_is_told_it_is_not_first() -> None:
    """Two handles on one name: the first wins, the second is told so.

    Against a test-only name, not ``MUTEX_NAME``. Taking the real one would make
    the test's answer depend on whether the app happens to be running -- it would
    pass on a clean machine and fail on the developer's, which is the worst
    possible failure mode for a test of a locking primitive.
    """
    name = f"Local\\adc-test-{uuid.uuid4().hex}"

    first_handle, first = win.create_single_instance_mutex(name)
    second_handle, second = win.create_single_instance_mutex(name)
    try:
        assert first is True
        assert second is False
        # The loser still gets a handle, and still has to close it. Returning None
        # would leak the kernel object for the life of the process.
        assert second_handle is not None
    finally:
        for handle in (first_handle, second_handle):
            if handle is not None:
                win.close_handle(handle)


def test_the_name_is_released_when_the_handle_closes() -> None:
    """Which is what makes the elevation handoff possible at all.

    The elevated process waits for this to happen; if the name outlived the
    handle, "Restart as Administrator" could never succeed.
    """
    name = f"Local\\adc-test-{uuid.uuid4().hex}"

    handle, first = win.create_single_instance_mutex(name)
    assert first is True
    assert handle is not None
    win.close_handle(handle)

    again, free = win.create_single_instance_mutex(name)
    try:
        assert free is True, "the name outlived its handle"
    finally:
        if again is not None:
            win.close_handle(again)


def test_the_mutex_is_session_scoped() -> None:
    """``Local\\``, not ``Global\\``.

    Fast User Switching, or a service account, must get its own instance rather
    than being told one is already running by a window it cannot see and cannot
    close.
    """
    assert window.MUTEX_NAME.startswith("Local\\")
    assert "Global\\" not in window.MUTEX_NAME


def test_a_relaunch_waits_for_the_old_instance_to_let_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The elevation handoff, without a UAC prompt.

    The unelevated process is still closing when the elevated one starts, so a
    ``--relaunched`` start that gave up on the first try would focus a window
    that is about to vanish and exit -- every elevation would appear to do
    nothing. This stands in for the old instance letting go on the third attempt.
    """
    attempts: list[int] = []

    def flaky(name: str) -> tuple[int | None, bool]:
        attempts.append(1)
        return (111, False) if len(attempts) < 3 else (222, True)

    monkeypatch.setattr(window.win, "create_single_instance_mutex", flaky)
    closed: list[int] = []
    monkeypatch.setattr(window.win, "close_handle", lambda h: bool(closed.append(h)))

    handle, first = window.single_instance(wait_s=5.0)

    assert (handle, first) == (222, True)
    assert len(attempts) == 3
    # Each losing handle was closed on the way round: holding one would itself
    # count as an instance to whoever asks next.
    assert closed == [111, 111]


def test_without_a_wait_a_held_mutex_is_reported_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal double-launch must not pause before focusing the first window."""
    calls: list[str] = []
    monkeypatch.setattr(
        window.win,
        "create_single_instance_mutex",
        lambda name: (calls.append(name), (99, False))[1],
    )

    handle, first = window.single_instance()

    assert (handle, first) == (99, False)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# The window is never opened by a test
# ---------------------------------------------------------------------------
def test_run_refuses_a_url_that_would_open_a_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-flight refusal, asserted without creating a window.

    ``run`` checks before ``create_window``, so a regression that turned the URL
    back into a path fails closed -- no window, no port -- rather than starting a
    server and logging about it. ``webview.create_window`` is replaced to prove
    it is never reached.
    """
    monkeypatch.setattr(window, "index_url", lambda: "C:\\adc\\ui\\index.html")
    monkeypatch.setattr(window, "single_instance", lambda **kw: (None, True))
    monkeypatch.setattr(
        window.webview,
        "create_window",
        lambda *a, **k: pytest.fail("a window was created for a server-starting URL"),
    )

    with pytest.raises(RuntimeError, match="would start an HTTP server"):
        window.run()
