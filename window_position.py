"""Keep saved floating-window positions on the nearest available monitor."""
import ctypes
from ctypes import wintypes as wt
import math
import os


def visible_position(x, y, width, height, areas):
    if not areas:
        return x, y
    if not isinstance(x, (int, float)) or not math.isfinite(x):
        x = areas[0][0]
    if not isinstance(y, (int, float)) or not math.isfinite(y):
        y = areas[0][1]
    candidates = [(max(left, min(x, left + max(0, w - width))),
                   max(top, min(y, top + max(0, h - height))))
                  for left, top, w, h in areas]
    px, py = min(candidates, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
    return int(px), int(py)


def windows_work_areas():
    """Win32 rectangles in the calling UI thread's DPI coordinate space (Tk)."""
    if os.name != "nt":
        return []
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wt.DWORD), ("monitor", wt.RECT),
                    ("work", wt.RECT), ("flags", wt.DWORD)]
    api = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    api.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MonitorInfo)]
    api.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), callback_type, wt.LPARAM]
    areas = []
    @callback_type
    def collect(monitor, dc, rect, data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if api.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.work
            areas.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True
    api.EnumDisplayMonitors(None, None, collect, 0)
    return areas
