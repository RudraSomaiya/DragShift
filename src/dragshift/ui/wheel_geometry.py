"""Wheel geometry and hit-testing (PRD §8.3, FR-2.3).

Pure QtGui maths: no QApplication needed. Paths are built around the origin and the widget
translates them to its centre.
"""

import cmath
import math
from collections.abc import Sequence

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath

# Sizes at 100 % scale (PRD §8.3).
WIN = 380  # window; the margin around the disc holds the painted shadow
RING_WIN = 460  # window while the More… ring is open
R_DISC = 150  # 300 px disc
R_HUB = 48  # 96 px hub
R_IN, R_OUT = 58, 142  # petal annulus
RING_IN, RING_OUT = 150, 210  # More… ring annulus, and the outer ring of an Op's children
FAN_STEP = math.radians(40)  # the width of each petal in an Op's outer ring
GAP = 6  # between neighbouring petals
CORNER = 18  # petal corner rounding
HOVER_SCALE = 1.04


def sector_angles(n: int) -> list[tuple[float, float, float]]:
    """(start, mid, end) in radians per sector, clockwise on screen from 12 o'clock (FR-2.3)."""
    if n < 1:
        return []
    step = 2 * math.pi / n
    return [(m - step / 2, m, m + step / 2) for m in (-math.pi / 2 + i * step for i in range(n))]


def fan_angles(mid: float, n: int, step: float = FAN_STEP) -> list[tuple[float, float, float]]:
    """(start, mid, end) of ``n`` sectors ``step`` wide, clockwise and centred on ``mid``: an
    Op's outer ring, fanned out beside its petal."""
    first = mid - step * (n - 1) / 2
    return [(m - step / 2, m, m + step / 2) for m in (first + i * step for i in range(n))]


def polar(angle: float, radius: float) -> QPointF:
    return QPointF(radius * math.cos(angle), radius * math.sin(angle))


def _arc(c: complex, radius: float, start: float, delta: float) -> list[complex]:
    steps = max(2, math.ceil(abs(delta) / math.radians(2)))
    return [c + cmath.rect(radius, start + delta * k / steps) for k in range(1, steps + 1)]


def _fillet(centre: complex, p: complex, q: complex) -> list[complex]:
    """Short circular arc around ``centre`` from p to q."""
    a = cmath.phase(p - centre)
    d = (cmath.phase(q - centre) - a + math.pi) % (2 * math.pi) - math.pi
    return _arc(centre, abs(p - centre), a, d)


def sector_path(a0: float, a1: float, r_in: float, r_out: float) -> QPainterPath:
    """Annular sector a0..a1 (radians, clockwise on screen) around the origin.

    Both radial edges are inset by GAP / 2, and all four corners are rounded with CORNER-px
    fillets. A single sector (a1 - a0 = 2π) becomes a C-shaped ring with its gap at the bottom.
    """
    h = GAP / 2

    def corner(
        u: complex, n: complex, r_circle: float, r: float, outer: bool
    ) -> tuple[complex, complex, complex]:
        # Fillet circle of radius r tangent to the radial edge (offset h along n) and to the arc.
        rho = r_circle - r if outer else r_circle + r
        t = math.sqrt(rho * rho - (h + r) ** 2)
        centre = t * u + (h + r) * n
        return centre, t * u + h * n, centre / abs(centre) * r_circle

    u0, u1 = cmath.rect(1, a0), cmath.rect(1, a1)
    n0, n1 = u0 * 1j, -u1 * 1j  # normals pointing into the sector
    co0, lo0, ao0 = corner(u0, n0, r_out, CORNER, True)
    co1, lo1, ao1 = corner(u1, n1, r_out, CORNER, True)
    r = CORNER  # the inner arc is short: shrink its fillets until they do not overlap
    while True:
        ci0, li0, ai0 = corner(u0, n0, r_in, r, False)
        ci1, li1, ai1 = corner(u1, n1, r_in, r, False)
        span = (cmath.phase(ai1) - cmath.phase(ai0)) % (2 * math.pi)
        if span < a1 - a0 or r <= 1:
            break
        r -= 1

    pts = [li0, lo0, *_fillet(co0, lo0, ao0)]
    outer_span = (cmath.phase(ao1) - cmath.phase(ao0)) % (2 * math.pi)
    pts += _arc(0, r_out, cmath.phase(ao0), outer_span)
    pts += [*_fillet(co1, ao1, lo1), li1, *_fillet(ci1, li1, ai1)]
    pts += _arc(0, r_in, cmath.phase(ai1), -span)
    pts += _fillet(ci0, ai0, li0)

    path = QPainterPath(QPointF(pts[0].real, pts[0].imag))
    for z in pts[1:]:
        path.lineTo(z.real, z.imag)
    path.closeSubpath()
    return path


def sector_paths(n: int, r_in: float, r_out: float) -> list[QPainterPath]:
    """The ``n`` sector paths of one annulus, in clockwise order from 12 o'clock."""
    return [sector_path(a0, a1, r_in, r_out) for a0, _, a1 in sector_angles(n)]


def hit_test(paths: Sequence[QPainterPath], pos: QPointF) -> int | None:
    """Index of the sector containing ``pos`` (relative to the wheel centre), else None.

    Gaps, the hub, the dead band around it and everything outside the annulus return None.
    """
    for i, path in enumerate(paths):
        if path.contains(pos):
            return i
    return None
