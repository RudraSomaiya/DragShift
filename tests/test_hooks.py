"""HookThread (DS-7): event mapping, injected filtering, Qt bridge, watchdog, NFR-3 latency.

The "live" tests install the real low-level hooks and inject F24 with SendInput. F24 is a key
nobody binds, and it is flagged INJECTED, so DragShift drops it. They skip themselves when the
session has no interactive desktop (hooks cannot be installed, or input is not registered).
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Slot
from pytestqt.qtbot import QtBot

from dragshift.platform import hooks
from dragshift.platform.hooks import HookThread, InputKind, RawInput, hook_looks_dead

VK_A = 0x41
VK_F24 = 0x87
HC_ACTION = hooks.HC_ACTION


def key_struct(vk: int, flags: int = 0) -> hooks.KBDLLHOOKSTRUCT:
    return hooks.KBDLLHOOKSTRUCT(vk, 0, flags, hooks.kernel32.GetTickCount(), 0)


def mouse_struct(x: int, y: int, flags: int = 0) -> hooks.MSLLHOOKSTRUCT:
    return hooks.MSLLHOOKSTRUCT(wintypes.POINT(x, y), 0, flags, hooks.kernel32.GetTickCount(), 0)


def feed_mouse(ht: HookThread, msg: int, x: int = 10, y: int = 20, flags: int = 0) -> None:
    s = mouse_struct(x, y, flags)
    ht._dispatch_mouse(msg, ctypes.addressof(s), time.perf_counter_ns())


def feed_key(ht: HookThread, msg: int, vk: int = VK_A, flags: int = 0) -> None:
    s = key_struct(vk, flags)
    ht._dispatch_key(msg, ctypes.addressof(s), time.perf_counter_ns())


class Receiver(QObject):
    """A Qt-thread QObject, like the GestureController will be."""

    def __init__(self) -> None:
        super().__init__()
        self.got: list[tuple[RawInput, int]] = []

    @Slot(object)
    def on_input(self, ev: RawInput) -> None:
        self.got.append((ev, threading.get_ident()))


class Counter(QObject):
    hits = 0

    @Slot()
    def hit(self) -> None:
        self.hits += 1


@pytest.fixture
def ht(qapp: object) -> HookThread:
    """A HookThread that is not started: the dispatch methods are plain calls."""
    return HookThread()


@pytest.fixture
def events(ht: HookThread) -> list[RawInput]:
    out: list[RawInput] = []
    ht.raw_input.connect(out.append)  # emitted on this thread, so delivered at once
    return out


# --- event mapping -----------------------------------------------------------------------------
def test_mouse_move_is_forwarded_only_while_lmb_is_held(
    ht: HookThread, events: list[RawInput]
) -> None:
    feed_mouse(ht, hooks.WM_MOUSEMOVE, 1, 1)
    assert events == []
    feed_mouse(ht, hooks.WM_LBUTTONDOWN, 5, 6)
    feed_mouse(ht, hooks.WM_MOUSEMOVE, 7, 8)
    feed_mouse(ht, hooks.WM_LBUTTONUP, 9, 10)
    feed_mouse(ht, hooks.WM_MOUSEMOVE, 11, 12)
    assert [(e.kind, e.x, e.y) for e in events] == [
        (InputKind.LBUTTON_DOWN, 5, 6),
        (InputKind.MOUSE_MOVE, 7, 8),
        (InputKind.LBUTTON_UP, 9, 10),
    ]


@pytest.mark.parametrize(
    "msg",
    [
        hooks.WM_RBUTTONDOWN,
        hooks.WM_MBUTTONDOWN,
        hooks.WM_XBUTTONDOWN,
        hooks.WM_MOUSEWHEEL,
        hooks.WM_MOUSEHWHEEL,
    ],
)
def test_other_buttons_and_wheel_map_to_mouse_other(
    ht: HookThread, events: list[RawInput], msg: int
) -> None:
    feed_mouse(ht, msg)
    assert [e.kind for e in events] == [InputKind.MOUSE_OTHER]


def test_unknown_mouse_message_is_ignored(ht: HookThread, events: list[RawInput]) -> None:
    feed_mouse(ht, 0x0203)  # WM_LBUTTONDBLCLK
    assert events == []


@pytest.mark.parametrize(
    ("msg", "kind"),
    [
        (hooks.WM_KEYDOWN, InputKind.KEY_DOWN),
        (hooks.WM_SYSKEYDOWN, InputKind.KEY_DOWN),  # Alt arrives as WM_SYSKEYDOWN
        (hooks.WM_KEYUP, InputKind.KEY_UP),
        (hooks.WM_SYSKEYUP, InputKind.KEY_UP),
    ],
)
def test_key_messages_carry_the_virtual_key(
    ht: HookThread, events: list[RawInput], msg: int, kind: InputKind
) -> None:
    feed_key(ht, msg, vk=0xA4)  # VK_LMENU
    assert [(e.kind, e.vk) for e in events] == [(kind, 0xA4)]


# --- injected events (FR-1.1) --------------------------------------------------------------------
def test_injected_events_are_dropped_and_counted(ht: HookThread, events: list[RawInput]) -> None:
    feed_mouse(ht, hooks.WM_LBUTTONDOWN, flags=hooks.LLMHF_INJECTED)
    feed_mouse(ht, hooks.WM_LBUTTONUP, flags=hooks.LLMHF_INJECTED)
    feed_key(ht, hooks.WM_KEYDOWN, flags=hooks.LLKHF_INJECTED)
    assert events == []
    assert ht.injected_ignored == 3


def test_injected_button_does_not_arm_the_drag_move_filter(
    ht: HookThread, events: list[RawInput]
) -> None:
    feed_mouse(ht, hooks.WM_LBUTTONDOWN, flags=hooks.LLMHF_INJECTED)
    feed_mouse(ht, hooks.WM_MOUSEMOVE)
    assert events == []


# --- Qt bridge -----------------------------------------------------------------------------------
def test_events_cross_to_the_qt_thread_queued(ht: HookThread, qtbot: QtBot) -> None:
    rx = Receiver()
    ht.raw_input.connect(rx.on_input)
    s = key_struct(VK_A)
    worker = threading.Thread(
        target=ht._dispatch_key,
        args=(hooks.WM_KEYDOWN, ctypes.addressof(s), time.perf_counter_ns()),
    )
    worker.start()
    worker.join()
    assert rx.got == []  # queued: nothing runs until the Qt thread processes events
    qtbot.waitUntil(lambda: bool(rx.got), timeout=2000)
    ev, thread_id = rx.got[0]
    assert thread_id == threading.get_ident()
    assert (ev.kind, ev.vk) == (InputKind.KEY_DOWN, VK_A)


def test_events_arrive_in_order_and_cheaply(ht: HookThread, qtbot: QtBot) -> None:
    """NFR-3 (body cost) and the state machine's need for ordered, lossless events."""
    n = 3000
    rx = Receiver()
    ht.raw_input.connect(rx.on_input)
    down = mouse_struct(0, 0)
    moves = [mouse_struct(i, 0) for i in range(n)]
    elapsed: list[int] = []

    def hook_thread_work() -> None:
        ht._dispatch_mouse(hooks.WM_LBUTTONDOWN, ctypes.addressof(down), time.perf_counter_ns())
        for s in moves:
            t0 = time.perf_counter_ns()
            ht._dispatch_mouse(hooks.WM_MOUSEMOVE, ctypes.addressof(s), t0)
            elapsed.append(time.perf_counter_ns() - t0)

    worker = threading.Thread(target=hook_thread_work)
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: len(rx.got) == n + 1, timeout=5000)
    assert [ev.x for ev, _ in rx.got[1:]] == list(range(n))
    assert sum(elapsed) / n < 100_000  # mean < 100 us, 10x under the 1 ms budget


