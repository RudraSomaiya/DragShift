"""Start DragShift at sign-in (FR-7.2): the ``HKCU\\...\\Run\\DragShift`` registry value.

The value is ``"<venv>\\Scripts\\pythonw.exe" -m dragshift``: pythonw has no console, and the
venv's own interpreter finds the installed package from any working folder. ``config.general.
autostart`` is the source of truth; ``sync`` makes the registry match it.
"""

import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DragShift"


def pythonw() -> Path:
    """The windowless interpreter next to the one running now (``python.exe`` -> ``pythonw.exe``)."""
    exe = Path(sys.executable)
    return exe if exe.name.lower() == "pythonw.exe" else exe.with_name("pythonw.exe")


def command() -> str:
    return f'"{pythonw()}" -m dragshift'


def is_enabled(key: str = RUN_KEY) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.QueryValueEx(k, VALUE_NAME)
            return True
    except FileNotFoundError:
        return False


def enable(key: str = RUN_KEY) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, command())


def disable(key: str = RUN_KEY) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
    except FileNotFoundError:
        pass  # already off


def sync(wanted: bool, key: str = RUN_KEY) -> None:
    """Make the registry match the config. Re-writes the value when on, so it follows the venv
    if the project folder moved."""
    if wanted:
        enable(key)
    else:
        disable(key)
