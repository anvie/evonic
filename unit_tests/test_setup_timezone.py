"""Regression tests for setup-time platform timezone configuration."""

from backend.setup import DEFAULT_PLATFORM_TIMEZONE, run_setup, validate_timezone
from models.db import db


def _remove_seed_super_agent():
    with db._connect() as conn:
        conn.execute("DELETE FROM agents WHERE is_super = 1")
        conn.commit()


def test_validate_timezone_accepts_iana_name():
    assert validate_timezone("Europe/London") == "Europe/London"


def test_validate_timezone_rejects_unknown_name():
    try:
        validate_timezone("Jakarta")
    except ValueError as e:
        assert "IANA" in str(e)
    else:
        raise AssertionError("invalid timezone was accepted")


def test_run_setup_persists_default_timezone(tmp_path, monkeypatch):
    _remove_seed_super_agent()
    monkeypatch.setattr("config.BASE_DIR", str(tmp_path))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("EVONIC_TIMEZONE", raising=False)

    result = run_setup(
        provider="ollama", model_name="llama3", base_url="", api_key="",
        agent_name="Timezone Admin", agent_id="timezone_admin",
    )

    assert result["success"] is True
    env_text = (tmp_path / ".env").read_text()
    assert f"EVONIC_TIMEZONE={DEFAULT_PLATFORM_TIMEZONE}" in env_text


def test_run_setup_persists_custom_timezone(tmp_path, monkeypatch):
    _remove_seed_super_agent()
    monkeypatch.setattr("config.BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-key")

    result = run_setup(
        provider="ollama", model_name="llama3", base_url="", api_key="",
        agent_name="Timezone Admin", agent_id="timezone_admin",
        timezone_name="Europe/London",
    )

    assert result["success"] is True
    assert "EVONIC_TIMEZONE=Europe/London" in (tmp_path / ".env").read_text()


def test_run_setup_rejects_invalid_timezone_before_writes(tmp_path, monkeypatch):
    _remove_seed_super_agent()
    monkeypatch.setattr("config.BASE_DIR", str(tmp_path))

    result = run_setup(
        provider="ollama", model_name="llama3", base_url="", api_key="",
        agent_name="Timezone Admin", agent_id="timezone_admin",
        timezone_name="Not/A_Zone",
    )

    assert "Unknown timezone" in result["error"]
    assert not db.has_super_agent()
    assert not (tmp_path / ".env").exists()