# --- callbacks must never swallow input or raise -----------------------------------------------------
def test_callback_always_calls_next_hook_even_if_dispatch_raises(
    ht: HookThread, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []

    def next_hook(*args: object) -> int:
        calls.append(args)
        return 42

    def boom(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "user32", SimpleNamespace(CallNextHookEx=next_hook))
    monkeypatch.setattr(ht, "_dispatch_mouse", boom)
    monkeypatch.setattr(ht, "_dispatch_key", boom)
    assert ht._on_mouse(HC_ACTION, hooks.WM_LBUTTONDOWN, 0) == 42
    assert ht._on_key(HC_ACTION, hooks.WM_KEYDOWN, 0) == 42
    assert ht.errors == 2
    assert len(calls) == 2


def test_negative_ncode_is_passed_through_untouched(
    ht: HookThread, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows requires a hook to pass nCode < 0 straight to CallNextHookEx."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        hooks, "user32", SimpleNamespace(CallNextHookEx=lambda *a: calls.append(a) or 0)
    )
    monkeypatch.setattr(ht, "_dispatch_key", lambda *_: pytest.fail("must not dispatch"))
    ht._on_key(-1, hooks.WM_KEYDOWN, 0)
    assert len(calls) == 1


# --- watchdog decision -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("last_input", "last_seen", "dead"),
    [
        (10_000, 10_000, False),  # healthy: we saw the last input
        (10_500, 10_000, False),  # slightly behind, within the stale window
        (13_000, 10_000, True),  # the system saw input 3 s after we last did
        (9_000, 10_000, False),  # we saw something newer than the system reports
        (1_000, 0xFFFFF000, True),  # 32-bit tick wrap: input at 1000 vs seen at 4294963200
        (0xFFFFF000, 1_000, False),  # wrap, the other way: we are ahead
    ],
)
def test_hook_looks_dead(last_input: int, last_seen: int, dead: bool) -> None:
    assert hook_looks_dead(last_input, last_seen) is dead


