"""Tests for the Telegram tool-call notification line (`show_tool_calls`).

``TelegramChannel.start()`` runs against a fake bot (no polling thread), the
debounce timers are faked so nothing depends on wall-clock time, and the
``tool_executed`` handler is driven directly.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.channels import telegram as tg
from backend.event_stream import event_stream


class _FakeTimer:
    """Stand-in for threading.Timer: records itself, fires only when told to."""

    instances = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or []
        self.cancelled = False
        self.started = False
        _FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.function(*self.args)


@pytest.fixture
def harness(monkeypatch):
    _FakeTimer.instances = []
    monkeypatch.setattr(tg.threading, 'Timer', _FakeTimer)
    monkeypatch.setattr(tg.threading, 'Thread', lambda **kwargs: MagicMock())

    registered, removed = {}, {}
    monkeypatch.setattr(event_stream, 'on',
                        lambda name, cb: registered.setdefault(name, []).append(cb))
    monkeypatch.setattr(event_stream, 'off',
                        lambda name, cb: removed.setdefault(name, []).append(cb))

    def build(config):
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            send_chat_action=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        app = MagicMock()
        app.bot = bot
        builder = MagicMock()
        builder.token.return_value.build.return_value = app
        monkeypatch.setattr('telegram.ext.ApplicationBuilder', lambda: builder)
        # BaseChannel reads the agent row for the outbound buffer window.
        monkeypatch.setattr(tg.TelegramChannel, '_outbound_buffer_seconds', 1.5, raising=False)
        channel = tg.TelegramChannel('tg-chan', 'agent-1', config)
        channel.start()
        return channel, bot

    yield SimpleNamespace(build=build, registered=registered, removed=removed,
                          timers=_FakeTimer)


def _tool(channel, name='read_file', user='12345', error=False):
    channel._tool_executed_handler({
        'channel_id': 'tg-chan', 'external_user_id': user,
        'tool_name': name, 'has_error': error,
    })


def test_flag_off_registers_no_tool_handler(harness):
    channel, bot = harness.build({'bot_token': 'x'})
    assert channel._tool_executed_handler is None
    assert 'tool_executed' not in harness.registered
    assert harness.timers.instances == []
    assert bot.send_message.await_count == 0


def test_flag_on_registers_the_handler(harness):
    channel, _ = harness.build({'bot_token': 'x', 'show_tool_calls': True})
    assert channel._tool_executed_handler is not None
    assert channel._tool_executed_handler in harness.registered['tool_executed']


def test_burst_is_coalesced_into_one_line(harness):
    channel, bot = harness.build({'bot_token': 'x', 'show_tool_calls': True})
    for _ in range(3):
        _tool(channel)
    # Nothing sent during the window — one timer is armed instead.
    assert bot.send_message.await_count == 0
    assert len(harness.timers.instances) == 1

    harness.timers.instances[0].fire()
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.kwargs['text'].endswith('Tool: read_file (3x)')


def test_error_in_burst_marks_the_line(harness):
    channel, bot = harness.build({'bot_token': 'x', 'show_tool_calls': True})
    _tool(channel, 'read_file')
    _tool(channel, 'bash', error=True)
    harness.timers.instances[0].fire()
    text = bot.send_message.await_args.kwargs['text']
    assert text.startswith('\u26a0\ufe0f')
    assert 'bash (1x)' in text and 'read_file (1x)' in text


def test_line_is_held_while_a_reply_is_streaming(harness):
    channel, bot = harness.build({'bot_token': 'x', 'show_tool_calls': True})
    _tool(channel)
    # A reply is mid-stream: the outbound buffer still holds its text.
    with channel._buf_lock:
        channel._buf['12345'] = 'partial answer'

    harness.timers.instances[0].fire()
    assert bot.send_message.await_count == 0, 'sent into a streaming reply'
    retry = harness.timers.instances[-1]
    assert retry is not harness.timers.instances[0] and retry.interval == tg._TOOL_NOTIFY_RETRY

    # Reply flushed — the queued line goes out, still coalesced.
    with channel._buf_lock:
        channel._buf.pop('12345')
    retry.fire()
    assert bot.send_message.await_count == 1
    assert 'read_file (1x)' in bot.send_message.await_args.kwargs['text']


def test_stop_cancels_a_pending_timer(harness):
    channel, bot = harness.build({'bot_token': 'x', 'show_tool_calls': True})
    _tool(channel)
    timer = harness.timers.instances[0]

    channel.stop()

    assert timer.cancelled
    assert channel._tool_executed_handler is None
    # Even a timer that had already been scheduled must not talk to a dead channel.
    timer.cancelled = False
    timer.fire()
    assert bot.send_message.await_count == 0
