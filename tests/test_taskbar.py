"""任务栏坐标、空间不足降级；不修改真实任务栏或发起网络请求。"""
import unittest
from unittest.mock import Mock, patch

from taskbar import Rect, WindowsTaskbar, arrange
from taskbar_widgets import _button_rects


class TaskbarPlacementTests(unittest.TestCase):
    def test_windows_11_buttons_extending_past_legacy_container(self):
        # 实机 150% DPI：旧容器到 2484，实际图标到 2815；额度窗左侧为 2712。
        bar, tray = Rect(0, 2088, 3840, 2160), Rect(3325, 2088, 3840, 2160)
        monitor = Rect(0, 0, 3840, 2160)
        self.assertEqual(arrange(bar, tray, monitor, 2484, 604, 144).state, "docked")
        buttons = tuple(Rect(x, 2088, x + 66, 2160) for x in range(1429, 2815, 66))
        self.assertEqual(arrange(bar, tray, monitor, 2484, 604, 144, buttons).state,
                         "fallback")
        # 关闭几个任务后重新出现足够空白，应自动恢复任务栏显示。
        self.assertEqual(arrange(bar, tray, monitor, 2484, 604, 144, buttons[:-3]).state,
                         "docked")

    def test_buttons_keep_click_margin_even_without_direct_overlap(self):
        bar, tray = Rect(0, 1032, 1920, 1080), Rect(1660, 1032, 1920, 1080)
        # 额度窗原本从 1054 开始，按钮虽止于 1052，但仍需保留 6px 间距。
        result = arrange(bar, tray, Rect(0, 0, 1920, 1080), 900, 600, 96,
                         (Rect(990, 1032, 1052, 1080), tray))
        self.assertEqual(result.state, "fallback")

    def test_weather_and_other_reserved_areas_remain_clickable(self):
        weather = Rect(2646, 1940, 2836, 2000)
        result = arrange(Rect(0, 1940, 3200, 2000), Rect(2851, 1940, 3200, 2000),
                         Rect(0, 0, 3200, 2000), 1009, 520, 120, (weather,))
        self.assertEqual(result.state, 'docked')
        self.assertEqual(result.rect.right, weather.left - 8)
        # 控件位于左侧时不必在右侧额外留白。
        left_widget = Rect(0, 1940, 190, 2000)
        result = arrange(Rect(0, 1940, 3200, 2000), Rect(2851, 1940, 3200, 2000),
                         Rect(0, 0, 3200, 2000), 1009, 520, 120, (left_widget,))
        self.assertEqual(result.rect.right, 2843)

    def test_weather_leaves_too_little_room_so_all_readings_fall_back(self):
        result = arrange(Rect(0, 1032, 1920, 1080), Rect(1660, 1032, 1920, 1080),
                         Rect(0, 0, 1920, 1080), 950, 600, 96,
                         (Rect(1470, 1032, 1660, 1080),))
        self.assertEqual(result.state, 'fallback')

    def test_right_aligned_before_tray_with_high_dpi(self):
        result = arrange(Rect(0, 1940, 3200, 2000), Rect(2851, 1940, 3200, 2000),
                         Rect(0, 0, 3200, 2000), 1009, 1135, 120)
        self.assertEqual(result.state, "docked")
        self.assertEqual(result.rect, Rect(1708, 1942, 2843, 1998))

    def test_negative_monitor_origin_is_preserved(self):
        result = arrange(Rect(-1920, 1032, 0, 1080), Rect(-260, 1032, 0, 1080),
                         Rect(-1920, 0, 0, 1080), -1500, 900)
        self.assertEqual(result.state, "docked")
        self.assertEqual(result.rect.right, -266)
        self.assertEqual(result.rect.width, 900)

    def test_full_taskbar_falls_back_instead_of_covering_icons_or_truncating(self):
        result = arrange(Rect(0, 1032, 1920, 1080), Rect(1660, 1032, 1920, 1080),
                         Rect(0, 0, 1920, 1080), 1100, 900)
        self.assertEqual(result.state, "fallback")
        self.assertIn("空间不足", result.reason)

    def test_auto_hidden_taskbar_hides_overlay(self):
        result = arrange(Rect(0, 1078, 1920, 1126), Rect(1660, 1078, 1920, 1126),
                         Rect(0, 0, 1920, 1080), 700, 900)
        self.assertEqual(result.state, "hidden")

    def test_small_and_vertical_taskbars_preserve_floating_layout(self):
        for bar, tray in [(Rect(0, 1048, 1920, 1080), Rect(1660, 1048, 1920, 1080)),
                          (Rect(0, 0, 48, 1080), Rect(0, 800, 48, 1080))]:
            with self.subTest(bar=bar):
                self.assertEqual(arrange(bar, tray, Rect(0, 0, 1920, 1080), 0, 900).state,
                                 "fallback")

    def test_top_taskbar_and_malformed_tray(self):
        bar, monitor = Rect(0, 0, 1920, 48), Rect(0, 0, 1920, 1080)
        result = arrange(bar, Rect(1660, 0, 1920, 48), monitor, 700, 900)
        self.assertEqual(result.state, "docked")
        self.assertEqual(result.rect.top, 2)
        self.assertEqual(arrange(bar, Rect(1660, 50, 1920, 100), monitor, 700, 900).state,
                         "fallback")


