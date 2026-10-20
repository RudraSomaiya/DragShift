"""DS-5 spike (throwaway): "Frost" glass wheel prototype + idle RAM baseline.

Run:
  uv run python spikes/ds5_glass_wheel.py                   wheel at the cursor after 2 s; click it to close
  uv run python spikes/ds5_glass_wheel.py --theme dark --save wheel.png
  uv run python spikes/ds5_glass_wheel.py --ram             bare-app RAM baseline (~4 min)
  uv run python spikes/ds5_glass_wheel.py --ram --gdi-fonts same, with Qt's GDI font engine

What it does (PRD §8.2-§8.4, NFR-1, M1, M2):
  * Snapshots the screen under the future wheel with BitBlt SRCCOPY *without* CAPTUREBLT.
  * Renders the §8.4 recipe once into a cached QPixmap: blur (1/8 down/up, twice) + saturation
    x1.3, refraction rim (x1.06 in the outer 18 px band), tint, petals, specular arc, stroke,
    pre-blurred shadow sprite. Qt only, no numpy.
  * Shows it in a frameless, translucent, no-focus window centred on the cursor and clamped to the
    monitor work area, and logs how long each step took.
  * --ram: records private bytes / working set of a bare PySide6 app after an idle period, then
    again after one wheel was shown and closed.

Everything is logged to the console and to ds5-spike.log (git-ignored).
"""

import argparse
import cmath
import ctypes
import logging
import math
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QFileInfo, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QConicalGradient,
    QCursor,
    QFont,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QFileIconProvider, QWidget

log = logging.getLogger("ds5")

# --- Geometry at 100 % scale (PRD §8.3) -------------------------------------------------------
WIN = 380  # window size; the margin around the disc holds the shadow
R_DISC = 150  # 300 px disc
R_IN, R_OUT = 58, 142  # petal annulus
GAP = 6  # gap between petals
CORNER = 18  # petal corner rounding
R_HUB = 48  # 96 px hub
RIM, RIM_ZOOM = 18, 1.06  # refraction band width and magnification (§8.4 step 3)
HOVER_SCALE = 1.04

# --- Design tokens (PRD §8.2) -----------------------------------------------------------------
TOKENS = {
    "light": {
        "tint": (255, 255, 255, 0.22),
        "petal": (255, 255, 255, 0.30),
        "stroke": (255, 255, 255, 0.45),
        "highlight": (255, 255, 255, 0.60),
        "text": (0x0B, 0x12, 0x20, 1.0),
        "text2": (11, 18, 32, 0.62),
        "shadow": 0.30,
        "hover": 0.55,
    },
    "dark": {
        "tint": (16, 20, 28, 0.38),
        "petal": (255, 255, 255, 0.08),
        "stroke": (255, 255, 255, 0.14),
        "highlight": (255, 255, 255, 0.28),
        "text": (0xF2, 0xF5, 0xFA, 1.0),
        "text2": (242, 245, 250, 0.62),
        "shadow": 0.50,
        "hover": 0.65,
    },
}
GLACIER = QColor("#4C8DFF")  # accent fallback


def rgba(t: tuple[int, int, int, float]) -> QColor:
    r, g, b, a = t
    return QColor(r, g, b, round(a * 255))


# --- Win32 ------------------------------------------------------------------------------------
SRCCOPY = 0x00CC0020  # plain copy; deliberately NOT | CAPTUREBLT (0x40000000), so layered windows
#                       such as the OLE drag image are left out of the snapshot (§8.4 step 1)
BI_RGB = 0
DIB_RGB_COLORS = 0

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
# BitBlt(hdcDest, x, y, cx, cy, hdcSrc, x1, y1, rop)
gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    *[ctypes.c_int] * 4,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
dwmapi.DwmGetColorizationColor.argtypes = [
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.BOOL),
]


class BITMAPINFOHEADER(ctypes.Structure):
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


gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFOHEADER),  # BITMAPINFO; no colour table needed at 32 bpp
    wintypes.UINT,
]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),  # = "PM" / private bytes in Get-Process
    ]


kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.K32GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
    wintypes.DWORD,
]


def grab_screen(x: int, y: int, w: int, h: int) -> QImage:
    """BitBlt a rect of the virtual screen (physical px) into an opaque QImage."""
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, x, y, SRCCOPY)
        gdi32.SelectObject(mem_dc, old)  # GetDIBits needs the bitmap deselected
        hdr = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, BI_RGB)  # top-down
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(hdr), DIB_RGB_COLORS)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)
    # BGRA bytes with an undefined alpha byte = QImage RGB32 on little-endian.
    return QImage(buf.raw, w, h, w * 4, QImage.Format.Format_RGB32).copy()


def cursor_physical() -> QPoint:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))  # physical px: Qt makes the process per-monitor aware
    return QPoint(pt.x, pt.y)


def windows_accent() -> QColor:
    color, opaque = wintypes.DWORD(), wintypes.BOOL()
    if dwmapi.DwmGetColorizationColor(ctypes.byref(color), ctypes.byref(opaque)) != 0:
        return GLACIER
    return QColor((color.value >> 16) & 0xFF, (color.value >> 8) & 0xFF, color.value & 0xFF)


def system_theme() -> str:
    key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            light, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
    except OSError:
        return "light"
    return "light" if light else "dark"


def memory_mb() -> tuple[float, float]:
    pmc = PROCESS_MEMORY_COUNTERS_EX()
    pmc.cb = ctypes.sizeof(pmc)
    kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return pmc.PrivateUsage / 2**20, pmc.WorkingSetSize / 2**20


# --- §8.4 image steps -------------------------------------------------------------------------
def saturate(img: QImage, factor: float) -> QImage:
    """Cheap saturation: push each pixel away from its luma (run at 1/8 size only)."""
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    buf = bytearray(img.constBits().tobytes())
    for i in range(0, len(buf), 4):
        b, g, r = buf[i], buf[i + 1], buf[i + 2]
        y = (r * 77 + g * 150 + b * 29) >> 8
        buf[i] = min(255, max(0, int(y + (b - y) * factor)))
        buf[i + 1] = min(255, max(0, int(y + (g - y) * factor)))
        buf[i + 2] = min(255, max(0, int(y + (r - y) * factor)))
    return QImage(bytes(buf), img.width(), img.height(), img.bytesPerLine(), img.format()).copy()