@pytest.mark.parametrize(
    ("desktop", "fg", "self_elevated", "expected"),
    [
        ("Default", ("explorer.exe", False), False, None),
        ("Default", ("Taskmgr.exe", True), False, "an elevated window (Taskmgr.exe)"),
        ("Default", ("Taskmgr.exe", True), True, None),  # we are elevated too: no UIPI
        ("Default", ("", None), False, None),  # unknown: let the watchdog decide
        ("Winlogon", ("explorer.exe", False), False, "the secure desktop"),
        ("", ("explorer.exe", False), False, "the secure desktop"),  # could not open it
    ],
)
def test_unseeable_input(
    monkeypatch: pytest.MonkeyPatch, desktop, fg, self_elevated, expected
) -> None:
    monkeypatch.setattr(hooks, "_input_desktop", lambda: desktop)
    monkeypatch.setattr(hooks, "_foreground", lambda: fg)
    monkeypatch.setattr(hooks, "_SELF_ELEVATED", self_elevated)
    reason = hooks.unseeable_input()
    assert reason is None if expected is None else reason.startswith(expected)


def test_unseeable_input_reads_the_real_desktop() -> None:
    # This test runs non-elevated on the normal desktop, so there is nothing hidden.
    assert hooks._input_desktop().lower() in ("default", "")
    name, _elevated = hooks._foreground()
    assert isinstance(name, str)


def _stale_thread(monkeypatch: pytest.MonkeyPatch, reason: str | None) -> HookThread:
    """A HookThread whose watchdog sees input the hooks missed, and fakes the re-install."""
    ht = HookThread()
    monkeypatch.setattr(hooks, "_last_input_tick", lambda: ht._last_seen_tick + 10_000)
    monkeypatch.setattr(hooks, "unseeable_input", lambda: reason)
    monkeypatch.setattr(hooks, "_foreground", lambda: ("app.exe", False))
    monkeypatch.setattr(ht, "_uninstall", lambda: None)
    monkeypatch.setattr(ht, "_install", lambda: None)
    return ht


