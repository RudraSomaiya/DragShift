"""HookThread: low-level mouse + keyboard hooks on their own thread, bridged to Qt (PRD §9.2, §9.6).

- ``WH_MOUSE_LL`` and ``WH_KEYBOARD_LL`` are installed on one thread that runs a ``GetMessageW``
  loop. Callbacks do O(1) work: copy a few fields, emit a queued Qt signal, call
  ``CallNextHookEx``. No COM, no I/O, no logging, and input is never swallowed (NFR-3).
- Synthesised events (``SendInput`` etc.) carry the ``INJECTED`` flag and are dropped (FR-1.1).
- Windows silently removes a hook whose callback is too slow. A 5 s watchdog on the same thread
  notices that the system saw input our hooks did not, and re-installs them (NFR-3).
- Mouse moves are forwarded only while the left button is held; that is all the drag branch of
  §9.5 needs, and it avoids ~1000 signals per second the rest of the time.
"""

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from enum import IntEnum
from typing import NamedTuple

from PySide6.QtCore import QObject, QThread, Signal, Slot

__all__ = ["HookInstallError", "HookThread", "InputKind", "RawInput", "hook_looks_dead"]

log = logging.getLogger(__name__)

# --- Win32 constants -------------------------------------------------------------------------
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0  # nCode: the hook procedure must process the message
WM_USER = 0x0400
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
WM_TIMER = 0x0113
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104  # Alt (and any key pressed while Alt is held) arrives as WM_SYS*
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_MOUSEHWHEEL = 0x020E
LLMHF_INJECTED = 0x01  # MSLLHOOKSTRUCT.flags: event was synthesised (SendInput etc.)
LLKHF_INJECTED = 0x10  # KBDLLHOOKSTRUCT.flags: same, for keys

STALE_MS = 2000  # the system saw input this much later than our hooks did -> hooks look dead
MAX_BACKOFF_TICKS = 12  # ~1 min at the default 5 s watchdog
# A callback needs the GIL. While another thread runs Python, the default 5 ms switch interval
# made it wait up to ~47 ms (measured). Windows rounds waits below 1 ms down to 0, which hands
# the GIL over at once: p99 140 us instead of 16 ms.
SWITCH_INTERVAL_S = 0.0005


class InputKind(IntEnum):
    MOUSE_MOVE = 1  # only while the left button is held
    LBUTTON_DOWN = 2
    LBUTTON_UP = 3
    MOUSE_OTHER = 4  # right/middle/X button down or wheel: cancels a modifier tap (§9.5)
    KEY_DOWN = 5  # WM_KEYDOWN and WM_SYSKEYDOWN; auto-repeat sends one per repeat
    KEY_UP = 6


class RawInput(NamedTuple):
    """One input event. Mouse events fill ``x``/``y`` (physical screen px); key events fill ``vk``."""

    kind: InputKind
    x: int
    y: int
    vk: int  # virtual-key code; left/right variants are distinct (Shift 0xA0/0xA1, ...)
    t_ns: int  # time.perf_counter_ns() when the hook saw the event


_MOUSE_KINDS = {
    WM_LBUTTONDOWN: InputKind.LBUTTON_DOWN,
    WM_LBUTTONUP: InputKind.LBUTTON_UP,
    WM_RBUTTONDOWN: InputKind.MOUSE_OTHER,
    WM_MBUTTONDOWN: InputKind.MOUSE_OTHER,
    WM_XBUTTONDOWN: InputKind.MOUSE_OTHER,
    WM_MOUSEWHEEL: InputKind.MOUSE_OTHER,
    WM_MOUSEHWHEEL: InputKind.MOUSE_OTHER,
}
_KEY_KINDS = {
    WM_KEYDOWN: InputKind.KEY_DOWN,
    WM_SYSKEYDOWN: InputKind.KEY_DOWN,
    WM_KEYUP: InputKind.KEY_UP,
    WM_SYSKEYUP: InputKind.KEY_UP,
}

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# LRESULT CALLBACK LowLevelProc(int nCode, WPARAM wParam, LPARAM lParam)
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


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


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int  # -1 on error, so not BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetTickCount.restype = wintypes.DWORD


