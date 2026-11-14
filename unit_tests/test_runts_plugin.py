"""
runts plugin tests — the tool, its guard, and one real execution.

Covers the four ways this plugin can go wrong:
1. the guard does not fire for the plugin tool (core injection guard only knows
   built-in tool names), so `runts` would be a bypass of bash/runpy guarding;
2. the code is mangled on the way to the shell (quotes, newlines, $, backticks);
3. the tool runs somewhere other than the workplace, or silently on the host
   when the workplace is missing;
4. the knobs (ENABLED / TIMEOUT_SECONDS / MAX_OUTPUT_BYTES / BUN_BIN) do not
   actually govern the call.
"""

import importlib.util
import json
import os
import shlex
import shutil
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.plugin_lifecycle import PluginManager
from backend.tools.lib.backends.local_backend import LocalBackend
from backend.tools.lib.exec_backend import registry as exec_registry

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, 'plugins', 'runts')


def _load_tool_module():
    path = os.path.join(PLUGIN_DIR, 'backend', 'tools', 'runts.py')
    spec = importlib.util.spec_from_file_location('runts_tool_under_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['runts_tool_under_test'] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runts(monkeypatch):
    module = _load_tool_module()
    monkeypatch.setattr(module, '_config', lambda: dict(module._DEFAULTS))
    return module


class _Backend:
    """Records the bash command instead of running it."""

    def __init__(self, result=None):
        self.commands = []
        self.result = result or {'stdout': 'ok\n', 'stderr': '', 'exit_code': 0,
                                 'execution_time': 0.1}

    def run_bash(self, script, timeout, env, on_output=None):
        self.commands.append({'script': script, 'timeout': timeout, 'env': env})
        return dict(self.result)


@pytest.fixture
def backend(monkeypatch):
    fake = _Backend()
    monkeypatch.setattr(exec_registry, 'get_backend', lambda sid, ctx: fake)
    monkeypatch.setattr('backend.tools.lib.shell_path.ensure_path_env',
                        lambda agent, be, env: env)
    return fake


class TestPluginManifest:
    def test_manifest_and_tools_file_agree(self):
        manifest = json.load(open(os.path.join(PLUGIN_DIR, 'plugin.json')))
        assert manifest['id'] == 'runts'
        assert manifest['tools_file'] == 'tools.json'
        names = {v['name'] for v in manifest['variables']}
        assert {'ENABLED', 'TIMEOUT_SECONDS', 'MAX_OUTPUT_BYTES', 'BUN_BIN'} <= names

        tools = json.load(open(os.path.join(PLUGIN_DIR, 'tools.json')))
        fn = tools[0]['function']
        assert fn['name'] == 'runts'
        assert fn['parameters']['required'] == ['code']
        assert 'TypeScript' in fn['description']
        # The statelessness promise must be visible to the model, not just in code.
        assert 'STATELESS' in fn['description']

    def test_tool_and_guard_load_through_the_plugin_api(self, monkeypatch, tmp_path):
        """Enabled plugin → tool def tagged plugin:runts:runts and a live guard."""
        import backend.plugin_hooks as hooks

        # Only runts in the plugins dir: the real lifecycle, no unrelated plugins.
        plugins_dir = tmp_path / 'plugins'
        plugins_dir.mkdir()
        shutil.copytree(PLUGIN_DIR, plugins_dir / 'runts')
        monkeypatch.setattr('backend.plugin_lifecycle.PLUGINS_DIR', str(plugins_dir))
        monkeypatch.setattr(PluginManager, '_is_plugin_enabled', lambda self, pid: True)
        pm = PluginManager()
        monkeypatch.setattr('backend.plugin_manager.plugin_manager', pm)

        defs = [d for d in pm.get_all_plugin_tool_defs() if d['_plugin_id'] == 'runts']
        assert [d['id'] for d in defs] == ['plugin:runts:runts']

        # The handler loaded through the real lifecycle hook registers a guard
        # that knows this tool — the gap the core injection guard leaves open.
        loaded = pm._modules.get('runts')
        assert loaded is not None, 'plugins/runts/handler.py was not loaded'
        assert loaded.tool_guard in hooks._tool_guards
        verdict = loaded.tool_guard('a', 'runts', {'code': '   '})
        assert verdict and verdict['block']
        assert loaded.tool_guard('a', 'bash', {'code': 'rm -rf /'}) is None
        assert loaded.tool_guard('a', 'runts', {'code': 'let n = 1'}) is None

        # Disabling the plugin removes the guard: the tool stops existing AND
        # stops being guarded, so nothing slips through half-removed.
        loaded.on_disable()
        assert loaded.tool_guard not in hooks._tool_guards


class TestGuard:
    def test_guard_blocks_bad_code_and_respects_kill_switch(self, monkeypatch):
        module = _load_tool_module()
        # Load handler.py the way the plugin manager does: as a submodule of a
        # package whose __path__ is the plugin dir, so its relative import of
        # backend.tools.runts resolves.
        pkg_name = 'plugin_pkg_runts_under_test'
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [PLUGIN_DIR]
        sys.modules[pkg_name] = pkg
        spec = importlib.util.spec_from_file_location(
            f'{pkg_name}.handler', os.path.join(PLUGIN_DIR, 'handler.py'))
        handler = importlib.util.module_from_spec(spec)
        sys.modules[f'{pkg_name}.handler'] = handler
        spec.loader.exec_module(handler)
        # Point the handler at our already-loaded tool module.
        monkeypatch.setattr(handler, '_tool', module)

        assert handler.tool_guard('a', 'other_tool', {'code': 'print(1)'}) is None
        assert handler.tool_guard('a', 'runts', {})['block']
        assert handler.tool_guard('a', 'runts', {'code': 42})['block']
        too_big = 'x' * (module.MAX_CODE_BYTES + 1)
        assert handler.tool_guard('a', 'runts', {'code': too_big})['block']
        assert handler.tool_guard('a', 'runts', {'code': 'let n = 1'}) is None

        monkeypatch.setattr(module, '_config', lambda: {'ENABLED': False})
        monkeypatch.setattr(handler, '_tool', module)
        verdict = handler.tool_guard('a', 'runts', {'code': 'let n = 1'})
        assert verdict['block'] and 'disabled' in verdict['error']


class TestExecute:
    def test_code_reaches_the_shell_byte_for_byte(self, runts, backend):
        code = 'const s = "it\'s $HOME `x`";\nconsole.log(s)\nconst n: number = 2'
        out = runts.execute({'session_id': 's'}, {'code': code})
        assert out['exit_code'] == 0
        cmd = backend.commands[0]['script']
        # Exactly two argv words after the binary: -e and the untouched code.
        argv = shlex.split(cmd)
        assert argv[:2] == ['bun', '-e'] and argv[2] == code
        assert '$HOME' in cmd  # not expanded: single-quoted for the shell

    def test_knobs_govern_timeout_output_and_binary(self, runts, backend, monkeypatch):
        monkeypatch.setattr(runts, '_config', lambda: {
            'ENABLED': True, 'TIMEOUT_SECONDS': 7, 'MAX_OUTPUT_BYTES': 1024,
            'BUN_BIN': '/opt/bun/bin/bun'})
        backend.result = {'stdout': 'a' * 5000, 'stderr': '', 'exit_code': 0,
                          'execution_time': 0.2}
        out = runts.execute({'session_id': 's'}, {'code': 'console.log(1)'})
        assert backend.commands[0]['timeout'] == 7
        assert backend.commands[0]['script'].startswith('/opt/bun/bin/bun -e ')
        assert len(out['stdout']) <= 1024 + len('\n[truncated]')
        assert out['stdout'].endswith('[truncated]')

    def test_timeout_is_clamped(self, runts, backend, monkeypatch):
        monkeypatch.setattr(runts, '_config', lambda: {'TIMEOUT_SECONDS': 99999})
        runts.execute({'session_id': 's'}, {'code': 'console.log(1)'})
        assert backend.commands[0]['timeout'] == 300

    def test_missing_workplace_fails_loudly_and_runs_nothing(self, runts, monkeypatch):
        def boom(session_id, ctx):
            raise RuntimeError("Workplace 'wp-1' not found.")
        monkeypatch.setattr(exec_registry, 'get_backend', boom)
        out = runts.execute({'session_id': 's', 'workplace_id': 'wp-1'},
                            {'code': 'console.log(1)'})
        assert 'no execution backend' in out['error']
        assert 'wp-1' in out['error']

    def test_missing_bun_is_a_loud_error(self, runts, backend):
        backend.result = {'stdout': '', 'stderr': 'bash: line 1: bun: command not found',
                          'exit_code': 127, 'execution_time': 0.01}
        out = runts.execute({'session_id': 's'}, {'code': 'console.log(1)'})
        assert out['exit_code'] == 127
        assert 'BUN_BIN' in out['error']

    def test_empty_code_rejected(self, runts, backend):
        assert 'error' in runts.execute({'session_id': 's'}, {'code': '  '})
        assert backend.commands == []


class TestRuntimeDispatch:
    """The path the agent actually takes: Tools-page assignment → executor."""

    def test_assigned_plugin_tool_dispatches_and_unassigned_is_denied(
            self, monkeypatch, tmp_path):
        from backend.tools.registry import ToolRegistry
        import backend.plugin_hooks as hooks

        plugins_dir = tmp_path / 'plugins'
        plugins_dir.mkdir()
        shutil.copytree(PLUGIN_DIR, plugins_dir / 'runts')
        monkeypatch.setattr('backend.plugin_lifecycle.PLUGINS_DIR', str(plugins_dir))
        monkeypatch.setattr(PluginManager, '_is_plugin_enabled', lambda self, pid: True)
        pm = PluginManager()
        monkeypatch.setattr('backend.plugin_manager.plugin_manager', pm)
        monkeypatch.setattr(pm, 'get_plugin_config', lambda pid: {
            'ENABLED': True, 'TIMEOUT_SECONDS': 5, 'MAX_OUTPUT_BYTES': 65536,
            'BUN_BIN': 'node'})

        fake = _Backend()
        monkeypatch.setattr(exec_registry, 'get_backend', lambda sid, ctx: fake)
        monkeypatch.setattr('backend.tools.lib.shell_path.ensure_path_env',
                            lambda agent, be, env: env)

        assigned = {'session_id': 's', 'assigned_tool_ids': ['plugin:runts:runts']}
        out = ToolRegistry().get_real_executor(assigned)('runts', {'code': 'console.log(1)'})
        assert out.get('exit_code') == 0, out
        assert shlex.split(fake.commands[0]['script'])[:2] == ['node', '-e']

        denied = ToolRegistry().get_real_executor({'session_id': 's'})(
            'runts', {'code': 'console.log(1)'})
        assert denied.get('blocked_by') == 'authorization'

        loaded = pm._modules['runts']
        if loaded.tool_guard in hooks._tool_guards:
            hooks.unregister_tool_guard(loaded.tool_guard)


class TestHostRefusal:
    def test_local_backend_is_refused_not_run(self, runts, monkeypatch, tmp_path):
        """sandbox_enabled=0 must fail loudly; the host backend never runs code."""
        host = LocalBackend(session_id='runts-host', workspace=str(tmp_path))
        called = []
        monkeypatch.setattr(host, 'run_bash', lambda *a, **k: called.append(a) or {})
        monkeypatch.setattr(exec_registry, 'get_backend', lambda sid, ctx: host)

        out = runts.execute({'session_id': 'runts-host', 'sandbox_enabled': 0},
                            {'code': 'console.log(1)'})
        assert 'refuses to run on the Evonic host' in out['error']
        assert called == []


_JS_RUNNER = shutil.which('bun') or shutil.which('node')


@pytest.mark.skipif(not _JS_RUNNER, reason='neither bun nor node available for the smoke run')
class TestRealExecution:
    """One real run through a real shell and a real JS runtime.

    bun runs TypeScript natively, node only JavaScript at `-e`, so the snippet
    gains type syntax when bun is the runner. Either way this exercises the
    shell quoting, the workplace cwd and node_modules resolution end to end.
    The host backend is refused by the tool, so the backend here is a
    same-shaped test double that runs the command the way an exec backend does.
    """

    class _ShellBackend:
        """Runs the command string the way an exec backend does: cwd + shell."""

        def __init__(self, workspace):
            self.workspace = workspace

        def run_bash(self, script, timeout, env, on_output=None):
            import subprocess
            import time
            started = time.time()
            proc = subprocess.run(['/bin/bash', '-c', script], cwd=self.workspace,
                                  capture_output=True, text=True, timeout=timeout)
            return {'stdout': proc.stdout, 'stderr': proc.stderr,
                    'exit_code': proc.returncode, 'execution_time': time.time() - started}

    def test_snippet_imports_a_workspace_dependency(self, runts, monkeypatch, tmp_path):
        dep = tmp_path / 'node_modules' / 'answer'
        dep.mkdir(parents=True)
        (dep / 'package.json').write_text('{"name":"answer","main":"index.js"}')
        (dep / 'index.js').write_text('module.exports = 42;')

        backend = self._ShellBackend(str(tmp_path))
        monkeypatch.setattr(exec_registry, 'get_backend', lambda sid, ctx: backend)
        monkeypatch.setattr('backend.tools.lib.shell_path.ensure_path_env',
                            lambda agent, be, env: env)
        monkeypatch.setattr(runts, '_config', lambda: {
            'ENABLED': True, 'TIMEOUT_SECONDS': 30, 'MAX_OUTPUT_BYTES': 65536,
            'BUN_BIN': _JS_RUNNER})

        if os.path.basename(_JS_RUNNER) == 'bun':
            code = ('import ans from "answer";\n'
                    'const n: number = ans;\n'
                    'console.log(n + 1)')
        else:
            code = 'const n = require("answer"); console.log(n + 1)'
        out = runts.execute({'session_id': 'runts-smoke'}, {'code': code})
        assert out['exit_code'] == 0, out
        assert out['stdout'].strip() == '43'
