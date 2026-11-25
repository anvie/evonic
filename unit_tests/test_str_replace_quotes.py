"""str_replace must not reword curly quotes it was told to keep."""
import pytest

from backend.tools.str_replace import execute

CURLY = "Pilih \u201cA\u201d dan panel.\n"


def _agent(tmp_path):
    return {'id': 'eval_x', 'session_id': 's', 'workspace': str(tmp_path), 'workplace_id': None,
            'sandbox_enabled': 0, 'run_as_user': None}


def _edit(tmp_path, content, old, new):
    (tmp_path / 'a.md').write_text(content, encoding='utf-8')
    result = execute(_agent(tmp_path), {'file_path': 'a.md', 'old_str': old, 'new_str': new})
    assert result.get('result') == 'success', result
    return (tmp_path / 'a.md').read_text(encoding='utf-8')


def test_curly_quotes_copied_from_the_file_are_preserved(tmp_path):
    out = _edit(tmp_path, CURLY, "\u201cA\u201d dan panel", "\u201cA\u201d dan [[panel]]")
    assert out == "Pilih \u201cA\u201d dan [[panel]].\n"


def test_curly_quotes_the_caller_introduces_are_still_straightened(tmp_path):
    # Code written by a small model: it types curly quotes where the file has straight ones.
    out = _edit(tmp_path, 'x = "a"\n', 'x = "a"', 'x = \u201cb\u201d')
    assert out == 'x = "b"\n'
