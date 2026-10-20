"""Liquid-glass rendering (PRD §8.4). Qt only: no numpy, no Pillow.

The desktop behind a wheel is static while the wheel is open, so ``render`` runs once per open
on a snapshot of the desktop and returns cached pixmaps. Petals, the hub, hover and motion are
painted on top of them every frame, so a swap or the More… ring never re-renders the glass.
"""

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QConicalGradient, QImage, QPainter, QPainterPath, QPen, QPixmap

from dragshift.ui import wheel_geometry as geo
from dragshift.ui.theme import SHADOW_OFFSET, Theme

__all__ = ["SIZE", "Glass", "blur", "render", "saturate", "shadow_sprite"]

SIZE = geo.RING_WIN  # rendered at the ring window's size, so the More… ring has glass too
RIM, RIM_ZOOM = 18, 1.06  # refraction band width and magnification (step 3)
SATURATION = 1.3  # step 2


@dataclass(frozen=True)
class Glass:
    """SIZE x SIZE (logical px) layers, centred on the wheel centre."""

    disc: QPixmap  # shadow, blurred backdrop, refraction rim, tint, specular arc, stroke
    ring: QPixmap  # the More… annulus: blurred backdrop, tint, outer stroke


def saturate(img: QImage, factor: float) -> QImage:
    """Push each pixel away from its luma. Pure Python, so only run it at 1/8 size."""
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    buf = bytearray(img.constBits().tobytes())
    for i in range(0, len(buf), 4):  # BGRA
        b, g, r = buf[i], buf[i + 1], buf[i + 2]
        y = (r * 77 + g * 150 + b * 29) >> 8
        buf[i] = min(255, max(0, int(y + (b - y) * factor)))
        buf[i + 1] = min(255, max(0, int(y + (g - y) * factor)))
        buf[i + 2] = min(255, max(0, int(y + (r - y) * factor)))
    return QImage(bytes(buf), img.width(), img.height(), img.bytesPerLine(), img.format()).copy()


def blur(img: QImage, saturation: float = 1.0) -> QImage:
    """Step 2: scale to 1/8 and back, twice (about a 20 px Gaussian), saturating at 1/8 size."""
    full = img.size()
    small = QSize(max(1, full.width() // 8), max(1, full.height() // 8))
    smooth = Qt.TransformationMode.SmoothTransformation
    ignore = Qt.AspectRatioMode.IgnoreAspectRatio
    dpr = img.devicePixelRatio()
    for i in range(2):
        img = img.scaled(small, ignore, smooth)
        if i == 0 and saturation != 1.0:
            img = saturate(img, saturation)
        img = img.scaled(full, ignore, smooth)
    img.setDevicePixelRatio(dpr)
    return img


_shadows: dict[tuple[bool, float], QImage] = {}


def shadow_sprite(theme: Theme, dpr: float) -> QImage:
    """Step 6: the disc's ``0 12px 32px`` shadow, blurred once per mode and DPR."""
    key = (theme.dark, dpr)
    if key not in _shadows:
        side = round(SIZE * dpr)
        img = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        img.setDevicePixelRatio(dpr)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.shadow)
        p.drawEllipse(QPointF(SIZE / 2, SIZE / 2 + SHADOW_OFFSET), geo.R_DISC, geo.R_DISC)
        p.end()
        _shadows[key] = blur(img)
    return _shadows[key]


def _circle(c: QPointF, r: float) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(c, r, r)
    return path


def _canvas(dpr: float) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(round(SIZE * dpr), round(SIZE * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    return pm, p


def _specular(p: QPainter, c: QPointF, theme: Theme) -> None:
    """Step 5: a 1 px inner highlight from 10 to 2 o'clock, brightest at 12, plus the stroke."""
    lit = theme.highlight
    clear = QColor(lit)
    clear.setAlpha(0)
    spec = QConicalGradient(c, 30)  # degrees counter-clockwise from 3 o'clock: 30 = 2 o'clock
    spec.setColorAt(0.0, clear)
    spec.setColorAt(60 / 360, lit)  # 12 o'clock
    spec.setColorAt(120 / 360, clear)  # 10 o'clock
    spec.setColorAt(1.0, clear)
    r = geo.R_DISC - 1.5
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(spec, 1))
    p.drawArc(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), 30 * 16, 120 * 16)
    p.setPen(QPen(theme.stroke, 1))
    p.drawEllipse(c, geo.R_DISC - 0.5, geo.R_DISC - 0.5)


def render(snapshot: QImage, theme: Theme) -> Glass:
    """Steps 2-7 for a SIZE x SIZE (logical) snapshot centred on the wheel."""
    dpr = snapshot.devicePixelRatio()
    blurred = blur(snapshot, SATURATION)
    c = QPointF(SIZE / 2, SIZE / 2)
    disc = _circle(c, geo.R_DISC)

    pm, p = _canvas(dpr)
    p.drawImage(0, 0, shadow_sprite(theme, dpr))  # step 6, under everything
    p.save()  # the blurred backdrop inside the disc
    p.setClipPath(disc)
    p.drawImage(0, 0, blurred)
    p.restore()
    p.save()  # step 3: the outer band shows the backdrop magnified about the centre
    p.setClipPath(disc.subtracted(_circle(c, geo.R_DISC - RIM)))
    p.translate(c)
    p.scale(RIM_ZOOM, RIM_ZOOM)
    p.translate(-c)
    p.drawImage(0, 0, blurred)
    p.restore()
    p.fillPath(disc, theme.tint)  # step 4; petals get glass.petal when the wheel paints them
    _specular(p, c, theme)
    p.end()

    ring_pm, p = _canvas(dpr)
    ring = _circle(c, geo.RING_OUT).subtracted(_circle(c, geo.RING_IN))
    p.setClipPath(ring)
    p.drawImage(0, 0, blurred)
    p.fillPath(ring, theme.tint)
    p.setClipping(False)
    p.setPen(QPen(theme.stroke, 1))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(c, geo.RING_OUT - 0.5, geo.RING_OUT - 0.5)
    p.end()
    return Glass(pm, ring_pm)
