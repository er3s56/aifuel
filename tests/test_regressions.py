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
    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows geometry')
    def test_first_dock_keeps_native_geometry_after_show(self):
        self.run_qt("""
from unittest.mock import Mock
from taskbar import WindowsTaskbar, Placement, Rect
sources.read_all = lambda on_result=None: []
w = widget_qt.QuotaWidget()
drain_until(lambda: w._thread is None)
w.timer.stop()
backend = w._taskbar = WindowsTaskbar()
backend.reserve = Mock()
backend.attach = Mock()
backend.release_space = Mock()
target = Rect(100, 200, 700, 248)
backend.placement = Mock(return_value=Placement('docked', target, '', 123))
w._sync_taskbar()
app.processEvents()
assert backend._rect(int(w.winId())) == target, (backend._rect(int(w.winId())), target)
backend.move = Mock(wraps=backend.move)
w._sync_taskbar()
backend.move.assert_not_called()  # Stable ticks must not move/reorder the window.
w.move(300, 100)  # A later Qt geometry update must not defeat the cached placement.
app.processEvents()
w._sync_taskbar()
assert backend._rect(int(w.winId())) == target
backend.move.assert_called_once()
w.hide()
w._sync_taskbar()
app.processEvents()
assert w.isVisible() and backend._rect(int(w.winId())) == target
w.close()
""", platform="windows")

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

    def test_taskbar_initialization_does_not_flash_floating_window(self):
        self.run_qt("""
from unittest.mock import Mock
from taskbar import Placement, Rect
sources.read_all = lambda on_result=None: []
w = widget_qt.QuotaWidget()
drain_until(lambda: w._thread is None)
w.timer.stop()
backend = w._taskbar = Mock()
backend.is_our_window.return_value = True
backend.placement.return_value = Placement('pending')
w._sync_taskbar()
assert not w.isVisible(), 'Initialization flashed a floating panel'
backend.placement.return_value = Placement('docked', Rect(100, 100, 600, 148), '', 123)
w._sync_taskbar()
assert w.isVisible() and w._docked
w.hide()
backend.placement.return_value = Placement('pending')
w._taskbar_pending_since = time.monotonic() - 6
w._sync_taskbar()
assert w.isVisible() and not w._docked, 'Failed initialization left an invisible app'
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

    def test_context_menu_mode_switches_details_and_mouse_buttons(self):
        self.run_qt("""
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QMouseEvent
from PySide6.QtWidgets import QMessageBox
from unittest.mock import patch
from taskbar import Placement, Rect
sources.read_all = lambda **kwargs: [sources.Reading('chatgpt', 'wk', 25)]
w = widget_qt.QuotaWidget()
drain_until(lambda: w._thread is None)
w.timer.stop()
w.cfg.update(x=40, y=60)
w.move(40, 60)
w.show()

class Taskbar:
    reserved = False
    def reserve(self, hwnd, width): self.reserved = True
    def release_space(self, closing=False): self.reserved = False
    def is_our_window(self, hwnd): return True
    def attach(self, hwnd, owner): pass
    def detach(self): pass
    def position_matches(self, hwnd, rect):
        return (w.x(), w.y(), w.width(), w.height()) == (rect.left, rect.top, rect.width, rect.height)
    def placement(self, width):
        return Placement('docked', Rect(10, 100, 10 + width, 148), '', 123)
    def move(self, hwnd, rect):
        w.setGeometry(rect.left, rect.top, rect.width, rect.height)
w._taskbar = Taskbar()
errors = []
def choose(label, before=None):
    def action():
        menu = app.activePopupWidget()
        try:
            assert menu
            selected = next(a for a in menu.actions() if a.text().startswith(label))
            if before: before(menu)
            menu.close()
            selected.trigger()
        except Exception as error:
            errors.append(error)
            if menu: menu.close()
    QTimer.singleShot(0, action)
    app.sendEvent(w, QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), w.mapToGlobal(QPoint(5, 5))))
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not errors, errors

choose('显示在任务栏')
assert w._docked and w._taskbar.reserved
def check_disabled(menu):
    assert not next(a for a in menu.actions() if a.text() == '不透明度').isEnabled()
choose('显示在任务栏', check_disabled)
assert not w._docked and not w._taskbar.reserved
assert (w.x(), w.y()) == (40, 60)
choose('显示在任务栏')
def dismiss_details():
    dialog = app.activeModalWidget()
    try:
        assert isinstance(dialog, QMessageBox)
        assert '25%' in dialog.text() and w._docked and w._taskbar.reserved
    except Exception as error:
        errors.append(error)
    finally:
        if dialog: dialog.accept()
choose('额度详情', lambda menu: QTimer.singleShot(0, dismiss_details))
assert w._docked and w._taskbar.reserved
with patch.object(w, 'refresh') as refresh:
    for button in (Qt.RightButton, Qt.MiddleButton, Qt.LeftButton):
        app.sendEvent(w, QMouseEvent(QEvent.MouseButtonDblClick, QPointF(5, 5), QPointF(5, 5),
                                    button, button, Qt.NoModifier))
    refresh.assert_called_once()
w.close()
""")

    def test_taskbar_menu_stays_on_screen_and_clear_of_the_panel(self):
        self.run_qt("""
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QMenu
sources.read_all = lambda **kwargs: []
w = widget_qt.QuotaWidget()
drain_until(lambda: w._thread is None)
w.timer.stop()
w.show()
menu = QMenu(w)
for label in ('Refresh', 'Details', 'Taskbar', 'Opacity', 'Autostart', 'Exit'):
    menu.addAction(label)
area = w.screen().availableGeometry()
w._docked = True
for top in (False, True):
    y = area.top() if top else area.bottom() - 47
    w.setGeometry(area.right() - 299, y, 300, 48)
    position = w._menu_position(menu, QPoint(area.right(), y + 24))
    popup = QRect(position, menu.sizeHint())
    assert area.contains(popup), 'Menu must fit the available screen'
    assert not popup.intersects(w.frameGeometry()), 'Menu covers taskbar quota text'
w._docked = False
cursor = QPoint(100, 200)
assert w._menu_position(menu, cursor) == cursor
w.close()
""")

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
    def reserve(self, hwnd, width):
        self.reserved = True
    def release_space(self, closing=False):
        self.reserved = False
    def is_our_window(self, hwnd):
        return True
    def placement(self, width):
        return Placement(self.state, Rect(100, 100, 100 + width, 148), '测试降级', 123)
    def attach(self, hwnd, owner):
        self.owner = owner
    def detach(self):
        self.owner = None
    def position_matches(self, hwnd, rect):
        return (w.x(), w.y(), w.width(), w.height()) == (rect.left, rect.top, rect.width, rect.height)
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
assert not w._taskbar.reserved
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
    def reserve(self, hwnd, width):
        pass  # This test owns a synthetic taskbar, not the real Explorer taskbar.
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