def test_watchdog_leaves_hooks_alone_when_input_is_out_of_reach(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ht = _stale_thread(monkeypatch, "an elevated window (Taskmgr.exe)")
    resets: list[int] = []
    ht.reinstalled.connect(lambda: resets.append(1))
    with caplog.at_level("INFO", logger=hooks.__name__):
        for _ in range(3):
            ht._watchdog_tick()
    assert ht.reinstall_count == 0
    assert resets == []  # the gesture state is kept
    assert len(caplog.records) == 1  # said once, not every tick
    assert caplog.records[0].levelname == "INFO"


def test_watchdog_reinstalls_when_the_cause_is_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ht = _stale_thread(monkeypatch, None)
    with caplog.at_level("WARNING", logger=hooks.__name__):
        ht._watchdog_tick()
    assert ht.reinstall_count == 1
    assert "foreground app.exe" in caplog.text


# --- live hooks -------------------------------------------------------------------------------------
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):  # only here so the INPUT union has its real size
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20  # TOKEN_INFORMATION_CLASS


def foreground() -> tuple[str, bool]:
    """(window class, hidden-from-us?) of the focused window.

    Windows (UIPI) does not show input going to an elevated window to a non-elevated hook, so a
    window whose process we cannot inspect counts as hidden too.
    """
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return "<no foreground window>", False
    cls = ctypes.create_unicode_buffer(128)
    _user32.GetClassNameW(hwnd, cls, 128)
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not proc:
        return f"{cls.value} (process not inspectable)", True
    token = wintypes.HANDLE()
    elevated = wintypes.DWORD(0)
    try:
        if _advapi32.OpenProcessToken(proc, TOKEN_QUERY, ctypes.byref(token)):
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(
                token, TOKEN_ELEVATION, ctypes.byref(elevated), 4, ctypes.byref(size)
            )
            _kernel32.CloseHandle(token)
    finally:
        _kernel32.CloseHandle(proc)
    return cls.value, bool(elevated.value)


def tap_f24() -> None:
    """Inject F24 down+up. Skips the test if hooks could not see it anyway."""
    name, hidden = foreground()
    if hidden:
        pytest.skip(f"foreground window {name} is elevated; Windows hides its input from the hooks")
    before = hooks._last_input_tick()
    inputs = (_INPUT * 2)()
    for i, flags in enumerate((0, KEYEVENTF_KEYUP)):
        inputs[i].type = INPUT_KEYBOARD
        inputs[i].ki = _KEYBDINPUT(VK_F24, 0, flags, 0, 0)
    sent = _user32.SendInput(2, inputs, ctypes.sizeof(_INPUT))
    time.sleep(0.05)
    if sent != 2 or hooks._last_input_tick() == before:
        pytest.skip("this session does not register injected input (no interactive desktop)")


