"""Focused tests for WhatsApp outbound correlation and persisted JID routing."""

import os
import threading
import tempfile
from unittest.mock import MagicMock, patch

from backend.channels.whatsapp import WhatsAppChannel


def _channel():
    with patch("backend.channels.base.BaseChannel.__init__", return_value=None):
        channel = WhatsAppChannel("channel-1", "agent-1", {"bridge_port": 3001})
    channel.channel_id = "channel-1"
    channel.agent_id = "agent-1"
    channel.config = {}
    # BaseChannel.__init__ is patched out above, so re-create the outbound
    # coalescing buffer state it normally sets up.
    channel._buf = {}
    channel._buf_timers = {}
    channel._buf_lock = threading.Lock()
    channel._last_sent = {}
    channel._send_errors = {}
    channel._send_errors_lock = threading.Lock()
    channel._send_error_ttl = 3600
    return channel


def test_whatsapp_suppresses_buffered_intermediate_output_with_dispatcher():
    channel = _channel()
    channel._dispatcher = MagicMock()

    channel.send_message_buffered("628222", "technical progress", session_id="session-1")

    channel._dispatcher.enqueue.assert_not_called()


def test_whatsapp_suppresses_buffered_intermediate_output_without_dispatcher():
    channel = _channel()

    with patch("backend.channels.base.BaseChannel.send_message_buffered") as buffered:
        channel.send_message_buffered("628222", "technical progress", session_id="session-1")

    buffered.assert_not_called()


def test_whatsapp_queues_final_output_with_dispatcher():
    channel = _channel()
    channel._dispatcher = MagicMock()

    channel.send_message("628222", "final answer", session_id="session-1")

    channel._dispatcher.enqueue.assert_called_once_with(
        "628222", "final answer", session_id="session-1", is_final=True)


def test_whatsapp_sends_final_output_without_dispatcher():
    channel = _channel()

    with patch("backend.channels.base.BaseChannel.send_message") as send:
        channel.send_message("628222", "final answer", session_id="session-1")

    send.assert_called_once_with("628222", "final answer", session_id="session-1")


def test_whatsapp_system_instructions_require_natural_non_human_replies():
    instructions = _channel().get_system_instructions()

    assert "concisely, naturally, and conversationally" in instructions
    assert "repetitive greetings" in instructions
    assert "one complete combined answer" in instructions
    assert "Avoid Markdown constructs" in instructions
    assert "Do not claim to be human" in instructions
    assert "AI assistant" in instructions
    assert "NEVER use markdown symbols" not in instructions


def test_lid_dm_uses_inbound_jid_and_pn_as_recovery_fallback():
    channel = _channel()
    payload = {
        "from": "lid-user",
        "jid": "lid-user@lid",
        "alt_sender": "628111",
        "alt_jid": "628111@s.whatsapp.net",
        "text": "hello",
    }

    with patch.object(channel, "_resolve_agent", return_value=None):
        channel.handle_callback(payload)

    assert channel._jid_map["lid-user"] == "lid-user@lid"
    assert channel._alternate_jids["lid-user"] == "628111@s.whatsapp.net"

    sent = []
    with patch.object(channel, "_bridge_send_retry", side_effect=lambda body, _: sent.append(body) or True), \
            patch.object(channel, "send_typing"), patch.object(channel, "_clear_typing"):
        channel._do_send("lid-user", "response")

    assert sent[0]["to"] == "lid-user@lid"
    assert sent[0]["correlation_id"]


def test_phone_dm_keeps_phone_jid():
    channel = _channel()
    channel._jid_map["628222"] = "628222@s.whatsapp.net"
    sent = []

    with patch.object(channel, "_bridge_send_retry", side_effect=lambda body, _: sent.append(body) or True), \
            patch.object(channel, "send_typing"), patch.object(channel, "_clear_typing"):
        channel._do_send("628222", "response")

    assert sent[0]["to"] == "628222@s.whatsapp.net"
    assert sent[0]["correlation_id"]


def test_persisted_jid_route_survives_channel_reconstruction():
    channel = _channel()
    channel._load_persisted_jid_routes({
        "jid_routes": {
            "lid-user": {
                "primary": "lid-user@lid",
                "alternate": "628111@s.whatsapp.net",
            }
        }
    })

    assert channel._jid_map["lid-user"] == "lid-user@lid"
    assert channel._alternate_jids["lid-user"] == "628111@s.whatsapp.net"


