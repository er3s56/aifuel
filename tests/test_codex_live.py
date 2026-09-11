"""账户实时查询、明确降级以及 stdio 子进程生命周期的回归测试。"""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from test_regressions import IsolatedTest
import codex_live
import display
import sources


def live_payload(percent):
    return {"rateLimitsByLimitId": {"codex": {
        "limitId": "codex", "primary": {
            "usedPercent": percent, "windowDurationMins": 10080, "resetsAt": 2000000000},
        "secondary": None}}}


class DiscoveryTests(unittest.TestCase):
    def test_removed_override_falls_back_to_installed_cli(self):
        with patch.dict(os.environ, {"AIFUEL_CODEX_EXE": "removed/codex.exe"}), \
                patch.object(codex_live, "_executable_path", return_value=None), \
                patch.object(codex_live.shutil, "which", return_value="installed/codex.exe"):
            self.assertEqual(codex_live.find_codex(), "installed/codex.exe")

    def test_missing_cli_is_a_retryable_local_dependency(self):
        with patch.dict(os.environ, {"AIFUEL_CODEX_EXE": ""}), \
                patch.object(codex_live, "_executable_path", return_value=None), \
                patch.object(codex_live.shutil, "which", return_value=None):
            with self.assertRaises(codex_live.QueryError) as failure:
                codex_live.find_codex()
            self.assertEqual(failure.exception.kind, "dependency")


