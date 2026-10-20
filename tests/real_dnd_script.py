"""A real OLE drag onto the real WheelWindow, with Shift held. Run by tests/test_real_dnd.py.

A Qt source window starts a genuine ``QDrag`` (Windows ``DoDragDrop``). A helper thread moves the
real cursor and presses the real left button and Shift with SendInput, so Windows proposes MOVE,
the effect that makes Explorer delete a source. The effect the wheel reports back is what Explorer
would act on. Prints one JSON object: {"petal": ..., "hub": ...}.

This moves the user's cursor for about two seconds. Nothing is clicked outside our own windows.
"""

import ctypes
import json
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QEventLoop, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QCursor, QDrag, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from dragshift.ui.wheel import WheelWindow

user32 = ctypes.WinDLL("user32", use_last_error=True)
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]  # fmt: skip


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]  # fmt: skip


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def send(inp: INPUT) -> None:
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def mouse(flags: int, x: int = 0, y: int = 0) -> None:
    i = INPUT(type=INPUT_MOUSE)
    i.mi = MOUSEINPUT(x, y, 0, flags, 0, 0)
    send(i)


def key(vk: int, up: bool = False) -> None:
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    send(i)


class Source(QWidget):
    """Starts a real QDrag of ``path`` as soon as the pressed mouse moves a little."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.path = path
        self.text_only = False  # drag plain text instead of the file (checklist item 6)
        self.result: Qt.DropAction | None = None
        self.resize(200, 100)
        self.setStyleSheet("background: #446;")

    def mouseMoveEvent(self, event) -> None:
        if not event.buttons() or self.result is not None:
            return
        mime = QMimeData()
        if self.text_only:
            mime.setText("just some selected text")
        else:
            mime.setUrls([QUrl.fromLocalFile(str(self.path))])
        drag = QDrag(self)
        drag.setMimeData(mime)
        # What Explorer offers for a file: copy, move or link, with Move the default.
        self.result = drag.exec(
            Qt.DropAction.CopyAction | Qt.DropAction.MoveAction | Qt.DropAction.LinkAction,
            Qt.DropAction.MoveAction,
        )


def first_petal_point(wheel: WheelWindow) -> QPoint:
    for y in range(0, wheel.height(), 4):
        for x in range(0, wheel.width(), 4):
            index = wheel._hit(QPointF(x, y))
            if index is not None and wheel._is_target(index):
                return QPoint(x, y)
    raise RuntimeError("no enabled petal found")


def physical(p: QPoint, dpr: float, screen_w: int, screen_h: int) -> tuple[int, int]:
    """Logical global point -> SendInput's 0..65535 absolute coordinates on the primary screen."""
    return int(p.x() * dpr * 65535 / (screen_w - 1)), int(p.y() * dpr * 65535 / (screen_h - 1))


def drag_onto(app: QApplication, source: Source, wheel: WheelWindow, target_local: QPoint) -> str:
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio()
    w, h = int(screen.size().width() * dpr), int(screen.size().height() * dpr)
    start = source.mapToGlobal(QPoint(100, 50))
    end = wheel.mapToGlobal(target_local)
    p0, p1 = physical(start, dpr, w, h), physical(end, dpr, w, h)
    source.result = None

    def choreography() -> None:
        def move(x: int, y: int) -> None:
            mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, x, y)
            time.sleep(0.015)

        move(*p0)
        time.sleep(0.2)
        key(VK_SHIFT)  # the dangerous case: Shift makes Windows propose MOVE
        mouse(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.1)
        for i in range(1, 9):  # a few pixels at a time, so Qt sees a drag start
            move(p0[0] + i * 40, p0[1])
        time.sleep(0.3)  # the drag is running now
        steps = 25
        for i in range(1, steps + 1):
            move(p0[0] + (p1[0] - p0[0]) * i // steps, p0[1] + (p1[1] - p0[1]) * i // steps)
        time.sleep(0.4)
        mouse(MOUSEEVENTF_LEFTUP)
        key(VK_SHIFT, up=True)

    threading.Thread(target=choreography, daemon=True).start()
    deadline = time.monotonic() + 8
    while source.result is None and time.monotonic() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.005)
    key(VK_SHIFT, up=True)  # in case the drag never ran
    if source.result is None:
        mouse(MOUSEEVENTF_LEFTUP)
    return source.result.name if source.result is not None else "NO_RESULT"


def main() -> int:
    if "--unsafe-control" in sys.argv:
        # Negative control: the forbidden acceptProposedAction(). The script must then see MOVE,
        # proving it really reproduces the Shift-drag that makes Explorer delete a source.
        from dragshift.ui import wheel as wheel_module

        wheel_module._decide = lambda e, ok: e.acceptProposedAction() if ok else e.ignore()
    app = QApplication(sys.argv)
    folder = Path(tempfile.mkdtemp(prefix="ds11-dnd-"))
    sample = folder / "sample.png"
    from PIL import Image

    Image.new("RGB", (8, 8), (10, 120, 200)).save(sample)

    screen = QGuiApplication.primaryScreen().availableGeometry()
    source = Source(sample)
    source.move(screen.left() + 80, screen.top() + 120)
    source.show()
    wheel = WheelWindow()
    results: dict[str, str] = {}
    saved = QCursor.pos()
    for name in ("petal", "hub", "text_on_petal"):
        source.text_only = name == "text_on_petal"
        wheel.open("convert", QPoint(screen.right() - 330, screen.center().y()), [sample])
        app.processEvents()
        time.sleep(0.2)
        centre = QPoint(wheel.width() // 2, wheel.height() // 2)
        target = centre if name == "hub" else first_petal_point(wheel)
        results[name] = drag_onto(app, source, wheel, target)
        wheel.close()
        app.processEvents()
        time.sleep(0.3)
    QCursor.setPos(saved)
    results["source_still_exists"] = str(sample.is_file())
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
