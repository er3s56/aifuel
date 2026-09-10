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
    def run_qt(self, body, platform="offscreen"):
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

    def test_application_quit_during_fetch_exits_cleanly(self):
        self.check_quit_during_fetch("app.quit")

    def test_taskbar_keeps_every_reading_and_restores_floating_position(self):
        self.run_qt("""
from taskbar import Rect, Placement
from unittest.mock import patch
items = [sources.Reading('claude', label, 20, observed_at=time.time())
         for label in ['5h', 'wk', 'Fable']]
items += [sources.Reading('chatgpt', 'wk', 30, observed_at=time.time()),
          sources.Reading('chatgpt', '余额', None, text='$12', observed_at=time.time())]
calls = []
def read(on_result=None):
    calls.append(1)
    return items
sources.read_all = read
w = widget_qt.QuotaWidget()
drain_until(lambda: w._thread is None)
w.timer.stop()
w.cfg.update(x=40, y=60)
w.move(40, 60)

class Taskbar:
    state = 'docked'
    moves = 0
    owner = None
    def is_our_window(self, hwnd):
        return True
    def placement(self, width):
        return Placement(self.state, Rect(100, 100, 100 + width, 148), '测试降级', 123)
    def attach(self, hwnd, owner):
        self.owner = owner
    def detach(self):
        self.owner = None
    def move(self, hwnd, rect):
        self.moves += 1
        w.setGeometry(rect.left, rect.top, rect.width, rect.height)
w._taskbar = Taskbar()
w._set_taskbar(True)
assert w._docked and w.width() > 300 and w.height() == 48
assert w.width() == widget_qt.taskbar_view.width(w._taskbar_columns)
assert w._taskbar.owner == 123
with patch.object(w, 'update') as repaint:
    for _ in range(20):
        w._sync_taskbar()
        w._update_display()
    assert w._taskbar.moves == 1, 'Unchanged taskbar must not repeatedly move the window'
    repaint.assert_not_called()
layout = w._taskbar_columns
items[0].percent = 100
items[0].stale = True
assert widget_qt.taskbar_view.measure(items) == layout, 'Percentage changes must not resize columns'
assert w.cfg['x'] == 40 and w.cfg['y'] == 60
with patch.object(widget_qt.display, 'resolve', wraps=widget_qt.display.resolve) as resolve:
    w.grab()
    assert [c.args[0] for c in resolve.call_args_list] == items
w._taskbar.state = 'hidden'
w._sync_taskbar()
assert not w.isVisible()
w._taskbar.state = 'docked'
w._sync_taskbar()
assert w.isVisible() and w._docked
assert w._taskbar.moves == 2, 'Returning from hidden mode must restore placement'
w._taskbar.state = 'fallback'
w._sync_taskbar()
assert not w._docked and w.width() == 300 and w.height() == 126
assert w._taskbar.owner is None
assert w.x() == 40 and w.y() == 60
w._taskbar.state = 'docked'
w._sync_taskbar()
assert w._docked
w._set_taskbar(False)
assert not w.taskbar_timer.isActive() and not w._docked
assert w.x() == 40 and w.y() == 60 and w.width() == 300
assert len(calls) == 1, 'Changing display mode must not query quotas'
w.close()
assert not w.taskbar_timer.isActive()
assert w._taskbar.owner is None
""")

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native window test')
    def test_taskbar_stacking_and_owner_recreation(self):
        # 自建屏幕外窗口模拟任务栏；不点击、移动或重启用户的 Explorer。
        self.run_qt("""
from ctypes import wintypes as wt
from taskbar import WindowsTaskbar, Placement, Rect
calls = []
items = [sources.Reading('chatgpt', 'wk', 30)]
def read(on_result=None):
    calls.append(1)
    return items
sources.read_all = read
w = widget_qt.QuotaWidget()
w.cfg.update(x=-32000, y=-32000)
w.move(-32000, -32000)
w.show()
drain_until(lambda: w._thread is None)
w.timer.stop()

class Taskbar(WindowsTaskbar):
    owner = 0
    def placement(self, width):
        return (Placement('docked', Rect(-32000, -32000, -31600, -31950), '', self.owner)
                if self.owner else Placement('hidden'))
backend = w._taskbar = Taskbar()
api = backend.api
api.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
api.CreateWindowExW.restype = wt.HWND
api.DestroyWindow.argtypes = [wt.HWND]
def create_owner():
    handle = api.CreateWindowExW(8, 'STATIC', 'aifuel-test', 0x90000000,
        -32000, -32000, 400, 50, None, None, None, None)
    assert handle, 'Cannot create native test window'
    return handle
def raise_owner():
    assert api.SetWindowPos(backend.owner, wt.HWND(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)
def above_owner():
    hwnd = int(w.winId())
    previous = api.GetWindow(backend.owner, 3)
    while previous:
        if previous == hwnd:
            return True
        previous = api.GetWindow(previous, 3)
    return False

backend.owner = create_owner()
original_owner = api.GetWindow(int(w.winId()), 4) or 0
try:
    raise_owner()
    assert not above_owner(), 'Control must reproduce the original overlap'
    w._sync_taskbar()
    assert above_owner(), 'Initial attachment must place quota window above taskbar'
    for _ in range(20):
        raise_owner()
        # 在 Qt 计时器补偿之前立即验证，不能只测轮询后的最终层级。
        assert above_owner(), 'Taskbar raise temporarily covered quota window'
    w._restore_floating()
    assert (api.GetWindow(int(w.winId()), 4) or 0) == original_owner
    w._sync_taskbar()
    for hidden_gap in [True, False]:
        assert api.DestroyWindow(backend.owner)
        backend.owner = 0
        app.processEvents()
        if hidden_gap:
            w._sync_taskbar()
            assert not w.isVisible()
        backend.owner = create_owner()
        w._sync_taskbar()
        app.processEvents()
        assert backend.is_our_window(int(w.winId())) and w.isVisible()
        assert w._docked and w.readings == items and not w._closing
        assert api.GetWindow(int(w.winId()), 4) == backend.owner
        raise_owner()
        assert above_owner()
    assert len(calls) == 1, 'Window recovery must preserve quota requests'
finally:
    w.close()
    if backend.owner:
        api.DestroyWindow(backend.owner)
""", platform="windows")


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
