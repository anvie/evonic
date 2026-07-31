"""Deterministic unit tests for WhatsApp safe outbound delivery."""

from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from backend.channels.whatsapp_dispatcher import (
    WhatsAppOutboundDispatcher,
    _natural_whatsapp_format,
    _split_message,
)


@pytest.fixture(autouse=True)
def enable_testing_mode():
    """Keep dispatcher tests independent from the full Flask application."""
    yield


class FakeChannel:
    channel_id = "channel-1"
    agent_id = "agent-1"

    def __init__(self):
        self.sent = []
        self.presence = []

    def _do_send(self, user_id, text, session_id=None):
        self.sent.append((user_id, text, session_id))
        return True

    def send_typing(self, user_id, state="composing"):
        self.presence.append((user_id, state))


def _settings(**overrides):
    values = {
        "whatsapp_safe_delivery_enabled": "1",
        "whatsapp_pool_window_seconds": "2.0",
        "whatsapp_min_send_interval_seconds": "0.1",
        "whatsapp_typing_chars_per_second": "20.0",
        "whatsapp_max_typing_delay_seconds": "15.0",
        "whatsapp_delay_jitter_ratio": "0",
        "whatsapp_max_outbound_per_minute": "30",
        "whatsapp_natural_formatting_enabled": "1",
    }
    values.update(overrides)
    return lambda key, default: values.get(key, default)


def _dispatcher(**settings):
    channel = FakeChannel()
    dispatcher = WhatsAppOutboundDispatcher(channel, _settings(**settings))
    return dispatcher, channel


def test_enqueue_drops_empty_and_no_response_without_starting_worker():
    dispatcher, channel = _dispatcher()
    with patch("backend.channels.whatsapp_dispatcher.threading.Thread") as thread:
        dispatcher.enqueue("user-1", "   ")
        dispatcher.enqueue("user-1", "(No response)")

    thread.assert_not_called()
    assert dispatcher._queues == {}
    assert channel.sent == []


def test_enqueue_deduplicates_same_recipient_text_and_keeps_other_recipients():
    dispatcher, _ = _dispatcher()
    with patch("backend.channels.whatsapp_dispatcher.threading.Thread"):
        dispatcher.enqueue("user-1", "same", session_id="s-1")
        dispatcher.enqueue("user-1", "same", session_id="s-2")
        dispatcher.enqueue("user-2", "same", session_id="s-3")

    assert list(dispatcher._queues["user-1"])[0]["text"] == "same"
    assert list(dispatcher._queues["user-2"])[0]["session_id"] == "s-3"


def test_pooling_and_final_merge_preserve_fifo_text_order():
    dispatcher, _ = _dispatcher()
    with patch("backend.channels.whatsapp_dispatcher.threading.Thread"):
        dispatcher.enqueue("user-1", "first", session_id="s-1", is_final=False)
        dispatcher.enqueue("user-1", "second", session_id="s-1", is_final=False)
        dispatcher.enqueue("user-1", "final", session_id="s-1", is_final=True)

    queue = dispatcher._queues["user-1"]
    assert len(queue) == 1
    assert queue[0]["text"] == "first\n\nsecond\n\nfinal"
    assert queue[0]["is_final"] is True


def test_pop_ready_merges_ready_items_and_preserves_session_id():
    dispatcher, _ = _dispatcher()
    dispatcher._queues["user-1"] = deque([
        {"text": "one", "session_id": "s-1", "is_final": True, "queued_at": 1},
        {"text": "two", "session_id": "s-2", "is_final": True, "queued_at": 1},
    ])
    dispatcher._locks["user-1"] = MagicMock()

    with patch("backend.channels.whatsapp_dispatcher.time.monotonic", return_value=2):
        item = dispatcher._pop_ready_impl(dispatcher._locks["user-1"], "user-1", 2)

    assert item == {"text": "one\n\ntwo", "session_id": "s-2", "sent_at": 2}
    assert not dispatcher._queues["user-1"]


def test_delivery_uses_adaptive_delay_and_presence_without_message_body_events():
    dispatcher, channel = _dispatcher(
        whatsapp_typing_chars_per_second="10",
        whatsapp_delay_jitter_ratio="0",
    )
    events = []
    dispatcher._emit = lambda event, *args, **kwargs: events.append((event, kwargs))

    with patch.object(dispatcher, "_interruptible_sleep") as sleep:
        dispatcher._deliver({"text": "12345", "session_id": "s-1"}, "user-1")

    assert channel.sent == [("user-1", "12345", "s-1")]
    assert channel.presence == [("user-1", "composing"), ("user-1", "paused")]
    assert sleep.call_args.args == (0.5,)
    sent_event = next(data for name, data in events if name == "sent")
    assert "text" not in sent_event
    assert "body" not in sent_event


