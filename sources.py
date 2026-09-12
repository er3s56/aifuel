# -*- coding: utf-8 -*-
"""额度数据层：Claude(OAuth API) + ChatGPT/Codex(账户实时查询)。

设计约束：
  * Claude 凭证只读；续期交给官方 CLI 协调锁与 token 轮换，不自行写入。
  * 按需取键、忽略未知键 —— 服务端随时会加新的限制类型。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from codex_live import CodexClient
import claude_auth
import claude_desktop
from quota_policy import CLAUDE_POLL_INTERVAL, QueryError, RetryState, retry_after_seconds

HOME = os.path.expanduser("~")
CLAUDE_CREDS = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"),
                            ".credentials.json")


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

_cache_lock = threading.RLock()
_memory_samples = {}
_retry_states = {name: RetryState() for name in ("claude", "chatgpt")}
_provider_locks = {name: threading.Lock() for name in _retry_states}


def backoff_remaining() -> float:
    """离最近一次重试还有几秒；两家的等待互不影响。"""
    waits = [state.retry_at - time.time() for state in _retry_states.values()]
    return min((wait for wait in waits if wait > 0), default=0.0)

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
    text: Optional[str] = None          # 非百分比的值（如余额），有则替代百分比显示
    observed_at: Optional[float] = None # 数据采样时间，不是读取缓存的时间
    source: str = ""                    # "api" | "log"
    detail: Optional[str] = None        # 缓存降级原因，保留百分比显示
    failure_kind: str = ""              # temporary | rate_limit | auth | invalid | setup
    retry_at: Optional[float] = None    # 下次允许尝试的时间；手动刷新同样遵守

    def reset_in(self) -> Optional[float]:
        """距离重置还有多少秒；None 表示未知。"""
        if self.resets_at is None:
            return None
        return max(0.0, self.resets_at - time.time())


# ---------------------------------------------------------------- 缓存

def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(key: str, readings: "list[Reading]") -> None:
    # 两家并行完成时，读改写必须一起加锁，否则后写者会丢掉另一家的结果。
    with _cache_lock:
        data = _load_cache()
        data[key] = {"at": time.time(), "readings": [asdict(r) for r in readings]}
        # 磁盘写入失败时也保留这次成功值；保存快照，避免前端或降级标记改写原始时间。
        _memory_samples[(CACHE_FILE, key)] = data[key]
        tmp = CACHE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, CACHE_FILE)
        except Exception:
            pass


def _from_cache(key: str) -> "list[Reading]":
    entry = _memory_samples.get((CACHE_FILE, key)) or _load_cache().get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("readings"), list):
        return []
    out = []
    for d in entry.get("readings", []):
        if not isinstance(d, dict):
            continue
        d = dict(d)
        d.pop("stale", None)
        d.pop("error", None)
        try:
            reading = Reading(stale=True, **d)
            if reading.provider != key:
                continue
            _validate_readings([reading])
            out.append(reading)
        except (TypeError, ValueError):
            pass                              # 缓存是旧版本结构，丢弃
    return out


def _credential_stamp(provider):
    if provider == "claude" and claude_auth.uses_desktop(CLAUDE_CREDS):
        return claude_desktop.credential_stamp()
    path = CLAUDE_CREDS if provider == "claude" else os.path.join(
        os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex"), "auth.json")
    try:
        with open(path, "rb") as stream:
            return hashlib.sha256(stream.read()).digest()
    except OSError:
        return None


def _validate_readings(readings):
    if not readings:
        raise ValueError("missing limits")
    for reading in readings:
        if not isinstance(reading.label, str) or not reading.label:
            raise ValueError("invalid label")
        if reading.percent is None and reading.text is None:
            raise ValueError("missing value")
        if reading.percent is not None and (
                isinstance(reading.percent, bool) or not isinstance(reading.percent, (int, float))
                or not math.isfinite(reading.percent) or reading.percent < 0):
            raise ValueError("invalid percentage")
        for value in (reading.observed_at, reading.resets_at):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or not 0 <= value <= 253402214400):
                raise ValueError("invalid timestamp")
        if reading.text is not None and not isinstance(reading.text, str):
            raise ValueError("invalid balance")


def _fallback(provider, state):
    # 主面板仅接受明确来自账户接口的缓存。旧版日志或来源不明的缓存一律忽略。
    readings = [r for r in _from_cache(provider) if r.source == "api"]
    if not readings:
        readings = [Reading(provider, "状态", None, stale=True, error=state.detail)]
    for reading in readings:
        reading.stale = True
        reading.detail = state.detail
        reading.failure_kind = state.kind
        reading.retry_at = state.retry_at
    return readings


def _query_provider(provider, fetch, timeout, min_interval=0):
    with _provider_locks[provider]:
        state = _retry_states[provider]
        stamp = _credential_stamp(provider)
        # 凭证变化可提前结束登录等待，但不能绕过接口限流。
        if state.kind == "auth" and stamp != state.credential_stamp:
            state.clear()
            if provider == "chatgpt" and _codex_client is not None:
                _codex_client.close()
        if time.time() < state.retry_at:
            return _fallback(provider, state)
        if min_interval and not state.kind:
            cached = [r for r in _from_cache(provider) if r.source == "api"]
            now = time.time()
            if cached and all(r.observed_at is not None and
                              0 <= now - r.observed_at < min_interval for r in cached):
                # 正常查询间隔内沿用最近成功值；不是失败降级，不改变采样时间。
                # 根据采样时间判断，手动刷新和程序重启也不会制造额外请求。
                for reading in cached:
                    reading.stale = False
                return cached
        try:
            readings = fetch(timeout)
            _validate_readings(readings)
        except Exception as exc:
            if isinstance(exc, QueryError):
                error = exc
            elif isinstance(exc, (TimeoutError, urllib.error.URLError)):
                error = QueryError("额度查询超时" if isinstance(exc, TimeoutError) else "网络连接失败")
            elif isinstance(exc, FileNotFoundError):
                # FileNotFoundError can also come from process startup/state files.
                # It does not prove that the CLI or the user's login is missing.
                error = QueryError("本地查询文件暂不可用，正在自动重新检查", "dependency")
            elif isinstance(exc, (ValueError, TypeError, KeyError, AttributeError, OverflowError)):
                error = QueryError("额度响应格式异常或缺少有效额度字段", "invalid")
            else:
                error = QueryError("账户额度查询失败，请检查网络后重试")
            # The official CLI may have renewed credentials during fetch().
            # Record the failed attempt's final credentials, not its initial ones.
            state.fail(error, _credential_stamp(provider), time.time())
            return _fallback(provider, state)
        state.clear()
        now = time.time()
        for reading in readings:
            reading.observed_at, reading.source = now, "api"
            reading.stale, reading.error, reading.detail = False, None, None
            reading.failure_kind, reading.retry_at = "", None
        _save_cache(provider, readings)
        return readings


# ---------------------------------------------------------------- Claude

def _iso_to_epoch(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def read_claude(timeout: float = 12.0) -> "list[Reading]":
    return _query_provider("claude", _fetch_claude, timeout, min_interval=CLAUDE_POLL_INTERVAL)


def _fetch_claude(timeout):
    token = claude_auth.access_token(CLAUDE_CREDS, data_dir())
    for attempt in range(2):
        try:
            return _parse_claude_limits(_request_claude_usage(token, timeout))
        except QueryError as error:
            if error.kind != "auth" or attempt:
                raise
            token = claude_auth.access_token(CLAUDE_CREDS, data_dir(), rejected_token=token)


def _request_claude_usage(token, timeout):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer %s" % token,
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        retry_after = retry_after_seconds(e.headers.get("Retry-After")) if e.headers else None
        e.close()
        if e.code == 429:
            raise QueryError("额度接口限流，等待后自动重试", "rate_limit",
                             retry_after) from None
        if e.code == 401:
            raise QueryError("Claude 授权已失效，需要重新授权", "auth") from None
        if e.code == 403:
            raise QueryError("额度接口拒绝访问，请检查账户权限", "setup") from None
        raise QueryError("额度接口返回 HTTP %s" % e.code) from None
    return payload


# 已知 kind 的短标签。不在表里的 kind 不会被丢掉，会用 kind 名本身当标签 ——
# 服务端随时可能加新的限制类型（Opus/Sonnet 分项之类），硬编码白名单就会漏。
_CLAUDE_LABELS = {
    "session": "5h",
    "weekly_all": "wk",
    "weekly_scoped": "wk",      # 后面会被 scope 里的模型名覆盖
}


def _claude_label(item: dict) -> str:
    scope = item.get("scope") or {}
    model = (scope.get("model") or {}).get("display_name")
    if model:
        return str(model)[:8]               # 如 "Fable"，列宽有限
    surface = (scope.get("surface") or {}).get("display_name") if scope else None
    if surface:
        return str(surface)[:8]
    kind = item.get("kind") or "?"
    return _CLAUDE_LABELS.get(kind, kind.replace("_", " ")[:8])


def _parse_claude_limits(payload: dict) -> "list[Reading]":
    """把返回里的每一条限额都变成一行，而不是只挑认识的那两条。"""
    out = []
    seen = set()
    for item in payload.get("limits") or []:
        if not isinstance(item, dict) or item.get("percent") is None:
            continue
        if isinstance(item["percent"], bool):
            raise ValueError("invalid percentage")
        label = _claude_label(item)
        key = (label, item.get("kind"))
        if key in seen:
            continue
        seen.add(key)
        out.append(Reading(
            "claude", label, float(item["percent"]),
            _iso_to_epoch(item.get("resets_at")),
            item.get("severity") or "normal",
        ))
    if out:
        return out

    # limits[] 缺失时回退到顶层字段。顶层还有一堆未发布功能的占位键
    # （nimbus_quill 之类，utilization 恒为 0），只取这两个明确的。
    for top_key, label in (("five_hour", "5h"), ("seven_day", "wk")):
        blk = payload.get(top_key)
        if isinstance(blk, dict) and blk.get("utilization") is not None:
            if isinstance(blk["utilization"], bool):
                raise ValueError("invalid percentage")
            out.append(Reading(
                "claude", label, float(blk["utilization"]),
                _iso_to_epoch(blk.get("resets_at")),
            ))
    return out


# ---------------------------------------------------------------- Codex

# 键名 primary/secondary 只是位置，不代表窗口长度 —— 换套餐会变。
# 实测 prolite 计划的 primary 是 10080 分钟（周窗），secondary 为 null；
# 而 plus 计划的 primary 是 300 分钟（5 小时窗）。所以标签必须由
# windowDurationMins 推出来，按键名硬编码会把周额度标成 5h。
_CODEX_FALLBACK_LABELS = {"primary": "主", "secondary": "次"}


def _window_label(wm: Optional[float], key: str) -> str:
    if isinstance(wm, (int, float)) and wm > 0:
        wm = int(wm)
        if wm >= 10080 and wm % 10080 == 0:
            n = wm // 10080
            return "wk" if n == 1 else "%dwk" % n
        if wm >= 1440:
            return "%dd" % round(wm / 1440)
        if wm >= 60:
            return "%dh" % round(wm / 60)
        return "%dm" % wm
    return _CODEX_FALLBACK_LABELS.get(key, key.replace("_", " ")[:8])


_codex_client = None


def _fetch_codex_live(timeout: float) -> dict:
    global _codex_client
    if _codex_client is None:
        _codex_client = CodexClient(os.path.join(data_dir(), "codex-rpc"))
        atexit.register(_codex_client.close)
    return _codex_client.read_rate_limits(timeout)


def _parse_codex_live(payload: dict) -> "list[Reading]":
    buckets = payload.get("rateLimitsByLimitId")
    # 主额度必须明确选 codex，不能把 Spark / Reserve 的百分比混进 GPT 主额度。
    limits = buckets.get("codex") if isinstance(buckets, dict) else None
    if not isinstance(limits, dict):
        limits = payload.get("rateLimits")
    if not isinstance(limits, dict) or limits.get("limitId") not in (None, "codex"):
        return []
    ranked = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict) or window.get("usedPercent") is None or isinstance(window.get("usedPercent"), bool):
            continue
        try:
            percent = float(window["usedPercent"])
            if not math.isfinite(percent):
                continue
            reset = window.get("resetsAt")
            if reset is not None:
                reset = float(reset)
                if not math.isfinite(reset):
                    continue
            minutes = window.get("windowDurationMins")
            if not isinstance(minutes, (int, float)) or not math.isfinite(minutes):
                minutes = None
        except (TypeError, ValueError, OverflowError):
            continue
        ranked.append((minutes if minutes is not None else float("inf"),
                       Reading("chatgpt", _window_label(minutes, key), percent, reset)))
    # 短窗口排前面；直接解析账户响应，不再转换为旧日志字段。
    ranked.sort(key=lambda item: item[0])
    readings = [reading for _, reading in ranked]
    credits = limits.get("credits")
    if isinstance(credits, dict) and credits.get("hasCredits"):
        balance = credits.get("balance")
        text = "∞" if credits.get("unlimited") else (str(balance) if balance is not None else "?")
        readings.append(Reading("chatgpt", "余额", None, text=text))
    return readings


def read_codex(timeout: float = 15.0) -> "list[Reading]":
    return _query_provider("chatgpt", lambda wait: _parse_codex_live(_fetch_codex_live(wait)), timeout)


def read_all(on_result=None) -> "list[Reading]":
    """并行查询；完成一家立即通知前端，最终列表仍保持 Claude、GPT 的顺序。"""
    results = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="quota") as pool:
        pending = {pool.submit(read): provider for provider, read in (
            ("claude", read_claude), ("chatgpt", read_codex))}
        for future in as_completed(pending):
            provider = pending[future]
            try:
                data = future.result()
            except Exception:
                state = _retry_states[provider]
                state.fail(QueryError("账户额度查询失败"), _credential_stamp(provider), time.time())
                data = _fallback(provider, state)
            results[provider] = data
            if on_result is not None:
                on_result(data)
    return [reading for provider in ("claude", "chatgpt") for reading in results[provider]]


# ---------------------------------------------------------------- 单实例

def acquire_single_instance(name: str = "aifuel_widget") -> bool:
    """确保同时只有一份在跑。

    多开的代价不是难看，是每个实例都在敲 /api/oauth/usage，很容易把
    自己撞到 429。返回 False 表示已经有一份在跑了，调用方应当退出。
    """
    from single_instance import acquire
    return acquire(name)


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
