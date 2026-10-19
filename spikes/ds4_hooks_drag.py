"""DS-4 spike (throwaway): global hooks + file-drag detection + COPY-only drop window.

Run:  uv run python spikes/ds4_hooks_drag.py        (Ctrl+C in the console to quit)

What it does (PRD FR-1.1, FR-1.2, FR-1.8):
  * LL mouse + keyboard hooks on their own thread; callbacks only emit a queued Qt signal.
  * Logs LMB-down, "drag candidate" (movement beyond SM_CXDRAG/SM_CYDRAG), and modifier key-downs.
  * Polls for the `SysDragImage` window to measure how soon it appears (gate (a)).
  * On a modifier key-down during a drag: evaluates gate (a) and gate (b), logs both, and if
    either holds opens a test drop window centred on the cursor.
  * The drop window accepts ONLY with CopyAction on its green zone and ignores everywhere else.
    After a drop it re-checks that the source file still exists (FR-1.8).

Everything is logged to the console and to ds4-spike.log (git-ignored).
"""

import argparse
import ctypes
import logging
import signal
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger("ds4")

# --- Win32 constants -------------------------------------------------------------------------
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_QUIT = 0x0012
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104  # Alt (and any key pressed while Alt is held) arrives as WM_SYS*
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
LLMHF_INJECTED = 0x01  # MSLLHOOKSTRUCT.flags: event was synthesised (SendInput etc.)
LLKHF_INJECTED = 0x10  # KBDLLHOOKSTRUCT.flags: same, for keys
SM_CXDRAG = 68  # drag threshold in pixels
SM_CYDRAG = 69
GA_ROOT = 2
VK_ESCAPE = 0x1B

# Shift 0x10 / L 0xA0 / R 0xA1, Ctrl 0x11 / 0xA2 / 0xA3, Alt 0x12 / 0xA4 / 0xA5
MODIFIER_VKS = {
    0x10: "Shift",
    0xA0: "Shift",
    0xA1: "Shift",
    0x11: "Ctrl",
    0xA2: "Ctrl",
    0xA3: "Ctrl",
    0x12: "Alt",
    0xA4: "Alt",
    0xA5: "Alt",
}
EXPLORER_CLASSES = {"CabinetWClass", "Progman", "WorkerW"}  # FR-1.2 gate (b)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# LRESULT CALLBACK LowLevelProc(int nCode, WPARAM wParam, LPARAM lParam)
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int  # -1 on error, so not BOOL
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


def ms_since(t0_ns: int, t1_ns: int | None = None) -> float:
    return ((t1_ns or time.perf_counter_ns()) - t0_ns) / 1e6


def root_class_at(x: int, y: int) -> str:
    """Class name of the top-level window under the physical point (FR-1.2 gate (b))."""
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    root = user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
    if not root:
        return "<none>"
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(root, buf, 256)
    return buf.value


def sys_drag_image_visible() -> bool:
    """FR-1.2 gate (a): a visible top-level window of class SysDragImage exists."""
    hwnd = user32.FindWindowW("SysDragImage", None)
    return bool(hwnd) and bool(user32.IsWindowVisible(hwnd))


# --- Hook thread -----------------------------------------------------------------------------
class HookBridge(QObject):
    # ("mouse", msg, x, y, injected, t_ns) or ("key", msg, vk, 0, injected, t_ns)
    event = Signal(object)


