"""Windows Claude Desktop: discover its CLI and read its existing OAuth grant.

Desktop owns token renewal. Never copy its refresh tokens into CLI credentials,
write its store, or read browser cookies. Plaintext is used only in memory.
"""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from quota_policy import QueryError


_CACHE_NAMES = ("oauth:tokenCache", "oauth:tokenCacheV2")
_SIGN_IN = "请打开 Claude 桌面端的 Code 模式并登录，AI Fuel 会自动重新检查授权"
_FAMILY = re.compile(r"claude_[a-z0-9]+", re.IGNORECASE)


def _children(path):
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _family_directories(parent):
    return {p.name.lower(): p for p in _children(parent)
            if _FAMILY.fullmatch(p.name) and p.is_dir()}


def _moved_families(root):
    prefix = ".user-data-left."
    return {p.name[len(prefix):].lower() for p in _children(root)
            if p.name.startswith(prefix) and _FAMILY.fullmatch(p.name[len(prefix):])
            and p.is_file()}


def data_roots():
    if sys.platform != "win32":
        return []
    roots = []
    local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    roaming_root = Path(roaming) / "Claude" if roaming else None
    roaming_moved = _moved_families(roaming_root) if roaming_root else set()
    if local:
        local = Path(local)
        # Discover package families from their actual directories, not a publisher ID.
        moved = _family_directories(local / "Claude-Data")
        for family, package in sorted(_family_directories(local / "Packages").items()):
            old = package / "LocalCache" / "Roaming" / "Claude"
            if family in _moved_families(old):
                # Even a temporarily missing destination must not revive old grants.
                moved.setdefault(family, local / "Claude-Data" / family)
            elif family not in moved:
                roots.append(old)
        for family in roaming_moved:
            moved.setdefault(family, local / "Claude-Data" / family)
        roots.extend(path for _, path in sorted(moved.items()))
    if roaming_root and not roaming_moved:
        roots.append(roaming_root)
    return roots


def find_cli():
    candidates = []
    for root in data_roots():
        try:
            base = root / "claude-code"
            paths = list(base.glob("*/claude.exe")) + list(base.glob("*/*/claude.exe"))
        except OSError:
            continue
        for path in paths:
            try:
                # Only native versioned components, never the Electron GUI claude.exe.
                version = path.relative_to(base).parts[0]
                if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version) and path.is_file():
                    candidates.append((path.stat().st_mtime_ns, str(path)))
            except OSError:
                continue
    return max(candidates, default=(0, None))[1]


def _config_path():
    candidates = []
    for root in data_roots():
        path = root / "config.json"
        try:
            candidates.append((path.stat().st_mtime_ns, str(path)))
        except OSError:
            continue
    # Multiple package families can be installed. Never silently choose a different
    # account merely because its window settings were written more recently.
    accounts = set()
    if len(candidates) > 1:
        for _, filename in candidates:
            try:
                account = _json_file(Path(filename)).get("lastKnownAccountUuid")
                if isinstance(account, str) and account:
                    accounts.add(account)
            except (OSError, ValueError, TypeError):
                raise QueryError("无法确认 Claude 多个数据目录的账户，请打开桌面端的 Code 模式检查登录", "auth") from None
        if len(accounts) > 1:
            raise QueryError("发现多个 Claude 账户的数据目录，无法确定额度来源；请用 Claude Code CLI 登录目标账户", "auth")
    # MSIX migrations may leave an older non-MSIX profile behind. Read the latest
    # desktop profile, including its logged-out state, rather than trying old accounts.
    newest = max(candidates, default=(0, None))[1]
    return Path(newest) if newest else None


def _json_file(path):
    with path.open(encoding="utf-8") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Desktop settings too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid desktop settings")
    return value


