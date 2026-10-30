"""GPU engine utilisation for this process via the Windows PDH counters
``\\GPU Engine(*)\\Utilization Percentage`` filtered to ``pid_<PID>_*`` (the source Task Manager uses)."""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections import defaultdict
from ctypes import wintypes

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
PDH_INVALID_DATA = 0xC0000BC6
PDH_CSTATUS_VALID_DATA = 0x0
PDH_CSTATUS_NEW_DATA = 0x1


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", PDH_FMT_COUNTERVALUE)]


class GpuEngineSampler:
    """Call ``sample()`` once to start an interval and again to read the average
    utilisation per engine type (3D, VideoEncode, VideoProcessing, Copy, ...)."""

    def __init__(self, pid: int | None = None) -> None:
        self.available = sys.platform == "win32"
        self.pid = pid or os.getpid()
        self._query = wintypes.HANDLE()
        self._counter = wintypes.HANDLE()
        if not self.available:
            return
        self._pdh = ctypes.WinDLL("pdh")
        self._pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        self._pdh.PdhAddEnglishCounterW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)
        ]
        self._pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        self._pdh.PdhGetFormattedCounterArrayW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p
        ]
        self._pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
        if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
            self.available = False
            return
        path = "\\GPU Engine(*)\\Utilization Percentage"
        if self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(self._counter)) != 0:
            self.available = False

    def sample(self) -> dict[str, float]:
        if not self.available:
            return {}
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            return {}
        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        status = self._pdh.PdhGetFormattedCounterArrayW(self._counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if (status & 0xFFFFFFFF) != PDH_MORE_DATA or size.value == 0:
            return {}  # first sample of an interval, or no GPU activity yet
        buffer = (ctypes.c_byte * size.value)()
        status = self._pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buffer
        )
        if status != 0:
            return {}
        items = ctypes.cast(buffer, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        totals: dict[str, float] = defaultdict(float)
        for i in range(count.value):
            item = items[i]
            if item.FmtValue.CStatus not in (PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA):
                continue
            if not (item.szName or "").startswith(f"pid_{self.pid}_"):
                continue
            match = re.search(r"engtype_(\w+)", item.szName or "")
            engine = match.group(1) if match else "other"
            totals[engine] += item.FmtValue.doubleValue
        return dict(totals)

    def close(self) -> None:
        if self.available:
            self._pdh.PdhCloseQuery(self._query)
            self.available = False
