"""Is this a file drag? (FR-1.2) plus the Win32 probes the gesture state machine needs.

FR-1.2: a modifier key-down during a drag candidate confirms a file drag if either
  (a) a visible ``SysDragImage`` window exists (Explorer's drag image), or
  (b) the root window under the cursor at LMB-down was ``CabinetWClass``, ``Progman`` or ``WorkerW``.
The DS-4 spike measured (a) as reliable and (b) as permissive: it passes for any drag that starts
in Explorer, such as a rubber-band selection.

Everything here is called on the Qt thread, never inside a hook callback.
"""

import ctypes
from ctypes import wintypes

EXPLORER_CLASSES = frozenset({"CabinetWClass", "Progman", "WorkerW"})

# Win32 constants
SM_CXDRAG = 68  # GetSystemMetrics: drag threshold in pixels (x)
SM_CYDRAG = 69  # ... (y)
GA_ROOT = 2  # GetAncestor: the top-level window
KEY_DOWN_BIT = 0x8000  # GetAsyncKeyState: the key is down now
# Left/right variants, as the low-level keyboard hook reports them: Shift, Ctrl, Alt
MODIFIER_VKS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short


def is_file_drag(press_root_class: str, sys_drag_image_visible: bool) -> bool:
    """FR-1.2: gate (a) or gate (b)."""
    return sys_drag_image_visible or press_root_class in EXPLORER_CLASSES


class Win32Probe:
    """The real answers to the questions the gesture controller asks about the desktop."""

    def drag_threshold(self) -> tuple[int, int]:
        """Pixels the cursor must move with LMB down before it counts as a drag (FR-1.1)."""
        return _user32.GetSystemMetrics(SM_CXDRAG), _user32.GetSystemMetrics(SM_CYDRAG)

    def root_at(self, x: int, y: int) -> tuple[int, str]:
        """(HWND, class name) of the top-level window under the physical point, else (0, "")."""
        hwnd = _user32.WindowFromPoint(wintypes.POINT(x, y))
        root = _user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
        if not root:
            return 0, ""
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(root, buf, 256)
        return int(root), buf.value

    def sys_drag_image_visible(self) -> bool:
        """FR-1.2 gate (a). Check it when the key goes down; it can vanish a moment later."""
        hwnd = _user32.FindWindowW("SysDragImage", None)
        return bool(hwnd) and bool(_user32.IsWindowVisible(hwnd))

    def held_modifier_vks(self) -> frozenset[int]:
        """Modifier keys physically down right now (for "already held before the drag")."""
        return frozenset(vk for vk in MODIFIER_VKS if _user32.GetAsyncKeyState(vk) & KEY_DOWN_BIT)
