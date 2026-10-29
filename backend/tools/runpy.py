"""
runpy — Python code execution via the active execution backend.

The backend is resolved per-session from the backend registry:
  - Default: DockerBackend (sandboxed Docker container, shared with bash)
  - If sandbox_enabled=0: LocalBackend (direct host subprocess)
  - If a remote workplace is active: RemoteWorkplaceBackend/SSHBackend (remote server)

New backends (E2B, etc.) plug in without changing this file.
"""

# Re-export shared utils that other modules (bash.py, etc.) have historically
# imported from here. Keeps backwards compatibility.
from backend.tools.lib.agent_env import build_agent_env
from backend.tools.lib.exec_backend import registry, validate_env_keys, truncate
from backend.tools.lib.shell_path import ensure_path_env

# Keep these names importable from runpy for any code that imported them before
_validate_env_keys = validate_env_keys
_truncate = truncate

# Also re-export Docker pool internals that bash.py used to import from here.
# These now live in the DockerBackend module.
from backend.tools.lib.backends.docker_backend import (
    _get_or_create_container,
    _destroy_container,
    _docker,
    _pool_lock as _lock,
    _containers,
    _HELPERS_MOUNT,
    SANDBOX_WORKSPACE,
    _MAX_OUTPUT_BYTES,
)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _update_decim_disposition(event_id, result=None, disposition=None):
    """Record the lifecycle outcome on the matching Decim telemetry event.

    The event id is produced by the safety pipeline and kept internal; it is
    never returned to the tool caller.  Failures here must never affect
    execution.
    """
    if not event_id:
        return
    try:
        from backend.services import decim_safety_telemetry
        if disposition is None:
            failed = bool((result or {}).get('error')) or (result or {}).get('exit_code', 0) != 0
            disposition = 'execution_failed' if failed else 'executed'
        decim_safety_telemetry.update_disposition(event_id, disposition)
    except Exception:
        pass


def execute(agent_context: dict, args: dict) -> dict:
    action = args.get('action', 'run')
    session_id = (agent_context or {}).get('session_id') or 'default'

    if action == 'destroy':
        sandbox_enabled = (agent_context or {}).get('sandbox_enabled', 1)
        if not sandbox_enabled:
            return {'status': 'ok', 'message': 'Sandbox disabled; nothing to destroy.'}
        backend = registry.get_backend(session_id, agent_context)
        return backend.destroy()

    if action != 'run':
        return {'error': f"Unknown action: {action!r}. Use 'run' or 'destroy'."}

    code = args.get('code')
    if not code:
        return {'error': "Missing required argument: 'code'"}

    # ------------------------------------------------------------------
    # HMADS safety check (pipeline: system rules + optional Decim Safety)
    # ------------------------------------------------------------------
    from backend.tools.lib.safety_pipeline import (
        get_safety_pipeline, should_skip_safety, verdict_provenance,
    )

    # Exact-boolean trusted replay: only server-owned `_skip_safety is True`
    # bypasses safety (set after human approval).  User/LLM truthy values never do.
    if get_safety_pipeline is not None and not should_skip_safety(agent_context) \
            and agent_context.get('safety_checker_enabled', 1) and not agent_context.get('is_super'):
        safety = get_safety_pipeline().check(code, tool_type='python', agent_context=agent_context)
    else:
        safety = {'level': 'safe', 'score': 0, 'reasons': [], 'blocked_patterns': [], 'approval_info': {}}

    # Decim Safety correlation id (internal only; never returned to the caller).
    _decim_event_id = (safety.get('decim_safety') or {}).get('event_id')
    # Compact, UI-safe provenance for the chat timeline.  The LLM loop pops it
    # before the result reaches the model or the user-visible output.
    _safety_prov = verdict_provenance(safety)

    if safety['level'] == 'dangerous':
        _update_decim_disposition(_decim_event_id, disposition='blocked')
        return {
            'error': 'Execution blocked by heuristic safety system',
            'level': 'dangerous',
            'score': safety['score'],
            'reasons': safety['reasons'],
            'blocked_patterns': safety['blocked_patterns'],
            '_safety': _safety_prov,
        }

    if safety['level'] == 'requires_approval':
        _update_decim_disposition(_decim_event_id, disposition='approval_requested')
        return {
            'error': 'Code requires manual approval before execution',
            'level': 'requires_approval',
            'score': safety['score'],
            'reasons': safety['reasons'],
            'blocked_patterns': safety['blocked_patterns'],
            'approval_info': safety['approval_info'],
            '_safety': _safety_prov,
        }

    if safety['level'] == 'warning':
        print(f'[runpy] WARNING: Code flagged as suspicious (score={safety["score"]}): {safety["reasons"]}')

    raw_timeout = args.get('timeout', 60)
    try:
        timeout = max(1, min(int(raw_timeout), 300))
    except (TypeError, ValueError):
        timeout = 60

    env = args.get('env') or {}
    if not isinstance(env, dict):
        return {'error': "'env' must be an object (dict) of string key-value pairs.",
                '_safety': _safety_prov}

    # Auto-inject agent variables as environment variables (base layer).
    # LLM-specified env takes priority over agent variables.
    env = {**build_agent_env(agent_context), **env}

    env, err = validate_env_keys(env)
    if err:
        return {'error': err, '_safety': _safety_prov}

    # ------------------------------------------------------------------
    # Dispatch to active backend
    # ------------------------------------------------------------------
    backend = registry.get_backend(session_id, agent_context)
    # SSH/Evonet: resolve the host's real PATH once (non-login `bash -s` misses it).
    env = ensure_path_env(agent_context, backend, env)
    result = backend.run_python(code, timeout, env)

    # Best-effort lifecycle disposition for Decim telemetry (internal only).
    _update_decim_disposition(_decim_event_id, result)

    # Provenance travels out-of-band: the LLM loop pops `_safety` before the
    # result is serialized for the model or the user.
    if isinstance(result, dict):
        result['_safety'] = _safety_prov
    return result


