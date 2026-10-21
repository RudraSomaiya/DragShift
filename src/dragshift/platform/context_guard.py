"""Quick Launch gates G-a ... G-f (PRD FR-3.2): may a modifier tap open a launcher wheel here?

All gates must pass, or nothing happens. ``failed_gate`` checks the cheap ones first and the
Explorer COM selection (G-d) last, and stops at the first that fails. Anything it cannot read
counts as a failure: a launcher that does not open is better than one that opens over a game.

Called on the Qt thread when a tap completes, never inside a hook callback.
"""

import ctypes
import os
from ctypes import wintypes
from typing import Protocol

from dragshift.config import Config
from dragshift.platform.drag_detect import EXPLORER_CLASSES
from dragshift.platform.shell import selection_count

# G-b: SHQueryUserNotificationState results that mean "do not disturb"
QUNS_BUSY = 2  # a full-screen app that is not D3D (a video player, say)
QUNS_RUNNING_D3D_FULL_SCREEN = 3  # a full-screen game
QUNS_PRESENTATION_MODE = 4
BLOCKING_STATES = frozenset({QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE})

# G-c: classes of a focused text input (renaming a file, the Explorer address or search box)
TEXT_INPUT_CLASSES = frozenset({"edit", "windows.ui.input.inputsite.windowclass"})
TEXT_INPUT_PREFIX = "richedit"  # RichEdit20W, RICHEDIT50W, ...

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # OpenProcess right: enough for the image name


class ContextProbe(Protocol):
    """What the gates ask the desktop. ``Win32Context`` is the real one; tests fake it."""

    def foreground(self) -> tuple[int, str]: ...  # (root HWND, class) of the foreground window
    def focused_class(self) -> str | None: ...  # class of its focused control; None if unknown
    def notification_state(self) -> int | None: ...
    def foreground_process(self) -> str | None: ...  # full path of its executable
    def selection_count(self, hwnd: int, root_class: str) -> int | None: ...


def is_text_input(class_name: str) -> bool:
    name = class_name.lower()
    return name in TEXT_INPUT_CLASSES or name.startswith(TEXT_INPUT_PREFIX)


def is_blocked(process_path: str, blocklist: tuple[str, ...]) -> bool:
    """A blocklist entry is an executable name (``game.exe``) or a full path; case is ignored."""
    path = os.path.normcase(process_path)
    name = os.path.basename(path)
    return any(os.path.normcase(entry) in (name, path) for entry in blocklist)


def failed_gate(probe: ContextProbe, config: Config) -> str | None:
    """The first FR-3.2 gate that fails, e.g. ``"G-d"``, or ``None`` when all pass."""
    if config.general.paused or not config.launcher.enabled:
        return "G-f"  # paused, or Quick Launch switched off in Settings (FR-8.1)
    hwnd, root_class = probe.foreground()
    if root_class not in EXPLORER_CLASSES:
        return "G-a"
    focused = probe.focused_class()
    if focused is None or is_text_input(focused):
        return "G-c"
    state = probe.notification_state()
    if state is None or state in BLOCKING_STATES:
        return "G-b"
    if config.general.blocklist:
        process = probe.foreground_process()
        if process is None or is_blocked(process, config.general.blocklist):
            return "G-e"
    if probe.selection_count(hwnd, root_class) != 0:  # None: could not tell
        return "G-d"
    return None


# --- the real probe --------------------------------------------------------------------------
class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]  # fmt: skip


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]  # fmt: skip
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
_shell32.SHQueryUserNotificationState.restype = ctypes.c_long  # HRESULT

GA_ROOT = 2  # GetAncestor: the top-level window


def _class_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


class Win32Context:
    """The real answers to the gate questions, about the window that has the keyboard now."""

    def foreground(self) -> tuple[int, str]:
        fg = _user32.GetForegroundWindow()
        root = _user32.GetAncestor(fg, GA_ROOT) if fg else None
        return (int(root), _class_of(root)) if root else (0, "")

    def focused_class(self) -> str | None:
        fg = _user32.GetForegroundWindow()
        if not fg:
            return None
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        tid = _user32.GetWindowThreadProcessId(fg, None)
        if not _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return None
        return _class_of(info.hwndFocus) if info.hwndFocus else ""  # "": nothing focused

    def notification_state(self) -> int | None:
        state = ctypes.c_int()
        ok = _shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0  # S_OK
        return state.value if ok else None

    def foreground_process(self) -> str | None:
        fg = _user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        if not fg or not _user32.GetWindowThreadProcessId(fg, ctypes.byref(pid)):
            return None
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return None  # e.g. an elevated process
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            return buf.value if ok else None
        finally:
            _kernel32.CloseHandle(handle)

    def selection_count(self, hwnd: int, root_class: str) -> int | None:
        return selection_count(hwnd, root_class)
