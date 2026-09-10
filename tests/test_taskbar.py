"""任务栏坐标、空间不足降级；不修改真实任务栏或发起网络请求。"""
import unittest

from taskbar import Rect, arrange


class TaskbarPlacementTests(unittest.TestCase):
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
