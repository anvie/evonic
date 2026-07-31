"""Tests for the Muktamar registration token verification endpoint.

The endpoint lives in routes/skills.py at
/api/internal/skills/muktamar-agent/verify-token.  These tests create a
minimal Flask app with ONLY the skills blueprint to avoid pulling in
full app dependencies.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMuktamarTokenVerification(unittest.TestCase):
    """Unit tests for the verify-token loopback endpoint."""

    @classmethod
    def setUpClass(cls):
        """Build a minimal Flask test app once for all tests."""
        # Import and patch before the blueprint registers its routes.
        import routes.skills as skills_mod

        cls._original_get_skill_config = skills_mod.skills_manager.get_skill_config

        from flask import Flask

        app = Flask(__name__)
        app.secret_key = "test-secret"
        app.register_blueprint(skills_mod.skills_bp)
        app.testing = True
        cls._app = app
        cls._skills = skills_mod

    @classmethod
    def tearDownClass(cls):
        cls._skills.skills_manager.get_skill_config = cls._original_get_skill_config

    def _set_config(self, token):
        """Patch the skills manager to return the given token for muktamar-agent."""

        def _mock_config(skill_id):
            if skill_id == "muktamar-agent":
                return {"REGISTRATION_API_TOKEN": token}
            return {}

        self._skills.skills_manager.get_skill_config = _mock_config

    # ── loopback gating ────────────────────────────────────────────────

    def test_rejects_non_loopback_request(self):
        """Non-loopback IPs must receive 403."""
        self._set_config("test-token")
        with self._app.test_client() as client:
            resp = client.post(
                "/api/internal/skills/muktamar-agent/verify-token",
                headers={"X-API-Token": "test-token"},
                environ_base={"REMOTE_ADDR": "192.168.1.1"},
            )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.get_json()["valid"])

    def test_accepts_loopback_v4_request(self):
        """127.0.0.1 must be accepted (returns 200 or 401 depending on token)."""
        self._set_config("test-token")
        with self._app.test_client() as client:
            resp = client.post(
                "/api/internal/skills/muktamar-agent/verify-token",
                headers={"X-API-Token": "test-token"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["valid"])

    def test_accepts_loopback_v6_request(self):
        """::1 must be accepted."""
        self._set_config("test-token")
        with self._app.test_client() as client:
            resp = client.post(
                "/api/internal/skills/muktamar-agent/verify-token",
                headers={"X-API-Token": "test-token"},
                environ_base={"REMOTE_ADDR": "::1"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["valid"])

    # ── response safety ────────────────────────────────────────────────

    def test_response_body_never_contains_token(self):
        """The response body must never echo the token value."""
        self._set_config("s3cret-t0k3n")
        with self._app.test_client() as client:
            resp = client.post(
                "/api/internal/skills/muktamar-agent/verify-token",
                headers={"X-API-Token": "wrong-token"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(resp.status_code, 401)
        body = str(resp.get_json())
        self.assertNotIn("s3cret-t0k3n", body)

    # ── token comparison ───────────────────────────────────────────────

    def test_verify_token_valid(self):
        """Correct token returns 200 and valid=True."""
        self._set_config("correct-token")
        with self._app.test_client() as client:
            resp = client.post(
                "/api/internal/skills/muktamar-agent/verify-token",
                headers={"X-API-Token": "correct-token"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["valid"])

    def test_verify_token_invalid(self):
        """Wrong token returns 401 and valid=False."""
        self._set_config("service-token")
        with self._app.test_client() as client:
            resp = client.post(
                "/api/internal/skills/muktamar-agent/verify-token",
                headers={"X-API-Token": "wrong-token"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.get_json()["valid"])

    def test_verify_token_missing_header(self):
        """Missing X-API-Token header also returns invalid."""
        self._set_config("service-token")
        with self._app.test_client() as client:
            resp = client.post(
                "/api/internal/skills/muktamar-agent/verify-token",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.get_json()["valid"])

    def test_verify_token_empty_config(self):
        """When no token is configured, any supplied token fails."""
        self._set_config("")
        with self._app.test_client() as client:
            resp = client.post(
                "/api/internal/skills/muktamar-agent/verify-token",
                headers={"X-API-Token": "anything"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.get_json()["valid"])

    def test_verify_token_no_get(self):
        """GET must return 405 (only POST is allowed)."""
        self._set_config("test")
        with self._app.test_client() as client:
            resp = client.get(
                "/api/internal/skills/muktamar-agent/verify-token",
                headers={"X-API-Token": "test"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(resp.status_code, 405)


if __name__ == "__main__":
    unittest.main()
