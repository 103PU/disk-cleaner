"""The WebView2 window. The only module in the project that imports pywebview.

Kept apart from :mod:`adc.shell.bridge` so that the argument validation -- the
part worth testing -- stays importable in a plain console process. This module is
thin on purpose: create the window, hand it a :class:`~adc.shell.bridge.Bridge`,
start the loop.

**No HTTP server, and the reason is one line of pywebview.** ``webview.start``
starts its global Bottle server when ``http_server`` is true *or* any window's URL
is "local" (webview/__init__.py:191), and ``is_local_url`` counts a bare
filesystem path as local while explicitly excluding ``file://``
(webview/util.py:76-78). So the URL handed to :func:`webview.create_window` must
be a ``file://`` URI, not a path -- that single detail is what keeps ADC v2 off
the network and closes SEC-01/02/03 from docs/01-AUDIT.md.
:func:`assert_no_server` checks the invariant at runtime rather than trusting the
reading of it.

One consequence for P4: the page is loaded from ``file://``, where WebView2
applies CORS to module scripts. Use classic ``<script>`` tags, not
``<script type="module">``, and keep every asset relative.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Final

import webview

from adc.engine import platform_win as win
from adc.engine.audit import get as get_logger
from adc.shell.bridge import Bridge

_log = get_logger(__name__)

#: Also the string the second instance looks for with ``FindWindowW``, so it must
#: stay stable and must not carry a version or a document name.
WINDOW_TITLE: Final = "Disk CleanUp"

#: ``Local\`` so each desktop session gets its own instance. See
#: :func:`adc.engine.platform_win.create_single_instance_mutex`.
MUTEX_NAME: Final = r"Local\DiskCleanUp.SingleInstance"


WIDTH: Final = 1280
HEIGHT: Final = 860
MIN_SIZE: Final = (1024, 700)

#: SPEC 4.1's ``--bg-color``. Set on the native window as well as in the page, so
#: the frame does not flash white for the frame or two before the first paint.
BACKGROUND: Final = "#f8f9fc"

#: How long a ``--relaunched`` process waits for the mutex the unelevated process
#: is still holding. Two seconds is far more than the handoff needs and still
#: short enough that a stuck first instance does not hang the elevated one.
RELAUNCH_WAIT_S: Final = 2.0


def asset_dir() -> Path:
    """Where ``index.html`` and its assets live, frozen or from source.

    PyInstaller unpacks a onedir bundle's data into ``sys._MEIPASS``; from source
    the same tree is ``src/adc/ui``. One function so P6's ``--add-data`` has one
    place to match.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(str(base)) / "ui"
    return Path(__file__).resolve().parent.parent / "ui"


def index_url() -> str:
    """The ``file://`` URI of the entry document.

    A URI rather than a path, and that is not cosmetic: a path would make
    pywebview start an HTTP server (see the module docstring).
    """
    index = asset_dir() / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"UI not found at {index}")
    return index.as_uri()


def icon_path() -> str | None:
    """The window and taskbar icon, if one has been built yet."""
    icon = asset_dir() / "adc.ico"
    return str(icon) if icon.is_file() else None


def single_instance(*, wait_s: float = 0.0) -> tuple[int | None, bool]:
    """Take the mutex. Returns ``(handle, we_are_first)``.

    *wait_s* is for the elevation handoff and nothing else. A ``--relaunched``
    process starts while the unelevated one is still closing, so it would find
    the mutex held, focus a window that is about to vanish, and exit -- meaning
    every elevation would appear to do nothing. Waiting a bounded moment for the
    handle to be released is what makes "Restart as Administrator" work.
    """
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        handle, first = win.create_single_instance_mutex(MUTEX_NAME)
        if first or time.monotonic() >= deadline:
            return handle, first
        # The loser must not keep the handle: holding it would itself count as an
        # instance to whoever asks next.
        if handle is not None:
            win.close_handle(handle)
        time.sleep(0.05)


