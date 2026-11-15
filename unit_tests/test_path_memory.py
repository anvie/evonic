"""Directories an agent's tools touch are recorded and recallable via mode='paths'."""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db
from backend.agent_runtime.path_memory import extract_dirs, on_tool_executed
from backend.tools.registry import _builtin_recall_factory


@pytest.fixture
def agent_id(tmp_path):
    aid = f"test_agent_{uuid.uuid4().hex[:8]}"
    original_path = db.db_path
    db.db_path = str(tmp_path / f"{aid}_db.sqlite")
    db._tls.conn = None
    db._init_tables()
    db.create_agent({'id': aid, 'name': 'Test Agent', 'system_prompt': 'test',
                     'workspace': '/work/proj'})
    yield aid
    db._tls.conn = None
    db.db_path = original_path


def _event(aid, tool, args, has_error=False):
    return {'payload': {'agent_id': aid, 'session_id': 's1', 'tool_name': tool,
                        'tool_args': args, 'has_error': has_error}}


def test_extract_file_tools():
    assert extract_dirs('read_file', {'file_path': '/a/b/c.py'}, '/ws') == {'/a/b'}
    assert extract_dirs('write_file', {'file_path': 'src/x.py'}, '/ws') == {'/ws/src'}
    assert extract_dirs('patch', {'file_path': 'x.py'}, '/ws') == {'/ws'}


def test_extract_dir_tools():
    assert extract_dirs('tree', {'path': '/a/b/'}, '/ws') == {'/a/b'}
    assert extract_dirs('stats', {'path': 'sub'}, '/ws') == {'/ws/sub'}


def test_extract_bash_cd():
    script = 'cd "/a/b" && make; cd ../c\ncd $HOME; cd ~/x; cd -'
    assert extract_dirs('bash', {'script': script}, '/ws/p') == {'/a/b', '/ws/c'}


def test_extract_skips_noise():
    for p in ('/tmp/x/f.py', '/_self/kb/f.md', '/f.py', '/proc/1/f'):
        assert extract_dirs('read_file', {'file_path': p}, '/ws') == set()
    assert extract_dirs('grep', {'path': '/a'}, '/ws') == set()


def test_touch_and_rank(agent_id):
    db.touch_dirs(agent_id, ['/a'])
    db.touch_dirs(agent_id, ['/a', '/b'])
    rows = db.get_frequent_dirs(agent_id)
    assert [(r['path'], r['hits']) for r in rows] == [('/a', 2), ('/b', 1)]
    assert [r['path'] for r in db.get_frequent_dirs(agent_id, 'b')] == ['/b']


def test_listener_records_and_ignores_errors(agent_id):
    on_tool_executed(_event(agent_id, 'read_file', {'file_path': 'src/a.py'}, has_error=True))
    assert db.get_frequent_dirs(agent_id) == []
    on_tool_executed(_event(agent_id, 'read_file', {'file_path': 'src/a.py'}))
    assert [r['path'] for r in db.get_frequent_dirs(agent_id)] == ['/work/proj/src']


def test_recall_paths(agent_id):
    _, executor = _builtin_recall_factory({'id': agent_id})
    assert executor({'query': '', 'mode': 'paths'})['frequent_dirs'] == []
    db.touch_dirs(agent_id, ['/work/proj'])
    out = executor({'query': 'proj', 'mode': 'paths'})
    assert [r['path'] for r in out['frequent_dirs']] == ['/work/proj']
