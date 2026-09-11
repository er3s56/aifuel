# -*- coding: utf-8 -*-
"""Windows 任务栏定位，配合独立占位扩展为额度面板预留空间。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from dataclasses import dataclass
import os
import threading
import time

from taskbar_widgets import taskbar_button_rects
from taskbar_reservation import ReservationClient


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    def contains(self, other):
        return (self.left <= other.left and self.top <= other.top
                and self.right >= other.right and self.bottom >= other.bottom)


@dataclass(frozen=True)
class Placement:
    state: str
    rect: Rect | None = None
    reason: str = ""
    taskbar_hwnd: int = 0


def arrange(bar: Rect, tray: Rect, monitor: Rect, occupied_right: int,
            width: int, dpi: int = 96, blocked: tuple[Rect, ...] = ()) -> Placement:
    """所有坐标都为物理像素；装不下就保留完整悬浮窗，不截掉额度项。"""
    if bar.height <= 0 or bar.width <= bar.height:
        return Placement("fallback", reason="当前任务栏方向不支持，暂以悬浮窗显示")
    visible_height = min(bar.bottom, monitor.bottom) - max(bar.top, monitor.top)
    if visible_height < bar.height - 2 or bar.right <= monitor.left or bar.left >= monitor.right:
        return Placement("hidden")
    if not bar.contains(tray) or tray.width <= 0:
        return Placement("fallback", reason="未能定位系统托盘，暂以悬浮窗显示")
    gap = max(4, round(6 * dpi / 96))
    height = min(bar.height - 4, round(48 * dpi / 96))
    if height < round(38 * dpi / 96):
        return Placement("fallback", reason="任务栏高度不足，暂以悬浮窗显示")
    right = tray.left - gap
    # 从右向左寻找能容纳整块额度窗的空隙，每个按钮两侧都留出点击间距。
    for obstacle in sorted(blocked, key=lambda rect: rect.left, reverse=True):
        if (obstacle.bottom > bar.top and obstacle.top < bar.bottom
                and obstacle.left < right + gap and obstacle.right > right - width - gap):
            right = obstacle.left - gap
    left = right - width
    if left < max(bar.left, occupied_right) + gap:
        return Placement("fallback", reason="任务栏空间不足，暂以悬浮窗显示全部额度")
    top = bar.top + (bar.height - height) // 2
    return Placement("docked", Rect(left, top, right, top + height))


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class WindowsTaskbar:
    def __init__(self):
        if os.name != "nt":
            raise OSError("任务栏模式仅支持 Windows")
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self._buttons_key = None
        self._buttons_result = None
        self._buttons_worker = None
        self._buttons_next = 0.0
        self._buttons_epoch = 0
        self._owned_window = None
        self._reservation = None
        self.callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        signatures = {
            "FindWindowW": ([wt.LPCWSTR, wt.LPCWSTR], wt.HWND),
            "FindWindowExW": ([wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR], wt.HWND),
            "GetWindowRect": ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
            "GetClassNameW": ([wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
            "IsWindowVisible": ([wt.HWND], wt.BOOL),
            "IsWindow": ([wt.HWND], wt.BOOL),
            "GetWindow": ([wt.HWND, wt.UINT], wt.HWND),
            "GetDpiForWindow": ([wt.HWND], wt.UINT),
            "GetForegroundWindow": ([], wt.HWND),
            "GetWindowThreadProcessId": ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
            "EnumChildWindows": ([wt.HWND, self.callback_type, wt.LPARAM], wt.BOOL),
            "MonitorFromWindow": ([wt.HWND, wt.DWORD], wt.HANDLE),
            "GetMonitorInfoW": ([wt.HANDLE, ctypes.POINTER(_MonitorInfo)], wt.BOOL),
            "SetWindowPos": ([wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wt.UINT], wt.BOOL),
            "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self._set_window_long = getattr(self.api, "SetWindowLongPtrW", None) or self.api.SetWindowLongW
        self._set_window_long.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._set_window_long.restype = ctypes.c_ssize_t

    def _rect(self, hwnd):
        value = wt.RECT()
        if not self.api.GetWindowRect(hwnd, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Rect(value.left, value.top, value.right, value.bottom)

    def reserve(self, hwnd, logical_width):
        if self._reservation is None:
            self._reservation = ReservationClient(self.api)
        self._reservation.request(hwnd, logical_width)

    def release_space(self, closing=False):
        # A later reservation must not reuse button positions measured before it.
        self._buttons_key = self._buttons_result = None
        self._buttons_next = 0.0
        self._buttons_epoch += 1
        if self._reservation is not None:
            if closing:
                self._reservation.close()
            else:
                self._reservation.release()

    def _class(self, hwnd):
        value = ctypes.create_unicode_buffer(256)
        self.api.GetClassNameW(hwnd, value, len(value))
        return value.value

    def placement(self, logical_width: int) -> Placement:
        # Qt 的逻辑坐标与 Win32 的屏幕坐标不能混用（尤其是多屏、不同 DPI）。
        previous = self.api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            return self._placement(logical_width)
        except OSError:
            return Placement("fallback", reason="任务栏定位失败，暂以悬浮窗显示")
        finally:
            if previous:
                self.api.SetThreadDpiAwarenessContext(previous)

    def _placement(self, logical_width):
        bar_hwnd = self.api.FindWindowW("Shell_TrayWnd", None)
        if not bar_hwnd or not self.api.IsWindowVisible(bar_hwnd):
            # Explorer 重启或任务栏隐藏时等待下一轮，不能把旧位置留在桌面上。
            return Placement("hidden")
        tray_hwnd = self.api.FindWindowExW(bar_hwnd, None, "TrayNotifyWnd", None)
        if not tray_hwnd:
            return Placement("fallback", reason="未能定位系统托盘，暂以悬浮窗显示")
        bar, tray = self._rect(bar_hwnd), self._rect(tray_hwnd)
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        monitor_handle = self.api.MonitorFromWindow(bar_hwnd, 2)
        if not self.api.GetMonitorInfoW(monitor_handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        monitor = Rect(info.rcMonitor.left, info.rcMonitor.top,
                       info.rcMonitor.right, info.rcMonitor.bottom)
        foreground = self.api.GetForegroundWindow()
        if foreground and foreground != bar_hwnd:
            process = wt.DWORD()
            self.api.GetWindowThreadProcessId(foreground, ctypes.byref(process))
            if (process.value != os.getpid()
                    and self._class(foreground) not in {"Progman", "WorkerW"}
                    and self._rect(foreground).contains(monitor)):
                return Placement("hidden")

        occupied_right = bar.left

        @self.callback_type
        def child(hwnd, _):
            nonlocal occupied_right
            if self._class(hwnd) in {"MSTaskSwWClass", "ReBarWindow32"} and self.api.IsWindowVisible(hwnd):
                try:
                    rect = self._rect(hwnd)
                    if bar.contains(rect):
                        occupied_right = max(occupied_right, rect.right)
                except OSError:
                    pass
            return True

        self.api.EnumChildWindows(bar_hwnd, child, 0)
        dpi = self.api.GetDpiForWindow(bar_hwnd) or 96
        # 扩展只调整应用按钮区域；天气/股票等入口仍需实际边界确认。
        # 不能把扫描尚未完成当成没有障碍物，否则首次布局会短暂重叠。
        reserved = bool(self._reservation and self._reservation.ready)
        reservation = (self._reservation.hwnd, logical_width) if reserved else None
        blocked = self._button_bounds(bar_hwnd, bar, tray, dpi, occupied_right, reservation)
        boundary = bar.left if reserved else occupied_right
        # Qt rounds positive half pixels upward; Python round() uses ties-to-even.
        # Match Qt so a mode switch does not change a 403-DIP panel by one pixel.
        physical_width = int(logical_width * dpi / 96 + .5)
        result = arrange(bar, tray, monitor, boundary,
                         physical_width, dpi, blocked or ())
        if result.state == "docked" and blocked is None:
            result = Placement("pending" if self._reservation else "fallback",
                               reason="正在确认任务栏按钮位置")
        if result.state == "fallback" and self._reservation and self._reservation.error:
            result = Placement("fallback", reason="任务栏占位未启用：" + self._reservation.error)
        elif (result.state == "fallback" and self._reservation and not reserved
              and (blocked is None or result.reason.startswith("任务栏空间不足"))
              and bar.width > bar.height and bar.height >= round(38 * dpi / 96) + 4):
            result = Placement("pending", reason="正在准备任务栏显示")
        return Placement(result.state, result.rect, result.reason, bar_hwnd)

    def _button_bounds(self, hwnd, bar, tray, dpi, occupied_right, reservation=None):
        key = (hwnd, bar, tray, dpi, occupied_right, reservation, self._buttons_epoch)
        if key != self._buttons_key:
            self._buttons_key, self._buttons_next = key, 0
        now = time.monotonic()
        if now >= self._buttons_next and not (self._buttons_worker and self._buttons_worker.is_alive()):
            self._buttons_next = now + .5

            def query():
                try:
                    result = tuple(Rect(*value) for value in taskbar_button_rects(hwnd))
                    # 只有托盘或没有按钮，可能是 Explorer 的 UIA 树尚未就绪。
                    if not any(rect.left < tray.left and rect.right > bar.left
                               and rect.top < bar.bottom and rect.bottom > bar.top
                               for rect in result):
                        result = None
                except Exception:
                    result = None
                # 连同布局标识一起发布，旧查询完成时不能覆盖新位置的判断。
                self._buttons_result = (key, now, result)

            # UI Automation 的跨进程查询不能阻塞 Qt 主线程；只允许一个查询在途。
            self._buttons_worker = threading.Thread(target=query, daemon=True)
            self._buttons_worker.start()
        snapshot = self._buttons_result
        if snapshot and snapshot[0] == key and now - snapshot[1] <= 1.5:
            return snapshot[2]
        # 首次查询、布局变化或查询卡住时不能把未知区域当成空白。
        return None

    def _set_owner(self, hwnd, owner):
        ctypes.set_last_error(0)
        previous = self._set_window_long(hwnd, -8, owner)  # GWLP_HWNDPARENT
        error = ctypes.get_last_error()
        if not previous and error:
            raise ctypes.WinError(error)

    def is_our_window(self, hwnd: int):
        process = wt.DWORD()
        self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
        return process.value == os.getpid()

    def attach(self, hwnd: int, taskbar_hwnd: int):
        """让系统始终把额度窗排在任务栏之前，不再靠定时重新置顶。"""
        if not self.is_our_window(hwnd) or not taskbar_hwnd or not self.api.IsWindow(taskbar_hwnd):
            raise OSError("Taskbar no longer exists")
        if self._owned_window and self._owned_window[0] != hwnd:
            self.detach()
        previous = (self._owned_window[1] if self._owned_window
                    else self.api.GetWindow(hwnd, 4) or 0)  # GW_OWNER
        if self.api.GetWindow(hwnd, 4) != taskbar_hwnd:
            self._set_owner(hwnd, taskbar_hwnd)
            self._owned_window = (hwnd, previous)
            # 改 owner 本身不会立即重排已有窗口；仅首次绑定时建立初始层级。
            if not self.api.SetWindowPos(hwnd, wt.HWND(-1), 0, 0, 0, 0,
                                         0x0001 | 0x0002 | 0x0010 | 0x0200):
                raise ctypes.WinError(ctypes.get_last_error())
        self._owned_window = (hwnd, previous)

    def detach(self):
        """切回悬浮窗或退出时恢复 Qt 原有的 owner。"""
        if self._owned_window:
            hwnd, previous = self._owned_window
            if self.is_our_window(hwnd):
                self._set_owner(hwnd, previous if self.api.IsWindow(previous) else 0)
            self._owned_window = None

    def position_matches(self, hwnd: int, rect: Rect):
        return self._rect(hwnd) == rect

    def move(self, hwnd: int, rect: Rect):
        previous = self.api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            # 层级由 owner 关系保证。这里只改变位置/尺寸，不能抢焦点或重排窗口。
            if not self.api.SetWindowPos(hwnd, None, rect.left, rect.top,
                                         rect.width, rect.height, 0x0010 | 0x0200 | 0x0004):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if previous:
                self.api.SetThreadDpiAwarenessContext(previous)


def _setting(section, name, default):
    if os.name != "nt":
        return default
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            "Software\\Microsoft\\Windows\\CurrentVersion\\" + section) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def light_theme():
    return bool(_setting("Themes\\Personalize", "SystemUsesLightTheme", 1))
