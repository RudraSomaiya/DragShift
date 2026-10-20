"""GestureController: the drag branch of the gesture state machine (PRD §9.5).

    IDLE --LMB down--> PRESSED --moved past SM_CXDRAG/SM_CYDRAG--> DRAGGING
    PRESSED/DRAGGING --LMB up--> IDLE
    DRAGGING --mapped modifier key-down, and is_file_drag()--> WHEEL_OPEN(wheel)
    WHEEL_OPEN --another mapped modifier down--> WHEEL_OPEN(other wheel)         (swap, FR-1.6)
    WHEEL_OPEN --LMB up outside / Esc / 30 s / any close--> IDLE                 (FR-1.9)

It consumes ``RawInput`` events from the HookThread on the Qt thread and drives a WheelWindow.
It never inspects the desktop inside a hook callback. The tap branch (Quick Launch) comes with
DS-15 and extends the same state enum.

Wiring (done in app.py): ``hooks.raw_input -> on_input``, ``hooks.reinstalled ->
on_hooks_reinstalled``, ``store.changed -> apply_config``.
"""

import logging
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject, QPoint, QTimer, Slot
from PySide6.QtGui import QCursor

from dragshift.config import Config
from dragshift.platform.drag_detect import EXPLORER_CLASSES, Win32Probe, is_file_drag
from dragshift.platform.hooks import InputKind, RawInput
from dragshift.platform.shell import selected_paths

if TYPE_CHECKING:
    from dragshift.engine.registry import Wheel
    from dragshift.ui.wheel import WheelWindow

__all__ = ["GestureController", "Probe", "State"]

log = logging.getLogger(__name__)

WHEEL_TIMEOUT_MS = 30_000  # FR-1.9: close after this long with no drag input
DROP_GRACE_MS = 400  # after LMB up over the wheel, wait this long for the drop to arrive
VK_ESCAPE = 0x1B
# Left/right/generic virtual keys -> modifier name, as used by config.keys
MODIFIER_NAMES = {
    0x10: "shift",
    0xA0: "shift",
    0xA1: "shift",
    0x11: "ctrl",
    0xA2: "ctrl",
    0xA3: "ctrl",
    0x12: "alt",
    0xA4: "alt",
    0xA5: "alt",
}


class State(Enum):
    IDLE = auto()
    PRESSED = auto()
    DRAGGING = auto()
    WHEEL_OPEN = auto()


class Probe(Protocol):
    """What the controller asks the desktop. ``Win32Probe`` is the real one; tests fake it."""

    def drag_threshold(self) -> tuple[int, int]: ...
    def root_at(self, x: int, y: int) -> tuple[int, str]: ...
    def sys_drag_image_visible(self) -> bool: ...
    def held_modifier_vks(self) -> frozenset[int]: ...


