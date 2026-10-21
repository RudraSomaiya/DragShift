"""Give a launcher wheel the keyboard (FR-3.5: Quick Launch wheels may take focus for 1-8 / Esc).

Windows only lets the process that owns the foreground window move it. When a tap opens the
launcher, that is Explorer, not us (low-level hooks do not count as "received the last input").
For the moment of the call this thread shares the foreground thread's input state, which makes
SetForegroundWindow allowed, and then lets go again.
"""

import ctypes
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.argtypes = [wintypes.HWND]


def bring_to_front(hwnd: int) -> bool:
    """Make ``hwnd`` the foreground window. Returns False if Windows still refused."""
    if _user32.GetForegroundWindow() == hwnd:
        return True
    ours = _kernel32.GetCurrentThreadId()
    theirs = _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), None)
    attached = bool(theirs) and theirs != ours and _user32.AttachThreadInput(ours, theirs, True)
    try:
        _user32.BringWindowToTop(hwnd)
        return bool(_user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            _user32.AttachThreadInput(ours, theirs, False)
