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
