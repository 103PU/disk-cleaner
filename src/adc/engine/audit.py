r"""The audit log: ``%LOCALAPPDATA%\DiskCleanUp\logs\adc-YYYYMMDD.log``.


One file per day, seven days kept (SPEC 3.2). Set up on the first line of
``__main__`` so that a crash *before* the window exists still leaves a trace --
v1 wrote nothing anywhere, so "it did not start" was the whole bug report.

What belongs in here is the audit trail: which job ran, which targets it touched,
what it reclaimed, what it refused and why. What does **not** belong is a line
per deleted file. A clean can remove two hundred thousand files, and a disk
cleaner that fills the disk with its own log has failed at its one job. The
per-file detail lives in the job event log, which is bounded at
``Job.max_events`` and ends up in the JSON report.

No secrets, by construction: nothing in the engine holds a credential. Paths are
logged, because a deletion record without the path is not an audit trail.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Final, TextIO

from .paths import ensure, logs_dir

LOGGER_NAME: Final = "adc"
FILE_PREFIX: Final = "adc-"
FILE_SUFFIX: Final = ".log"
KEEP_DAYS: Final = 7
DATE_FORMAT: Final = "%Y%m%d"

LINE_FORMAT: Final = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
TIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

_lock = threading.Lock()
_configured = False


def _today() -> str:
    return dt.datetime.now().strftime(DATE_FORMAT)


def log_file(day: str | None = None) -> Path:
    return logs_dir() / f"{FILE_PREFIX}{day or _today()}{FILE_SUFFIX}"


class DailyFileHandler(logging.Handler):
    """Writes to today's file, and notices when today changes.

    ``TimedRotatingFileHandler`` would rename the live file to
    ``adc.log.2026-08-29``, which is not the name the SPEC asks for and not a name
    that sorts usefully in Explorer. This handler instead derives the filename
    from the date on every emit -- one ``strftime`` per record, against a job that
    is doing disk I/O -- and reopens when it differs. That matters for the 02:00
    scheduled run, which is precisely the one nobody is watching.
    """

    def __init__(self) -> None:
        super().__init__()
        self._stream: TextIO | None = None
        self._day = ""

    def _open_for(self, day: str) -> TextIO | None:
        try:
            ensure(logs_dir())
            # Line buffered: a crash must not eat the last thing we wrote.
            return open(log_file(day), "a", encoding="utf-8", buffering=1)
        except OSError:
            return None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = _today()
            if self._stream is None or day != self._day:
                if self._stream is not None:
                    with contextlib.suppress(OSError):
                        self._stream.close()
                self._stream = self._open_for(day)
                self._day = day
            if self._stream is None:
                return  # a log that cannot be written is not worth crashing over
            self._stream.write(self.format(record) + "\n")
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with _lock:
            if self._stream is not None:
                with contextlib.suppress(OSError):
                    self._stream.close()
                self._stream = None
        super().close()


def setup(*, level: int = logging.INFO, console: bool | None = None) -> Path:
    """Attach the file handler once and return the path being written to.

    Idempotent: ``__main__`` calls it, and a test or a ``--self-check`` may call
    it again without stacking handlers.

    *console* defaults to "only when there is a console to write to". The
    packaged build is ``pythonw``-based and has no stdout at all, so an
    unconditional ``StreamHandler`` would be writing into a closed handle.
    """
    global _configured
    with _lock:
        logger = logging.getLogger(LOGGER_NAME)
        if _configured:
            return log_file()
        logger.setLevel(level)
        logger.propagate = False
        formatter = logging.Formatter(LINE_FORMAT, datefmt=TIME_FORMAT)

        handler = DailyFileHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        if console is None:
            console = sys.stderr is not None and sys.stderr.isatty()
        if console:
            stream = logging.StreamHandler()
            stream.setFormatter(formatter)
            logger.addHandler(stream)
        _configured = True
    prune()
    return log_file()


def get(name: str | None = None) -> logging.Logger:
    """``adc`` or ``adc.<name>``. Callers never touch ``logging`` directly."""
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def prune(keep_days: int = KEEP_DAYS) -> int:
    """Delete day files older than *keep_days*. Returns how many went.

    Deliberately narrow, like :func:`adc.engine.report.prune_reports`: it globs
    its own directory for its own exact ``adc-YYYYMMDD.log`` shape, parses the
    date, and skips anything that does not match. A stray file in the log
    directory is not ADC's to remove.
    """
    directory = logs_dir()
    cutoff = dt.date.today() - dt.timedelta(days=max(0, keep_days))
    removed = 0
    try:
        candidates = sorted(directory.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"))
    except OSError:
        return 0
    for path in candidates:
        stamp = path.name[len(FILE_PREFIX) : -len(FILE_SUFFIX)]
        try:
            day = dt.datetime.strptime(stamp, DATE_FORMAT).date()
        except ValueError:
            continue
        if day >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def install_excepthooks() -> None:
    """Route every unhandled exception -- main thread and workers -- to the log.

    Without this, an exception in a job worker thread prints to a stderr that the
    packaged ``pythonw`` build does not have, and the UI simply stops updating
    with no record of why. ``JobRunner`` already turns a worker exception into a
    FAILED job, so this is the backstop for everything outside a job.
    """
    log = get("crash")

    def on_exception(
        kind: type[BaseException], value: BaseException, tb: Any
    ) -> None:
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, tb)
            return
        log.critical("unhandled %s: %s", kind.__name__, value, exc_info=(kind, value, tb))

    def on_thread_exception(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        value = args.exc_value
        log.critical(
            "unhandled %s in thread %s: %s",
            args.exc_type.__name__,
            args.thread.name if args.thread else "?",
            value,
            exc_info=(args.exc_type, value, args.exc_traceback) if value else None,
        )

    sys.excepthook = on_exception
    threading.excepthook = on_thread_exception
