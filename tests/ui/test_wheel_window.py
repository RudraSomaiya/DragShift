import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QGuiApplication
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from dragshift.ui import wheel as wheel_module
from dragshift.ui import wheel_geometry as geo
from dragshift.ui.wheel import WheelWindow

AT = QPoint(400, 300)  # inside the 800 x 600 offscreen screen, so no clamping
PNG = Path("a.png")
ALL_ACTIONS = Qt.DropAction.MoveAction | Qt.DropAction.CopyAction | Qt.DropAction.LinkAction


def mime_of(*paths: Path) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p.resolve())) for p in paths])
    return mime


def text_mime() -> QMimeData:
    mime = QMimeData()
    mime.setText("not a file")
    return mime


def centre(w: WheelWindow) -> QPointF:
    return QPointF(w.width() / 2, w.height() / 2)


def petal_pos(w: WheelWindow, i: int, r: float = 100) -> QPointF:
    _, mid, _ = geo.sector_angles(len(w.layout.petals))[i]
    return centre(w) + geo.polar(mid, r)


def ring_pos(w: WheelWindow, i: int, r: float = 180) -> QPointF:
    _, mid, _ = geo.sector_angles(len(w.layout.ring))[i]
    return centre(w) + geo.polar(mid, r)


def send(w: WheelWindow, kind: str, pos: QPointF, mime: QMimeData) -> Any:
    """Deliver a drag event the way Explorer's drag would with Shift held, proposing a Move."""
    shift = Qt.KeyboardModifier.ShiftModifier
    left = Qt.MouseButton.LeftButton
    if kind == "enter":
        event: Any = QDragEnterEvent(pos, ALL_ACTIONS, mime, left, shift)
    elif kind == "move":
        event = QDragMoveEvent(pos, ALL_ACTIONS, mime, left, shift)
    else:
        event = QDropEvent(pos, ALL_ACTIONS, mime, left, shift)
    event.setDropAction(Qt.DropAction.MoveAction)
    QApplication.sendEvent(w, event)
    return event


def is_copy_accepted(event: Any) -> bool:
    return event.isAccepted() and event.dropAction() == Qt.DropAction.CopyAction


def begin(w: WheelWindow, mime: QMimeData) -> None:
    """Start a drag over the first petal."""
    assert is_copy_accepted(send(w, "enter", petal_pos(w, 0), mime))


def recorder(signal: Any) -> list:
    got: list = []
    signal.connect(lambda *args: got.append(args))
    return got


# --- window ---------------------------------------------------------------------------------
def test_window_flags_never_take_focus(wheel: WheelWindow) -> None:
    flags = wheel.windowFlags()
    for flag in (
        Qt.WindowType.FramelessWindowHint,
        Qt.WindowType.Tool,
        Qt.WindowType.WindowStaysOnTopHint,
        Qt.WindowType.WindowDoesNotAcceptFocus,
    ):
        assert flags & flag
    assert wheel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert wheel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert wheel.acceptDrops()