class HookInstallError(OSError):
    """``SetWindowsHookExW`` failed."""


def hook_looks_dead(last_input_tick: int, last_seen_tick: int, stale_ms: int = STALE_MS) -> bool:
    """True if the system saw input at least ``stale_ms`` later than our hooks did.

    Both arguments are ``GetTickCount`` values (32-bit ms), so the difference is wrap-safe.
    """
    behind = (last_input_tick - last_seen_tick) & 0xFFFFFFFF
    return stale_ms < behind < 0x80000000


def _last_input_tick() -> int:
    """Tick of the last keyboard/mouse input the system saw (``GetLastInputInfo``)."""
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(info))
    return info.dwTime


class HookThread(QThread):
    """Owns the hooks. Connect ``raw_input`` with a slot on a Qt-thread QObject (queued)."""

    raw_input = Signal(object)  # RawInput
    reinstalled = Signal()  # hooks were re-installed; events were missed, reset any state
    _warmup = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        watchdog_ms: int = 5000,
        stale_ms: int = STALE_MS,
    ) -> None:
        super().__init__(parent)
        self._watchdog_ms = watchdog_ms
        self._stale_ms = stale_ms
        self._tid = 0
        self._hooks: list[int | None] = []
        self._ready = threading.Event()
        self._error: HookInstallError | None = None
        self._lmb_down = False  # only touched on the hook thread
        self._last_seen_tick = 0
        self._fruitless = 0
        self._skip_ticks = 0
        self.injected_ignored = 0
        self.errors = 0  # exceptions swallowed inside callbacks (the hook must keep going)
        self.reinstall_count = 0
        self.max_callback_ns = 0
        self._warmup.connect(self._on_warmup)
        # Keep references: a garbage-collected CFUNCTYPE callback crashes the process (§9.6).
        self._mouse_proc = HOOKPROC(self._on_mouse)
        self._key_proc = HOOKPROC(self._on_key)

    # --- lifecycle (Qt thread)
    def start_hooks(self, timeout: float = 5.0) -> None:
        """Start the thread and wait until the hooks are installed. Raises HookInstallError.

        Also lowers the process-wide GIL switch interval (see ``SWITCH_INTERVAL_S``).
        """
        if sys.getswitchinterval() > SWITCH_INTERVAL_S:
            sys.setswitchinterval(SWITCH_INTERVAL_S)
        self._ready.clear()
        self._error = None
        self.start()
        if not self._ready.wait(timeout):
            raise HookInstallError(0, "timed out installing the input hooks")
        if self._error:
            self.wait()
            raise self._error

    def stop_hooks(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self.wait(2000)

    def is_installed(self) -> bool:
        return bool(self._hooks)

    @Slot(object)
    def _on_warmup(self, _event: object) -> None:
        """Receives the warm-up emit from ``run``; does nothing."""

    # --- hook callbacks (hook thread). O(1): no COM, no I/O, no logging.
    def _on_mouse(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == HC_ACTION:
            t0 = time.perf_counter_ns()
            try:
                self._dispatch_mouse(w_param, l_param, t0)
            except Exception:  # noqa: BLE001  # an error must never stop CallNextHookEx
                self.errors += 1
            self.max_callback_ns = max(self.max_callback_ns, time.perf_counter_ns() - t0)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)  # never swallow input

    def _on_key(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == HC_ACTION:
            t0 = time.perf_counter_ns()
            try:
                self._dispatch_key(w_param, l_param, t0)
            except Exception:  # noqa: BLE001
                self.errors += 1
            self.max_callback_ns = max(self.max_callback_ns, time.perf_counter_ns() - t0)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _dispatch_mouse(self, w_param: int, l_param: int, t0: int) -> None:
        info = MSLLHOOKSTRUCT.from_address(l_param)
        # For the watchdog; counts injected events too. Taken now, in the same clock as
        # GetLastInputInfo, rather than the event's own time stamp.
        self._last_seen_tick = kernel32.GetTickCount()
        if info.flags & LLMHF_INJECTED:
            self.injected_ignored += 1
            return
        if w_param == WM_MOUSEMOVE:
            if self._lmb_down:
                self.raw_input.emit(RawInput(InputKind.MOUSE_MOVE, info.pt.x, info.pt.y, 0, t0))
        elif kind := _MOUSE_KINDS.get(w_param):
            if kind is InputKind.LBUTTON_DOWN:
                self._lmb_down = True
            elif kind is InputKind.LBUTTON_UP:
                self._lmb_down = False
            self.raw_input.emit(RawInput(kind, info.pt.x, info.pt.y, 0, t0))

    def _dispatch_key(self, w_param: int, l_param: int, t0: int) -> None:
        info = KBDLLHOOKSTRUCT.from_address(l_param)
        self._last_seen_tick = kernel32.GetTickCount()
        if info.flags & LLKHF_INJECTED:
            self.injected_ignored += 1
            return
        if kind := _KEY_KINDS.get(w_param):
            self.raw_input.emit(RawInput(kind, 0, 0, info.vkCode, t0))

    # --- hook thread: install, message loop, watchdog
    def run(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        # PostThreadMessage (WM_QUIT in stop_hooks) fails until the thread has a message queue,
        # and PeekMessage is what creates it. Do it before start_hooks() can return.
        user32.PeekMessageW(ctypes.byref(wintypes.MSG()), None, WM_USER, WM_USER, PM_NOREMOVE)
        # The first cross-thread emit costs ~4 ms (measured); pay it here, not on the first
        # real event, which would blow the 1 ms budget.
        self._warmup.emit(RawInput(InputKind.KEY_UP, 0, 0, 0, 0))
        try:
            self._install()
        except HookInstallError as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        # A thread timer (no window) posts WM_TIMER to this queue; LL hook callbacks are also
        # delivered to this thread while it waits in GetMessageW.
        timer = user32.SetTimer(None, 0, self._watchdog_ms, None)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_TIMER:
                self._watchdog_tick()
        user32.KillTimer(None, timer)
        self._uninstall()

    def _install(self) -> None:
        hmod = kernel32.GetModuleHandleW(None)
        for hook_id, proc in ((WH_MOUSE_LL, self._mouse_proc), (WH_KEYBOARD_LL, self._key_proc)):
            handle = user32.SetWindowsHookExW(hook_id, proc, hmod, 0)
            if not handle:
                err = ctypes.get_last_error()
                self._uninstall()
                raise HookInstallError(err, ctypes.FormatError(err))
            self._hooks.append(handle)
        self._lmb_down = False  # a button-up may have been missed
        self._last_seen_tick = kernel32.GetTickCount()

    def _uninstall(self) -> None:
        for handle in self._hooks:
            user32.UnhookWindowsHookEx(handle)  # FALSE if Windows already removed it; harmless
        self._hooks = []

    def _watchdog_tick(self) -> None:
        if self._skip_ticks:
            self._skip_ticks -= 1
            return
        if not hook_looks_dead(_last_input_tick(), self._last_seen_tick, self._stale_ms):
            self._fruitless = 0
            return
        log.warning("input hooks look dead (system saw input they did not); re-installing")
        self._uninstall()
        try:
            self._install()
        except HookInstallError:
            log.exception("could not re-install the input hooks; will retry")
        else:
            self.reinstall_count += 1
            self.reinstalled.emit()
        # Input in an elevated window never reaches a non-elevated hook (UIPI). When a
        # re-install does not help, back off instead of re-installing every tick.
        self._fruitless += 1
        self._skip_ticks = min(2**self._fruitless - 1, MAX_BACKOFF_TICKS)
