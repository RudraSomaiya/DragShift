"""Esc during a REAL Explorer drag over the wheel: is the shell's drag image left behind? (DS-11)

Explorer is the drag source: a throwaway PNG on the Desktop is dragged with injected mouse input.
The wheel is opened under the cursor (what the gesture controller does when Shift goes down), the
cursor hovers a petal, then Esc is pressed (injected, Explorer cancels the OLE drag) and the wheel
is closed ``delay`` ms later. Afterwards the script looks for a visible ``SysDragImage`` window:
the dimmed icon with a prohibited sign that used to stay on screen.

Usage: real_dnd_esc_script.py 0,0,150,-1   (one run per delay in ms; -1 = never close). Prints a JSON object.
This moves the real cursor for a few seconds and puts a temporary PNG on the Desktop.
"""

import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

import real_dnd_script as h
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

from dragshift import gesture as gesture_module
from dragshift.gesture import GestureController
from dragshift.platform.hooks import InputKind, RawInput
from dragshift.ui.wheel import WheelWindow

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]

UIA_SCRIPT = """
param([long]$Hwnd, [string]$Prefix)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' `
    -Name U -Namespace W
[W.U]::SetProcessDPIAware() | Out-Null   # report physical pixels, like SendInput uses
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)  # one window only
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition)
foreach ($e in $all) {
    if ($e.Current.Name -like "$Prefix*" -and
        $e.Current.ControlType.ProgrammaticName -match 'ListItem|DataItem') {
        $r = $e.Current.BoundingRectangle
        "{0},{1},{2},{3}" -f [int]$r.X,[int]$r.Y,[int]$r.Width,[int]$r.Height
        break
    }
}
"""


def ghost_visible() -> bool:
    hwnd = user32.FindWindowW("SysDragImage", None)
    return bool(hwnd) and bool(user32.IsWindowVisible(hwnd))