def test_open_centres_the_wheel_on_the_cursor(
    wheel: WheelWindow, serve_ops: Callable[[Any], None]
) -> None:
    serve_ops(5)
    wheel.open("convert", AT, [PNG])
    assert wheel.isVisible()
    assert len(wheel.layout.petals) == 5
    assert wheel.mapToGlobal(QPoint(wheel.width() // 2, wheel.height() // 2)) == AT


@pytest.mark.parametrize("at", (QPoint(0, 0), QPoint(799, 599), QPoint(10_000, 10_000)))
def test_open_clamps_to_the_work_area(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], at: QPoint
) -> None:
    serve_ops(5)
    wheel.open("convert", at, [PNG])
    assert QGuiApplication.primaryScreen().availableGeometry().contains(wheel.geometry())


def test_opening_the_ring_near_an_edge_stays_on_screen(
    wheel: WheelWindow,
    serve_ops: Callable[[Any], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(WheelWindow, "MORE_DELAY_MS", 10)
    serve_ops(12)
    wheel.open("convert", QPoint(0, 0), [PNG])
    mime = mime_of(PNG)
    begin(wheel, mime)
    send(wheel, "move", petal_pos(wheel, 7), mime)
    qtbot.waitUntil(lambda: wheel.ring_open)
    assert QGuiApplication.primaryScreen().availableGeometry().contains(wheel.geometry())


# --- hover (FR-1.7) -------------------------------------------------------------------------
def test_hover_shows_the_petal_label_in_the_hub(
    wheel: WheelWindow, serve_ops: Callable[[Any], None]
) -> None:
    serve_ops(5)
    wheel.open("convert", AT, [PNG])
    assert wheel.hub_text() == ""
    send(wheel, "enter", petal_pos(wheel, 2), mime_of(PNG))
    assert wheel.hub_text() == wheel.layout.petals[2].label
    send(wheel, "move", petal_pos(wheel, 4), mime_of(PNG))
    assert wheel.hub_text() == wheel.layout.petals[4].label
    send(wheel, "move", centre(wheel), mime_of(PNG))  # the hub
    assert wheel.hub_text() == ""
    send(wheel, "move", petal_pos(wheel, 0), mime_of(PNG))
    wheel.dragLeaveEvent(None)
    assert wheel.hub_text() == ""


# --- drop safety (FR-1.8, FR-1.9) -----------------------------------------------------------
def test_drop_on_an_enabled_petal_is_a_copy(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], qtbot: QtBot
) -> None:
    serve_ops(5)
    wheel.open("convert", AT, [PNG])
    mime = mime_of(PNG)
    enter = send(wheel, "enter", petal_pos(wheel, 1), mime)
    move = send(wheel, "move", petal_pos(wheel, 1), mime)
    assert is_copy_accepted(enter) and is_copy_accepted(move)  # never the Move Explorer proposed

    dropped = recorder(wheel.dropped)
    with qtbot.waitSignal(wheel.closed, timeout=2000):
        drop = send(wheel, "drop", petal_pos(wheel, 1), mime)
    assert is_copy_accepted(drop)
    assert dropped == [(wheel.layout.petals[1].op_id, [PNG.resolve()])]
    assert not wheel.isVisible()


@pytest.mark.parametrize(
    "where",
    ("hub", "gap", "dead_band", "outside_disc", "window_corner"),
)
def test_drop_anywhere_else_is_ignored_and_closes(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], qtbot: QtBot, where: str
) -> None:
    serve_ops(5)
    wheel.open("convert", AT, [PNG])
    _, _, boundary = geo.sector_angles(5)[0]
    pos = {
        "hub": centre(wheel),
        "gap": centre(wheel) + geo.polar(boundary, 100),
        "dead_band": centre(wheel) + geo.polar(0, 53),
        "outside_disc": centre(wheel) + geo.polar(math.pi / 3, 170),
        "window_corner": QPointF(3, 3),
    }[where]
    mime = mime_of(PNG)
    dropped = recorder(wheel.dropped)

    assert is_copy_accepted(send(wheel, "enter", pos, mime))  # the enter is always accepted
    assert not send(wheel, "move", pos, mime).isAccepted()  # the move Qt sends right after
    with qtbot.waitSignal(wheel.closed, timeout=2000):
        drop = send(wheel, "drop", pos, mime)
    assert not drop.isAccepted()
    assert dropped == []
    assert not wheel.isVisible()


def test_a_drag_entering_over_the_hub_still_reaches_the_petals(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], qtbot: QtBot
) -> None:
    """The wheel opens with the cursor on the hub, so that is where the first enter lands. Windows
    delivers later moves and the drop only to a target that accepted the enter."""
    serve_ops(5)
    wheel.open("convert", AT, [PNG])
    mime = mime_of(PNG)
    assert is_copy_accepted(send(wheel, "enter", centre(wheel), mime))
    assert not send(wheel, "move", centre(wheel), mime).isAccepted()  # still none over the hub
    assert is_copy_accepted(send(wheel, "move", petal_pos(wheel, 1), mime))
    assert wheel.hub_text() == wheel.layout.petals[1].label

    dropped = recorder(wheel.dropped)
    with qtbot.waitSignal(wheel.closed, timeout=2000):
        drop = send(wheel, "drop", petal_pos(wheel, 1), mime)
    assert is_copy_accepted(drop)
    assert dropped == [(wheel.layout.petals[1].op_id, [PNG.resolve()])]


