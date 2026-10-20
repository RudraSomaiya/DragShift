"""ui/theme.py: §8.2 tokens, light/dark, accent, reduced motion, contrast (NFR-8)."""

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QGradient

from dragshift.config import Appearance
from dragshift.platform import dwm
from dragshift.ui import theme
from dragshift.ui.theme import contrast, over


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake Windows settings: light apps, a blue accent, animations on."""
    state = {"light": True, "accent": (0, 120, 212), "animations": True}
    monkeypatch.setattr(dwm, "apps_use_light_theme", lambda: state["light"])
    monkeypatch.setattr(dwm, "accent_rgb", lambda: state["accent"])
    monkeypatch.setattr(dwm, "animations_enabled", lambda: state["animations"])
    return state


def test_tokens_match_the_prd() -> None:
    light, dark = theme.make(dark=False), theme.make(dark=True)
    assert light.tint == QColor(255, 255, 255, round(0.22 * 255))
    assert dark.tint == QColor(16, 20, 28, round(0.38 * 255))
    assert light.text == QColor("#0B1220")
    assert dark.text == QColor("#F2F5FA")
    assert light.hover.alphaF() == pytest.approx(0.55, abs=0.01)
    assert dark.hover.alphaF() == pytest.approx(0.65, abs=0.01)
    assert light.shadow.alphaF() == pytest.approx(0.30, abs=0.01)
    assert dark.shadow.alphaF() == pytest.approx(0.50, abs=0.01)


def test_system_settings_are_followed(windows: dict) -> None:
    t = theme.resolve(Appearance())
    assert not t.dark
    assert t.accent == QColor(0, 120, 212)
    assert t.gradient is None
    assert not t.reduced_motion
    windows.update(light=False, animations=False)
    t = theme.resolve(Appearance())
    assert t.dark
    assert t.reduced_motion


def test_unset_windows_theme_is_light(windows: dict) -> None:
    windows["light"] = None
    assert not theme.resolve(Appearance()).dark


def test_config_overrides_windows(windows: dict) -> None:
    t = theme.resolve(Appearance(theme="dark", accent="#FF0000", reduced_motion="on"))
    assert t.dark
    assert t.accent == QColor("#FF0000")
    assert t.reduced_motion
    assert not theme.resolve(Appearance(reduced_motion="off")).reduced_motion


def test_missing_or_bad_accent_falls_back_to_glacier(windows: dict) -> None:
    windows["accent"] = None
    t = theme.resolve(Appearance())
    assert t.accent == QColor("#4C8DFF")
    assert t.gradient == (QColor("#4C8DFF"), QColor("#8B6CFF"))
    assert theme.resolve(Appearance(accent="not a colour")).accent == QColor("#4C8DFF")


def test_the_gradient_is_used_only_for_the_fallback() -> None:
    rect = QRectF(0, 0, 10, 10)
    assert theme.make(dark=True).accent_brush(rect).gradient().type() == (
        QGradient.Type.LinearGradient
    )
    solid = theme.make(dark=True, accent=QColor("#00FF00")).accent_brush(rect, 0.5)
    assert solid.gradient() is None
    assert solid.color().alphaF() == pytest.approx(0.5, abs=0.01)


def test_reduced_motion_makes_every_duration_instant() -> None:
    durations = (theme.OPEN_MS, theme.CLOSE_MS, theme.SWAP_MS, theme.RING_MS, theme.HOVER_MS)
    assert [theme.make(dark=False, reduced_motion=True).ms(d) for d in durations] == [0] * 5
    assert [theme.make(dark=False).ms(d) for d in durations] == list(durations)


@pytest.mark.parametrize(
    ("dark", "backdrops"),
    [
        (False, [QColor(128, 128, 128), QColor(200, 210, 220), QColor("white")]),
        (True, [QColor(128, 128, 128), QColor(40, 44, 52), QColor("black")]),
    ],
)
def test_label_text_contrast_on_glass(dark: bool, backdrops: list[QColor]) -> None:
    """NFR-8: >= 4.5:1 over a petal on a desktop that suits the mode (mid grey and beyond)."""
    t = theme.make(dark=dark)
    for backdrop in backdrops:
        glass = over(t.petal, over(t.tint, backdrop))
        assert contrast(t.text, glass) >= 4.5, backdrop.name()


@pytest.mark.parametrize("accent", ["#4C8DFF", "#0078D4", "#FFB900", "#E81123", "#00CC6A"])
@pytest.mark.parametrize("dark", [False, True])
def test_hover_label_picks_the_more_readable_colour(accent: str, dark: bool) -> None:
    t = theme.make(dark=dark, accent=QColor(accent))
    fill = over(t.hover, over(t.petal, over(t.tint, t.backdrop)))
    chosen = t.on_accent()
    other = QColor("#0B1220") if chosen == QColor("white") else QColor("white")
    assert contrast(chosen, fill) >= contrast(other, fill)


def test_hover_label_on_a_purple_accent_in_light_mode_is_ink() -> None:
    """Found live: white on #680081 at 55 % over light glass was 3.8:1; ink is 4.9:1."""
    t = theme.make(dark=False, accent=QColor("#680081"))
    assert t.on_accent() == QColor("#0B1220")


def test_text_on_a_stronger_accent_fill_follows_its_alpha() -> None:
    """Found live: the 85 % pill and solid badge got the hover's ink, at about 2:1."""
    t = theme.make(dark=False, accent=QColor("#680081"))
    assert t.on_accent(0.85) == QColor("white")
    assert t.on_accent(1.0) == QColor("white")


def test_contrast_ratio_extremes() -> None:
    assert contrast(QColor("black"), QColor("white")) == pytest.approx(21.0)
    assert contrast(QColor("red"), QColor("red")) == pytest.approx(1.0)
