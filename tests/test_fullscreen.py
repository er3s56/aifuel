"""Foreground detection with deterministic Win32 geometry and failure cases."""
import ctypes
from ctypes import wintypes as wt
import os
import unittest
from unittest.mock import Mock, patch

import fullscreen


class FullscreenTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.GetForegroundWindow.return_value = 0x123456789
        self.api.IsWindowVisible.return_value = True
        self.api.IsIconic.return_value = False
        self.pid = os.getpid() + 1
        self.name = "GameWindow"
        self.screen = (-2560, 0, 0, 1440)
        self.origin = (-2560, 0)
        self.size = (2560, 1440)

        def process(window, ptr):
            ctypes.cast(ptr, ctypes.POINTER(wt.DWORD)).contents.value = self.pid
            return 1

        def class_name(window, buffer, size):
            buffer.value = self.name
            return len(self.name)

        def monitor(handle, ptr):
            info = ctypes.cast(ptr, ctypes.POINTER(fullscreen.MonitorInfo)).contents
            info.monitor = wt.RECT(*self.screen)
            info.work = wt.RECT(-2560, 0, 0, 1400)
            return True

        def client(window, ptr):
            rect = ctypes.cast(ptr, ctypes.POINTER(wt.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, *self.size
            return True

        def to_screen(window, ptr):
            point = ctypes.cast(ptr, ctypes.POINTER(wt.POINT)).contents
            point.x += self.origin[0]
            point.y += self.origin[1]
            return True

        self.api.GetWindowThreadProcessId.side_effect = process
        self.api.GetClassNameW.side_effect = class_name
        self.api.GetMonitorInfoW.side_effect = monitor
        self.api.GetClientRect.side_effect = client
        self.api.ClientToScreen.side_effect = to_screen

    def test_fullscreen_secondary_monitor_and_maximized_window(self):
        self.assertTrue(fullscreen._foreground_fullscreen(self.api))
        for origin, size in (((-2560, 0), (2560, 1400)),
                             ((-2560, 30), (2560, 1410)),
                             ((0, 0), (2560, 1440))):
            self.origin, self.size = origin, size
            self.assertFalse(fullscreen._foreground_fullscreen(self.api))

    def test_shell_own_process_minimized_and_invisible_are_excluded(self):
        for name in fullscreen._SHELL_CLASSES:
            self.name = name
            self.assertFalse(fullscreen._foreground_fullscreen(self.api))
        self.name = "GameWindow"
        self.pid = os.getpid()
        self.assertFalse(fullscreen._foreground_fullscreen(self.api))
        self.pid += 1
        self.api.IsIconic.return_value = True
        self.assertFalse(fullscreen._foreground_fullscreen(self.api))
        self.api.IsIconic.return_value = False
        self.api.IsWindowVisible.return_value = False
        self.assertFalse(fullscreen._foreground_fullscreen(self.api))

    def test_missing_focus_failed_api_and_focus_race_are_unknown(self):
        self.api.GetForegroundWindow.return_value = None
        self.assertIsNone(fullscreen._foreground_fullscreen(self.api))
        self.api.GetForegroundWindow.side_effect = [123, 456]
        self.assertIsNone(fullscreen._foreground_fullscreen(self.api))
        self.api.GetForegroundWindow.side_effect = None
        self.api.GetForegroundWindow.return_value = 123
        for name in ("GetWindowThreadProcessId", "GetClassNameW", "GetClientRect",
                     "ClientToScreen", "MonitorFromWindow", "GetMonitorInfoW"):
            with patch.object(self.api, name, return_value=0):
                self.assertIsNone(fullscreen._foreground_fullscreen(self.api), name)

    def test_dpi_context_restored_even_on_failure(self):
        self.api.SetThreadDpiAwarenessContext.return_value = 99
        with patch.object(fullscreen.os, "name", "nt"), \
                patch.object(fullscreen, "_user32", return_value=self.api):
            self.assertTrue(fullscreen.foreground_fullscreen())
            self.assertEqual(self.api.SetThreadDpiAwarenessContext.call_args.args, (99,))
            self.api.GetClientRect.side_effect = OSError("window closed")
            self.assertIsNone(fullscreen.foreground_fullscreen())
            self.assertEqual(self.api.SetThreadDpiAwarenessContext.call_args.args, (99,))


if __name__ == "__main__":
    unittest.main()
