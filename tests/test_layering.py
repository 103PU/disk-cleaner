"""The layering rule, as a test: ``adc.engine`` knows nothing about the UI.

docs/03-PLAN.md P1 states the constraint and asks for it to be checked by an
import test. The point is not tidiness -- it is that the engine must stay
importable and testable in a plain console process, with no WebView2 runtime and
no ``pythonnet``. The moment one engine module imports ``webview``, the whole
test suite starts depending on a GUI stack, and a headless CI run stops being
possible.

``ast`` rather than a text grep: a grep matches the word inside a docstring or a
comment (this file included) and would either miss a real ``import`` written
oddly or fail on prose that merely mentions the name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Anything the engine must not reach for. ``tkinter`` and ``PySide6`` are here so
# that a future "just pop up a quick dialog" cannot slip into the engine either.
FORBIDDEN_ROOTS = frozenset({"webview", "tkinter", "PySide6", "PyQt5", "PyQt6", "wx"})

# The engine may not import the layers above it, by name or relatively. Held as
# bare subpackage names because that is what survives both spellings: ``adc.shell``
# and ``from ..shell import bridge`` have to be caught by one rule, and the
# absolute form is the one a grep would find while the relative form is the one
# somebody actually writes from inside ``adc.engine``.
FORBIDDEN_SIBLINGS = frozenset({"ui", "app", "bridge", "shell"})
FORBIDDEN_ADC = frozenset(f"adc.{name}" for name in FORBIDDEN_SIBLINGS)


def _reaches_upward(name: str) -> bool:
    """Whether an import name names a sibling package above the engine.

    Three spellings to cover: ``adc.shell.bridge``, ``..shell`` (from a module in
    ``adc.engine``), and ``...shell`` if the file ever moves a level deeper.
    """
    if name.startswith("."):
        head = name.lstrip(".").split(".")[0]
        return head in FORBIDDEN_SIBLINGS
    parts = name.split(".")
    return len(parts) >= 2 and parts[0] == "adc" and parts[1] in FORBIDDEN_SIBLINGS


def _engine_files(repo_root: Path) -> list[Path]:
    engine = repo_root / "src" / "adc" / "engine"
    assert engine.is_dir(), f"engine package not found at {engine}"
    return sorted(p for p in engine.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Every module name this file imports, with the line it is on.

    ``from x import y`` yields ``x``; ``import a.b`` yields ``a.b``. Relative
    imports yield a leading-dot form so a sibling package can be spotted.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            found.append((f"{prefix}{node.module or ''}", node.lineno))
    return found


def test_engine_has_files_to_check(repo_root: Path) -> None:
    """Guards against a passing suite that checked nothing."""
    files = _engine_files(repo_root)

    assert len(files) >= 6, f"expected the P1 engine modules, found {len(files)}"


def test_engine_does_not_import_the_ui(repo_root: Path) -> None:
    offences: list[str] = []
    for path in _engine_files(repo_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, lineno in _imported_names(tree):
            root = name.lstrip(".").split(".")[0]
            if root in FORBIDDEN_ROOTS or _reaches_upward(name):
                offences.append(f"{path.name}:{lineno} imports {name}")

    assert offences == [], "engine reached into the UI layer: " + "; ".join(offences)


def test_engine_imports_are_stdlib_or_sibling(repo_root: Path) -> None:
    """No third-party dependency in the engine at all, beyond the stdlib.

    Deliberately strict. Everything the engine needs on Windows is reachable
    through ``ctypes``, and keeping it that way is what makes the PyInstaller
    bundle small and the import graph predictable.
    """
    allowed_third_party: frozenset[str] = frozenset()
    stdlib = frozenset(
        {
            "__future__", "ast", "collections", "collections.abc", "contextlib",
            "csv", "ctypes", "dataclasses", "datetime", "enum", "errno", "fnmatch",
            "functools", "glob", "hashlib", "io", "itertools", "json", "logging", "math", "os",
            "os.path", "pathlib", "platform", "re", "shutil", "sqlite3", "stat",
            "string", "subprocess", "sys", "tempfile", "threading", "time",
            "traceback", "types", "typing", "urllib", "uuid", "warnings", "winreg",
            "ctypes.wintypes", "logging.handlers", "concurrent.futures",
        }
    )
    strays: list[str] = []
    for path in _engine_files(repo_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, lineno in _imported_names(tree):
            if name.startswith("."):
                # A relative import inside the engine. Which sibling it names is
                # the other test's job (``_reaches_upward``); this one is only
                # asking whether a third-party package crept in, and a relative
                # import can never be one.
                continue
            if name.startswith("adc.engine"):
                continue
            root = name.split(".")[0]
            if name in stdlib or root in stdlib or root in allowed_third_party:
                continue
            strays.append(f"{path.name}:{lineno} imports {name}")

    assert strays == [], "engine grew a dependency: " + "; ".join(strays)


@pytest.mark.parametrize(
    "module",
    ["fsutil", "guard", "jobs", "paths", "report", "volumes", "schedule", "updater"],
)
def test_engine_module_imports_without_a_gui_stack(module: str) -> None:
    """Each module imports on its own, in this plain process. No WebView2 here."""
    __import__(f"adc.engine.{module}")


def test_the_bridge_imports_without_a_gui_stack() -> None:
    """``adc.shell.bridge`` is testable in this process too, and deliberately so.

    The bridge is where JavaScript's arguments are validated, which is the part
    most worth having tests for. If it imported ``webview`` at module scope, every
    bridge test would need a WebView2 runtime -- so the window is a separate
    module and this import is the check that it stayed that way.
    """
    __import__("adc.shell.bridge")


def test_the_bridge_does_not_import_webview(repo_root: Path) -> None:
    offences: list[str] = []
    path = repo_root / "src" / "adc" / "shell" / "bridge.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for name, lineno in _imported_names(tree):
        if name.lstrip(".").split(".")[0] in FORBIDDEN_ROOTS:
            offences.append(f"bridge.py:{lineno} imports {name}")

    assert offences == [], "the bridge grew a GUI dependency: " + "; ".join(offences)
