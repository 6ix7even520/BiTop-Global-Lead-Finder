import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import siwc


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class SIWCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = (siwc.AUTH_FILE, siwc.HOST_FILE, siwc.SETTINGS_FILE, siwc.ROOT)
        siwc.ROOT = root
        siwc.AUTH_FILE = root / "chatgpt_auth.bin"
        siwc.HOST_FILE = root / "chatgpt_host_id.txt"
        siwc.SETTINGS_FILE = root / "app_settings.json"
        siwc.PENDING.clear()

    def tearDown(self):
        siwc.AUTH_FILE, siwc.HOST_FILE, siwc.SETTINGS_FILE, siwc.ROOT = self.paths
        self.tmp.cleanup()
        siwc.PENDING.clear()

    def test_encrypted_store_roundtrip(self):
        sample = {"version": 2, "profiles": [], "active_profile_id": "", "selected_models": {}}
        siwc.save_store(sample)
        self.assertEqual(siwc.load_store(), sample)
        raw = siwc.AUTH_FILE.read_bytes()
        self.assertTrue(raw.startswith(b"FERNET1") or raw.startswith(b"DPAPI1"))

    def test_host_id_is_stable(self):
        first = siwc._new_host_id()
        second = siwc._new_host_id()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("urn:uuid:"))

    @patch.object(siwc, "verify_public_repo", return_value={"url": "https://github.com/example/bitop", "license": "MIT", "name": "example/bitop"})
    def test_start_signin_uses_pkce_and_required_scopes(self, _repo):
        url = siwc.start_signin("https://github.com/example/bitop", "http://127.0.0.1:8765/auth/callback")
        from urllib.parse import urlsplit, parse_qs
        q = parse_qs(urlsplit(url).query)
        self.assertEqual(q["client_id"][0], "dynamic_agent_client")
        self.assertEqual(q["code_challenge_method"][0], "S256")
        self.assertIn("chatgpt.tokens.use.direct", q["scope"][0])
        self.assertEqual(q["redirect_uri"][0], "http://127.0.0.1:8765/auth/callback")
        self.assertIn(q["state"][0], siwc.PENDING)

    def test_callback_stores_validated_profile_and_scope(self):
        state = "test-state"
        siwc.PENDING[state] = {
            "nonce": "nonce-123", "verifier": "pkce-verifier", "redirect_uri": "http://127.0.0.1:8765/auth/callback",
            "client_id": "dynamic_agent_client", "is_new": True, "profile_id": "", "existing_subject": "",
            "repo": {"url": "https://github.com/example/bitop", "license": "MIT"}, "created_at": time.time()
        }
        token_data = {"access_token": "access-test", "refresh_token": "refresh-test", "id_token": "id-token-test",
                      "token_type": "Bearer", "expires_in": 3600,
                      "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"}
        with patch.object(siwc, "_post_form", return_value=token_data), patch.object(siwc, "_validate_id_token", return_value={"sub": "subject-1", "email": "buyer@example.com", "name": "Buyer"}):
            result = siwc.complete_callback({"state": [state], "code": ["auth-code"], "client_id": ["oaiapp_test"]})
        self.assertTrue(result["plan_usage_enabled"])
        status = siwc.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["email"], "buyer@example.com")
        self.assertEqual(len(status["accounts"]), 1)
        self.assertEqual(siwc.load_store()["profiles"][0]["client_id"], "oaiapp_test")

    def test_refresh_rotates_tokens(self):
        profile = {"profile_id": "p1", "client_id": "oaiapp_1", "subject": "sub", "status": "connected",
                   "access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 0,
                   "scopes": [siwc.SHARING_SCOPE]}
        siwc.save_store({"version": 2, "profiles": [profile], "active_profile_id": "p1", "selected_models": {}})
        with patch.object(siwc, "_post_form", return_value={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}):
            token = siwc.get_access_token()
        self.assertEqual(token, "new-access")
        self.assertEqual(siwc.load_store()["profiles"][0]["refresh_token"], "new-refresh")

    def test_responses_stream_requires_completed_event(self):
        stream = b'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":true}"}\n\ndata: {"type":"response.completed","response":{}}\n\n'
        with patch.object(siwc, "get_access_token", return_value="test-token"), patch("siwc.urllib.request.urlopen", return_value=FakeResponse(stream)):
            result = siwc.responses_text("model-slug", "instructions", "user input")
        self.assertEqual(result, '{"ok":true}')

    def test_active_profile_is_used_without_account_rotation(self):
        p1 = {"profile_id": "p1", "client_id": "c1", "status": "connected", "access_token": "a1", "refresh_token": "r1", "expires_at": time.time()+3600, "scopes": [siwc.SHARING_SCOPE], "email": "one@example.com"}
        p2 = {"profile_id": "p2", "client_id": "c2", "status": "connected", "access_token": "a2", "refresh_token": "r2", "expires_at": time.time()+3600, "scopes": [siwc.SHARING_SCOPE], "email": "two@example.com"}
        siwc.save_store({"version": 2, "profiles": [p1,p2], "active_profile_id": "p1", "selected_models": {"p1":"model-1", "p2":"model-2"}})
        self.assertEqual(siwc.status()["email"], "one@example.com")
        self.assertEqual(siwc.status()["selected_model"], "model-1")
        self.assertEqual(len(siwc.status()["accounts"]), 1)

    def test_missing_token_scope_does_not_fall_back_to_callback_scope(self):
        state = "scope-state"
        siwc.PENDING[state] = {
            "nonce": "nonce-scope", "verifier": "pkce-verifier", "redirect_uri": "http://127.0.0.1:8765/auth/callback",
            "client_id": "dynamic_agent_client", "is_new": True, "profile_id": "", "existing_subject": "",
            "repo": {"url": "https://github.com/example/bitop", "license": "MIT"}, "created_at": time.time()
        }
        token_data = {"access_token": "access-test", "refresh_token": "refresh-test", "id_token": "id-token-test",
                      "token_type": "Bearer", "expires_in": 3600}
        with patch.object(siwc, "_post_form", return_value=token_data), patch.object(siwc, "_validate_id_token", return_value={"sub": "subject-scope", "email": "buyer@example.com"}):
            result = siwc.complete_callback({"state": [state], "code": ["auth-code"], "client_id": ["oaiapp_scope"], "scope": ["openid profile email resource.invoke chatgpt.tokens.use.direct"]})
        self.assertFalse(result["plan_usage_enabled"])
        self.assertTrue(siwc.status()["connected"])
        self.assertFalse(siwc.status()["plan_usage_enabled"])

    def test_legacy_single_profile_store_migrates(self):
        old = {"version": 1, "profile": {"client_id": "old-client", "status": "connected", "email": "old@example.com"}, "selected_model": "old-model"}
        # Save a protected legacy blob directly to exercise the migration path.
        siwc.AUTH_FILE.write_bytes(siwc._protect(json.dumps(old).encode()))
        migrated = siwc.load_store()
        self.assertEqual(len(migrated["profiles"]), 1)
        self.assertEqual(migrated["profiles"][0]["client_id"], "old-client")
        self.assertEqual(migrated["selected_models"][migrated["active_profile_id"]], "old-model")

    def test_revocation_accepts_empty_http_200(self):
        profile = {"profile_id": "p1", "client_id": "c1", "status": "connected", "access_token": "a", "refresh_token": "r", "expires_at": time.time()+3600, "scopes": [siwc.SHARING_SCOPE]}
        siwc.save_store({"version": 2, "profiles": [profile], "active_profile_id": "p1", "selected_models": {"p1":"m"}})
        with patch.object(siwc, "_discovery", return_value={"revocation_endpoint": "https://auth.openai.com/revoke"}), patch.object(siwc, "_post_form", return_value={}):
            result = siwc.disconnect()
        self.assertTrue(result["remote_revoked"])
        self.assertFalse(siwc.status()["connected"])
        self.assertEqual(siwc.load_store()["profiles"][0]["client_id"], "c1")

    def test_public_repo_requires_public_license_and_source_files(self):
        repo_info = {"private": False, "license": {"spdx_id": "MIT"}, "full_name": "example/bitop"}
        contents = [{"name": n} for n in ["app.py", "siwc.py", "index.html", "privacy.html", "requirements.txt", "LICENSE"]]
        with patch("siwc.urllib.request.urlopen", side_effect=[FakeResponse(json.dumps(repo_info).encode()), FakeResponse(json.dumps(contents).encode())]):
            result = siwc.verify_public_repo("https://github.com/example/bitop")
        self.assertEqual(result["license"], "MIT")
        self.assertEqual(result["url"], "https://github.com/example/bitop")

    def test_private_repo_is_rejected(self):
        repo_info = {"private": True, "license": {"spdx_id": "MIT"}}
        with patch("siwc.urllib.request.urlopen", return_value=FakeResponse(json.dumps(repo_info).encode())):
            with self.assertRaisesRegex(ValueError, "Private"):
                siwc.verify_public_repo("https://github.com/example/private")


if __name__ == "__main__":
    unittest.main()