def test_inbound_debug_event_contains_identity_transport_and_route_metadata():
    channel = _channel()
    payload = {
        "from": "lid-user",
        "jid": "lid-user@lid",
        "alt_sender": "628111",
        "alt_jid": "628111@s.whatsapp.net",
        "message_id": "message-1",
        "message_timestamp": 1720000000,
        "content_type": "extendedTextMessage",
        "wrapper_types": ["ephemeralMessage"],
        "payload_keys": ["extendedTextMessage"],
        "quoted_message": {"type": "image"},
        "bot_mentioned": False,
        "text": "hello",
    }

    with patch.object(channel, "_resolve_agent", return_value="agent-1"), \
            patch.object(channel, "_gate_sender", return_value=False), \
            patch("backend.event_stream.event_stream.emit") as emit:
        channel.handle_callback(payload)

    event = next(
        call.args[1] for call in emit.call_args_list
        if call.args[0] == "whatsapp_inbound"
    )
    assert event["message_id"] == "message-1"
    assert event["jid_namespace"] == "lid"
    assert event["alt_jid_namespace"] == "s.whatsapp.net"
    assert event["reply_jid"] == "lid-user@lid"
    assert event["fallback_jid"] == "628111@s.whatsapp.net"
    assert event["route_status"] == "matched"
    assert event["routed_agent_id"] == "agent-1"
    assert event["content_type"] == "extendedTextMessage"
    assert event["wrapper_types"] == ["ephemeralMessage"]
    assert event["quoted_type"] == "image"


def test_group_jid_is_preserved_with_stable_correlation():
    channel = _channel()
    group_id = "120363000000000001"
    channel._jid_map[group_id] = f"{group_id}@g.us"
    sent = []

    with patch.object(channel, "_bridge_send_retry", side_effect=lambda body, _: sent.append(body) or True), \
            patch.object(channel, "send_typing"), patch.object(channel, "_clear_typing"):
        channel._do_send(group_id, "group response")

    assert sent[0]["to"] == f"{group_id}@g.us"
    assert sent[0]["correlation_id"]


def test_outbound_status_callback_is_forwarded_with_stable_correlation():
    channel = _channel()
    payload = {
        "event": "outbound_status",
        "correlation_id": "correlation-1",
        "status": "failed",
        "retry_count": 1,
        "reason": "NACK 463",
    }

    with patch("backend.event_stream.event_stream.emit") as emit:
        channel.handle_callback(payload)

    event_name, event = emit.call_args.args
    assert event_name == "whatsapp_outbound_status"
    assert event["correlation_id"] == "correlation-1"
    assert event["status"] == "failed"
    assert event["channel_id"] == "channel-1"


def test_session_id_is_included_in_local_bridge_payload():
    channel = _channel()
    channel._jid_map["628222"] = "628222@s.whatsapp.net"
    sent = []

    with patch.object(channel, "_bridge_send_retry",
                      side_effect=lambda body, _: sent.append(body) or True), \
            patch.object(channel, "send_typing"), patch.object(channel, "_clear_typing"):
        channel._do_send("628222", "response", session_id="session-1")

    assert sent[0]["session_id"] == "session-1"


def test_attachment_send_retains_route_and_reports_acceptance_metadata():
    channel = _channel()
    channel._jid_map["lid-user"] = "lid-user@lid"
    payloads = []

    def accept_attachment(path, payload):
        payloads.append((path, payload))
        return {
            "success": True,
            "status": "accepted",
            "correlation_id": payload["correlation_id"],
            "message_id": "attachment-key-1",
        }

    with tempfile.NamedTemporaryFile(suffix=".pdf") as attachment, \
            patch.object(channel, "_bridge_post", side_effect=accept_attachment), \
            patch.object(channel, "send_typing"), \
            patch.object(channel, "_clear_typing"), \
            patch("backend.event_stream.event_stream.emit") as emit:
        attachment.write(b"%PDF-1.7\n%%EOF\n")
        attachment.flush()

        result = channel._do_send_file(
            "lid-user", attachment.name,
            caption="**Report**", mime_type="application/pdf")

    assert result is True
    bridge_path, payload = payloads[0]
    assert bridge_path == "/send-file"
    assert payload["to"] == "lid-user@lid"
    assert payload["caption"] == "Report"
    assert payload["mimeType"] == "application/pdf"
    assert payload["correlation_id"]
    event_name, event = emit.call_args.args
    assert event_name == "message_sent"
    assert event["status"] == "accepted"
    assert event["correlation_id"] == payload["correlation_id"]
    assert event["message_id"] == "attachment-key-1"


def test_attachment_send_rejects_non_accepted_bridge_status():
    channel = _channel()

    with tempfile.NamedTemporaryFile(suffix=".pdf") as attachment, \
            patch.object(channel, "_bridge_post", return_value={
                "success": False,
                "status": "failed",
                "correlation_id": "correlation-failed",
            }), \
            patch.object(channel, "send_typing"), \
            patch.object(channel, "_clear_typing"), \
            patch("backend.event_stream.event_stream.emit") as emit:
        attachment.write(b"%PDF-1.7\n%%EOF\n")
        attachment.flush()

        result = channel._do_send_file("628222", attachment.name)

    assert result is False
    emit.assert_not_called()


