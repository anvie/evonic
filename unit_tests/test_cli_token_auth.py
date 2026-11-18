"""The local CLI Bearer token is accepted only on the chat-related routes."""
import pytest

import app as app_module
import config


@pytest.fixture
def ok():
    def check(path, auth):
        headers = {"Authorization": auth} if auth else {}
        with app_module.app.test_request_context(path, headers=headers):
            return app_module._cli_token_ok()
    return check


def test_valid_token_on_allowed_paths(ok):
    auth = f"Bearer {config.cli_api_token()}"
    for path in ("/api/agents/bot/chat", "/api/agents/bot/chat/session",
                 "/api/agents/bot/chat/approve", "/api/realtime/stream",
                 "/api/sessions/s1/stop"):
        assert ok(path, auth), path


def test_valid_token_rejected_elsewhere(ok):
    auth = f"Bearer {config.cli_api_token()}"
    for path in ("/api/agents", "/api/agents/bot", "/api/settings", "/api/agents/bot/chat/clear"):
        assert not ok(path, auth), path


def test_bad_or_missing_token(ok):
    assert not ok("/api/agents/bot/chat", "Bearer nope")
    assert not ok("/api/agents/bot/chat", "Basic abc")
    assert not ok("/api/agents/bot/chat", None)
