"""Thin ctypes wrappers over the Win32 / Shell APIs the engine needs.

Every function here is a leaf. It converts Python values into Win32 calling
conventions, checks the failure signal the API actually documents, and raises
``OSError`` carrying the real ``GetLastError`` code. There is no policy in this
module: it never decides *whether* a path may be touched -- ``guard.py`` owns
that -- it only knows *how* to ask Windows.

Why ctypes rather than pywin32: the frozen bundle has to stay small and the
spike measured a 29 MB onedir with the stdlib only (docs/02-SPEC.md 3.3).
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

# --- SHFileOperationW ------------------------------------------------------
FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_NOCONFIRMMKDIR = 0x0200
FOF_WANTNUKEWARNING = 0x4000

# --- SHEmptyRecycleBinW ----------------------------------------------------
SHERB_NOCONFIRMATION = 0x0001
SHERB_NOPROGRESSUI = 0x0002
SHERB_NOSOUND = 0x0004

# --- MoveFileExW -----------------------------------------------------------
MOVEFILE_REPLACE_EXISTING = 0x0001
MOVEFILE_DELAY_UNTIL_REBOOT = 0x0004

# --- ShellExecuteExW -------------------------------------------------------
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400
SW_SHOWNORMAL = 1
SW_RESTORE = 9
ERROR_CANCELLED = 1223

# --- Restart Manager -------------------------------------------------------
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
RM_REBOOT_REASON_NONE = 0
ERROR_MORE_DATA = 234

# --- DPI -------------------------------------------------------------------
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

# GetCompressedFileSizeW returns this on failure.
INVALID_FILE_SIZE = 0xFFFFFFFF


class UnsupportedPlatformError(RuntimeError):
    """Raised when a Win32-only helper is called on a non-Windows host."""


def _require_windows(what: str) -> None:
    if not IS_WINDOWS:
        raise UnsupportedPlatformError(f"{what} needs Windows")


def _kernel32() -> ctypes.WinDLL:
    _require_windows("kernel32")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _shell32() -> ctypes.WinDLL:
    _require_windows("shell32")
    return ctypes.WinDLL("shell32", use_last_error=True)


def _rstrtmgr() -> ctypes.WinDLL:
    _require_windows("rstrtmgr")
    return ctypes.WinDLL("rstrtmgr", use_last_error=True)


def _raise_last_error(func_name: str, path: str | None = None) -> None:
    code = ctypes.get_last_error()
    raise ctypes.WinError(code, f"{func_name} failed" + (f": {path}" if path else ""))


def long_path(path: str) -> str:
    r"""Prefix with \\?\ so the API is not capped at MAX_PATH.

    Node and .NET caches routinely blow past 260 characters, which is where v1's
    walker started reporting silent zeroes. The prefix requires an absolute path
    with no forward slashes and no relative components, hence the abspath first.
    """
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute.lstrip("\\")
    return "\\\\?\\" + absolute


# ---------------------------------------------------------------------------
# Size on disk
# ---------------------------------------------------------------------------
def get_size_on_disk(path: str) -> int | None:
    """Allocated size of *path* in bytes, or ``None`` if Windows would not say.

    Fixes BUG-12. ``st_size`` is the logical length; for an NTFS-compressed or
    sparse file the space actually reclaimed by deleting it is this number
    instead, and the two can differ by an order of magnitude (WSL2 ``ext4.vhdx``
    is the case that matters here).

    ``None`` -- not an exception and not zero -- is deliberate: the caller must
    be able to fall back to ``st_size`` rather than silently report 0 B.
    """
    _require_windows("GetCompressedFileSizeW")
    k32 = _kernel32()
    k32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, wintypes.LPDWORD]
    k32.GetCompressedFileSizeW.restype = wintypes.DWORD

    high = wintypes.DWORD(0)
    ctypes.set_last_error(0)
    low = k32.GetCompressedFileSizeW(long_path(path), ctypes.byref(high))
    if low == INVALID_FILE_SIZE and ctypes.get_last_error() != 0:
        return None
    return (high.value << 32) | int(low)


# ---------------------------------------------------------------------------
# Recycle Bin
# ---------------------------------------------------------------------------
class SHFILEOPSTRUCTW(ctypes.Structure):
    """Layout per shellapi.h. ``fAnyOperationsAborted`` is what tells us the
    user (or the shell) stopped half way, which we must report as a partial
    result rather than as success."""

    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    )


@dataclass(frozen=True)
class ShellOpResult:
    """Outcome of one SHFileOperationW batch."""

    ok: bool
    aborted: bool
    code: int


def recycle_delete(paths: list[str], *, show_progress: bool = False) -> ShellOpResult:
    r"""Move *paths* to the Recycle Bin via the shell, so the user can undo.

    This is the default strategy for anything inside the user profile
    (docs/02-SPEC.md 4.5). ``pFrom`` is a double-NUL-terminated list, and the
    shell refuses ``\\?\`` prefixed paths here -- SHFileOperation is one of the
    APIs that does its own parsing -- so we pass plain absolute paths and accept
    the MAX_PATH limit for this strategy only. HardDelete handles the long ones.
    """
    _require_windows("SHFileOperationW")
    if not paths:
        return ShellOpResult(ok=True, aborted=False, code=0)

    sh = _shell32()
    sh.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    sh.SHFileOperationW.restype = ctypes.c_int

    joined = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
    flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOCONFIRMMKDIR | FOF_NOERRORUI
    if not show_progress:
        flags |= FOF_SILENT

    op = SHFILEOPSTRUCTW(
        hwnd=None,
        wFunc=FO_DELETE,
        pFrom=joined,
        pTo=None,
        fFlags=flags,
        fAnyOperationsAborted=False,
        hNameMappings=None,
        lpszProgressTitle=None,
    )
    code = sh.SHFileOperationW(ctypes.byref(op))
    return ShellOpResult(ok=code == 0, aborted=bool(op.fAnyOperationsAborted), code=int(code))


def empty_recycle_bin(drive: str | None = None) -> None:
    """Empty the Recycle Bin for *drive* (all drives when ``None``).

    v1 shelled out to PowerShell ``Clear-RecycleBin``, which spawns a console
    window and fails silently when the bin is already empty. The API returns
    S_OK for an empty bin, so no special case is needed.
    """
    _require_windows("SHEmptyRecycleBinW")
    sh = _shell32()
    sh.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
    sh.SHEmptyRecycleBinW.restype = ctypes.c_long

    flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    hr = sh.SHEmptyRecycleBinW(None, drive, flags)
    # S_OK, or E_UNEXPECTED (0x8000FFFF) which the shell also returns for
    # "nothing to do" on some builds. Anything else is a real failure.
    if hr not in (0, -2147418113):
        raise OSError(f"SHEmptyRecycleBinW returned 0x{hr & 0xFFFFFFFF:08X}")


# ---------------------------------------------------------------------------
# Locked files
# ---------------------------------------------------------------------------
def delete_on_reboot(path: str) -> None:
    """Queue *path* for deletion at the next boot.

    Opt-in only (docs/02-SPEC.md 4.6 item 2). Needs SeCreatePermanentPrivilege,
    i.e. Administrator; unelevated callers get ERROR_ACCESS_DENIED, which is
    surfaced as OSError rather than swallowed.
    """
    _require_windows("MoveFileExW")
    k32 = _kernel32()
    k32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    k32.MoveFileExW.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    if not k32.MoveFileExW(long_path(path), None, MOVEFILE_DELAY_UNTIL_REBOOT):
        _raise_last_error("MoveFileExW", path)


class RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = (("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME))


class RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = (
        ("Process", RM_UNIQUE_PROCESS),
        ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", ctypes.c_uint),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    )


@dataclass(frozen=True)
class LockingProcess:
    """One process the Restart Manager says is holding a file open."""

    pid: int
    name: str
    service_name: str
    restartable: bool


def who_locks(paths: list[str], *, limit: int = 64) -> list[LockingProcess]:
    """Name the processes holding *paths* open, via the Restart Manager.

    This is the upgrade over v1, which counted locked files and reported a bare
    number the user could do nothing with (docs/02-SPEC.md 4.6). We never kill
    anything -- the caller shows "Chrome is holding 142 files" and stops there.

    Returns an empty list when nothing holds the files, or when the Restart
    Manager itself is unavailable: a diagnostic must never break a clean.
    """
    _require_windows("RmStartSession")
    if not paths:
        return []

    rm = _rstrtmgr()
    session = wintypes.DWORD(0)
    key = (wintypes.WCHAR * 256)()
    if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return []

    try:
        n = min(len(paths), limit)
        arr = (wintypes.LPCWSTR * n)(*[os.path.abspath(p) for p in paths[:n]])
        if rm.RmRegisterResources(session, n, arr, 0, None, 0, None) != 0:
            return []

        needed = ctypes.c_uint(0)
        have = ctypes.c_uint(0)
        reason = wintypes.DWORD(0)
        rc = rm.RmGetList(
            session, ctypes.byref(needed), ctypes.byref(have), None, ctypes.byref(reason)
        )
        if rc not in (0, ERROR_MORE_DATA) or needed.value == 0:
            return []

        have = ctypes.c_uint(needed.value)
        info = (RM_PROCESS_INFO * needed.value)()
        rc = rm.RmGetList(
            session, ctypes.byref(needed), ctypes.byref(have), info, ctypes.byref(reason)
        )
        if rc != 0:
            return []

        return [
            LockingProcess(
                pid=int(info[i].Process.dwProcessId),
                name=str(info[i].strAppName),
                service_name=str(info[i].strServiceShortName),
                restartable=bool(info[i].bRestartable),
            )
            for i in range(have.value)
        ]
    finally:
        rm.RmEndSession(session)


# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------
def is_user_an_admin() -> bool:
    """True when this process is already running elevated.

    v2.0 runs unelevated by design and disables admin-only targets rather than
    demanding a UAC prompt at startup (docs/02-SPEC.md 2.1).
    """
    if not IS_WINDOWS:
        return False
    try:
        sh = _shell32()
        sh.IsUserAnAdmin.restype = wintypes.BOOL
        return bool(sh.IsUserAnAdmin())
    except OSError:
        return False


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    )


def relaunch_as_admin(exe: str, params: str = "", cwd: str | None = None) -> bool:
    """Restart *exe* elevated with the ``runas`` verb.

    Returns False -- not an exception -- when the user dismisses the UAC prompt,
    because a declined prompt is a normal answer and the app must carry on
    unelevated. Any other shell failure raises.
    """
    _require_windows("ShellExecuteExW")
    sh = _shell32()
    sh.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    sh.ShellExecuteExW.restype = wintypes.BOOL

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params or None
    info.lpDirectory = cwd
    info.nShow = SW_SHOWNORMAL

    ctypes.set_last_error(0)
    if not sh.ShellExecuteExW(ctypes.byref(info)):
        if ctypes.get_last_error() == ERROR_CANCELLED:
            return False
        _raise_last_error("ShellExecuteExW", exe)
    return True


def enable_per_monitor_dpi() -> bool:
    """Declare per-monitor-v2 DPI awareness before any window is created.

    The spike opened a 520x340 window at 646x414 on this 1.25x display; the
    manifest is the real fix for the frozen build, and this call covers running
    from source. Returns False when the OS is too old to know the context.
    """
    if not IS_WINDOWS:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        return bool(
            user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        )
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------
ERROR_ALREADY_EXISTS = 183


def create_single_instance_mutex(name: str) -> tuple[int | None, bool]:
    r"""Take a named mutex. Returns ``(handle, we_are_first)``.

    ``Local\`` scope, not ``Global\``: the mutex must be per-session, so that a
    second desktop session -- Fast User Switching, or a service account -- gets
    its own instance rather than being told one is already running by a window it
    cannot see.

    The handle is returned even when *we_are_first* is False, because the loser
    still has to close it. It is deliberately not closed here: the mutex lives as
    long as the handle does, and releasing it early would let a second instance
    through while the first is still starting up.
    """
    _require_windows("CreateMutexW")
    k32 = _kernel32()
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE

    ctypes.set_last_error(0)
    handle = k32.CreateMutexW(None, False, name)
    last = ctypes.get_last_error()
    if not handle:
        _raise_last_error("CreateMutexW", name)
    return int(handle), last != ERROR_ALREADY_EXISTS


def close_handle(handle: int) -> bool:
    """Release a Win32 handle. False rather than raising -- this runs on shutdown."""
    if not IS_WINDOWS:
        return False
    try:
        k32 = _kernel32()
    except UnsupportedPlatformError:
        return False
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return bool(k32.CloseHandle(wintypes.HANDLE(handle)))


def focus_window(title: str) -> bool:
    """Bring the window with this exact title to the front. False if not found.

    What the second instance calls before exiting, so that double-clicking the
    shortcut behaves like every other Windows app instead of silently doing
    nothing. ``SetForegroundWindow`` is allowed here because the caller is a
    process the user just started, which is one of the cases Windows permits.
    """
    if not IS_WINDOWS:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        return False
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    return bool(user32.SetForegroundWindow(hwnd))


# ---------------------------------------------------------------------------
# WebView2
# ---------------------------------------------------------------------------
#: Where the evergreen runtime records its version (docs/02-SPEC.md:617). The
#: GUID is the WebView2 Runtime's own EdgeUpdate client id, not a machine id.
WEBVIEW2_CLIENT_KEY = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
    r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)


def webview2_version() -> str | None:
    """The installed WebView2 Runtime version, or ``None`` when absent.

    Read from the registry rather than by loading ``WebView2Loader.dll``, because
    this has to answer *before* a window exists -- ``--self-check`` reports it,
    and the installer's bootstrapper decision depends on it.

    Per-machine first, then per-user: an evergreen runtime installed by another
    app is usually per-machine, but a per-user install is a valid state and an app
    that failed to see it would demand a redundant download.
    """
    if not IS_WINDOWS:
        return None
    import winreg

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, WEBVIEW2_CLIENT_KEY) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6


def logical_drives() -> list[str]:
    r"""Every mounted root, as ``C:\``, ``D:\``, ...

    ``GetLogicalDriveStringsW`` over ``string.ascii_uppercase`` guessing: the API
    reports what is actually mounted, including drives without a letter's worth
    of media, and never touches the hardware -- which matters because probing an
    empty optical drive spins it up.
    """
    _require_windows("GetLogicalDriveStringsW")
    k32 = _kernel32()
    k32.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    k32.GetLogicalDriveStringsW.restype = wintypes.DWORD

    needed = k32.GetLogicalDriveStringsW(0, None)
    if needed == 0:
        _raise_last_error("GetLogicalDriveStringsW")
    buf = ctypes.create_unicode_buffer(needed)
    written = k32.GetLogicalDriveStringsW(needed, buf)
    if written == 0:
        _raise_last_error("GetLogicalDriveStringsW")
    # The buffer is a NUL-separated, double-NUL-terminated list. Slicing a
    # unicode buffer yields a str at runtime; the ctypes stub says list[str],
    # and join is correct under either reading.
    listing = "".join(buf[:written])
    return [part for part in listing.split("\0") if part]


def drive_type(root: str) -> int:
    """One of the ``DRIVE_*`` constants. Never raises; an unknown drive is 0."""
    if not IS_WINDOWS:
        return DRIVE_UNKNOWN
    try:
        k32 = _kernel32()
    except UnsupportedPlatformError:
        return DRIVE_UNKNOWN
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT
    return int(k32.GetDriveTypeW(root))


def volume_info(root: str) -> tuple[str, str]:
    """``(label, filesystem)`` for *root*; empty strings when Windows declines.

    A missing label is normal (an unlabelled data disk), so this reports it as
    empty rather than raising -- the caller shows the drive letter instead.
    """
    _require_windows("GetVolumeInformationW")
    k32 = _kernel32()
    k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD,
        wintypes.LPWSTR, wintypes.DWORD,
    ]
    k32.GetVolumeInformationW.restype = wintypes.BOOL

    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    ok = k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
    if not ok:
        return "", ""
    return label.value, fs.value
