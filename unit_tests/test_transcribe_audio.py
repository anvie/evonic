"""Tests for the transcribe_audio tool backend and the Telegram voice hint."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from backend.tools import transcribe_audio as ta
from models.db import db


def _make_agent(agent_id, audio_enabled=1, attachments_enabled=1):
    db.create_agent({
        'id': agent_id, 'name': agent_id, 'system_prompt': '',
    })
    model_id = f'model_for_{agent_id}'
    db.create_model({
        'id': model_id, 'name': model_id, 'type': 'openai',
        'provider': 'openai', 'model_name': model_id,
    })
    with db._connect() as conn:
        conn.execute(
            "UPDATE agents SET audio_enabled=?, attachments_enabled=?, model_id=? "
            "WHERE id=?",
            (audio_enabled, attachments_enabled, model_id, agent_id),
        )
    return agent_id


def _write_ogg(tmp_path, name='voice.ogg', body=b'OGG-DATA'):
    path = os.path.join(str(tmp_path), name)
    with open(path, 'wb') as f:
        f.write(body)
    return path


def _fake_llm_result(content='transcript text'):
    return {
        'success': True,
        'response': {'choices': [{'message': {'content': content}}]},
    }


# ---------------------------------------------------------------------------
# Gating and validation
# ---------------------------------------------------------------------------

def test_gate_audio_disabled(tmp_path):
    path = _write_ogg(tmp_path)
    out = ta.execute({'id': 'x', 'audio_enabled': 0}, {'path': path})
    assert 'audio_enabled=0' in out


def test_missing_path():
    out = ta.execute({'id': 'x', 'audio_enabled': 1}, {})
    assert "'path' parameter is required" in out


def test_file_not_found():
    out = ta.execute({'id': 'x', 'audio_enabled': 1}, {'path': '/nope/missing.ogg'})
    assert 'File not found' in out


def test_unsupported_extension(tmp_path):
    path = _write_ogg(tmp_path, name='clip.xyz')
    out = ta.execute({'id': 'x', 'audio_enabled': 1}, {'path': path})
    assert 'Unsupported audio type' in out


def test_non_string_path_arg():
    out = ta.execute({'id': 'x', 'audio_enabled': 1}, {'path': {'oops': 1}})
    assert "'path' parameter is required" in out


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def test_resolve_models_prefers_system_setting(monkeypatch):
    agent = _make_agent('ta_resolver')
    sys_model = {'id': 'sys_audio_model', 'name': 'SysAudio', 'enabled': 1}
    monkeypatch.setattr(db, 'get_setting',
                        lambda key, default=None: 'sys_audio_model' if key == 'audio_model_id' else default)
    monkeypatch.setattr(db, 'get_model_by_id',
                        lambda mid: sys_model if mid == 'sys_audio_model' else None)
    models, err = ta._resolve_audio_models({'id': agent})
    assert err is None
    assert models[0]['id'] == 'sys_audio_model'
    # Agent's own model follows as fallback
    assert any(m.get('id') == f'model_for_{agent}' for m in models[1:])


def test_resolve_models_falls_back_to_agent_model(monkeypatch):
    agent = _make_agent('ta_resolver2')
    monkeypatch.setattr(db, 'get_setting', lambda key, default=None: default)
    models, err = ta._resolve_audio_models({'id': agent})
    assert err is None
    assert models[0]['id'] == f'model_for_{agent}'


def test_resolve_models_none_available(monkeypatch):
    monkeypatch.setattr(db, 'get_setting', lambda key, default=None: default)
    monkeypatch.setattr(db, 'get_agent_model', lambda aid: None)
    models, err = ta._resolve_audio_models({'id': 'ghost'})
    assert models == []
    assert 'No audio-capable model' in err


# ---------------------------------------------------------------------------
# Success and fallback paths (LLM + conversion mocked)
# ---------------------------------------------------------------------------

def _patch_conversion(monkeypatch):
    import backend.audio_utils as au
    monkeypatch.setattr(au, 'convert_to_wav16k', lambda b: b'WAV16K')


def test_success_returns_transcript(tmp_path, monkeypatch):
    _patch_conversion(monkeypatch)
    path = _write_ogg(tmp_path)
    monkeypatch.setattr(ta, '_resolve_audio_models',
                        lambda agent: ([{'id': 'm1', 'name': 'M1'}], None))

    captured = {}

    class _FakeClient:
        def __init__(self, model_config=None):
            captured['model'] = model_config

        def chat_completion(self, messages=None, enable_thinking=None):
            captured['messages'] = messages
            captured['enable_thinking'] = enable_thinking
            return _fake_llm_result('  halo dunia  ')

    monkeypatch.setattr(ta, 'LLMClient', _FakeClient)
    out = ta.execute({'id': 'x', 'audio_enabled': 1}, {'path': path})
    assert out == 'halo dunia'
    assert captured['enable_thinking'] is False
    # The user message must carry a 16kHz WAV input_audio part.
    user_parts = captured['messages'][1]['content']
    audio_parts = [p for p in user_parts if p['type'] == 'input_audio']
    assert len(audio_parts) == 1
    assert audio_parts[0]['input_audio']['format'] == 'wav'


def test_query_is_included(tmp_path, monkeypatch):
    _patch_conversion(monkeypatch)
    path = _write_ogg(tmp_path)
    monkeypatch.setattr(ta, '_resolve_audio_models',
                        lambda agent: ([{'id': 'm1', 'name': 'M1'}], None))
    captured = {}

    class _FakeClient:
        def __init__(self, model_config=None):
            pass

        def chat_completion(self, messages=None, enable_thinking=None):
            captured['messages'] = messages
            return _fake_llm_result('English')

    monkeypatch.setattr(ta, 'LLMClient', _FakeClient)
    out = ta.execute({'id': 'x', 'audio_enabled': 1},
                     {'path': path, 'query': 'What language is spoken?'})
    assert out == 'English'
    text_part = captured['messages'][1]['content'][0]
    assert 'What language is spoken?' in text_part['text']


def test_connection_error_falls_back_to_next_model(tmp_path, monkeypatch):
    _patch_conversion(monkeypatch)
    path = _write_ogg(tmp_path)
    monkeypatch.setattr(ta, '_resolve_audio_models',
                        lambda agent: ([{'id': 'm1', 'name': 'M1'},
                                        {'id': 'm2', 'name': 'M2'}], None))
    calls = []

    class _FakeClient:
        def __init__(self, model_config=None):
            self._model = model_config['id']

        def chat_completion(self, messages=None, enable_thinking=None):
            calls.append(self._model)
            if self._model == 'm1':
                return {'success': False, 'error_type': 'connection_error',
                        'error_detail': 'refused'}
            return _fake_llm_result('ok from m2')

    monkeypatch.setattr(ta, 'LLMClient', _FakeClient)
    out = ta.execute({'id': 'x', 'audio_enabled': 1}, {'path': path})
    assert out == 'ok from m2'
    assert calls == ['m1', 'm2']


def test_non_connection_error_fails_immediately(tmp_path, monkeypatch):
    _patch_conversion(monkeypatch)
    path = _write_ogg(tmp_path)
    monkeypatch.setattr(ta, '_resolve_audio_models',
                        lambda agent: ([{'id': 'm1', 'name': 'M1'},
                                        {'id': 'm2', 'name': 'M2'}], None))
    calls = []

    class _FakeClient:
        def __init__(self, model_config=None):
            self._model = model_config['id']

        def chat_completion(self, messages=None, enable_thinking=None):
            calls.append(self._model)
            return {'success': False, 'error_type': 'api_error',
                    'error_detail': 'bad request'}

    monkeypatch.setattr(ta, 'LLMClient', _FakeClient)
    out = ta.execute({'id': 'x', 'audio_enabled': 1}, {'path': path})
    assert 'api_error' in out
    assert calls == ['m1']  # no fallback on non-connection errors


def test_conversion_failure_is_reported(tmp_path, monkeypatch):
    import backend.audio_utils as au

    def _boom(b):
        raise RuntimeError('ffmpeg is not installed')

    monkeypatch.setattr(au, 'convert_to_wav16k', _boom)
    path = _write_ogg(tmp_path)
    out = ta.execute({'id': 'x', 'audio_enabled': 1}, {'path': path})
    assert 'Audio conversion failed' in out


# ---------------------------------------------------------------------------
# Telegram: voice info line carries the transcribe_audio hint
# ---------------------------------------------------------------------------

def _msg_with_voice(file_size=2048, file_id='tg_voice_hint'):
    voice = SimpleNamespace(file_id=file_id, file_size=file_size,
                            mime_type=None, file_name=None)
    return SimpleNamespace(
        document=None, audio=None, voice=voice, video=None,
        video_note=None, animation=None, sticker=None, photo=None,
        reply_text=AsyncMock(),
    )


class _FakeTgFile:
    async def download_to_drive(self, path):
        with open(path, 'wb') as f:
            f.write(b'OGG')


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_telegram_voice_info_line_includes_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from backend.channels.telegram import _ingest_non_photo_attachment
    agent_id = _make_agent('tg_voice_hint_on', audio_enabled=1)
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=_FakeTgFile())
    ctx = SimpleNamespace(bot=bot)
    info_line, rejected = _run(_ingest_non_photo_attachment(
        _msg_with_voice(), ctx, agent_id, 's1', 'u1', 'ch1', db))
    assert rejected is False
    assert info_line.startswith('[Attached: voice.ogg')
    assert 'transcribe_audio' in info_line


def test_telegram_voice_info_line_no_hint_when_audio_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from backend.channels.telegram import _ingest_non_photo_attachment
    agent_id = _make_agent('tg_voice_hint_off', audio_enabled=0)
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=_FakeTgFile())
    ctx = SimpleNamespace(bot=bot)
    info_line, rejected = _run(_ingest_non_photo_attachment(
        _msg_with_voice(file_id='tg_voice_hint2'), ctx, agent_id, 's1', 'u1', 'ch1', db))
    assert rejected is False
    assert info_line.startswith('[Attached: voice.ogg')
    assert 'transcribe_audio' not in info_line


def _msg_with_pdf(file_id='tg_pdf'):
    document = SimpleNamespace(
        file_id=file_id,
        file_name='report.pdf',
        mime_type='application/pdf',
        file_size=3,
    )
    return SimpleNamespace(
        document=document, audio=None, voice=None, video=None,
        video_note=None, animation=None, sticker=None, photo=None,
        reply_text=AsyncMock(),
    )


def test_telegram_pdf_info_line_uses_native_analysis_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from backend.channels.telegram import _ingest_non_photo_attachment
    agent_id = _make_agent('tg_pdf_hint')
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=_FakeTgFile())
    info_line, rejected = _run(_ingest_non_photo_attachment(
        _msg_with_pdf(), SimpleNamespace(bot=bot), agent_id,
        's1', 'u1', 'ch1', db,
    ))

    assert rejected is False
    assert info_line.startswith('[Attached: report.pdf')
    assert 'analyze_document' in info_line


def test_telegram_pdf_hint_respects_agent_toggle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from backend.channels.telegram import _ingest_non_photo_attachment
    agent_id = _make_agent('tg_pdf_hint_off')
    db.update_agent(agent_id, {'document_enabled': 0})
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=_FakeTgFile())
    info_line, rejected = _run(_ingest_non_photo_attachment(
        _msg_with_pdf('tg_pdf_off'), SimpleNamespace(bot=bot), agent_id,
        's1', 'u1', 'ch1', db,
    ))

    assert rejected is False
    assert 'analyze_document' not in info_line


def test_telegram_text_document_uses_exact_read_hint_even_when_native_is_off(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    from backend.channels.telegram import _ingest_non_photo_attachment
    agent_id = _make_agent('tg_text_hint')
    db.update_agent(agent_id, {'document_enabled': 0})
    message = _msg_with_pdf('tg_text')
    message.document.file_name = 'notes.txt'
    message.document.mime_type = 'text/plain'
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=_FakeTgFile())

    info_line, rejected = _run(_ingest_non_photo_attachment(
        message, SimpleNamespace(bot=bot), agent_id, 's1', 'u1', 'ch1', db,
    ))

    assert rejected is False
    assert 'read_attachment' in info_line
    assert 'attachment_id=' in info_line
    assert 'analyze_document' not in info_line


def test_telegram_rejects_zip_before_download(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from backend.channels.telegram import _ingest_non_photo_attachment
    agent_id = _make_agent('tg_zip_rejected')
    message = _msg_with_pdf('tg_zip')
    message.document.file_name = 'archive.zip'
    message.document.mime_type = 'application/zip'
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=_FakeTgFile())

    info_line, rejected = _run(_ingest_non_photo_attachment(
        message, SimpleNamespace(bot=bot), agent_id, 's1', 'u1', 'ch1', db,
    ))

    assert (info_line, rejected) == (None, True)
    message.reply_text.assert_awaited_once()
    bot.get_file.assert_not_awaited()
