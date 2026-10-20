import os
from collections.abc import Callable
from typing import Any

import pytest

# Never let a UI test open a real window: the wheel is always-on-top. CI sets this too.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage

from dragshift.config import Appearance
from dragshift.engine.kinds import Kind
from dragshift.engine.registry import Op
from dragshift.platform import screen
from dragshift.ui import wheel as wheel_module
from dragshift.ui.wheel import WheelWindow

# Fixed appearance for wheel tests: no Windows settings, and no motion unless a test asks for it.
STILL = Appearance(theme="dark", accent="#4C8DFF", reduced_motion="on")


@pytest.fixture(autouse=True)
def fake_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The glass snapshot is a flat grey image instead of the real desktop."""

    def grab(rect: QRect) -> QImage:
        img = QImage(rect.width(), rect.height(), QImage.Format.Format_RGB32)
        img.fill(QColor(90, 110, 140))
        return img

    monkeypatch.setattr(screen, "grab", grab)


@pytest.fixture
def make_ops() -> Callable[[int], list[Op]]:
    def make(n: int) -> list[Op]:
        return [
            Op(
                id=f"convert.image.op{i}",
                wheel="convert",
                label=f"Op{i}",
                kinds=frozenset({Kind.IMAGE}),
                func="tests.fake:run",
            )
            for i in range(n)
        ]

    return make


@pytest.fixture
def serve_ops(
    monkeypatch: pytest.MonkeyPatch, make_ops: Callable[[int], list[Op]]
) -> Callable[[Any], None]:
    """Make the wheel's Op lookup return ``n`` fake Ops, or ``{extension: n}`` per first file."""
    state: dict[str, Any] = {"n": 0}

    def fake(paths: list, _wheel: str) -> list[Op]:
        n = state["n"]
        if isinstance(n, dict):
            n = n.get(paths[0].suffix, 0) if paths else 0
        return make_ops(n) if paths else []

    monkeypatch.setattr(wheel_module, "ops_for", fake)

    def serve(n: Any) -> None:
        state["n"] = n

    return serve


@pytest.fixture
def wheel(qtbot) -> WheelWindow:
    w = WheelWindow()
    w.set_appearance(STILL)
    qtbot.addWidget(w)
    return w