@unittest.skipUnless(sys.platform == "win32", "Windows launcher junctions")
class JunctionDiscoveryTests(IsolatedTest):
    def test_installer_protection_follows_current_release_without_disabling_it(self):
        release = self.state / "release with spaces"
        (release / "bin").mkdir(parents=True)
        executable = release / "bin" / "codex.exe"
        executable.write_bytes(b"test executable")
        current = self.state / "current"
        local = self.state / "local"
        launcher = local / "Programs" / "OpenAI" / "Codex" / "bin"
        launcher.parent.mkdir(parents=True)
        pairs = [(current, release), (launcher, current / "bin")]
        script = "\n".join(
            "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''")
            + "' -Target '" + str(target).replace("'", "''") + "' | Out-Null"
            for link, target in pairs)
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], check=True,
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        # This protection is enabled only in the disposable child test process.
        # A real junction must fail with 448 before testing the discovery fallback.
        code = r'''
import ctypes, os, sys
from pathlib import Path
from unittest.mock import patch
import codex_live
api = ctypes.WinDLL("kernel32", use_last_error=True)
api.SetProcessMitigationPolicy.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
flags = ctypes.c_uint32(1)
if not api.SetProcessMitigationPolicy(16, ctypes.byref(flags), ctypes.sizeof(flags)):
    sys.exit(77 if ctypes.get_last_error() == 87 else 1)
launcher, expected, local = sys.argv[1:]
try:
    os.stat(launcher)
except OSError as error:
    assert error.winerror == 448, error
else:
    raise AssertionError("Fixture did not reproduce the installer protection")
with patch.dict(os.environ, {"AIFUEL_CODEX_EXE": launcher}):
    assert Path(codex_live.find_codex()).samefile(expected)
with patch.dict(os.environ, {"AIFUEL_CODEX_EXE": "", "PATH": str(Path(launcher).parent)}):
    assert Path(codex_live.find_codex()).samefile(expected)
with patch.dict(os.environ, {"AIFUEL_CODEX_EXE": "", "PATH": "", "LOCALAPPDATA": local}):
    assert Path(codex_live.find_codex()).samefile(expected)
api.GetCurrentProcess.restype = ctypes.c_void_p
api.GetProcessMitigationPolicy.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_size_t]
assert api.GetProcessMitigationPolicy(api.GetCurrentProcess(), 16,
                                     ctypes.byref(flags), ctypes.sizeof(flags))
assert flags.value & 1, "Discovery must preserve the process protection"
'''
        result = subprocess.run([sys.executable, "-c", code, str(launcher / "codex.exe"),
                                 str(executable), str(local)], capture_output=True, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        if result.returncode == 77:
            self.skipTest("RedirectionGuard is unavailable on this Windows version")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class LiveSourceTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(sources, "CACHE_FILE", str(self.state / "cache.json")))
        self.enterContext(patch.object(sources, "_retry_states", {
            name: sources.RetryState() for name in ("claude", "chatgpt")}))
        self.enterContext(patch.object(sources, "_credential_stamp", return_value=b"test"))
        self.query = self.enterContext(patch.object(sources, "_fetch_codex_live"))
        self.query.return_value = live_payload(18)

    def test_remote_usage_updates_without_any_local_session(self):
        self.query.side_effect = [live_payload(18), live_payload(19)]
        first, second = sources.read_codex()[0], sources.read_codex()[0]
        self.assertEqual((first.percent, second.percent), (18, 19))
        self.assertEqual(second.source, "api")
        self.assertFalse(second.stale)
        self.assertIsNotNone(second.observed_at)
        self.assertEqual(self.query.call_count, 2)

    def test_live_account_overrides_old_cache(self):
        sources._save_cache("chatgpt", [sources.Reading("chatgpt", "wk", 16, source="log")])
        result = sources.read_codex()[0]
        self.assertEqual(result.percent, 18)
        self.assertFalse(result.stale)

    def test_codex_bucket_has_priority_over_legacy_and_other_buckets(self):
        payload = live_payload(18)
        payload["rateLimits"] = live_payload(16)["rateLimitsByLimitId"]["codex"]
        payload["rateLimitsByLimitId"]["spark"] = {
            "limitId": "spark", "primary": {"usedPercent": 99, "windowDurationMins": 300}}
        self.assertEqual(sources._parse_codex_live(payload)[0].percent, 18)

    def test_other_bucket_cannot_be_mistaken_for_codex(self):
        self.assertEqual(sources._parse_codex_live({"rateLimits": {
            "limitId": "spark", "primary": {"usedPercent": 99}}}), [])

    def test_window_labels_and_order_follow_account_window_lengths(self):
        payload = live_payload(18)
        limits = payload["rateLimitsByLimitId"]["codex"]
        limits["secondary"] = {"usedPercent": 27, "windowDurationMins": 300}
        rows = sources._parse_codex_live(payload)
        self.assertEqual([(r.label, r.percent) for r in rows], [("5h", 27), ("wk", 18)])
        # 旧版账户响应只有 rateLimits，也必须保持同样的窗口含义。
        rows = sources._parse_codex_live({"rateLimits": limits})
        self.assertEqual([(r.label, r.percent) for r in rows], [("5h", 27), ("wk", 18)])

    def test_credit_rows_only_appear_when_enabled(self):
        for credits, expected in (({"hasCredits": False, "balance": "0"}, None),
                                  ({"hasCredits": True, "balance": "12.50"}, "12.50"),
                                  ({"hasCredits": True, "unlimited": True}, "∞")):
            with self.subTest(credits=credits):
                payload = live_payload(18)
                payload["rateLimitsByLimitId"]["codex"]["credits"] = credits
                rows = sources._parse_codex_live(payload)
                balances = [r.text for r in rows if r.label == "余额"]
                self.assertEqual(balances, [] if expected is None else [expected])

    def test_failure_preserves_last_live_sample_and_marks_it_stale(self):
        sampled = time.time()
        sources._save_cache("chatgpt", [sources.Reading(
            "chatgpt", "wk", 18, source="api", observed_at=sampled)])
        self.query.side_effect = TimeoutError("Codex 额度查询超时")
        result = sources.read_codex()[0]
        self.assertEqual(result.percent, 18)
        self.assertEqual(result.observed_at, sampled)
        self.assertTrue(result.stale)
        self.assertIn("超时", result.detail)
        self.assertEqual(display.resolve(result).note, "⟳")

    def test_missing_cli_without_account_cache_shows_unknown(self):
        self.query.side_effect = FileNotFoundError("Codex is missing")
        result = sources.read_codex()[0]
        self.assertIsNone(result.percent)
        self.assertNotEqual(result.source, "log")
        self.assertTrue(result.stale)
        self.assertIn("查询失败", display.details([result]))

    def test_invalid_live_values_are_not_displayed_as_success(self):
        for percent in (None, "invalid", False, float("nan"), float("inf")):
            with self.subTest(percent=percent):
                sources._retry_states["chatgpt"].clear()
                self.query.return_value = live_payload(percent)
                result = sources.read_codex()[0]
                self.assertIsNone(result.percent)
                self.assertTrue(result.stale)
                self.assertIsNotNone(result.error)

    def test_claude_failure_does_not_block_live_codex(self):
        with patch.object(sources, "read_claude", side_effect=ValueError("bad credentials")):
            readings = sources.read_all()
        self.assertIsNotNone(readings[0].error)
        self.assertEqual(readings[1].percent, 18)
        self.assertFalse(readings[1].stale)

    def test_details_explain_used_remaining_and_sample_time(self):
        text = display.details(sources.read_codex())
        self.assertIn("已用 18% / 剩余 82%", text)
        self.assertIn("采样时间", text)
        self.assertIn("账户查询", text)

    def test_cached_credit_balance_also_gets_stale_marker(self):
        reading = sources.Reading("chatgpt", "余额", None, text="12", stale=True,
                                  observed_at=time.time(), source="api")
        self.assertEqual(display.resolve(reading).note, "⟳")


class TransportTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.server = self.state / "server.py"
        self.trace = self.state / "requests.jsonl"
        self.real_popen = subprocess.Popen
        self.enterContext(patch.object(codex_live, "find_codex", return_value=sys.executable))
        self.spawn = self.enterContext(patch.object(codex_live.subprocess, "Popen", side_effect=self.popen))
        self.client = codex_live.CodexClient(str(self.state / "db"))
        self.addCleanup(self.client.close)
        self.write_server("normal")

    def popen(self, args, **kwargs):
        return self.real_popen([sys.executable, "-u", str(self.server), str(self.trace)], **kwargs)

    def write_server(self, behavior):
        self.server.write_text("""
import json, sys, time
behavior = """ + repr(behavior) + """
count = 0
for line in sys.stdin:
    request = json.loads(line)
    with open(sys.argv[1], 'a', encoding='utf-8') as trace:
        trace.write(json.dumps(request) + '\\n')
    method = request['method']
    if method == 'initialized':
        continue
    if method == 'initialize':
        result = {'userAgent': 'test'}
    elif method == 'account/read':
        assert request['params'] == {'refreshToken': False}
        result = {'account': None if behavior == 'unauthenticated' else {'type': 'chatgpt'}}
    else:
        assert method == 'account/rateLimits/read', method
        if behavior == 'timeout':
            time.sleep(5)
        if behavior == 'exit':
            sys.exit(3)
        if behavior == 'error':
            print(json.dumps({'id': request['id'], 'error': {'code': -1, 'message': 'private-detail'}}), flush=True)
            continue
        count += 1
        result = {'sample': count}
    print(json.dumps({'method': 'account/rateLimits/updated', 'params': {}}), flush=True)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
""", encoding="utf-8")

    def test_one_process_two_fresh_queries_and_clean_shutdown(self):
        self.assertEqual(self.client.read_rate_limits(3), {"sample": 1})
        self.assertEqual(self.client.read_rate_limits(3), {"sample": 2})
        self.spawn.assert_called_once()
        process = self.client._process
        self.client.close()
        self.assertIsNotNone(process.poll())
        methods = [json.loads(line)["method"] for line in self.trace.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(methods, ["initialize", "initialized", "account/read", "account/rateLimits/read",
                                   "account/read", "account/rateLimits/read"])

    def test_unauthenticated_client_does_not_request_quota(self):
        self.write_server("unauthenticated")
        with self.assertRaises(codex_live.CodexLiveError) as caught:
            self.client.read_rate_limits(3)
        self.assertEqual(caught.exception.kind, "auth")
        self.assertNotIn("account/rateLimits/read", self.trace.read_text(encoding="utf-8"))

    def test_timeout_reaps_process_and_next_query_reconnects(self):
        self.write_server("timeout")
        with self.assertRaises(TimeoutError):
            self.client.read_rate_limits(.2)
        self.assertIsNone(self.client._process)
        self.write_server("normal")
        self.assertEqual(self.client.read_rate_limits(3), {"sample": 1})
        self.assertEqual(self.spawn.call_count, 2)

    def test_server_exit_is_reported_without_hanging(self):
        self.write_server("exit")
        with self.assertRaises(codex_live.CodexLiveError):
            self.client.read_rate_limits(3)
        self.assertIsNone(self.client._process)

    def test_rpc_error_does_not_expose_raw_account_error(self):
        self.write_server("error")
        with self.assertRaises(codex_live.CodexLiveError) as caught:
            self.client.read_rate_limits(3)
        self.assertNotIn("private-detail", str(caught.exception))
        self.assertIsNone(self.client._process)
        diagnostic = json.loads((self.state / "codex-failure.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["phase"], "read_rate_limits")
        self.assertNotIn("private-detail", json.dumps(diagnostic))


if __name__ == "__main__":
    unittest.main()
