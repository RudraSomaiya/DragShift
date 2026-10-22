"""The Acrylic dialog base (PRD §8.6): title bar, fallback panel, styled controls."""

import pytest
from PySide6.QtCore import QPoint, Qt
from pytestqt.qtbot import QtBot

from dragshift.platform import dwm
from dragshift.ui import theme
from dragshift.ui.dialogs.base_glass import (
    TITLE_BAR,
    GlassDialog,
    Segmented,
    primary_button,
    stylesheet,
)
from dragshift.ui.theme import css as css_of


@pytest.mark.parametrize("dark", [False, True])
def test_the_title_bar_is_40px_with_a_close_button_at_the_left(qtbot: QtBot, dark: bool) -> None:
    dialog = GlassDialog("Crop Image", theme.make(dark=dark))
    qtbot.addWidget(dialog)
    assert dialog.title_bar.height() == TITLE_BAR == 40
    assert dialog.close_button.x() < dialog.title_bar.width() / 2
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def test_close_button_rejects(qtbot: QtBot) -> None:
    dialog = GlassDialog("x", theme.make(dark=True))
    qtbot.addWidget(dialog)
    with qtbot.waitSignal(dialog.rejected):
        qtbot.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))


def test_the_fallback_panel_is_used_when_dwm_refuses(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dwm, "apply_acrylic", lambda hwnd, dark: False)
    dialog = GlassDialog("x", theme.make(dark=False))
    qtbot.addWidget(dialog)
    dialog.show()
    assert not dialog.acrylic
    assert (
        dialog.grab().toImage().pixelColor(dialog.width() // 2, dialog.height() // 2).alpha() > 240
    )


def test_acrylic_is_used_when_dwm_accepts(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dwm, "apply_acrylic", lambda hwnd, dark: True)
    dialog = GlassDialog("x", theme.make(dark=True))
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.acrylic


def test_windows_10_gets_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class V:
        build = 19045

    monkeypatch.setattr(dwm.sys, "getwindowsversion", lambda: V)
    assert dwm.apply_acrylic(0, True) is False


def test_segmented_control_is_exclusive(qtbot: QtBot) -> None:
    seg = Segmented(["Free", "1:1", "16:9"])
    qtbot.addWidget(seg)
    with qtbot.waitSignal(seg.changed) as sig:
        seg.buttons["16:9"].click()
    assert sig.args == ["16:9"]
    assert [n for n, b in seg.buttons.items() if b.isChecked()] == ["16:9"]


def test_primary_button_and_stylesheet_use_the_accent(qtbot: QtBot) -> None:
    t = theme.make(dark=True)
    assert primary_button("Apply").property("primary") is True
    assert t.accent.name() in stylesheet(t)


def test_dwm_failure_never_raises() -> None:
    assert dwm.apply_acrylic(0, True) in (True, False)  # a bogus HWND: DWM refuses, no crash


def test_set_theme_restyles_and_repaints(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dwm, "apply_acrylic", lambda hwnd, dark: True)
    dialog = GlassDialog("x", theme.make(dark=False))
    qtbot.addWidget(dialog)
    dialog.show()
    dark = theme.make(dark=True)
    dialog.set_theme(dark)
    assert dialog.theme is dark
    assert css_of(dark.text) in dialog.styleSheet()


def test_disabled_controls_look_disabled() -> None:
    assert ":disabled" in stylesheet(theme.make(dark=True))


def test_close_button_is_named(qtbot: QtBot) -> None:
    dialog = GlassDialog("x", theme.make(dark=True))
    qtbot.addWidget(dialog)
    assert dialog.close_button.accessibleName() == "Close"
