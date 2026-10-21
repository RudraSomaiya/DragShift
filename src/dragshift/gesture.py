"""GestureController: the gesture state machine (PRD §9.5).

Drag branch:
    IDLE --LMB down--> PRESSED --moved past SM_CXDRAG/SM_CYDRAG--> DRAGGING
    PRESSED/DRAGGING --LMB up--> IDLE
    DRAGGING --mapped modifier key-down, and is_file_drag()--> WHEEL_OPEN(wheel)
    WHEEL_OPEN --another mapped modifier down--> WHEEL_OPEN(other wheel)         (swap, FR-1.6)
    WHEEL_OPEN --LMB up outside / Esc / 30 s / any close--> IDLE                 (FR-1.9)

Tap branch (Quick Launch, FR-3.1):
    IDLE --launch modifier down, no other key held--> TAP_PENDING(key, t0)
    TAP_PENDING --any other key, mouse button or wheel, or auto-repeat--> IDLE
    TAP_PENDING --the same key up within TAP_MS--> IDLE, and that is a tap:
        single mode: if the gates pass (FR-3.2), emit quick_launch(kind, cursor)
        double mode: the same, on a second tap starting within DOUBLE_TAP_MS of the first

It consumes ``RawInput`` events from the HookThread on the Qt thread and drives a WheelWindow.
It never inspects the desktop inside a hook callback. Tap times come from the events' own hook
time stamps, so a busy Qt thread cannot turn a quick tap into a slow one. The launcher wheel is
DS-19; until then app.py only logs ``quick_launch``.

Wiring (done in app.py): ``hooks.raw_input -> on_input``, ``hooks.reinstalled ->
on_hooks_reinstalled``, ``store.changed -> apply_config``.
"""

import logging
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject, QPoint, QTimer, Signal, Slot
from PySide6.QtGui import QCursor

from dragshift.config import Config
from dragshift.platform.context_guard import ContextProbe, Win32Context, failed_gate
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
# Esc also cancels Explorer's OLE drag, and Windows then tells the wheel the drag has left. If the
# wheel is hidden before that, the shell's drag image (a dimmed icon with a prohibited sign) is
# orphaned and stays on screen. Measured with a real Explorer drag: 7 of 7 ghosts when closing at
# once, none in 8 of 8 when closing 100 ms or more after Esc.
ESC_CLOSE_DELAY_MS = 150
TAP_MS = 300  # FR-3.1: longest key-down to key-up of a tap
DOUBLE_TAP_MS = 350  # FR-3.1 double-tap mode: first tap's key-up to the second's key-down
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
    TAP_PENDING = auto()


class Probe(Protocol):
    """What the controller asks the desktop. ``Win32Probe`` is the real one; tests fake it."""

    def drag_threshold(self) -> tuple[int, int]: ...
    def root_at(self, x: int, y: int) -> tuple[int, str]: ...
    def sys_drag_image_visible(self) -> bool: ...
    def held_modifier_vks(self) -> frozenset[int]: ...