# --- unsupported data (FR-1.10) -------------------------------------------------------------
def test_data_without_local_files_disables_every_petal(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], qtbot: QtBot
) -> None:
    serve_ops(5)
    wheel.open("convert", AT, [PNG])
    assert all(p.enabled for p in wheel.layout.petals)
    dropped = recorder(wheel.dropped)

    mime = text_mime()
    send(wheel, "enter", petal_pos(wheel, 0), mime)
    assert not send(wheel, "move", petal_pos(wheel, 0), mime).isAccepted()  # nothing to drop on
    assert wheel.hub_text() == "Unsupported"
    assert len(wheel.layout.petals) == 5
    assert not any(p.enabled for p in wheel.layout.petals)

    with qtbot.waitSignal(wheel.closed, timeout=2000):
        drop = send(wheel, "drop", petal_pos(wheel, 0), mime)
    assert not drop.isAccepted()
    assert dropped == []


def test_files_with_no_valid_ops_show_unsupported(
    wheel: WheelWindow, serve_ops: Callable[[Any], None]
) -> None:
    serve_ops(0)
    wheel.open("convert", AT, [PNG])
    assert wheel.layout.petals == ()
    assert wheel.hub_text() == "Unsupported"


def test_no_prefill_and_no_files_is_unsupported(
    wheel: WheelWindow, serve_ops: Callable[[Any], None]
) -> None:
    serve_ops(5)
    wheel.open("convert", AT)
    assert wheel.layout.petals == ()
    assert wheel.hub_text() == ""  # nothing known yet, so not "Unsupported"
    send(wheel, "enter", centre(wheel), text_mime())
    assert wheel.hub_text() == "Unsupported"


# --- dropped paths are authoritative (FR-1.5) -----------------------------------------------
def test_dragged_paths_replace_the_prefill(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], qtbot: QtBot
) -> None:
    serve_ops({".png": 3, ".pdf": 5})
    wheel.open("convert", AT, [PNG])
    assert len(wheel.layout.petals) == 3
    pdf = Path("b.pdf")
    mime = mime_of(pdf)
    begin(wheel, mime)  # the first petal is at 12 o'clock in every layout
    assert len(wheel.layout.petals) == 5

    with qtbot.waitSignal(wheel.dropped, timeout=2000) as blocker:
        send(wheel, "drop", petal_pos(wheel, 0), mime)
    assert blocker.args[1] == [pdf.resolve()]


# --- More… ring (FR-2.1) --------------------------------------------------------------------
@pytest.fixture
def ringed(
    wheel: WheelWindow, serve_ops: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
) -> WheelWindow:
    monkeypatch.setattr(WheelWindow, "MORE_DELAY_MS", 30)
    serve_ops(12)  # 7 petals + More… + a ring of 5
    wheel.open("convert", AT, [PNG])
    return wheel


def open_ring(w: WheelWindow, qtbot: QtBot, mime: QMimeData) -> None:
    begin(w, mime)
    send(w, "move", petal_pos(w, 7), mime)
    assert not w.ring_open  # not before the hover delay
    qtbot.waitUntil(lambda: w.ring_open, timeout=2000)


def test_more_petal_holds_the_remaining_ops(ringed: WheelWindow) -> None:
    layout = ringed.layout
    assert len(layout.petals) == 8 and layout.petals[7].is_more
    assert len(layout.ring) == 5
    assert not ringed.ring_open


