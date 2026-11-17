"""The login page must explain an instance that has no admin password yet.

A fresh (or non-interactively installed) instance used to render a normal login
form whose every submit failed with "Admin password not configured." — a dead
end. The page now names the problem and shows the commands that fix it.
"""

import re

import pytest
from werkzeug.security import generate_password_hash

import config
from app import app


def _tag(body, node_id):
    match = re.search(rf'<[a-z]+[^>]*id="{re.escape(node_id)}"[^>]*>', body)
    assert match is not None, f"no element with id={node_id} in the page"
    return match.group(0)


@pytest.fixture
def no_password(monkeypatch):
    monkeypatch.setattr(config, 'ADMIN_PASSWORD_HASH', '')
    monkeypatch.setattr(config, 'TURNSTILE_SECRET_KEY', '')
    monkeypatch.setattr(config, 'TURNSTILE_SITE_KEY', '')
    return app.test_client()


def test_login_page_explains_the_missing_password(no_password):
    response = no_password.get('/login')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Password not configured' in body
    assert 'no admin password yet' in body
    assert 'Quick start' in body
    assert './evonic pass' in body
    assert './evonic restart' in body


def test_form_is_disabled_while_no_password_exists(no_password):
    body = no_password.get('/login').get_data(as_text=True)

    assert 'disabled' in _tag(body, 'lg-password')
    assert 'disabled' in _tag(body, 'lg-submit')
    assert 'Password required' in body


def test_fresh_install_points_at_the_setup_wizard(no_password, monkeypatch):
    """A fresh install (no super agent yet) can also use the setup wizard."""
    from models.db import db

    monkeypatch.setattr(db, 'has_super_agent', lambda: False)
    body = no_password.get('/login').get_data(as_text=True)

    assert 'href="/setup"' in body
    assert './evonic setup' in body


def test_installed_instance_does_not_point_at_the_setup_wizard(no_password):
    """With a super agent already created, /setup redirects home — say nothing."""
    assert 'href="/setup"' not in no_password.get('/login').get_data(as_text=True)


def test_posting_without_a_password_keeps_the_guide_on_screen(no_password):
    response = no_password.post('/login', data={'password': 'anything'})
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'Admin password not configured.' in body
    assert 'Password not configured' in body
    assert 'Quick start' in body


def test_configured_instance_keeps_the_plain_form(monkeypatch):
    monkeypatch.setattr(
        config, 'ADMIN_PASSWORD_HASH',
        generate_password_hash('correct-horse-battery-staple', method='pbkdf2:sha256'),
    )
    monkeypatch.setattr(config, 'TURNSTILE_SECRET_KEY', '')
    client = app.test_client()

    body = client.get('/login').get_data(as_text=True)

    assert 'Password not configured' not in body
    assert 'Quick start' not in body
    assert 'disabled' not in _tag(body, 'lg-password')
    assert 'disabled' not in _tag(body, 'lg-submit')
    assert 'Sign In' in body
