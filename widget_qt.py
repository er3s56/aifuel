# -*- coding: utf-8 -*-
"""置顶额度悬浮窗 —— PySide6 版（圆角、半透明、抗锯齿）。

和 tk 版共用 sources.py / display.py，行为一致，只是画得好看些。
左键拖动，右键菜单。行数随服务端返回的限额条数自适应。
"""
from __future__ import annotations

import json
import os
import sys
import time

from PySide6.QtCore import QPoint, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QWidget

import autostart
import display
import sources
import single_instance
import taskbar_view
from taskbar import WindowsTaskbar, light_theme

CONFIG = os.path.join(sources.data_dir(), "config_qt.json")

# 绝对时间（"周二 01:59"）比倒计时占列宽，窗口相应加宽。
W = 300
PAD_X, ROW_H, TOP = 11, 22, 8
X_LABEL, X_PCT = 42, 150
# 进度条右边界要给 "09-15 01:59" 留够宽度（约 70px）。条是装饰、时间是信息，
# 宽度不够时先牺牲条。
BAR_X0, BAR_X1, BAR_H = 158, 204, 6
X_RESET_END = W - PAD_X
MIN_ROWS = 4                    # 高度基准，实际行数少于它时也不至于窄成一条
# 正常轮询间隔。失败后的独立退避由数据层控制，手动刷新也不能绕过。
REFRESH_SEC = 30
RADIUS = 10


def _qc(hexstr: str, alpha: int = 255) -> QColor:
    c = QColor(hexstr)
    c.setAlpha(alpha)
    return c


class Fetcher(QThread):
    """在子线程里跑网络请求，用信号把结果送回主线程。"""
    done = Signal(list)
    updated = Signal(list)

    def run(self) -> None:
        try:
            self.done.emit(sources.read_all(on_result=self.updated.emit))
        except Exception:
            self.done.emit([])


class QuotaWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = self._load_cfg()
        self.readings: list = []
        self.loading = True
        self._drag_pos = None
        self._taskbar = None
        self._docked = False
        self._taskbar_note = ""
        self._taskbar_columns = taskbar_view.measure([])
        self._taskbar_light = light_theme()
        self._last_taskbar_position = None
        self._display_signature = None
        self._taskbar_pending_since = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("aifuel")
        self.resize(W, TOP * 2 + MIN_ROWS * ROW_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(self.cfg.get("x", screen.right() - W - 24), self.cfg.get("y", 48))
        self.setWindowOpacity(self.cfg.get("alpha", 0.94))

        # 单次触发：每轮取完数在 _apply 里自己排下一次，这样才能按退避调节奏
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.display_timer = QTimer(self)
        self.display_timer.timeout.connect(self._update_display)
        self.display_timer.start(1000)
        self._thread = None
        self._closing = False
        self.reopen_timer = QTimer(self)
        self.reopen_timer.timeout.connect(self._resume_if_requested)
        self.taskbar_timer = QTimer(self)
        self.taskbar_timer.timeout.connect(self._sync_taskbar)
        if self.cfg.get("taskbar") and os.name == "nt":
            self.taskbar_timer.start(250)
        QApplication.instance().aboutToQuit.connect(self._wait_for_fetch)
        self.refresh()

    # ------------------------------------------------ 配置

    def _load_cfg(self) -> dict:
        try:
            with open(CONFIG, encoding="utf-8") as f:
                value = json.load(f)
                return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save_cfg(self) -> None:
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f)
        except Exception:
            pass

    # ------------------------------------------------ 取数

    def refresh(self) -> None:
        if self._closing or self._thread is not None:
            return                                   # 上一轮还没回来，跳过
        self.timer.stop()
        self._thread = Fetcher(self)
        self._thread.updated.connect(self._apply_partial)
        self._thread.done.connect(self._apply)
        self._thread.finished.connect(self._fetch_finished)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _fetch_finished(self) -> None:
        self._thread = None
        if self._closing and not self._resume_if_requested():
            self.close()

    def _resume_if_requested(self):
        if not self._closing or not single_instance.take_reopen_request():
            return False
        self._closing = False
        single_instance.cancel_shutdown()
        self.reopen_timer.stop()
        self.display_timer.start(1000)
        if self.cfg.get("taskbar") and os.name == "nt":
            self.taskbar_timer.start(250)
            self._sync_taskbar()
        else:
            self._restore_floating()
        if self._thread is None:
            self.refresh()
        return True

    def _wait_for_fetch(self) -> None:
        # app.quit / 系统退出也可能绕过 closeEvent。run 不依赖 GUI 事件循环，
        # 等待它完成后才能销毁窗口及其子线程，不能强杀正在执行的网络请求。
        single_instance.begin_shutdown()
        self._closing = True
        self.reopen_timer.stop()
        self.timer.stop()
        self.display_timer.stop()
        self.taskbar_timer.stop()
        self._release_taskbar()
        if self._taskbar is not None:
            self._taskbar.release_space(closing=True)
        if self._thread is not None:
            self._thread.wait()

    def closeEvent(self, event) -> None:
        single_instance.begin_shutdown()
        self._closing = True
        self.timer.stop()
        self.display_timer.stop()
        self.taskbar_timer.stop()
        self._release_taskbar()
        if self._thread is not None:
            if self._taskbar is not None:
                self._taskbar.release_space()
            self.reopen_timer.start(100)
            self.hide()
            event.ignore()             # 取数结束后由 _fetch_finished 再次关闭
        else:
            if self._taskbar is not None:
                self._taskbar.release_space(closing=True)
            event.accept()
            QApplication.quit()

    def _apply_partial(self, data: list) -> None:
        self._apply(display.merge_readings(self.readings, data), schedule=False)

    def _update_display(self) -> None:
        if not self._closing:
            self._taskbar_light = light_theme()
            detail = display.details(self.readings)
            tooltip = (self._taskbar_note + "\n\n" if self._taskbar_note else "") + detail
            if self.toolTip() != tooltip:
                self.setToolTip(tooltip)
            signature = (self.loading, self._docked, self._taskbar_light,
                         [(r.provider, r.label, display.resolve(r), display.status_text(r)) for r in self.readings])
            if signature != self._display_signature:
                self._display_signature = signature
                self.update()

    def _apply(self, data: list, schedule=True) -> None:
        if self._closing:
            return
        if data:
            self.readings = data
        self.loading = False
        self._taskbar_columns = taskbar_view.measure(self.readings)
        # 行数随服务端返回的限额条数变化（多一条 Fable、多一条余额都可能），
        # 所以每轮取完数都要按实际行数重算窗口高度
        if self.taskbar_timer.isActive():
            self._sync_taskbar()
        else:
            self._resize_floating()
        self._update_display()
        if schedule:
            wait = sources.backoff_remaining()
            self.timer.start(int((min(REFRESH_SEC, wait + .1) if wait else REFRESH_SEC) * 1000))

    # ------------------------------------------------ 交互

    def _resize_floating(self):
        want_h = TOP * 2 + max(MIN_ROWS, len(self.readings)) * ROW_H
        if self.width() != W or self.height() != want_h:
            self.resize(W, want_h)

    def _restore_floating(self):
        self._release_taskbar()
        self._ensure_native_window()
        self._docked = False
        self._last_taskbar_position = None
        self._resize_floating()
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(self.cfg.get("x", screen.right() - W - 24), self.cfg.get("y", 48))
        self.setWindowOpacity(self.cfg.get("alpha", 0.94))
        self.show()

    def _release_taskbar(self):
        if self._taskbar is not None:
            try:
                self._taskbar.detach()
            except OSError:
                pass  # Explorer 或窗口已经退出时仍须正常关闭 aifuel。

    def _ensure_native_window(self):
        hwnd = int(self.winId())
        if self._taskbar is not None and not self._taskbar.is_our_window(hwnd):
            # owner 被销毁可能连带销毁原生窗口，而 QWidget 仍保留旧句柄。
            # 重建窗口表面，保留读数、计时器和正在进行的请求。
            self._release_taskbar()
            self.hide()
            self.destroy()
            self.create()
            self._last_taskbar_position = None
            hwnd = int(self.winId())
        return hwnd

    def _set_taskbar(self, enabled):
        self.cfg["taskbar"] = bool(enabled)
        self._save_cfg()
        self._drag_pos = None
        if enabled:
            self.taskbar_timer.start(250)
            self._sync_taskbar()
        else:
            self.taskbar_timer.stop()
            if self._taskbar is not None:
                self._taskbar.release_space()
            self._taskbar_note = ""
            self._restore_floating()
        self._update_display()

    def _sync_taskbar(self):
        if self._closing:
            return
        width = taskbar_view.width(self._taskbar_columns)
        try:
            if self._taskbar is None:
                self._taskbar = WindowsTaskbar()
            hwnd = self._ensure_native_window()
            self._taskbar.reserve(hwnd, width)
            placement = self._taskbar.placement(width)
            self._taskbar_note = placement.reason
            if placement.state == "pending":
                if self._taskbar_pending_since is None:
                    self._taskbar_pending_since = time.monotonic()
                if time.monotonic() - self._taskbar_pending_since < 5:
                    self.hide()
                    self._last_taskbar_position = None
                    return
                self._taskbar_note = "任务栏初始化较慢，暂以悬浮窗显示"
            else:
                self._taskbar_pending_since = None
            if placement.state == "hidden":
                if self.isVisible():
                    self.hide()
                self._last_taskbar_position = None
                return
            if placement.state == "docked":
                changed_mode = not self._docked
                self._docked = True
                if changed_mode:
                    self.setWindowOpacity(1.0)
                position = (placement.rect, placement.taskbar_hwnd)
                if (position != self._last_taskbar_position or not self.isVisible()
                        or not self._taskbar.position_matches(hwnd, placement.rect)):
                    if not self.isVisible():
                        self.show()
                    # show() applies Qt's cached floating geometry. Position the
                    # current native window afterwards, then bind its owner.
                    hwnd = int(self.winId())
                    self._taskbar.move(hwnd, placement.rect)
                    self._taskbar.attach(hwnd, placement.taskbar_hwnd)
                    self._last_taskbar_position = position
                if changed_mode:
                    self.update()
                return
        except OSError:
            self._taskbar_note = "任务栏定位失败，暂以悬浮窗显示"
        if self._docked or not self.isVisible():
            self._restore_floating()
        else:
            self._resize_floating()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and not self._docked:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e) -> None:
        if self._drag_pos is not None:
            self.cfg["x"], self.cfg["y"] = self.x(), self.y()
            self._save_cfg()
        self._drag_pos = None

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.refresh()

    def contextMenuEvent(self, e) -> None:
        if self._closing:
            return
        m = QMenu(self)
        act = m.addAction("立即刷新")
        act.triggered.connect(self.refresh)
        detail_action = m.addAction("额度详情（百分比为已用）")
        detail_action.triggered.connect(lambda: QMessageBox.information(
            self, "额度详情", display.details(self.readings)))
        taskbar_action = m.addAction("显示在任务栏（托盘左侧）")
        taskbar_action.setCheckable(True)
        taskbar_action.setChecked(bool(self.cfg.get("taskbar")))
        taskbar_action.setEnabled(os.name == "nt")
        taskbar_action.triggered.connect(self._set_taskbar)
        sub = m.addMenu("不透明度")
        sub.setEnabled(not self._docked)
        for a in (1.0, 0.94, 0.8, 0.6):
            act = sub.addAction("%d%%" % (a * 100))
            act.triggered.connect(lambda _c=False, v=a: self._set_alpha(v))

        act = m.addAction("开机自启")
        act.setCheckable(True)
        act.setChecked(autostart.is_enabled())      # 状态直接读快捷方式是否存在
        act.triggered.connect(self._toggle_autostart)

        m.addSeparator()
        act = m.addAction("退出")
        act.triggered.connect(self.close)
        try:
            m.exec(self._menu_position(m, e.globalPos()))
        finally:
            # Menu actions belong to the menu; release the whole popup after it
            # finishes dispatching callbacks (including mode changes and exit).
            m.deleteLater()

    def _menu_position(self, menu, cursor):
        if not self._docked:
            return cursor
        menu.ensurePolished()
        size = menu.sizeHint()
        area = self.screen().availableGeometry()
        panel = self.frameGeometry()
        # Bottom taskbar: open above it. A top taskbar opens below instead.
        y = panel.top() - size.height() - 4
        if y < area.top():
            y = panel.bottom() + 5
        x = min(cursor.x(), area.right() - size.width() + 1)
        return QPoint(max(area.left(), x), max(area.top(), min(y, area.bottom() - size.height() + 1)))

    def _toggle_autostart(self, checked: bool) -> None:
        ok, msg = autostart.enable() if checked else autostart.disable()
        if not ok:
            # 失败必须告诉用户 —— 静默失败会让人以为设好了，开机才发现没有
            QMessageBox.warning(self, "开机自启", msg)

    def _set_alpha(self, v: float) -> None:
        self.cfg["alpha"] = v
        self._save_cfg()
        self.setWindowOpacity(v)

    # ------------------------------------------------ 绘制

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._docked:
            taskbar_view.paint(p, QRectF(self.rect()), self.readings, self._taskbar_columns,
                               self._taskbar_light, self.loading)
            return

        # 一律用窗口的「逻辑」尺寸算布局。在 125%/150% DPI 缩放下，
        # resize() 拿到的物理像素和绘制用的逻辑坐标不是一回事，
        # 硬编码常量会让最后一行溢出窗口被裁掉。
        w, h = float(self.width()), float(self.height())
        sx = w / W
        pad, x_label, x_pct = PAD_X * sx, X_LABEL * sx, X_PCT * sx
        bar0, bar1 = BAR_X0 * sx, BAR_X1 * sx
        x_reset_end = X_RESET_END * sx
        rows = max(MIN_ROWS, len(self.readings))
        top = h * TOP / (TOP * 2 + rows * ROW_H)
        row_h = (h - 2 * top) / rows
        bar_h = max(4.0, BAR_H * sx)

        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), RADIUS, RADIUS)
        p.fillPath(path, _qc(display.BG, 236))
        p.setPen(_qc("#2a2e35"))
        p.drawPath(path)

        if self.loading:
            p.setPen(_qc(display.FG_DIM))
            p.setFont(QFont("Segoe UI", 9))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "读取中…")
            return

        mono_b = QFont("Consolas", 9, QFont.Bold)
        brand_f = QFont("Segoe UI Semibold", 8)
        small = QFont("Consolas", 8)
        small_cjk = QFont("Microsoft YaHei", 7)     # 绝对时间里有"周二"这样的汉字

        prev_provider = None
        for i, r in enumerate(self.readings):
            res = display.resolve(r)
            y = top + i * row_h
            cy = y + row_h / 2

            # 品牌名只在该服务的第一行标一次
            if r.provider != prev_provider:
                p.setPen(_qc(display.BRAND.get(r.provider, display.FG)))
                p.setFont(brand_f)
                p.drawText(QRectF(pad, y, x_label - pad, row_h),
                           Qt.AlignVCenter | Qt.AlignLeft,
                           display.NAME.get(r.provider, r.provider[:3].upper()))
                prev_provider = r.provider

            p.setPen(_qc(display.FG_DIM))
            p.setFont(small_cjk if any(ord(c) > 127 for c in r.label) else small)
            p.drawText(QRectF(x_label, y, (x_pct - x_label) * 0.55, row_h),
                       Qt.AlignVCenter | Qt.AlignLeft, r.label)

            p.setPen(_qc(res.color, 150 if res.dim else 255))
            p.setFont(mono_b)
            if res.text is not None:
                val = res.text
            else:
                val = "--" if res.percent is None else "%d%%" % round(res.percent)
            p.drawText(QRectF(x_label + (x_pct - x_label) * 0.5, y,
                              (x_pct - x_label) * 0.5, row_h),
                       Qt.AlignVCenter | Qt.AlignRight, val + res.note)

            # 只有百分比类的行才画进度条
            if r.text is None:
                track = QPainterPath()
                track.addRoundedRect(QRectF(bar0, cy - bar_h / 2, bar1 - bar0, bar_h),
                                     bar_h / 2, bar_h / 2)
                p.fillPath(track, _qc(display.TRACK))
                if res.percent is not None:
                    fw = (bar1 - bar0) * min(100.0, max(0.0, res.percent)) / 100.0
                    if fw >= 2:
                        fill = QPainterPath()
                        fill.addRoundedRect(QRectF(bar0, cy - bar_h / 2, fw, bar_h),
                                            bar_h / 2, bar_h / 2)
                        p.fillPath(fill, _qc(res.color, 150 if res.dim else 255))

            reset_txt = display.status_text(r)
            if reset_txt:
                p.setPen(_qc(display.FG_DIM))
                p.setFont(small_cjk if any(ord(c) > 127 for c in reset_txt) else small)
                p.drawText(QRectF(bar1, y, x_reset_end - bar1, row_h),
                           Qt.AlignVCenter | Qt.AlignRight, reset_txt)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--diagnose-codex":
        # Match the GUI's Qt initialization and background-thread query path.
        from dataclasses import asdict
        app = QApplication([])
        class DiagnosticWorker(QThread):
            def run(self):
                self.readings = sources.read_codex()
        worker = DiagnosticWorker()
        worker.finished.connect(app.quit)
        worker.start()
        app.exec()
        worker.wait()
        readings = worker.readings
        report = os.path.join(sources.data_dir(), "codex-report.json")
        with open(report, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in readings], f, ensure_ascii=False, indent=2)
        sys.exit(0 if readings and all(not r.stale and not r.error for r in readings) else 1)

    # 诊断入口：不开窗口，直接查/改开机自启。GUI 里点菜单不便于自动化验证，
    # 而"打包后还能不能正常建快捷方式"必须在真实的冻结进程里测过才算数。
    if len(sys.argv) > 1 and sys.argv[1] == "--autostart":
        arg = sys.argv[2] if len(sys.argv) > 2 else "status"
        tgt, wd = autostart.target()
        lines = ["frozen   : %s" % getattr(sys, "frozen", False),
                 "target   : %s (存在: %s)" % (tgt, os.path.exists(tgt)),
                 "workdir  : %s" % wd,
                 "link     : %s" % autostart.link_path(),
                 "enabled  : %s" % autostart.is_enabled()]
        if arg in ("on", "off"):
            ok, msg = autostart.enable() if arg == "on" else autostart.disable()
            lines += ["%s -> %s: %s" % (arg, "OK" if ok else "失败", msg),
                      "enabled  : %s" % autostart.is_enabled()]
        report = "\n".join(lines)
        # --windowed 打包后 sys.stdout 是 None，print 会抛异常，所以写文件为主
        try:
            with open(os.path.join(sources.data_dir(), "autostart-report.txt"),
                      "w", encoding="utf-8") as f:
                f.write(report + "\n")
        except Exception:
            pass
        if sys.stdout is not None:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(report)
        sys.exit(0)

    if not sources.acquire_single_instance():
        sys.exit(0)                 # 已经有一份在跑，静默退出
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    w = QuotaWidget()
    if "--taskbar" in sys.argv:
        w._set_taskbar(True)
    elif "--floating" in sys.argv:
        w._set_taskbar(False)
    elif w.taskbar_timer.isActive():
        w._sync_taskbar()
    else:
        w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
