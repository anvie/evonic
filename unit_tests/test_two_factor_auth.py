"""Coverage for administrator TOTP authentication and its configuration precedence."""

import time

from werkzeug.security import generate_password_hash


def _configure_login(monkeypatch):
    import config

    monkeypatch.setattr(
        config, 'ADMIN_PASSWORD_HASH',
        generate_password_hash('correct-password', method='pbkdf2:sha256'),
    )
    monkeypatch.setattr(config, 'TURNSTILE_SECRET_KEY', '')


def _current_code(secret):
    from backend import totp
    return totp._code_at(secret, int(time.time() // 30))


def test_enable_2fa_environment_override_has_precedence(monkeypatch):
    """An explicitly set ENABLE_2FA takes precedence over the DB setting."""
    from backend import totp
    from models.db import db

    db.set_setting('two_factor_auth_enabled', '0')
    monkeypatch.setenv('ENABLE_2FA', 'true')
    assert totp.get_enabled_state() == (True, True)

    db.set_setting('two_factor_auth_enabled', '1')
    monkeypatch.setenv('ENABLE_2FA', 'false')
    assert totp.get_enabled_state() == (False, True)

    monkeypatch.delenv('ENABLE_2FA')
    assert totp.get_enabled_state() == (True, False)


def test_login_requires_totp_enrollment_when_enabled(monkeypatch):
    """A password-only login becomes authenticated only after TOTP setup."""
    from app import app
    from models.db import db
    from backend import totp

    _configure_login(monkeypatch)
    monkeypatch.delenv('ENABLE_2FA', raising=False)
    db.set_setting('two_factor_auth_enabled', '1')
    db.set_setting('admin_totp_secret', '')
    db.set_setting('admin_totp_confirmed', '0')

    client = app.test_client()
    response = client.post('/login', data={'password': 'correct-password', 'next': '/agents'})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/two-factor')

    setup = client.get('/two-factor')
    assert setup.status_code == 200
    assert b'Set up two-factor authentication' in setup.data
    secret = totp.get_secret()
    assert secret

    response = client.post('/two-factor', data={'code': _current_code(secret)})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/agents')
    assert totp.is_configured()
    with client.session_transaction() as session:
        assert session['authenticated'] is True


def test_settings_api_reports_environment_override(monkeypatch):
    """The settings API exposes effective and stored values without bypassing env."""
    from app import app
    from models.db import db

    db.set_setting('two_factor_auth_enabled', '0')
    monkeypatch.setenv('ENABLE_2FA', 'true')
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True

    response = client.get('/api/settings/two-factor-auth')
    assert response.status_code == 200
    body = response.get_json()
    assert body['enabled'] is True
    assert body['stored_enabled'] is False
    assert body['env_override'] is True

    response = client.put('/api/settings/two-factor-auth', json={'enabled': False})
    assert response.status_code == 200
    body = response.get_json()
    assert body['enabled'] is True
    assert body['stored_enabled'] is False
    assert body['env_override'] is True


def test_totp_guesses_are_rate_limited_across_password_logins(monkeypatch):
    """Repeated bad codes lock the second factor even if the password is re-entered."""
    from app import app
    from models.db import db
    from backend import totp
    from routes import auth

    _configure_login(monkeypatch)
    monkeypatch.delenv('ENABLE_2FA', raising=False)
    db.set_setting('two_factor_auth_enabled', '1')
    db.set_setting('admin_totp_secret', '')
    db.set_setting('admin_totp_confirmed', '0')
    auth._login_attempts.clear()

    client = app.test_client()
    client.post('/login', data={'password': 'correct-password'})
    client.get('/two-factor')
    secret = totp.get_secret()

    for _ in range(auth._MAX_ATTEMPTS):
        response = client.post('/two-factor', data={'code': '000000'})
        assert response.status_code == 200

    # A fresh correct-password login must not reset the guess counter.
    client.post('/login', data={'password': 'correct-password'})
    response = client.post('/two-factor', data={'code': _current_code(secret)})
    assert response.status_code == 429
    with client.session_transaction() as session:
        assert not session.get('authenticated')
    auth._login_attempts.clear()
