"""Quick Launch gates G-a ... G-f (FR-3.2), with a fake desktop and a smoke test of the real one."""

import dataclasses

import pytest

from dragshift.config import Config, General, Launcher
from dragshift.platform import context_guard
from dragshift.platform.context_guard import Win32Context, failed_gate, is_blocked, is_text_input


@dataclasses.dataclass
class Desktop:
    root: tuple[int, str] = (222, "Progman")
    focused: str | None = "SysListView32"
    state: int | None = 5  # QUNS_ACCEPTS_NOTIFICATIONS
    process: str | None = "C:\\Windows\\explorer.exe"
    selected: int | None = 0
    asked_selection: bool = False

    def foreground(self) -> tuple[int, str]:
        return self.root

    def focused_class(self) -> str | None:
        return self.focused

    def notification_state(self) -> int | None:
        return self.state

    def foreground_process(self) -> str | None:
        return self.process

    def selection_count(self, hwnd: int, root_class: str) -> int | None:
        self.asked_selection = True
        return self.selected


def test_an_empty_desktop_or_explorer_window_passes() -> None:
    assert failed_gate(Desktop(), Config()) is None
    assert failed_gate(Desktop(root=(1, "WorkerW")), Config()) is None
    assert failed_gate(Desktop(root=(1, "CabinetWClass"), focused="DirectUIHWND"), Config()) is None
    assert failed_gate(Desktop(focused=""), Config()) is None  # nothing focused


@pytest.mark.parametrize(
    ("desktop", "config", "gate"),
    [
        (Desktop(), Config(general=General(paused=True)), "G-f"),
        (Desktop(), Config(launcher=Launcher(enabled=False)), "G-f"),
        (Desktop(root=(1, "Notepad")), Config(), "G-a"),
        (Desktop(root=(1, "Shell_TrayWnd")), Config(), "G-a"),  # the taskbar
        (Desktop(root=(0, "")), Config(), "G-a"),
        (Desktop(focused="Edit"), Config(), "G-c"),  # renaming a file
        (Desktop(focused="RICHEDIT50W"), Config(), "G-c"),
        (Desktop(focused="Windows.UI.Input.InputSite.WindowClass"), Config(), "G-c"),  # search
        (Desktop(focused=None), Config(), "G-c"),
        (Desktop(state=2), Config(), "G-b"),  # QUNS_BUSY
        (Desktop(state=3), Config(), "G-b"),  # QUNS_RUNNING_D3D_FULL_SCREEN
        (Desktop(state=4), Config(), "G-b"),  # QUNS_PRESENTATION_MODE
        (Desktop(state=None), Config(), "G-b"),
        (Desktop(), Config(general=General(blocklist=("EXPLORER.EXE",))), "G-e"),
        (Desktop(process=None), Config(general=General(blocklist=("x.exe",))), "G-e"),
        (Desktop(selected=1), Config(), "G-d"),
        (Desktop(selected=None), Config(), "G-d"),  # could not tell
    ],
)
def test_each_gate_fails_closed(desktop: Desktop, config: Config, gate: str) -> None:
    assert failed_gate(desktop, config) == gate


def test_the_explorer_com_check_runs_only_when_the_cheap_gates_pass() -> None:
    notepad = Desktop(root=(1, "Notepad"))
    failed_gate(notepad, Config())
    assert not notepad.asked_selection
    desktop = Desktop()
    failed_gate(desktop, Config())
    assert desktop.asked_selection


def test_text_inputs() -> None:
    assert is_text_input("Edit") and is_text_input("RichEdit20W")
    assert not is_text_input("SysListView32") and not is_text_input("DirectUIHWND")


def test_blocklist_matches_a_name_or_a_full_path_ignoring_case() -> None:
    exe = "C:\\Games\\Valorant\\VALORANT.exe"
    assert is_blocked(exe, ("valorant.exe",))
    assert is_blocked(exe, ("c:\\games\\valorant\\valorant.exe",))
    assert not is_blocked(exe, ("valo.exe", "C:\\Other\\VALORANT.exe"))
    assert not is_blocked(exe, ())


def test_the_real_probe_answers_sensibly() -> None:
    real = Win32Context()
    hwnd, cls = real.foreground()
    assert isinstance(hwnd, int) and isinstance(cls, str)
    assert real.focused_class() is None or isinstance(real.focused_class(), str)
    assert real.notification_state() in (None, *range(1, 8))
    process = real.foreground_process()
    assert process is None or process.lower().endswith(".exe")
    assert context_guard.failed_gate(real, Config()) in (None, "G-a", "G-b", "G-c", "G-d", "G-e")
