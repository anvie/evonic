"""Credential-source isolation for Codex and Claude providers."""

import time
from unittest.mock import MagicMock, patch


def _authenticated_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def test_subscription_providers_are_seeded():
    from models.db import db

    codex = db.get_provider("openai-codex")
    claude = db.get_provider("anthropic")
    assert (codex["api_format"], codex["credential_source"]) == ("codex", "")
    assert (claude["api_format"], claude["credential_source"]) == ("anthropic", "api_key")


def test_legacy_codex_route_never_selects_anthropic_oauth():
    from app import app
    from models.db import db

    db.update_provider("anthropic", {"auth_type": "oauth", "credential_source": "claude_code"})
    with patch("routes.codex.get_valid_token", return_value=None), \
         patch("routes.codex.read_codex_cli_credentials", return_value=None):
        response = _authenticated_client(app).get("/api/provider/codex/status")

    assert response.status_code == 200
    assert response.get_json()["provider_id"] == "openai-codex"


def test_codex_existing_login_is_imported_into_its_provider_only():
    from backend.provider.oauth_codex import use_codex_cli_credentials
    from models.db import db

    creds = {
        "source": "codex_auth_file",
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": int(time.time()) + 3600,
    }
    with patch("backend.provider.oauth_codex.read_codex_cli_credentials", return_value=creds):
        result = use_codex_cli_credentials(db, "openai-codex")

    codex = db.get_provider("openai-codex")
    claude = db.get_provider("anthropic")
    assert result["success"]
    assert (codex["api_key"], codex["credential_source"]) == ("access", "codex_cli_import")
    assert not claude["api_key"]


def test_codex_device_flow_returns_tokens_after_approval():
    from backend.provider import oauth_codex

    oauth_codex._pending_device_auth.clear()
    user_code = MagicMock(status_code=200)
    user_code.json.return_value = {"user_code": "ABCD-EFGH", "device_auth_id": "device", "interval": 3}
    approval = MagicMock(status_code=200)
    approval.json.return_value = {"authorization_code": "code", "code_verifier": "verifier"}
    exchange = MagicMock(status_code=200)
    exchange.json.return_value = {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

    with patch("backend.provider.oauth_codex.requests.post", side_effect=[user_code, approval, exchange]):
        started = oauth_codex.start_device_auth_flow("openai-codex")
        finished = oauth_codex.poll_device_auth_flow("openai-codex")

    assert started["user_code"] == "ABCD-EFGH"
    assert finished["status"] == "complete"
    assert finished["tokens"]["access_token"] == "access"


def test_codex_device_flow_rejects_invalid_json():
    from backend.provider import oauth_codex

    response = MagicMock(status_code=200)
    response.json.side_effect = ValueError
    with patch("backend.provider.oauth_codex.requests.post", return_value=response):
        result = oauth_codex.start_device_auth_flow("openai-codex")
    assert result == {"error": "OpenAI returned an invalid device code response."}


def test_claude_chooses_freshest_valid_store():
    from backend.provider.claude_code import read_claude_code_credentials

    now = int(time.time() * 1000)
    keychain = {"access_token": "old", "expires_at_ms": now + 120_000, "source": "macos_keychain"}
    file_creds = {"access_token": "new", "expires_at_ms": now + 240_000, "source": "claude_credentials_file"}
    with patch("backend.provider.claude_code._read_keychain", return_value=keychain), \
         patch("backend.provider.claude_code._read_file", return_value=file_creds):
        assert read_claude_code_credentials()["access_token"] == "new"


def test_claude_existing_login_links_without_copying_secret():
    from backend.provider.claude_code import use_claude_code_credentials
    from models.db import db

    creds = {
        "access_token": "secret-not-for-db",
        "refresh_token": "refresh-not-for-db",
        "expires_at_ms": int(time.time() * 1000) + 3600_000,
        "source": "macos_keychain",
    }
    with patch("backend.provider.claude_code.read_claude_code_credentials", return_value=creds):
        result = use_claude_code_credentials(db, "anthropic")

    provider = db.get_provider("anthropic")
    assert result["success"]
    assert provider["credential_source"] == "claude_code"
    assert provider["api_key"] == provider["refresh_token"] == ""


def test_claude_oauth_headers_match_claude_code_identity():
    from backend.provider.claude_code import SYSTEM_PREFIX, auth_headers

    with patch("backend.provider.claude_code.claude_code_version", return_value="9.9.9"):
        headers = auth_headers("token", oauth=True)
    assert headers["Authorization"] == "Bearer token"
    assert headers["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert headers["user-agent"] == "claude-code/9.9.9 (external, cli)"
    assert headers["x-app"] == "cli"
    assert "x-api-key" not in headers
    assert SYSTEM_PREFIX.startswith("You are Claude Code")
    assert auth_headers("api-key", oauth=False)["x-api-key"] == "api-key"


def test_provider_api_never_returns_tokens():
    from app import app
    from models.db import db

    db.update_provider("openai-codex", {
        "api_key": "access-secret",
        "refresh_token": "refresh-secret",
        "auth_type": "oauth",
        "credential_source": "evonic_oauth",
    })
    response = _authenticated_client(app).get("/api/providers")
    payload = response.get_json()
    encoded = str(payload)
    assert "access-secret" not in encoded
    assert "refresh-secret" not in encoded
    codex = next(item for item in payload["providers"] if item["id"] == "openai-codex")
    assert codex["credential_configured"] is True
