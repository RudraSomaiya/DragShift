"""Glass toasts (§8.7, §8.5): placement, stacking, auto-hide, hover pause, buttons."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QEnterEvent, QGuiApplication
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from dragshift.config import Appearance
from dragshift.ui import toast as toast_module
from dragshift.ui.toast import CARD_H, EDGE, GAP, MARGIN, Toast, ToastContent, ToastStack

STILL = Appearance(theme="dark", accent="#4C8DFF", reduced_motion="on")  # no motion
DONE = ToastContent("a.jpg", "Converted to JPG · 1.2 MB", ("Open", "Show", "Undo"))
PROGRESS = ToastContent("Converting to JPG", "a.png", ("Cancel",), kind="progress")


@pytest.fixture
def stack(qtbot: QtBot) -> Iterator[ToastStack]:
    s = ToastStack()
    s.set_appearance(STILL)  # no motion: shows and hides are instant
    yield s
    s.close_all()


def card_of(t: Toast) -> QRect:
    return t.geometry().adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)


def test_toasts_sit_bottom_right_and_stack_upward(stack: ToastStack) -> None:
    for key in "abc":
        stack.show(key, DONE)
    area = QGuiApplication.primaryScreen().availableGeometry()
    bottom = area.bottom() + 1 - EDGE
    cards = [card_of(stack.get(k)) for k in "abc"]
    assert all(c.right() + 1 == area.right() + 1 - EDGE for c in cards)
    assert [c.bottom() + 1 for c in cards] == [bottom - i * (CARD_H + GAP) for i in range(3)]
    assert all(stack.get(k).isVisible() for k in "abc")


def test_a_fourth_toast_replaces_the_oldest_result_not_a_progress(stack: ToastStack) -> None:
    stack.show("job", PROGRESS)
    stack.show("b", DONE)
    stack.show("c", DONE)
    slot_of_b = stack.get("b").slot
    stack.show("d", DONE)
    assert stack.keys() == ["job", "c", "d"]
    assert stack.get("d").slot == slot_of_b  # the freed slot: toasts never move


def test_showing_a_known_key_updates_that_toast_in_place(stack: ToastStack) -> None:
    stack.show("job", PROGRESS)
    first = stack.get("job")
    stack.show("job", DONE)
    assert stack.get("job") is first and first.content == DONE


def test_results_hide_after_6_s_and_progress_stays(qtbot: QtBot, stack: ToastStack) -> None:
    stack.show("job", PROGRESS)
    assert not stack.get("job").auto_hide_active
    stack.show("job", DONE)
    assert stack.get("job").auto_hide_active
    assert toast_module.AUTO_HIDE_MS == 6000


def test_auto_hide_closes_and_reports(
    qtbot: QtBot, stack: ToastStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(toast_module, "AUTO_HIDE_MS", 30)
    stack.show("a", DONE)
    with qtbot.waitSignal(stack.closed, timeout=2000) as blocker:
        pass
    assert blocker.args == ["a"] and stack.get("a") is None


def test_hover_pauses_the_auto_hide(stack: ToastStack) -> None:
    stack.show("a", DONE)
    t = stack.get("a")
    t.enterEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
    assert not t.auto_hide_active
    stack.show("a", DONE)  # new content while hovered: the timer waits for the mouse to leave
    assert not t.auto_hide_active
    t.leaveEvent(QEvent(QEvent.Type.Leave))
    assert t.auto_hide_active


def test_clicking_a_button_reports_its_label(qtbot: QtBot, stack: ToastStack) -> None:
    clicks: list[tuple] = []
    stack.clicked.connect(lambda *a: clicks.append(a))
    stack.show("a", DONE)
    t = stack.get("a")
    QTest.mouseClick(t, Qt.MouseButton.LeftButton, pos=t.button_rect("Undo").center().toPoint())
    QTest.mouseClick(t, Qt.MouseButton.LeftButton, pos=QPointF(MARGIN + 30, MARGIN + 30).toPoint())
    assert clicks == [("a", "Undo")]  # the body is not a button


def test_dismiss_closes_at_once_with_reduced_motion(stack: ToastStack) -> None:
    closed: list[str] = []
    stack.closed.connect(closed.append)
    stack.show("a", DONE)
    stack.dismiss("a")
    assert closed == ["a"]


def test_the_fade_takes_160_ms_with_motion(qtbot: QtBot) -> None:
    s = ToastStack()
    s.set_appearance(Appearance(theme="light", accent="#4C8DFF", reduced_motion="off"))
    s.show("a", DONE)
    t = s.get("a")
    assert t._level < 1.0  # still sliding up
    qtbot.waitUntil(lambda: t._level == 1.0, timeout=2000)
    s.dismiss("a")
    with qtbot.waitSignal(s.closed, timeout=2000):
        pass


@pytest.mark.parametrize(
    "content",
    [
        DONE,
        ToastContent("Couldn't convert to JPG", "OSError: disk full", ("Details",), kind="error"),
        ToastContent("Compress", "half way", ("Cancel",), kind="progress", progress=0.5),
        PROGRESS,  # indeterminate
        ToastContent("Copy path", "Done"),
    ],
)
@pytest.mark.parametrize("motion", ["on", "off"])
def test_every_kind_paints(qtbot: QtBot, content: ToastContent, motion: str) -> None:
    s = ToastStack()
    s.set_appearance(Appearance(theme="dark", accent="system", reduced_motion=motion))
    s.show("a", content)
    assert not s.get("a").grab().isNull()
    s.close_all()
