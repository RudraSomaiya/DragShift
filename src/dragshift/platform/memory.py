"""This process's memory, and giving idle pages back to Windows (NFR-1, "Trim memory when idle").

``trim`` empties the working set: the pages go to the standby list and come back on demand, so
the number Task Manager shows drops to a few MB after a wheel or a dialog. It does not change
*private bytes* (the commit charge M1 is measured in); that is kept low by the lean imports and
the GDI font engine.
"""

import ctypes
import gc
from ctypes import wintypes
from typing import NamedTuple

__all__ = ["Usage", "trim", "usage"]

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),  # the "Private bytes" of Process Explorer / PerfMon
    ]


_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.K32GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_ProcessMemoryCountersEx),
    wintypes.DWORD,
]
_kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
_kernel32.K32EmptyWorkingSet.argtypes = [wintypes.HANDLE]
_kernel32.K32EmptyWorkingSet.restype = wintypes.BOOL


class Usage(NamedTuple):
    """Private bytes and working set of this process, in MB."""

    private_mb: float
    working_set_mb: float


def usage() -> Usage:
    counters = _ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    # K32GetProcessMemoryInfo(process, PROCESS_MEMORY_COUNTERS*, cb) -> BOOL
    _kernel32.K32GetProcessMemoryInfo(
        _kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    return Usage(counters.PrivateUsage / 2**20, counters.WorkingSetSize / 2**20)


def trim() -> bool:
    """Collect garbage, then K32EmptyWorkingSet on this process. False if Windows refused."""
    gc.collect()
    return bool(_kernel32.K32EmptyWorkingSet(_kernel32.GetCurrentProcess()))