class TaskbarProbeTests(unittest.TestCase):
    def setUp(self):
        self.backend = WindowsTaskbar.__new__(WindowsTaskbar)
        self.backend._reservation = None
        self.backend._buttons_key = self.backend._buttons_result = None
        self.backend._buttons_worker = None
        self.backend._buttons_next = 0
        self.backend._buttons_epoch = 0
        self.now = 10.0
        self.enterContext(patch("taskbar.time.monotonic", side_effect=lambda: self.now))
        self.probe = self.enterContext(patch("taskbar.taskbar_button_rects",
                                            return_value=((100, 1032, 1050, 1080),)))
        self.workers = []

        class DeferredThread:
            def __init__(worker, target, daemon):
                worker.target, worker.alive = target, False
                self.workers.append(worker)

            def start(worker):
                worker.alive = True

            def is_alive(worker):
                return worker.alive

            def finish(worker):
                worker.target()
                worker.alive = False

        self.enterContext(patch("taskbar.threading.Thread", DeferredThread))
        self.bar = Rect(0, 1032, 1920, 1080)
        self.tray = Rect(1660, 1032, 1920, 1080)

    def bounds(self, hwnd=10, bar=None, tray=None, dpi=96, occupied=900):
        return self.backend._button_bounds(hwnd, bar or self.bar, tray or self.tray,
                                           dpi, occupied)

    def test_initial_query_and_growth_then_recovery(self):
        self.assertIsNone(self.bounds())
        self.assertFalse(self.probe.called, "COM must run in the background")
        self.workers[-1].finish()
        initial = self.bounds()
        self.assertEqual(initial, (Rect(100, 1032, 1050, 1080),))
        self.now += .5
        self.probe.return_value = ((100, 1032, 1400, 1080),)
        self.assertEqual(self.bounds(), initial)
        self.workers[-1].finish()
        self.assertEqual(self.bounds(), (Rect(100, 1032, 1400, 1080),))
        self.now += .5
        self.probe.return_value = ((100, 1032, 1000, 1080),)
        self.bounds()
        self.workers[-1].finish()
        self.assertEqual(self.bounds(), (Rect(100, 1032, 1000, 1080),))

    def test_slow_probe_expires_cached_space_and_never_overlaps_queries(self):
        self.bounds()
        self.workers[-1].finish()
        self.now += .5
        self.assertIsNotNone(self.bounds())
        self.now += 2
        self.assertIsNone(self.bounds())
        self.assertEqual(len(self.workers), 2)
        self.assertTrue(self.workers[-1].is_alive())
        self.workers[-1].finish()
        self.assertIsNone(self.bounds(), "A slow result must not renew stale free space")
        self.workers[-1].finish()
        self.assertIsNotNone(self.bounds())

    def test_failed_or_incomplete_probe_is_unknown_then_retries(self):
        for value in (OSError("Explorer restarting"), (), ((1660, 1032, 1920, 1080),)):
            with self.subTest(value=value):
                self.now += .5
                self.probe.side_effect = value if isinstance(value, Exception) else None
                self.probe.return_value = value
                self.bounds()
                self.workers[-1].finish()
                self.assertIsNone(self.bounds())
        self.now += .5
        self.probe.return_value = ((100, 1032, 1000, 1080),)
        self.bounds()
        self.workers[-1].finish()
        self.assertIsNotNone(self.bounds())

    def test_old_query_cannot_authorize_new_taskbar_layout(self):
        for change in ({"hwnd": 11}, {"dpi": 144}, {"occupied": 1100},
                       {"bar": Rect(-1920, 1032, 0, 1080)},
                       {"tray": Rect(1600, 1032, 1920, 1080)}):
            with self.subTest(change=change):
                self.now += 2
                self.bounds()
                self.assertIsNone(self.bounds(**change))
                self.workers[-1].finish()  # 旧位置的查询比布局变化晚完成。
                self.assertIsNone(self.bounds(**change))
                self.workers[-1].finish()
                self.assertIsNotNone(self.bounds(**change))

    def test_inflight_scan_cannot_be_reused_after_leaving_taskbar_mode(self):
        self.bounds()
        old_worker = self.workers[-1]
        self.backend.release_space()
        self.assertIsNone(self.bounds())  # Same HWND, geometry and requested width.
        old_worker.finish()
        self.assertIsNone(self.bounds())
        self.workers[-1].finish()
        self.assertIsNotNone(self.bounds())

    def test_placement_requires_confirmed_buttons_and_respects_hidden_bar(self):
        backend = self.backend
        backend.api = Mock()
        backend.api.FindWindowW.return_value = 10
        backend.api.FindWindowExW.return_value = 20
        backend.api.GetForegroundWindow.return_value = 0
        backend.api.GetDpiForWindow.return_value = 96
        backend.api.EnumChildWindows.side_effect = lambda hwnd, cb, _: cb(30, 0)
        backend.callback_type = lambda fn: fn
        backend._class = lambda hwnd: "MSTaskSwWClass"
        backend._rect = lambda hwnd: {10: self.bar, 20: self.tray,
                                     30: Rect(100, 1032, 900, 1080)}[hwnd]

        def monitor_info(handle, pointer):
            pointer._obj.rcMonitor.right = 1920
            pointer._obj.rcMonitor.bottom = 1080
            return True

        backend.api.GetMonitorInfoW.side_effect = monitor_info
        self.assertEqual(backend._placement(600).state, "fallback")
        self.workers[-1].finish()
        self.assertEqual(backend._placement(600).state, "fallback")  # only 4px gap
        self.now += .5
        self.probe.return_value = ((100, 1032, 1000, 1080),)
        backend._placement(600)
        self.workers[-1].finish()
        self.assertEqual(backend._placement(600).state, "docked")
        old_rect = backend._rect
        backend._rect = lambda hwnd: Rect(100, 1032, 1600, 1080) if hwnd == 30 else old_rect(hwnd)
        # 占位扩展让真实按钮缩回后，旧容器仍可能报告很宽的区域。
        backend._placement(600)
        self.workers[-1].finish()
        self.assertEqual(backend._placement(600).state, "fallback")
        backend._reservation = Mock(ready=True, error="")
        self.assertEqual(backend._placement(600).state, "docked")
        backend._buttons_result = None
        backend._buttons_next = float("inf")
        # A popup can delay UIA; the native reservation remains authoritative.
        self.assertEqual(backend._placement(600).state, "docked")
        backend._reservation.ready = False
        self.assertEqual(backend._placement(600).state, "fallback")
        backend._reservation.ready = True
        self.assertEqual(backend._placement(600).state, "docked")
        backend._buttons_result = (backend._buttons_key, self.now, (Rect(0, 1032, 1660, 1080),))
        self.assertEqual(backend._placement(600).state, "fallback")  # still honor known obstacles
        # Changing reservation width invalidates bounds from the old allocation.
        self.assertEqual(backend._placement(620).state, "docked")
        backend.release_space()
        self.assertIsNone(backend._buttons_key)
        self.assertIsNone(backend._buttons_result)
        self.bar, self.tray = Rect(0, 1008, 1920, 1080), Rect(1660, 1008, 1920, 1080)
        backend.api.GetDpiForWindow.return_value = 144
        self.assertEqual(backend._placement(403).rect.width, 605)
        self.bar, self.tray = Rect(0, 1078, 1920, 1126), Rect(1660, 1078, 1920, 1126)
        self.assertEqual(backend._placement(600).state, "hidden")


