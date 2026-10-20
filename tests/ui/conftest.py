import os
from collections.abc import Callable
from typing import Any

import pytest

# Never let a UI test open a real window: the wheel is always-on-top. CI sets this too.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dragshift.engine.kinds import Kind
from dragshift.engine.registry import Op
from dragshift.ui import wheel as wheel_module
from dragshift.ui.wheel import WheelWindow


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
    qtbot.addWidget(w)
    return w
