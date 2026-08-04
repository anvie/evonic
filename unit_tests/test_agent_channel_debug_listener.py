"""Regression coverage for agent-owned WhatsApp Debug Listener controls and SSE."""

from pathlib import Path

import pytest
from flask import Flask

from models.db import db


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "agent_detail.html"


@pytest.fixture
def client():
    from routes.agents import agents_bp

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(agents_bp)
    return app.test_client()


@pytest.fixture
def owned_whatsapp_channel():
    db.create_agent({'id': 'listener-agent', 'name': 'Listener Agent'})
    return db.create_channel({
        'agent_id': 'listener-agent',
        'type': 'whatsapp',
        'name': 'Listener WhatsApp',
    })


def test_agent_channel_card_only_shows_listener_for_whatsapp():
    markup = TEMPLATE.read_text(encoding='utf-8')

    assert "const debugBtn = ch.type === 'whatsapp'" in markup
    assert "channelDebugListener.open('${ch.id}')" in markup
    assert "const channelDebugListener = {" in markup
    assert "/api/agents/${agentId}/channels/${channelId}/debug/listen" in markup
    assert 'id="channel-debug-modal"' in markup


def test_listener_uses_native_sse_lifecycle_and_preserves_retry():
    markup = TEMPLATE.read_text(encoding='utf-8')

    assert "const source = new EventSource(" in markup
    assert "source.onopen = markConnected;" in markup
    assert "source.addEventListener('connected', markConnected);" in markup
    assert "source.readyState === EventSource.CLOSED ? 'disconnected' : 'connecting'" in markup
    assert "listener._disconnect();\n            setTimeout" not in markup


def test_listener_rejects_non_owned_and_non_whatsapp_channels(client, owned_whatsapp_channel):
    db.create_agent({'id': 'other-agent', 'name': 'Other Agent'})
    foreign_channel = db.create_channel({
        'agent_id': 'other-agent', 'type': 'whatsapp', 'name': 'Foreign WhatsApp',
    })
    telegram_channel = db.create_channel({
        'agent_id': 'listener-agent', 'type': 'telegram', 'name': 'Listener Telegram',
    })

    assert client.get(
        f'/api/agents/listener-agent/channels/{foreign_channel}/debug/listen'
    ).status_code == 404
    assert client.get(
        f'/api/agents/listener-agent/channels/{telegram_channel}/debug/listen'
    ).status_code == 400
    assert client.get(
        '/api/agents/unknown/channels/not-a-channel/debug/listen'
    ).status_code == 404


def test_listener_streams_only_its_owned_channel_events(client, monkeypatch, owned_whatsapp_channel):
    from backend.event_stream import event_stream

    handlers = []
    monkeypatch.setattr(event_stream, 'on', lambda event, handler: handlers.append(handler))
    monkeypatch.setattr(event_stream, 'off', lambda event, handler: handlers.remove(handler))

    response = client.get(
        f'/api/agents/listener-agent/channels/{owned_whatsapp_channel}/debug/listen',
        buffered=False,
    )
    stream = iter(response.response)
    assert b'event: connected' in next(stream)
    assert len(handlers) == 1

    handlers[0]({'channel_id': 'another-channel', 'text': 'not for this listener'})
    handlers[0]({'channel_id': owned_whatsapp_channel, 'text': 'for this listener'})

    event = next(stream)
    assert b'event: whatsapp_inbound' in event
    assert b'for this listener' in event
    assert b'not for this listener' not in event

    response.close()