# ---------------------------------------------------------------------------
# Self-tests (run with: python3 backend/tools/runpy.py)
# ---------------------------------------------------------------------------

def test_execute():
    import shutil
    import subprocess

    try:
        import pytest as _pytest
        _skip = _pytest.skip
    except ImportError:
        def _skip(msg):
            print(f'SKIP: {msg}')

    if not shutil.which('docker'):
        _skip('docker not found in PATH')
        return

    check = subprocess.run(['docker', 'info'], capture_output=True, timeout=10)
    if check.returncode != 0:
        _skip('docker daemon not reachable')
        return

    agent = {'session_id': 'test-runpy-self-test'}
    passed = 0

    # Smoke check: verify Docker sandbox is functional before running full suite
    print('Test 0: Docker sandbox smoke check')
    r = execute(agent, {'code': 'print("smoke")'})
    if r.get('error') or r.get('exit_code') != 0:
        execute(agent, {'action': 'destroy'})
        _skip(f'Docker sandbox not functional in this environment: {r}')
        return
    passed += 1

    print('Test 1: Basic code execution')
    r = execute(agent, {'code': 'print(2 + 2)'})
    assert r.get('exit_code') == 0, r
    assert '4' in r['stdout'], r
    passed += 1

    print('Test 2: Session persistence (second exec reuses container)')
    r2 = execute(agent, {'code': 'import os; open("/tmp/flag.txt","w").write("ok")'})
    r3 = execute(agent, {'code': 'print(open("/tmp/flag.txt").read())'})
    assert r3.get('exit_code') == 0, r3
    assert 'ok' in r3['stdout'], r3
    passed += 1

    print('Test 3: Non-zero exit code on error')
    r = execute(agent, {'code': 'raise ValueError("test error")'})
    assert r.get('exit_code') != 0, r
    assert 'ValueError' in r['stderr'], r
    passed += 1

    print('Test 4: Timeout enforcement')
    r = execute(agent, {'code': 'import time; time.sleep(999)', 'timeout': 2})
    assert 'timed out' in r.get('error', '').lower() or r.get('exit_code', 0) != 0, r
    passed += 1

    print('Test 5: Environment variables injected')
    r = execute(agent, {'code': 'import os; print(os.environ["MY_VAR"])', 'env': {'MY_VAR': 'hello123'}})
    assert r.get('exit_code') == 0, r
    assert 'hello123' in r['stdout'], r
    passed += 1

    print('Test 6: Invalid env key rejected')
    r = execute(agent, {'code': 'print("x")', 'env': {'bad key!': 'v'}})
    assert 'error' in r, r
    passed += 1

    print('Test 7: Missing code returns error')
    r = execute(agent, {})
    assert 'error' in r, r
    passed += 1

    print('Test 8: Destroy action tears down container')
    r = execute(agent, {'action': 'destroy'})
    assert r.get('result') == 'container_destroyed', r
    passed += 1

    print('Test 9: Destroy on non-existent session returns graceful message')
    r = execute({'session_id': 'no-such-session'}, {'action': 'destroy'})
    assert r.get('result') == 'no_container', r
    passed += 1

    print('Test 10: /workspace mount is accessible')
    r = execute(agent, {'code': 'import os; files = os.listdir("/workspace"); print(len(files))'})
    assert r.get('exit_code') == 0, r
    count = int(r['stdout'].strip())
    assert count > 0, f'Expected files in /workspace, got {count}'
    passed += 1

    execute(agent, {'action': 'destroy'})
    print(f'\nAll {passed} tests passed!')


if __name__ == '__main__':
    test_execute()
