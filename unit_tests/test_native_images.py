import base64

from PIL import Image

from backend.agent_runtime import native_images


class _FakeDb:
    def __init__(self, records):
        self.records = records

    def list_session_attachments(self, session_id, agent_id):
        return self.records


def _png(tmp_path, name='a.png', size=(40, 30)):
    path = tmp_path / name
    Image.new('RGB', size, (10, 120, 80)).save(path)
    return str(path)


def _msg(attachment_id):
    return {'role': 'user', 'content': (
        'recreate this\n\n[Attachment: a.png (image/png, 1 KB)]\n'
        f'Attachment ID: {attachment_id}\nFile path: /etc/passwd')}


def test_native_mode_requires_vision_only():
    assert native_images.native_mode_enabled({'vision_supported': True})
    assert not native_images.native_mode_enabled({'vision_supported': False})
    assert not native_images.native_mode_enabled({})
    assert not native_images.native_mode_enabled(None)


def test_describe_hint_is_stripped_from_string_content():
    hint = native_images.DESCRIBE_HINT
    messages = [{'role': 'user', 'content': f'what is this?\n\n[Attachment: a.png]\n{hint}'}]
    out = native_images.strip_describe_image_hints(messages)
    assert out[0]['content'] == 'what is this?\n\n[Attachment: a.png]'


