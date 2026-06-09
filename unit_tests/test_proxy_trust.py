"""Tests for PROXY_TRUST_COUNT configuration and IP resolution behaviour.

When Evonic is deployed without a reverse proxy (common for self-hosted
setups), ProxyFix must NOT trust X-Forwarded-For headers. Otherwise any
client can spoof its IP address, defeating login rate limiting and audit
logging.

PROXY_TRUST_COUNT=0  → direct deployment, ignore X-Forwarded-For
PROXY_TRUST_COUNT=1  → behind one proxy (default, backward-compatible)
PROXY_TRUST_COUNT=2  → behind two proxies (e.g. CDN → nginx → app)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestProxyTrustCountConfig(unittest.TestCase):
    """Verify that config.py reads PROXY_TRUST_COUNT correctly."""

    def test_default_is_one(self):
        """Default must be 1 for backward compatibility with existing deployments."""
        os.environ.pop('PROXY_TRUST_COUNT', None)
        import importlib
        import config as cfg
        importlib.reload(cfg)
        self.assertEqual(cfg.PROXY_TRUST_COUNT, 1)

    def test_zero_for_direct_deployment(self):
        """PROXY_TRUST_COUNT=0 must be accepted for direct (no-proxy) deployments."""
        os.environ['PROXY_TRUST_COUNT'] = '0'
        try:
            import importlib
            import config as cfg
            importlib.reload(cfg)
            self.assertEqual(cfg.PROXY_TRUST_COUNT, 0)
        finally:
            os.environ.pop('PROXY_TRUST_COUNT', None)

    def test_two_for_double_proxy(self):
        """PROXY_TRUST_COUNT=2 must be accepted for CDN + reverse-proxy setups."""
        os.environ['PROXY_TRUST_COUNT'] = '2'
        try:
            import importlib
            import config as cfg
            importlib.reload(cfg)
            self.assertEqual(cfg.PROXY_TRUST_COUNT, 2)
        finally:
            os.environ.pop('PROXY_TRUST_COUNT', None)

    def test_negative_clamped_to_zero(self):
        """Negative values must be clamped to 0."""
        os.environ['PROXY_TRUST_COUNT'] = '-1'
        try:
            import importlib
            import config as cfg
            importlib.reload(cfg)
            self.assertEqual(cfg.PROXY_TRUST_COUNT, 0)
        finally:
            os.environ.pop('PROXY_TRUST_COUNT', None)

    def test_invalid_falls_back_to_default(self):
        """Non-numeric values must fall back to the default (1)."""
        os.environ['PROXY_TRUST_COUNT'] = 'abc'
        try:
            import importlib
            import config as cfg
            importlib.reload(cfg)
            self.assertEqual(cfg.PROXY_TRUST_COUNT, 1)
        finally:
            os.environ.pop('PROXY_TRUST_COUNT', None)


class TestProxyTrustIpResolution(unittest.TestCase):
    """Verify that ProxyFix honours PROXY_TRUST_COUNT for IP resolution."""

    def _make_app(self, trust_count):
        """Build a minimal Flask app with the given proxy trust count."""
        os.environ.setdefault('SECRET_KEY', 'test-secret')
        os.environ['PROXY_TRUST_COUNT'] = str(trust_count)

        from flask import Flask, jsonify, request
        from werkzeug.middleware.proxy_fix import ProxyFix

        app = Flask(__name__)
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trust_count,
            x_proto=1,
            x_host=1,
            x_prefix=1,
        )

        @app.route('/ip')
        def ip():
            return jsonify({'ip': request.remote_addr})

        return app

    def tearDown(self):
        os.environ.pop('PROXY_TRUST_COUNT', None)
        # Reload config to restore defaults
        import importlib
        import config as cfg
        importlib.reload(cfg)

    def test_trust_zero_ignores_xff(self):
        """With trust=0, X-Forwarded-For must be ignored — real client IP is used."""
        app = self._make_app(0)
        client = app.test_client()
        resp = client.get('/ip', headers={'X-Forwarded-For': '1.2.3.4'})
        data = resp.get_json()
        # Flask test client sends 127.0.0.1; with trust=0, XFF is ignored
        self.assertNotEqual(data['ip'], '1.2.3.4')
        self.assertEqual(data['ip'], '127.0.0.1')

    def test_trust_one_uses_xff(self):
        """With trust=1, the rightmost X-Forwarded-For entry is trusted."""
        app = self._make_app(1)
        client = app.test_client()
        resp = client.get('/ip', headers={'X-Forwarded-For': '1.2.3.4'})
        data = resp.get_json()
        self.assertEqual(data['ip'], '1.2.3.4')


if __name__ == '__main__':
    unittest.main()