class GestureController(QObject):
    def __init__(
        self,
        wheel: "WheelWindow",
        config: Config | None = None,
        *,
        probe: Probe | None = None,
        selection: Callable[[int, str], list[Path]] = selected_paths,
        cursor_pos: Callable[[], QPoint] = QCursor.pos,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._wheel = wheel
        self._probe: Probe = probe or Win32Probe()
        self._selection = selection
        self._cursor_pos = cursor_pos
        self._state = State.IDLE
        self._down_vks: set[int] = set()  # keys currently down, from the event stream
        self._held_at_press: set[int] = set()  # modifiers already down when LMB went down
        self._press_pos = (0, 0)
        self._press_root: tuple[int, str] = (0, "")
        self._open_kind: Wheel | None = None
        self._kind_for_modifier: dict[str, Wheel] = {}
        self._paused = False

        self._timeout = QTimer(self)  # FR-1.9 safety timeout
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self._grace = QTimer(self)  # LMB released over the wheel: wait for the drop
        self._grace.setSingleShot(True)
        self._grace.timeout.connect(self._on_grace_over)
        wheel.closed.connect(self._on_wheel_closed)
        self.apply_config(config or Config())

    @property
    def state(self) -> State:
        return self._state

    @Slot(object)
    def apply_config(self, config: Config) -> None:
        """Modifier-to-wheel mapping (FR-1.3) and Pause (FR-1.12)."""
        mapping: dict[str, Wheel] = {}
        for kind, modifier in (
            ("convert", config.keys.convert),
            ("tools", config.keys.tools),
            ("actions", config.keys.actions),
        ):
            mapping.setdefault(modifier, kind)  # if two wheels share a key, the first wins
        self._kind_for_modifier = mapping
        self._paused = config.general.paused

    @Slot()
    def on_hooks_reinstalled(self) -> None:
        """The hooks were re-installed, so events were missed (a button-up, say): start over."""
        self._down_vks.clear()
        self._close_wheel()
        self._to_idle()

    @Slot(object)
    def on_input(self, ev: RawInput) -> None:
        kind = ev.kind
        if kind is InputKind.KEY_DOWN:
            self._key_down(ev.vk)
        elif kind is InputKind.KEY_UP:
            self._down_vks.discard(ev.vk)
            self._held_at_press.discard(ev.vk)  # released: pressing it again is a new press
        elif kind is InputKind.LBUTTON_DOWN:
            self._lmb_down(ev.x, ev.y)
        elif kind is InputKind.MOUSE_MOVE:
            self._move(ev.x, ev.y)
        elif kind is InputKind.LBUTTON_UP:
            self._lmb_up()
        # MOUSE_OTHER only matters to the tap branch (DS-15)

    # --- mouse
    def _lmb_down(self, x: int, y: int) -> None:
        if self._state is State.WHEEL_OPEN:
            return
        self._state = State.PRESSED
        self._press_pos = (x, y)
        self._press_root = self._probe.root_at(x, y)
        self._held_at_press = set(self._down_vks) | set(self._probe.held_modifier_vks())

    def _move(self, x: int, y: int) -> None:
        if self._state is State.PRESSED:
            threshold_x, threshold_y = self._probe.drag_threshold()
            if (
                abs(x - self._press_pos[0]) > threshold_x
                or abs(y - self._press_pos[1]) > threshold_y
            ):
                self._state = State.DRAGGING
        elif self._state is State.WHEEL_OPEN:
            self._timeout.start(WHEEL_TIMEOUT_MS)  # still dragging: not "no drag events"

    def _lmb_up(self) -> None:
        if self._state is State.WHEEL_OPEN:
            if self._wheel.frameGeometry().contains(self._cursor_pos()):
                # Released over the wheel: the drop arrives in a moment and closes it.
                self._grace.start(DROP_GRACE_MS)
            else:
                self._close_wheel()
                self._to_idle()
        elif self._state in (State.PRESSED, State.DRAGGING):
            self._to_idle()

    # --- keyboard
    def _key_down(self, vk: int) -> None:
        repeat = vk in self._down_vks
        self._down_vks.add(vk)
        if self._state is State.WHEEL_OPEN and vk == VK_ESCAPE:
            self._close_wheel()  # Esc also cancels the OLE drag itself
            self._to_idle()
            return
        modifier = MODIFIER_NAMES.get(vk)
        if modifier is None or repeat or vk in self._held_at_press:
            return  # not a modifier, auto-repeat, or held since before the drag (FR-1.2)
        kind = self._kind_for_modifier.get(modifier)
        if kind is None:
            return
        if self._state is State.DRAGGING:
            self._try_open(kind)
        elif self._state is State.WHEEL_OPEN and kind != self._open_kind:
            self._wheel.swap(kind)
            self._open_kind = kind
            self._timeout.start(WHEEL_TIMEOUT_MS)

    def _try_open(self, kind: "Wheel") -> None:
        if self._paused:
            return
        root_class = self._press_root[1]
        if not is_file_drag(root_class, self._probe.sys_drag_image_visible()):
            log.debug("modifier during a drag that is not a file drag (root %s)", root_class)
            return
        prefill = self._selection(*self._press_root) if root_class in EXPLORER_CLASSES else []
        self._wheel.open(kind, self._cursor_pos(), prefill)
        self._open_kind = kind
        self._state = State.WHEEL_OPEN
        self._timeout.start(WHEEL_TIMEOUT_MS)

    # --- closing (FR-1.9)
    def _close_wheel(self) -> None:
        self._timeout.stop()
        self._grace.stop()
        if self._wheel.isVisible():
            self._wheel.close()

    def _to_idle(self) -> None:
        self._timeout.stop()
        self._grace.stop()
        self._open_kind = None
        self._state = State.IDLE

    def _on_wheel_closed(self) -> None:
        """Any close: a drop anywhere, Esc, LMB up, or the user's own action."""
        self._to_idle()

    def _on_grace_over(self) -> None:
        if self._state is State.WHEEL_OPEN:
            log.debug("no drop within %d ms of LMB up; closing the wheel", DROP_GRACE_MS)
            self._close_wheel()
            self._to_idle()

    def _on_timeout(self) -> None:
        if self._state is State.WHEEL_OPEN:
            log.debug("no drag input for %d s; closing the wheel", WHEEL_TIMEOUT_MS // 1000)
            self._close_wheel()
            self._to_idle()
