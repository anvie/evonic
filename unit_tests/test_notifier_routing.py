"""Regression tests for central agent-notification session routing."""

import types
from unittest import mock

import pytest

from backend.agent_runtime import notifier


@pytest.fixture
def routing_env(monkeypatch):
    fake_db = mock.MagicMock()
    fake_db.get_session_messages.return_value = []
    fake_db.get_web_fallback_session.return_value = None
    fake_db.get_channel.return_value = None

    channel_manager = mock.MagicMock()
    channel_manager.is_running.return_value = False
    registry_stub = types.ModuleType("backend.channels.registry")
    registry_stub.channel_manager = channel_manager

    send_guard_stub = types.ModuleType("backend.tools.channel_send_guard")
    send_guard_stub.wait_for_send_slot = mock.MagicMock()

    event_stream = mock.MagicMock()
    event_stream_stub = types.ModuleType("backend.event_stream")
    event_stream_stub.event_stream = event_stream

    monkeypatch.setattr(notifier, "db", fake_db)
    monkeypatch.setitem(__import__("sys").modules, "backend.channels.registry", registry_stub)
    monkeypatch.setitem(__import__("sys").modules, "backend.tools.channel_send_guard", send_guard_stub)
    monkeypatch.setitem(__import__("sys").modules, "backend.event_stream", event_stream_stub)

    return fake_db, channel_manager, event_stream


def _notify(**kwargs):
    defaults = {
        "agent_id": "target_agent",
        "tag": "AGENT/Sender",
        "message": "Finished",
        "dedup": False,
        "trigger_llm": False,
    }
    defaults.update(kwargs)
    return notifier.notify_agent(**defaults)


def test_explicit_session_owned_by_another_agent_is_rejected(routing_env):
    fake_db, _, event_stream = routing_env
    fake_db.get_session_with_details.return_value = {
        "id": "foreign-session",
        "agent_id": "other_agent",
        "external_user_id": "web-user",
        "channel_id": None,
    }

    result = _notify(session_id="foreign-session")

    assert result == {
        "success": False,
        "session_id": None,
        "reason": "session_owner_mismatch",
    }
    fake_db.add_chat_message.assert_not_called()
    event_stream.emit.assert_not_called()


@pytest.mark.parametrize(
    ("enabled", "running"),
    [(False, False), (True, False)],
    ids=["disabled", "stopped"],
)
def test_inactive_explicit_session_is_preserved_without_web_fallback(
    routing_env, enabled, running,
):
    fake_db, channel_manager, event_stream = routing_env
    fake_db.get_session_with_details.return_value = {
        "id": "originating-session",
        "agent_id": "target_agent",
        "external_user_id": "external-user",
        "channel_id": "channel-1",
    }
    fake_db.get_channel.return_value = {
        "id": "channel-1",
        "agent_id": "target_agent",
        "type": "telegram",
        "enabled": enabled,
    }
    channel_manager.is_running.return_value = running
    fake_db.get_web_fallback_session.return_value = {
        "id": "unrelated-web-session",
        "agent_id": "target_agent",
        "external_user_id": "web-user",
        "channel_id": None,
    }

    result = _notify(session_id="originating-session")

    assert result == {
        "success": True,
        "session_id": "originating-session",
        "reason": None,
        "route": "direct",
        "fallback_reason": "inactive_channel",
        "delivery": "web",
    }
    fake_db.get_web_fallback_session.assert_not_called()
    fake_db.add_chat_message.assert_called_once()
    assert fake_db.add_chat_message.call_args.args[0] == "originating-session"
    assert fake_db.add_chat_message.call_args.kwargs["metadata"] == {
        "notification_channel_unavailable": True,
    }
    emitted = event_stream.emit.call_args.args[1]
    assert emitted["session_id"] == "originating-session"
    assert emitted["external_user_id"] == "external-user"
    assert emitted["channel_id"] == "channel-1"


def test_inactive_explicit_session_remains_available_without_web_fallback(routing_env):
    fake_db, _, event_stream = routing_env
    fake_db.get_session_with_details.return_value = {
        "id": "originating-session",
        "agent_id": "target_agent",
        "external_user_id": "external-user",
        "channel_id": "channel-1",
    }
    fake_db.get_channel.return_value = {"id": "channel-1", "enabled": False}

    result = _notify(session_id="originating-session")

    assert result["success"] is True
    assert result["session_id"] == "originating-session"
    assert result["fallback_reason"] == "inactive_channel"
    fake_db.get_web_fallback_session.assert_not_called()
    fake_db.add_chat_message.assert_called_once()
    event_stream.emit.assert_called_once()


def test_inter_agent_reply_with_inactive_origin_session_never_uses_web_fallback(routing_env):
    fake_db, _, event_stream = routing_env
    fake_db.get_session_with_details.return_value = {
        "id": "originating-whatsapp-session",
        "agent_id": "target_agent",
        "external_user_id": "external-user",
        "channel_id": "channel-1",
    }
    fake_db.get_channel.return_value = {"id": "channel-1", "enabled": False}
    fake_db.get_web_fallback_session.return_value = {
        "id": "unrelated-web-session",
        "agent_id": "target_agent",
        "external_user_id": "web-user",
        "channel_id": None,
    }

    result = _notify(session_id="originating-whatsapp-session")

    assert result["success"] is True
    assert result["session_id"] == "originating-whatsapp-session"
    fake_db.get_web_fallback_session.assert_not_called()
    assert fake_db.add_chat_message.call_args.args[0] == "originating-whatsapp-session"
    assert event_stream.emit.call_args.args[1]["session_id"] == "originating-whatsapp-session"


