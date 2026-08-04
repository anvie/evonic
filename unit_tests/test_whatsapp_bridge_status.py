"""Regression coverage for WhatsApp bridge status reconciliation and UI actions."""

from pathlib import Path
from unittest.mock import Mock

import requests

from backend.channels.whatsapp import WhatsAppChannel


ROOT = Path(__file__).resolve().parents[1]
AGENT_DETAIL_TEMPLATE = ROOT / "templates" / "agent_detail.html"


def _channel(cached_status=None):
    channel = WhatsAppChannel.__new__(WhatsAppChannel)
    channel._bridge_port = 3210
    channel._last_bridge_status = cached_status
    return channel


def _response(status):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": status}
    return response


def test_live_connected_status_replaces_stale_disconnected_cache(monkeypatch):
    channel = _channel("disconnected")
    get = Mock(return_value=_response("connected"))
    monkeypatch.setattr(requests, "get", get)

    assert channel.get_bridge_status() == {"status": "connected"}
    assert channel._last_bridge_status == "connected"
    get.assert_called_once_with("http://127.0.0.1:3210/status", timeout=5)


def test_probe_failure_preserves_last_valid_status(monkeypatch):
    channel = _channel("connected")
    monkeypatch.setattr(
        requests,
        "get",
        Mock(side_effect=requests.ConnectionError("bridge unavailable")),
    )

    assert channel.get_bridge_status() == {"status": "connected"}
    assert channel._last_bridge_status == "connected"


def test_probe_failure_without_cache_uses_disconnected_fallback(monkeypatch):
    channel = _channel()
    monkeypatch.setattr(
        requests,
        "get",
        Mock(side_effect=requests.ConnectionError("bridge unavailable")),
    )

    assert channel.get_bridge_status() == {"status": "disconnected"}
    assert channel._last_bridge_status is None


def test_invalid_live_status_does_not_replace_cache(monkeypatch):
    channel = _channel("connected")
    monkeypatch.setattr(requests, "get", Mock(return_value=_response("starting")))

    assert channel.get_bridge_status() == {"status": "connected"}
    assert channel._last_bridge_status == "connected"


def test_scan_qr_action_requires_an_authentication_needed_status():
    template = AGENT_DETAIL_TEMPLATE.read_text(encoding="utf-8")

    assert (
        "const needsWhatsAppAuth = ch.bridge_status === 'qr_pending' || "
        "ch.bridge_status === 'disconnected';"
    ) in template
    assert "ch.type === 'whatsapp' && running && needsWhatsAppAuth" in template
    assert "const qrBtn = (ch.type === 'whatsapp' && running)" not in template
