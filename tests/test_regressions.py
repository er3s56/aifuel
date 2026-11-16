"""离线回归测试：python -m unittest discover -s tests -v。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

import autostart

PROJECT = Path(__file__).resolve().parents[1]


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.state = PROJECT / (".test-state-" + uuid.uuid4().hex)
        # Python 3.14 的 TemporaryDirectory 在部分 Windows 沙箱中创建不可访问的 ACL。
        self.state.mkdir(mode=0o777)
        self.addCleanup(self.remove_state)

    def remove_state(self):
        path = self.state.resolve()
        if path.parent != PROJECT or not path.name.startswith(".test-state-"):
            raise RuntimeError("Unexpected test directory")
        shutil.rmtree(path)


class AutostartTests(IsolatedTest):
    @unittest.skipUnless(os.name == "nt", "Windows shortcut COM")
    def test_shortcut_paths_are_data_even_with_smart_quotes_and_code_like_text(self):
        target = self.state / "O\u2018\u2019\u201a\u201bBrien; Write-Output injected; $value.exe"
        target.touch()
        link = self.state / "test.lnk"
        autostart._make_shortcut(str(link), str(target), str(self.state))
        self.assertTrue(link.is_file())
        self.assertEqual(autostart._read_shortcut(str(link)), (str(target), ""))

    def test_source_app_targets_qt_launcher(self):
        with patch.object(sys, "frozen", False, create=True):
            target, workdir = autostart.target()
            self.assertEqual(Path(target).name, "run_qt.vbs")
            self.assertEqual(Path(workdir), PROJECT)

    def test_frozen_app_always_targets_itself(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(autostart.target()[0], os.path.abspath(sys.executable))

    def test_missing_launcher_reports_failure_without_creating_shortcut(self):
        with patch.object(autostart, "_here", return_value=str(self.state)), \
                patch.object(sys, "frozen", False, create=True), \
                patch.object(sys, "platform", "win32"), \
                patch.object(autostart, "_make_shortcut") as make:
            ok, message = autostart.enable()
        self.assertFalse(ok)
        self.assertIn("run_qt.vbs", message)
        make.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 not installed")
class QtTests(IsolatedTest):

    @unittest.skipUnless(sys.platform == 'win32', 'Windows activation event')
    def test_reopen_during_slow_fetch_restores_window_without_waiting(self):
        self.run_qt("""
import subprocess, single_instance, os
name = 'aifuel-qt-reopen-' + str(os.getpid())
assert single_instance.acquire(name)
release = threading.Event()
calls = []
def slow_read(on_result=None):
    calls.append(1)
    release.wait(5)
    return []
sources.read_all = slow_read
w = widget_qt.QuotaWidget()
w.show()
drain_until(lambda: bool(calls))
original_thread = w._thread
w.close()
assert w._closing and not w.isVisible()
launcher = subprocess.Popen([sys.executable, '-c',
    'import single_instance,sys; print(single_instance.acquire(sys.argv[1]))', name],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    creationflags=subprocess.CREATE_NO_WINDOW)
try:
    drain_until(lambda: not w._closing)
    assert w.isVisible() and w._thread is original_thread
    assert not release.is_set() and len(calls) == 1
    assert launcher.communicate(timeout=3)[0].strip() == 'False'
    # A duplicate activation arriving after restoration must not undo a later exit.
    single_instance._kernel().SetEvent(single_instance._reopen)
    w.close()
    assert w._closing and not w._resume_if_requested()
finally:
    release.set()
    if launcher.poll() is None:
        launcher.kill()
        launcher.communicate()
drain_until(lambda: w._thread is None)
w.close()
""")

    def test_offscreen_saved_position_is_recovered_on_start_and_floating_restore(self):
        self.run_qt("""
from unittest.mock import patch
sources.read_all = lambda on_result=None: []
with patch.object(widget_qt.QuotaWidget, '_load_cfg', return_value={'x': 100000, 'y': 100000}):
    w = widget_qt.QuotaWidget()
drain_until(lambda: w._thread is None)
w.timer.stop()
assert any(s.availableGeometry().contains(w.frameGeometry()) for s in app.screens())
w._show_window()
assert any(s.availableGeometry().contains(w.frameGeometry()) for s in app.screens())
w.close()
""")

    def run_qt(self, body, platform="offscreen"):
        script = """
