"""Tests for the project-root .env resolution and live password-hash application.

Both defects surfaced as "password was set but login does not work":
``load_dotenv()`` resolved ``.env`` against the process cwd (the launcher never
chdirs), and the setup wizard wrote the hash to ``.env`` only, leaving the
already-running process with the old value read at import time.
"""

import os

from werkzeug.security import generate_password_hash

import config
from backend import dotenv_loader
from backend import setup as setup_backend  # not "setup_module": pytest treats that name as a hook


def test_default_dotenv_path_is_project_root():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(dotenv_loader.__file__)))
    assert dotenv_loader.DEFAULT_DOTENV_PATH == os.path.join(project_root, ".env")


def test_load_dotenv_ignores_cwd(monkeypatch, tmp_path):
    """The project-root .env wins even when the server runs from elsewhere."""
    root_env = tmp_path / "project.env"
    root_env.write_text("EVONIC_TEST_DOTENV_ORIGIN=project\n")

    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    (other_cwd / ".env").write_text("EVONIC_TEST_DOTENV_ORIGIN=cwd\n")

    monkeypatch.delenv("EVONIC_TEST_DOTENV_ORIGIN", raising=False)
    monkeypatch.setattr(dotenv_loader, "DEFAULT_DOTENV_PATH", str(root_env))
    monkeypatch.chdir(other_cwd)

    assert dotenv_loader.load_dotenv() is True
    assert os.environ["EVONIC_TEST_DOTENV_ORIGIN"] == "project"


def test_load_dotenv_falls_back_to_cwd(monkeypatch, tmp_path):
    """Deployments keeping .env next to the process still work."""
    monkeypatch.setattr(dotenv_loader, "DEFAULT_DOTENV_PATH", str(tmp_path / "missing.env"))
    monkeypatch.delenv("EVONIC_TEST_DOTENV_FALLBACK", raising=False)
    (tmp_path / ".env").write_text("EVONIC_TEST_DOTENV_FALLBACK=yes\n")
    monkeypatch.chdir(tmp_path)

    assert dotenv_loader.load_dotenv() is True
    assert os.environ["EVONIC_TEST_DOTENV_FALLBACK"] == "yes"


def test_apply_admin_password_hash_updates_running_process(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)

    pw_hash = generate_password_hash("s3cret", method="pbkdf2:sha256")
    setup_backend.apply_admin_password_hash(pw_hash)

    assert config.ADMIN_PASSWORD_HASH == pw_hash
    assert os.environ["ADMIN_PASSWORD_HASH"] == pw_hash
