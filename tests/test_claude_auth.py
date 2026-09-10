"""自动续期与 401 恢复；使用隔离凭证和本地假 CLI，不连接账户服务。"""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
from unittest.mock import Mock, patch

import claude_auth
import display
import sources
from quota_policy import QueryError, RetryState
from test_regressions import IsolatedTest


FAKE_CLI = r'''
import json, os, sys, time
from pathlib import Path
mode = os.environ['AIFUEL_TEST_AUTH_MODE']
path = Path(os.environ['CLAUDE_CONFIG_DIR']) / '.credentials.json'
requests = [json.loads(sys.stdin.readline())]
assert requests[0]['type'] == 'control_request'
assert requests[0]['request']['subtype'] == 'initialize'
if mode == 'force':
    requests.append(json.loads(sys.stdin.readline()))
    assert requests[-1]['request']['subtype'] == 'get_usage'
if mode == 'exit':
    sys.exit(1)
if mode in ('refresh', 'force', 'revoked'):
    time.sleep(.15)
    data = json.loads(path.read_text(encoding='utf-8'))
    data['claudeAiOauth'].update(accessToken='renewed-test-token',
                               refreshToken='renewed-test-refresh', expiresAt=(time.time()+28800)*1000)
    if mode == 'revoked':
        data['claudeAiOauth'].update(accessToken='', refreshToken='', expiresAt=0)
    staged = path.with_suffix('.tmp')
    staged.write_text(json.dumps(data), encoding='utf-8')
    os.replace(staged, path)
# 大量诊断输出不应阻塞助手，也不能泄露到 aifuel 界面或日志。
sys.stdout.write('private-test-output' * 20000)
sys.stdout.flush()
remaining = sys.stdin.read()
assert not remaining, 'Unexpected additional request'
Path('probe.json').write_text(json.dumps({'requests': requests, 'argv': sys.argv[1:],
    'eof': True, 'overrides_cleared': all(key not in os.environ for key in
        ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN'])}))
'''


class ClaudeAuthTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.credentials = self.state / '.credentials.json'
        self.write_credentials(expired=True)
        self.helper = self.state / 'fake_claude.py'
        self.helper.write_text(FAKE_CLI, encoding='utf-8')
        command = claude_auth._command
        self.enterContext(patch.object(claude_auth, 'find_claude', return_value=sys.executable))
        self.enterContext(patch.object(claude_auth, '_command',
                                     side_effect=lambda: [sys.executable, str(self.helper), *command()[1:]]))
        self.enterContext(patch.dict(os.environ, AIFUEL_TEST_AUTH_MODE='refresh'))
        self.launch = self.enterContext(patch.object(claude_auth.subprocess, 'Popen',
                                                    wraps=subprocess.Popen))

    def write_credentials(self, expired, refresh='test-refresh'):
        self.credentials.write_text(json.dumps({'otherSetting': 'preserved', 'claudeAiOauth': {
            'accessToken': 'old-test-token', 'refreshToken': refresh,
            'expiresAt': (time.time() + (-10 if expired else 10000)) * 1000}}), encoding='utf-8')

    def token(self, **kwargs):
        return claude_auth.access_token(str(self.credentials), str(self.state), timeout=3, **kwargs)

    def test_valid_token_needs_no_helper_or_credential_write(self):
        self.write_credentials(expired=False)
        original = self.credentials.read_bytes()
        self.assertEqual(self.token(), 'old-test-token')
        self.launch.assert_not_called()
        self.assertEqual(self.credentials.read_bytes(), original)
        self.assertNotIn('old-test-token', repr(claude_auth._read(self.credentials)))

    def test_expired_token_refreshes_without_prompt_or_inference(self):
        with patch.dict(os.environ, ANTHROPIC_API_KEY='private-test-key',
                        ANTHROPIC_AUTH_TOKEN='private-test-auth', ANTHROPIC_BASE_URL='https://example.test',
                        CLAUDE_CODE_OAUTH_TOKEN='private-test-oauth'):
            self.assertEqual(self.token(), 'renewed-test-token')
        self.launch.assert_called_once()
        report = json.loads((self.state / 'claude-auth-helper/probe.json').read_text())
        self.assertEqual([r['request']['subtype'] for r in report['requests']], ['initialize'])
        self.assertTrue(report['eof'] and report['overrides_cleared'])
        args = report['argv']
        self.assertIn('--no-session-persistence', args)
        self.assertIn('--setting-sources=', args)
        self.assertIn('--strict-mcp-config', args)
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertTrue(json.loads(args[args.index('--settings') + 1])['disableAllHooks'])
        self.assertEqual(json.loads(self.credentials.read_text())['otherSetting'], 'preserved')

    def test_concurrent_expiry_uses_one_official_helper(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.token(), range(2)))
        self.assertEqual(results, ['renewed-test-token'] * 2)
        self.launch.assert_called_once()

    def test_rejected_token_asks_official_usage_flow_to_refresh(self):
        self.write_credentials(expired=False)
        with patch.dict(os.environ, AIFUEL_TEST_AUTH_MODE='force'):
            self.assertEqual(self.token(rejected_token='old-test-token'), 'renewed-test-token')
        report = json.loads((self.state / 'claude-auth-helper/probe.json').read_text())
        self.assertEqual([r['request']['subtype'] for r in report['requests']], ['initialize', 'get_usage'])

    def test_other_client_already_renewed_rejected_token(self):
        self.write_credentials(expired=False)
        self.assertEqual(self.token(rejected_token='previous-client-token'), 'old-test-token')
        self.launch.assert_not_called()

    def test_missing_refresh_grant_is_auth_failure_without_launching(self):
        self.write_credentials(expired=True, refresh='')
        with self.assertRaises(QueryError) as caught:
            self.token()
        self.assertEqual(caught.exception.kind, 'auth')
        self.launch.assert_not_called()

    def test_revoked_grant_is_reported_after_official_recovery_attempt(self):
        with patch.dict(os.environ, AIFUEL_TEST_AUTH_MODE='revoked'), self.assertRaises(QueryError) as caught:
            self.token()
        self.assertEqual(caught.exception.kind, 'auth')
        self.launch.assert_called_once()

    def test_timeout_preserves_credentials_and_does_not_demand_login(self):
        original = self.credentials.read_bytes()
        with patch.dict(os.environ, AIFUEL_TEST_AUTH_MODE='wait'), self.assertRaises(QueryError) as caught:
            claude_auth.access_token(str(self.credentials), str(self.state), timeout=.5)
        self.assertEqual(caught.exception.kind, 'temporary')
        self.assertEqual(self.credentials.read_bytes(), original)
        self.assertTrue(json.loads((self.state / 'claude-auth-helper/probe.json').read_text())['eof'])

    def test_early_exit_is_retryable_and_keeps_private_output_out_of_error(self):
        with patch.dict(os.environ, AIFUEL_TEST_AUTH_MODE='exit'), self.assertRaises(QueryError) as caught:
            self.token()
        self.assertEqual(caught.exception.kind, 'temporary')
        self.assertNotIn('private', str(caught.exception))

    def test_unresponsive_helper_is_killed_and_reaped(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired('fake-claude', 5), 0]
        claude_auth._stop(process)
        process.stdin.close.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_401_renews_and_retries_account_query_once(self):
        self.write_credentials(expired=False)
        with patch.object(sources, 'CLAUDE_CREDS', str(self.credentials)), \
             patch.object(sources, 'data_dir', return_value=str(self.state)), \
             patch.dict(os.environ, AIFUEL_TEST_AUTH_MODE='force'), \
             patch.object(sources.urllib.request, 'urlopen', side_effect=[
                 urllib.error.HTTPError('https://example.test', 401, 'private', {}, io.BytesIO()),
                 io.BytesIO(json.dumps({'limits': [{'kind': 'session', 'percent': 27}]}).encode())]) as query:
            readings = sources._fetch_claude(1)
        self.assertEqual(readings[0].percent, 27)
        self.assertEqual(query.call_count, 2)
        self.assertEqual([c.args[0].get_header('Authorization') for c in query.call_args_list],
                         ['Bearer old-test-token', 'Bearer renewed-test-token'])
        self.launch.assert_called_once()

    def test_failed_renewal_uses_retry_policy_instead_of_login_state(self):
        with patch.object(sources, 'CLAUDE_CREDS', str(self.credentials)), \
             patch.object(sources, 'data_dir', return_value=str(self.state)), \
             patch.object(sources, 'CACHE_FILE', str(self.state / 'cache.json')), \
             patch.object(sources, '_retry_states', {'claude': RetryState(), 'chatgpt': RetryState()}), \
             patch.dict(os.environ, AIFUEL_TEST_AUTH_MODE='exit'), \
             patch.object(sources.urllib.request, 'urlopen') as query:
            first = sources.read_claude()[0]
            second = sources.read_claude()[0]
        self.assertEqual(first.failure_kind, 'temporary')
        self.assertEqual(display.status_text(first), '查询失败')
        self.assertEqual(first.retry_at, second.retry_at)
        self.launch.assert_called_once()
        query.assert_not_called()

    def test_401_after_recovery_does_not_loop_or_launch_another_helper(self):
        self.write_credentials(expired=False)
        with patch.object(sources, 'CLAUDE_CREDS', str(self.credentials)), \
             patch.object(sources, 'data_dir', return_value=str(self.state)), \
             patch.dict(os.environ, AIFUEL_TEST_AUTH_MODE='force'), \
             patch.object(sources.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError(
                 'https://example.test', 401, 'private', {}, io.BytesIO())) as query, \
             self.assertRaises(QueryError) as caught:
            sources._fetch_claude(1)
        self.assertEqual(caught.exception.kind, 'auth')
        self.assertEqual(query.call_count, 2)
        self.launch.assert_called_once()