class HookThread(threading.Thread):
    """LL hooks + their own GetMessage loop. Callbacks do O(1) work: copy fields, emit, return."""

    def __init__(self, bridge: HookBridge) -> None:
        super().__init__(name="ds4-hooks", daemon=True)
        self._bridge = bridge
        self._tid = 0
        self._lmb_down = False  # only touched on the hook thread
        self.ready = threading.Event()
        self.error: OSError | None = None
        self.max_callback_ns = 0
        # Module-level/instance references: a garbage-collected CFUNCTYPE callback crashes the hook.
        self._mouse_proc = HOOKPROC(self._on_mouse)
        self._key_proc = HOOKPROC(self._on_key)

    def _on_mouse(self, n_code: int, w_param: int, l_param: int) -> int:
        t0 = time.perf_counter_ns()
        try:
            if n_code == HC_ACTION and w_param in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE):
                info = MSLLHOOKSTRUCT.from_address(l_param)
                if w_param == WM_LBUTTONDOWN:
                    self._lmb_down = True
                elif w_param == WM_LBUTTONUP:
                    self._lmb_down = False
                # Moves are only interesting while LMB is held; skip the other ~1000/s.
                if w_param != WM_MOUSEMOVE or self._lmb_down:
                    injected = bool(info.flags & LLMHF_INJECTED)
                    self._bridge.event.emit(("mouse", w_param, info.pt.x, info.pt.y, injected, t0))
        finally:
            self.max_callback_ns = max(self.max_callback_ns, time.perf_counter_ns() - t0)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)  # never swallow input

    def _on_key(self, n_code: int, w_param: int, l_param: int) -> int:
        t0 = time.perf_counter_ns()
        try:
            if n_code == HC_ACTION and w_param in (
                WM_KEYDOWN,
                WM_KEYUP,
                WM_SYSKEYDOWN,
                WM_SYSKEYUP,
            ):
                info = KBDLLHOOKSTRUCT.from_address(l_param)
                injected = bool(info.flags & LLKHF_INJECTED)
                self._bridge.event.emit(("key", w_param, info.vkCode, 0, injected, t0))
        finally:
            self.max_callback_ns = max(self.max_callback_ns, time.perf_counter_ns() - t0)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def run(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        hooks = [
            user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, hmod, 0),
            user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._key_proc, hmod, 0),
        ]
        if not all(hooks):
            self.error = ctypes.WinError(ctypes.get_last_error())
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        # LL hook callbacks are delivered to this thread while it waits in GetMessageW.
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        for h in hooks:
            user32.UnhookWindowsHookEx(h)

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self.join(timeout=2)