def credential_stamp():
    try:
        path = _config_path()
        if path is None:
            return None
        data = _json_file(path)
        # Window movement and other preferences must not cancel auth cooldown.
        auth = [str(path), data.get("lastKnownAccountUuid"), *(data.get(k) for k in _CACHE_NAMES)]
        return hashlib.sha256(json.dumps(auth, sort_keys=True).encode()).digest()
    except (OSError, ValueError, TypeError, QueryError):
        return None


def _decrypt_cache(config, root):
    from windows_crypto import decrypt_gcm, unprotect
    state = _json_file(root / "Local State")
    wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"], validate=True)
    if not wrapped.startswith(b"DPAPI"):
        raise ValueError("Unsupported desktop key format")
    key = unprotect(wrapped[5:])
    entries = {}
    for name in _CACHE_NAMES:
        value = config.get(name)
        if not value:
            continue
        encrypted = base64.b64decode(value, validate=True)
        if not encrypted.startswith(b"v10") or len(encrypted) < 31:
            raise ValueError("Unsupported desktop cache format")
        data = json.loads(decrypt_gcm(key, encrypted[3:15], encrypted[15:-16], encrypted[-16:]))
        if not isinstance(data, dict):
            raise ValueError("Invalid desktop token cache")
        # V2 entries (including revoked/null ones) supersede matching legacy entries.
        entries.update(data)
    return entries


def _select_token(entries, account, rejected_token=None, required_identity=None):
    if account is not None and (not isinstance(account, str) or not account):
        raise ValueError("Invalid desktop account")
    candidates, organizations = [], set()
    scoped = any(key.startswith("acct:") for key in entries)
    if required_identity is not None and (not scoped or account != required_identity[0]):
        raise QueryError("无法确认 Claude 桌面端与 CLI 属于同一账户，保留原有授权来源", "auth")
    for cache_key, entry in entries.items():
        if scoped:
            if not account or not cache_key.startswith("acct:" + account + "|"):
                continue
            cache_key = cache_key.split("|", 1)[1]
        identity, separator, scope = cache_key.partition(":https://api.anthropic.com:")
        if not separator or not {"user:profile", "user:inference"} <= set(scope.split()):
            continue
        parts = identity.split(":")
        if len(parts) != 2 or not all(parts) or not isinstance(entry, dict):
            continue
        if required_identity is not None and parts[1] != required_identity[1]:
            continue
        token, expiry = entry.get("token"), entry.get("expiresAt")
        if (not isinstance(token, str) or not token or isinstance(expiry, bool)
                or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)):
            continue
        organizations.add(parts[1])
        if expiry / 1000 > time.time() and token != rejected_token:
            candidates.append(("user:sessions:claude_code" in scope.split(), expiry, token))
    if len(organizations) > 1:
        raise QueryError("Claude 桌面端存在多个组织的授权，无法确定要显示的额度；请用 Claude Code CLI 登录目标账户", "auth")
    if not candidates:
        reason = "Claude 桌面端授权已过期或被拒绝；" if organizations else "Claude 桌面端的 Code 授权尚未就绪；"
        raise QueryError(reason + _SIGN_IN, "auth")
    return max(candidates)[2]


def access_token(rejected_token=None, *, required_identity=None):
    path = _config_path()
    if path is None:
        raise QueryError("未找到 Claude 桌面端授权文件；请登录 Claude Code CLI，或打开 Claude 桌面端的 Code 模式并登录", "auth")
    try:
        config = _json_file(path)
        if not any(config.get(k) for k in _CACHE_NAMES):
            raise QueryError("未找到 Claude 桌面端的 Code 授权；" + _SIGN_IN, "auth")
        entries = _decrypt_cache(config, path.parent)
        return _select_token(entries, config.get("lastKnownAccountUuid"), rejected_token, required_identity)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        # Never include Windows errors, decrypted content, or account identifiers.
        raise QueryError("无法读取 Claude 桌面端授权；请更新并打开桌面端的 Code 模式重试，也可使用 Claude Code CLI 登录", "auth") from None
