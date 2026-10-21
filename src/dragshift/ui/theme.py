"""Design tokens and the resolved theme (PRD §8.2, §8.5, NFR-8).

``resolve(appearance)`` turns the config's ``appearance`` section into a ``Theme``:
- ``theme``: "system" follows ``AppsUseLightTheme``; "light" and "dark" override it.
- ``accent``: "system" is the Windows accent; "#RRGGBB" overrides it. Either falls back to
  Glacier, and only the fallback uses ``accent.gradient``.
- ``reduced_motion``: "system" follows "Animation effects"; "on" and "off" override it. With
  reduced motion every duration is 0 (§8.5).
"""

import logging
from dataclasses import dataclass

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient

from dragshift.config import Appearance
from dragshift.platform import dwm

__all__ = [
    "DARK",
    "LIGHT",
    "Palette",
    "Theme",
    "contrast",
    "label_font",
    "make",
    "over",
    "resolve",
    "ui_font",
]

log = logging.getLogger(__name__)

RGBA = tuple[int, int, int, float]

GLACIER = QColor("#4C8DFF")  # accent fallback
GRADIENT_END = QColor("#8B6CFF")  # accent.gradient, fallback only

# Motion (§8.5), in ms
OPEN_MS = 140
OPEN_SCALE_FROM = 0.85
OPEN_OVERSHOOT = 1.2  # OutBack(1.2)
PETAL_STAGGER_MS = 12
CLOSE_MS = 90
SWAP_MS = 120
RING_MS = 160
HOVER_MS = 90  # §8.3
TOAST_MS = 160


@dataclass(frozen=True)
class Palette:
    """The per-mode tokens of §8.2."""

    tint: RGBA
    petal: RGBA
    stroke: RGBA
    highlight: RGBA  # top-left inner rim, fading to 0
    text: RGBA
    text_secondary: RGBA
    hover_alpha: float  # petal.hover: accent at this alpha
    shadow_alpha: float  # shadow: 0 12px 32px rgba(0,0,0,a)


LIGHT = Palette(
    tint=(255, 255, 255, 0.22),
    petal=(255, 255, 255, 0.30),
    stroke=(255, 255, 255, 0.45),
    highlight=(255, 255, 255, 0.60),
    text=(0x0B, 0x12, 0x20, 1.0),
    text_secondary=(11, 18, 32, 0.62),
    hover_alpha=0.55,
    shadow_alpha=0.30,
)
DARK = Palette(
    tint=(16, 20, 28, 0.38),
    petal=(255, 255, 255, 0.08),
    stroke=(255, 255, 255, 0.14),
    highlight=(255, 255, 255, 0.28),
    text=(0xF2, 0xF5, 0xFA, 1.0),
    text_secondary=(242, 245, 250, 0.62),
    hover_alpha=0.65,
    shadow_alpha=0.50,
)
SHADOW_OFFSET, SHADOW_BLUR = 12, 32


def _rgba(value: RGBA) -> QColor:
    r, g, b, a = value
    return QColor(r, g, b, round(a * 255))


def over(top: QColor, bottom: QColor) -> QColor:
    """``top`` composited over an opaque ``bottom`` (source-over)."""
    a = top.alphaF()
    return QColor(
        round(top.red() * a + bottom.red() * (1 - a)),
        round(top.green() * a + bottom.green() * (1 - a)),
        round(top.blue() * a + bottom.blue() * (1 - a)),
    )


