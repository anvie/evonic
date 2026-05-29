"""Regression tests for SVG avatar upload XSS prevention.

SEC-1: SVG files uploaded as agent avatars can contain <script> tags or
event handlers. When served back via /api/agents/<id>/avatar with
mimetype image/svg+xml, browsers render them as active documents,
enabling stored XSS.

Fix: reject .svg uploads; or if SVG is kept, serve with
Content-Disposition: attachment to prevent inline rendering.
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAvatarSvgUploadRejected(unittest.TestCase):
    """SVG must be rejected at upload time so XSS payload never reaches disk."""

    def setUp(self):
        from routes.agents import agents_bp
        from flask import Flask

        template_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "templates",
        )
        self.app = Flask(__name__, template_folder=template_dir)
        self.app.secret_key = "test-avatar-xss-secret"
        self.app.register_blueprint(agents_bp)
        self.client = self.app.test_client()

    def _upload_avatar(self, filename: str, content: bytes, content_type: str):
        """POST a fake avatar file to the upload endpoint."""
        data = {
            "file": (io.BytesIO(content), filename, content_type),
        }
        return self.client.post(
            "/api/agents/test-agent/avatar",
            data=data,
            content_type="multipart/form-data",
        )

    def test_svg_upload_is_rejected(self):
        """.svg extension must be rejected with 400 to prevent stored XSS."""
        svg_payload = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        resp = self._upload_avatar("evil.svg", svg_payload, "image/svg+xml")
        self.assertEqual(
            resp.status_code,
            400,
            "SVG upload should be rejected with HTTP 400 to prevent XSS",
        )

    def test_svg_with_image_mimetype_upload_is_rejected(self):
        """.svg with image/png content-type spoofing must still be rejected."""
        svg_payload = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        resp = self._upload_avatar("evil.svg", svg_payload, "image/png")
        self.assertEqual(
            resp.status_code,
            400,
            "SVG upload with spoofed MIME type must be rejected based on extension",
        )

    def test_png_upload_is_still_accepted(self):
        """Legitimate .png uploads must not be rejected with 400 (non-regression).

        The extension check now runs before the agent DB lookup, so a valid
        extension with a non-existent agent yields 404, not 400.
        """
        # 1x1 transparent PNG
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
            b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
            b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        resp = self._upload_avatar("avatar.png", png_bytes, "image/png")
        # 404 expected: agent 'test-agent' doesn't exist in test DB.
        # The important invariant is that .png is NOT rejected with 400.
        self.assertEqual(
            resp.status_code,
            404,
            "PNG should pass extension check and reach the agent-lookup step",
        )

    def test_jpg_upload_is_still_accepted(self):
        """.jpg must pass the extension check and reach the agent lookup (non-regression)."""
        resp = self._upload_avatar("photo.jpg", b"\xff\xd8\xff", "image/jpeg")
        # 404 because the test agent doesn't exist in DB, NOT 400 (extension rejected).
        self.assertEqual(
            resp.status_code,
            404,
            ".jpg should pass extension check",
        )

    def test_webp_upload_is_still_accepted(self):
        """.webp must pass the extension check and reach the agent lookup (non-regression)."""
        resp = self._upload_avatar("photo.webp", b"RIFF....WEBP", "image/webp")
        # 404 because the test agent doesn't exist in DB, NOT 400 (extension rejected).
        self.assertEqual(
            resp.status_code,
            404,
            ".webp should pass extension check",
        )


class TestAvatarAllowedExtensions(unittest.TestCase):
    """Unit-test the allowed_exts set in the upload handler directly."""

    def _get_allowed_exts(self):
        """Import the constant from agents route module."""
        import importlib
        import routes.agents as agents_mod
        importlib.reload(agents_mod)
        # The allowed_exts set is defined inside api_upload_avatar; we
        # verify the behaviour via the upload endpoint test above.
        # Here we just confirm .svg is not in the module-level comment/docs.
        return agents_mod

    def test_svg_not_in_allowed_exts(self):
        """Verify .svg is explicitly excluded from avatar allowed extensions."""
        import routes.agents as agents_mod
        import inspect
        source = inspect.getsource(agents_mod.api_upload_avatar)
        self.assertNotIn(
            "'.svg'",
            source,
            ".svg must be removed from the allowed_exts set in api_upload_avatar",
        )


if __name__ == "__main__":
    unittest.main()
