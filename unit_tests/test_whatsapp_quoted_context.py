"""Regression tests for agent-readable WhatsApp quoted-message context."""

from backend.channels.whatsapp import _format_quoted_context, _wrap_group_message


def test_plain_text_quote_remains_compatible():
    assert _format_quoted_context('hello', quoted_is_bot=True) == (
        '[Replying to bot: "hello"]'
    )


def test_image_caption_is_rendered_with_media_metadata():
    result = _format_quoted_context(quoted_message={
        'type': 'image',
        'caption': 'Use the NU logo in this design',
        'mimetype': 'image/png',
    })
    assert '[Replying to — quoted image; MIME type: "image/png"]' in result
    assert 'Use the NU logo in this design' in result


def test_document_caption_includes_filename_and_mime_type():
    result = _format_quoted_context(quoted_message={
        'type': 'document',
        'caption': '<html><body>complete instruction</body></html>',
        'filename': 'brief.html',
        'mimetype': 'text/html',
    })
    assert 'quoted document' in result
    assert 'filename: "brief.html"' in result
    assert 'MIME type: "text/html"' in result
    assert '<html><body>complete instruction</body></html>' in result


def test_captionless_media_has_useful_fallback():
    result = _format_quoted_context(quoted_message={
        'type': 'video',
        'caption': None,
        'mimetype': 'video/mp4',
    })
    assert 'quoted video' in result
    assert 'MIME type: "video/mp4"' in result
    assert '(no caption)' in result


def test_long_caption_is_not_truncated():
    caption = '<html>' + ('x' * 500) + '</html>'
    result = _format_quoted_context(quoted_message={
        'type': 'document', 'caption': caption,
    })
    assert caption in result


def test_group_quote_preserves_sender_and_structured_media_context():
    result = _wrap_group_message(
        text='Can you read this?',
        group_name='Design Team',
        push_name='Budi',
        sender='628123456789',
        quoted_text=None,
        quoted_is_bot=False,
        quoted_sender_name='Andi',
        quoted_sender='628999888777',
        quoted_message={
            'type': 'document',
            'caption': 'Full document instruction',
            'filename': 'requirements.pdf',
            'mimetype': 'application/pdf',
        },
    )
    assert '[Replying to Andi — quoted document;' in result
    assert 'Full document instruction' in result
    assert result.endswith('Can you read this?')