import ctypes, sys, threading, time
if sys.platform == 'win32':
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer
from PySide6.QtWidgets import QApplication
import sources, widget_qt
widget_qt.fullscreen.foreground_fullscreen = lambda: False
app = QApplication([])
def drain_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Qt event loop timed out')
        app.processEvents()
        time.sleep(.001)
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
""" + body
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=PROJECT,
            env=dict(os.environ, QT_QPA_PLATFORM=platform, LOCALAPPDATA=str(self.state)),
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_finished_threads_are_released_and_results_still_arrive(self):
        self.run_qt("""
calls = []
def read(on_result=None):
    calls.append(1)
    return [sources.Reading('chatgpt', '5h', len(calls))]
sources.read_all = read
w = widget_qt.QuotaWidget()
for i in range(20):
    if i:
        w.refresh()
    w.refresh()  # 忙时重复刷新应被忽略
    drain_until(lambda: w._thread is None)
    assert w.readings[0].percent == i + 1
    assert not w.findChildren(QThread), 'Finished QThread objects leaked'
    w.timer.stop()
w.close()
""")

    def test_partial_results_and_expiry_update_while_other_provider_is_waiting(self):
        self.run_qt("""
release = threading.Event()
sampled = time.time()
def read(on_result=None):
    on_result([sources.Reading('chatgpt', 'wk', 18, observed_at=sampled, source='api')])
    assert release.wait(3)
    return [sources.Reading('claude', '5h', None, error='timeout')]
sources.read_all = read
w = widget_qt.QuotaWidget()
try:
    drain_until(lambda: bool(w.readings))
    assert w.readings[0].percent == 18
    assert w._thread is not None and w._thread.isRunning()
    w.readings[0].observed_at -= 301
    drain_until(lambda: '历史读数' in w.toolTip())
    assert '未知' in w.toolTip()
finally:
    release.set()
drain_until(lambda: w._thread is None)
w.close()
""")

    def check_quit_during_fetch(self, close):
        self.run_qt("""
started, finished = threading.Event(), threading.Event()
def slow_read(on_result=None):
    started.set()
    time.sleep(.2)
    finished.set()
    return []
sources.read_all = slow_read
w = widget_qt.QuotaWidget()
assert started.wait(2)
QTimer.singleShot(0, """ + close + """)
app.exec()
assert finished.is_set(), 'Exited before fetch completed'
assert w._thread is None or not w._thread.isRunning()
""")

    def test_menu_close_during_fetch_exits_cleanly(self):
        self.check_quit_during_fetch("w.close")

    def test_context_menus_release_actions_after_repeated_open_and_close(self):
        self.run_qt("""
from PySide6.QtCore import QPoint
from PySide6.QtGui import QAction, QContextMenuEvent
from PySide6.QtWidgets import QMenu
sources.read_all = lambda **kwargs: []
w = widget_qt.QuotaWidget()
drain_until(lambda: w._thread is None)
w.timer.stop()
w.show()
menus = len(w.findChildren(QMenu))
actions = len(w.findChildren(QAction))
for _ in range(12):
    QTimer.singleShot(0, lambda: app.activePopupWidget().close())
    event = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), w.mapToGlobal(QPoint(5, 5)))
    app.sendEvent(w, event)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert len(w.findChildren(QMenu)) == menus, 'Closed context menus leaked'
    assert len(w.findChildren(QAction)) == actions, 'Closed menu actions leaked'
w.close()
""")

    def test_application_quit_during_fetch_exits_cleanly(self):
        self.check_quit_during_fetch("app.quit")


@unittest.skipUnless(sys.platform == "win32" and (PROJECT / "dist/aifuel/aifuel.exe").is_file(),
                     "Windows packaged executable not built")
class PackagedTests(IsolatedTest):
    def test_packaged_runtime_imports_and_exits_without_enabling_autostart(self):
        exe = PROJECT / "dist/aifuel/aifuel.exe"
        result = subprocess.run(
            [str(exe), "--autostart", "status"], cwd=PROJECT,
            env=dict(os.environ, LOCALAPPDATA=str(self.state), APPDATA=str(self.state / "roaming")),
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, repr(result.stderr))
        report = (self.state / "aifuel/autostart-report.txt").read_text(encoding="utf-8")
        self.assertIn("frozen   : True", report)
        self.assertIn(str(exe), report)
        self.assertIn("enabled  : False", report)


if __name__ == "__main__":
    unittest.main()