def _luminance(c: QColor) -> float:
    def linear(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    return 0.2126 * linear(c.red()) + 0.7152 * linear(c.green()) + 0.0722 * linear(c.blue())


def contrast(a: QColor, b: QColor) -> float:
    """WCAG 2 contrast ratio between two opaque colours (NFR-8 asks for >= 4.5 on glass)."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# The desktop a mode is assumed to sit on when picking text colours: light mode on a light
# desktop, dark mode on a dark one (the live DS-12 check picked white at 3.8:1 with mid grey).
_BACKDROP = {False: QColor(224, 224, 224), True: QColor(40, 40, 40)}
_WHITE = QColor("white")
_INK = _rgba(LIGHT.text)


@dataclass(frozen=True, eq=False)
class Theme:
    dark: bool
    accent: QColor
    gradient: tuple[QColor, QColor] | None  # accent.gradient, set only for the Glacier fallback
    reduced_motion: bool
    palette: Palette

    @property
    def tint(self) -> QColor:
        return _rgba(self.palette.tint)

    @property
    def petal(self) -> QColor:
        return _rgba(self.palette.petal)

    @property
    def stroke(self) -> QColor:
        return _rgba(self.palette.stroke)

    @property
    def highlight(self) -> QColor:
        return _rgba(self.palette.highlight)

    @property
    def text(self) -> QColor:
        return _rgba(self.palette.text)

    @property
    def text_secondary(self) -> QColor:
        return _rgba(self.palette.text_secondary)

    @property
    def shadow(self) -> QColor:
        return QColor(0, 0, 0, round(self.palette.shadow_alpha * 255))

    @property
    def hover(self) -> QColor:
        """petal.hover: the accent at 55 % (light) or 65 % (dark)."""
        c = QColor(self.accent)
        c.setAlphaF(self.palette.hover_alpha)
        return c

    @property
    def backdrop(self) -> QColor:
        """The desktop colour this mode is assumed to sit on, for picking text colours."""
        return QColor(_BACKDROP[self.dark])

    def on_accent(self, alpha: float | None = None) -> QColor:
        """Text colour on the accent at ``alpha`` (default: the petal.hover alpha) over glass:
        white or ink, whichever contrasts more (NFR-8)."""
        accent = QColor(self.accent)
        accent.setAlphaF(self.palette.hover_alpha if alpha is None else alpha)
        fill = over(accent, over(self.petal, over(self.tint, self.backdrop)))
        return _WHITE if contrast(_WHITE, fill) >= contrast(_INK, fill) else _INK

    def accent_brush(self, rect: QRectF, alpha: float = 1.0) -> QBrush:
        """The accent at ``alpha``, as the fallback gradient across ``rect`` when there is one."""
        if self.gradient is None:
            c = QColor(self.accent)
            c.setAlphaF(alpha)
            return QBrush(c)
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        for stop, colour in zip((0.0, 1.0), self.gradient, strict=True):
            c = QColor(colour)
            c.setAlphaF(alpha)
            grad.setColorAt(stop, c)
        return QBrush(grad)

    def ms(self, duration: int) -> int:
        """A §8.5 duration, or 0 with reduced motion."""
        return 0 if self.reduced_motion else duration


def make(*, dark: bool, accent: QColor | None = None, reduced_motion: bool = False) -> Theme:
    """A theme from explicit choices; ``accent=None`` is the Glacier fallback."""
    palette = DARK if dark else LIGHT
    if accent is None or not accent.isValid():
        gradient = (QColor(GLACIER), QColor(GRADIENT_END))
        return Theme(dark, QColor(GLACIER), gradient, reduced_motion, palette)
    opaque = QColor(accent.red(), accent.green(), accent.blue())
    return Theme(dark, opaque, None, reduced_motion, palette)


def _choice(value: str, name: str, overrides: dict[str, bool]) -> bool | None:
    """An override from the config, or None for "system". Unknown values count as "system"."""
    if value in overrides:
        return overrides[value]
    if value != "system":
        log.warning("appearance.%s: unknown value %r, following Windows", name, value)
    return None


def resolve(appearance: Appearance) -> Theme:
    """The theme for the config's appearance and the current Windows settings."""
    dark = _choice(appearance.theme, "theme", {"light": False, "dark": True})
    if dark is None:
        dark = dwm.apps_use_light_theme() is False  # not set: Windows' default, light

    accent: QColor | None = None
    if appearance.accent == "system":
        rgb = dwm.accent_rgb()
        accent = QColor(*rgb) if rgb else None
    elif QColor.isValidColorName(appearance.accent):
        accent = QColor(appearance.accent)
    else:
        log.warning("appearance.accent: %r is not a colour, using Glacier", appearance.accent)

    reduced = _choice(appearance.reduced_motion, "reduced_motion", {"on": True, "off": False})
    if reduced is None:
        reduced = not dwm.animations_enabled()
    return make(dark=dark, accent=accent, reduced_motion=reduced)


def ui_font(pixel_size: int, semibold: bool = False) -> QFont:
    """Segoe UI Variable, falling back to Segoe UI (§8.2).

    Under Qt's GDI font engine (used for its RAM, NFR-1) the variable font has no DemiBold
    weight, so for semibold its named Semibold instance comes first; DirectWrite skips that name.
    """
    font = QFont()
    if semibold:
        families = [
            "Segoe UI Variable Text Semibold",
            "Segoe UI Variable Text",
            "Segoe UI Semibold",
            "Segoe UI",
        ]
        font.setWeight(QFont.Weight.DemiBold)
    else:
        families = ["Segoe UI Variable Text", "Segoe UI"]
    font.setFamilies(families)
    font.setPixelSize(pixel_size)
    return font


def label_font(pixel_size: int = 11) -> QFont:
    """Petal labels: Segoe UI Variable, semibold, 0.4 px letter-spacing (§8.2)."""
    font = ui_font(pixel_size, semibold=True)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4)
    return font