# --- Test drop window ------------------------------------------------------------------------
class DropWindow(QWidget):
    """Frameless, no-focus window. Green zone = enabled petal (Copy). Red zone = hub/gap."""

    closed = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.resize(420, 220)
        self.wheel = ""
        self.shown_t = 0
        self.first_enter_t: int | None = None
        self.moves_since_show = 0
        self._zone = ""
        self._status = "waiting for drag events..."

    def copy_zone(self) -> QRect:
        return QRect(10, 10, 270, 200)

    def open_at_cursor(self, wheel: str) -> None:
        self.wheel = wheel
        self.first_enter_t = None
        self.moves_since_show = 0
        self._status = "waiting for drag events..."
        pos = QCursor.pos()  # logical (DPI-independent) pixels, unlike the hook's physical ones
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(pos.x() - self.width() // 2, area.left()), area.right() - self.width())
        y = min(max(pos.y() - self.height() // 2, area.top()), area.bottom() - self.height())
        self.move(x, y)
        self.shown_t = time.perf_counter_ns()
        self.show()
        self.update()

    def close_window(self) -> None:
        if self.isVisible():
            self.hide()
        self.closed.emit()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(30, 30, 36))
        p.fillRect(self.copy_zone(), QColor(34, 120, 70))
        p.fillRect(QRect(290, 10, 120, 200), QColor(130, 40, 40))
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Segoe UI", 11))
        p.drawText(
            self.copy_zone(),
            Qt.AlignmentFlag.AlignCenter,
            f"{self.wheel} wheel\nGREEN = enabled petal\n(CopyAction)\n\n{self._status}",
        )
        p.drawText(
            QRect(290, 10, 120, 200), Qt.AlignmentFlag.AlignCenter, "RED = hub/gap\n(ignore)"
        )

    # --- drag events: FR-1.8. Never acceptProposedAction(): Explorer would treat Shift-drops
    # as a Move and delete the source.
    def _describe(self, event) -> str:
        fmts = ",".join(event.mimeData().formats())
        return (
            f"proposed={event.proposedAction()} possible={event.possibleActions()} "
            f"mods={event.modifiers()} formats=[{fmts}]"
        )

    def _accept_over_copy_zone(self, event) -> bool:
        ok = event.mimeData().hasUrls() and self.copy_zone().contains(event.position().toPoint())
        if ok:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()
        return ok

    def dragEnterEvent(self, event) -> None:
        if self.first_enter_t is None:
            self.first_enter_t = time.perf_counter_ns()
            log.info(
                "DragEnter first fired %.0f ms after the window was shown, after %d mouse moves "
                "since show. %s",
                ms_since(self.shown_t, self.first_enter_t),
                self.moves_since_show,
                self._describe(event),
            )
        self._accept_over_copy_zone(event)

    def dragMoveEvent(self, event) -> None:
        ok = self._accept_over_copy_zone(event)
        zone = "GREEN(copy)" if ok else "RED/outside(ignore)"
        if zone != self._zone:
            self._zone = zone
            log.info("dragMove zone -> %s", zone)

    def dragLeaveEvent(self, _event) -> None:
        log.info("DragLeave")
        self._zone = ""

    def dropEvent(self, event) -> None:
        if not self._accept_over_copy_zone(event):
            log.info("DROP on RED zone / non-file data -> ignored (event.ignore())")
            self._status = "drop ignored"
            self.close_window()
            return
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        log.info(
            "DROP accepted: dropAction=%s paths=%s", event.dropAction(), [str(p) for p in paths]
        )
        self._status = "dropped; checking source..."
        self.update()
        QTimer.singleShot(1500, lambda: self._check_sources(paths))
        self.hide()
        # The "closed" signal fires after the check below so the log stays readable.

    def _check_sources(self, paths: list[Path]) -> None:
        for p in paths:
            ok = p.exists()
            log.info(
                "FR-1.8 CHECK %s: source %s %s",
                "PASS" if ok else "*** FAIL ***",
                p,
                "is still in place" if ok else "IS GONE (Explorer treated it as a Move!)",
            )
        self.closed.emit()


# --- Gesture logic on the Qt thread ----------------------------------------------------------
class Spike(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.state = "IDLE"  # IDLE | PRESSED | DRAGGING | OPEN
        self.held_vks: set[int] = set()
        self.held_at_press: set[str] = set()
        self.press_pos = (0, 0)
        self.press_root = ""
        self.press_t = 0
        self.drag_t: int | None = None
        self.sys_seen_t: int | None = None
        self.dx = user32.GetSystemMetrics(SM_CXDRAG)
        self.dy = user32.GetSystemMetrics(SM_CYDRAG)
        self.injected_ignored = 0
        self.window = DropWindow()
        self.window.closed.connect(self._on_window_closed)
        self.poll = QTimer(self)  # samples SysDragImage while LMB is held
        self.poll.setTimerType(Qt.TimerType.PreciseTimer)
        self.poll.setInterval(10)
        self.poll.timeout.connect(self._poll_sys_drag_image)
        self.timeout = QTimer(self)  # FR-1.9 safety timeout
        self.timeout.setSingleShot(True)
        self.timeout.setInterval(30_000)
        self.timeout.timeout.connect(self._on_safety_timeout)

    def held_names(self) -> set[str]:
        return {MODIFIER_VKS[vk] for vk in self.held_vks}

    # --- dispatch
    def on_event(self, ev: tuple) -> None:
        kind, msg, a, b, injected, t = ev
        if injected:
            self.injected_ignored += 1  # FR-1.1: ignore synthesised events
            return
        if kind == "mouse":
            self._on_mouse(msg, a, b, t)
        else:
            self._on_key(msg, a, t)

    # --- mouse
    def _on_mouse(self, msg: int, x: int, y: int, t: int) -> None:
        if self.state == "OPEN":
            if msg == WM_MOUSEMOVE:
                self.window.moves_since_show += 1
            elif msg == WM_LBUTTONUP:
                inside = self.window.frameGeometry().contains(QCursor.pos())
                if inside:
                    # The drop (if any) is delivered by Explorer's OLE loop around now.
                    log.info("LMB up inside the window; waiting 400 ms for a drop")
                    QTimer.singleShot(400, self._close_if_still_open)
                else:
                    log.info("LMB up outside the window -> close (FR-1.9)")
                    self.window.close_window()
            return
        if msg == WM_LBUTTONDOWN:
            self.state = "PRESSED"
            self.press_pos = (x, y)
            self.press_t = t
            self.drag_t = None
            self.sys_seen_t = None
            self.held_at_press = self.held_names()
            self.press_root = root_class_at(x, y)
            log.info(
                "LMB down at (%d,%d) root_class=%s in_gate_b=%s held_before=%s "
                "(class lookup %.1f ms after the hook event)",
                x,
                y,
                self.press_root,
                self.press_root in EXPLORER_CLASSES,
                sorted(self.held_at_press) or "-",
                ms_since(t),
            )
            self.poll.start()
        elif msg == WM_MOUSEMOVE and self.state == "PRESSED":
            if abs(x - self.press_pos[0]) > self.dx or abs(y - self.press_pos[1]) > self.dy:
                self.state = "DRAGGING"
                self.drag_t = t
                log.info(
                    "DRAG CANDIDATE %.0f ms after LMB down (threshold %dx%d px)",
                    ms_since(self.press_t, t),
                    self.dx,
                    self.dy,
                )
        elif msg == WM_LBUTTONUP and self.state in ("PRESSED", "DRAGGING"):
            if self.state == "DRAGGING":
                seen = "never seen" if self.sys_seen_t is None else "was seen"
                log.info(
                    "LMB up after a drag; SysDragImage %s during this press (root=%s)",
                    seen,
                    self.press_root,
                )
            self.poll.stop()
            self.state = "IDLE"

    def _poll_sys_drag_image(self) -> None:
        visible = sys_drag_image_visible()
        if visible and self.sys_seen_t is None:
            self.sys_seen_t = time.perf_counter_ns()
            after_drag = (
                f"{ms_since(self.drag_t, self.sys_seen_t):.0f} ms after drag candidate"
                if self.drag_t
                else "BEFORE the drag threshold"
            )
            log.info(
                "SysDragImage VISIBLE: %.0f ms after LMB down, %s",
                ms_since(self.press_t, self.sys_seen_t),
                after_drag,
            )
        elif not visible and self.sys_seen_t is not None:
            log.info("SysDragImage gone")
            self.sys_seen_t = None
            self.poll.stop()

    # --- keyboard
    def _on_key(self, msg: int, vk: int, t: int) -> None:
        if msg in (WM_KEYUP, WM_SYSKEYUP):
            self.held_vks.discard(vk)
            return
        if vk == VK_ESCAPE and self.state == "OPEN":
            log.info("Esc -> close (FR-1.9)")
            self.window.close_window()
            return
        name = MODIFIER_VKS.get(vk)
        if name is None:
            return
        if vk in self.held_vks:
            return  # auto-repeat
        self.held_vks.add(vk)
        if self.state == "OPEN":
            if name != self.window.wheel:
                log.info(
                    "modifier %s down while open -> swap wheel %s -> %s (FR-1.6)",
                    name,
                    self.window.wheel,
                    name,
                )
                self.window.wheel = name
                self.window.update()
            return
        if self.state != "DRAGGING":
            log.info("modifier %s down while %s -> ignored (not dragging)", name, self.state)
            return
        if name in self.held_at_press:
            log.info(
                "modifier %s down but it was already held before the drag -> ignored (FR-1.2)", name
            )
            return
        gate_a = sys_drag_image_visible()
        gate_b = self.press_root in EXPLORER_CLASSES
        log.info(
            "MODIFIER %s down during drag (checked %.1f ms after the hook event): "
            "gate(a) SysDragImage visible=%s | gate(b) root=%s in set=%s -> %s",
            name,
            ms_since(t),
            gate_a,
            self.press_root,
            gate_b,
            "FILE DRAG CONFIRMED" if gate_a or gate_b else "not a file drag, do nothing",
        )
        if gate_a or gate_b:
            self.state = "OPEN"
            self.window.open_at_cursor(name)
            self.timeout.start()

    # --- closing
    def _close_if_still_open(self) -> None:
        if self.window.isVisible():
            log.info("no drop arrived within 400 ms -> the drop was rejected; closing")
            self.window.close_window()

    def _on_safety_timeout(self) -> None:
        if self.state == "OPEN":
            log.info("30 s safety timeout -> close (FR-1.9)")
            self.window.close_window()

    def _on_window_closed(self) -> None:
        self.timeout.stop()
        self.state = "IDLE"
        log.info("window closed -> IDLE")


def main() -> int:
    parser = argparse.ArgumentParser(description="DS-4 spike: hooks + drag detection + copy drop")
    parser.add_argument("--seconds", type=float, default=0, help="auto-quit after N seconds")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("ds4-spike.log", mode="w", encoding="utf-8"),
        ],
    )
    app = QApplication(sys.argv)

    sample_dir = Path(tempfile.gettempdir()) / "ds4-spike"
    sample_dir.mkdir(exist_ok=True)
    for name in ("sample-1.txt", "sample-2.txt"):
        (sample_dir / name).write_text("DS-4 spike sample file\n", encoding="utf-8")

    spike = Spike()
    bridge = HookBridge()
    bridge.event.connect(spike.on_event)  # emitted from the hook thread -> queued to this thread
    hooks = HookThread(bridge)
    hooks.start()
    hooks.ready.wait(5)
    if hooks.error:
        log.error("could not install hooks: %s", hooks.error)
        return 1

    # Let Python see Ctrl+C while Qt's loop runs.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(200)
    if args.seconds:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)

    log.info("Hooks installed. SM_CXDRAG/SM_CYDRAG = %dx%d px", spike.dx, spike.dy)
    log.info("Test files: %s   (open this folder in Explorer)", sample_dir)
    log.info("Drag a file, then TAP Shift/Ctrl/Alt mid-drag. Ctrl+C here to quit.")
    code = app.exec()

    hooks.stop()
    log.info(
        "Max hook callback time: %.0f us (NFR-3 budget: < 1000 us)", hooks.max_callback_ns / 1e3
    )
    log.info("Injected events ignored: %d", spike.injected_ignored)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
