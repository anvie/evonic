"""Tests for WhatsApp group context wrapping.

``_wrap_group_message`` is a pure module-level helper, so it is testable
without a running bridge or channel instance.
"""

from backend.channels.whatsapp import _wrap_group_message


def _wrap(**kwargs):
    defaults = dict(
        text='hello there, can you help?',
        group_name='Family Chat',
        push_name='Budi',
        sender='628123456789',
        quoted_text=None,
        quoted_is_bot=False,
        quoted_sender_name='',
        quoted_sender='',
    )
    defaults.update(kwargs)
    return _wrap_group_message(**defaults)


def test_group_mention_header_only():
    result = _wrap()
    assert result == (
        '[WhatsApp group "Family Chat" — message from Budi (628123456789)]\n'
        'hello there, can you help?'
    )


def test_group_reply_to_bot():
    result = _wrap(quoted_text='I suggest option A', quoted_is_bot=True)
    lines = result.split('\n')
    assert lines[0] == '[WhatsApp group "Family Chat" — message from Budi (628123456789)]'
    assert lines[1] == '[Replying to your message: "I suggest option A"]'
    assert lines[2] == 'hello there, can you help?'


def test_group_reply_to_user_with_name():
    result = _wrap(quoted_text='see you at 5',
                   quoted_sender_name='Andi', quoted_sender='628999888777')
    assert '[Replying to Andi: "see you at 5"]' in result


def test_group_reply_to_user_without_name_falls_back_to_digits():
    result = _wrap(quoted_text='see you at 5', quoted_sender='628999888777')
    assert '[Replying to 628999888777: "see you at 5"]' in result


def test_group_reply_to_unknown_author():
    result = _wrap(quoted_text='see you at 5')
    assert '[Replying to unknown: "see you at 5"]' in result


def test_missing_group_name_falls_back_to_generic_label():
    result = _wrap(group_name='')
    assert result.startswith('[WhatsApp group — message from Budi (628123456789)]')


def test_missing_push_name_falls_back_to_digits():
    result = _wrap(push_name='')
    assert result.startswith('[WhatsApp group "Family Chat" — message from 628123456789]')


def test_quoted_text_is_not_truncated():
    long_quote = 'x' * 500
    result = _wrap(quoted_text=long_quote, quoted_sender_name='Andi')
    assert f'[Replying to Andi: "{long_quote}"]' in result
