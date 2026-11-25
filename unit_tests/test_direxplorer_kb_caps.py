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
    from backend.tools import read_file
    (tmp_path / "big.md").write_text("".join(f"line {i} " + "x" * 60 + "\n" for i in range(400)))
    normal = read_file.execute(_agent(tmp_path, False), {'file_path': 'big.md'})
    capped = read_file.execute(_agent(tmp_path, True), {'file_path': 'big.md'})
    assert len(normal) > len(capped) > 0
    assert len(capped) < 3600
    assert 'capped at 3000 chars for the KB organizer' in capped
    assert 'offset=' in capped
    assert 'capped' not in normal


def test_read_file_accepts_a_bare_slug_for_the_kb_organizer_only(tmp_path):
    from backend.tools import read_file
    (tmp_path / "sample-tabs-csv.md").write_text("hello\n")
    assert '1: hello' in read_file.execute(_agent(tmp_path, True), {'file_path': 'sample-tabs-csv'})
    assert read_file.execute(_agent(tmp_path, False), {'file_path': 'sample-tabs-csv'}).startswith('Error')


def test_read_file_stays_inside_the_organizer_workspace(tmp_path):
    from backend.tools import read_file
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "outside.md").write_text("secret\n")
    result = read_file.execute(_agent(vault, True), {'file_path': '../outside.md'})
    assert 'secret' not in str(result)


@pytest.mark.skipif(shutil.which('rg') is None, reason="ripgrep not installed")
def test_grep_trims_long_lines_for_kb_organizer_so_more_matches_fit(tmp_path):
    for i in range(30):
        (tmp_path / f"doc{i:02d}.md").write_text("padding " * 80 + f"NEEDLE{i} " + "tail " * 80 + "\n")
    capped = Grep.execute(_agent(tmp_path, True), {'pattern': 'NEEDLE'})
    normal = Grep.execute(_agent(tmp_path, False), {'pattern': 'NEEDLE'})
    lines = [m['content'] for f in capped['matches'] for m in f['matches']]
    assert all(len(line) <= 202 and 'NEEDLE' in line for line in lines)
    # Untrimmed, each ~1000-char line would leave room for only ~5 matches under the 6000 cap.
    assert capped['total_matches'] >= 15
    assert any(len(m['content']) > 500 for f in normal['matches'] for m in f['matches'])


def test_glob_truncation_tells_the_model_what_to_do(vault):
    capped = Glob.execute(_agent(vault, True), {'pattern': '**/*.md'})
    assert capped['truncated'] and 'narrower pattern' in capped['note']
    assert 'note' not in Glob.execute(_agent(vault, False), {'pattern': '**/*.md'})


def test_path_checks_use_a_single_backend_call(tmp_path):
    from unittest.mock import patch
    from skills.direxplorer.backend.tools import _utils
    (tmp_path / "a.md").write_text("x\n")
    real = _utils._run_python_json
    with patch.object(_utils, '_run_python_json', side_effect=real) as spy:
        _utils._prepare_path(_agent(tmp_path, False), 'a.md', want_dir=False)
    assert spy.call_count == 1
