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
    def test_saved_position_survives_monitor_removal_and_negative_origins(self):
        screens = [(-1920, 0, 1920, 1040), (0, 40, 2560, 1400)]
        self.assertEqual(visible_position(-1800, 100, 300, 104, screens), (-1800, 100))
        self.assertEqual(visible_position(-1800, 100, 300, 104, screens[1:]), (0, 100))
        self.assertEqual(visible_position(9000, 9000, 300, 104, screens), (2260, 1336))
        self.assertEqual(visible_position(20, -500, 300, 104, screens[1:]), (20, 40))
        self.assertEqual(visible_position(500, 500, 300, 104, [(0, 0, 200, 80)]), (0, 0))
