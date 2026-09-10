"""离线回归测试：python -m unittest discover -s tests -v。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import autostart

# 模块导入时不创建用户的真实运行目录；每个测试都使用独立的运行目录。
with patch("os.makedirs"):
    import sources
    import widget_tk


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


class Scheduler:
    """手动推进 Tk 主线程回调，避免测试依赖真实桌面或等待 30 秒。"""
    def __init__(self):
        self.pending = {}
        self.serial = 0
        self.destroyed = False
        self.owner = threading.get_ident()

    def after(self, delay, callback, *args):
        assert threading.get_ident() == self.owner
        assert not self.destroyed
        self.serial += 1
        self.pending[self.serial] = (delay, callback, args)
        return self.serial

    def after_cancel(self, timer_id):
        assert threading.get_ident() == self.owner
        self.pending.pop(timer_id, None)

    def tick(self):
        due, self.pending = self.pending, {}
        for _, callback, args in due.values():
            callback(*args)

    def destroy(self):
        self.destroyed = True


class TkTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler()
        root = Mock()
        root.winfo_screenwidth.return_value = 1920
        root.after = self.scheduler.after
        root.after_cancel = self.scheduler.after_cancel
        root.destroy = self.scheduler.destroy
        for target, value in (("tk.Tk", Mock(return_value=root)),
                              ("tk.Canvas", Mock()), ("load_config", Mock(return_value={})),
                              ("QuotaWidget.draw", Mock())):
            self.enterContext(patch("widget_tk." + target, value))
        self.enterContext(patch.object(sources, "backoff_remaining", return_value=0))

    def synchronous_threads(self):
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()
        return patch.object(widget_tk.threading, "Thread", InlineThread)

    def test_manual_refresh_keeps_one_periodic_chain(self):
        with self.synchronous_threads(), patch.object(sources, "read_all", return_value=[]) as read:
            widget = widget_tk.QuotaWidget()
            self.scheduler.tick()
            for _ in range(3):
                widget.refresh()
                self.scheduler.tick()
                self.assertEqual(len(self.scheduler.pending), 2)  # 刷新链 + 纯显示计时器
            self.assertEqual(read.call_count, 4)
            self.scheduler.tick()   # 唯一的自动刷新
            self.scheduler.tick()   # 主线程接收结果
            self.assertEqual(read.call_count, 5)
            self.assertEqual(len(self.scheduler.pending), 2)
            widget.close()
            self.assertFalse(self.scheduler.pending)

    def test_repeated_refresh_during_fetch_starts_only_one_request(self):
        with self.synchronous_threads(), patch.object(sources, "read_all", return_value=[]) as read:
            widget = widget_tk.QuotaWidget()
            for _ in range(5):
                widget.refresh()  # 结果仍未被主线程接收
            read.assert_called_once()
            self.scheduler.tick()
            self.assertEqual(len(self.scheduler.pending), 2)
            widget.close()

    def test_close_during_fetch_never_calls_tk_from_worker(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_fetch = widget_tk.QuotaWidget._fetch

        def slow_read(on_result=None):
            started.set()
            if not release.wait(2):
                raise TimeoutError("Test worker was not released")
            return []

        def observed_fetch(widget):
            try:
                original_fetch(widget)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        with patch.object(sources, "read_all", side_effect=slow_read), \
                patch.object(widget_tk.QuotaWidget, "_fetch", observed_fetch):
            widget = widget_tk.QuotaWidget()
            try:
                self.assertTrue(started.wait(2))
                widget.close()
                self.assertFalse(self.scheduler.pending)
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
            self.assertEqual(errors, [])

    def test_tk_menu_enables_tk_autostart(self):
        with self.synchronous_threads(), patch.object(sources, "read_all", return_value=[]):
            widget = widget_tk.QuotaWidget()
        widget._auto_var = Mock()
        widget._auto_var.get.return_value = True
        with patch.object(autostart, "enable", return_value=(True, "OK")) as enable:
            widget._toggle_autostart()
        enable.assert_called_once_with("tk")

    def test_partial_result_updates_before_completion_and_display_tick_never_queries(self):
        with self.synchronous_threads(), patch.object(sources, "read_all", return_value=[]) as read:
            widget = widget_tk.QuotaWidget()
            self.scheduler.tick()
            widget._fetching = True
            reading = sources.Reading("chatgpt", "wk", 18)
            widget._results.put((False, [reading]))
            widget._poll_result()
            self.assertEqual(widget.readings, [reading])
            self.assertTrue(widget._fetching)
            widget._display_tick()
            read.assert_called_once()
            widget.close()
        widget.close()


class AutostartTests(IsolatedTest):
    def test_source_frontends_choose_their_own_launchers(self):
        with patch.object(sys, "frozen", False, create=True):
            for frontend in ("tk", "qt"):
                target, workdir = autostart.target(frontend)
                self.assertEqual(Path(target).name, "run_%s.vbs" % frontend)
                self.assertEqual(Path(workdir), PROJECT)

    def test_frozen_app_always_targets_itself(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(autostart.target("tk")[0], os.path.abspath(sys.executable))

    def test_missing_launcher_reports_failure_without_creating_shortcut(self):
        with patch.object(autostart, "_here", return_value=str(self.state)), \
                patch.object(sys, "frozen", False, create=True), \
                patch.object(sys, "platform", "win32"), \
                patch.object(autostart, "_make_shortcut") as make:
            ok, message = autostart.enable("tk")
        self.assertFalse(ok)
        self.assertIn("run_tk.vbs", message)
        make.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 not installed")
class QtTests(IsolatedTest):
    def run_qt(self, body):
        script = """
import ctypes, sys, threading, time
if sys.platform == 'win32':
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer
from PySide6.QtWidgets import QApplication
import sources, widget_qt
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
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen", LOCALAPPDATA=str(self.state)),
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