def test_delivery_respects_rolling_quota_and_emits_throttled_event():
    dispatcher, channel = _dispatcher(whatsapp_max_outbound_per_minute="1")
    events = []
    dispatcher._emit = lambda event, *args, **kwargs: events.append(event)
    dispatcher._interruptible_sleep = lambda _: None

    dispatcher._deliver({"text": "first"}, "user-1")
    dispatcher._deliver({"text": "second"}, "user-1")

    assert [text for _, text, _ in channel.sent] == ["first"]
    assert "sent" in events
    assert "throttled" in events


def test_delivery_reports_false_send_as_failed_and_stops_chunk_sequence():
    dispatcher, channel = _dispatcher()
    channel._do_send = MagicMock(return_value=False)
    events = []
    dispatcher._emit = lambda event, *args, **kwargs: events.append(event)
    dispatcher._interruptible_sleep = lambda _: None

    dispatcher._deliver({"text": "failed"}, "user-1")

    channel._do_send.assert_called_once()
    assert "failed" in events
    assert "sent" not in events


def test_delivery_splits_long_text_and_preserves_session_id():
    dispatcher, channel = _dispatcher()
    dispatcher._interruptible_sleep = lambda _: None
    text = "a" * 4097

    dispatcher._deliver({"text": text, "session_id": "session-1"}, "user-1")

    assert [len(call[1]) for call in channel.sent] == [4096, 1]
    assert all(call[2] == "session-1" for call in channel.sent)


def test_restriction_pause_blocks_delivery_until_operator_resumes():
    dispatcher, channel = _dispatcher()
    dispatcher.pause_for_restriction(reason="RESTRICT_ALL_COMPANIONS")
    dispatcher._interruptible_sleep = lambda _: None

    dispatcher._deliver({"text": "blocked"}, "user-1")
    assert channel.sent == []

    dispatcher.resume_after_restriction()
    dispatcher._deliver({"text": "allowed"}, "user-1")
    assert [text for _, text, _ in channel.sent] == ["allowed"]


def test_natural_formatting_handles_headings_lists_links_code_and_urls():
    text = "# Heading\n- item\n[docs](https://example.test/a?q=1)\n`inline`\n```py\nprint('x')\n```\nhttps://example.test/raw"

    formatted = _natural_whatsapp_format(text)

    assert "Heading" in formatted
    assert "• item" in formatted
    assert "docs: https://example.test/a?q=1" in formatted
    assert "CODE:" in formatted
    assert "https://example.test/raw" in formatted


def test_split_message_keeps_unicode_and_respects_limit():
    chunks = _split_message("🙂" * 4097)

    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunks) == "🙂" * 4097


def test_restriction_deadline_auto_resumes():
    dispatcher, _ = _dispatcher()
    dispatcher.pause_for_restriction("2000-01-01T00:00:00Z")

    assert dispatcher._restriction_active() is False
    assert dispatcher._restriction_until == 0


def test_terminal_restriction_callback_pauses_channel_dispatcher():
    from backend.channels.whatsapp import WhatsAppChannel

    with patch("backend.channels.base.BaseChannel.__init__", return_value=None):
        channel = WhatsAppChannel("channel-1", "agent-1", {"bridge_port": 3001})
    channel.channel_id = "channel-1"
    channel.agent_id = "agent-1"
    channel._dispatcher = MagicMock()
    payload = {
        "event": "outbound_status",
        "status": "failed",
        "terminal": True,
        "reachout_timelocked": True,
        "reachout_enforcement_type": "RESTRICT_ALL_COMPANIONS",
        "reachout_enforcement_ends": "2030-01-01T00:00:00Z",
        "session_id": "session-1",
    }

    with patch("models.db.db"), \
            patch("backend.event_stream.event_stream.emit"), \
            patch.object(channel, "_record_reachout_restriction"):
        channel.handle_callback(payload)

    channel._dispatcher.pause_for_restriction.assert_called_once_with(
        "2030-01-01T00:00:00Z", "RESTRICT_ALL_COMPANIONS"
    )


