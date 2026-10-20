"""Screen snapshot for the glass (PRD §8.4 step 1).

``grab`` copies a rect of the desktop with ``BitBlt(SRCCOPY)`` and *without* ``CAPTUREBLT``:
layered windows, such as the OLE drag image under the cursor, are left out, so they are not baked
into the glass. Call it before the wheel is shown, or the wheel ends up in its own snapshot.
"""

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QImage, QScreen

__all__ = ["grab", "physical_rect"]

# Win32 constants
SRCCOPY = 0x00CC0020  # plain copy; deliberately NOT | CAPTUREBLT (0x40000000), see above
BI_RGB = 0  # uncompressed DIB
DIB_RGB_COLORS = 0  # the colour table holds literal RGB values

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
# BitBlt(hdcDest, x, y, cx, cy, hdcSrc, x1, y1, rop)
_gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    *[ctypes.c_int] * 4,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


_gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(_BITMAPINFOHEADER),  # BITMAPINFO; no colour table at 32 bpp
    wintypes.UINT,
]


def physical_rect(logical: QRect, screen: QScreen) -> QRect:
    """Map a rect in Qt's logical coordinates on ``screen`` to physical desktop pixels.

    With per-monitor DPI, Qt keeps each screen's top-left at its physical position and scales
    only the distances from it, so this is exact for any rect that lies on ``screen``.
    """
    dpr = screen.devicePixelRatio()
    origin = screen.geometry().topLeft()
    top_left = origin + (logical.topLeft() - origin) * dpr  # QPoint * float rounds
    return QRect(top_left, QSize(round(logical.width() * dpr), round(logical.height() * dpr)))


def grab(rect: QRect) -> QImage:
    """The desktop pixels in ``rect`` (physical px) as an opaque QImage. Off-screen parts are black."""
    w, h = rect.width(), rect.height()
    screen_dc = _user32.GetDC(None)  # the whole virtual screen
    mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
    bmp = _gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    old = _gdi32.SelectObject(mem_dc, bmp)
    try:
        _gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, rect.x(), rect.y(), SRCCOPY)
        _gdi32.SelectObject(mem_dc, old)  # GetDIBits needs the bitmap deselected
        header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, BI_RGB)
        buf = ctypes.create_string_buffer(w * h * 4)  # -h above: rows top-down
        _gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(header), DIB_RGB_COLORS)
    finally:
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)
    # BGRA bytes with an undefined alpha byte: QImage RGB32 on little-endian.
    return QImage(buf.raw, w, h, w * 4, QImage.Format.Format_RGB32).copy()
