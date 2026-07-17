"""Tests for the read_attachment tool backend."""
import os
import json
import pytest

from backend.tools import read_attachment as ra
from models.db import db


def _make_agent(agent_id, is_super=False):
    db.create_agent({
        'id': agent_id, 'name': agent_id, 'system_prompt': '',
        'is_super': is_super,
    })
    return agent_id


def _store(agent_id, body: bytes, name='note.txt', mime='text/plain', tmp_path=None):
    target_dir = os.path.join(str(tmp_path), 'data', 'attachments', agent_id, 's1')
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"file_{name}")
    with open(path, 'wb') as f:
        f.write(body)
    aid = db.save_attachment(
        agent_id=agent_id, session_id='s1',
        filename=os.path.basename(path), file_path=path,
        original_filename=name, mime_type=mime,
        file_type='document', size_bytes=len(body),
    )
    return aid, path


def test_read_text_attachment_by_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_t')
    aid, _ = _store('agent_t', b"line1\nline2\nline3\n", tmp_path=tmp_path)
    result = ra.execute({'id': 'agent_t'}, {'attachment_id': aid})
    assert 'result' in result
    content = result['result']
    assert '1: line1' in content
    assert '3: line3' in content


def test_cross_agent_denial(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('alpha')
    _make_agent('beta')
    aid, _ = _store('alpha', b"secret", tmp_path=tmp_path)
    result = ra.execute({'id': 'beta'}, {'attachment_id': aid})
    assert 'error' in result
    assert 'different agent' in result['error']


def test_super_agent_can_access_cross_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('gamma')
    _make_agent('super_x', is_super=True)
    aid, _ = _store('gamma', b"shared", tmp_path=tmp_path)
    result = ra.execute({'id': 'super_x', 'is_super': True}, {'attachment_id': aid})
    assert 'result' in result


def test_path_outside_agent_denied(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_p')
    # Path resolves outside the agent's attachment directory.
    other_file = tmp_path / "outside.txt"
    other_file.write_text("nope")
    result = ra.execute({'id': 'agent_p'}, {'path': str(other_file)})
    assert 'error' in result
    assert 'Access denied' in result['error']


def test_path_within_agent_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_q')
    _, path = _store('agent_q', b"hello\nworld\n", tmp_path=tmp_path)
    result = ra.execute({'id': 'agent_q'}, {'path': path})
    assert 'result' in result
    assert '1: hello' in result['result']


def test_missing_attachment_id_returns_actionable_not_found_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_m')
    result = ra.execute({'id': 'agent_m'}, {'attachment_id': 75433064})
    assert 'error' in result
    assert 'Attachment ID 75433064 was not found' in result['error']
    assert "numeric 'Attachment ID' shown in the attachment metadata" in result['error']
    assert 'session ID' in result['error']
    assert 'expired' not in result['error'].lower()


def test_existing_record_with_missing_file_reports_distinct_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_missing_file')
    aid, path = _store('agent_missing_file', b'gone', tmp_path=tmp_path)
    os.remove(path)

    result = ra.execute({'id': 'agent_missing_file'}, {'attachment_id': aid})

    assert 'missing on disk' in result['error']
    assert 'retention cleanup' in result['error']


def test_invalid_attachment_id_type(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_inv')
    result = ra.execute({'id': 'agent_inv'}, {'attachment_id': 'abc'})
    assert 'error' in result
    assert 'Invalid attachment_id' in result['error']


def test_no_args_returns_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_z')
    result = ra.execute({'id': 'agent_z'}, {})
    assert 'error' in result
    assert "attachment_id" in result['error'] or 'path' in result['error']


def test_binary_attachment_returns_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_bin')
    aid, _ = _store('agent_bin', b'\x00\x01\x02BIN', name='photo.jpg',
                    mime='image/jpeg', tmp_path=tmp_path)
    result = ra.execute({'id': 'agent_bin'}, {'attachment_id': aid})
    assert 'result' in result
    out = result['result']
    assert 'metadata' in out.lower()
    # Verify it includes JSON with the expected fields.
    json_part = out.split('\n\n', 1)[1]
    parsed = json.loads(json_part)
    assert parsed['mime_type'] == 'image/jpeg'
    assert parsed['filename'] == 'photo.jpg'


def test_pdf_no_pypdf_returns_unavailable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_pdf')
    aid, _ = _store('agent_pdf', b'%PDF-1.4 fake', name='doc.pdf',
                    mime='application/pdf', tmp_path=tmp_path)
    # Force ImportError for pypdf
    import sys
    monkeypatch.setitem(sys.modules, 'pypdf', None)
    result = ra.execute({'id': 'agent_pdf'}, {'attachment_id': aid})
    assert 'result' in result
    out = result['result']
    assert 'PDF text extraction unavailable' in out or 'install' in out


def test_mock_shape_matches_execute(tmp_path, monkeypatch):
    """Contract test: the mock_response in tools/read_attachment.json must have
    the same top-level key set as a real `execute()` call (either {'result'}
    or {'error'}). This prevents evaluator paths that swap the real backend
    for the mock from silently producing a different shape than tests assert.
    """
    # Find the tool definition relative to the repository root, not tmp_path.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tool_path = os.path.join(repo_root, 'tools', 'read_attachment.json')
    with open(tool_path, 'r', encoding='utf-8') as f:
        spec = json.load(f)

    mock = spec['mock_response']
    # `mock_response_type` should be `json`, mirroring sibling tools.
    assert spec.get('mock_response_type') == 'json'
    # If stored as a JSON-encoded string, parse it; otherwise expect a dict.
    if isinstance(mock, str):
        mock = json.loads(mock)
    assert isinstance(mock, dict)
    mock_keys = set(mock.keys())
    assert mock_keys in ({'result'}, {'error'}), (
        f"Mock response shape {mock_keys!r} does not match execute() "
        "contract ({'result'} or {'error'})."
    )

    # Now confirm a real execute() call exposes the same key set.
    monkeypatch.chdir(tmp_path)
    _make_agent('agent_mock_shape')
    aid, _ = _store('agent_mock_shape', b'hello\n', tmp_path=tmp_path)
    real = ra.execute({'id': 'agent_mock_shape'}, {'attachment_id': aid})
    real_keys = set(real.keys())
    assert real_keys in ({'result'}, {'error'})
    # Same contract: both result-shaped (or both error-shaped) in the happy path.
    assert real_keys == mock_keys