def test_resume_outbound_route_calls_operator_resume_without_restart():
    from flask import Flask
    from routes.agents import agents_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(agents_bp)
    dispatcher = MagicMock()
    channel = MagicMock()
    channel._dispatcher = dispatcher

    with patch("routes.agents.db.get_channel", return_value={
        "id": "channel-1", "agent_id": "agent-1", "type": "whatsapp",
    }), \
            patch("backend.channels.registry.channel_manager.get_channel_instance",
                  return_value=channel), \
            patch("backend.channels.whatsapp.WhatsAppChannel", type(channel)):
        response = app.test_client().post(
            "/api/agents/agent-1/channels/channel-1/resume-outbound"
        )

    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    dispatcher.resume_after_restriction.assert_called_once_with()


def test_delivery_preserves_fifo_and_keeps_chats_independent():
    dispatcher, channel = _dispatcher()
    dispatcher._interruptible_sleep = lambda _: None

    dispatcher._deliver({"text": "first", "session_id": "s-1"}, "user-1")
    dispatcher._deliver({"text": "other", "session_id": "s-2"}, "user-2")
    dispatcher._deliver({"text": "second", "session_id": "s-3"}, "user-1")

    assert [(uid, text) for uid, text, _ in channel.sent] == [
        ("user-1", "first"), ("user-2", "other"), ("user-1", "second")
    ]
    assert [session for _, _, session in channel.sent] == ["s-1", "s-2", "s-3"]


def test_delay_respects_hard_minimum_interval():
    dispatcher, channel = _dispatcher(
        whatsapp_typing_chars_per_second="1",
        whatsapp_max_typing_delay_seconds="2",
        whatsapp_min_send_interval_seconds="5",
    )
    dispatcher._last_send["user-1"] = 0
    dispatcher._interruptible_sleep = MagicMock()
    with patch("backend.channels.whatsapp_dispatcher.time.monotonic", return_value=1):
        dispatcher._deliver({"text": "x"}, "user-1")

    # The hard minimum is applied before the configured typing-delay cap.
    assert dispatcher._interruptible_sleep.call_args.args == (2.0,)
    assert channel.sent == [("user-1", "x", None)]


def test_jitter_is_applied_within_configured_ratio():
    dispatcher, _ = _dispatcher(whatsapp_delay_jitter_ratio="0.2")
    dispatcher._interruptible_sleep = MagicMock()
    with patch("backend.channels.whatsapp_dispatcher.secrets.randbelow", return_value=1000):
        dispatcher._deliver({"text": "1234567890"}, "user-1")

    # The base delay is 0.5s, so the upper 20% jitter boundary is 0.6s.
    assert dispatcher._interruptible_sleep.call_args.args[0] == 0.6


def test_shutdown_cancels_workers_and_sends_paused_presence():
    dispatcher, channel = _dispatcher()
    cancel = MagicMock()
    dispatcher._workers["user-1"] = cancel

    dispatcher.shutdown()

    cancel.set.assert_called_once_with()
    assert ("user-1", "paused") in channel.presence
    dispatcher.shutdown()
    cancel.set.assert_called_once_with()


def test_ambiguous_send_error_is_not_retried():
    dispatcher, channel = _dispatcher()
    channel._do_send = MagicMock(side_effect=TimeoutError("read timeout"))
    dispatcher._interruptible_sleep = lambda _: None

    dispatcher._deliver({"text": "ambiguous"}, "user-1")

    channel._do_send.assert_called_once()


def test_throttled_delivery_requests_requeue_without_sending():
    dispatcher, channel = _dispatcher(whatsapp_max_outbound_per_minute="1")
    dispatcher._outbound_window.append(0)
    dispatcher._interruptible_sleep = lambda _: None

    with patch("backend.channels.whatsapp_dispatcher.time.monotonic", return_value=1):
        retry = dispatcher._deliver({"text": "deferred", "session_id": "s-1"}, "user-1")

    assert retry is True
    assert channel.sent == []


def test_restriction_after_delay_requests_requeue_and_pauses_presence():
    dispatcher, channel = _dispatcher()
    dispatcher._restriction_until = float("inf")
    dispatcher._interruptible_sleep = lambda _: None

    assert dispatcher._deliver({"text": "deferred"}, "user-1") is True
    assert channel.sent == []
    assert channel.presence == [("user-1", "composing"), ("user-1", "paused")]


def test_queue_event_contains_depth_without_message_body():
    dispatcher, _ = _dispatcher()
    events = []
    dispatcher._emit = lambda event, *args, **kwargs: events.append((event, kwargs))
    with patch("backend.channels.whatsapp_dispatcher.threading.Thread"):
        dispatcher.enqueue("user-1", "private outbound body", session_id="s-1")

    assert events[0][0] == "queued"
    assert events[0][1]["queue_depth"] == 1
    assert all("private outbound body" not in repr(event) for event in events)
