"""
bash — Bash script execution via the active execution backend.

The backend is resolved per-session from the backend registry:
  - Default: DockerBackend (sandboxed Docker container, shared with runpy)
  - If sandbox_enabled=0: LocalBackend (direct host subprocess)
  - If sshc is connected: SSHBackend (remote server)

New backends (E2B, etc.) plug in without changing this file.
"""

from backend.tools.lib.exec_backend import registry, validate_env_keys


def execute(agent: dict, args: dict) -> dict:
    action = args.get('action', 'run')
    session_id = (agent or {}).get('session_id') or 'default'

    if action == 'destroy':
        sandbox_enabled = (agent or {}).get('sandbox_enabled', 1)
        # Only destroy Docker containers; SSH/other backends ignore this
        if not sandbox_enabled:
            return {'status': 'ok', 'message': 'Sandbox disabled; nothing to destroy.'}
        backend = registry.get_backend(session_id, agent)
        return backend.destroy()

    if action != 'run':
        return {'error': f"Unknown action: {action!r}. Use 'run' or 'destroy'."}

    script = args.get('script')
    if not script:
        return {'error': "Missing required argument: 'script'"}

    # ------------------------------------------------------------------
    # Long-running command guard (detect build commands, suggest tmux/screen)
    # ------------------------------------------------------------------
    from backend.tools.lib.long_running_guard import check_long_running

    lr = check_long_running(script)
    if lr:
        return {
            'error': (
                f"Long-running command detected: {lr['matched_command']}. "
                f"This command may exceed the bash timeout. "
                f"Run it in a background session instead by executing the `run_script` below, "
                f"then use `monitor_script` to watch output and `check_status_script` to check completion."
            ),
            'level': 'long_running',
            'matched_command': lr['matched_command'],
            'run_script': lr['run_script'],
            'log_file': lr['log_file'],
            'monitor_script': lr['monitor_script'],
            'check_status_script': lr['check_status_script'],
            'check_exit_code_script': lr['check_exit_code_script'],
        }

    # ------------------------------------------------------------------
    # HMADS safety check (pipeline: system rules + custom user rules)
    # ------------------------------------------------------------------
    from backend.tools.lib.safety_pipeline import get_safety_pipeline, should_skip_safety

    if not should_skip_safety(agent) and agent.get('safety_checker_enabled', 1) and not agent.get('is_super'):
        safety = get_safety_pipeline().check(script, tool_type='bash', agent_context=agent)
    else:
        safety = {'level': 'safe', 'score': 0, 'reasons': [], 'blocked_patterns': [], 'approval_info': {}}

    if safety['level'] == 'dangerous':
        return {
            'error': 'Execution blocked by heuristic safety system',
            'level': 'dangerous',
            'score': safety['score'],
            'reasons': safety['reasons'],
            'blocked_patterns': safety['blocked_patterns'],
        }

    if safety['level'] == 'requires_approval':
        return {
            'error': 'Script requires manual approval before execution',
            'level': 'requires_approval',
            'score': safety['score'],
            'reasons': safety['reasons'],
            'blocked_patterns': safety['blocked_patterns'],
            'approval_info': safety['approval_info'],
        }

    if safety['level'] == 'warning':
        print(f'[bash] WARNING: Script flagged as suspicious (score={safety["score"]}): {safety["reasons"]}')

    raw_timeout = args.get('timeout', 60)
    try:
        timeout = max(1, min(int(raw_timeout), 300))
    except (TypeError, ValueError):
        timeout = 60

    env = args.get('env') or {}
    if not isinstance(env, dict):
        return {'error': "'env' must be an object (dict) of string key-value pairs."}
    env, err = validate_env_keys(env)
    if err:
        return {'error': err}

    # ------------------------------------------------------------------
    # Dispatch to active backend
    # ------------------------------------------------------------------
    backend = registry.get_backend(session_id, agent)
    return backend.run_bash(script, timeout, env)


# ---------------------------------------------------------------------------
# Self-tests (run with: python3 -m backend.tools.bash)
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

    agent = {'session_id': 'test-bash-self-test'}
    passed = 0

    # Smoke check: verify Docker sandbox is functional before running full suite
    print('Test 0: Docker sandbox smoke check')
    r = execute(agent, {'script': 'echo "smoke"'})
    if r.get('error') or r.get('exit_code') != 0:
        execute(agent, {'action': 'destroy'})
        _skip(f'Docker sandbox not functional in this environment: {r}')
        return
    passed += 1

    print('Test 1: Basic script execution')
    r = execute(agent, {'script': 'echo "hello world"'})
    assert r.get('exit_code') == 0, r
    assert 'hello world' in r['stdout'], r
    passed += 1

    print('Test 2: Multi-line script')
    r = execute(agent, {'script': 'x=42\necho "x=$x"'})
    assert r.get('exit_code') == 0, r
    assert 'x=42' in r['stdout'], r
    passed += 1

    print('Test 3: Session persistence via filesystem')
    execute(agent, {'script': 'echo "persistent" > /tmp/bash_flag.txt'})
    r = execute(agent, {'script': 'cat /tmp/bash_flag.txt'})
    assert r.get('exit_code') == 0, r
    assert 'persistent' in r['stdout'], r
    passed += 1

    print('Test 4: Shared container with runpy — bash reads file written by Python')
    from backend.tools import runpy
    runpy.execute(agent, {'code': 'open("/tmp/cross_tool.txt","w").write("from_python")'})
    r = execute(agent, {'script': 'cat /tmp/cross_tool.txt'})
    assert r.get('exit_code') == 0, r
    assert 'from_python' in r['stdout'], r
    passed += 1

    print('Test 5: Non-zero exit code on failure')
    r = execute(agent, {'script': 'exit 1'})
    assert r.get('exit_code') == 1, r
    passed += 1

    print('Test 6: Stderr captured')
    r = execute(agent, {'script': 'echo "err msg" >&2'})
    assert r.get('exit_code') == 0, r
    assert 'err msg' in r['stderr'], r
    passed += 1

    print('Test 7: Timeout enforcement')
    r = execute(agent, {'script': 'sleep 999', 'timeout': 2})
    assert 'timed out' in r.get('error', '').lower() or r.get('exit_code', 0) != 0, r
    passed += 1

    print('Test 8: Environment variables injected')
    r = execute(agent, {'script': 'echo "$MY_VAR"', 'env': {'MY_VAR': 'hello123'}})
    assert r.get('exit_code') == 0, r
    assert 'hello123' in r['stdout'], r
    passed += 1

    print('Test 9: Invalid env key rejected')
    r = execute(agent, {'script': 'echo x', 'env': {'bad key!': 'v'}})
    assert 'error' in r, r
    passed += 1

    print('Test 10: Missing script returns error')
    r = execute(agent, {})
    assert 'error' in r, r
    passed += 1

    print('Test 11: /workspace is mounted and accessible')
    r = execute(agent, {'script': 'ls /workspace | wc -l'})
    assert r.get('exit_code') == 0, r
    assert int(r['stdout'].strip()) > 0, r
    passed += 1

    print('Test 12: Destroy action tears down shared container')
    r = execute(agent, {'action': 'destroy'})
    assert r.get('result') == 'container_destroyed', r
    passed += 1

    print('Test 13: Destroy on non-existent session returns graceful message')
    r = execute({'session_id': 'no-such-session'}, {'action': 'destroy'})
    assert r.get('result') == 'no_container', r
    passed += 1

    print(f'\nAll {passed} tests passed!')


if __name__ == '__main__':
    test_execute()
