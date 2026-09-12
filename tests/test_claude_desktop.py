"""Desktop-only auth and discovery; synthetic credentials, no network or user data."""
import base64
import json
import os
import sys
import unittest
from unittest.mock import patch

import claude_auth
import claude_desktop as desktop
import sources
import windows_crypto
from quota_policy import QueryError, RetryState
from test_regressions import IsolatedTest


def entry(token="test-desktop-token", expiry=9000000):
    return {"token": token, "expiresAt": expiry, "refreshToken": "never-copy-this-grant"}


def cache_key(account="active", org="org-one", scope="user:profile user:inference user:sessions:claude_code"):
    return f"acct:{account}|client:{org}:https://api.anthropic.com:{scope}"


class DesktopTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.root = self.state / "desktop"
        self.root.mkdir()
        self.config = self.root / "config.json"
        self.enterContext(patch.object(desktop, "data_roots", return_value=[self.root]))
        self.enterContext(patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "", "AIFUEL_CLAUDE_EXE": ""}))
        self.clock = self.enterContext(patch.object(desktop.time, "time", return_value=2000.0))
        self.entries = {cache_key(): entry()}
        self.decrypt = self.enterContext(patch.object(desktop, "_decrypt_cache", return_value=self.entries))
        self.write_config()

    def write_config(self, **changes):
        data = {"lastKnownAccountUuid": "active", "oauth:tokenCacheV2": "encrypted-test-cache"}
        data.update(changes)
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def test_desktop_only_queries_live_usage_without_cli_or_credential_writes(self):
        before = self.config.read_bytes()
        with patch.object(claude_auth.sys, "platform", "win32"), \
             patch.object(sources, "CLAUDE_CREDS", str(self.state / "missing-cli.json")), \
             patch.object(claude_auth, "_refresh") as refresh, \
             patch.object(sources, "_request_claude_usage", return_value={
                 "limits": [{"kind": "session", "percent": 27}]}) as query:
            self.assertEqual(sources._fetch_claude(1)[0].percent, 27)
        self.assertEqual(query.call_args.args[0], "test-desktop-token")
        refresh.assert_not_called()
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse((self.state / "missing-cli.json").exists())

    def test_existing_cli_login_keeps_its_account(self):
        path = self.state / "cli.json"
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "cli-account", "expiresAt": 9000000}}), encoding="utf-8")
        self.assertEqual(claude_auth.access_token(str(path), str(self.state)), "cli-account")
        self.decrypt.assert_not_called()
        path.write_text("invalid", encoding="utf-8")
        with self.assertRaises(QueryError):
            claude_auth.access_token(str(path), str(self.state))
        self.decrypt.assert_not_called()

    def test_explicit_config_and_unreadable_cli_do_not_switch_accounts(self):
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(self.state)):
            self.assertFalse(claude_auth.uses_desktop(str(self.state / "missing.json")))
        with patch.object(claude_auth.os, "stat", side_effect=PermissionError):
            self.assertFalse(claude_auth.uses_desktop("unreadable"))

    def test_other_account_and_non_usage_scopes_are_ignored(self):
        self.entries[cache_key(account="other")] = entry("wrong-account", 99000000)
        self.entries[cache_key(scope="user:office user:inference")] = entry("office", 99000000)
        self.assertEqual(desktop.access_token(), "test-desktop-token")

    def test_missing_active_account_does_not_choose_another_account(self):
        self.write_config(lastKnownAccountUuid=None)
        with self.assertRaises(QueryError):
            desktop.access_token()

    def test_legacy_single_account_cache_is_supported(self):
        value = self.entries.pop(cache_key())
        self.entries[cache_key().split("|", 1)[1]] = value
        self.write_config(lastKnownAccountUuid=None)
        self.assertEqual(desktop.access_token(), "test-desktop-token")

    def test_multiple_organizations_require_an_explicit_login(self):
        self.entries[cache_key(org="other-org")] = entry("wrong-org")
        with self.assertRaises(QueryError) as caught:
            desktop.access_token()
        self.assertIn("多个组织", str(caught.exception))

    def test_expired_and_rejected_desktop_grants_prompt_desktop_refresh(self):
        for expiry, rejected in ((1999000, None), (9000000, "test-desktop-token")):
            with self.subTest(expiry=expiry):
                self.entries[cache_key()] = entry(expiry=expiry)
                with self.assertRaises(QueryError) as caught:
                    desktop.access_token(rejected)
                self.assertEqual(caught.exception.kind, "auth")
                self.assertIn("Code 模式", str(caught.exception))
                self.assertNotIn("never-copy", str(caught.exception))

    def test_rejected_token_can_recover_from_desktop_rotation(self):
        self.assertEqual(desktop.access_token("previous-desktop-token"), "test-desktop-token")

    def test_invalid_or_revoked_entries_are_not_accepted(self):
        for value in (None, {}, entry(expiry=float("nan")), entry(expiry=True), entry(token=123)):
            with self.subTest(value=value):
                self.entries[cache_key()] = value
                with self.assertRaises(QueryError):
                    desktop.access_token()

    def test_decryption_errors_are_sanitized(self):
        self.decrypt.side_effect = OSError("sensitive-diagnostic")
        with self.assertRaises(QueryError) as caught:
            desktop.access_token()
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(caught.exception.kind, "auth")

    def test_window_settings_do_not_change_auth_fingerprint(self):
        original = desktop.credential_stamp()
        self.write_config(windowSize={"width": 200})
        self.assertEqual(desktop.credential_stamp(), original)
        self.write_config(**{"oauth:tokenCacheV2": "rotated"})
        self.assertNotEqual(desktop.credential_stamp(), original)

    def test_desktop_renewal_ends_auth_cooldown_but_window_movement_does_not(self):
        self.entries[cache_key()] = entry(expiry=1000000)
        with patch.object(claude_auth.sys, "platform", "win32"), \
             patch.object(sources, "CLAUDE_CREDS", str(self.state / "missing-cli.json")), \
             patch.object(sources, "CACHE_FILE", str(self.state / "quota.json")), \
             patch.object(sources, "_retry_states", {"claude": RetryState()}), \
             patch.object(sources, "_request_claude_usage", return_value={
                 "limits": [{"kind": "session", "percent": 27}]}) as query:
            first = sources.read_claude()[0]
            self.assertEqual(first.failure_kind, "auth")
            self.clock.return_value += 30
            self.write_config(windowSize={"width": 201})
            self.assertEqual(sources.read_claude()[0].retry_at, first.retry_at)
            query.assert_not_called()
            self.write_config(**{"oauth:tokenCacheV2": "rotated"})
            self.entries[cache_key()] = entry("renewed")
            self.assertFalse(sources.read_claude()[0].stale)
            query.assert_called_once()

    def test_latest_logged_out_install_does_not_fall_back_to_old_profile(self):
        old = self.state / "old"
        old.mkdir()
        (old / "config.json").write_bytes(self.config.read_bytes())
        os.utime(old / "config.json", (1000, 1000))
        self.write_config(**{"oauth:tokenCacheV2": None})
        with patch.object(desktop, "data_roots", return_value=[old, self.root]), self.assertRaises(QueryError):
            desktop.access_token()
        self.decrypt.assert_not_called()

    def test_401_after_desktop_query_retries_only_once(self):
        with patch.object(claude_auth.sys, "platform", "win32"), \
             patch.object(sources, "CLAUDE_CREDS", str(self.state / "missing-cli.json")), \
             patch.object(sources, "_request_claude_usage", side_effect=QueryError("expired", "auth")) as query, \
             patch.object(claude_auth, "_refresh") as refresh, self.assertRaises(QueryError):
            sources._fetch_claude(1)
        query.assert_called_once()  # Same rejected token is never sent a second time.
        refresh.assert_not_called()


class DesktopDiscoveryTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(desktop, "data_roots", return_value=[self.state]))
        self.enterContext(patch.dict(os.environ, AIFUEL_CLAUDE_EXE=""))
        self.enterContext(patch.object(claude_auth.shutil, "which", return_value=None))
        self.enterContext(patch.object(claude_auth.os.path, "expanduser", return_value=str(self.state / "home")))
        self.enterContext(patch.object(claude_auth.sys, "platform", "win32"))

    def component(self, version):
        path = self.state / "claude-code" / version / "claude.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_desktop_only_discovers_latest_native_component_and_rediscovers_updates(self):
        old, new = self.component("2.1.260"), self.component("2.1.266")
        os.utime(old, (1000, 1000))
        self.assertEqual(claude_auth.find_claude(), str(new))
        new.unlink()
        self.assertEqual(claude_auth.find_claude(), str(old))

    def test_gui_executable_is_never_launched_as_cli(self):
        (self.state / "claude.exe").touch()
        self.component("app")
        with self.assertRaises(QueryError) as caught:
            claude_auth.find_claude()
        self.assertEqual(caught.exception.kind, "dependency")

    def test_explicit_cli_and_standalone_cli_keep_priority(self):
        desktop_cli = self.component("2.1.266")
        installed = self.state / "home" / ".local" / "bin" / "claude.exe"
        installed.parent.mkdir(parents=True)
        installed.touch()
        self.assertEqual(claude_auth.find_claude(), str(installed))
        with patch.dict(os.environ, AIFUEL_CLAUDE_EXE=str(desktop_cli)):
            self.assertEqual(claude_auth.find_claude(), str(desktop_cli))


