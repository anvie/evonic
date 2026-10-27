"""Tests for ask_user questions shown as Telegram inline-keyboard buttons.

``TelegramChannel.start()`` runs against a fake bot (no polling thread), then the
ask_user event handlers and the callback-query handler are driven directly.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import CallbackQueryHandler

from backend.channels import telegram as tg
from backend.event_stream import event_stream
from plugins.ask_user.store import normalize_questions, question_registry


def _questions():
    questions, error = normalize_questions([
        {'question': 'Which database?', 'header': 'Database',
         'options': [{'label': 'PostgreSQL', 'description': 'Full server'},
                     {'label': 'SQLite', 'recommended': True}]},
        {'question': 'Which extras?', 'header': 'Extras', 'multiSelect': True,
         'options': ['Auth', 'Cache', 'Search']},
    ])
    assert error is None
    return questions


@pytest.fixture
def harness(monkeypatch):
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
        edit_message_text=AsyncMock(),
    )
    app = MagicMock()
    app.bot = bot
    builder = MagicMock()
    builder.token.return_value.build.return_value = app
    monkeypatch.setattr('telegram.ext.ApplicationBuilder', lambda: builder)
    monkeypatch.setattr(tg.threading, 'Thread', lambda **kwargs: MagicMock())
    monkeypatch.setattr(event_stream, 'on', lambda *args: None)
    enabled = {'value': True}
    monkeypatch.setattr(tg, '_ask_user_enabled', lambda: enabled['value'])

    channel = tg.TelegramChannel('tg-chan', 'agent-1', {'bot_token': 'x'})
    channel.start()
    callback = next(call.args[0].callback for call in app.add_handler.call_args_list
                    if isinstance(call.args[0], CallbackQueryHandler))

    def tap(data):
        query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                                edit_message_reply_markup=AsyncMock())
        asyncio.run(callback(SimpleNamespace(callback_query=query), None))
        return query

    def ask(questions):
        pending = question_registry.create('sess-1', 'agent-1', questions)
        channel._ask_user_required_handler({
            'question_id': pending.question_id, 'channel_id': 'tg-chan',
            'external_user_id': '12345', 'session_id': 'sess-1', 'questions': questions,
        })
        return pending

    yield SimpleNamespace(channel=channel, bot=bot, tap=tap, ask=ask, enabled=enabled)


def _button_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_question_is_sent_with_option_buttons(harness):
    pending = harness.ask(_questions())
    kwargs = harness.bot.send_message.call_args.kwargs
    assert kwargs['chat_id'] == 12345
    assert 'Which database?' in kwargs['text']
    assert '2. SQLite ⭐ recommended' in kwargs['text']
    assert '(1/2)' in kwargs['text']
    qid = pending.question_id
    assert _button_data(kwargs['reply_markup']) == [f'aq:{qid}:o:0:0', f'aq:{qid}:o:0:1', f'aq:{qid}:x']
    assert all(len(d.encode()) <= 64 for d in _button_data(kwargs['reply_markup']))
    question_registry.remove(qid, 'dismissed')


def test_single_then_multi_select_answers_the_question(harness):
    pending = harness.ask(_questions())
    qid = pending.question_id

    query = harness.tap(f'aq:{qid}:o:0:1')  # SQLite: moves on to the multi-select step
    text = query.edit_message_text.call_args.args[0]
    markup = query.edit_message_text.call_args.kwargs['reply_markup']
    assert 'Which extras?' in text
    assert f'aq:{qid}:d:1' in _button_data(markup)

    query = harness.tap(f'aq:{qid}:d:1')  # Done with nothing picked is refused
    query.answer.assert_awaited_with('Pick at least one option.')
    assert not pending.event.is_set()

    harness.tap(f'aq:{qid}:o:1:2')
    query = harness.tap(f'aq:{qid}:o:1:0')
    markup = query.edit_message_reply_markup.call_args.kwargs['reply_markup']
    assert [b.text for row in markup.inline_keyboard for b in row][:3] == [
        '✅ 1. Auth', '2. Cache', '✅ 3. Search']

    harness.tap(f'aq:{qid}:o:0:0')  # stale tap on the first step's buttons is ignored
    query = harness.tap(f'aq:{qid}:d:1')
    assert pending.event.is_set()
    assert [a['answer'] for a in pending.answers] == ['SQLite', 'Auth, Search']
    assert query.edit_message_text.call_args.args[0] == 'Answered:\nDatabase: SQLite\nExtras: Auth, Search'

    query = harness.tap(f'aq:{qid}:o:1:1')
    query.edit_message_text.assert_awaited_with('This question is no longer open.')
    question_registry.remove(qid, 'answered')


def test_cancel_dismisses_the_question(harness):
    pending = harness.ask(_questions())
    query = harness.tap(f'aq:{pending.question_id}:x')
    assert pending.dismissed and pending.event.is_set()
    query.edit_message_text.assert_awaited_with('Closed without answering.')
    question_registry.remove(pending.question_id, 'dismissed')


def test_resolution_elsewhere_updates_the_message(harness):
    pending = harness.ask(_questions())
    harness.channel._ask_user_resolved_handler({
        'question_id': pending.question_id, 'channel_id': 'tg-chan',
        'status': 'replied_in_chat', 'answers': [],
    })
    kwargs = harness.bot.edit_message_text.call_args.kwargs
    assert (kwargs['chat_id'], kwargs['message_id'], kwargs['text']) == (12345, 77, 'Answered in chat.')
    query = harness.tap(f'aq:{pending.question_id}:o:0:0')
    query.edit_message_text.assert_awaited_with('This question is no longer open.')
    question_registry.remove(pending.question_id, 'replied_in_chat')


def test_disabled_plugin_shows_nothing_and_ignores_taps(harness):
    pending = harness.ask(_questions())
    harness.enabled['value'] = False
    query = harness.tap(f'aq:{pending.question_id}:o:0:0')
    query.edit_message_text.assert_awaited_with('This question is no longer open.')
    assert not pending.event.is_set()

    harness.bot.send_message.reset_mock()
    other = harness.ask(_questions())
    harness.bot.send_message.assert_not_called()
    question_registry.remove(pending.question_id, 'disabled')
    question_registry.remove(other.question_id, 'disabled')


def test_other_channels_are_ignored(harness):
    pending = question_registry.create('sess-1', 'agent-1', _questions())
    harness.channel._ask_user_required_handler({
        'question_id': pending.question_id, 'channel_id': 'another-chan',
        'external_user_id': '12345', 'questions': _questions(),
    })
    harness.bot.send_message.assert_not_called()
    question_registry.remove(pending.question_id, 'dismissed')
