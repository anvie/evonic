import base64
import io
import json

from PIL import Image

from backend.agent_runtime import context, native_images, tool_images


class _FakeDb:
    def __init__(self):
        self.records = []

    def save_attachment(self, **row):
        row['id'] = len(self.records) + 1
        self.records.append(row)
        return row['id']

    def list_session_attachments(self, session_id, agent_id):
        return [r for r in self.records if r['session_id'] == session_id and r['agent_id'] == agent_id]


def _png_b64(size=(32, 24)):
    buf = io.BytesIO()
    Image.new('RGB', size, (10, 120, 80)).save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def test_images_become_attachments_and_references(tmp_path, monkeypatch):
    monkeypatch.setattr(context, '_BASE_DIR', str(tmp_path))
    db = _FakeDb()
    result = {'ok': True, 'images': [
        {'mime': 'image/png', 'data': _png_b64(), 'label': 'preview'},
        {'mime': 'image/svg+xml', 'data': 'PHN2Zy8+'},
        {'mime': 'image/png', 'data': 'not base64!!'},
    ]}
    out = tool_images.store(result, 'agent1', 'sess1', 'scene_preview', db)
    assert out['ok'] is True
    assert out['images'][0][tool_images.REF_KEY] == 1
    assert out['images'][0]['label'] == 'preview'
    assert 'not supported' in out['images'][1]['skipped']
    assert 'base64' in out['images'][2]['skipped']
    assert 'describe_image' in out['images_note']
    assert len(db.records) == 1 and db.records[0]['channel_type'] == 'tool'
    assert (tmp_path / 'data' / 'attachments' / 'agent1' / 'sess1').is_dir()
    # no base64 is left in the result that goes to the model
    assert _png_b64() not in json.dumps(out)


def test_results_without_images_are_untouched():
    result = {'ok': True}
    assert tool_images.store(result, 'a', 's', 'f', _FakeDb()) is result


def test_vision_models_get_tool_images_since_the_last_user_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(context, '_BASE_DIR', str(tmp_path))
    db = _FakeDb()
    stored = tool_images.store({'images': [{'mime': 'image/png', 'data': _png_b64(), 'label': 'preview'}]}, 'agent1', 'sess1', 'scene_preview', db)
    # an earlier turn's image is not attached again
    old = tool_images.store({'images': [{'mime': 'image/png', 'data': _png_b64(), 'label': 'old'}]}, 'agent1', 'sess1', 'scene_preview', db)
    messages = [
        {'role': 'user', 'content': 'first'},
        {'role': 'tool', 'tool_call_id': 't0', 'content': json.dumps(old)},
        {'role': 'user', 'content': 'make a render'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 't1'}]},
        {'role': 'tool', 'tool_call_id': 't1', 'content': json.dumps(stored)},
    ]
    out = native_images.attach_tool_images(messages, 'sess1', 'agent1', db)
    assert len(out) == len(messages) + 1, 'one user message with the image is appended'
    added = out[-1]
    assert added['role'] == 'user'
    assert 'Attachment ID 1 (preview)' in added['content'][0]['text']
    assert [p['type'] for p in added['content'][1:]] == ['image_url']
    assert added['content'][1]['image_url']['url'].startswith('data:image/'), 're-encoded like other native images'
    assert len(messages) == 5, 'the canonical messages are not changed'


def test_only_this_sessions_tool_attachments_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(context, '_BASE_DIR', str(tmp_path))
    db = _FakeDb()
    other = tool_images.store({'images': [{'mime': 'image/png', 'data': _png_b64()}]}, 'agent1', 'other-session', 'f', db)
    user_upload = db.save_attachment(agent_id='agent1', session_id='sess1', filename='u.png', file_path=str(tmp_path / 'u.png'),
                                     channel_type='telegram', mime_type='image/png')
    messages = [
        {'role': 'user', 'content': 'go'},
        {'role': 'tool', 'tool_call_id': 't', 'content': json.dumps(other)},
        {'role': 'tool', 'tool_call_id': 'u', 'content': json.dumps({tool_images.REF_KEY: user_upload})},
    ]
    assert native_images.attach_tool_images(messages, 'sess1', 'agent1', db) is messages


def test_nothing_to_attach_returns_the_same_list():
    messages = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}]
    assert native_images.attach_tool_images(messages, 's', 'a', _FakeDb()) is messages