def icon_centre(hwnd: int, prefix: str, script: Path) -> tuple[int, int] | None:
    out = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Hwnd",
            str(hwnd),
            "-Prefix",
            prefix,
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not out:
        return None
    x, y, w, ht = (int(v) for v in out.split(","))
    return x + min(w // 2, 60), y + ht // 2  # inside the row or tile, on the file


class FakeProbe:
    """The drag really is a file drag from Explorer; only the desktop questions are faked."""

    def drag_threshold(self) -> tuple[int, int]:
        return 4, 4

    def root_at(self, x: int, y: int) -> tuple[int, str]:
        return 1, "CabinetWClass"

    def sys_drag_image_visible(self) -> bool:
        return True

    def held_modifier_vks(self) -> frozenset[int]:
        return frozenset()


class Bridge(QObject):
    """Feeds the real GestureController the events the hook would, from the Qt thread."""

    open_wheel = Signal()
    press_esc = Signal()
    finish = Signal()

    def __init__(self, wheel: WheelWindow, png: Path) -> None:
        super().__init__()
        self.wheel = wheel
        self.gesture = GestureController(wheel, probe=FakeProbe(), selection=lambda *_: [png])
        self.petal_phys: tuple[int, int] = (0, 0)
        self.was_open = False  # was the wheel really showing when Esc arrived?
        self.open_wheel.connect(self._open)
        self.press_esc.connect(self._esc)
        self.finish.connect(self._finish)

    def _feed(self, kind: InputKind, vk: int = 0, x: int = 0, y: int = 0) -> None:
        self.gesture.on_input(RawInput(kind, x, y, vk, 0))

    @Slot()
    def _open(self) -> None:
        # LMB down, movement and a Shift key-down: exactly what makes the controller open a wheel.
        self._feed(InputKind.LBUTTON_DOWN, x=10, y=10)
        self._feed(InputKind.MOUSE_MOVE, x=60, y=10)
        self._feed(InputKind.KEY_DOWN, vk=0xA0)
        local = h.first_petal_point(self.wheel)
        g = self.wheel.mapToGlobal(local)
        dpr = self.wheel.devicePixelRatioF()
        self.petal_phys = (int(g.x() * dpr), int(g.y() * dpr))

    @Slot()
    def _esc(self) -> None:
        self.was_open = self.wheel.isVisible()
        self._feed(InputKind.KEY_DOWN, vk=h.VK_ESCAPE)  # the hook saw Esc

    @Slot()
    def _finish(self) -> None:
        """Release everything, so the next run starts clean (a held Shift would read as repeat)."""
        self._feed(InputKind.KEY_UP, vk=h.VK_ESCAPE)
        self._feed(InputKind.KEY_UP, vk=0xA0)
        self._feed(InputKind.LBUTTON_UP, x=10, y=10)


def main() -> int:
    delays = [int(d) for d in sys.argv[1].split(",")] if len(sys.argv) > 1 else [0]
    app = QApplication(sys.argv)
    here = Path(__file__).parent
    script = here / "_uia_icon.ps1"
    script.write_text(UIA_SCRIPT, encoding="utf-8")
    folder = Path(tempfile.mkdtemp(prefix="ds11-esc-"))
    png = folder / "ghost.png"
    from PIL import Image

    Image.new("RGB", (32, 32), (10, 120, 200)).save(png)
    saved = QCursor.pos()
    out: dict[str, dict[str, object]] = {}
    explorer = None
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        subprocess.Popen(f'explorer.exe /select,"{png}"')  # a real Explorer window, file selected
        shell = win32com.client.Dispatch("Shell.Application")
        centre = None
        for _ in range(30):
            for w in shell.Windows():
                try:
                    if Path(w.Document.Folder.Self.Path) == folder:
                        explorer = w
                except (pythoncom.com_error, AttributeError):  # not a folder view
                    continue
            if explorer:
                time.sleep(1.0)  # let it finish drawing
                centre = icon_centre(int(explorer.HWND), "ghost", script)
                break
            time.sleep(0.5)
        if not centre:
            print(json.dumps({"error": "could not find the file in the Explorer window"}))
            return 1
        wheel = WheelWindow()
        bridge = Bridge(wheel, png)
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)  # physical, DPI aware

        def norm(p: tuple[int, int]) -> tuple[int, int]:
            return p[0] * 65535 // (sw - 1), p[1] * 65535 // (sh - 1)

        for run, delay in enumerate(delays):
            info: dict[str, object] = {}
            if delay >= 0:
                gesture_module.ESC_CLOSE_DELAY_MS = delay

            def choreography(info: dict[str, object] = info) -> None:
                def move(p: tuple[int, int]) -> None:
                    x, y = norm(p)
                    h.mouse(h.MOUSEEVENTF_MOVE | h.MOUSEEVENTF_ABSOLUTE, x, y)
                    time.sleep(0.015)

                start = centre
                move(start)
                time.sleep(0.4)
                h.mouse(h.MOUSEEVENTF_LEFTDOWN)
                time.sleep(0.15)
                for i in range(1, 9):  # Explorer starts its OLE drag
                    move((start[0] + i * 8, start[1] + i * 3))
                time.sleep(0.5)
                info["image_during_drag"] = ghost_visible()
                bridge.open_wheel.emit()
                time.sleep(0.6)
                here_p = (start[0] + 64, start[1] + 24)
                target = bridge.petal_phys
                for i in range(1, 16):  # onto a petal, so the wheel is the current drop target
                    move(
                        (
                            here_p[0] + (target[0] - here_p[0]) * i // 15,
                            here_p[1] + (target[1] - here_p[1]) * i // 15,
                        )
                    )
                time.sleep(0.5)
                h.key(h.VK_ESCAPE)  # Explorer cancels the OLE drag...
                h.key(h.VK_ESCAPE, up=True)
                bridge.press_esc.emit()  # ...and the hook reports the same Esc to the app
                time.sleep(0.8)
                h.mouse(h.MOUSEEVENTF_LEFTUP)
                bridge.finish.emit()
                time.sleep(0.3)
                info["done"] = True

            t = threading.Thread(target=choreography, daemon=True)
            t.start()
            while t.is_alive():
                app.processEvents()
                time.sleep(0.01)
            wheel.close()
            for _ in range(25):
                app.processEvents()
                time.sleep(0.02)
            info["wheel_open_before_esc"] = bridge.was_open
            info["ghost_after"] = ghost_visible()
            out[f"run {run}, Esc close delay {delay} ms"] = info
            time.sleep(0.4)
    finally:
        h.key(h.VK_ESCAPE, up=True)
        QCursor.setPos(saved)
        if explorer is not None:
            explorer.Quit()
        shutil.rmtree(folder, ignore_errors=True)
        script.unlink(missing_ok=True)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
