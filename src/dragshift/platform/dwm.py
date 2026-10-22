"""What Windows says about appearance: accent colour, light/dark, animation effects (§8.2, NFR-8).

Every call is cheap (one registry read or one API call), so the wheel asks again on each open and
follows a change in Windows settings without listening for one. Everything returns ``None`` (or
the Windows default) when the setting cannot be read, and the theme code applies its fallbacks.

``apply_acrylic`` sets the DWM backdrop, dark-mode and corner attributes of a dialog (§8.6).
"""

import ctypes
import sys
import winreg
from ctypes import wintypes

__all__ = [
    "CORNER_RADIUS",
    "accent_rgb",
    "animations_enabled",
    "apply_acrylic",
    "apps_use_light_theme",
]

RGB = tuple[int, int, int]

# Win32 constants
SPI_GETCLIENTAREAANIMATION = 0x1042  # SystemParametersInfoW: "Animation effects" (BOOL)
S_OK = 0
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic
DWMWCP_ROUND = 2
CORNER_RADIUS = 8  # px, what DWMWCP_ROUND draws on Windows 11
WIN11_22H2 = 22621  # first build that honours DWMWA_SYSTEMBACKDROP_TYPE
_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_DWM = r"Software\Microsoft\Windows\DWM"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    wintypes.UINT,
]
_user32.SystemParametersInfoW.restype = wintypes.BOOL
_dwmapi = ctypes.WinDLL("dwmapi")
# DwmGetColorizationColor(DWORD* 0xAARRGGBB, BOOL* opaque_blend) -> HRESULT
_dwmapi.DwmGetColorizationColor.argtypes = [
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.BOOL),
]
_dwmapi.DwmGetColorizationColor.restype = ctypes.c_long
# DwmSetWindowAttribute(HWND, DWORD attribute, LPCVOID value, DWORD size) -> HRESULT
_dwmapi.DwmSetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
# DwmExtendFrameIntoClientArea(HWND, const MARGINS*) -> HRESULT
_dwmapi.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.INT * 4)]
_dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long


def rgb_from_argb(value: int) -> RGB:
    """0xAARRGGBB (DwmGetColorizationColor) -> (r, g, b)."""
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def rgb_from_abgr(value: int) -> RGB:
    """0xAABBGGRR (the DWM ``AccentColor`` registry value) -> (r, g, b)."""
    return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF


def _read_dword(key: str, name: str) -> int | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, kind = winreg.QueryValueEx(k, name)
    except OSError:
        return None
    return value if kind == winreg.REG_DWORD else None


def _dwm_colorization() -> RGB | None:
    color, opaque = wintypes.DWORD(), wintypes.BOOL()
    if _dwmapi.DwmGetColorizationColor(ctypes.byref(color), ctypes.byref(opaque)) != S_OK:
        return None
    return rgb_from_argb(color.value)


def _registry_accent() -> RGB | None:
    value = _read_dword(_DWM, "AccentColor")
    return None if value is None else rgb_from_abgr(value)


def accent_rgb() -> RGB | None:
    """The Windows accent colour (§9.6): the DWM ``AccentColor`` registry value, else
    ``DwmGetColorizationColor``. The API can return a darker shade than the accent the user
    picked (measured: #5E0074 for an #680081 accent), so it is only the fallback."""
    return _registry_accent() or _dwm_colorization()


def apps_use_light_theme() -> bool | None:
    """``AppsUseLightTheme`` (§8.2): True for light, False for dark, None if it is not set."""
    value = _read_dword(_PERSONALIZE, "AppsUseLightTheme")
    return None if value is None else bool(value)


def animations_enabled() -> bool:
    """Settings > Accessibility > Animation effects (NFR-8). True when it cannot be read."""
    enabled = wintypes.BOOL(True)
    if not _user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0):
        return True
    return bool(enabled.value)


def _set_attribute(hwnd: int, attribute: int, value: int) -> bool:
    v = wintypes.DWORD(value)
    return _dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(v), ctypes.sizeof(v)) == S_OK


def apply_acrylic(hwnd: int, dark: bool) -> bool:
    """Acrylic backdrop, per-theme dark mode and round corners for a dialog (§8.6).

    False on Windows 10 or when DWM refuses: the caller paints its opaque fallback panel.
    """
    if sys.getwindowsversion().build < WIN11_22H2:
        return False
    try:
        _set_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, int(dark))
        _set_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
        margins = (wintypes.INT * 4)(-1, -1, -1, -1)  # sheet of glass: the whole client area
        _dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
        return _set_attribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW)
    except (OSError, ctypes.ArgumentError):
        return False  # a dialog must still open: it paints the fallback panel