def test_attachment_send_rejects_missing_file_before_bridge_submission():
    channel = _channel()
    missing_path = os.path.join(tempfile.gettempdir(), "evonic-missing-attachment.pdf")

    with patch.object(channel, "_bridge_post") as bridge_post:
        result = channel._do_send_file("628222", missing_path)

    assert result is False
    bridge_post.assert_not_called()


def _restriction_payload():
    return {
        "event": "outbound_status",
        "correlation_id": "correlation-restricted",
        "session_id": "session-1",
        "status": "failed",
        "terminal": True,
        "reachout_timelocked": True,
        "reachout_enforcement_type": "RESTRICT_ALL_COMPANIONS",
        "reachout_enforcement_ends": "2026-07-30T06:59:55Z",
    }


def test_group_slash_command_response_is_sent_to_group_jid_once():
    channel = _channel()
    channel._running = True
    payload = {
        "from": "628111",
        "jid": "120363000000000001@g.us",
        "is_group": True,
        "bot_mentioned": True,
        "text": "/help",
    }
    sent = []
    mock_db = MagicMock()
    mock_db.get_agent.return_value = {"id": "agent-1", "enabled": True}
    mock_db.get_or_create_session.return_value = "session-1"
    mock_db.is_session_bot_enabled.return_value = True
    mock_db.list_session_attachments.return_value = []

    with patch("models.db.db", mock_db), \
            patch.object(channel, "_resolve_agent", return_value="agent-1"), \
            patch("backend.agent_runtime.agent_runtime.handle_message",
                  return_value={"response": "Available commands", "slash_command": True}), \
            patch.object(channel, "_do_send", side_effect=lambda *args, **kwargs: sent.append((args, kwargs))), \
            patch.object(channel, "send_typing"), \
            patch.object(channel, "_clear_typing"), \
            patch("backend.event_stream.event_stream.emit"):
        channel.handle_callback(payload)

    assert sent == [(("120363000000000001", "Available commands"),
                     {"session_id": "session-1"})]


def test_terminal_reachout_restriction_persists_and_emits_once():
    channel = _channel()
    mock_db = MagicMock()
    mock_db.get_session_with_details.return_value = {
        "id": "session-1", "agent_id": "agent-1", "channel_id": "channel-1"}
    mock_db.get_session_messages.side_effect = [[], [{
        "metadata": {
            "whatsapp_restriction_key":
                "RESTRICT_ALL_COMPANIONS|2026-07-30T06:59:55Z"}}]]

    with patch("models.db.db", mock_db), \
            patch("backend.event_stream.event_stream.emit") as emit:
        channel.handle_callback(_restriction_payload())
        channel.handle_callback(_restriction_payload())

    mock_db.add_chat_message.assert_called_once()
    args, kwargs = mock_db.add_chat_message.call_args
    assert args[:2] == ("session-1", "system")
    assert "RESTRICT_ALL_COMPANIONS" in args[2]
    assert "2026-07-30T06:59:55Z" in args[2]
    assert kwargs["agent_id"] == "agent-1"
    assert len([call for call in emit.call_args_list
                if call.args[0] == "whatsapp_restriction_warning"]) == 1


def test_shared_restriction_uses_session_routed_agent():
    channel = _channel()
    channel.agent_id = None
    mock_db = MagicMock()
    mock_db.get_session_with_details.return_value = {
        "id": "session-1", "agent_id": "routed-agent", "channel_id": "channel-1"}
    mock_db.get_session_messages.return_value = []
    event_stream = MagicMock()

    with patch.object(channel, "get_channel_type", return_value="whatsapp_shared"):
        channel._record_reachout_restriction(
            _restriction_payload(), mock_db, event_stream)

    assert mock_db.add_chat_message.call_args.kwargs["agent_id"] == "routed-agent"
    emitted = event_stream.emit.call_args.args[1]
    assert emitted["agent_id"] == "routed-agent"
    assert emitted["session_id"] == "session-1"


def test_restriction_callback_rejects_channel_mismatch():
    channel = _channel()
    mock_db = MagicMock()
    mock_db.get_session_with_details.return_value = {
        "id": "session-1", "agent_id": "agent-1", "channel_id": "another-channel"}
    event_stream = MagicMock()

    channel._record_reachout_restriction(
        _restriction_payload(), mock_db, event_stream)

    mock_db.add_chat_message.assert_not_called()
    event_stream.emit.assert_not_called()
