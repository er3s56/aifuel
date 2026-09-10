# -*- coding: utf-8 -*-
"""置顶额度悬浮窗 —— tkinter 版（零依赖）。

左键拖动移动，右键出菜单。窗口位置记在 config.json 里。
"""
from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from tkinter import messagebox

import autostart
import display
import sources

CONFIG = os.path.join(sources.data_dir(), "config_tk.json")

# 绝对时间（"周二 01:59"）比倒计时占列宽，窗口相应加宽。
W = 292
ROW_H, TOP = 20, 8
X_TAG, X_LABEL, X_PCT = 9, 42, 146
# 进度条右边界要给 "09-15 01:59" 留够宽度。条是装饰、时间是信息，先牺牲条。
BAR_X0, BAR_X1, BAR_H = 154, 200, 5
X_RESET = W - 9
MIN_ROWS = 4                      # 高度基准，行数少时也不至于窄成一条
# 30 秒。理由与取舍见 widget_qt.py 里同名常量的注释（退避充当限流器）。
REFRESH_SEC = 30


def load_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


class QuotaWidget:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.root = tk.Tk()
        self.root.overrideredirect(True)                 # 无边框
        self.root.attributes("-topmost", True)           # 置顶
        self.root.attributes("-alpha", self.cfg.get("alpha", 0.92))
        self.root.configure(bg=display.BG)

        x = self.cfg.get("x", self.root.winfo_screenwidth() - W - 24)
        y = self.cfg.get("y", 48)
        h0 = TOP * 2 + MIN_ROWS * ROW_H
        self.root.geometry("%dx%d+%d+%d" % (W, h0, x, y))

        self.canvas = tk.Canvas(self.root, width=W, height=h0, bg=display.BG,
                                highlightthickness=1, highlightbackground="#2a2e35")
        self.canvas.pack()

        self._drag = (0, 0)
        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Double-Button-1>", lambda e: self.refresh())

        self.readings: list = []
        self.loading = True
        self.draw()
        self.refresh()

    # ------------------------------------------------ 拖动 / 菜单

    def _drag_start(self, e):
        self._drag = (e.x, e.y)

    def _drag_move(self, e):
        self.root.geometry("+%d+%d" % (self.root.winfo_x() + e.x - self._drag[0],
                                       self.root.winfo_y() + e.y - self._drag[1]))

    def _drag_end(self, _e):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.cfg)

    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="立即刷新", command=self.refresh)
        sub = tk.Menu(m, tearoff=0)
        for a in (1.0, 0.9, 0.75, 0.6):
            sub.add_command(label="%d%%" % (a * 100), command=lambda v=a: self._alpha(v))
        m.add_cascade(label="不透明度", menu=sub)

        # 状态直接读快捷方式是否存在，不落配置文件
        self._auto_var = tk.BooleanVar(value=autostart.is_enabled())
        m.add_checkbutton(label="开机自启", variable=self._auto_var,
                          command=self._toggle_autostart)

        m.add_separator()
        m.add_command(label="退出", command=self.root.destroy)
        m.tk_popup(e.x_root, e.y_root)

    def _toggle_autostart(self) -> None:
        want = self._auto_var.get()
        ok, msg = autostart.enable() if want else autostart.disable()
        if not ok:
            # 失败必须告诉用户 —— 静默失败会让人以为设好了，开机才发现没有
            messagebox.showwarning("开机自启", msg, parent=self.root)
            self._auto_var.set(autostart.is_enabled())

    def _alpha(self, v: float) -> None:
        self.cfg["alpha"] = v
        self.root.attributes("-alpha", v)
        save_config(self.cfg)

    # ------------------------------------------------ 取数

    def refresh(self) -> None:
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        try:
            data = sources.read_all()
        except Exception:
            data = []
        # 网络在子线程，回主线程画 —— tkinter 不是线程安全的
        self.root.after(0, self._apply, data)

    def _apply(self, data: list) -> None:
        self.readings, self.loading = data, False
        self.draw()
        # 取 min 的用意见 widget_qt.py 同处注释（当前 30s 间隔下总是取 REFRESH_SEC）
        wait = sources.backoff_remaining()
        delay = min(REFRESH_SEC, wait + 5) if wait else REFRESH_SEC
        self.root.after(int(delay * 1000), self.refresh)

    # ------------------------------------------------ 绘制

    def draw(self) -> None:
        c = self.canvas
        rows = max(MIN_ROWS, len(self.readings))
        h = TOP * 2 + rows * ROW_H
        if int(c["height"]) != h:                    # 行数变了就重排窗口
            c.config(height=h)
            self.root.geometry("%dx%d+%d+%d" % (W, h, self.root.winfo_x(), self.root.winfo_y()))

        c.delete("all")
        if self.loading:
            c.create_text(W // 2, h // 2, text="读取中…", fill=display.FG_DIM,
                          font=("Segoe UI", 9))
            return

        cjk = ("Microsoft YaHei", 7)                 # 绝对时间里有"周二"这样的汉字
        prev_provider = None
        for i, r in enumerate(self.readings):
            y = TOP + i * ROW_H + ROW_H // 2
            res = display.resolve(r)
            # Canvas 没有 per-item alpha，只能手工混色来表达 dim
            color = display.dimmed(res.color) if res.dim else res.color

            if r.provider != prev_provider:          # 每个服务只在首行标名字
                c.create_text(X_TAG, y, anchor="w",
                              text=display.NAME.get(r.provider, r.provider[:3].upper()),
                              fill=display.BRAND.get(r.provider, display.FG),
                              font=("Segoe UI Semibold", 8))
                prev_provider = r.provider

            has_cjk = any(ord(ch) > 127 for ch in r.label)
            c.create_text(X_LABEL, y, text=r.label, anchor="w", fill=display.FG_DIM,
                          font=cjk if has_cjk else ("Consolas", 8))

            if r.text is not None:                   # 非百分比的值（余额等）
                val = r.text
            else:
                val = "--" if res.percent is None else "%d%%" % round(res.percent)
            c.create_text(X_PCT, y, text=val + res.note, anchor="e",
                          fill=color, font=("Consolas", 9, "bold"))

            if r.text is None:                       # 只有百分比类的行才画进度条
                c.create_rectangle(BAR_X0, y - BAR_H // 2, BAR_X1, y + BAR_H // 2 + 1,
                                   fill=display.TRACK, width=0)
                if res.percent is not None:
                    w = (BAR_X1 - BAR_X0) * min(100.0, max(0.0, res.percent)) / 100.0
                    if w >= 1:
                        c.create_rectangle(BAR_X0, y - BAR_H // 2, BAR_X0 + w,
                                           y + BAR_H // 2 + 1, fill=color, width=0)

            reset_txt = display.fmt_reset(r.resets_at)
            if reset_txt:
                c.create_text(X_RESET, y, text=reset_txt, anchor="e", fill=display.FG_DIM,
                              font=cjk if any(ord(ch) > 127 for ch in reset_txt)
                              else ("Consolas", 8))

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    import sys
    if not sources.acquire_single_instance():
        sys.exit(0)                 # 已经有一份在跑，静默退出
    QuotaWidget().run()

