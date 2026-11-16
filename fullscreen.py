"""Detect foreground fullscreen apps without hooks, injection or game APIs."""
import ctypes
from ctypes import wintypes as wt
from functools import lru_cache
import os


POLL_MS = 250
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("monitor", wt.RECT),
                ("work", wt.RECT), ("flags", wt.DWORD)]


@lru_cache(maxsize=1)
def _user32():
    api = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "GetForegroundWindow": ([], wt.HWND),
        "IsWindowVisible": ([wt.HWND], wt.BOOL),
        "IsIconic": ([wt.HWND], wt.BOOL),
        "GetWindowThreadProcessId": ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
        "GetClassNameW": ([wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetClientRect": ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
        "ClientToScreen": ([wt.HWND, ctypes.POINTER(wt.POINT)], wt.BOOL),
        "MonitorFromWindow": ([wt.HWND, wt.DWORD], wt.HANDLE),
        "GetMonitorInfoW": ([wt.HANDLE, ctypes.POINTER(MonitorInfo)], wt.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    if hasattr(api, "SetThreadDpiAwarenessContext"):
        api.SetThreadDpiAwarenessContext.argtypes = [wt.HANDLE]
        api.SetThreadDpiAwarenessContext.restype = wt.HANDLE
    return api


def _foreground_fullscreen(api):
    window = api.GetForegroundWindow()
    if not window:
        return None  # Focus can briefly be absent while switching apps.
    if not api.IsWindowVisible(window) or api.IsIconic(window):
        return False
    pid = wt.DWORD()
    if not api.GetWindowThreadProcessId(window, ctypes.byref(pid)):
        return None
    if pid.value == os.getpid():
        return False
    name = ctypes.create_unicode_buffer(256)
    if not api.GetClassNameW(window, name, len(name)):
        return None
    if name.value in _SHELL_CLASSES:
        return False

    monitor = api.MonitorFromWindow(window, 2)  # MONITOR_DEFAULTTONEAREST
    info = MonitorInfo()
    info.size = ctypes.sizeof(info)
    if not monitor or not api.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    client = wt.RECT()
    if not api.GetClientRect(window, ctypes.byref(client)):
        return None
    top_left = wt.POINT(client.left, client.top)
    bottom_right = wt.POINT(client.right, client.bottom)
    if (not api.ClientToScreen(window, ctypes.byref(top_left))
            or not api.ClientToScreen(window, ctypes.byref(bottom_right))):
        return None
    if api.GetForegroundWindow() != window:
        return None  # Do not apply a stale sample after Alt+Tab.
    screen = info.monitor
    # Compare the client area to the whole monitor, not its work area. A normal
    # maximized window still has a caption/taskbar and must remain unobscured.
    return (screen.right > screen.left and screen.bottom > screen.top
            and top_left.x <= screen.left and top_left.y <= screen.top
            and bottom_right.x >= screen.right and bottom_right.y >= screen.bottom)


def foreground_fullscreen():
    """True/False on a valid sample; None preserves visibility on transient failure.

    Covers fullscreen games and borderless apps on any foreground monitor.
    All geometry is read in one DPI context, including mixed-scale monitors.
    """
    if os.name != "nt":
        return False
    try:
        api = _user32()
        set_dpi = getattr(api, "SetThreadDpiAwarenessContext", None)
        previous = set_dpi(-4) if set_dpi else None  # PER_MONITOR_AWARE_V2
        try:
            return _foreground_fullscreen(api)
        finally:
            if previous:
                set_dpi(previous)
    except (OSError, AttributeError):
        return None
