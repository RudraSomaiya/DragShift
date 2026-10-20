"""Explorer COM: the files selected in the window or Desktop a drag started in (PRD §9.6, FR-1.5).

This only pre-fills the wheel so its petals can render at once. The paths that arrive in
``dragEnterEvent`` are authoritative, so every failure here just returns an empty list.

Call on the Qt thread. Qt has already initialised COM there (it needs OLE for drag and drop).
pywin32 is imported on first use to keep start-up light.
"""

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CSIDL_DESKTOP = 0
SWC_DESKTOP = 8  # IShellWindows.FindWindowSW: look in the desktop
SWFO_NEEDDISPATCH = 1  # ... and return its IDispatch
CLSID_SHELL_WINDOWS = "{9BA05972-F6A8-11CF-A442-00A0C90A8F39}"


def paths_from_items(items: Any) -> list[Path]:
    """Real file-system paths of a ``FolderItems`` collection, in selection order.

    Virtual items (Recycle Bin, This PC) have paths such as ``::{GUID}`` and are skipped. (Do not
    test them with ``Path.drive``: on Windows ``Path('::{GUID}').drive`` is not empty.)
    """
    paths = []
    for i in range(items.Count):
        raw = items.Item(i).Path
        path = Path(raw)
        if not raw.startswith("::") and path.is_absolute():  # "::{GUID}" is a virtual item
            paths.append(path)
    return paths


def selected_paths(root_hwnd: int, root_class: str) -> list[Path]:
    """The selection in the Explorer window ``root_hwnd`` (class ``CabinetWClass``) or, for the
    Desktop (``Progman``/``WorkerW``), in the desktop view. Anything else gives ``[]``."""
    try:
        import pythoncom
        import win32com.client
        from win32com.client import VARIANT

        pythoncom.CoInitialize()  # a no-op (S_FALSE) when Qt already did it on this thread
        if root_class == "CabinetWClass":
            for window in win32com.client.Dispatch("Shell.Application").Windows():
                try:
                    if window.HWND == root_hwnd:
                        return paths_from_items(window.Document.SelectedItems())
                except Exception:  # a window without a folder view (IE, ...)
                    log.debug("skipping a window without a folder view", exc_info=True)
        elif root_class in ("Progman", "WorkerW"):
            shell_windows = win32com.client.Dispatch(CLSID_SHELL_WINDOWS)
            hwnd = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            desktop = shell_windows.FindWindowSW(
                VARIANT(pythoncom.VT_I4, CSIDL_DESKTOP),
                VARIANT(pythoncom.VT_EMPTY, None),
                SWC_DESKTOP,
                hwnd,
                SWFO_NEEDDISPATCH,
            )
            return paths_from_items(desktop.Document.SelectedItems())
    except Exception:  # COM can fail in many ways; the pre-fill is only a hint
        log.debug("could not read the Explorer selection", exc_info=True)
    return []
