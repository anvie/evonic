"""Tests for open redirect prevention in the auth blueprint."""
import os
import sys
import unittest
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash

TEST_PASSWORD = "test123"
TEST_PASSWORD_HASH = generate_password_hash(TEST_PASSWORD)


class TestOpenRedirectPrevention(unittest.TestCase):
    """Verify that the auth blueprint rejects unsafe redirect URLs."""

    @classmethod
    def setUpClass(cls):
        """Set environment before any module imports."""
        os.environ.setdefault("SECRET_KEY", "test-redirect-secret")
        os.environ["ADMIN_PASSWORD_HASH"] = TEST_PASSWORD_HASH
        os.environ["TURNSTILE_SECRET_KEY"] = ""  # disable captcha for tests

    def setUp(self):
        # Reload config so it picks up env vars set in setUpClass.
        # (Other test classes may have imported config with stale values.)
        import config
        importlib.reload(config)
        # load_dotenv() during reload may overwrite our env var with .env values,
        # so force the config attribute to the test hash directly.
        config.ADMIN_PASSWORD_HASH = TEST_PASSWORD_HASH

        from routes.auth import auth_bp
        from flask import Flask

        template_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "templates",
        )
        self.app = Flask(__name__, template_folder=template_dir)
        self.app.secret_key = "test-redirect-secret"
        self.app.register_blueprint(auth_bp)
        self.client = self.app.test_client()
        
        # Clear rate limiter before each test to avoid rate limit issues
        from backend.rate_limiter import get_rate_limiter
        get_rate_limiter().clear_all()

    def _login(self, next_url):
        """Helper: POST to /login with given next_url and correct password."""
        return self.client.post(
            "/login",
            data={"password": TEST_PASSWORD, "next": next_url},
            follow_redirects=False,
        )

    def test_safe_relative_url_allowed(self):
        """A valid relative path like /dashboard should be allowed."""
        resp = self._login("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/dashboard")

    def test_root_path_allowed(self):
        """'/' is the default and must always work."""
        resp = self._login("/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    def test_absolute_http_url_rejected(self):
        """http://evil.com must NOT be allowed — falls back to '/'."""
        resp = self._login("http://evil.com")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    def test_absolute_https_url_rejected(self):
        """https://evil.com must NOT be allowed."""
        resp = self._login("https://evil.com")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    def test_protocol_relative_url_rejected(self):
        """//evil.com (protocol-relative) must NOT be allowed."""
        resp = self._login("//evil.com")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    def test_javascript_url_rejected(self):
        """javascript:alert(1) must not be allowed."""
        resp = self._login("javascript:alert(1)")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    def test_data_url_rejected(self):
        """data:text/html,... must not be allowed."""
        resp = self._login("data:text/html,<script>alert(1)</script>")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    def test_external_url_with_path_rejected(self):
        """http://evil.com/dashboard must not be allowed."""
        resp = self._login("http://evil.com/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    def test_get_login_redirect_safe_url_allowed(self):
        """GET /login?next=/dashboard (when authenticated) should redirect to /dashboard."""
        # First log in
        self._login("/")
        # Now make GET request with next param
        resp = self.client.get("/login?next=/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/dashboard")

    def test_get_login_redirect_unsafe_url_rejected(self):
        """GET /login?next=http://evil.com (when authenticated) must fall back to /."""
        self._login("/")
        resp = self.client.get("/login?next=http://evil.com", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

    def test_empty_next_falls_back_to_root(self):
        """Empty string for next should fall back to /."""
        resp = self._login("")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")


class TestIsSafeRedirectUrl(unittest.TestCase):
    """Unit tests for _is_safe_redirect_url helper directly."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from routes.auth import _is_safe_redirect_url
        self.check = _is_safe_redirect_url

    def test_safe_paths(self):
        self.assertTrue(self.check("/"))
        self.assertTrue(self.check("/dashboard"))
        self.assertTrue(self.check("/api/health"))
        self.assertTrue(self.check("/agents/linus/sessions"))

    def test_absolute_urls_rejected(self):
        self.assertFalse(self.check("http://evil.com"))
        self.assertFalse(self.check("https://evil.com"))
        self.assertFalse(self.check("HTTP://EVIL.COM"))
        self.assertFalse(self.check("ftp://files.example.com"))

    def test_protocol_relative_rejected(self):
        self.assertFalse(self.check("//evil.com"))
        self.assertFalse(self.check("//evil.com/path"))

    def test_non_slash_start_rejected(self):
        self.assertFalse(self.check("evil.com"))
        self.assertFalse(self.check("javascript:alert(1)"))
        self.assertFalse(self.check("data:text/html,<script>"))
        self.assertFalse(self.check(""))
