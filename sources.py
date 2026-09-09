# -*- coding: utf-8 -*-
"""额度数据层：Claude(OAuth API) + ChatGPT/Codex(本地会话日志)。

设计约束：
  * 对 ~/.claude/.credentials.json 只读，永不写入 —— 避免与 Claude Code
    抢 refresh token 轮换导致登出。token 过期时退化为读磁盘缓存。
  * 按需取键、忽略未知键 —— 服务端随时会加新的限制类型。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

HOME = os.path.expanduser("~")
CLAUDE_CREDS = os.path.join(HOME, ".claude", ".credentials.json")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")


def data_dir() -> str:
    """运行时状态的存放处（缓存、窗口位置）。

    不能用 __file__ —— 打包成 exe 后那是 _internal\\ 里的路径，
    往程序目录写状态既不合规也可能没权限。统一放 %LOCALAPPDATA%\\aifuel。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
    d = os.path.join(base, "aifuel")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    return d


CACHE_FILE = os.path.join(data_dir(), "cache.json")

# 429 退避：/api/oauth/usage 自己有速率限制，撞上了就别再猛敲。
# 起步必须明显短于刷新间隔，否则退避窗口和刷新周期同步，每次刷新都空转一轮，
# 恢复要拖十几分钟——用户看到的就是"一直 ⟳，像是坏了"。
BACKOFF_FIRST = 90.0
BACKOFF_MAX = 900.0
_backoff_until = 0.0
_backoff_step = 0.0


def backoff_remaining() -> float:
    """还要退避多少秒；0 表示现在就能发请求。前端据此安排下次刷新。"""
    return max(0.0, _backoff_until - time.time())

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
UA = "claude-cli/2.1.260 (external, cli)"


@dataclass
class Reading:
    """一条额度读数。percent 为 None 表示取不到。"""
    provider: str            # "claude" | "chatgpt"
    label: str               # "5h" | "week"
    percent: Optional[float]
    resets_at: Optional[float] = None   # unix 秒
    severity: str = "normal"            # normal | warning | critical
    stale: bool = False                 # True = 缓存值，非实时
    error: Optional[str] = None

    def reset_in(self) -> Optional[float]:
        """距离重置还有多少秒；None 表示未知。"""
        if self.resets_at is None:
            return None
        return max(0.0, self.resets_at - time.time())


# ---------------------------------------------------------------- 缓存

def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(key: str, readings: "list[Reading]") -> None:
    data = _load_cache()
    data[key] = {"at": time.time(), "readings": [asdict(r) for r in readings]}
    tmp = CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE_FILE)          # 原子替换，避免半截文件
    except Exception:
        pass


def _from_cache(key: str) -> "list[Reading]":
    entry = _load_cache().get(key)
    if not entry:
        return []
    out = []
    for d in entry.get("readings", []):
        d = dict(d)
        d.pop("stale", None)
        d.pop("error", None)
        try:
            out.append(Reading(stale=True, **d))
        except TypeError:
            pass                              # 缓存是旧版本结构，丢弃
    return out


# ---------------------------------------------------------------- Claude

