r"""Audit log tests. The one invariant that matters more than the others: a
logging failure must never become the reason the cleaner crashed.

This module owns process-wide state -- the root ``adc`` logger's handlers and
``sys.excepthook`` / ``threading.excepthook`` -- so every test that calls
``setup()`` or ``install_excepthooks()`` runs under an autouse fixture that
snapshots and restores all of it. Without that, one red test here would
corrupt every test file that runs after it in the same process.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from adc.engine import audit


@pytest.fixture(autouse=True)
def _isolated_logging_state() -> Iterator[None]:
    """Snapshot and restore everything this module mutates globally.

    ``audit._configured`` gates ``setup()``'s idempotency, the ``adc`` logger
    keeps its own handler list and level across the whole process, and
    ``install_excepthooks`` overwrites ``sys.excepthook`` /
    ``threading.excepthook``. Any test that leaves one of those changed would
    poison whichever test file pytest happens to collect next.
    """
    logger = logging.getLogger(audit.LOGGER_NAME)
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    configured = audit._configured
    excepthook = sys.excepthook
    thread_excepthook = threading.excepthook

    yield

    for handler in logger.handlers:
        if handler not in handlers:
            with contextlib.suppress(Exception):
                handler.close()
    logger.handlers = handlers
    logger.setLevel(level)
    logger.propagate = propagate
    audit._configured = configured
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------
def test_get_returns_the_root_logger_by_default() -> None:
    assert audit.get() is logging.getLogger("adc")


def test_get_namespaces_under_the_root_logger() -> None:
    assert audit.get("crash") is logging.getLogger("adc.crash")


def test_get_does_not_stack_handlers_across_repeated_calls(data_dir: Path) -> None:
    """Logging a line through ``get()`` twice must not double-write it."""
    audit.setup(console=False)
    log_path = audit.log_file()

    audit.get().info("một dòng")
    audit.get().info("một dòng")

    lines = [line for line in _read_lines(log_path) if "một dòng" in line]
    assert len(lines) == 2, "two calls, two lines -- not a handler-stacking artefact"
    assert len(logging.getLogger(audit.LOGGER_NAME).handlers) == 1


# ---------------------------------------------------------------------------
# setup() -- idempotency and where the file lands
# ---------------------------------------------------------------------------
def test_setup_returns_todays_log_file_under_the_data_dir(data_dir: Path) -> None:
    path = audit.setup(console=False)

    assert path == data_dir / "logs" / f"adc-{audit._today()}.log"


def test_setup_creates_the_logs_directory_if_missing(data_dir: Path) -> None:
    assert not data_dir.exists()

    path = audit.setup(console=False)
    audit.get().info("bootstrap")

    assert path.parent.is_dir()
    assert path.is_file()


def test_setup_called_twice_does_not_stack_handlers(data_dir: Path) -> None:
    """``__main__`` calls this once; a ``--self-check`` might call it again."""
    audit.setup(console=False)
    audit.setup(console=False)

    logger = logging.getLogger(audit.LOGGER_NAME)
    assert len(logger.handlers) == 1


def test_setup_called_twice_does_not_double_write_a_line(data_dir: Path) -> None:
    audit.setup(console=False)
    audit.setup(console=False)
    audit.get().info("chỉ một lần")

    lines = [line for line in _read_lines(audit.log_file()) if "chỉ một lần" in line]
    assert len(lines) == 1


def test_setup_sets_the_requested_level(data_dir: Path) -> None:
    audit.setup(level=logging.WARNING, console=False)

    assert logging.getLogger(audit.LOGGER_NAME).level == logging.WARNING


def test_setup_does_not_propagate_to_the_root_logger(data_dir: Path) -> None:
    """A duplicate line in pytest's own log capture would mean this broke."""
    audit.setup(console=False)

    assert logging.getLogger(audit.LOGGER_NAME).propagate is False


def test_setup_with_console_false_adds_no_stream_handler(data_dir: Path) -> None:
    audit.setup(console=False)

    handlers = logging.getLogger(audit.LOGGER_NAME).handlers
    assert not any(isinstance(h, logging.StreamHandler) for h in handlers)


