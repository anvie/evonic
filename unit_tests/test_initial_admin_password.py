"""A fresh install must always end with an admin password the user can find."""

import os
import stat

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

import config
from app import app
from backend import dotenv_loader, setup as setup_backend


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(config, "TURNSTILE_SECRET_KEY", "")
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    path = tmp_path / ".env"
    monkeypatch.setattr(dotenv_loader, "DEFAULT_DOTENV_PATH", str(path))
    monkeypatch.setattr(
        setup_backend, "initial_admin_password_path",
        lambda: str(tmp_path / "data" / "initial_admin_password"),
    )
    return path


def test_generates_once_and_stores_private_copy(env_file, tmp_path):
    password = setup_backend.ensure_admin_password()

    assert password
    assert check_password_hash(config.ADMIN_PASSWORD_HASH, password)
    assert dotenv_loader.read_dotenv_value("ADMIN_PASSWORD_HASH") == config.ADMIN_PASSWORD_HASH
    saved = tmp_path / "data" / "initial_admin_password"
    assert saved.read_text().strip() == password
    assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600

    assert setup_backend.ensure_admin_password() is None


def test_clear_removes_the_file(env_file, tmp_path):
    setup_backend.ensure_admin_password()
    setup_backend.clear_initial_admin_password()
    setup_backend.clear_initial_admin_password()  # idempotent

    assert not (tmp_path / "data" / "initial_admin_password").exists()


def test_login_picks_up_hash_written_to_env_after_start(env_file):
    """`evonic pass` edits .env only; a running server must accept it."""
    env_file.write_text("ADMIN_PASSWORD_HASH=%s\n" % generate_password_hash(
        "fresh-secret", method="pbkdf2:sha256"))
    client = app.test_client()

    response = client.post("/login", data={"password": "fresh-secret"})

    assert response.status_code == 302
    assert config.ADMIN_PASSWORD_HASH.startswith("pbkdf2:sha256")
