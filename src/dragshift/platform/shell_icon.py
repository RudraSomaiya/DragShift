"""The icon Windows shows for any shell item, including Store apps (FR-3.3 launcher icons).

``QFileIconProvider`` needs a file path, and a Store app has none: it is
``shell:AppsFolder\\<AUMID>``. The shell's ``IShellItemImageFactory`` draws the same icon the
Start menu uses. pywin32 does not wrap that interface, so it is called through ctypes.

Call on the Qt thread (COM is initialised there).
"""

import ctypes
from ctypes import wintypes

_ole32 = ctypes.WinDLL("ole32")
_shell32 = ctypes.WinDLL("shell32")
_gdi32 = ctypes.WinDLL("gdi32")
_user32 = ctypes.WinDLL("user32")

SIIGBF_BIGGERSIZEOK = 0x1  # GetImage: a larger cached image may be scaled down
SIIGBF_ICONONLY = 0x4  # ...an icon, never a thumbnail of the content
BI_RGB = 0
DIB_RGB_COLORS = 0
S_OK = 0


class _GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]  # fmt: skip


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]  # fmt: skip


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]  # fmt: skip


_shell32.SHCreateItemFromParsingName.argtypes = [
    wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
]  # fmt: skip
_shell32.SHCreateItemFromParsingName.restype = ctypes.c_long  # HRESULT
_gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]  # fmt: skip
_gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

# IShellItemImageFactory's vtable: IUnknown's QueryInterface, AddRef, Release, then GetImage.
_RELEASE, _GET_IMAGE = 2, 3
_Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_GetImage = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, _SIZE, ctypes.c_int, ctypes.POINTER(wintypes.HBITMAP)
)
_IID_IMAGE_FACTORY = _GUID(
    0xBCC18B79, 0xBA16, 0x442F, (ctypes.c_ubyte * 8)(0x80, 0xC4, 0x8A, 0x59, 0xC3, 0x0C, 0x46, 0x3B)
)


def _bgra(hbm: int) -> tuple[int, int, bytes] | None:
    """Width, height and top-down 32-bit BGRA pixels (premultiplied alpha) of a bitmap."""
    bm = _BITMAP()
    if not _gdi32.GetObjectW(hbm, ctypes.sizeof(_BITMAP), ctypes.byref(bm)):
        return None
    w, h = bm.bmWidth, abs(bm.bmHeight)
    header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, BI_RGB)
    buf = ctypes.create_string_buffer(w * h * 4)
    dc = _user32.GetDC(None)
    try:
        rows = _gdi32.GetDIBits(dc, hbm, 0, h, buf, ctypes.byref(header), DIB_RGB_COLORS)
    finally:
        _user32.ReleaseDC(None, dc)
    return (w, h, buf.raw) if rows == h else None


def shell_icon(parsing_name: str, size: int) -> tuple[int, int, bytes] | None:
    """The shell's icon for ``parsing_name`` (a path, or ``shell:AppsFolder\\<AUMID>``) at
    about ``size`` px, as (width, height, BGRA bytes). None if the shell has no such item."""
    _ole32.CoInitialize(None)  # S_FALSE when the thread already has COM: a no-op
    factory = ctypes.c_void_p()
    hr = _shell32.SHCreateItemFromParsingName(
        parsing_name, None, ctypes.byref(_IID_IMAGE_FACTORY), ctypes.byref(factory)
    )
    if hr != S_OK or not factory:
        return None
    vtable = ctypes.cast(ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p))[0],
                         ctypes.POINTER(ctypes.c_void_p))  # fmt: skip
    hbm = wintypes.HBITMAP()
    try:
        get_image = _GetImage(vtable[_GET_IMAGE])
        flags = SIIGBF_ICONONLY | SIIGBF_BIGGERSIZEOK
        if get_image(factory, _SIZE(size, size), flags, ctypes.byref(hbm)) != S_OK:
            return None
        return _bgra(hbm.value)
    finally:
        if hbm:
            _gdi32.DeleteObject(hbm)
        _Release(vtable[_RELEASE])(factory)
