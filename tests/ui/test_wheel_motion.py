"""Wheel glass, theme and motion (PRD §8.2-§8.5, NFR-8)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QColor, QImage
from pytestqt.qtbot import QtBot
from test_wheel_window import AT, PNG, mime_of, petal_pos, recorder, send

from dragshift.config import Appearance
from dragshift.platform import screen
from dragshift.ui import glass
from dragshift.ui.wheel import WheelWindow

MOVING = Appearance(theme="dark", accent="#4C8DFF", reduced_motion="off")


@pytest.fixture
def moving(wheel: WheelWindow, serve_ops: Callable[[Any], None]) -> WheelWindow:
    serve_ops(4)
    wheel.set_appearance(MOVING)
    return wheel


def settle(w: WheelWindow, qtbot: QtBot) -> None:
    qtbot.waitUntil(lambda: not w.animating, timeout=2000)


# --- glass -----------------------------------------------------------------------------------
def test_snapshot_is_taken_before_the_window_shows(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_ops(3)
    seen: list[tuple[QRect, bool]] = []

    def grab(rect: QRect) -> QImage:
        seen.append((QRect(rect), wheel.isVisible()))
        img = QImage(rect.width(), rect.height(), QImage.Format.Format_RGB32)
        img.fill(QColor("grey"))
        return img

    monkeypatch.setattr(screen, "grab", grab)
    wheel.open("convert", AT, [PNG])
    (rect, visible), *_ = seen
    assert not visible  # the wheel is not in its own glass
    half = glass.SIZE // 2
    assert rect == QRect(AT - QPoint(half, half), AT + QPoint(half - 1, half - 1))


def test_swap_reuses_the_glass(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_ops(12)
    wheel.open("convert", AT, [PNG])
    calls: list = []
    original = screen.grab
    monkeypatch.setattr(screen, "grab", lambda rect: calls.append(rect) or original(rect))
    wheel.swap("tools")
    wheel.swap("convert")
    assert calls == []


# --- theme -----------------------------------------------------------------------------------
def test_appearance_applies_on_the_next_open(
    wheel: WheelWindow, serve_ops: Callable[[Any], None]
) -> None:
    serve_ops(3)
    wheel.open("convert", AT, [PNG])
    assert wheel.theme.dark
    wheel.set_appearance(Appearance(theme="light", accent="#00FF00", reduced_motion="on"))
    assert wheel.theme.dark  # not under the cursor
    wheel.open("convert", AT, [PNG])
    assert not wheel.theme.dark
    assert wheel.theme.accent == QColor("#00FF00")


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_both_modes_paint(wheel: WheelWindow, serve_ops: Callable[[Any], None], mode: str):
    serve_ops(12)
    wheel.set_appearance(Appearance(theme=mode, accent="system", reduced_motion="on"))
    wheel.open("convert", AT, [PNG, Path("b.png")])
    send(wheel, "enter", petal_pos(wheel, 1), mime_of(PNG))
    assert not wheel.grab().isNull()


# --- reduced motion (NFR-8) ------------------------------------------------------------------
def test_reduced_motion_is_instant(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], qtbot: QtBot
) -> None:
    serve_ops(4)
    wheel.open("convert", AT, [PNG])
    assert not wheel.animating
    send(wheel, "enter", petal_pos(wheel, 1), mime_of(PNG))
    assert not wheel.animating
    closed = recorder(wheel.closed)
    wheel.close()
    assert closed == [()]
    assert not wheel.isVisible()


# --- motion (§8.5) ---------------------------------------------------------------------------
def test_open_animates_then_settles(moving: WheelWindow, qtbot: QtBot) -> None:
    moving.open("convert", AT, [PNG])
    assert moving.animating
    assert not moving.grab().isNull()  # a mid-animation frame paints
    settle(moving, qtbot)


def test_hover_transitions_in_and_settles(moving: WheelWindow, qtbot: QtBot) -> None:
    moving.open("convert", AT, [PNG])
    settle(moving, qtbot)
    send(moving, "enter", petal_pos(moving, 1), mime_of(PNG))
    assert moving.animating
    assert moving.hub_text() == "Op1"  # the label changes at once; only the paint fades
    settle(moving, qtbot)
    send(moving, "move", petal_pos(moving, 2), mime_of(PNG))
    assert moving.animating
    settle(moving, qtbot)


def test_close_fades_out_before_closed(moving: WheelWindow, qtbot: QtBot) -> None:
    moving.open("convert", AT, [PNG])
    closed = recorder(moving.closed)
    moving.close()
    assert moving.isVisible()
    assert closed == []
    moving.close()  # a second close while fading waits for the first
    with qtbot.waitSignal(moving.closed, timeout=1000):
        pass
    assert not moving.isVisible()
    assert closed == [()]


def test_a_fading_wheel_takes_no_drop(moving: WheelWindow, qtbot: QtBot) -> None:
    moving.open("convert", AT, [PNG])
    settle(moving, qtbot)
    dropped = recorder(moving.dropped)
    # Enter first, as a real drag does: Qt keeps the last accepting widget as the drop target.
    send(moving, "enter", petal_pos(moving, 0), mime_of(PNG))
    moving.close()
    drop = send(moving, "drop", petal_pos(moving, 0), mime_of(PNG))
    assert not drop.isAccepted()
    assert dropped == []
    qtbot.waitUntil(lambda: not moving.isVisible(), timeout=1000)


def test_reopening_while_fading_cancels_the_close(moving: WheelWindow, qtbot: QtBot) -> None:
    moving.open("convert", AT, [PNG])
    closed = recorder(moving.closed)
    moving.close()
    moving.open("tools", AT, [PNG])
    settle(moving, qtbot)
    qtbot.wait(150)  # longer than the close fade
    assert moving.isVisible()
    assert closed == []


def test_swap_cross_fades(moving: WheelWindow, qtbot: QtBot) -> None:
    moving.open("convert", AT, [PNG])
    settle(moving, qtbot)
    moving.swap("tools")
    assert moving.animating
    assert not moving.grab().isNull()
    settle(moving, qtbot)


def test_more_ring_expands(
    moving: WheelWindow,
    serve_ops: Callable[[Any], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(WheelWindow, "MORE_DELAY_MS", 10)
    serve_ops(12)
    moving.open("convert", AT, [PNG])
    settle(moving, qtbot)
    send(moving, "enter", petal_pos(moving, 7), mime_of(PNG))  # More…
    qtbot.waitUntil(lambda: moving.ring_open, timeout=1000)
    assert moving.animating
    assert not moving.grab().isNull()
    settle(moving, qtbot)


def test_a_new_file_types_icon_loads_after_the_first_frame(
    wheel: WheelWindow,
    serve_ops: Callable[[Any], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NFR-2: the first shell icon of a type costs up to ~100 ms; it must not delay the open."""
    serve_ops(3)
    loaded: list[Path] = []
    original = WheelWindow._thumbnail
    monkeypatch.setattr(
        WheelWindow, "_thumbnail", lambda self, p: loaded.append(p) or original(self, p)
    )
    wheel.open("convert", AT, [Path("new-type.xyz")])
    assert loaded == []
    wheel.repaint()
    qtbot.waitUntil(lambda: loaded == [Path("new-type.xyz")], timeout=1000)
    wheel.repaint()
    qtbot.wait(20)
    assert len(loaded) == 1  # loaded once, then cached
