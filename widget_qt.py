# -*- coding: utf-8 -*-
"""置顶额度悬浮窗 —— PySide6 版（圆角、半透明、抗锯齿）。

和 tk 版共用额度数据与显示规则，额外提供托盘入口和位置锁定。
左键拖动，右键菜单。行数随服务端返回的限额条数自适应。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QActionGroup, QColor, QFont, QPainter, QPainterPath, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QWidget, QSystemTrayIcon, QStyle

import autostart
import display
import sources
import single_instance
import legacy_cleanup
import panel_layout
from window_position import visible_position

CONFIG = os.path.join(sources.data_dir(), "config_qt.json")

W = panel_layout.CELL_WIDTH
PAD_X, BAR_H = 11, 6
RESIZE_MARGIN = 5
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
        self._resize_start = None
        self.layout_mode = "horizontal" if self.cfg.get("layout") == "horizontal" else "vertical"
        self._display_signature = None
        self._hidden_by_user = False
        if "taskbar" in self.cfg:
            self.cfg.pop("taskbar")
            self._save_cfg()

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("aifuel")
        self.setMouseTracking(True)
        self._resize_floating(recover=False)

        self._position_floating()
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
        self.reopen_timer.start(250)
        icon = QIcon(str(Path(__file__).resolve().parent / "aifuel.ico"))
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("AI Fuel — 单击显示或隐藏额度悬浮窗")
        self._tray_menu = self._create_menu()
        self._tray.setContextMenu(self._tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()
        app = QApplication.instance()
        app.screenAdded.connect(self._screen_added)
        app.screenRemoved.connect(self._screen_changed)
        for screen in app.screens():
            self._screen_added(screen)
        app.aboutToQuit.connect(self._wait_for_fetch)
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
        if not single_instance.take_reopen_request():
            return False
        was_closing = self._closing
        self._closing = False
        single_instance.cancel_shutdown()
        self.display_timer.start(1000)
        self._tray.show()
        self._show_window()
        if was_closing and self._thread is None:
            self.refresh()
        return True

    def _wait_for_fetch(self) -> None:
        single_instance.begin_shutdown()
        self._closing = True
        self.reopen_timer.stop()
        self.timer.stop()
        self.display_timer.stop()
        self._tray.hide()
        if self._thread is not None:
            self._thread.wait()

    def closeEvent(self, event) -> None:
        single_instance.begin_shutdown()
        self._closing = True
        self.timer.stop()
        self.display_timer.stop()
        self._tray.hide()
        if self._thread is not None:
            self.hide()
            event.ignore()  # Keep the worker alive; a new launch can restore us.
        else:
            self.reopen_timer.stop()
            event.accept()
            QApplication.quit()

    def _apply_partial(self, data: list) -> None:
        self._apply(display.merge_readings(self.readings, data), schedule=False)

    def _update_display(self) -> None:
        if self._closing:
            return
        if self._hidden_by_user and not QSystemTrayIcon.isSystemTrayAvailable():
            self._show_window()  # Never leave a hidden app without a recovery entry.
        detail = display.details(self.readings)
        if self.toolTip() != detail:
            self.setToolTip(detail)
        signature = (self.loading,
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
        # 行数随服务端返回的限额条数变化（多一条 Fable、多一条余额都可能），
        # 所以每轮取完数都要按实际行数重算窗口高度
        self._resize_floating()
        self._update_display()
        if schedule:
            wait = sources.backoff_remaining()
            self.timer.start(int((min(REFRESH_SEC, wait + .1) if wait else REFRESH_SEC) * 1000))

    # ------------------------------------------------ 交互

    def _resize_floating(self, recover=True):
        if self._resize_start is not None:
            return
        width, height = panel_layout.saved_size(self.cfg, self.layout_mode)
        minimum = panel_layout.minimum_height(self.layout_mode, width, len(self.readings))
        self.setMinimumSize(W, minimum)
        if self.width() != width or self.height() != max(height, minimum):
            self.resize(width, max(height, minimum))
            if recover:
                self._recover_on_screen()

    def _remember_size(self):
        if not isinstance(self.cfg.get("sizes"), dict):
            self.cfg["sizes"] = {}
        self.cfg["sizes"][self.layout_mode] = [self.width(), self.height()]
        self.cfg.update(x=self.x(), y=self.y())
        self._save_cfg()

    def _set_layout(self, mode):
        if mode == self.layout_mode:
            return
        self._drag_pos = self._resize_start = None
        self.layout_mode = mode
        self.cfg["layout"] = mode
        self._resize_floating()
        self.cfg.update(x=self.x(), y=self.y())
        self._save_cfg()
        self.update()

    def _reset_size(self):
        sizes = self.cfg.get("sizes")
        if isinstance(sizes, dict):
            sizes.pop(self.layout_mode, None)
        self._resize_floating()
        self._save_cfg()
        self.update()

    def _show_window(self):
        self._hidden_by_user = False
        self._recover_on_screen()
        self.show()
        self.raise_()

    def _can_hide(self):
        return self._tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable()

    def _toggle_visibility(self):
        if self._closing:
            return
        if self.isVisible():
            if self._can_hide():
                self._hidden_by_user = True
                self.hide()
        else:
            self._show_window()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._toggle_visibility()

    def _set_locked(self, locked):
        self.cfg["locked"] = bool(locked)
        self._drag_pos = self._resize_start = None
        self.unsetCursor()
        self._save_cfg()

    def _screen_added(self, screen):
        screen.availableGeometryChanged.connect(self._screen_changed)
        screen.geometryChanged.connect(self._screen_changed)
        self._screen_changed()

    def _screen_changed(self, *_):
        QTimer.singleShot(0, self._recover_on_screen)

    def _recover_on_screen(self):
        # Saved/taskbar-overlapping positions are intentional. Recover only
        # against monitor bounds, not the work area excluding the taskbar.
        areas = [s.geometry() for s in QApplication.screens()]
        if not areas:
            return
        x, y = visible_position(self.x(), self.y(), self.width(), self.height(),
                                [(r.x(), r.y(), r.width(), r.height()) for r in areas])
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
            self.cfg.update(x=x, y=y)
            self._save_cfg()

    def _position_floating(self):
        screens = [s.geometry() for s in QApplication.screens()]
        primary = QApplication.primaryScreen().availableGeometry()
        x, y = visible_position(self.cfg.get("x", primary.right() - self.width() - 24),
                                self.cfg.get("y", primary.top() + 48), self.width(), self.height(),
                                [(r.x(), r.y(), r.width(), r.height()) for r in screens])
        self.move(x, y)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and not self.cfg.get("locked"):
            edges = self._resize_edges(e.position())
            if edges:
                self._resize_start = (edges, e.globalPosition().toPoint(), self.geometry())
            else:
                self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _resize_edges(self, point):
        if self.cfg.get("locked"):
            return ""
        return (("l" if point.x() < RESIZE_MARGIN else "r" if point.x() >= self.width() - RESIZE_MARGIN else "")
                + ("t" if point.y() < RESIZE_MARGIN else "b" if point.y() >= self.height() - RESIZE_MARGIN else ""))

    def _resize_to(self, point):
        edges, start, rect = self._resize_start
        delta = point - start
        width = max(W, rect.width() + (delta.x() if "r" in edges else -delta.x() if "l" in edges else 0))
        minimum = panel_layout.minimum_height(self.layout_mode, width, len(self.readings))
        height = max(minimum, rect.height() + (delta.y() if "b" in edges else -delta.y() if "t" in edges else 0))
        x = rect.right() + 1 - width if "l" in edges else rect.x()
        y = rect.bottom() + 1 - height if "t" in edges else rect.y()
        self.setMinimumSize(W, minimum)
        self.setGeometry(QRect(x, y, width, height))

    def mouseMoveEvent(self, e) -> None:
        if self._resize_start is not None and e.buttons() & Qt.LeftButton:
            self._resize_to(e.globalPosition().toPoint())
        elif self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
        else:
            edges = self._resize_edges(e.position())
            cursor = {"l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
                      "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
                      "lt": Qt.SizeFDiagCursor, "rb": Qt.SizeFDiagCursor,
                      "rt": Qt.SizeBDiagCursor, "lb": Qt.SizeBDiagCursor}
            self.setCursor(cursor.get(edges, Qt.ArrowCursor))

    def leaveEvent(self, event):
        if self._resize_start is None:
            self.unsetCursor()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, e) -> None:
        if self._resize_start is not None:
            self._remember_size()
        if self._drag_pos is not None:
            self.cfg["x"], self.cfg["y"] = self.x(), self.y()
            self._save_cfg()
        self._drag_pos = self._resize_start = None
        self._resize_floating()

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.refresh()

    def contextMenuEvent(self, e) -> None:
        if self._closing:
            return
        menu = self._create_menu()
        try:
            menu.exec(e.globalPos())
        finally:
            menu.deleteLater()

    def _create_menu(self):
        menu = QMenu(self)
        visibility = menu.addAction("隐藏悬浮窗", self._toggle_visibility)
        locked = menu.addAction("锁定位置")
        locked.setCheckable(True)
        locked.triggered.connect(self._set_locked)
        layout_menu = menu.addMenu("布局")
        layout_group = QActionGroup(layout_menu)
        for mode, label in (("vertical", "纵向面板"), ("horizontal", "横向紧凑条")):
            action = layout_menu.addAction(label)
            action.setCheckable(True)
            action.setData(mode)
            layout_group.addAction(action)
            action.triggered.connect(lambda _checked=False, value=mode: self._set_layout(value))
        reset_size = menu.addAction("恢复当前布局默认尺寸", self._reset_size)
        menu.addAction("立即刷新", self.refresh)
        menu.addAction("额度详情（百分比为已用）", lambda: QMessageBox.information(
            self, "额度详情", display.details(self.readings)))
        opacity = menu.addMenu("不透明度")
        for alpha in (1.0, 0.94, 0.8, 0.6):
            action = opacity.addAction("%d%%" % (alpha * 100))
            action.triggered.connect(lambda _checked=False, value=alpha: self._set_alpha(value))
        startup = menu.addAction("开机自启")
        startup.setCheckable(True)
        startup.triggered.connect(self._toggle_autostart)
        menu.addSeparator()
        menu.addAction("退出", self.close)

        def sync_actions():
            visibility.setText("隐藏悬浮窗" if self.isVisible() else "显示悬浮窗")
            visibility.setEnabled(not self.isVisible() or self._can_hide())
            locked.setChecked(bool(self.cfg.get("locked")))
            for action in layout_group.actions():
                action.setChecked(action.data() == self.layout_mode)
            layout_menu.setEnabled(not self.cfg.get("locked"))
            reset_size.setEnabled(not self.cfg.get("locked"))
            startup.setChecked(autostart.is_enabled())

        menu.aboutToShow.connect(sync_actions)
        sync_actions()
        return menu

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
        # All geometry is in logical pixels; fonts keep their point sizes.
        w, h = float(self.width()), float(self.height())

        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), RADIUS, RADIUS)
        # The menu controls window opacity; keep the painted panel opaque so
        # 100% fully covers the desktop. Only the rounded corners stay clear.
        p.fillPath(path, _qc(display.BG))
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
        rects = panel_layout.cells(self.layout_mode, self.width(), self.height(), len(self.readings))
        for r, (x, y, width, row_h) in zip(self.readings, rects):
            # Stable columns reserve room for both reset times and error states.
            # Only the progress track grows when a cell is made wider.
            label_x, pct_x = x + 38, x + 90
            bar0, bar1 = x + 140, x + width - 100
            reset_end = x + width - PAD_X
            cy = y + row_h / 2
            if self.layout_mode == "horizontal" and x > 0:
                p.setPen(_qc("#343940"))
                p.drawLine(int(x), int(y + 3), int(x), int(y + row_h - 3))
            res = display.resolve(r)
            if self.layout_mode == "horizontal" or r.provider != prev_provider:
                p.setPen(_qc(display.BRAND.get(r.provider, display.FG)))
                p.setFont(brand_f)
                p.drawText(QRectF(x + PAD_X, y, 25, row_h),
                           Qt.AlignVCenter | Qt.AlignLeft,
                           display.NAME.get(r.provider, r.provider[:3].upper()))
            prev_provider = r.provider

            p.setPen(_qc(display.FG_DIM))
            p.setFont(small_cjk if any(ord(c) > 127 for c in r.label) else small)
            p.drawText(QRectF(label_x, y, pct_x - label_x, row_h),
                       Qt.AlignVCenter | Qt.AlignLeft, r.label)

            p.setPen(_qc(res.color, 150 if res.dim else 255))
            p.setFont(mono_b)
            if res.text is not None:
                val = res.text
            else:
                val = "--" if res.percent is None else "%d%%" % round(res.percent)
            # Text balances can use the otherwise empty progress-bar space.
            value_end = bar0 - 6 if r.text is None else bar1
            p.drawText(QRectF(pct_x, y, value_end - pct_x, row_h),
                       Qt.AlignVCenter | Qt.AlignRight, val + res.note)

            if r.text is None:
                track = QPainterPath()
                track.addRoundedRect(QRectF(bar0, cy - BAR_H / 2, bar1 - bar0, BAR_H),
                                     BAR_H / 2, BAR_H / 2)
                p.fillPath(track, _qc(display.TRACK))
                if res.percent is not None:
                    fw = (bar1 - bar0) * min(100.0, max(0.0, res.percent)) / 100.0
                    if fw >= 2:
                        fill = QPainterPath()
                        fill.addRoundedRect(QRectF(bar0, cy - BAR_H / 2, fw, BAR_H),
                                            BAR_H / 2, BAR_H / 2)
                        p.fillPath(fill, _qc(res.color, 150 if res.dim else 255))

            reset_txt = display.status_text(r)
            if reset_txt:
                p.setPen(_qc(display.FG_DIM))
                p.setFont(small_cjk if any(ord(c) > 127 for c in reset_txt) else small)
                p.drawText(QRectF(bar1 + 6, y, reset_end - bar1 - 6, row_h),
                           Qt.AlignVCenter | Qt.AlignRight, reset_txt)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--diagnose-codex", "--diagnose-claude"):
        # Match the GUI's Qt initialization and background-thread query path.
        from dataclasses import asdict
        provider = sys.argv[1].removeprefix("--diagnose-")
        app = QApplication([])
        class DiagnosticWorker(QThread):
            def run(self):
                self.readings = sources.read_claude() if provider == "claude" else sources.read_codex()
        worker = DiagnosticWorker()
        worker.finished.connect(app.quit)
        worker.start()
        app.exec()
        worker.wait()
        readings = worker.readings
        report = os.path.join(sources.data_dir(), provider + "-report.json")
        with open(report, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in readings], f, ensure_ascii=False, indent=2)
        sys.exit(0 if readings and all(not r.stale and not r.error for r in readings) else 1)

    # 诊断入口：不开窗口，直接查/改开机自启。GUI 里点菜单不便于自动化验证，
    # 而"打包后还能不能正常建快捷方式"必须在真实的冻结进程里测过才算数。
    if len(sys.argv) > 1 and sys.argv[1] == "--autostart":
        arg = sys.argv[2] if len(sys.argv) > 2 else "status"
        if arg == "migrate" and len(sys.argv) > 3:
            # Installer smoke tests use a separate basename, never an arbitrary path.
            name = sys.argv[3]
            if os.path.basename(name) != name or not name.startswith("aifuel") or not name.endswith(".lnk"):
                sys.exit(1)
            autostart.LINK_NAME = name
        ok = True
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
        elif arg == "migrate":
            ok, msg = autostart.migrate_existing()
            lines.append("migrate -> %s: %s" % ("OK" if ok else "失败", msg))
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
        sys.exit(0 if ok else 1)

    if not sources.acquire_single_instance():
        sys.exit(0)                 # 已经有一份在跑，静默退出
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = QuotaWidget()
    # Old --taskbar/--floating shortcuts remain valid and now show the same panel.
    w.show()
    threading.Thread(target=legacy_cleanup.stop_private_runtime, daemon=True).start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