def blur(img: QImage, saturation: float = 1.0) -> QImage:
    """§8.4 step 2: 1/8 down + up, twice (≈ a 20 px Gaussian), saturation at the small size."""
    full = img.size()
    small = QSize(max(1, full.width() // 8), max(1, full.height() // 8))
    smooth = Qt.TransformationMode.SmoothTransformation
    ignore = Qt.AspectRatioMode.IgnoreAspectRatio
    for i in range(2):
        img = img.scaled(small, ignore, smooth)
        if i == 0 and saturation != 1.0:
            img = saturate(img, saturation)
        img = img.scaled(full, ignore, smooth)
    return img


_shadow_cache: dict[tuple[str, float], QImage] = {}


def shadow_sprite(theme: str, dpr: float) -> QImage:
    """§8.4 step 6: shadow `0 12px 32px rgba(0,0,0,a)`, blurred once per theme and DPR."""
    key = (theme, dpr)
    if key not in _shadow_cache:
        img = QImage(round(WIN * dpr), round(WIN * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        img.setDevicePixelRatio(dpr)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, round(TOKENS[theme]["shadow"] * 255)))
        p.drawEllipse(QPointF(WIN / 2, WIN / 2 + 12), R_DISC, R_DISC)
        p.end()
        blurred = blur(img)
        blurred.setDevicePixelRatio(dpr)
        _shadow_cache[key] = blurred
    return _shadow_cache[key]


# --- Petal geometry ---------------------------------------------------------------------------
def _arc(c: complex, radius: float, start: float, delta: float) -> list[complex]:
    steps = max(2, math.ceil(abs(delta) / math.radians(2)))
    return [c + cmath.rect(radius, start + delta * k / steps) for k in range(1, steps + 1)]


def _fillet(centre: complex, p: complex, q: complex) -> list[complex]:
    """Short circular arc around `centre` from p to q."""
    a = cmath.phase(p - centre)
    d = (cmath.phase(q - centre) - a + math.pi) % (2 * math.pi) - math.pi
    return _arc(centre, abs(p - centre), a, d)


def petal_path(c: QPointF, a0: float, a1: float) -> QPainterPath:
    """Annular sector a0..a1 (radians, clockwise on screen) with a GAP/2 inset on both radial
    edges and CORNER-px fillets at all four corners. Points are complex numbers around (0, 0)."""
    h = GAP / 2

    def corner(u: complex, n: complex, r_circle: float, r: float, outer: bool):
        # Fillet circle tangent to the edge line (offset h along n) and to the arc r_circle.
        rho = r_circle - r if outer else r_circle + r
        t = math.sqrt(rho * rho - (h + r) ** 2)
        centre = t * u + (h + r) * n
        return centre, t * u + h * n, centre / abs(centre) * r_circle

    u0, u1 = cmath.rect(1, a0), cmath.rect(1, a1)
    n0, n1 = u0 * 1j, -u1 * 1j  # normals pointing into the petal
    co0, lo0, ao0 = corner(u0, n0, R_OUT, CORNER, True)
    co1, lo1, ao1 = corner(u1, n1, R_OUT, CORNER, True)
    r = CORNER  # the inner arc is short: shrink its fillets until they do not overlap
    while True:
        ci0, li0, ai0 = corner(u0, n0, R_IN, r, False)
        ci1, li1, ai1 = corner(u1, n1, R_IN, r, False)
        span = (cmath.phase(ai1) - cmath.phase(ai0)) % (2 * math.pi)
        if span < a1 - a0 or r <= 1:
            break
        r -= 1

    pts = [li0, lo0, *_fillet(co0, lo0, ao0)]
    outer_span = (cmath.phase(ao1) - cmath.phase(ao0)) % (2 * math.pi)
    pts += _arc(0, R_OUT, cmath.phase(ao0), outer_span)
    pts += [*_fillet(co1, ao1, lo1), li1, *_fillet(ci1, li1, ai1)]
    pts += _arc(0, R_IN, cmath.phase(ai1), -span)
    pts += _fillet(ci0, ai0, li0)

    path = QPainterPath(QPointF(c.x() + pts[0].real, c.y() + pts[0].imag))
    for z in pts[1:]:
        path.lineTo(c.x() + z.real, c.y() + z.imag)
    path.closeSubpath()
    return path


def petal_angles(n: int) -> list[tuple[float, float, float]]:
    """(start, mid, end) per petal, clockwise from 12 o'clock (FR-2.3)."""
    step = 2 * math.pi / n
    return [(m - step / 2, m, m + step / 2) for m in (-math.pi / 2 + i * step for i in range(n))]


# --- Composition ------------------------------------------------------------------------------
LABELS = ["PNG", "JPG", "WEBP", "HEIC", "AVIF", "PDF", "TIFF", "ICO"]


def label_font() -> QFont:
    f = QFont()
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
    f.setPixelSize(11)
    f.setWeight(QFont.Weight.DemiBold)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4)
    f.setCapitalization(QFont.Capitalization.AllUppercase)
    return f


def compose(
    snapshot: QImage,
    dpr: float,
    theme: str,
    accent: QColor,
    petals: int,
    hover: int,
    thumb: QPixmap,
) -> tuple[QPixmap, dict[str, float]]:
    """Render the whole static wheel once (§8.4 step 7). Returns the pixmap and step timings."""
    t: dict[str, float] = {}
    tok = TOKENS[theme]
    t0 = time.perf_counter()
    blurred = blur(snapshot, saturation=1.3)
    blurred.setDevicePixelRatio(dpr)
    t["blur"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    shadow = shadow_sprite(theme, dpr)
    t["shadow"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    pm = QPixmap(round(WIN * dpr), round(WIN * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHints(
        QPainter.RenderHint.Antialiasing
        | QPainter.RenderHint.SmoothPixmapTransform
        | QPainter.RenderHint.TextAntialiasing
    )
    c = QPointF(WIN / 2, WIN / 2)
    disc = QPainterPath()
    disc.addEllipse(c, R_DISC, R_DISC)

    p.drawImage(0, 0, shadow)  # step 6 (under everything)

    p.save()  # step 2: the blurred backdrop inside the disc
    p.setClipPath(disc)
    p.drawImage(0, 0, blurred)
    p.restore()

    inner = QPainterPath()
    inner.addEllipse(c, R_DISC - RIM, R_DISC - RIM)
    p.save()  # step 3: refraction rim, magnified about the centre
    p.setClipPath(disc.subtracted(inner))
    p.translate(c)
    p.scale(RIM_ZOOM, RIM_ZOOM)
    p.translate(-c)
    p.drawImage(0, 0, blurred)
    p.restore()

    p.fillPath(disc, rgba(tok["tint"]))  # step 4: tint

    font = label_font()
    p.setFont(font)
    for i, (a0, mid, a1) in enumerate(petal_angles(petals)):
        path = petal_path(c, a0, a1)
        p.save()
        if i == hover:  # §8.3 hover: accent fill, scale 1.04 about the centre, radial sheen
            p.translate(c)
            p.scale(HOVER_SCALE, HOVER_SCALE)
            p.translate(-c)
            fill = QColor(accent)
            fill.setAlphaF(tok["hover"])
            p.fillPath(path, fill)
            centroid = c + QPointF(math.cos(mid), math.sin(mid)) * (R_IN + R_OUT) / 2
            sheen = QRadialGradient(centroid + QPointF(-14, -14), 70)
            sheen.setColorAt(0, QColor(255, 255, 255, 90))
            sheen.setColorAt(1, QColor(255, 255, 255, 0))
            p.fillPath(path, sheen)
            p.setPen(QColor("white"))
        else:
            p.fillPath(path, rgba(tok["petal"]))
            p.setPen(rgba(tok["text"]))
        mid_pt = c + QPointF(math.cos(mid), math.sin(mid)) * (R_IN + R_OUT) / 2
        p.drawText(
            QRectF(mid_pt.x() - 40, mid_pt.y() - 10, 80, 20),
            Qt.AlignmentFlag.AlignCenter,
            LABELS[i % len(LABELS)],
        )
        p.restore()

    # Hub: glass circle, 40 px thumbnail, label pill of the hovered petal (§8.3)
    hub = QPainterPath()
    hub.addEllipse(c, R_HUB, R_HUB)
    p.fillPath(hub, rgba(tok["tint"]))
    p.fillPath(hub, rgba(tok["petal"]))
    p.setPen(QPen(rgba(tok["stroke"]), 1))
    p.drawPath(hub)
    p.drawPixmap(QRectF(c.x() - 20, c.y() - 30, 40, 40), thumb, QRectF(thumb.rect()))
    pill_text = LABELS[hover % len(LABELS)] if hover >= 0 else ""
    if pill_text:
        pill = QRectF(c.x() - 24, c.y() + 14, 48, 18)
        p.setPen(Qt.PenStyle.NoPen)
        pill_fill = QColor(accent)
        pill_fill.setAlphaF(0.85)
        p.setBrush(pill_fill)
        p.drawRoundedRect(pill, 9, 9)
        p.setPen(QColor("white"))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, pill_text)

    # Step 5: 1 px specular arc 10 → 2 o'clock fading to transparent, plus the outer stroke.
    hl = rgba(tok["highlight"])
    clear = QColor(hl)
    clear.setAlpha(0)
    spec = QConicalGradient(c, 30)  # degrees, counter-clockwise from 3 o'clock; 30° = 2 o'clock
    spec.setColorAt(0.0, clear)
    spec.setColorAt(60 / 360, hl)  # 12 o'clock
    spec.setColorAt(120 / 360, clear)  # 10 o'clock
    spec.setColorAt(1.0, clear)
    r_spec = R_DISC - 1.5
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(spec, 1))
    p.drawArc(QRectF(c.x() - r_spec, c.y() - r_spec, 2 * r_spec, 2 * r_spec), 30 * 16, 120 * 16)
    p.setPen(QPen(rgba(tok["stroke"]), 1))
    p.drawEllipse(c, R_DISC - 0.5, R_DISC - 0.5)
    p.end()
    t["compose"] = time.perf_counter() - t0
    return pm, t


# --- Window -----------------------------------------------------------------------------------
class WheelWindow(QWidget):
    closed = Signal()  # Qt.Tool windows do not trigger quitOnLastWindowClosed

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(WIN, WIN)
        self.pixmap = QPixmap()
        self.snapshot = QImage()
        self.open_t = 0.0
        self.painted = False

    def open_at_cursor(self, theme: str, petals: int, hover: int, thumb: QPixmap) -> None:
        self.open_t = time.perf_counter()
        self.painted = False
        pos = QCursor.pos()  # logical px
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio()
        area = screen.availableGeometry()
        x = min(max(pos.x() - WIN // 2, area.left()), area.right() + 1 - WIN)
        y = min(max(pos.y() - WIN // 2, area.top()), area.bottom() + 1 - WIN)

        t0 = time.perf_counter()
        phys = cursor_physical()  # map the clamped logical rect to physical px via the cursor
        size = round(WIN * dpr)
        self.snapshot = grab_screen(
            phys.x() + round((x - pos.x()) * dpr), phys.y() + round((y - pos.y()) * dpr), size, size
        )
        self.snapshot.setDevicePixelRatio(dpr)
        t_snap = time.perf_counter() - t0

        self.pixmap, t = compose(self.snapshot, dpr, theme, windows_accent(), petals, hover, thumb)
        log.info(
            "rendered at (%d,%d) dpr=%.2f theme=%s | snapshot %.1f ms, blur+sat %.1f ms, "
            "shadow %.1f ms, compose %.1f ms",
            x,
            y,
            dpr,
            theme,
            t_snap * 1e3,
            t["blur"] * 1e3,
            t["shadow"] * 1e3,
            t["compose"] * 1e3,
        )
        self.move(x, y)
        self.show()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.drawPixmap(0, 0, self.pixmap)
        p.end()
        if not self.painted:
            self.painted = True
            log.info("first paint %.1f ms after open (M2 budget: 120 ms)", self._since_open())

    def _since_open(self) -> float:
        return (time.perf_counter() - self.open_t) * 1e3

    def mousePressEvent(self, _event) -> None:
        self.close()

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        self.closed.emit()

    def save_screenshot(self, path: Path) -> None:
        """The wheel composited over its own snapshot, i.e. what is on screen."""
        out = QPixmap(self.pixmap.size())
        out.setDevicePixelRatio(self.pixmap.devicePixelRatio())
        p = QPainter(out)
        p.drawImage(QRect(0, 0, WIN, WIN), self.snapshot)
        p.drawPixmap(0, 0, self.pixmap)
        p.end()
        out.save(str(path))
        log.info("screenshot saved to %s", path.resolve())


def warm_up(wheel: WheelWindow) -> None:
    """Pay one-time costs at startup instead of on the first wheel (M2).

    The first drawText() loads Qt's DirectWrite font database: ~0.4 s warm, ~3 s on a cold
    disk cache. Creating the native window early saves its first-show cost too.
    """
    t0 = time.perf_counter()
    img = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setFont(label_font())
    p.drawText(0, 8, "PNG")
    p.end()
    wheel.winId()  # creates the native HWND without showing it
    log.info("warm-up (font database + native window) %.1f ms", (time.perf_counter() - t0) * 1e3)


# --- RAM baseline -----------------------------------------------------------------------------
HEAVY = ["PIL", "fitz", "pandas", "numpy", "pdf2docx", "pypandoc"]


def log_memory(stage: str) -> None:
    private, ws = memory_mb()
    heavy = [m for m in HEAVY if m in sys.modules] or "none"
    log.info(
        "RAM %-34s private %6.1f MB | working set %6.1f MB | heavy libs: %s",
        stage,
        private,
        ws,
        heavy,
    )


def run_ram(app: QApplication, args: argparse.Namespace, thumb: QPixmap, theme: str) -> None:
    idle_ms = int(args.idle * 1000)
    log_memory("after QApplication()")
    wheel = WheelWindow()
    warm_up(wheel)
    log_memory("after warm-up")
    log.info("idling %.0f s with no windows (exe: %s)...", args.idle, Path(sys.executable).name)

    def after_bare_idle() -> None:
        log_memory(f"bare app, idle {args.idle:.0f} s")
        wheel.open_at_cursor(theme, args.petals, args.hover, thumb)
        QTimer.singleShot(1500, close_wheel)

    def close_wheel() -> None:
        log_memory("wheel open")
        wheel.close()
        log.info("wheel closed; idling %.0f s...", args.idle)
        QTimer.singleShot(idle_ms, after_wheel_idle)

    def after_wheel_idle() -> None:
        log_memory(f"after 1 wheel, idle {args.idle:.0f} s")
        log.info("NFR-1 / M1 budget for the full UI process: 60 MB private bytes after 2 min idle")
        app.quit()

    QTimer.singleShot(idle_ms, after_bare_idle)


def main() -> int:
    parser = argparse.ArgumentParser(description="DS-5 spike: glass wheel + RAM baseline")
    parser.add_argument("--theme", choices=["system", "light", "dark"], default="system")
    parser.add_argument("--petals", type=int, default=8, choices=range(2, 9), metavar="2..8")
    parser.add_argument("--hover", type=int, default=2, help="highlighted petal, -1 for none")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds before the snapshot")
    parser.add_argument("--save", type=Path, help="save a screenshot PNG of the wheel")
    parser.add_argument("--seconds", type=float, default=0, help="auto-close after N seconds")
    parser.add_argument("--file", type=Path, default=Path(__file__), help="file for the hub icon")
    parser.add_argument("--ram", action="store_true", help="measure the idle RAM baseline")
    parser.add_argument("--idle", type=float, default=120.0, help="idle seconds for --ram")
    parser.add_argument(
        "--gdi-fonts",
        action="store_true",
        help="use Qt's GDI font engine; DirectWrite's font database costs ~28 MB private",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("ds5-spike.log", mode="w", encoding="utf-8"),
        ],
    )
    qt_argv = sys.argv[:1] + (["-platform", "windows:fontengine=gdi"] if args.gdi_fonts else [])
    app = QApplication(qt_argv)
    log.info("font engine: %s", "GDI" if args.gdi_fonts else "DirectWrite (Qt default)")
    theme = system_theme() if args.theme == "system" else args.theme
    thumb = QFileIconProvider().icon(QFileInfo(str(args.file))).pixmap(80, 80)

    if args.ram:
        run_ram(app, args, thumb, theme)
        return app.exec()

    wheel = WheelWindow()
    wheel.closed.connect(app.quit)
    warm_up(wheel)

    def open_wheel() -> None:
        wheel.open_at_cursor(theme, args.petals, args.hover, thumb)
        if args.save:
            wheel.save_screenshot(args.save)
        if args.seconds:
            QTimer.singleShot(int(args.seconds * 1000), wheel.close)

    log.info("Move the mouse over something colourful; the wheel opens in %.1f s.", args.delay)
    QTimer.singleShot(int(args.delay * 1000), open_wheel)
    code = app.exec()
    log_memory("at exit")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
