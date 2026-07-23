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


def test_resolved_lid_dm_uses_phone_jid_and_enables_recovery_retry():
    channel = _channel()
    payload = {
        "from": "lid-user",
        "jid": "lid-user@lid",
        "alt_sender": "628111",
        "alt_jid": "628111@s.whatsapp.net",
        "text": "hello",
    }

    with patch.object(channel, "_gate_sender", return_value=False):
        channel.handle_callback(payload)

    assert channel._jid_map["lid-user"] == "628111@s.whatsapp.net"
    assert "lid-user" in channel._resolved_lid_dm_targets

    sent = []
    with patch.object(channel, "_bridge_send_retry", side_effect=lambda body, _: sent.append(body) or True), \
            patch.object(channel, "send_typing"), patch.object(channel, "_clear_typing"):
        channel._do_send("lid-user", "response")

    assert sent[0]["to"] == "628111@s.whatsapp.net"
    assert sent[0]["retry_eligible"] is True
    assert sent[0]["correlation_id"]


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
