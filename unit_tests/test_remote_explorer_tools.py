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
    for name in ('_utils', 'Glob', 'Grep'):
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


def test_remote_glob_and_grep(remote):
    workspace, agent = remote
    glob = TOOLS_MODULES['Glob'].execute(agent, {'path': '.', 'pattern': '**/*.py'})
    assert glob['files'] == ['src/app.py']
    grep = TOOLS_MODULES['Grep'].execute(agent, {'path': '.', 'pattern': 'alpha'})
    if grep.get('error') == 'ripgrep (rg) is not installed':
        pytest.skip('ripgrep unavailable')
    assert grep['total_matches'] == 2
    assert grep['matches'][0]['file'] == 'src/app.py'


def test_remote_boundary_rejects_traversal(remote):
    _, agent = remote
    result = TOOLS_MODULES['Glob'].execute(agent, {'path': '..', 'pattern': '*'})
    assert result == {'error': 'Access denied: path escapes workspace'}


def test_remote_boundary_rejects_symlink_escape(remote, tmp_path):
    workspace, agent = remote
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('secret')
    (workspace / 'escape').symlink_to(outside)
    result = TOOLS_MODULES['Glob'].execute(agent, {'path': 'escape', 'pattern': '*'})
    assert result == {'error': 'Access denied: path escapes workspace'}


def test_remote_glob_caps_at_max_files(remote):
    """Recursive glob over a large tree must cap results, not blow the stdout limit."""
    workspace, agent = remote
    many = workspace / 'many'
    many.mkdir()
    for i in range(1005):
        (many / f'f{i:04d}.txt').write_text('x')
    total = 1005 + 1  # + src/app.py
    glob = TOOLS_MODULES['Glob'].execute(agent, {'path': '.', 'pattern': '**'})
    assert 'error' not in glob
    assert len(glob['files']) == 1000
    assert glob['count'] == total
    assert glob['truncated'] is True


def test_run_python_json_raises_on_truncated_output():
    """_run_python_json must surface backend stdout truncation clearly."""
    class TruncatedBackend:
        def run_python(self, code, timeout, env):
            return {'stdout': '{"files": ["a"' + '\n[truncated]', 'stderr': '', 'exit_code': 0}

    with pytest.raises(RuntimeError, match='truncated'):
        TOOLS_MODULES['_utils']._run_python_json(TruncatedBackend(), 'code')


def test_run_python_json_reports_unparseable_stdout():
    """Non-truncated invalid JSON should report the actual decode error."""
    class BadBackend:
        def run_python(self, code, timeout, env):
            return {'stdout': 'not json at all', 'stderr': '', 'exit_code': 0}

    with pytest.raises(RuntimeError, match='unparseable'):
        TOOLS_MODULES['_utils']._run_python_json(BadBackend(), 'code')
