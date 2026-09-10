"""两家共用的失败分类与重试规则，不保存凭证或服务端原始错误。"""
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time


CACHE_TTL = 300.0
RETRY_FIRST = 30.0
RATE_LIMIT_FIRST = 90.0
RETRY_MAX = 900.0
AUTH_RECHECK = 300.0


def retry_after_seconds(value, now=None):
    """支持 Retry-After 的秒数和 HTTP 日期；无效值交给指数退避。"""
    if value is None or isinstance(value, bool):
        return None
    now = time.time() if now is None else now
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class QueryError(RuntimeError):
    def __init__(self, message, kind="temporary", retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


@dataclass
class RetryState:
    kind: str = ""
    detail: str = ""
    retry_at: float = 0.0
    step: float = 0.0
    credential_stamp: object = None

    def clear(self):
        self.kind = self.detail = ""
        self.retry_at = self.step = 0.0
        self.credential_stamp = None

    def fail(self, error, stamp, now):
        self.kind, self.detail = error.kind, str(error)
        self.credential_stamp = stamp
        if error.kind == "auth":
            delay = AUTH_RECHECK
        else:
            first = RATE_LIMIT_FIRST if error.kind == "rate_limit" else RETRY_FIRST
            self.step = min(RETRY_MAX, max(first, self.step * 2))
            delay = error.retry_after if error.retry_after is not None else self.step
        self.retry_at = now + delay
