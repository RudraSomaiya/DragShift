"""GestureController drag branch (DS-10), driven by synthetic RawInput events."""

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPoint, QRect, Signal
from pytestqt.qtbot import QtBot

from dragshift import gesture
from dragshift.config import Config, General, Keys
from dragshift.gesture import GestureController, State
from dragshift.platform import drag_detect, shell
from dragshift.platform.drag_detect import is_file_drag
from dragshift.platform.hooks import InputKind, RawInput

SHIFT, LSHIFT, LCTRL, LALT = 0x10, 0xA0, 0xA2, 0xA4
ESC = 0x1B
DESKTOP_FILES = [Path("C:/Users/me/Desktop/a.png")]


class FakeWheel(QObject):
    """Same surface as WheelWindow (PRD §9.4) as far as the controller uses it."""

    closed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.visible = False
        self.geometry = QRect(0, 0, 100, 100)

    def open(self, kind: str, at: QPoint, prefill: list[Path]) -> None:
        self.calls.append(("open", kind, at, list(prefill)))
        self.visible = True

    def swap(self, kind: str) -> None:
        self.calls.append(("swap", kind))

    def close(self) -> None:
        self.calls.append(("close",))
        self.visible = False
        self.closed.emit()

    def isVisible(self) -> bool:
        return self.visible

    def frameGeometry(self) -> QRect:
        return self.geometry


@dataclasses.dataclass
class FakeProbe:
    root: tuple[int, str] = (111, "CabinetWClass")
    sys_drag: bool = True
    held: frozenset[int] = frozenset()
    threshold: tuple[int, int] = (4, 4)

    def drag_threshold(self) -> tuple[int, int]:
        return self.threshold

    def root_at(self, x: int, y: int) -> tuple[int, str]:
        return self.root

    def sys_drag_image_visible(self) -> bool:
        return self.sys_drag

    def held_modifier_vks(self) -> frozenset[int]:
        return self.held


class Rig:
    def __init__(self, probe: FakeProbe | None = None, config: Config | None = None) -> None:
        self.wheel = FakeWheel()
        self.probe = probe or FakeProbe()
        self.cursor = QPoint(500, 400)
        self.selection_calls: list[tuple[int, str]] = []

        def selection(hwnd: int, cls: str) -> list[Path]:
            self.selection_calls.append((hwnd, cls))
            return DESKTOP_FILES

        self.gc = GestureController(
            self.wheel,  # type: ignore[arg-type]
            config,
            probe=self.probe,
            selection=selection,
            cursor_pos=lambda: self.cursor,
        )

    def send(self, kind: InputKind, x: int = 0, y: int = 0, vk: int = 0) -> None:
        self.gc.on_input(RawInput(kind, x, y, vk, 0))

    def press(self, x: int = 100, y: int = 100) -> None:
        self.send(InputKind.LBUTTON_DOWN, x, y)

    def drag(self, dx: int = 20, dy: int = 0) -> None:
        self.send(InputKind.MOUSE_MOVE, 100 + dx, 100 + dy)

    def key(self, vk: int) -> None:
        self.send(InputKind.KEY_DOWN, vk=vk)

    def release_key(self, vk: int) -> None:
        self.send(InputKind.KEY_UP, vk=vk)

    def release(self) -> None:
        self.send(InputKind.LBUTTON_UP, 100, 100)

    def start_drag(self) -> None:
        self.press()
        self.drag()

    def opens(self) -> list[tuple]:
        return [c for c in self.wheel.calls if c[0] == "open"]


@pytest.fixture
def rig(qapp: object) -> Rig:
    return Rig()


# --- FR-1.2: is_file_drag -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("root_class", "sys_drag", "expected"),
    [
        ("Chrome_WidgetWin_1", True, True),  # gate (a)
        ("CabinetWClass", False, True),  # gate (b)
        ("Progman", False, True),
        ("WorkerW", False, True),
        ("Chrome_WidgetWin_1", False, False),  # neither
        ("", False, False),
    ],
)
def test_is_file_drag(root_class: str, sys_drag: bool, expected: bool) -> None:
    assert is_file_drag(root_class, sys_drag) is expected


# --- opening (FR-1.1..1.5) ------------------------------------------------------------------------
def test_modifier_during_a_file_drag_opens_the_wheel_at_the_cursor(rig: Rig) -> None:
    rig.start_drag()
    assert rig.gc.state is State.DRAGGING
    rig.key(LSHIFT)
    assert rig.opens() == [("open", "convert", QPoint(500, 400), DESKTOP_FILES)]
    assert rig.gc.state is State.WHEEL_OPEN
    assert rig.selection_calls == [(111, "CabinetWClass")]  # the window the drag started in


