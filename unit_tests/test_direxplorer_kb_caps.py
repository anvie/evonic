"""Grep/Glob output is capped much tighter for the KB organizer (small-context models)."""
import json
import shutil

import pytest

from skills.direxplorer.backend.tools import Glob, Grep


def _agent(ws, kb):
    return {'workspace': str(ws), 'sandbox_enabled': 0, 'workplace_id': None,
            'run_as_user': None, 'id': 'x', 'is_kb_organizer': kb}


@pytest.fixture
def vault(tmp_path):
    for i in range(250):
        (tmp_path / f"doc{i:03d}.md").write_text(f"line [[Name{i}]] hello\n")
    return tmp_path


def test_glob_caps_files_for_kb_organizer(vault):
    normal = Glob.execute(_agent(vault, False), {'pattern': '**/*.md'})
    capped = Glob.execute(_agent(vault, True), {'pattern': '**/*.md'})
    assert len(normal['files']) == 250 and not normal['truncated']
    assert len(capped['files']) <= 100 and capped['truncated'] and capped['count'] == 250


@pytest.mark.skipif(shutil.which('rg') is None, reason="ripgrep not installed")
def test_grep_caps_matches_for_kb_organizer(vault):
    normal = Grep.execute(_agent(vault, False), {'pattern': r'\[\['})
    capped = Grep.execute(_agent(vault, True), {'pattern': r'\[\['})
    assert normal['total_matches'] > 40
    assert capped['total_matches'] <= 40 and capped.get('truncated')
    assert len(json.dumps(capped)) <= 6000


def test_read_window_is_smaller_for_kb_organizer_and_says_so(tmp_path):
    from skills.direxplorer.backend.tools import Read
    (tmp_path / "big.md").write_text("".join(f"line {i} " + "x" * 60 + "\n" for i in range(400)))
    normal = Read.execute(_agent(tmp_path, False), {'file_path': 'big.md'})
    capped = Read.execute(_agent(tmp_path, True), {'file_path': 'big.md'})
    assert normal['shown_end'] > capped['shown_end'] > 0
    assert len(capped['content']) < 3600
    assert 'capped at 3000 chars for the KB organizer' in capped['content']
    assert f"offset={capped['shown_end'] + 1}" in capped['content']
    assert 'capped' not in normal['content']
