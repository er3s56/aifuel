"""失败、恢复和时间边界测试；全程使用假时间与模拟响应。"""
import io
import json
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch

import codex_live
import display
import sources
from quota_policy import QueryError, RetryState, retry_after_seconds
from test_codex_live import live_payload
from test_regressions import IsolatedTest


class FailurePolicyTests(IsolatedTest):
    def test_local_dependency_failure_recovers_without_long_backoff(self):
        first = sources.read_codex()[0]
        self.codex.side_effect = FileNotFoundError()
        for attempt in range(35):
            now = 2030 + attempt * 30
            self.clock.return_value = now
            reading = sources.read_codex()[0]
            self.assertEqual(reading.retry_at, now + 30)
            self.assertEqual(display.status_text(reading), "查询重试")
            self.assertEqual(reading.observed_at, first.observed_at)
        self.codex.side_effect = None
        self.clock.return_value += 30
        recovered = sources.read_codex()[0]
        self.assertFalse(recovered.stale)
        self.assertEqual(recovered.failure_kind, "")

    def setUp(self):
        super().setUp()
        self.clock = self.enterContext(patch.object(sources.time, "time", return_value=2000.0))
        self.enterContext(patch.object(sources, "CACHE_FILE", str(self.state / "cache.json")))
        self.enterContext(patch.object(sources, "_retry_states", {
            name: RetryState() for name in ("claude", "chatgpt")}))
        self.stamp = self.enterContext(patch.object(sources, "_credential_stamp", return_value=b"old"))
        self.claude = self.enterContext(patch.object(sources, "_fetch_claude", return_value=[
            sources.Reading("claude", "5h", 27, resets_at=9000)]))
        self.codex = self.enterContext(patch.object(sources, "_fetch_codex_live", return_value=live_payload(18)))

    def test_network_failure_and_recovery_are_identical_for_both_providers(self):
        for provider, read, fetch in (("claude", sources.read_claude, self.claude),
                                       ("chatgpt", sources.read_codex, self.codex)):
            with self.subTest(provider=provider):
                self.clock.return_value = 2000
                first = read()[0]
                self.clock.return_value = 2120
                fetch.side_effect = TimeoutError("do not leak transport internals")
                failed = read()[0]
                self.assertEqual(failed.percent, first.percent)
                self.assertEqual(failed.observed_at, 2000)
                self.assertEqual(failed.retry_at, 2150)
                self.assertIn("超时", failed.detail)
                self.assertEqual(display.status_text(failed), "缓存")
                calls = fetch.call_count
                for _ in range(3):
                    read()
                self.assertEqual(fetch.call_count, calls, "Manual refresh bypassed backoff")
                self.clock.return_value = 2150
                failed = read()[0]
                self.assertEqual(failed.retry_at, 2210)
                fetch.side_effect = None
                self.clock.return_value = 2210
                recovered = read()[0]
                self.assertFalse(recovered.stale)
                self.assertIsNone(recovered.detail)
                self.assertIsNone(recovered.retry_at)
                self.assertEqual(recovered.observed_at, 2210)
                self.assertEqual(sources._retry_states[provider].step, 0)

    def test_limit_wait_is_respected_even_by_manual_refresh_and_credential_changes(self):
        self.claude.side_effect = QueryError("限流", "rate_limit", retry_after=7200)
        reading = sources.read_claude()[0]
        self.assertEqual(reading.retry_at, 9200)  # 不把服务端等待截短为本地 15 分钟上限
        self.clock.return_value = 9199
        self.stamp.return_value = b"new"
        sources.read_claude()
        self.claude.assert_called_once()
        self.clock.return_value = 9200
        sources.read_claude()
        self.assertEqual(self.claude.call_count, 2)

    def test_zero_and_short_retry_after_never_cancel_exponential_backoff(self):
        for read, fetch in ((sources.read_claude, self.claude), (sources.read_codex, self.codex)):
            self.clock.return_value = 2000
            fetch.side_effect = QueryError("限流", "rate_limit", retry_after=0)
            first = read()[0]
            self.assertEqual(first.retry_at, 2090)
            for now in (2000, 2030, 2089):
                self.clock.return_value = now
                read()
            self.assertEqual(fetch.call_count, 1)
            self.clock.return_value = 2090
            fetch.side_effect = QueryError("限流", "rate_limit", retry_after=1)
            self.assertEqual(read()[0].retry_at, 2270)
            self.assertEqual(fetch.call_count, 2)

    def test_claude_queries_at_most_every_two_minutes_without_delaying_gpt(self):
        first = sources.read_claude()[0]
        for now in (2000, 2030, 2060, 2119):
            self.clock.return_value = now
            current = sources.read_claude()[0]
            self.assertFalse(current.stale)
            self.assertEqual(current.observed_at, first.observed_at)
            sources.read_codex()
        self.claude.assert_called_once()
        self.assertEqual(self.codex.call_count, 4)
        self.clock.return_value = 2120
        self.assertEqual(sources.read_claude()[0].observed_at, 2120)
        self.assertEqual(self.claude.call_count, 2)

    def test_recent_disk_sample_prevents_request_burst_after_restart(self):
        sources.read_claude()
        with patch.object(sources, "_memory_samples", {}), patch.object(sources, "_retry_states", {
                name: RetryState() for name in ("claude", "chatgpt")}):
            self.clock.return_value = 2010
            result = sources.read_claude()[0]
        self.claude.assert_called_once()
        self.assertEqual(result.observed_at, 2000)
        self.assertFalse(result.stale)

    def test_both_providers_use_same_rate_limit_backoff(self):
        for read, fetch in ((sources.read_claude, self.claude), (sources.read_codex, self.codex)):
            fetch.side_effect = QueryError("限流", "rate_limit")
            self.clock.return_value = 2000
            for delay in (90, 180, 360, 720, 900, 900):
                reading = read()[0]
                self.assertEqual(reading.retry_at - self.clock.return_value, delay)
                self.clock.return_value = reading.retry_at

    def test_login_change_resumes_both_providers_before_retry_time(self):
        for provider, read, fetch in (("claude", sources.read_claude, self.claude),
                                       ("chatgpt", sources.read_codex, self.codex)):
            with self.subTest(provider=provider):
                self.stamp.return_value = b"old"
                fetch.side_effect = QueryError("请重新登录", "auth")
                reading = read()[0]
                self.assertEqual(display.status_text(reading), "需授权" if provider == "claude" else "需登录")
                read()
                self.assertEqual(fetch.call_count, 1)
                self.stamp.return_value = b"new"
                fetch.side_effect = None
                with patch.object(sources, "_codex_client") as client:
                    recovered = read()[0]
                    if provider == "chatgpt":
                        client.close.assert_called_once()
                self.assertFalse(recovered.stale)
                self.assertEqual(fetch.call_count, 2)

    def test_unobservable_keyring_login_is_eventually_rechecked(self):
        self.codex.side_effect = QueryError("需登录", "auth")
        sources.read_codex()
        self.clock.return_value = 2300
        self.codex.side_effect = None
        self.assertFalse(sources.read_codex()[0].stale)

    def test_malformed_response_preserves_cache_and_reason(self):
        for read, fetch in ((sources.read_claude, self.claude), (sources.read_codex, self.codex)):
            first = read()[0]
            self.clock.return_value += 120
            fetch.return_value = [] if read == sources.read_claude else {}
            failed = read()[0]
            self.assertEqual(failed.percent, first.percent)
            self.assertEqual(failed.failure_kind, "invalid")
            self.assertIn("字段", failed.detail)

    def test_legacy_log_cache_is_never_used_by_panel(self):
        for source in ("log", ""):
            with self.subTest(source=source):
                sources._save_cache("chatgpt", [sources.Reading(
                    "chatgpt", "wk", 16, source=source, observed_at=2000)])
                self.codex.side_effect = TimeoutError()
                self.assertIsNone(sources.read_codex()[0].percent)

    def test_corrupt_cache_never_breaks_failure_handling(self):
        cases = [[], {"chatgpt": []}, {"chatgpt": {"readings": [None, 3, "bad", {
            "provider": "chatgpt", "label": "wk", "percent": "bad", "source": "api"}]}}]
        self.codex.side_effect = TimeoutError()
        for case in cases:
            with self.subTest(case=case):
                (self.state / "cache.json").write_text(json.dumps(case), encoding="utf-8")
                result = sources.read_codex()[0]
                self.assertIsNone(result.percent)
                self.assertIsNotNone(result.error)

    def test_disk_write_failure_still_preserves_last_success_in_memory(self):
        with patch("builtins.open", side_effect=OSError("disk unavailable")):
            first = sources.read_codex()[0]
            self.clock.return_value = 2030
            self.codex.side_effect = TimeoutError()
            cached = sources.read_codex()[0]
        self.assertEqual(cached.percent, first.percent)
        self.assertEqual(cached.observed_at, 2000)
        self.assertTrue(cached.stale)
        self.assertFalse(first.stale)

    def test_fast_provider_is_delivered_before_slow_provider_finishes(self):
        slow_started, release, delivered = threading.Event(), threading.Event(), threading.Event()
        seen = []

        def slow(timeout):
            slow_started.set()
            if not release.wait(3):
                raise TimeoutError()
            return [sources.Reading("claude", "5h", 27)]

        self.claude.side_effect = slow

        def received(data):
            seen.append(data[0].provider)
            if data[0].provider == "chatgpt":
                delivered.set()

        worker = threading.Thread(target=lambda: sources.read_all(on_result=received))
        worker.start()
        try:
            self.assertTrue(slow_started.wait(2))
            self.assertTrue(delivered.wait(2), "GPT waited for Claude")
            self.assertEqual(seen, ["chatgpt"])
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(set(sources._load_cache()), {"claude", "chatgpt"})