@pytest.mark.parametrize(
    ("vk", "kind"), [(LSHIFT, "convert"), (SHIFT, "convert"), (LCTRL, "tools"), (LALT, "actions")]
)
def test_default_mapping(rig: Rig, vk: int, kind: str) -> None:
    rig.start_drag()
    rig.key(vk)
    assert rig.opens()[0][1] == kind


def test_a_press_that_has_not_moved_far_is_not_a_drag(rig: Rig) -> None:
    rig.press()
    rig.send(InputKind.MOUSE_MOVE, 103, 103)  # within SM_CXDRAG/SM_CYDRAG
    assert rig.gc.state is State.PRESSED
    rig.key(LSHIFT)
    assert rig.opens() == []


def test_the_drag_threshold_comes_from_the_system(qapp: object) -> None:
    rig = Rig(FakeProbe(threshold=(30, 30)))
    rig.press()
    rig.drag(20)
    assert rig.gc.state is State.PRESSED
    rig.drag(31)
    assert rig.gc.state is State.DRAGGING


def test_a_modifier_with_no_drag_does_nothing(rig: Rig) -> None:
    rig.key(LSHIFT)
    rig.press()
    rig.release_key(LSHIFT)
    rig.key(LSHIFT)  # LMB is down but nothing has moved
    assert rig.opens() == []


def test_not_a_file_drag_does_nothing(qapp: object) -> None:
    rig = Rig(FakeProbe(root=(7, "Chrome_WidgetWin_1"), sys_drag=False))
    rig.start_drag()
    rig.key(LSHIFT)
    assert rig.opens() == []
    assert rig.gc.state is State.DRAGGING


def test_sys_drag_image_confirms_a_drag_that_began_outside_explorer(qapp: object) -> None:
    rig = Rig(FakeProbe(root=(7, "Chrome_WidgetWin_1"), sys_drag=True))
    rig.start_drag()
    rig.key(LSHIFT)
    assert len(rig.opens()) == 1
    assert rig.selection_calls == []  # no Explorer selection to read; dragEnter will fill in


def test_a_modifier_already_held_before_the_drag_does_not_open_a_wheel(qapp: object) -> None:
    rig = Rig(FakeProbe(held=frozenset({LSHIFT})))  # the key was down when LMB went down
    rig.start_drag()
    rig.key(LSHIFT)  # auto-repeat of the held key
    assert rig.opens() == []


def test_a_modifier_pressed_before_the_drag_is_tracked_from_events(rig: Rig) -> None:
    rig.key(LSHIFT)  # down before LMB, and the probe does not know about it either
    rig.start_drag()
    rig.key(LSHIFT)  # repeat
    assert rig.opens() == []


def test_releasing_and_pressing_again_counts_as_a_new_press(qapp: object) -> None:
    rig = Rig(FakeProbe(held=frozenset({LSHIFT})))
    rig.start_drag()
    rig.release_key(LSHIFT)
    rig.key(LSHIFT)
    assert len(rig.opens()) == 1


def test_auto_repeat_does_not_reopen_or_swap(rig: Rig) -> None:
    rig.start_drag()
    rig.key(LSHIFT)
    rig.key(LSHIFT)
    rig.key(LSHIFT)
    assert [c[0] for c in rig.wheel.calls] == ["open"]


def test_unmapped_modifier_does_nothing(qapp: object) -> None:
    config = Config(keys=Keys(convert="ctrl", tools="alt", actions="alt"))  # Shift is unmapped
    rig = Rig(config=config)
    rig.start_drag()
    rig.key(LSHIFT)
    assert rig.opens() == []
    rig.key(LCTRL)
    assert rig.opens()[0][1] == "convert"


def test_apply_config_remaps_keys_live(rig: Rig) -> None:
    rig.gc.apply_config(Config(keys=Keys(convert="alt", tools="shift", actions="ctrl")))
    rig.start_drag()
    rig.key(LSHIFT)
    assert rig.opens()[0][1] == "tools"


# --- pause (FR-1.12) ----------------------------------------------------------------------------
def test_paused_never_opens_a_wheel(rig: Rig) -> None:
    rig.gc.apply_config(Config(general=General(paused=True)))
    rig.start_drag()
    rig.key(LSHIFT)
    assert rig.opens() == []
    rig.release()
    rig.gc.apply_config(Config(general=General(paused=False)))
    rig.start_drag()
    rig.key(LCTRL)
    assert len(rig.opens()) == 1


