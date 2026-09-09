# -*- coding: utf-8 -*-
"""置顶额度悬浮窗 —— tkinter 版（零依赖）。

左键拖动移动，右键出菜单。窗口位置记在 config.json 里。
"""
from __future__ import annotations

import json
import os
import threading
import tkinter as tk

import display
import sources

CONFIG = os.path.join(sources.data_dir(), "config_tk.json")

W, H = 238, 80
ROW_Y = (13, 31, 51, 69)          # 四行基线：CL-5h, CL-wk, GPT-5h, GPT-wk
X_TAG, X_LABEL, X_PCT = 9, 34, 92
BAR_X0, BAR_X1, BAR_H = 99, 196, 5
X_RESET = W - 7
# 额度百分比变化很慢，而 /api/oauth/usage 有速率限制 —— 60 秒轮询纯属自伤。
REFRESH_SEC = 300


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
        self.root.geometry("%dx%d+%d+%d" % (W, H, x, y))

        self.canvas = tk.Canvas(self.root, width=W, height=H, bg=display.BG,
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
        m.add_separator()
        m.add_command(label="退出", command=self.root.destroy)
        m.tk_popup(e.x_root, e.y_root)

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
        # 退避中就掐着退避到期的点重试，别白等一整个刷新周期
        wait = sources.backoff_remaining()
        delay = min(REFRESH_SEC, wait + 5) if wait else REFRESH_SEC
        self.root.after(int(delay * 1000), self.refresh)

    # ------------------------------------------------ 绘制

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        if self.loading:
            c.create_text(W // 2, H // 2, text="读取中…", fill=display.FG_DIM,
                          font=("Segoe UI", 9))
            return

        order = [("claude", "5h"), ("claude", "week"),
                 ("chatgpt", "5h"), ("chatgpt", "week")]
        index = {(r.provider, r.label): r for r in self.readings}

        for i, key in enumerate(order):
            y = ROW_Y[i]
            r = index.get(key)
            if r is None:
                continue
            res = display.resolve(r)
            # Canvas 没有 per-item alpha，只能手工混色来表达 dim
            color = display.dimmed(res.color) if res.dim else res.color

            if i % 2 == 0:                                  # 每个服务只在首行标名字
                c.create_text(X_TAG, y, text=display.NAME[key[0]], anchor="w",
                              fill=display.BRAND[key[0]], font=("Segoe UI Semibold", 8))
            c.create_text(X_LABEL, y, text="5h" if key[1] == "5h" else "wk",
                          anchor="w", fill=display.FG_DIM, font=("Consolas", 8))

            pct_txt = "--" if res.percent is None else "%d%%" % round(res.percent)
            c.create_text(X_PCT, y, text=pct_txt + res.note, anchor="e",
                          fill=color, font=("Consolas", 9, "bold"))

            c.create_rectangle(BAR_X0, y - BAR_H // 2, BAR_X1, y + BAR_H // 2 + 1,
                               fill=display.TRACK, width=0)
            if res.percent is not None:
                w = (BAR_X1 - BAR_X0) * min(100.0, max(0.0, res.percent)) / 100.0
                if w >= 1:
                    c.create_rectangle(BAR_X0, y - BAR_H // 2, BAR_X0 + w,
                                       y + BAR_H // 2 + 1, fill=color, width=0)

            c.create_text(X_RESET, y, text=display.fmt_countdown(r.reset_in()),
                          anchor="e", fill=display.FG_DIM, font=("Consolas", 8))

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    import sys
    if not sources.acquire_single_instance():
        sys.exit(0)                 # 已经有一份在跑，静默退出
    QuotaWidget().run()