class DisplayExpiryTests(unittest.TestCase):
    def setUp(self):
        self.clock = self.enterContext(patch.object(display.time, "time", return_value=2299))

    def test_five_minute_boundary_hides_percent_and_balance_but_keeps_history(self):
        for provider in ("claude", "chatgpt"):
            for balance in (False, True):
                with self.subTest(provider=provider, balance=balance):
                    r = sources.Reading(provider, "wk", None if balance else 18,
                                        text="12" if balance else None, stale=True, source="api",
                                        observed_at=2000, resets_at=9000, detail="网络连接失败", retry_at=3000)
                    self.clock.return_value = 2299
                    result = display.resolve(r)
                    self.assertTrue(result.percent == 18 or result.text == "12")
                    self.clock.return_value = 2300
                    result = display.resolve(r)
                    self.assertIsNone(result.percent)
                    self.assertIsNone(result.text)
                    detail = display.details([r])
                    self.assertIn("历史读数", detail)
                    self.assertIn("不代表当前额度", detail)
                    self.assertIn("下次尝试", detail)
                    self.assertIn("网络连接失败", detail)

    def test_window_reset_hides_recent_cached_value(self):
        r = sources.Reading("claude", "5h", 27, observed_at=2290, resets_at=2299, stale=True)
        self.assertIsNone(display.resolve(r).percent)
        self.assertEqual(display.resolve(r).note, "↺")
        self.assertIn("历史读数：已用 27%", display.details([r]))

    def test_missing_timestamp_or_future_timestamp_never_looks_fresh(self):
        for observed in (None, 3000):
            r = sources.Reading("chatgpt", "wk", 18, source="api", observed_at=observed, stale=True)
            self.assertIsNone(display.resolve(r).percent)

    def test_even_last_success_expires_while_next_request_is_pending(self):
        r = sources.Reading("chatgpt", "wk", 18, source="api", observed_at=1999)
        self.assertIsNone(display.resolve(r).percent)


class ClaudeTransportTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        credentials = self.state / "credentials.json"
        credentials.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "fake", "expiresAt": 9000000000000}}), encoding="utf-8")
        self.enterContext(patch.object(sources, "CLAUDE_CREDS", str(credentials)))
        self.request = self.enterContext(patch.object(sources.urllib.request, "urlopen"))

    def test_retry_after_is_preserved_and_http_errors_are_classified(self):
        for code, kind in ((429, "rate_limit"), (401, "auth"), (403, "setup"), (500, "temporary")):
            self.request.side_effect = urllib.error.HTTPError(
                "https://example.test", code, "private detail", {"Retry-After": "120"}, io.BytesIO())
            with self.assertRaises(QueryError) as caught:
                sources._fetch_claude(1)
            self.assertEqual(caught.exception.kind, kind)
            self.assertNotIn("private", str(caught.exception))
            if code == 429:
                self.assertEqual(caught.exception.retry_after, 120)

    def test_invalid_numeric_response_is_rejected_before_cache_write(self):
        for percent in ("nan", "inf", "bad"):
            with self.subTest(percent=percent):
                self.request.return_value = io.BytesIO(json.dumps({
                    "limits": [{"kind": "session", "percent": percent}]}).encode())
                with self.assertRaises(ValueError):
                    sources._validate_readings(sources._fetch_claude(1))


class ErrorMetadataTests(unittest.TestCase):
    def test_retry_after_seconds_dates_and_invalid_values(self):
        date = format_datetime(datetime.fromtimestamp(2120, timezone.utc), usegmt=True)
        self.assertEqual(retry_after_seconds(date, now=2000), 120)
        self.assertEqual(retry_after_seconds("90"), 90)
        for value in ("bad", "nan", "inf", True, None):
            self.assertIsNone(retry_after_seconds(value))

    def test_codex_structured_and_text_errors_keep_only_safe_metadata(self):
        error = codex_live._rpc_error({"message": "private account data", "data": {
            "httpStatusCode": 429, "headers": {"Retry-After": "180"}}})
        self.assertEqual((error.kind, error.retry_after), ("rate_limit", 180))
        self.assertNotIn("private", str(error))
        for status, kind in ((429, "rate_limit"), (401, "auth"), (403, "setup")):
            error = codex_live._rpc_error({"message":
                "GET https://private.example failed: %d; content-type=text; body=private" % status})
            self.assertEqual(error.kind, kind)
            self.assertNotIn("private", str(error))
        error = codex_live._rpc_error({"message": "request failed; body=HTTP 429"})
        self.assertEqual(error.kind, "temporary")


if __name__ == "__main__":
    unittest.main()
