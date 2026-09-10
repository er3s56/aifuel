# -*- coding: utf-8 -*-
"""显示层：把 Reading 变成「显示什么文字、用什么颜色」。

UI 无关 —— tkinter 和 PySide6 两个窗口都调这里，保证两版行为一致。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sources import Reading
from quota_policy import CACHE_TTL

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
    text: Optional[str] = None  # 只有通过新鲜度检查的余额才允许前端显示


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


def fmt_reset(resets_at: Optional[float]) -> str:
    """把重置时刻格式化成本地绝对时间。

    倒计时（"5d"）看着紧凑，但没法指导行动 —— 你不知道该不该今晚省着用。
        今天   -> "19:19"
        其他   -> "09-15 02:00"
    """
    if resets_at is None:
        return ""
    now = datetime.now().astimezone()
    try:
        t = datetime.fromtimestamp(resets_at).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    if t <= now:
        return ""                       # 已经重置过，该行已用 ↺ 标记
    if t.date() == now.date():
        return t.strftime("%H:%M")
    return t.strftime("%m-%d %H:%M")


def window_expired(r: Reading) -> bool:
    """额度窗口已重置，之前的账户读数不再代表当前用量。"""
    return r.resets_at is not None and r.resets_at <= time.time()


def sample_expired(r: Reading) -> bool:
    if r.observed_at is None:
        return r.stale
    now = time.time()
    return r.observed_at > now + 1 or now - r.observed_at >= CACHE_TTL


def resolve(r: Reading) -> Resolved:
    """决定这一行怎么显示：用什么数字、什么颜色、要不要压暗。

    可用的输入信号：
      r.percent      float|None  —— 读到的百分比
      r.severity     str         —— 服务端给的判断: "normal"/"warning"/"critical"
                                    （只有 Claude 有；Codex 恒为 "normal"）
      r.stale        bool        —— True 表示这是缓存值，不是本次实时拉取
      r.observed_at  float|None  —— 最后成功采样时间，五分钟后隐藏旧值
      r.error        str|None    —— 取数失败的原因
      window_expired(r)  bool    —— 窗口已重置，percent 可能已经不作数

    可用的输出常量：GREEN / YELLOW / RED / FG / FG_DIM
    """
    # 1) 压根没取到数
    if r.error:
        return Resolved(None, FG_DIM, True, "")
    # 窗口和五分钟有效期独立于请求计时器检查，重试等待不能延长旧值寿命。
    if window_expired(r):
        return Resolved(None, FG_DIM, True, "↺")
    if sample_expired(r):
        return Resolved(None, FG_DIM, True, "")
    if r.text is not None:
        return Resolved(None, FG, r.stale, "⟳" if r.stale else "", r.text)
    if r.percent is None:
        return Resolved(None, FG_DIM, True, "")

    # 3) 定颜色：先按百分比分档，再让服务端的 severity 往上抬
    pct = r.percent
    color = GREEN if pct < 60 else (YELLOW if pct < 85 else RED)
    if r.severity == "critical":
        color = RED
    elif r.severity == "warning" and color == GREEN:
        color = YELLOW

    # 4) 缓存值：数字是真的，只是不新鲜 —— 压暗 + 标记，颜色保留
    return Resolved(pct, color, r.stale, "⟳" if r.stale else "")


def status_text(reading: Reading) -> str:
    """主面板右侧：失败时优先显示状态，正常时显示重置时刻。"""
    if reading.failure_kind == "auth":
        return "需授权" if reading.provider == "claude" else "需登录"
    if reading.failure_kind == "rate_limit":
        return "限流等待"
    if reading.error:
        return "查询失败"
    if window_expired(reading):
        return "窗口已重置"
    if sample_expired(reading):
        return "数据已过期"
    if reading.stale:
        return "缓存"
    return fmt_reset(reading.resets_at)


def merge_readings(previous, incoming):
    providers = {reading.provider for reading in incoming}
    data = [reading for reading in previous if reading.provider not in providers] + incoming
    return sorted(data, key=lambda reading: (reading.provider != "claude", reading.provider))


def _sample_time(timestamp):
    try:
        return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M:%S") if timestamp is not None else "未知"
    except (ValueError, TypeError, OSError, OverflowError):
        return "未知"


def details(readings: list[Reading]) -> str:
    """两版共用的额度口径、采样时间和缓存状态说明。"""
    lines = ["面板百分比表示已用额度；剩余额度 = 100% − 已用额度。"]
    for reading in readings:
        resolved = resolve(reading)
        value = resolved.text
        if value is None:
            value = ("未知" if resolved.percent is None else
                     "已用 %.0f%% / 剩余 %.0f%%" % (resolved.percent, max(0, 100 - resolved.percent)))
        expired = sample_expired(reading) or window_expired(reading)
        status = "查询失败" if reading.error else (
            "缓存（非实时）" if reading.stale or expired else "账户查询")
        lines.append("\n%s %s：%s\n%s；最后成功采样时间：%s" % (
            NAME.get(reading.provider, reading.provider), reading.label, value, status,
            _sample_time(reading.observed_at)))
        if expired and (reading.percent is not None or reading.text is not None):
            historical = reading.text if reading.text is not None else "已用 %.0f%%" % reading.percent
            lines.append("历史读数：%s（不代表当前额度）" % historical)
            lines.append("窗口已重置" if window_expired(reading) else
                         "超过 5 分钟未更新或采样时间未知，主面板隐藏旧值")
        if reading.detail or reading.error:
            lines.append(reading.detail or reading.error)
        if reading.retry_at is not None:
            lines.append("下次尝试：%s（约 %d 秒后）" % (
                _sample_time(reading.retry_at), max(0, int(reading.retry_at - time.time() + .999))))
        if reading.failure_kind == "auth":
            lines.append("检测到登录凭证更新后会提前重试；无需重启 aifuel")
    return "\n".join(lines)
