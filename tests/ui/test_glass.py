"""ui/glass.py: the §8.4 recipe on a generated snapshot."""

import math

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage

from dragshift.ui import glass, theme
from dragshift.ui import wheel_geometry as geo

pytestmark = pytest.mark.usefixtures("qapp")  # QPixmap needs a QGuiApplication


def snapshot(colour: QColor, dpr: float = 1.0) -> QImage:
    side = round(glass.SIZE * dpr)
    img = QImage(side, side, QImage.Format.Format_RGB32)
    img.fill(colour)
    img.setDevicePixelRatio(dpr)
    return img


def pixel(pm, dx: float, dy: float) -> QColor:
    """The pixel at (dx, dy) logical px from the centre of a glass layer."""
    img = pm.toImage()
    dpr = img.devicePixelRatio()
    c = glass.SIZE / 2
    return img.pixelColor(QPoint(math.floor((c + dx) * dpr), math.floor((c + dy) * dpr)))


def test_saturate_leaves_grey_alone_and_boosts_colour() -> None:
    img = QImage(2, 1, QImage.Format.Format_RGB32)
    img.setPixelColor(0, 0, QColor(128, 128, 128))
    img.setPixelColor(1, 0, QColor(160, 120, 100))
    out = glass.saturate(img, 1.3)
    grey = out.pixelColor(0, 0)
    assert abs(grey.red() - grey.blue()) <= 1
    assert out.pixelColor(1, 0).hsvSaturation() > QColor(160, 120, 100).hsvSaturation()


def test_blur_keeps_size_and_device_pixel_ratio() -> None:
    img = snapshot(QColor("teal"), 1.5)
    out = glass.blur(img, 1.3)
    assert out.size() == img.size()
    assert out.devicePixelRatio() == 1.5


@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
def test_layers_are_rendered_at_the_screen_scale(dpr: float) -> None:
    g = glass.render(snapshot(QColor("orange"), dpr), theme.make(dark=True))
    for layer in (g.disc, g.ring):
        assert layer.devicePixelRatio() == dpr
        assert layer.width() == round(glass.SIZE * dpr)


def test_disc_is_the_tinted_backdrop_with_a_shadow_around_it() -> None:
    t = theme.make(dark=True)
    g = glass.render(snapshot(QColor(200, 200, 200)), t)
    inside = pixel(g.disc, 0, 0)
    assert inside.alpha() == 255
    expected = theme.over(t.tint, QColor(200, 200, 200))
    assert abs(inside.red() - expected.red()) <= 6  # the blur of a flat image is flat
    shadow = pixel(g.disc, 0, geo.R_DISC + 14)  # just below the disc: the offset shadow
    assert 0 < shadow.alpha() < 255
    assert pixel(g.disc, -glass.SIZE / 2 + 1, -glass.SIZE / 2 + 1).alpha() == 0  # corner


def test_ring_layer_covers_only_the_more_annulus() -> None:
    g = glass.render(snapshot(QColor("navy")), theme.make(dark=False))
    assert pixel(g.ring, 0, -(geo.RING_IN + geo.RING_OUT) / 2).alpha() == 255
    assert pixel(g.ring, 0, -100).alpha() == 0
    assert pixel(g.ring, 0, 0).alpha() == 0


def test_light_and_dark_tint_differ() -> None:
    snap = snapshot(QColor(128, 128, 128))
    light = pixel(glass.render(snap, theme.make(dark=False)).disc, 0, 0)
    dark = pixel(glass.render(snap, theme.make(dark=True)).disc, 0, 0)
    assert light.lightness() > dark.lightness()


def test_specular_rim_is_brightest_at_twelve() -> None:
    g = glass.render(snapshot(QColor(60, 60, 60)), theme.make(dark=True))
    r = geo.R_DISC - 1.5
    top, bottom = pixel(g.disc, 0, -r), pixel(g.disc, 0, r)
    assert top.lightness() > bottom.lightness()


def test_shadow_sprite_is_made_once_per_mode_and_scale() -> None:
    dark = theme.make(dark=True)
    assert glass.shadow_sprite(dark, 1.0) is glass.shadow_sprite(dark, 1.0)
    assert glass.shadow_sprite(dark, 1.0) is not glass.shadow_sprite(theme.make(dark=False), 1.0)
