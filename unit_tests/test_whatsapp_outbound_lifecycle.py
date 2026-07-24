"""Focused tests for WhatsApp outbound correlation and LID retry eligibility."""

from unittest.mock import patch

from backend.channels.whatsapp import WhatsAppChannel


def _channel():
    with patch("backend.channels.base.BaseChannel.__init__", return_value=None):
        channel = WhatsAppChannel("channel-1", "agent-1", {"bridge_port": 3001})
    channel.channel_id = "channel-1"
    channel.agent_id = "agent-1"
    channel.config = {}
    return channel


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
    assert sent[0]["retry_eligible"] is True
    assert sent[0]["retry_jid"] == "628111@s.whatsapp.net"
    assert sent[0]["correlation_id"]


def test_phone_dm_keeps_phone_jid_without_fallback():
    channel = _channel()
    channel._jid_map["628222"] = "628222@s.whatsapp.net"
    sent = []

    with patch.object(channel, "_bridge_send_retry", side_effect=lambda body, _: sent.append(body) or True), \
            patch.object(channel, "send_typing"), patch.object(channel, "_clear_typing"):
        channel._do_send("628222", "response")

    assert sent[0]["to"] == "628222@s.whatsapp.net"
    assert sent[0]["retry_eligible"] is False
    assert sent[0]["retry_jid"] is None


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


def test_group_jid_is_preserved_and_not_lid_retry_eligible():
    channel = _channel()
    group_id = "120363000000000001"
    channel._jid_map[group_id] = f"{group_id}@g.us"
    sent = []

    with patch.object(channel, "_bridge_send_retry", side_effect=lambda body, _: sent.append(body) or True), \
            patch.object(channel, "send_typing"), patch.object(channel, "_clear_typing"):
        channel._do_send(group_id, "group response")

    assert sent[0]["to"] == f"{group_id}@g.us"
    assert sent[0]["retry_eligible"] is False


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
