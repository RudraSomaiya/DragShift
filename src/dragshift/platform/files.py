"""Open a file, or show it selected in Explorer: the toast's Open and Show (FR-5.5, FR-5.7).

Call on the Qt thread, where COM is already initialised (Qt's OleInitialize, for drag and drop).
"""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

__all__ = ["has_recycle_bin", "open_path", "show_in_folder"]

# GetDriveTypeW results for drives with no Recycle Bin: deleting there is permanent
DRIVE_REMOVABLE = 2  # USB sticks, SD cards
DRIVE_REMOTE = 4  # network shares and mapped drives

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
# GetDriveTypeW(root path, e.g. "C:\\" or "\\\\server\\share\\") -> DRIVE_* constant
_kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetDriveTypeW.restype = wintypes.UINT

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
# ILCreateFromPathW(path) -> PIDLIST_ABSOLUTE, or NULL if the path does not exist
_shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
_shell32.ILCreateFromPathW.restype = ctypes.c_void_p
# SHOpenFolderAndSelectItems(pidlFolder, cidl, apidl, dwFlags) -> HRESULT
_shell32.SHOpenFolderAndSelectItems.argtypes = [
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
_shell32.ILFree.argtypes = [ctypes.c_void_p]
_shell32.ILFree.restype = None


def has_recycle_bin(path: Path) -> bool:
    """False on network and removable drives, where the shell deletes for good instead of
    recycling, without asking when told not to confirm (as send2trash does)."""
    return _kernel32.GetDriveTypeW(Path(path).anchor) not in (DRIVE_REMOVABLE, DRIVE_REMOTE)


def open_path(path: Path) -> None:
    """Open ``path`` with its default app (a folder opens in Explorer). Raises OSError."""
    os.startfile(path)


def show_in_folder(path: Path) -> None:
    """Open ``path``'s folder in Explorer with ``path`` selected, reusing a window already
    showing that folder. Raises OSError."""
    pidl = _shell32.ILCreateFromPathW(str(path))
    if not pidl:
        raise FileNotFoundError(f"not found: {path}")
    try:
        # cidl = 0: pidlFolder is the item itself, and Explorer selects it in its parent.
        hr = _shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
    finally:
        _shell32.ILFree(pidl)
    if hr < 0:
        raise OSError(f"SHOpenFolderAndSelectItems failed: 0x{hr & 0xFFFFFFFF:08X}")