class GestureController(QObject):
    quick_launch = Signal(str, QPoint)  # "apps" | "sites", cursor position (FR-3.1, FR-3.5)

    def __init__(
        self,
        wheel: "WheelWindow",
        config: Config | None = None,
        *,
        probe: Probe | None = None,
        selection: Callable[[int, str], list[Path]] = selected_paths,
        cursor_pos: Callable[[], QPoint] = QCursor.pos,
        context: ContextProbe | None = None,
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
        self._context: ContextProbe = context or Win32Context()
        self._config = Config()
        self._launch_for_modifier: dict[str, str] = {}  # modifier name -> "apps" | "sites"
        self._tap = (0, 0)  # (vk, key-down t_ns) while TAP_PENDING
        self._first_tap: tuple[int, int] | None = None  # (vk, key-up t_ns), double-tap mode

        self._timeout = QTimer(self)  # FR-1.9 safety timeout
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self._grace = QTimer(self)  # LMB released over the wheel: wait for the drop
        self._grace.setSingleShot(True)
        self._grace.timeout.connect(self._on_grace_over)
        self._esc_close = QTimer(self)  # Esc pressed: close once the cancel has reached the wheel
        self._esc_close.setSingleShot(True)
        self._esc_close.timeout.connect(self._close_wheel)
        wheel.closed.connect(self._on_wheel_closed)
        self.apply_config(config or Config())

    @property
    def state(self) -> State:
        return self._state

    @Slot(object)
    def apply_config(self, config: Config) -> None:
        """Modifier-to-wheel mapping (FR-1.3), Quick Launch keys (FR-3.1), Pause (FR-1.12)."""
        mapping: dict[str, Wheel] = {}
        for kind, modifier in (
            ("convert", config.keys.convert),
            ("tools", config.keys.tools),
            ("actions", config.keys.actions),
        ):
            mapping.setdefault(modifier, kind)  # if two wheels share a key, the first wins
        self._kind_for_modifier = mapping
        launch: dict[str, str] = {}
        launch.setdefault(config.keys.launch_apps, "apps")  # the same key for both: Apps wins
        launch.setdefault(config.keys.launch_sites, "sites")
        self._launch_for_modifier = launch
        self._paused = config.general.paused
        self._config = config

    @Slot()
    def on_hooks_reinstalled(self) -> None:
        """The hooks were re-installed, so events were missed (a button-up, say): start over."""
        self._down_vks.clear()
        self._first_tap = None
        self._close_wheel()
        self._to_idle()

    @Slot(object)
    def on_input(self, ev: RawInput) -> None:
        kind = ev.kind
        if kind is InputKind.KEY_DOWN:
            self._key_down(ev.vk, ev.t_ns)
        elif kind is InputKind.KEY_UP:
            self._down_vks.discard(ev.vk)
            self._held_at_press.discard(ev.vk)  # released: pressing it again is a new press
            if self._state is State.TAP_PENDING and ev.vk == self._tap[0]:
                self._tap_up(ev.vk, ev.t_ns)
        elif kind is InputKind.LBUTTON_DOWN:
            self._cancel_tap()
            self._lmb_down(ev.x, ev.y)
        elif kind is InputKind.MOUSE_MOVE:
            self._move(ev.x, ev.y)
        elif kind is InputKind.LBUTTON_UP:
            self._lmb_up()
        elif kind is InputKind.MOUSE_OTHER:
            self._cancel_tap()  # another mouse button or the wheel: not a tap (FR-3.1)

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
    def _key_down(self, vk: int, t_ns: int) -> None:
        repeat = vk in self._down_vks
        others_held = bool(self._down_vks - {vk})
        self._down_vks.add(vk)
        if self._state in (State.IDLE, State.TAP_PENDING):
            self._tap_down(vk, t_ns, chord=repeat or others_held)
            return
        if self._state is State.WHEEL_OPEN and vk == VK_ESCAPE:
            self._to_idle()
            if ESC_CLOSE_DELAY_MS > 0:
                self._esc_close.start(ESC_CLOSE_DELAY_MS)  # not now: see ESC_CLOSE_DELAY_MS
            else:
                self._close_wheel()  # 0 = the old, racy behaviour (kept for the harness control)
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
        self._esc_close.stop()  # a close left over from an Esc must not hit this new wheel
        self._wheel.open(kind, self._cursor_pos(), prefill)
        self._open_kind = kind
        self._state = State.WHEEL_OPEN
        self._timeout.start(WHEEL_TIMEOUT_MS)

    # --- taps (FR-3.1)
    def _tap_down(self, vk: int, t_ns: int, *, chord: bool) -> None:
        """A key went down while idle. Only a lone launch modifier starts a tap."""
        if self._state is State.TAP_PENDING or chord:
            self._cancel_tap()  # a second key, or auto-repeat: a chord or a hold, not a tap
            return
        if MODIFIER_NAMES.get(vk) not in self._launch_for_modifier:
            self._first_tap = None  # typing between two taps breaks a double tap
            return
        first = self._first_tap
        if first and (first[0] != vk or t_ns - first[1] > DOUBLE_TAP_MS * 1_000_000):
            self._first_tap = None  # too late, or another key: this starts over
        self._tap = (vk, t_ns)
        self._state = State.TAP_PENDING

    def _tap_up(self, vk: int, t_ns: int) -> None:
        self._state = State.IDLE
        if t_ns - self._tap[1] > TAP_MS * 1_000_000:
            self._first_tap = None  # held too long: a hold, not a tap
            return
        if self._config.keys.tap_mode == "double" and self._first_tap is None:
            self._first_tap = (vk, t_ns)  # wait for the second tap
            return
        self._first_tap = None
        kind = self._launch_for_modifier[MODIFIER_NAMES[vk]]
        gate = failed_gate(self._context, self._config)
        if gate is not None:
            log.debug("%s tap ignored: gate %s failed", kind, gate)
            return
        self.quick_launch.emit(kind, self._cursor_pos())

    def _cancel_tap(self) -> None:
        self._first_tap = None
        if self._state is State.TAP_PENDING:
            self._state = State.IDLE

    # --- closing (FR-1.9)
    def _close_wheel(self) -> None:
        self._timeout.stop()
        self._grace.stop()
        self._esc_close.stop()
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