def test_setup_with_console_true_adds_a_stream_handler(data_dir: Path) -> None:
    audit.setup(console=True)

    handlers = logging.getLogger(audit.LOGGER_NAME).handlers
    stream_handlers = [
        h for h in handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, audit.DailyFileHandler)
    ]
    assert len(stream_handlers) == 1


def test_setup_prunes_old_log_files(data_dir: Path) -> None:
    """SPEC 3.2: seven days kept. ``setup`` runs ``prune`` on the way out."""
    logs = data_dir / "logs"
    logs.mkdir(parents=True)
    old_day = (audit.dt.date.today() - audit.dt.timedelta(days=audit.KEEP_DAYS + 1))
    stale = logs / f"adc-{old_day.strftime(audit.DATE_FORMAT)}.log"
    stale.write_text("old", encoding="utf-8")

    audit.setup(console=False)

    assert not stale.exists()


# ---------------------------------------------------------------------------
# A log line actually lands on disk, in UTF-8
# ---------------------------------------------------------------------------
def test_a_log_line_lands_in_the_expected_file(data_dir: Path) -> None:
    audit.setup(console=False)
    audit.get("job").info("dọn xong pnpm_cache")

    lines = _read_lines(audit.log_file())
    assert any("dọn xong pnpm_cache" in line for line in lines)
    assert any("adc.job" in line for line in lines)


def test_vietnamese_message_round_trips_as_utf8(data_dir: Path) -> None:
    """The most likely real bug on a Windows console: a non-ASCII message
    must reach the file without ``UnicodeEncodeError`` and read back intact.
    """
    audit.setup(console=False)
    message = "đã xóa 42 tệp, giải phóng 1,2 GB -- không có lỗi"

    audit.get().info(message)

    content = audit.log_file().read_text(encoding="utf-8")
    assert message in content


# ---------------------------------------------------------------------------
# DailyFileHandler -- rolling to a new day's file
# ---------------------------------------------------------------------------
def test_handler_reopens_when_the_day_changes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 02:00 scheduled run is exactly the case nobody is watching."""
    handler = audit.DailyFileHandler()
    record = logging.LogRecord("adc", logging.INFO, __file__, 0, "day one", None, None)
    handler.emit(record)
    first_day = handler._day
    first_stream = handler._stream

    monkeypatch.setattr(audit, "_today", lambda: "20991231")
    record2 = logging.LogRecord("adc", logging.INFO, __file__, 0, "day two", None, None)
    handler.emit(record2)

    assert handler._day == "20991231"
    assert handler._day != first_day
    assert handler._stream is not first_stream
    assert audit.log_file("20991231").is_file()
    handler.close()


def test_handler_close_releases_the_file(data_dir: Path) -> None:
    """Windows refuses to remove an open file; ``close`` must let it go."""
    handler = audit.DailyFileHandler()
    record = logging.LogRecord("adc", logging.INFO, __file__, 0, "line", None, None)
    handler.emit(record)
    path = audit.log_file()
    assert path.is_file()

    handler.close()

    path.unlink()  # would raise PermissionError if the handle were still open
    assert not path.exists()


# ---------------------------------------------------------------------------
# Graceful degradation: a log that cannot be written must not crash the caller
# ---------------------------------------------------------------------------
def test_setup_does_not_raise_when_the_data_dir_is_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ADC_DATA_DIR`` pointed at a file, not a directory: a cleaner that
    cannot log must still run.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("ADC_DATA_DIR", str(blocker))

    path = audit.setup(console=False)  # must not raise
    audit.get().info("this line has nowhere to go")  # must not raise either

    assert path == blocker / "logs" / f"adc-{audit._today()}.log"


def test_handler_emit_returns_quietly_when_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct unit check of the same failure, at the handler level."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("ADC_DATA_DIR", str(blocker))

    handler = audit.DailyFileHandler()
    record = logging.LogRecord("adc", logging.INFO, __file__, 0, "lost", None, None)

    handler.emit(record)  # must not raise

    assert handler._stream is None


# ---------------------------------------------------------------------------
# prune()
# ---------------------------------------------------------------------------
def test_prune_removes_only_files_older_than_keep_days(data_dir: Path) -> None:
    logs = data_dir / "logs"
    logs.mkdir(parents=True)
    today = audit.dt.date.today()
    old = today - audit.dt.timedelta(days=audit.KEEP_DAYS + 1)
    recent = today - audit.dt.timedelta(days=1)
    old_file = logs / f"adc-{old.strftime(audit.DATE_FORMAT)}.log"
    recent_file = logs / f"adc-{recent.strftime(audit.DATE_FORMAT)}.log"
    old_file.write_text("old", encoding="utf-8")
    recent_file.write_text("recent", encoding="utf-8")

    removed = audit.prune()

    assert removed == 1
    assert not old_file.exists()
    assert recent_file.exists()


def test_prune_ignores_files_outside_its_own_exact_shape(data_dir: Path) -> None:
    """A stray file in the log directory is not ADC's to remove."""
    logs = data_dir / "logs"
    logs.mkdir(parents=True)
    stray = logs / "notes.txt"
    stray.write_text("keep me", encoding="utf-8")
    garbage = logs / "adc-notaday.log"
    garbage.write_text("keep me too", encoding="utf-8")

    removed = audit.prune(keep_days=0)

    assert removed == 0
    assert stray.exists()
    assert garbage.exists()


