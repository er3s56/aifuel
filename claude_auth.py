"""通过官方 Claude CLI 自动续期；aifuel 不写凭证、不发送模型请求。"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time

from quota_policy import QueryError


REFRESH_TIMEOUT = 45.0
_refresh_lock = threading.Lock()


@dataclass(frozen=True)
class Credential:
    token: str = field(repr=False)
    expires_at: float
    refreshable: bool

    def usable(self, rejected_token=None):
        return bool(self.token and self.token != rejected_token and time.time() < self.expires_at)


def _read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            oauth = json.load(stream)["claudeAiOauth"]
        token = oauth.get("accessToken", "")
        expiry = float(oauth["expiresAt"]) / 1000
        refresh = oauth.get("refreshToken")
        if not isinstance(token, str) or not math.isfinite(expiry):
            raise ValueError("invalid credential metadata")
        return Credential(token, expiry, isinstance(refresh, str) and bool(refresh))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise QueryError("无法读取 Claude 授权信息，请在 Claude Code 中完成授权", "auth") from None


def find_claude():
    override = os.environ.get("AIFUEL_CLAUDE_EXE")
    if override:
        if os.path.isfile(override):
            return override
        raise QueryError("AIFUEL_CLAUDE_EXE 指定的 Claude 程序不存在", "setup")
    executable = "claude.exe" if sys.platform == "win32" else "claude"
    found = shutil.which(executable)
    if found:
        return found
    # 资源管理器启动的 exe 可能尚未继承 CLI 安装后的 PATH。
    installed = os.path.join(os.path.expanduser("~"), ".local", "bin", executable)
    if os.path.isfile(installed):
        return installed
    raise QueryError("自动续期需要 Claude Code，请安装或设置 AIFUEL_CLAUDE_EXE", "setup")


def _command():
    return [find_claude(), "--print", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--no-session-persistence",
            "--setting-sources=", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--tools", "", "--disable-slash-commands", "--settings", '{"disableAllHooks":true}']


def _environment(path):
    env = os.environ.copy()
    # 本助手只管理上述文件中的订阅授权，不能被其他 API/云平台凭证或端点覆盖。
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                 "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_UNIX_SOCKET", "CLAUDECODE",
                 "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                 "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        env.pop(name, None)
    env.update(CLAUDE_CONFIG_DIR=os.path.dirname(os.path.abspath(path)),
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
    return env


def _stop(process):
    # EOF 让 CLI 自己结束并释放官方续期锁；异常或超时也不能留下后台进程。
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _refresh(path, state_dir, rejected_token, timeout):
    cwd = os.path.join(state_dir, "claude-auth-helper")
    os.makedirs(cwd, exist_ok=True)
    try:
        process = subprocess.Popen(_command(), cwd=cwd, env=_environment(path),
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise QueryError("无法启动 Claude 自动续期助手，将稍后重试") from None
    try:
        requests = [{"subtype": "initialize"}]
        if rejected_token is not None:
            # 401 时让官方只读 /usage 流程处理服务端失效与强制续期。
            # 不采用它可能回退到的缓存，续期后由数据层重新查询账户接口。
            requests.append({"subtype": "get_usage"})
        for index, request in enumerate(requests):
            message = {"type": "control_request", "request_id": "aifuel-auth-%d" % index,
                       "request": request}
            process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                current = _read(path)
            except QueryError:
                current = None  # 客户端写入期间的短暂不可读，等它完成后再判断。
            if current is not None:
                if current.usable(rejected_token):
                    return
                if not current.refreshable:
                    raise QueryError("Claude 授权已失效，自动恢复失败，需要重新授权", "auth")
            if process.poll() is not None:
                break
            time.sleep(.1)
    except OSError:
        raise QueryError("Claude 自动续期暂未完成，将稍后重试") from None
    finally:
        _stop(process)


def access_token(path, state_dir, rejected_token=None, timeout=REFRESH_TIMEOUT):
    current = _read(path)
    if current.usable(rejected_token):
        return current.token
    with _refresh_lock:
        # 并发请求或官方客户端可能已续期；复查磁盘，避免轮换同一份 refresh token。
        current = _read(path)
        if current.usable(rejected_token):
            return current.token
        if not current.refreshable:
            raise QueryError("Claude 授权无法自动续期，需要重新授权", "auth")
        _refresh(path, state_dir, rejected_token, timeout)
        current = _read(path)
        if current.usable(rejected_token):
            return current.token
        if not current.refreshable:
            raise QueryError("Claude 授权已失效，自动恢复失败，需要重新授权", "auth")
        raise QueryError("Claude 自动续期暂未完成，将稍后重试")
