"""One DragShift per user session (FR-6.3).

The first instance holds the named mutex ``Local\\DragShift.SingleInstance``. A second launch
sees ERROR_ALREADY_EXISTS, sets the named event ``Local\\DragShift.ShowSettings`` and exits; the
first instance checks that event twice a second and asks for Settings. No socket and no extra
Qt module (QtNetwork) is needed, which keeps the UI process small (NFR-1). (QWinEventNotifier
would avoid the timer, but PySide6 cannot deliver its ``activated(HANDLE)`` signal to Python.)
"""

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QObject, QTimer, Signal

MUTEX_NAME = "Local\\DragShift.SingleInstance"  # FR-6.3
EVENT_NAME = "Local\\DragShift.ShowSettings"
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0  # WaitForSingleObject: the event was set (and, auto-reset, is clear again)
POLL_MS = 500

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class SingleInstance(QObject):
    """Create it once, early. ``first`` says whether this process is the one that runs."""

    show_settings = Signal()  # a second launch asked the running instance for Settings

    def __init__(
        self,
        mutex_name: str = MUTEX_NAME,
        event_name: str = EVENT_NAME,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        # Auto-reset event, created (or opened) before the mutex, so a second launch that comes
        # while the first is still starting finds it and its request is not lost.
        self._event = _kernel32.CreateEventW(None, False, False, event_name)
        self._mutex = _kernel32.CreateMutexW(None, False, mutex_name)
        self.first = bool(self._mutex) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._check)
        if self.first and self._event:
            self._poll.start(POLL_MS)

    def _check(self) -> None:
        if self._event and _kernel32.WaitForSingleObject(self._event, 0) == WAIT_OBJECT_0:
            self.show_settings.emit()

    def notify_first(self) -> None:
        """From a second launch: ask the running instance to open Settings."""
        if self._event:
            _kernel32.SetEvent(self._event)

    def release(self) -> None:
        """Let go of the mutex and the event (on quit; Windows also does it at exit)."""
        self._poll.stop()
        for handle in (self._mutex, self._event):
            if handle:
                _kernel32.CloseHandle(handle)
        self._mutex = self._event = None
