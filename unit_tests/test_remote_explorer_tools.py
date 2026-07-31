import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'skills' / 'direxplorer' / 'backend' / 'tools'


def _load_tools():
    package = 'test_direxplorer_tools'
    pkg = types.ModuleType(package)
    pkg.__path__ = [str(TOOLS)]
    sys.modules[package] = pkg
    loaded = {}
    for name in ('_utils', 'Read', 'Glob', 'Grep'):
        spec = importlib.util.spec_from_file_location(f'{package}.{name}', TOOLS / f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


TOOLS_MODULES = _load_tools()


class Backend:
    def resolve_path(self, path):
        return path

    def run_python(self, code, timeout, env):
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=timeout)
        return {'stdout': proc.stdout, 'stderr': proc.stderr, 'exit_code': proc.returncode}

    def run_bash(self, script, timeout, env):
        proc = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=timeout)
        return {'stdout': proc.stdout, 'stderr': proc.stderr, 'exit_code': proc.returncode}

    def read_file(self, path):
        try:
            return {'content': Path(path).read_text(errors='replace')}
        except Exception as exc:
            return {'error': str(exc)}


@pytest.fixture
def remote(tmp_path):
    workspace = tmp_path / 'remote'
    workspace.mkdir()
    (workspace / 'src').mkdir()
    (workspace / 'src' / 'app.py').write_text('alpha\nbeta alpha\n')
    agent = {'workspace': str(workspace), 'workplace_id': 'remote-1', 'session_id': 'explorer-session'}
    with patch('backend.tools.lib.exec_backend.registry.get_backend', return_value=Backend()):
        yield workspace, agent


def test_remote_read_glob_and_grep(remote):
    workspace, agent = remote
    read = TOOLS_MODULES['Read'].execute(agent, {'file_path': 'src/app.py'})
    assert '1: alpha' in read['content']
    glob = TOOLS_MODULES['Glob'].execute(agent, {'path': '.', 'pattern': '**/*.py'})
    assert glob['files'] == ['src/app.py']
    grep = TOOLS_MODULES['Grep'].execute(agent, {'path': '.', 'pattern': 'alpha'})
    if grep.get('error') == 'ripgrep (rg) is not installed':
        pytest.skip('ripgrep unavailable')
    assert grep['total_matches'] == 2
    assert grep['matches'][0]['file'] == 'src/app.py'


def test_remote_read_accepts_path_alias(remote):
    _, agent = remote
    result = TOOLS_MODULES['Read'].execute(agent, {'path': 'src/app.py'})
    assert '1: alpha' in result['content']


def test_remote_read_prefers_file_path_over_path(remote):
    _, agent = remote
    result = TOOLS_MODULES['Read'].execute(agent, {
        'file_path': 'src/app.py', 'path': '../outside.txt'
    })
    assert '1: alpha' in result['content']


def test_remote_read_requires_path_argument(remote):
    _, agent = remote
    assert TOOLS_MODULES['Read'].execute(agent, {}) == {'error': 'file_path is required'}


def test_remote_boundary_rejects_traversal(remote):
    _, agent = remote
    result = TOOLS_MODULES['Read'].execute(agent, {'file_path': '../outside.txt'})
    assert result == {'error': 'Access denied: path escapes workspace'}


def test_remote_boundary_rejects_symlink_escape(remote, tmp_path):
    workspace, agent = remote
    outside = tmp_path / 'outside.txt'
    outside.write_text('secret')
    (workspace / 'escape').symlink_to(outside)
    result = TOOLS_MODULES['Read'].execute(agent, {'file_path': 'escape'})
    assert result == {'error': 'Access denied: path escapes workspace'}


def test_local_backend_behavior(remote):
    workspace, agent = remote
    agent.pop('workplace_id')
    result = TOOLS_MODULES['Read'].execute(agent, {'file_path': 'src/app.py', 'offset': 2})
    assert result['shown_start'] == 2
    assert '2: beta alpha' in result['content']
