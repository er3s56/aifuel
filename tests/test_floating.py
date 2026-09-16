"""Floating/tray regressions. Qt events stay inside isolated test processes."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import autostart
import legacy_cleanup
from window_position import visible_position


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 not installed")
class FloatingTests(unittest.TestCase):
    def run_qt(self, body):
        with tempfile.TemporaryDirectory(prefix="aifuel-tray-test-") as temp:
            script = '''
import os, sys, time, threading, subprocess
from unittest.mock import patch
from PySide6.QtCore import QEvent, QTimer, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMessageBox
import sources, widget_qt, single_instance
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
sources.read_all = lambda **kw: [sources.Reading('chatgpt', 'wk', 25)]
def drain(predicate):
    until = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < until, 'Qt event timeout'
        app.processEvents()
        time.sleep(.001)
    app.processEvents()
w = widget_qt.QuotaWidget()
drain(lambda: w._thread is None)
w.timer.stop()
w.show()
''' + body + '\nw.close()\n'
            result = subprocess.run([sys.executable, '-c', script],
                cwd=Path(__file__).resolve().parents[1],
                env=dict(os.environ, LOCALAPPDATA=temp, APPDATA=temp, QT_QPA_PLATFORM='offscreen'),
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tray_toggle_hidden_updates_missing_tray_and_menu_state(self):
        self.run_qt('''
with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True):
    w._tray_activated(QSystemTrayIcon.Context)
    assert w.isVisible()
    w._tray_activated(QSystemTrayIcon.Trigger)
    assert not w.isVisible()
    w._apply([sources.Reading('chatgpt', 'wk', 35)], schedule=False)
    app.processEvents()
    assert not w.isVisible(), 'An update flashed a hidden window'
    w._tray_menu.aboutToShow.emit()
    show = w._tray_menu.actions()[0]
    assert show.text() == '显示悬浮窗' and show.isEnabled()
    show.trigger()
    assert w.isVisible() and w.readings[0].percent == 35
    w._toggle_visibility()
with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=False):
    w._update_display()
    assert w.isVisible(), 'Tray loss stranded the hidden window'
    w._toggle_visibility()
    assert w.isVisible(), 'Hiding without a tray loses the recovery entry'
    w._tray_menu.aboutToShow.emit()
    assert not w._tray_menu.actions()[0].isEnabled()
''')

    def test_lock_drag_details_refresh_and_persistence(self):
        self.run_qt('''
import json
def mouse(kind, button=Qt.LeftButton, x=120):
    app.sendEvent(w, QMouseEvent(kind, QPointF(10, 10), QPointF(x, 120), button, button, Qt.NoModifier))
locked = next(a for a in w._tray_menu.actions() if a.text() == '锁定位置')
locked.trigger()
assert w.cfg['locked'] and json.load(open(widget_qt.CONFIG))['locked']
before = w.pos()
mouse(QEvent.MouseButtonPress)
mouse(QEvent.MouseMove, x=160)
mouse(QEvent.MouseButtonRelease)
assert w.pos() == before and w._drag_pos is None
locked.trigger()
mouse(QEvent.MouseButtonPress)
mouse(QEvent.MouseMove, x=160)
mouse(QEvent.MouseButtonRelease)
assert w.x() == before.x() + 40 and w.cfg['x'] == w.x()
with patch.object(w, 'refresh') as refresh:
    for button in (Qt.RightButton, Qt.MiddleButton, Qt.LeftButton):
        mouse(QEvent.MouseButtonDblClick, button)
    refresh.assert_called_once()
with patch.object(QMessageBox, 'information') as dialog:
    next(a for a in w._tray_menu.actions() if a.text().startswith('额度详情')).trigger()
    assert '25%' in dialog.call_args.args[2]
w._set_alpha(.8)
assert abs(w.windowOpacity() - .8) < .01
''')

    def test_opacity_menu_uses_opaque_content_with_transparent_rounded_corners(self):
        self.run_qt('''
opacity = next(a.menu() for a in w._tray_menu.actions() if a.text() == '不透明度')
for action in opacity.actions():
    action.trigger()
    expected = int(action.text().rstrip('%')) / 100
    assert abs(w.windowOpacity() - expected) < 1 / 255
    for loading in (True, False):
        w.loading = loading
        image = w.grab().toImage()
        scale = image.devicePixelRatio()
        # Interior pixels must fully cover the desktop before the selected
        # window opacity is applied, including beneath text and progress bars.
        inset = int((widget_qt.RADIUS + 1) * scale)
        for y in range(inset, image.height() - inset, 7):
            for x in range(inset, image.width() - inset, 7):
                assert image.pixelColor(x, y).alpha() == 255, (action.text(), loading, x, y)
        assert image.pixelColor(0, 0).alpha() == 0, 'Rounded corners became a solid rectangle'
''')

    def test_layout_resize_refresh_and_restart_keep_preferences(self):
        self.run_qt('''
from PySide6.QtCore import QPoint
def resize_to(width, height):
    local = QPointF(w.width() - 2, w.height() - 2)
    start = w.mapToGlobal(local.toPoint())
    finish = start + QPoint(width - w.width(), height - w.height())
    for kind, global_pos in ((QEvent.MouseButtonPress, start), (QEvent.MouseMove, finish),
                             (QEvent.MouseButtonRelease, finish)):
        app.sendEvent(w, QMouseEvent(kind, local, QPointF(global_pos), Qt.LeftButton,
                                    Qt.LeftButton, Qt.NoModifier))
    app.processEvents()
layout = next(a.menu() for a in w._tray_menu.actions() if a.text() == '布局')
layout.actions()[1].trigger()
assert w.layout_mode == 'horizontal' and (w.width(), w.height()) == (600, 48)
resize_to(1200, 28)
assert (w.width(), w.height()) == (1200, 28)
original = list(w.cfg['sizes']['horizontal'])
for failure in ('', 'auth', 'rate_limit', 'dependency'):
    w._apply([sources.Reading('claude', 'Fable', 100, stale=bool(failure),
                             observed_at=time.time(), failure_kind=failure)] * 4, schedule=False)
    assert (w.width(), w.height()) == (1200, 28), 'Status changed panel dimensions'
    assert not w.grab().isNull()
# A new limit must be visible, with user width preserved.
w._apply(w.readings + [sources.Reading('chatgpt', '余额', None, text='$25.00')], schedule=False)
assert w.width() == 1200 and w.height() == 48
assert w.cfg['sizes']['horizontal'] == original
w._apply(w.readings[:4], schedule=False)
assert w.height() == 28
layout.actions()[0].trigger()
resize_to(390, 150)
w._apply(w.readings, schedule=False)
assert (w.width(), w.height()) == (390, 150)
layout.actions()[1].trigger()
assert (w.width(), w.height()) == (1200, 28)
w._set_locked(True)
resize_to(700, 70)
assert (w.width(), w.height()) == (1200, 28)
w._tray_menu.aboutToShow.emit()
assert not layout.isEnabled()
w._set_locked(False)
w.close()
w = widget_qt.QuotaWidget()
drain(lambda: w._thread is None)
w.timer.stop()
w.show()
assert w.layout_mode == 'horizontal' and (w.width(), w.height()) == (1200, 28)
w._reset_size()
assert (w.width(), w.height()) == (600, 48)
w._set_layout('vertical')
assert (w.width(), w.height()) == (390, 150)
''')

    def test_resize_edges_anchor_opposite_corner_and_preserve_taskbar_position(self):
        self.run_qt('''
from PySide6.QtCore import QPoint, QRect
w._set_layout('horizontal')
w.move(100, 100)
anchor = w.geometry().bottomRight()
start = w.pos()
w._resize_start = ('lt', start, w.geometry())
w._resize_to(start - QPoint(50, 30))
assert w.geometry().bottomRight() == anchor
w._remember_size()
w._resize_start = None
w._apply(w.readings, schedule=False)
assert w.geometry().bottomRight() == anchor
class Screen:
    def geometry(self): return QRect(0, 0, 1920, 1080)
    def availableGeometry(self): return QRect(0, 0, 1920, 1032)
with patch.object(QApplication, 'screens', return_value=[Screen()]), \
     patch.object(QApplication, 'primaryScreen', return_value=Screen()):
    w._reset_size()
    w.cfg.update(x=500, y=1032)
    w._position_floating()
    assert (w.x(), w.y()) == (500, 1032)
    w._recover_on_screen()
    assert w.y() == 1032, 'Taskbar placement was forcibly moved into work area'
''')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows activation event')
    def test_duplicate_launch_restores_hidden_window_without_new_fetch(self):
        self.run_qt('''
name = 'aifuel-hidden-test-' + str(os.getpid())
assert single_instance.acquire(name)
with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True):
    w._toggle_visibility()
assert not w.isVisible()
with patch.object(w, 'refresh') as refresh:
    p = subprocess.run([sys.executable, '-c',
        'import single_instance,sys; print(single_instance.acquire(sys.argv[1]))', name],
        capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
    assert p.returncode == 0 and p.stdout.strip() == 'False'
    drain(w.isVisible)
    refresh.assert_not_called()
''')

    def test_legacy_config_and_monitor_change_preserve_visible_unlocked_position(self):
        self.run_qt('''
w.close()
with patch.object(widget_qt.QuotaWidget, '_load_cfg', return_value={'taskbar': True, 'x': 99999, 'y': 99999}):
    w = widget_qt.QuotaWidget()
drain(lambda: w._thread is None)
w.timer.stop()
w.show()
assert 'taskbar' not in w.cfg
assert any(s.availableGeometry().contains(w.frameGeometry()) for s in app.screens())
w.move(99999, 99999)
w._set_locked(True)
w._screen_changed()
drain(lambda: any(s.availableGeometry().contains(w.frameGeometry()) for s in app.screens()))
before = w.pos()
for _ in range(100): w._update_display()
assert w.pos() == before and w.isVisible()
''')


@unittest.skipUnless(sys.platform == 'win32', 'Windows integration')
class MigrationTests(unittest.TestCase):
    def test_startup_preserves_disabled_and_unrelated_links_but_migrates_old_app(self):
        with tempfile.TemporaryDirectory(prefix='aifuel-migration-') as temp:
            with patch.dict(os.environ, APPDATA=temp), patch.object(autostart, '_here', return_value=temp), \
                    patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'executable', str(Path(temp) / 'aifuel.exe')):
                Path(sys.executable).touch()
                self.assertTrue(autostart.migrate_existing()[0])
                self.assertFalse(autostart.is_enabled())
                Path(autostart.startup_dir()).mkdir(parents=True)
                for target in (Path(temp) / 'other.exe', Path(temp) / '旧版' / 'aifuel.exe',
                               Path(temp) / '源码' / 'run_qt.vbs'):
                    target.parent.mkdir(exist_ok=True)
                    target.touch()
                    autostart._make_shortcut(autostart.link_path(), str(target), str(target.parent))
                    before = Path(autostart.link_path()).read_bytes()
                    self.assertTrue(autostart.migrate_existing()[0])
                    if target.name == 'other.exe':
                        self.assertEqual(Path(autostart.link_path()).read_bytes(), before)
                    else:
                        self.assertEqual(autostart._read_shortcut(autostart.link_path()), (sys.executable, ''))

    def test_cleanup_only_stops_exact_private_runtime_and_tolerates_failure(self):
        with tempfile.TemporaryDirectory(prefix='aifuel-retire-') as temp, \
                patch.dict(os.environ, LOCALAPPDATA=temp), \
                patch.object(legacy_cleanup, '_running_executable') as running, \
                patch.object(legacy_cleanup.subprocess, 'run') as stop:
            root = Path(temp) / 'aifuel/taskbar-runtime'
            for path in (None, Path(temp) / 'Windhawk/windhawk.exe', root / 'bad/windhawk.exe',
                         root / '0123456789abcdef/nested/windhawk.exe', root / '../windhawk.exe'):
                running.return_value = path
                legacy_cleanup.stop_private_runtime()
                stop.assert_not_called()
            running.return_value = root / '0123456789abcdef/windhawk.exe'
            legacy_cleanup.stop_private_runtime()
            stop.assert_called_once()
            self.assertEqual(stop.call_args.args[0][1:], ['-exit', '-wait'])
            stop.side_effect = subprocess.TimeoutExpired('windhawk', 10)
            legacy_cleanup.stop_private_runtime()


class WindowPositionTests(unittest.TestCase):
    def test_layout_cells_fit_minimum_size_and_invalid_settings_fall_back(self):
        import panel_layout as layout
        for mode in ('horizontal', 'vertical'):
            for count in (0, 1, 4, 5, 7, 12):
                for width in (300, 599, 600, 900, 1200):
                    height = layout.minimum_height(mode, width, count)
                    rects = layout.cells(mode, width, height, count)
                    self.assertEqual(len(rects), count)
                    for x, y, w, h in rects:
                        self.assertGreaterEqual(w, 300)
                        self.assertGreaterEqual(h, 20)
                        self.assertLessEqual(x + w, width)
                        self.assertLessEqual(y + h, height)
        for sizes in (None, [], {'horizontal': ['bad', 20]}, {'horizontal': [True, 20]},
                      {'horizontal': [900, -1]}, {'horizontal': [9999999, 30]}):
            self.assertEqual(layout.saved_size({'sizes': sizes}, 'horizontal'), (600, 48))

    def test_saved_position_survives_monitor_removal_and_negative_origins(self):
        screens = [(-1920, 0, 1920, 1040), (0, 40, 2560, 1400)]
        self.assertEqual(visible_position(-1800, 100, 300, 104, screens), (-1800, 100))
        self.assertEqual(visible_position(-1800, 100, 300, 104, screens[1:]), (0, 100))
        self.assertEqual(visible_position(9000, 9000, 300, 104, screens), (2260, 1336))
        self.assertEqual(visible_position(20, -500, 300, 104, screens[1:]), (20, 40))
        self.assertEqual(visible_position(500, 500, 300, 104, [(0, 0, 200, 80)]), (0, 0))
