"""
Test session fixation vulnerability in login flow.

Security: Login should regenerate session ID to prevent session fixation attacks.
An attacker who can set a victim's session cookie before login should not be able
to hijack the authenticated session afterward.
"""
import unittest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash

# Generate a fresh test password hash
TEST_PASSWORD = "test123"
TEST_PASSWORD_HASH = generate_password_hash(TEST_PASSWORD)

os.environ.setdefault("SECRET_KEY", "test-session-fixation-secret")
os.environ["ADMIN_PASSWORD_HASH"] = TEST_PASSWORD_HASH
os.environ["TURNSTILE_SECRET_KEY"] = ""  # disable captcha

from flask import Flask, session
import config

# Force config to use test hash
config.ADMIN_PASSWORD_HASH = TEST_PASSWORD_HASH

from routes.auth import auth_bp


class TestSessionFixation(unittest.TestCase):
    """Test that login regenerates session ID to prevent session fixation."""

    def setUp(self):
        """Set up Flask test client."""
        # Get the absolute path to templates directory
        current_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(current_dir)
        template_dir = os.path.join(parent_dir, 'templates')
        
        self.app = Flask(__name__, template_folder=template_dir)
        self.app.secret_key = "test-session-fixation-secret"
        self.app.config['TESTING'] = True
        self.app.register_blueprint(auth_bp)
        self.client = self.app.test_client()

    def test_login_regenerates_session_id(self):
        """Login should regenerate session ID to prevent session fixation."""
        with self.client as c:
            # Step 1: Establish a session by visiting login page
            response = c.get('/login')
            self.assertEqual(response.status_code, 200)
            
            # Step 2: Attacker sets malicious data in the session
            # (In real attack, attacker would set the session cookie on victim's browser)
            with c.session_transaction() as sess:
                sess['attacker_marker'] = 'attacker_data'
                sess['attacker_payload'] = 'malicious'
            
            # Step 3: Victim logs in using the tainted session
            response = c.post(
                '/login',
                data={'password': TEST_PASSWORD, 'next': '/'},
                follow_redirects=False
            )
            
            # Login should succeed
            self.assertIn(response.status_code, [200, 302])
            
            # Step 4: CRITICAL SECURITY CHECK
            # After successful login, all pre-login session data should be cleared
            # Only the authenticated flag and permanent flag should exist
            with c.session_transaction() as sess:
                # The attacker's markers should be gone
                self.assertNotIn(
                    'attacker_marker',
                    sess,
                    "Pre-login session data (attacker_marker) should be cleared to prevent session fixation"
                )
                self.assertNotIn(
                    'attacker_payload',
                    sess,
                    "Pre-login session data (attacker_payload) should be cleared to prevent session fixation"
                )
                
                # User should be authenticated
                self.assertTrue(
                    sess.get('authenticated'),
                    "User should be authenticated after successful login"
                )
                
                # Verify session only contains expected post-login data
                # (authenticated and any other legitimate session data, but NOT pre-login attacker data)
                self.assertLessEqual(
                    set(sess.keys()) - {'authenticated', '_permanent'},
                    set(),
                    "Session should only contain authenticated flag after login, no pre-login data"
                )

    def test_failed_login_does_not_regenerate_session(self):
        """Failed login should NOT regenerate session (only successful login should)."""
        with self.client as c:
            # Make a request to establish a session
            c.get('/login')
            
            # Set a marker in the session
            with c.session_transaction() as sess:
                sess['test_marker'] = 'should_remain'
                initial_marker = sess['test_marker']
            
            # Attempt failed login
            response = c.post(
                '/login',
                data={'password': 'wrong_password'},
                follow_redirects=False
            )
            
            # Login should fail
            self.assertIn(response.status_code, [400, 401, 200])  # Various error responses
            
            # Session marker should still exist (no regeneration on failed login)
            with c.session_transaction() as sess:
                self.assertEqual(
                    sess.get('test_marker'),
                    initial_marker,
                    "Failed login should not clear session"
                )
                self.assertFalse(
                    sess.get('authenticated', False),
                    "User should not be authenticated after failed login"
                )


if __name__ == '__main__':
    unittest.main()
