"""Repair a lost Windows topmost style without activating or moving a window."""
import ctypes
from ctypes import wintypes as wt
from functools import lru_cache
import sys

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200


def state_changed(message):
    """Inspect only messages for this QWidget; leave normal Qt handling intact."""
    if sys.platform != "win32":
        return False
    msg = wt.MSG.from_address(int(message))
    # WM_WINDOWPOSCHANGED, WM_STYLECHANGED, WM_SHOWWINDOW, WM_ACTIVATE.
    return msg.message in (0x0047, 0x007D, 0x0018, 0x0006)


@lru_cache(maxsize=1)
def _user32():
    api = ctypes.WinDLL("user32", use_last_error=True)
    for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
        getattr(api, name).argtypes = [wt.HWND]
        getattr(api, name).restype = wt.BOOL
    # The installer is x64; retain source-mode support for 32-bit Python.
    get_style = api.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else api.GetWindowLongW
    get_style.argtypes = [wt.HWND, ctypes.c_int]
    get_style.restype = ctypes.c_ssize_t
    api.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
    api.SetWindowPos.restype = wt.BOOL
    return api, get_style


def restore_if_lost(hwnd):
    """Already-topmost, hidden and minimized windows are never reordered."""
    if sys.platform != "win32" or not hwnd:
        return False
    api, get_style = _user32()
    if not api.IsWindow(hwnd) or not api.IsWindowVisible(hwnd) or api.IsIconic(hwnd):
        return False
    ctypes.set_last_error(0)
    style = get_style(hwnd, GWL_EXSTYLE)
    if (not style and ctypes.get_last_error()) or style & WS_EX_TOPMOST:
        return False
    return bool(api.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER))