class ButtonReaderTests(unittest.TestCase):
    def test_offscreen_and_empty_buttons_are_ignored_and_com_elements_released(self):
        released = []

        def method(pointer, index, args, *values):
            if pointer == "array":
                values[-1]._obj.value = 3 if index == 3 else values[0] + 1
            elif index == 2:
                released.append(pointer.value)
            elif index == 38:
                values[0]._obj.value = pointer.value == 2
            elif index == 43:
                rect = values[0]._obj
                rect.left, rect.top, rect.right, rect.bottom = (100, 1032, 166, 1080)
                if pointer.value == 3:
                    rect.right = rect.left

        with patch("taskbar_widgets._method", side_effect=method):
            self.assertEqual(_button_rects("array"), ((100, 1032, 166, 1080),))
        self.assertEqual(released, [1, 2, 3])

    def test_disappearing_button_invalidates_partial_results_and_releases_it(self):
        released = []

        def method(pointer, index, args, *values):
            if pointer == "array":
                values[-1]._obj.value = 1
            elif index == 2:
                released.append(pointer.value)
            else:
                raise OSError("Button disappeared during query")

        with patch("taskbar_widgets._method", side_effect=method):
            with self.assertRaises(OSError):
                _button_rects("array")
        self.assertEqual(released, [1])
