"""Thin ctypes wrappers over user32 / dwmapi / kernel32 used for enumeration and window
management. Every function degrades to a harmless default on non-Windows platforms so the
rest of the application (and its unit tests) can be imported anywhere."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from screenrecorder.core.models import MonitorInfo, Rect, WindowInfo

IS_WINDOWS = sys.platform == "win32"

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_F9 = 0x78
VK_F10 = 0x79

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MONITORINFOF_PRIMARY = 1
_WDA_NONE = 0x0
_WDA_EXCLUDEFROMCAPTURE = 0x11


if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    try:
        shcore = ctypes.WinDLL("shcore")
    except OSError:  # pragma: no cover
        shcore = None

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM]
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    _get_window_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    _get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_window_long.restype = ctypes.c_ssize_t
    dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def enable_dpi_awareness() -> None:
    """Per-monitor v2 DPI awareness so every coordinate is in physical pixels. Qt 6 does
    the same on startup; calling it first keeps headless tools consistent."""
    if not IS_WINDOWS:
        return
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        if shcore is not None:
            shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass


def _monitor_scale(hmonitor: int) -> float:
    if shcore is None:
        return 1.0
    dpi_x = wintypes.UINT()
    dpi_y = wintypes.UINT()
    try:
        if shcore.GetDpiForMonitor(wintypes.HMONITOR(hmonitor), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
            return dpi_x.value / 96.0
    except OSError:
        pass
    return 1.0


def list_monitors() -> list[MonitorInfo]:
    if not IS_WINDOWS:
        return []
    found: list[tuple[int, MONITORINFOEXW]] = []

    def callback(hmonitor, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            found.append((int(hmonitor), info))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    # Primary first, then left-to-right, so "Monitor 1" is stable and intuitive.
    found.sort(key=lambda item: (not (item[1].dwFlags & _MONITORINFOF_PRIMARY), item[1].rcMonitor.left))
    monitors = []
    for index, (handle, info) in enumerate(found):
        r = info.rcMonitor
        monitors.append(
            MonitorInfo(
                handle=handle,
                device_name=info.szDevice,
                rect=Rect(r.left, r.top, r.right - r.left, r.bottom - r.top),
                primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                index=index,
                scale=_monitor_scale(handle),
            )
        )
    return monitors


def _process_name(pid: int) -> str:
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


def window_rect(hwnd: int) -> Rect | None:
    if not IS_WINDOWS:
        return None
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
        return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    return None


def is_window(hwnd: int) -> bool:
    return bool(IS_WINDOWS and user32.IsWindow(hwnd))


def list_windows(exclude_pids: set[int] | None = None) -> list[WindowInfo]:
    """Top-level, visible, non-cloaked, titled application windows (what WGC can capture)."""
    if not IS_WINDOWS:
        return []
    exclude = exclude_pids if exclude_pids is not None else {os.getpid()}
    windows: list[WindowInfo] = []

    def callback(hwnd, _lparam):
        if not hwnd or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        if _get_window_long(hwnd, _GWL_EXSTYLE) & (_WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE):
            return True
        cloaked = wintypes.DWORD(0)
        dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in exclude:
            return True
        rect = window_rect(hwnd)
        if rect is None or rect.is_empty():
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        windows.append(
            WindowInfo(
                handle=int(hwnd),
                title=buffer.value,
                process_name=_process_name(pid.value),
                pid=pid.value,
                rect=rect,
                minimized=bool(user32.IsIconic(hwnd)),
            )
        )
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return windows


def set_exclude_from_capture(hwnd: int, exclude: bool) -> bool:
    """Hide our own window from screen capture (Windows 10 2004+)."""
    if not IS_WINDOWS:
        return False
    return bool(user32.SetWindowDisplayAffinity(hwnd, _WDA_EXCLUDEFROMCAPTURE if exclude else _WDA_NONE))


def register_hotkey(hwnd: int, hotkey_id: int, modifiers: int, vk: int) -> bool:
    if not IS_WINDOWS:
        return False
    return bool(user32.RegisterHotKey(hwnd, hotkey_id, modifiers | MOD_NOREPEAT, vk))


def unregister_hotkey(hwnd: int, hotkey_id: int) -> None:
    if IS_WINDOWS:
        user32.UnregisterHotKey(hwnd, hotkey_id)


def hotkey_id_from_message(message_address: int) -> int | None:
    """Decode a WM_HOTKEY from the MSG pointer Qt passes to QWidget.nativeEvent."""
    if not IS_WINDOWS or not message_address:
        return None
    msg = wintypes.MSG.from_address(message_address)
    if msg.message == WM_HOTKEY:
        return int(msg.wParam)
    return None


def reveal_in_explorer(path: Path) -> None:
    if IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", str(path)])  # noqa: S603,S607


def open_with_default_app(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
