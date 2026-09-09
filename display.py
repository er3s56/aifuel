# -*- coding: utf-8 -*-
"""显示层：把 Reading 变成「显示什么文字、用什么颜色」。

UI 无关 —— tkinter 和 PySide6 两个窗口都调这里，保证两版行为一致。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from sources import Reading

# 深色悬浮窗配色
BG        = "#14161a"
FG        = "#e6e6e6"
FG_DIM    = "#6b7280"      # 灰：数据不可信/过期时用
TRACK     = "#2a2e35"      # 进度条底槽
GREEN     = "#3fb950"
YELLOW    = "#d29922"
RED       = "#f85149"

BRAND = {"claude": "#d97757", "chatgpt": "#10a37f"}
NAME  = {"claude": "CL", "chatgpt": "GPT"}


@dataclass
class Resolved:
    """一行最终要画的东西。"""
    percent: Optional[float]   # None -> 画 "--"，不画进度条
    color: str                 # 百分比数字和进度条的颜色
    dim: bool                  # True -> 整行压暗，表示数据不可信
    note: str                  # 追加在行尾的小标记，如 "⟳" / "?"；无则空串


def dimmed(hex_color: str, t: float = 0.55) -> str:
    """把颜色朝背景色混，用来表达「数据不新鲜」。

    tkinter 的 Canvas 不支持单个图元设透明度（Qt 那边直接调 alpha 就行），
    所以 tk 版只能手工算混色。t 是混向背景的比例。
    """
    def rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    fr, fg_, fb = rgb(hex_color)
    br, bg_, bb = rgb(BG)
    mix = lambda f, b: int(round(f + (b - f) * t))
    return "#%02x%02x%02x" % (mix(fr, br), mix(fg_, bg_), mix(fb, bb))


def fmt_countdown(secs: Optional[float]) -> str:
    """把剩余秒数格式化成紧凑倒计时。空间很小，最多 5 个字符。"""
    if secs is None:
        return ""
    if secs <= 0:
        return ""            # 窗口已重置，倒计时无意义（该行已用 ↺ 标记）
    if secs >= 86400:
        return "%dd" % round(secs / 86400)
    if secs >= 3600:
        return "%dh" % round(secs / 3600)
    return "%dm" % max(1, round(secs / 60))


def window_expired(r: Reading) -> bool:
    """该读数所属的限额窗口是否已经滚过去了。

    Codex 的数据来自本地会话日志——你上次用 Codex 之后窗口可能早就重置了，
    此时日志里那个 used_percent 已经不代表现在的实际用量。
    """
    return r.resets_at is not None and r.resets_at <= time.time()


def resolve(r: Reading) -> Resolved:
    """决定这一行怎么显示：用什么数字、什么颜色、要不要压暗。

    可用的输入信号：
      r.percent      float|None  —— 读到的百分比
      r.severity     str         —— 服务端给的判断: "normal"/"warning"/"critical"
                                    （只有 Claude 有；Codex 恒为 "normal"）
      r.stale        bool        —— True 表示这是磁盘缓存值，不是本次实时拉取
      r.error        str|None    —— 取数失败的原因
      window_expired(r)  bool    —— 窗口已重置，percent 可能已经不作数

    可用的输出常量：GREEN / YELLOW / RED / FG / FG_DIM
    """
    # 1) 压根没取到数
    if r.error or r.percent is None:
        return Resolved(None, FG_DIM, True, "")

    # 2) 窗口已经滚过去了：日志里那个百分比属于上一个窗口，已知是错的。
    #    宁可画 "--↺" 说「不知道，窗口重置过」，也不要显示一个会骗人的数字。
    if window_expired(r):
        return Resolved(None, FG_DIM, True, "↺")

    # 3) 定颜色：先按百分比分档，再让服务端的 severity 往上抬
    pct = r.percent
    color = GREEN if pct < 60 else (YELLOW if pct < 85 else RED)
    if r.severity == "critical":
        color = RED
    elif r.severity == "warning" and color == GREEN:
        color = YELLOW

    # 4) 缓存值：数字是真的，只是不新鲜 —— 压暗 + 标记，颜色保留
    return Resolved(pct, color, r.stale, "⟳" if r.stale else "")
