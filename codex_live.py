"""通过已登录的 Codex app-server 查询账户额度，不创建会话或模型请求。"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

from quota_policy import QueryError, retry_after_seconds


def find_codex() -> str:
    override = os.environ.get("AIFUEL_CODEX_EXE")
    if override:
        if os.path.isfile(override):
            return override
        raise FileNotFoundError("AIFUEL_CODEX_EXE 指定的文件不存在")
    found = shutil.which("codex.exe" if sys.platform == "win32" else "codex")
    if found:
        return found
    # Windows 安装器的标准位置；从资源管理器启动时 PATH 可能尚未更新。
    local = os.environ.get("LOCALAPPDATA", "")
    installed = os.path.join(local, "Programs", "OpenAI", "Codex", "bin", "codex.exe")
    if sys.platform == "win32" and os.path.isfile(installed):
        return installed
    raise FileNotFoundError("找不到 Codex CLI，请安装并登录，或设置 AIFUEL_CODEX_EXE")


class CodexLiveError(QueryError):
    pass


def _rpc_error(error):
    """只提取分类和等待秒数，不把原始消息、URL 或账户信息传给界面。"""
    if not isinstance(error, dict):
        return CodexLiveError("Codex 返回了无效错误响应", "invalid")
    data = error.get("data")
    data = data if isinstance(data, dict) else {}
    status = data.get("httpStatusCode", data.get("statusCode"))
    message = str(error.get("message", "")).lower()
    # app-server 部分版本只在消息前缀里保留 HTTP 状态，不转发响应头。
    prefix = message.split("; body=", 1)[0].split("; content-type=", 1)[0]
    match = re.search(r"(?:http(?: status)?|status(?: code)?|failed:)\s*[:=]?\s*(401|403|429)\b", prefix)
    if match:
        status = match.group(1)
    if str(status) == "429" or "too many requests" in prefix:
        headers = data.get("headers")
        headers = headers if isinstance(headers, dict) else {}
        retry = data.get("retryAfterSeconds", data.get("retryAfter"))
        if retry is None:
            retry = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
        return CodexLiveError("额度接口限流，等待后自动重试", "rate_limit", retry_after_seconds(retry))
    if str(status) == "401" or any(text in prefix for text in (
            "not logged in", "not authenticated", "authentication required", "token has expired",
            "token expired", "unauthorized")):
        return CodexLiveError("Codex 登录已失效，请重新登录 Codex", "auth")
    if str(status) == "403":
        return CodexLiveError("额度接口拒绝访问，请检查账户权限", "setup")
    return CodexLiveError("账户额度查询失败，请确认 Codex 已登录且网络可用")


class CodexClient:
    """复用一个隐藏的 stdio 进程；每次 read 都向服务端重新查询。"""

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self._lock = threading.Lock()
        self._process = None
        self._reader = None
        self._messages = None
        self._next_id = 0

    @staticmethod
    def _read_output(stream, messages):
        try:
            for line in stream:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and "id" in message:
                    messages.put(message)
        finally:
            messages.put(None)

    def _send(self, message):
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def _request(self, method, params, deadline):
        self._next_id += 1
        ident = self._next_id
        self._send({"id": ident, "method": method, "params": params})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex 额度查询超时")
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Codex 额度查询超时") from None
            if message is None:
                raise CodexLiveError("Codex 查询进程已退出，请检查 Codex 是否能正常启动")
            if "method" in message:
                # 只使用已有登录；不代答登录、令牌更新或其他交互请求。
                self._send({"id": message["id"], "error": {
                    "code": -32601, "message": "Interactive requests are not supported by aifuel"}})
                continue
            if message.get("id") != ident:
                continue
            if "error" in message:
                raise _rpc_error(message["error"])
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexLiveError("Codex 返回了无效的额度响应", "invalid")
            return result

    def read_rate_limits(self, timeout: float = 15.0) -> dict:
        with self._lock:
            deadline = time.monotonic() + timeout
            try:
                if self._process is None or self._process.poll() is not None:
                    self._stop()
                    exe = find_codex()
                    os.makedirs(self.state_dir, exist_ok=True)
                    # 单独存放辅助进程的 SQLite 状态，避免与桌面应用共用状态数据库。
                    self._process = subprocess.Popen(
                        [exe, "app-server", "-c", "sqlite_home=" + json.dumps(os.path.abspath(self.state_dir))],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True, encoding="utf-8", errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    self._messages = queue.Queue()
                    self._reader = threading.Thread(
                        target=self._read_output, args=(self._process.stdout, self._messages), daemon=True)
                    self._reader.start()
                    self._request("initialize", {"clientInfo": {
                        "name": "aifuel", "title": "aifuel", "version": "0.2.0"}}, deadline)
                    self._send({"method": "initialized", "params": {}})
                account = self._request("account/read", {"refreshToken": False}, deadline).get("account")
                if account is None:
                    raise CodexLiveError("尚未登录，请先登录 Codex", "auth")
                if not isinstance(account, dict):
                    raise CodexLiveError("Codex 登录状态响应异常", "invalid")
                if account.get("type") not in ("chatgpt", "chatgptAuthTokens"):
                    raise CodexLiveError("请使用 ChatGPT 账户登录 Codex 后查询订阅额度", "auth")
                return self._request("account/rateLimits/read", {}, deadline)
            except Exception:
                self._stop()
                raise

    def _stop(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if self._reader is not None:
            self._reader.join(timeout=1)
        process.stdout.close()
        self._reader = self._messages = None

    def close(self):
        with self._lock:
            self._stop()