@unittest.skipUnless(sys.platform == "win32", "Windows system cryptography")
class WindowsCryptoTests(unittest.TestCase):
    def test_aes256_gcm_known_vector_and_tamper_rejection(self):
        cipher = bytes.fromhex("cea7403d4d606b6e074ec5d3baf39d18")
        tag = bytes.fromhex("d0d1c8a799996bf0265b98b5d48ab919")
        self.assertEqual(windows_crypto.decrypt_gcm(bytes(32), bytes(12), cipher, tag), bytes(16))
        for bad_cipher, bad_tag in ((cipher, bytes(16)), (bytes(16), tag)):
            with self.assertRaises(OSError):
                windows_crypto.decrypt_gcm(bytes(32), bytes(12), bad_cipher, bad_tag)


class EncryptedCacheTests(IsolatedTest):
    def test_v2_revocation_overrides_legacy_and_format_is_checked(self):
        (self.state / "Local State").write_text(json.dumps({"os_crypt": {
            "encrypted_key": base64.b64encode(b"DPAPIwrapped-key").decode()}}), encoding="utf-8")
        encrypted = b"v10" + bytes(12) + b"ciphertext" + bytes(16)
        config = {k: base64.b64encode(encrypted).decode() for k in desktop._CACHE_NAMES}
        with patch.object(windows_crypto, "unprotect", return_value=bytes(32)) as unprotect, \
             patch.object(windows_crypto, "decrypt_gcm", side_effect=[
                 json.dumps({cache_key(): entry()}).encode(), json.dumps({cache_key(): None}).encode()]) as decrypt:
            self.assertIsNone(desktop._decrypt_cache(config, self.state)[cache_key()])
        unprotect.assert_called_once_with(b"wrapped-key")
        self.assertEqual(decrypt.call_args.args[1:], (bytes(12), b"ciphertext", bytes(16)))
        config["oauth:tokenCache"] = base64.b64encode(b"v99unknown-format").decode()
        with patch.object(windows_crypto, "unprotect", return_value=bytes(32)), self.assertRaises(ValueError):
            desktop._decrypt_cache(config, self.state)
