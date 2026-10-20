"""platform/screen.py: logical-to-physical mapping and the BitBlt snapshot (PRD §8.4 step 1)."""

from PySide6.QtCore import QRect
from PySide6.QtGui import QImage

from dragshift.platform import screen


class FakeScreen:
    def __init__(self, geometry: QRect, dpr: float) -> None:
        self._geometry, self._dpr = geometry, dpr

    def geometry(self) -> QRect:
        return self._geometry

    def devicePixelRatio(self) -> float:
        return self._dpr


def test_primary_screen_at_100_percent_is_unchanged() -> None:
    s = FakeScreen(QRect(0, 0, 1920, 1080), 1.0)
    assert screen.physical_rect(QRect(100, 50, 460, 460), s) == QRect(100, 50, 460, 460)


def test_scaled_second_monitor_keeps_its_origin_and_scales_offsets() -> None:
    # A 150 % monitor to the right of a 1920 px one: its logical origin is its physical origin.
    s = FakeScreen(QRect(1920, 0, 1707, 960), 1.5)
    got = screen.physical_rect(QRect(2020, 100, 460, 460), s)
    assert got == QRect(1920 + 150, 150, 690, 690)


def test_grab_returns_an_opaque_image_of_the_requested_size() -> None:
    """A read-only copy of a few desktop pixels."""
    img = screen.grab(QRect(0, 0, 8, 6))
    assert (img.width(), img.height()) == (8, 6)
    assert img.format() == QImage.Format.Format_RGB32