def test_describe_hint_is_stripped_from_text_parts():
    hint = native_images.DESCRIBE_HINT
    messages = [{'role': 'user', 'content': [
        {'type': 'text', 'text': f'what is this?\n{hint}'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA'}},
    ]}]
    out = native_images.strip_describe_image_hints(messages)
    assert out[0]['content'][0]['text'] == 'what is this?'
    assert out[0]['content'][1]['type'] == 'image_url'


def test_stripping_never_mutates_canonical_messages():
    hint = native_images.DESCRIBE_HINT
    original = {'role': 'user', 'content': f'look\n{hint}'}
    parts = {'role': 'user', 'content': [{'type': 'text', 'text': f'look\n{hint}'}]}
    native_images.strip_describe_image_hints([original, parts])
    assert original['content'] == f'look\n{hint}'
    assert parts['content'][0]['text'] == f'look\n{hint}'


def test_messages_without_hint_are_returned_unchanged():
    plain = {'role': 'assistant', 'content': 'no hint here'}
    tool = {'role': 'tool', 'content': 'Attachment ID: 3'}
    out = native_images.strip_describe_image_hints([plain, tool])
    assert out[0] is plain and out[1] is tool


def test_recent_image_attachment_is_attached_without_mutating_history(tmp_path):
    records = [{'id': 7, 'mime_type': 'image/png', 'file_path': _png(tmp_path)}]
    history = [{'role': 'system', 'content': 's'}, _msg(7)]
    out = native_images.attach_native_images(history, 'sess', 'agent', _FakeDb(records))
    parts = out[1]['content']
    assert parts[0]['type'] == 'text' and 'Attachment ID: 7' in parts[0]['text']
    assert parts[1]['type'] == 'image_url'
    url = parts[1]['image_url']['url']
    assert url.startswith('data:image/')
    assert base64.b64decode(url.split(',', 1)[1])
    assert isinstance(history[1]['content'], str)  # canonical history untouched


def test_only_database_owned_image_ids_are_read(tmp_path):
    secret = tmp_path / 'secret.txt'
    secret.write_text('x')
    records = [{'id': 8, 'mime_type': 'text/plain', 'file_path': str(secret)}]
    out = native_images.attach_native_images([_msg(8), _msg(99)], 's', 'a', _FakeDb(records))
    assert all(isinstance(m['content'], str) for m in out)


def test_large_images_are_downscaled_and_old_turns_ignored(tmp_path):
    records = [{'id': 1, 'mime_type': 'image/png', 'file_path': _png(tmp_path, size=(3000, 2000))}]
    old = [_msg(1)] + [{'role': 'user', 'content': 'next'} for _ in range(3)]
    assert isinstance(native_images.attach_native_images(old, 's', 'a', _FakeDb(records))[0]['content'], str)
    out = native_images.attach_native_images([_msg(1)], 's', 'a', _FakeDb(records))
    raw = base64.b64decode(out[0]['content'][1]['image_url']['url'].split(',', 1)[1])
    import io
    assert max(Image.open(io.BytesIO(raw)).size) <= native_images.MAX_EDGE


# --- Per-agent vision toggle + note/hint consistency (task #910) -------------

from backend.agent_runtime import context as _ctx
from backend.agent_runtime.runtime import _append_attachment_context

_IMAGE_INFO = {
    'attachment_id': 42,
    'filename': 'photo.png',
    'mime_type': 'image/png',
    'size_bytes': 2048,
    'file_path': '/tmp/photo.png',
}
_DESCRIBE_HINT = 'Use the `describe_image` tool to view and analyze this image.'


def test_native_mode_honours_the_agent_vision_toggle():
    model = {'vision_supported': True}
    assert native_images.native_mode_enabled(model, vision_enabled=True)
    assert not native_images.native_mode_enabled(model, vision_enabled=False)
    assert not native_images.native_mode_enabled(model, vision_enabled=0)
    # Legacy callers/agent records without the flag keep the old behaviour.
    assert native_images.native_mode_enabled(model)
    assert native_images.native_mode_enabled(model, vision_enabled=None)


def test_native_mode_never_applies_without_model_vision_support():
    assert not native_images.native_mode_enabled({'vision_supported': False},
                                                 vision_enabled=True)
    assert not native_images.native_mode_enabled({}, vision_enabled=True)
    assert not native_images.native_mode_enabled(None, vision_enabled=True)


def test_native_turn_decision_combines_agent_flag_and_model_capability():
    vision_model = {'vision_supported': True}
    assert native_images.native_images_for_turn({'vision_enabled': 1}, vision_model)
    assert not native_images.native_images_for_turn({'vision_enabled': 0}, vision_model)
    # Missing vision_enabled (legacy agent record) means enabled.
    assert native_images.native_images_for_turn({}, vision_model)
    assert not native_images.native_images_for_turn({'vision_enabled': 1}, {})
    assert not native_images.native_images_for_turn({'vision_enabled': 0}, {})


def test_no_describe_hint_when_the_image_is_attached_natively():
    note = _ctx.build_attachment_note(_IMAGE_INFO, native_image=True)
    assert _DESCRIBE_HINT not in note
    assert 'Attachment ID: 42' in note
    assert 'File path:' in note


def test_describe_hint_is_present_for_the_describe_path():
    note = _ctx.build_attachment_note(_IMAGE_INFO)
    assert _DESCRIBE_HINT in note


def test_describe_hint_is_present_for_a_model_without_vision_support():
    native = native_images.native_mode_enabled({'vision_supported': False},
                                               vision_enabled=True)
    assert not native
    note = _ctx.build_attachment_note(_IMAGE_INFO, native_image=native)
    assert _DESCRIBE_HINT in note


def test_describe_hint_is_dropped_when_the_tool_is_unavailable_even_if_not_native():
    note = _ctx.build_attachment_note(_IMAGE_INFO, has_describe_image=False)
    assert _DESCRIBE_HINT not in note
    assert _ctx.build_attachment_note(_IMAGE_INFO, has_describe_image=False,
                                      native_image=True) == note


def test_native_mode_keeps_ids_and_paths_for_every_attachment():
    second = {**_IMAGE_INFO, 'attachment_id': 43, 'filename': 'second.png'}
    notes = _ctx.build_attachment_notes([_IMAGE_INFO, second], native_image=True)
    assert _DESCRIBE_HINT not in notes
    assert notes.count('Attachment ID:') == 2
    assert '[Attachment #1:' in notes and '[Attachment #2:' in notes


def test_audio_hint_is_unaffected_by_native_image_mode():
    audio = {'attachment_id': 44, 'filename': 'voice.ogg', 'mime_type': 'audio/ogg',
             'size_bytes': 512, 'file_path': '/tmp/voice.ogg'}
    note = _ctx.build_attachment_note(audio, audio_enabled=True, native_image=True)
    assert 'transcribe_audio' in note


def test_runtime_attachment_context_matches_the_context_note():
    """Regression guard: both note builders must agree on the hint."""
    for native_image in (False, True):
        note = _ctx.build_attachment_note(_IMAGE_INFO, native_image=native_image)
        content = _append_attachment_context(
            'look', [_IMAGE_INFO], None, {}, True, native_image=native_image)
        assert (_DESCRIBE_HINT in note) == (_DESCRIBE_HINT in content), native_image
        assert content.count(_DESCRIBE_HINT) == note.count(_DESCRIBE_HINT)
        assert 'File path:' in content and 'Attachment ID: 42' not in content


def test_runtime_attachment_context_omits_hint_when_native():
    content = _append_attachment_context(
        'look', None, _IMAGE_INFO, {}, True, native_image=True)
    assert _DESCRIBE_HINT not in content
    assert '[Attachment: photo.png' in content