def test_prune_on_a_missing_directory_is_zero(data_dir: Path) -> None:
    assert audit.prune() == 0


# ---------------------------------------------------------------------------
# install_excepthooks()
# ---------------------------------------------------------------------------
def test_main_thread_exception_reaches_the_log(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Invoked directly against a constructed exception, never by crashing
    the interpreter for real.
    """
    audit.install_excepthooks()

    with caplog.at_level(logging.INFO, logger=audit.LOGGER_NAME):
        try:
            raise ValueError("boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())

    assert any(
        r.name == "adc.crash" and "unhandled ValueError: boom" in r.message
        for r in caplog.records
    )


def test_main_thread_keyboardinterrupt_is_not_logged_but_forwarded(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ctrl+C must still terminate normally, not vanish into the audit log."""
    audit.install_excepthooks()
    forwarded: list[BaseException] = []
    monkeypatch.setattr(
        sys, "__excepthook__", lambda kind, value, tb: forwarded.append(value)
    )

    with caplog.at_level(logging.INFO, logger=audit.LOGGER_NAME):
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            sys.excepthook(*sys.exc_info())

    assert len(forwarded) == 1
    assert isinstance(forwarded[0], KeyboardInterrupt)
    assert not any("KeyboardInterrupt" in r.message for r in caplog.records)


def test_thread_exception_reaches_the_log(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    audit.install_excepthooks()

    with caplog.at_level(logging.INFO, logger=audit.LOGGER_NAME):
        try:
            raise RuntimeError("worker fell over")
        except RuntimeError:
            exc_type, exc_value, tb = sys.exc_info()
        args = threading.ExceptHookArgs(
            (exc_type, exc_value, tb, threading.current_thread())
        )
        threading.excepthook(args)

    assert any(
        r.name == "adc.crash"
        and "unhandled RuntimeError in thread" in r.message
        and "worker fell over" in r.message
        for r in caplog.records
    )


def test_thread_systemexit_is_not_logged(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A worker calling ``sys.exit()`` is an ordinary stop, not a crash."""
    audit.install_excepthooks()

    with caplog.at_level(logging.INFO, logger=audit.LOGGER_NAME):
        args = threading.ExceptHookArgs(
            (SystemExit, SystemExit(), None, threading.current_thread())
        )
        threading.excepthook(args)

    assert caplog.records == []


def test_thread_exception_survives_a_dead_thread_reference(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``args.thread`` can be ``None``; the message must still be built."""
    audit.install_excepthooks()

    with caplog.at_level(logging.INFO, logger=audit.LOGGER_NAME):
        try:
            raise RuntimeError("orphaned")
        except RuntimeError:
            exc_type, exc_value, tb = sys.exc_info()
        args = threading.ExceptHookArgs((exc_type, exc_value, tb, None))
        threading.excepthook(args)

    assert any("in thread ?" in r.message for r in caplog.records)