def test_channel_owned_by_another_agent_is_not_available(routing_env):
    fake_db, channel_manager, event_stream = routing_env
    fake_db.get_session_with_details.return_value = {
        "id": "external-session",
        "agent_id": "target_agent",
        "external_user_id": "external-user",
        "channel_id": "channel-1",
    }
    fake_db.get_channel.return_value = {
        "id": "channel-1",
        "agent_id": "other_agent",
        "type": "telegram",
        "enabled": True,
    }
    channel_manager.is_running.return_value = True

    result = _notify(session_id="external-session")

    assert result["success"] is True
    assert result["session_id"] == "external-session"
    assert result["fallback_reason"] == "inactive_channel"
    channel_manager.is_running.assert_not_called()
    fake_db.get_web_fallback_session.assert_not_called()
    assert fake_db.add_chat_message.call_args.args[0] == "external-session"
    assert event_stream.emit.call_args.args[1]["session_id"] == "external-session"


def test_external_delivery_sends_through_active_channel(routing_env):
    fake_db, channel_manager, event_stream = routing_env
    fake_db.get_session_with_details.return_value = {
        "id": "external-session", "agent_id": "target_agent",
        "external_user_id": "external-user", "channel_id": "channel-1",
    }
    fake_db.get_channel.return_value = {
        "id": "channel-1", "agent_id": "target_agent",
        "type": "telegram", "enabled": True,
    }
    channel_manager.is_running.return_value = True
    instance = mock.MagicMock(is_running=True)
    instance.has_send_error.return_value = False
    channel_manager.get_channel_instance.return_value = instance

    result = _notify(session_id="external-session", deliver_external=True)

    assert result["success"] is True
    assert result["delivery"] == "external_channel"
    instance.send_message.assert_called_once_with(
        "external-user", "[AGENT/Sender] Finished",
    )
    assert event_stream.emit.call_args.args[1]["session_id"] == "external-session"


def test_external_send_failure_is_reported(routing_env):
    fake_db, channel_manager, _ = routing_env
    fake_db.get_session_with_details.return_value = {
        "id": "external-session", "agent_id": "target_agent",
        "external_user_id": "external-user", "channel_id": "channel-1",
    }
    fake_db.get_channel.return_value = {
        "id": "channel-1", "agent_id": "target_agent",
        "type": "telegram", "enabled": True,
    }
    channel_manager.is_running.return_value = True
    instance = mock.MagicMock(is_running=True)
    instance.has_send_error.return_value = True
    channel_manager.get_channel_instance.return_value = instance

    result = _notify(session_id="external-session", deliver_external=True)

    assert result["success"] is False
    assert result["reason"] == "channel_send_failed"
    assert result["delivery"] == "database_only"


def test_active_external_session_remains_direct(routing_env):
    fake_db, channel_manager, event_stream = routing_env
    fake_db.get_session_with_details.return_value = {
        "id": "external-session",
        "agent_id": "target_agent",
        "external_user_id": "external-user",
        "channel_id": "channel-1",
    }
    fake_db.get_channel.return_value = {
        "id": "channel-1",
        "agent_id": "target_agent",
        "type": "telegram",
        "enabled": True,
    }
    channel_manager.is_running.return_value = True

    result = _notify(session_id="external-session")

    assert result["success"] is True
    assert result["session_id"] == "external-session"
    assert result["route"] == "direct"
    assert result["fallback_reason"] is None
    assert event_stream.emit.call_args.args[1]["channel_id"] == "channel-1"


@pytest.mark.parametrize(
    "external_user_id",
    ["web-user", "__agent__sender_agent"],
    ids=["web", "inter-agent"],
)
def test_channel_less_sessions_remain_valid(routing_env, external_user_id):
    fake_db, channel_manager, _ = routing_env
    fake_db.get_session_with_details.return_value = {
        "id": "safe-session",
        "agent_id": "target_agent",
        "external_user_id": external_user_id,
        "channel_id": None,
    }

    result = _notify(session_id="safe-session")

    assert result["success"] is True
    assert result["session_id"] == "safe-session"
    assert result["route"] == "direct"
    fake_db.get_channel.assert_not_called()
    channel_manager.is_running.assert_not_called()


def test_inactive_explicit_channel_route_falls_back_without_creating_session(routing_env):
    fake_db, _, event_stream = routing_env
    fake_db.get_channel.return_value = {"id": "channel-1", "enabled": False}
    fake_db.get_web_fallback_session.return_value = {
        "id": "web-session",
        "agent_id": "target_agent",
        "external_user_id": "web-user",
        "channel_id": None,
    }

    result = _notify(external_user_id="external-user", channel_id="channel-1")

    assert result["success"] is True
    assert result["session_id"] == "web-session"
    assert result["route"] == "web_fallback"
    fake_db.get_or_create_session.assert_not_called()
    assert event_stream.emit.call_args.args[1]["external_user_id"] == "web-user"