def _iso_to_epoch(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def read_claude(timeout: float = 12.0) -> "list[Reading]":
    global _backoff_until, _backoff_step

    if time.time() < _backoff_until:
        # 还在退避窗口里，直接吃缓存，一个请求都不发
        cached = _from_cache("claude")
        if cached:
            return cached
        left = int(_backoff_until - time.time())
        return [Reading("claude", "5h", None, error="接口限流中，%ds 后重试" % left)]

    try:
        with open(CLAUDE_CREDS, encoding="utf-8") as f:
            oauth = json.load(f)["claudeAiOauth"]
    except Exception as e:
        return _from_cache("claude") or [
            Reading("claude", "5h", None, error="读不到凭证: %s" % type(e).__name__)
        ]

    if time.time() >= oauth.get("expiresAt", 0) / 1000:
        # 过期 —— 不自己刷新（会轮换 refresh token，可能把你登出）
        cached = _from_cache("claude")
        if cached:
            return cached
        return [Reading("claude", "5h", None, error="token 过期，用一次 Claude Code 即可")]

    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer %s" % oauth["accessToken"],
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _backoff_step = min(BACKOFF_MAX, _backoff_step * 2 if _backoff_step else BACKOFF_FIRST)
            _backoff_until = time.time() + _backoff_step
        return _from_cache("claude") or [Reading("claude", "5h", None, error="HTTP %s" % e.code)]
    except Exception as e:
        return _from_cache("claude") or [Reading("claude", "5h", None, error=type(e).__name__)]

    _backoff_step = 0.0            # 成功一次就把退避清零
    _backoff_until = 0.0
    readings = []

    # 优先用 limits[]：带 severity，是服务端自己的判断
    by_kind = {}
    for item in payload.get("limits") or []:
        if isinstance(item, dict) and item.get("kind"):
            by_kind[item["kind"]] = item

    def take(kind, top_key, label):
        item = by_kind.get(kind)
        if item and item.get("percent") is not None:
            readings.append(Reading(
                "claude", label, float(item["percent"]),
                _iso_to_epoch(item.get("resets_at")),
                item.get("severity") or "normal",
            ))
            return
        blk = payload.get(top_key)          # 回退到顶层字段
        if isinstance(blk, dict) and blk.get("utilization") is not None:
            readings.append(Reading(
                "claude", label, float(blk["utilization"]),
                _iso_to_epoch(blk.get("resets_at")),
            ))

    take("session", "five_hour", "5h")
    take("weekly_all", "seven_day", "week")

    if readings:
        _save_cache("claude", readings)
    return readings or [Reading("claude", "5h", None, error="返回里没有额度字段")]


# ---------------------------------------------------------------- Codex

def _tail(path: str, nbytes: int = 512 * 1024) -> str:
    """只读文件尾部——会话 jsonl 可能几十 MB，全读太慢。"""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode("utf-8", "replace")


def _newest_sessions(limit: int = 8) -> "list[str]":
    found = []
    for root, _dirs, files in os.walk(CODEX_SESSIONS):
        for name in files:
            if name.endswith(".jsonl"):
                p = os.path.join(root, name)
                try:
                    found.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    found.sort(reverse=True)
    return [p for _, p in found[:limit]]


def read_codex() -> "list[Reading]":
    for path in _newest_sessions():
        try:
            chunk = _tail(path)
        except Exception:
            continue
        # 从后往前找最后一条带 rate_limits 的记录
        for line in reversed(chunk.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue          # 尾部截断的半行，跳过
            rl = (rec.get("payload") or {}).get("rate_limits")
            if not isinstance(rl, dict):
                continue
            readings = []
            for key, label in (("primary", "5h"), ("secondary", "week")):
                blk = rl.get(key)
                if isinstance(blk, dict) and blk.get("used_percent") is not None:
                    readings.append(Reading(
                        "chatgpt", label, float(blk["used_percent"]),
                        blk.get("resets_at"),
                    ))
            if readings:
                _save_cache("chatgpt", readings)
                return readings
    cached = _from_cache("chatgpt")
    return cached or [Reading("chatgpt", "5h", None, error="没找到 Codex 会话记录")]


def read_all() -> "list[Reading]":
    return read_claude() + read_codex()


# ---------------------------------------------------------------- 单实例

_mutex_handle = None


def acquire_single_instance(name: str = "aifuel_widget") -> bool:
    """确保同时只有一份在跑。

    多开的代价不是难看，是每个实例都在敲 /api/oauth/usage，很容易把
    自己撞到 429。返回 False 表示已经有一份在跑了，调用方应当退出。
    """
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _mutex_handle = kernel32.CreateMutexW(None, False, "Local\\" + name)
        return kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS
    except Exception:
        return True                                 # 拿不到锁就别挡着用户


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for r in read_all():
        left = r.reset_in()
        left_s = "%dh%02dm" % (left // 3600, left % 3600 // 60) if left else "-"
        pct = "%.0f%%" % r.percent if r.percent is not None else "--"
        flags = " ".join(x for x in (["STALE"] if r.stale else []) + ([r.error] if r.error else []))
        print("%-8s %-5s %5s  reset in %7s  %-8s %s" % (
            r.provider, r.label, pct, left_s, r.severity, flags))
