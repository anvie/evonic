"""Tests for channel actions: registry, validation, encoding and the text fallback."""
import time
from unittest.mock import patch

import pytest

from backend import channel_actions as ca
from backend.channel_actions import (
    ChannelActionError,
    decode_action,
    dispatch_channel_action,
    encode_action,
    handle_text_reply,
    normalize_actions,
    register_channel_action_handler,
    remember_text_options,
    render_text_options,
    take_text_option,
    unregister_channel_action_handler,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    ca._reset_for_tests()
    yield
    ca._reset_for_tests()


def _actions():
    return normalize_actions([
        {'label': 'Useful', 'namespace': 'demo', 'payload': 'd1:up'},
        {'label': 'Not relevant', 'namespace': 'demo', 'payload': 'd1:nr'},
    ])


# ── validation ───────────────────────────────────────────────────────

def test_normalize_returns_clean_copies():
    raw = [{'label': ' Yes ', 'namespace': 'demo', 'payload': 1, 'extra': 'x'}]
    assert normalize_actions(raw) == [{'label': 'Yes', 'namespace': 'demo', 'payload': '1'}]


def test_normalize_empty_is_empty_list():
    assert normalize_actions(None) == []
    assert normalize_actions([]) == []


@pytest.mark.parametrize('bad', [
    'not-a-list',
    [{'label': '', 'namespace': 'demo', 'payload': 'p'}],
    [{'label': 'x' * 41, 'namespace': 'demo', 'payload': 'p'}],
    [{'label': 'Ok', 'namespace': 'Bad Space', 'payload': 'p'}],
    [{'label': 'Ok', 'namespace': 'approve', 'payload': 'p'}],
    [{'label': 'Ok', 'namespace': 'demo', 'payload': 'p' * 60}],
    [{'label': str(i), 'namespace': 'demo', 'payload': 'p'} for i in range(9)],
])
def test_normalize_rejects_malformed(bad):
    with pytest.raises(ChannelActionError):
        normalize_actions(bad)


def test_reserved_namespace_cannot_register():
    with pytest.raises(ChannelActionError):
        register_channel_action_handler('aq', lambda p, c: None)


# ── encoding ─────────────────────────────────────────────────────────

def test_encode_decode_round_trip():
    action = {'label': 'Yes', 'namespace': 'demo', 'payload': 'a:b:c'}
    assert decode_action(encode_action(action)) == ('demo', 'a:b:c')


@pytest.mark.parametrize('data', ['', 'approve:123', 'act:', 'act:Bad:x', 'act:demo'])
def test_decode_ignores_non_actions(data):
    assert decode_action(data) is None


# ── dispatch ─────────────────────────────────────────────────────────

def test_dispatch_calls_handler_with_payload_and_context():
    seen = {}

    def handler(payload, context):
        seen.update(payload=payload, context=context)
        return 'Noted.'

    register_channel_action_handler('demo', handler)
    ack = dispatch_channel_action('demo', 'd1:up', {'actor_id': '42'})
    assert ack == 'Noted.'
    assert seen == {'payload': 'd1:up', 'context': {'actor_id': '42'}}


def test_dispatch_without_handler_reports_unavailable():
    assert dispatch_channel_action('missing', 'x', {}) == ca.UNAVAILABLE_TEXT


def test_dispatch_handler_error_is_contained():
    def boom(payload, context):
        raise RuntimeError('nope')

    register_channel_action_handler('demo', boom)
    assert dispatch_channel_action('demo', 'x', {}) == ca.FAILED_TEXT


def test_dispatch_handler_returning_none_gives_none():
    register_channel_action_handler('demo', lambda p, c: None)
    assert dispatch_channel_action('demo', 'x', {}) is None


def test_unregister_only_removes_matching_handler():
    first = lambda p, c: 'first'  # noqa: E731
    second = lambda p, c: 'second'  # noqa: E731
    register_channel_action_handler('demo', first)
    unregister_channel_action_handler('demo', second)
    assert dispatch_channel_action('demo', 'x', {}) == 'first'
    unregister_channel_action_handler('demo', first)
    assert dispatch_channel_action('demo', 'x', {}) == ca.UNAVAILABLE_TEXT


# ── text fallback ────────────────────────────────────────────────────

def test_render_text_options_numbers_labels():
    text = render_text_options('Hello', _actions())
    assert text.endswith('Reply with a number:\n1. Useful\n2. Not relevant')


def test_take_text_option_consumes_choice_once():
    remember_text_options('ch', 'u1', _actions())
    assert take_text_option('ch', 'u1', ' 2 ')['payload'] == 'd1:nr'
    assert take_text_option('ch', 'u1', '2') is None


@pytest.mark.parametrize('message', ['hello', '3', '0', '2 please', ''])
def test_take_text_option_ignores_non_choices(message):
    remember_text_options('ch', 'u1', _actions())
    assert take_text_option('ch', 'u1', message) is None


def test_text_options_are_per_channel_and_user():
    remember_text_options('ch', 'u1', _actions())
    assert take_text_option('ch', 'u2', '1') is None
    assert take_text_option('other', 'u1', '1') is None


def test_newer_options_replace_older():
    remember_text_options('ch', 'u1', _actions())
    remember_text_options('ch', 'u1', normalize_actions(
        [{'label': 'Only', 'namespace': 'demo', 'payload': 'new'}]))
    assert take_text_option('ch', 'u1', '2') is None
    assert take_text_option('ch', 'u1', '1')['payload'] == 'new'


def test_expired_options_are_ignored():
    remember_text_options('ch', 'u1', _actions(), ttl_seconds=10)
    with patch.object(ca.time, 'time', return_value=time.time() + 11):
        assert take_text_option('ch', 'u1', '1') is None


def test_handle_text_reply_dispatches_with_text_context():
    seen = {}
    register_channel_action_handler('demo', lambda p, c: seen.update(p=p, c=c) or 'Noted.')
    remember_text_options('ch', 'u1', _actions())
    matched, ack = handle_text_reply('ch', 'u1', '1', agent_id='a1', channel_type='whatsapp')
    assert (matched, ack) == (True, 'Noted.')
    assert seen['p'] == 'd1:up'
    assert seen['c'] == {'channel_type': 'whatsapp', 'channel_id': 'ch', 'agent_id': 'a1',
                         'external_user_id': 'u1', 'actor_id': 'u1', 'source': 'text'}


def test_handle_text_reply_unmatched():
    assert handle_text_reply('ch', 'u1', '1') == (False, None)


# ── channel integration ──────────────────────────────────────────────

def _dummy_channel():
    from backend.channels.base import BaseChannel

    class DummyChannel(BaseChannel):
        sent = []

        def start(self):
            pass

        def stop(self):
            pass

        def _do_send(self, external_user_id, text, session_id=None):
            self.sent.append((external_user_id, text))

        @staticmethod
        def get_channel_type():
            return 'dummy'

    with patch('models.db.db.get_agent', return_value=None):
        return DummyChannel('ch', 'a1', {})


def test_base_channel_falls_back_to_numbered_text():
    channel = _dummy_channel()
    channel.send_message_with_actions('u1', 'Hello', [
        {'label': 'Useful', 'namespace': 'demo', 'payload': 'd1:up'}])
    assert channel.sent == [('u1', 'Hello\n\nReply with a number:\n1. Useful')]
    assert take_text_option('ch', 'u1', '1')['payload'] == 'd1:up'


def test_base_channel_without_actions_sends_plain_text():
    channel = _dummy_channel()
    channel.send_message_with_actions('u1', 'Hello', [])
    assert channel.sent == [('u1', 'Hello')]


def test_telegram_keyboard_rows_of_three():
    pytest.importorskip('telegram')
    from backend.channels.telegram import _actions_keyboard
    actions = normalize_actions(
        [{'label': f'B{i}', 'namespace': 'demo', 'payload': str(i)} for i in range(5)])
    keyboard = _actions_keyboard(actions)
    rows = keyboard.inline_keyboard
    assert [len(r) for r in rows] == [3, 2]
    assert rows[1][1].callback_data == 'act:demo:4'


def test_handle_message_routes_numbered_reply_before_agent_turn():
    from backend.agent_runtime import agent_runtime
    register_channel_action_handler('demo', lambda p, c: f'Got {p}')
    remember_text_options('ch', 'u1', _actions())
    with patch('backend.agent_runtime.runtime.db.get_agent') as get_agent:
        result = agent_runtime.handle_message('a1', 'u1', '2', channel_id='ch')
    assert result == {'response': 'Got d1:nr', 'tool_trace': []}
    get_agent.assert_not_called()


def test_sdk_send_message_rejects_invalid_actions():
    from backend.plugin_sdk import PluginSDK
    sdk = PluginSDK('demo-plugin', {}, {})
    result = sdk.send_message('a1', 'u1', 'telegram', 'Hi',
                              actions=[{'label': 'x', 'namespace': 'approve', 'payload': '1'}])
    assert result['success'] is False
    assert 'reserved' in result['error']