# --- swapping (FR-1.6) --------------------------------------------------------------------------
def test_another_modifier_swaps_and_releasing_one_does_not_close(rig: Rig) -> None:
    rig.start_drag()
    rig.key(LSHIFT)
    rig.key(LCTRL)
    rig.release_key(LSHIFT)
    rig.key(LALT)
    rig.release_key(LCTRL)
    assert rig.wheel.calls[1:] == [("swap", "tools"), ("swap", "actions")]
    assert rig.gc.state is State.WHEEL_OPEN
    assert rig.wheel.visible


def test_pressing_the_open_wheels_own_modifier_again_does_not_swap(rig: Rig) -> None:
    rig.start_drag()
    rig.key(LSHIFT)
    rig.release_key(LSHIFT)
    rig.key(LSHIFT)
    assert [c[0] for c in rig.wheel.calls] == ["open"]


# --- closing (FR-1.9) ---------------------------------------------------------------------------
def test_lmb_up_without_a_wheel_goes_idle(rig: Rig) -> None:
    rig.start_drag()
    rig.release()
    assert rig.gc.state is State.IDLE
    rig.key(LSHIFT)
    assert rig.opens() == []


def test_esc_closes_the_wheel_only_after_the_cancel_has_reached_it(
    rig: Rig, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hiding the wheel at once on Esc orphans the shell's drag image (a ghost icon on screen)."""
    monkeypatch.setattr(gesture, "ESC_CLOSE_DELAY_MS", 80)
    rig.start_drag()
    rig.key(LSHIFT)
    rig.key(ESC)
    assert rig.gc.state is State.IDLE  # the gesture is over straight away...
    assert ("close",) not in rig.wheel.calls  # ...but the wheel is still there
    assert rig.wheel.visible
    qtbot.waitUntil(lambda: ("close",) in rig.wheel.calls, timeout=2000)
    assert not rig.wheel.visible


def test_the_default_esc_delay_is_long_enough_to_matter() -> None:
    """Measured with a real Explorer drag: closing 100 ms after Esc never left a ghost."""
    assert gesture.ESC_CLOSE_DELAY_MS >= 100


def test_a_pending_esc_close_does_not_hit_a_wheel_opened_meanwhile(
    rig: Rig, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gesture, "ESC_CLOSE_DELAY_MS", 80)
    rig.start_drag()
    rig.key(LSHIFT)
    rig.key(ESC)
    rig.release_key(LSHIFT)
    rig.release()
    rig.start_drag()  # a new drag within the delay
    rig.key(LSHIFT)
    assert len(rig.opens()) == 2
    qtbot.wait(200)
    assert ("close",) not in rig.wheel.calls  # the leftover close was cancelled
    assert rig.gc.state is State.WHEEL_OPEN


def test_esc_with_no_wheel_does_nothing(rig: Rig) -> None:
    rig.start_drag()
    rig.key(ESC)
    assert rig.wheel.calls == []
    assert rig.gc.state is State.DRAGGING


def test_lmb_up_outside_the_wheel_closes_it(rig: Rig) -> None:
    rig.start_drag()
    rig.key(LSHIFT)
    rig.cursor = QPoint(900, 900)  # the wheel covers (0,0)-(100,100)
    rig.release()
    assert rig.wheel.calls[-1] == ("close",)
    assert rig.gc.state is State.IDLE


def test_lmb_up_over_the_wheel_waits_for_the_drop(
    rig: Rig, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gesture, "DROP_GRACE_MS", 50)
    rig.start_drag()
    rig.key(LSHIFT)
    rig.cursor = QPoint(50, 50)
    rig.release()
    assert rig.wheel.calls[-1][0] == "open"  # not closed yet
    assert rig.gc.state is State.WHEEL_OPEN
    qtbot.waitUntil(lambda: rig.gc.state is State.IDLE, timeout=2000)  # no drop came
    assert rig.wheel.calls[-1] == ("close",)


def test_a_drop_closes_the_wheel_and_cancels_the_wait(
    rig: Rig, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gesture, "DROP_GRACE_MS", 50)
    rig.start_drag()
    rig.key(LSHIFT)
    rig.cursor = QPoint(50, 50)
    rig.release()
    rig.wheel.visible = False
    rig.wheel.closed.emit()  # what WheelWindow does after a drop (FR-1.9)
    assert rig.gc.state is State.IDLE
    qtbot.wait(120)
    assert ("close",) not in rig.wheel.calls  # the pending grace timer was cancelled


def test_the_wheel_closes_after_the_safety_timeout(
    rig: Rig, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gesture, "WHEEL_TIMEOUT_MS", 60)
    rig.start_drag()
    rig.key(LSHIFT)
    qtbot.waitUntil(lambda: rig.gc.state is State.IDLE, timeout=2000)
    assert rig.wheel.calls[-1] == ("close",)


def test_drag_movement_keeps_the_wheel_open_past_the_timeout(
    rig: Rig, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gesture, "WHEEL_TIMEOUT_MS", 120)
    rig.start_drag()
    rig.key(LSHIFT)
    for _ in range(6):  # 6 x 40 ms of movement, longer than the 120 ms timeout
        qtbot.wait(40)
        rig.drag(40)
    assert rig.gc.state is State.WHEEL_OPEN
    qtbot.waitUntil(lambda: rig.gc.state is State.IDLE, timeout=2000)


def test_the_user_closing_the_wheel_returns_to_idle(rig: Rig) -> None:
    rig.start_drag()
    rig.key(LSHIFT)
    rig.wheel.close()
    assert rig.gc.state is State.IDLE


def test_reinstalled_hooks_reset_everything(rig: Rig) -> None:
    rig.start_drag()
    rig.key(LSHIFT)
    rig.gc.on_hooks_reinstalled()
    assert rig.gc.state is State.IDLE
    assert rig.wheel.calls[-1] == ("close",)
    rig.press()
    rig.drag()
    rig.key(LSHIFT)  # key state was cleared too, so this is a fresh press
    assert len(rig.opens()) == 2


def test_other_mouse_events_are_ignored_in_the_drag_branch(rig: Rig) -> None:
    rig.start_drag()
    rig.send(InputKind.MOUSE_OTHER, 5, 5)
    assert rig.gc.state is State.DRAGGING


# --- with the real WheelWindow ----------------------------------------------------------------------
def test_drives_the_real_wheel_window(qapp: object, qtbot: QtBot) -> None:
    from dragshift.ui.wheel import WheelWindow

    wheel = WheelWindow()
    gc = GestureController(
        wheel,
        probe=FakeProbe(),
        selection=lambda *_: [],
        cursor_pos=lambda: QPoint(300, 300),
    )
    gc.on_input(RawInput(InputKind.LBUTTON_DOWN, 10, 10, 0, 0))
    gc.on_input(RawInput(InputKind.MOUSE_MOVE, 40, 10, 0, 0))
    gc.on_input(RawInput(InputKind.KEY_DOWN, 0, 0, LSHIFT, 0))
    assert wheel.isVisible()
    assert gc.state is State.WHEEL_OPEN
    gc.on_input(RawInput(InputKind.KEY_DOWN, 0, 0, LCTRL, 0))
    assert wheel.isVisible()
    gc.on_input(RawInput(InputKind.KEY_DOWN, 0, 0, ESC, 0))
    assert gc.state is State.IDLE
    qtbot.waitUntil(lambda: not wheel.isVisible(), timeout=2000)  # closes after ESC_CLOSE_DELAY_MS


# --- the real probes and Explorer COM, smoke-tested on this desktop ----------------------------------
def test_win32_probe_answers_sensibly() -> None:
    probe = drag_detect.Win32Probe()
    dx, dy = probe.drag_threshold()
    assert dx > 0 and dy > 0
    hwnd, cls = probe.root_at(0, 0)
    assert isinstance(hwnd, int) and isinstance(cls, str)
    assert probe.root_at(-30000, -30000) == (0, "")
    assert isinstance(probe.sys_drag_image_visible(), bool)
    assert probe.held_modifier_vks() <= set(drag_detect.MODIFIER_VKS)


def test_paths_from_items_skips_virtual_items() -> None:
    class FakeItem:
        def __init__(self, path: str) -> None:
            self.Path = path

    class Items:
        def __init__(self, *paths: str) -> None:
            self._items = [FakeItem(p) for p in paths]
            self.Count = len(self._items)

        def Item(self, i: int) -> FakeItem:
            return self._items[i]

    items = Items("C:\\a.png", "::{645FF040-5081-101B-9F08-00AA002F954E}", "\\\\nas\\share\\b.pdf")
    assert shell.paths_from_items(items) == [Path("C:\\a.png"), Path("\\\\nas\\share\\b.pdf")]


def test_selected_paths_never_raises() -> None:
    assert isinstance(shell.selected_paths(0, "Progman"), list)
    assert shell.selected_paths(0, "CabinetWClass") == []  # no such window
    assert shell.selected_paths(0, "Chrome_WidgetWin_1") == []  # not an Explorer view


# --- the UI process stays lean (CLAUDE.md rule 2) ----------------------------------------------------
def test_gesture_modules_import_no_engine_libraries() -> None:
    heavy = ["PIL", "fitz", "pandas", "numpy", "pdf2docx", "pypandoc"]
    script = (
        f"import sys, dragshift.gesture;print(','.join(m for m in {heavy!r} if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