def wait_for(condition: object, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def live(qapp: object):
    """Real hooks with a fast watchdog. Restores the GIL switch interval afterwards."""
    old_interval = sys.getswitchinterval()
    # stale_ms: GetLastInputInfo runs 50-250 ms ahead of the hooks right after an injected key
    # (measured here; cause outside the hooks), so 100 ms would re-install healthy hooks.
    thread = HookThread(watchdog_ms=100, stale_ms=1000)
    try:
        thread.start_hooks()
    except hooks.HookInstallError as exc:
        pytest.skip(f"cannot install low-level hooks in this session: {exc}")
    yield thread
    thread.stop_hooks()
    sys.setswitchinterval(old_interval)


def test_live_hooks_see_injected_input_and_drop_it(live: HookThread, qtbot: QtBot) -> None:
    out: list[RawInput] = []
    live.raw_input.connect(out.append)
    tap_f24()
    assert wait_for(lambda: live.injected_ignored >= 2), f"hooks never saw F24; fg={foreground()}"
    qtbot.wait(100)  # let any (wrongly) emitted signal be delivered
    # Real input from whoever is using the machine is forwarded, so only F24 must be absent.
    assert [e for e in out if e.vk == VK_F24] == []
    assert live.errors == 0


def test_stop_removes_the_hooks(live: HookThread) -> None:
    assert live.is_installed()
    live.stop_hooks()
    assert not live.is_installed()
    assert not live.isRunning()
    seen = live.injected_ignored
    tap_f24()
    time.sleep(0.2)
    assert live.injected_ignored == seen  # no hook left to see it


def test_start_hooks_lowers_the_gil_switch_interval(live: HookThread) -> None:
    assert sys.getswitchinterval() <= hooks.SWITCH_INTERVAL_S


def test_watchdog_reinstalls_hooks_that_windows_removed(live: HookThread, qtbot: QtBot) -> None:
    # A QObject receiver, not qtbot.waitSignal: tap_f24() may skip, and a skip inside waitSignal
    # leaves its blocker connected, so the watchdog's later emit crashes the teardown.
    counter = Counter()
    live.reinstalled.connect(counter.hit)
    for handle in list(live._hooks):  # what Windows does after a callback timeout
        hooks.user32.UnhookWindowsHookEx(handle)
    time.sleep(1.2)  # let the stale window pass
    tap_f24()  # the system sees input; the dead hooks do not
    qtbot.waitUntil(lambda: counter.hits > 0, timeout=5000)
    assert live.reinstall_count >= 1
    seen = live.injected_ignored
    tap_f24()
    assert wait_for(lambda: live.injected_ignored >= seen + 2), f"not live; fg={foreground()}"


def test_watchdog_leaves_healthy_hooks_alone(live: HookThread) -> None:
    for _ in range(4):
        tap_f24()
        time.sleep(0.2)
    assert live.reinstall_count == 0, f"fg={foreground()}"


def test_callback_latency_with_a_busy_python_thread_stays_under_1ms(live: HookThread) -> None:
    """NFR-3 including the GIL wait, which the callback body cannot measure itself."""
    s = key_struct(VK_A)
    stop = threading.Event()

    def burn() -> None:
        x = 0
        while not stop.is_set():
            x += 1

    burner = threading.Thread(target=burn, daemon=True)
    burner.start()
    samples = []
    for _ in range(300):
        t0 = time.perf_counter_ns()
        # Called through the C pointer, so the GIL is released and re-acquired like a real callback.
        live._key_proc(HC_ACTION, hooks.WM_KEYDOWN, ctypes.addressof(s))
        samples.append((time.perf_counter_ns() - t0) / 1e3)
        time.sleep(0.001)
    stop.set()
    samples.sort()
    assert samples[int(len(samples) * 0.9)] < 1000, f"p90={samples[int(len(samples) * 0.9)]:.0f} us"


def test_hooks_module_keeps_the_ui_process_lean() -> None:
    """CONTRIBUTING.md rule 2: nothing under dragshift/platform may import engine libraries."""
    heavy = ["PIL", "fitz", "pandas", "numpy", "pdf2docx", "pypandoc"]
    script = (
        "import sys, dragshift.platform.hooks;"
        f"print(','.join(m for m in {heavy!r} if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_first_event_after_start_hooks_is_not_slow() -> None:
    """In a fresh process the first cross-thread emit costs ~4 ms unless start_hooks warms it up."""
    script = """
import ctypes, threading, time
from PySide6.QtCore import QCoreApplication, QObject, Slot
from dragshift.platform import hooks

app = QCoreApplication([])
ht = hooks.HookThread()

class Rx(QObject):
    @Slot(object)
    def on(self, ev):
        pass

rx = Rx()
ht.raw_input.connect(rx.on)
try:
    ht.start_hooks()
except hooks.HookInstallError:
    pass  # the warm-up runs before the hooks are installed, so it still counts
s = hooks.KBDLLHOOKSTRUCT(0x41, 0, 0, 0, 0)
out = []

def work():
    t0 = time.perf_counter_ns()
    ht._dispatch_key(hooks.WM_KEYDOWN, ctypes.addressof(s), t0)
    out.append((time.perf_counter_ns() - t0) / 1e3)

t = threading.Thread(target=work)
t.start()
t.join()
ht.stop_hooks()
print(out[0])
"""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    run = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, env=env
    )
    assert float(run.stdout.strip().splitlines()[-1]) < 1000  # microseconds
