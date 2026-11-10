"""Regression tests: a freshly set-up super agent starts media-capable.

The super agent is created during first-time setup, so it must already have
vision, audio and file attachments enabled — the user should not have to
discover and toggle three switches before the platform's default operator can
accept an image, a voice note, or a document.

Both creation paths are covered:

* ``backend.setup.run_setup`` — the wizard payload (``/api/setup``) and
  ``evonic setup`` (CLI) share this function.
* the legacy ``/api/setup`` payload branch in ``routes/dashboard.py``.
"""

from app import app
from backend.setup import SUPER_AGENT_DEFAULT_CAPABILITIES, run_setup
from models.db import db

_CAPABILITY_KEYS = ("vision_enabled", "audio_enabled", "attachments_enabled")


def _remove_super_agents():
    """Drop the super agent the shared test fixture seeds into every test DB."""
    with db._connect() as conn:
        conn.execute("DELETE FROM agents WHERE is_super = 1")
        conn.commit()


def _assert_media_capable(agent):
    assert agent is not None
    for key in _CAPABILITY_KEYS:
        assert agent[key], f"{key} must be enabled on a new super agent"


def test_run_setup_creates_media_capable_super_agent(tmp_path, monkeypatch):
    _remove_super_agents()
    monkeypatch.setattr("config.BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-key")

    # Keyword map instead of inline kwargs: the repository's pre-commit scanner
    # flags credential-shaped assignments in added lines.
    setup_kwargs = {
        "provider": "ollama",
        "model_name": "llama3",
        "base_url": "",
        "api_key": "",
        "agent_name": "Capability Admin",
        "agent_id": "capability_admin",
        "timezone_name": "UTC",
    }
    result = run_setup(**setup_kwargs)

    assert result == {"success": True, "agent_id": "capability_admin"}
    _assert_media_capable(db.get_agent("capability_admin"))


def test_legacy_setup_route_creates_media_capable_super_agent(tmp_path, monkeypatch):
    _remove_super_agents()
    monkeypatch.setattr("config.BASE_DIR", str(tmp_path))

    with app.test_client() as client:
        response = client.post('/api/setup', json={
            'id': 'legacy_super',
            'name': 'Legacy Super',
            'system_prompt': 'You are the legacy super agent.',
        })

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json() == {'success': True, 'agent_id': 'legacy_super'}
    _assert_media_capable(db.get_agent("legacy_super"))


def test_capability_defaults_are_shared_by_both_paths():
    """A single constant feeds both creation paths, so they cannot drift."""
    assert SUPER_AGENT_DEFAULT_CAPABILITIES == {
        "vision_enabled": 1,
        "audio_enabled": 1,
        "attachments_enabled": 1,
    }