def would_start_server(url: str) -> bool:
    """Whether pywebview would start its HTTP server for this URL.

    Asked with pywebview's own predicate rather than a copy of it, so the answer
    tracks the library across upgrades instead of tracking one reading of it. This
    is the check that can run *before* a window exists, which is what makes it
    testable: ``tests/test_shell_window.py`` asserts it is False for
    :func:`index_url`, and ``--self-check`` reports it.
    """
    from webview.util import is_local_url

    return bool(is_local_url(url))


def assert_no_server() -> None:
    """Fail if pywebview started its HTTP server after all.

    The belt to :func:`would_start_server`'s braces, checked on the way out: a
    comment claiming there is no server is worth nothing beside something that
    looks. If this fires, some URL stopped being a ``file://`` URI and v1's
    listening socket is back.
    """
    server = getattr(webview.http, "global_server", None)
    if server is not None:
        raise RuntimeError(
            "pywebview started an HTTP server; a window URL is no longer a file:// URI"
        )


def _shutdown() -> None:
    """Close every window, which ends ``webview.start`` and releases the mutex.

    Looked up through ``webview.windows`` rather than closed over a reference,
    because the bridge is constructed before the window exists and this is what it
    is handed as its relaunch hook.
    """
    for window in list(webview.windows):
        window.destroy()


def self_check() -> dict[str, Any]:
    """What P3's acceptance asks for, without opening a window.

    ``guilib.initialize`` is the honest way to answer "which renderer": on Windows
    both ``edgechromium`` and ``mshtml`` load the same WinForms module, and which
    one it then uses is decided inside it by whether WebView2 is actually present
    (webview/platforms/winforms.py:107). Reporting the string this asked for
    rather than the one that loaded would make the check worthless -- silently
    falling back to MSHTML is exactly the failure it exists to catch.
    """
    guilib = webview.initialize("edgechromium")
    handle, first = single_instance()
    if handle is not None:
        win.close_handle(handle)
    url = index_url()
    return {
        "renderer": getattr(guilib, "renderer", "unknown"),
        "webview2_runtime": win.webview2_version(),
        "admin": win.is_user_an_admin(),
        "single_instance": "ok" if first else "already running",
        "http_server": "none" if not would_start_server(url) else "WOULD START",
        "dpi_per_monitor": win.enable_per_monitor_dpi(),
        "ui": str(asset_dir()),
    }


def run(*, relaunched: bool = False, debug: bool = False) -> int:
    """Open the window and run until it closes. Returns a process exit code.

    Order matters twice here. DPI awareness has to be declared before any window
    exists, or the spike's 520x340 window opens at 646x414 on a 1.25x display
    (docs/02-SPEC.md 3.3). And the mutex has to be taken before the window is
    created, so a second launch never gets as far as a WebView2 instance.
    """
    win.enable_per_monitor_dpi()
    handle, first = single_instance(wait_s=RELAUNCH_WAIT_S if relaunched else 0.0)
    if not first:
        focused = win.focus_window(WINDOW_TITLE)
        _log.info("second instance: focusing the first window (found=%s)", focused)
        if handle is not None:
            win.close_handle(handle)
        return 0

    try:
        url = index_url()
        if would_start_server(url):
            # Refuse rather than open a window with a listening socket behind it.
            # The whole SEC-01..03 class came from having one.
            raise RuntimeError(f"refusing to open: {url} would start an HTTP server")
        bridge = Bridge(on_relaunch=_shutdown)
        webview.create_window(
            WINDOW_TITLE,
            url=url,
            js_api=bridge,
            width=WIDTH,
            height=HEIGHT,
            min_size=MIN_SIZE,
            background_color=BACKGROUND,
            text_select=False,
            zoomable=False,
        )
        _log.info(
            "window: %dx%d admin=%s webview2=%s",
            WIDTH,
            HEIGHT,
            win.is_user_an_admin(),
            win.webview2_version(),
        )
        webview.start(gui="edgechromium", debug=debug, icon=icon_path())
        assert_no_server()
    finally:
        # Released on the way out, including after a crash: a mutex left held by a
        # dead process is freed by the kernel, but the elevation handoff waits on
        # this handle and should not have to wait for the kernel to notice.
        if handle is not None:
            win.close_handle(handle)
    return 0
