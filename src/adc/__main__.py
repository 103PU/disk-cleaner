"""Entry point. ``python -m adc``, and what the frozen launcher calls.

Logging is configured on the first line of :func:`main`, before argument parsing
and before anything imports the shell. That ordering is the point of this module:
a crash during startup -- a missing WebView2 runtime, an unwritable profile
directory, a broken ``sys.path`` in the frozen bundle -- happens in exactly the
window where there is no UI to show it in, so the log has to already exist.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from adc import __version__
from adc.engine import audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="disk-cleanup",
        description="Disk CleanUp -- reclaim disk space on Windows.",
    )
    parser.add_argument("--version", action="version", version=f"Disk CleanUp {__version__}")

    parser.add_argument(
        "--self-check",
        action="store_true",
        help="report renderer, WebView2 runtime, elevation and instance state, then exit",
    )
    parser.add_argument(
        "--relaunched",
        action="store_true",
        help=(
            "set by the elevation handoff: wait briefly for the previous instance's "
            "single-instance mutex instead of treating it as an app already running"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="open WebView2 devtools",
    )
    return parser


def _attach_console_if_needed() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    if ctypes.windll.kernel32.AttachConsole(-1):
        try:
            if sys.stdout is None or getattr(sys.stdout, "closed", False):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
            if sys.stderr is None or getattr(sys.stderr, "closed", False):
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        except OSError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _attach_console_if_needed()
    log_path = audit.setup()
    audit.install_excepthooks()
    args = _parser().parse_args(argv)
    log = audit.get(__name__)
    log.info(
        "adc %s starting: self_check=%s relaunched=%s",
        __version__,
        args.self_check,
        args.relaunched,
    )

    # Imported here, not at module scope: --version and --self-check must not
    # depend on pythonnet loading, and a frozen build should fail with a readable
    # message rather than an import traceback before argv is even parsed.
    from adc.shell import window

    if args.self_check:
        for key, value in window.self_check().items():
            print(f"{key}={value}")
        print(f"log={log_path}")
        return 0

    try:
        return window.run(relaunched=args.relaunched, debug=args.debug)
    except Exception:
        log.exception("startup failed")
        raise


if __name__ == "__main__":
    sys.exit(main())