def test_hovering_more_opens_the_ring_around_the_same_centre(
    ringed: WheelWindow, qtbot: QtBot
) -> None:
    open_ring(ringed, qtbot, mime_of(PNG))
    assert ringed.size().width() == geo.RING_WIN
    assert ringed.mapToGlobal(QPoint(ringed.width() // 2, ringed.height() // 2)) == AT


def test_leaving_more_before_the_delay_keeps_the_ring_closed(
    ringed: WheelWindow, qtbot: QtBot
) -> None:
    mime = mime_of(PNG)
    begin(ringed, mime)
    send(ringed, "move", petal_pos(ringed, 7), mime)
    send(ringed, "move", petal_pos(ringed, 0), mime)
    qtbot.wait(150)
    assert not ringed.ring_open


def test_ring_petals_are_not_hit_while_the_ring_is_closed(ringed: WheelWindow) -> None:
    send(ringed, "enter", petal_pos(ringed, 0), mime_of(PNG))
    ring_probe = centre(ringed) + geo.polar(-math.pi / 2, 180)
    event = send(ringed, "move", ring_probe, mime_of(PNG))
    assert not event.isAccepted()
    assert ringed.hub_text() == ""


def test_drop_on_more_itself_is_ignored(ringed: WheelWindow, qtbot: QtBot) -> None:
    mime = mime_of(PNG)
    dropped = recorder(ringed.dropped)
    begin(ringed, mime)
    assert not send(ringed, "move", petal_pos(ringed, 7), mime).isAccepted()
    with qtbot.waitSignal(ringed.closed, timeout=2000):
        drop = send(ringed, "drop", petal_pos(ringed, 7), mime)
    assert not drop.isAccepted()
    assert dropped == []


def test_drop_on_a_ring_petal_is_a_copy(ringed: WheelWindow, qtbot: QtBot) -> None:
    mime = mime_of(PNG)
    open_ring(ringed, qtbot, mime)
    send(ringed, "move", ring_pos(ringed, 2), mime)
    assert ringed.hub_text() == ringed.layout.ring[2].label
    dropped = recorder(ringed.dropped)
    with qtbot.waitSignal(ringed.closed, timeout=2000):
        drop = send(ringed, "drop", ring_pos(ringed, 2), mime)
    assert is_copy_accepted(drop)
    assert dropped == [(ringed.layout.ring[2].op_id, [PNG.resolve()])]


def test_closing_collapses_the_ring(ringed: WheelWindow, qtbot: QtBot) -> None:
    open_ring(ringed, qtbot, mime_of(PNG))
    ringed.close()
    assert not ringed.ring_open
    ringed.open("convert", AT, [PNG])
    assert ringed.size().width() == geo.WIN


# --- swap and ordering (FR-1.6, FR-2.2, FR-2.3) ---------------------------------------------
def test_swap_shows_the_other_wheel_in_the_same_place(
    wheel: WheelWindow, make_ops: Callable[[int], list], monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = {"convert": 3, "tools": 6}
    monkeypatch.setattr(
        wheel_module, "ops_for", lambda paths, kind: make_ops(counts[kind]) if paths else []
    )
    wheel.open("convert", AT, [PNG])
    assert len(wheel.layout.petals) == 3
    wheel.swap("tools")
    assert len(wheel.layout.petals) == 6
    assert wheel.mapToGlobal(QPoint(wheel.width() // 2, wheel.height() // 2)) == AT


def test_petals_follow_pinned_order_then_usage(
    wheel: WheelWindow, serve_ops: Callable[[Any], None]
) -> None:
    serve_ops(4)
    wheel.set_ordering(
        pinned={"image": ("convert.image.op2",)},
        usage={"image": {"convert.image.op3": 7}},
    )
    wheel.open("convert", AT, [PNG])
    got = [p.op_id for p in wheel.layout.petals]
    assert got == [f"convert.image.op{i}" for i in (2, 3, 0, 1)]


def test_ordering_changes_wait_for_the_next_open(
    wheel: WheelWindow, serve_ops: Callable[[Any], None]
) -> None:
    serve_ops(3)
    wheel.open("convert", AT, [PNG])
    before = wheel.layout
    wheel.set_ordering(pinned={"image": ("convert.image.op2",)})
    assert wheel.layout == before  # petals never move under the cursor
    wheel.open("convert", AT, [PNG])
    assert wheel.layout.petals[0].op_id == "convert.image.op2"


# --- painting and closing -------------------------------------------------------------------
def test_every_state_paints(
    wheel: WheelWindow,
    serve_ops: Callable[[Any], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(WheelWindow, "MORE_DELAY_MS", 10)
    serve_ops(12)
    wheel.open("convert", AT, [PNG, Path("b.png"), Path("c.png")])  # count badge
    assert not wheel.grab().isNull()
    send(wheel, "enter", petal_pos(wheel, 3), mime_of(PNG))  # hovered petal
    assert not wheel.grab().isNull()
    send(wheel, "move", petal_pos(wheel, 7), mime_of(PNG))  # More…, then the open ring
    qtbot.waitUntil(lambda: wheel.ring_open)
    send(wheel, "move", ring_pos(wheel, 1), mime_of(PNG))
    assert not wheel.grab().isNull()
    send(wheel, "enter", centre(wheel), text_mime())  # unsupported, disabled petals
    assert not wheel.grab().isNull()


def test_close_emits_closed(wheel: WheelWindow, serve_ops: Callable[[Any], None], qtbot: QtBot):
    serve_ops(3)
    wheel.open("convert", AT, [PNG])
    with qtbot.waitSignal(wheel.closed, timeout=1000):
        wheel.close()


def test_ui_sources_never_accept_the_proposed_action() -> None:
    """FR-1.8: that call reports Move while Shift is held, and Explorer then deletes the source."""
    ui = Path(wheel_module.__file__).parent
    offenders = [p.name for p in ui.rglob("*.py") if "acceptProposedAction" in p.read_text("utf-8")]
    assert offenders == []


# --- an Op's own ring (Copy to ▸) ----------------------------------------------------------
@pytest.fixture
def pinned() -> Any:
    from dragshift.engine.actions import files as actions

    actions.pin_folders(["C:/Work/Projects", "D:/"])
    yield
    actions.pin_folders(())


def fan_pos(w: WheelWindow, owner: int, i: int) -> QPointF:
    _, mid, _ = geo.sector_angles(len(w.layout.petals))[owner]
    n = len(w.layout.petals[owner].ring)
    return centre(w) + geo.polar(geo.fan_angles(mid, n)[i][1], 180)


def test_copy_to_opens_a_ring_of_pinned_folders_and_only_they_take_drops(
    wheel: WheelWindow, pinned: None, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(WheelWindow, "MORE_DELAY_MS", 30)
    wheel.open("actions", AT, [PNG])
    labels = [p.label for p in wheel.layout.petals]
    owner = labels.index("Copy to ▸")
    assert [p.label for p in wheel.layout.petals[owner].ring] == ["Projects", "D:"]
    mime = mime_of(PNG)
    begin(wheel, mime)
    assert not send(wheel, "move", petal_pos(wheel, owner), mime).isAccepted()  # no drop here
    qtbot.waitUntil(lambda: wheel.ring_open, timeout=2000)
    assert wheel.size().width() == geo.RING_WIN
    target = fan_pos(wheel, owner, 1)
    assert is_copy_accepted(send(wheel, "move", target, mime))
    assert wheel.hub_text() == "D:"
    dropped = recorder(wheel.dropped)
    with qtbot.waitSignal(wheel.closed, timeout=2000):
        assert is_copy_accepted(send(wheel, "drop", target, mime))
    assert dropped == [("actions.copy_to.2", [PNG.resolve()])]


def test_hovering_another_op_with_a_ring_swaps_the_ring(
    wheel: WheelWindow, pinned: None, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(WheelWindow, "MORE_DELAY_MS", 30)
    wheel.open("actions", AT, [PNG])
    labels = [p.label for p in wheel.layout.petals]
    copy, move = labels.index("Copy to ▸"), labels.index("Move to ▸")
    mime = mime_of(PNG)
    begin(wheel, mime)
    send(wheel, "move", petal_pos(wheel, copy), mime)
    qtbot.waitUntil(lambda: wheel.ring_open, timeout=2000)
    send(wheel, "move", petal_pos(wheel, move), mime)
    qtbot.waitUntil(lambda: wheel._ring_of == move, timeout=2000)
    target = fan_pos(wheel, move, 0)
    assert is_copy_accepted(send(wheel, "move", target, mime))
    assert wheel.hub_text() == "Projects"
    dropped = recorder(wheel.dropped)
    send(wheel, "drop", target, mime)
    assert dropped[0][0] == "actions.move_to.1"
