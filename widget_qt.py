# -*- coding: utf-8 -*-
"""置顶额度悬浮窗 —— PySide6 版（圆角、半透明、抗锯齿）。

和 tk 版共用 sources.py / display.py，行为一致，只是画得好看些。
左键拖动，右键菜单。行数随服务端返回的限额条数自适应。
"""
from __future__ import annotations

import json
import os
import sys

from PySide6.QtCore import QObject, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QMenu, QWidget

import display
import sources

CONFIG = os.path.join(sources.data_dir(), "config_qt.json")

# 绝对时间（"周二 01:59"）比倒计时占列宽，窗口相应加宽。
W = 300
PAD_X, ROW_H, TOP = 11, 22, 8
X_LABEL, X_PCT = 42, 150
BAR_X0, BAR_X1, BAR_H = 158, 226, 6
X_RESET_END = W - PAD_X
MIN_ROWS = 4                    # 高度基准，实际行数少于它时也不至于窄成一条
# 额度百分比变化很慢，而 /api/oauth/usage 有速率限制 —— 60 秒轮询纯属自伤。
REFRESH_SEC = 300
RADIUS = 10


def _qc(hexstr: str, alpha: int = 255) -> QColor:
    c = QColor(hexstr)
    c.setAlpha(alpha)
    return c


class Fetcher(QObject):
    """在子线程里跑网络请求，用信号把结果送回主线程。"""
    done = Signal(list)

    def run(self) -> None:
        try:
            self.done.emit(sources.read_all())
        except Exception:
            self.done.emit([])


class QuotaWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = self._load_cfg()
        self.readings: list = []
        self.loading = True
        self._drag_pos = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(W, TOP * 2 + MIN_ROWS * ROW_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(self.cfg.get("x", screen.right() - W - 24), self.cfg.get("y", 48))
        self.setWindowOpacity(self.cfg.get("alpha", 0.94))

        # 单次触发：每轮取完数在 _apply 里自己排下一次，这样才能按退避调节奏
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self._thread = None
        self.refresh()

    # ------------------------------------------------ 配置

    def _load_cfg(self) -> dict:
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
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
        if self._thread is not None and self._thread.isRunning():
            return                                   # 上一轮还没回来，跳过
        self._thread = QThread(self)
        self._worker = Fetcher()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._apply)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _apply(self, data: list) -> None:
        if data:
            self.readings = data
        self.loading = False
        # 行数随服务端返回的限额条数变化（多一条 Fable、多一条余额都可能），
        # 所以每轮取完数都要按实际行数重算窗口高度
        rows = max(MIN_ROWS, len(self.readings))
        want_h = TOP * 2 + rows * ROW_H
        if self.height() != want_h:
            self.resize(W, want_h)
        self.update()
        # 退避中就掐着退避到期的点重试，别白等一整个刷新周期
        wait = sources.backoff_remaining()
        self.timer.start(int((min(REFRESH_SEC, wait + 5) if wait else REFRESH_SEC) * 1000))

    # ------------------------------------------------ 交互

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
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
        self.refresh()

    def contextMenuEvent(self, e) -> None:
        m = QMenu(self)
        act = QAction("立即刷新", self)
        act.triggered.connect(self.refresh)
        m.addAction(act)
        sub = m.addMenu("不透明度")
        for a in (1.0, 0.94, 0.8, 0.6):
            act = QAction("%d%%" % (a * 100), self)
            act.triggered.connect(lambda _c=False, v=a: self._set_alpha(v))
            sub.addAction(act)
        m.addSeparator()
        act = QAction("退出", self)
        act.triggered.connect(QApplication.quit)
        m.addAction(act)
        m.exec(e.globalPos())

    def _set_alpha(self, v: float) -> None:
        self.cfg["alpha"] = v
        self._save_cfg()
        self.setWindowOpacity(v)

    # ------------------------------------------------ 绘制

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

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
            if r.text is not None:                  # 非百分比的值（余额等）
                val = r.text
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

            reset_txt = display.fmt_reset(r.resets_at)
            if reset_txt:
                p.setPen(_qc(display.FG_DIM))
                p.setFont(small_cjk if any(ord(c) > 127 for c in reset_txt) else small)
                p.drawText(QRectF(bar1, y, x_reset_end - bar1, row_h),
                           Qt.AlignVCenter | Qt.AlignRight, reset_txt)


def main() -> None:
    if not sources.acquire_single_instance():
        sys.exit(0)                 # 已经有一份在跑，静默退出
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    w = QuotaWidget()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
