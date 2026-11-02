"""Regression tests for the plain-HTTP login loop.

Cookies used to be marked ``Secure`` unconditionally (``FORCE_INSECURE_COOKIES``
defaulted to 0), so a browser talking to ``http://<host>:8080`` silently dropped
the session cookie and login looped back to the login page without any error.
The ``Secure`` flag must follow the request scheme instead.
"""

import pytest
from werkzeug.security import generate_password_hash

import config
from app import app

PASSWORD = "correct-horse-battery-staple"
SESSION_COOKIE = "session"  # Flask default SESSION_COOKIE_NAME
LAN_BASE = "http://192.168.1.50:8080"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        config, "ADMIN_PASSWORD_HASH",
        generate_password_hash(PASSWORD, method="pbkdf2:sha256"),
    )
    monkeypatch.setattr(config, "FORCE_INSECURE_COOKIES", False)
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "")
    return app.test_client()


def _cookie_attrs(response, name):
    """Return the raw Set-Cookie header for *name*, or None when absent."""
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith(name + "="):
            return header
    return None


def _login(client, base_url):
    return client.post(
        "/login", data={"password": PASSWORD}, base_url=base_url,
        follow_redirects=False,
    )


def test_session_cookie_is_not_secure_over_plain_http(client):
    """A LAN HTTP origin gets a usable (non-Secure) session cookie."""
    response = _login(client, LAN_BASE)
    assert response.status_code == 302

    cookie = _cookie_attrs(response, SESSION_COOKIE)
    assert cookie is not None, "login must set a session cookie"
    assert "Secure" not in cookie


def test_session_cookie_is_secure_over_https(client):
    response = _login(client, "https://evonic.example.com")
    assert response.status_code == 302

    cookie = _cookie_attrs(response, SESSION_COOKIE)
    assert cookie is not None
    assert "Secure" in cookie


def test_csrf_cookie_follows_request_scheme(client):
    http_cookie = _cookie_attrs(client.get("/login", base_url=LAN_BASE), "csrf_token")
    assert http_cookie is not None and "Secure" not in http_cookie

    https_cookie = _cookie_attrs(
        client.get("/login", base_url="https://evonic.example.com"), "csrf_token"
    )
    assert https_cookie is not None and "Secure" in https_cookie


def test_forwarded_proto_https_gets_secure_cookie(client):
    """A TLS-terminating proxy (X-Forwarded-Proto) is honoured via ProxyFix."""
    response = client.post(
        "/login", data={"password": PASSWORD}, base_url=LAN_BASE,
        headers={"X-Forwarded-Proto": "https"},
    )
    cookie = _cookie_attrs(response, SESSION_COOKIE)
    assert cookie is not None and "Secure" in cookie


def test_force_insecure_cookies_wins_over_https(client, monkeypatch):
    """Explicit opt-out still disables the Secure flag on HTTPS deployments."""
    monkeypatch.setattr(config, "FORCE_INSECURE_COOKIES", True)
    response = _login(client, "https://evonic.example.com")
    cookie = _cookie_attrs(response, SESSION_COOKIE)
    assert cookie is not None and "Secure" not in cookie


def test_setup_notice_is_rendered_on_login_page():
    """After setup the wizard redirects to /login?ready=1 with a friendly notice."""
    html = app.test_client().get("/login?ready=1").get_data(as_text=True)
    assert "Setup complete" in html
