"""
bash — Bash script execution via the active execution backend.

The backend is resolved per-session from the backend registry:
  - Default: DockerBackend (sandboxed Docker container, shared with runpy)
  - If sandbox_enabled=0: LocalBackend (direct host subprocess)
  - If sshc is connected: SSHBackend (remote server)

New backends (E2B, etc.) plug in without changing this file.
"""

import logging
import re

from backend.tools.lib.exec_backend import registry, validate_env_keys

_VALID_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

try:
    from backend.tools.lib.safety_pipeline import get_safety_pipeline, should_skip_safety
except ImportError:
    logging.getLogger(__name__).warning("safety_pipeline unavailable — safety checks disabled for bash tool")
    get_safety_pipeline = None
    should_skip_safety = lambda agent: True

try:
    from backend.tools.lib.heuristic_safety import check_root_filesystem_scan
except ImportError:
    check_root_filesystem_scan = lambda script: None


def _get_long_running_setting() -> bool:
    """Check whether the long-running command guard is enabled.

    Priority: env var LR_GUARD_DISABLED=1 force-disables it (config.py).
    Otherwise, falls back to the 'long_running_guard_enabled' DB setting
    (defaults to '1' = enabled).
    """
    import config as _cfg
    if not _cfg.LONG_RUNNING_GUARD_ENABLED:
        return False
    try:
        from models.db import db
        val = db.get_setting('long_running_guard_enabled', '1')
        return val == '1'
    except Exception:
        return True


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

    script = args.get('script') or args.get('command')
    if not script:
        return {'error': "Missing required argument: 'script'"}

    # ------------------------------------------------------------------
    # Long-running command guard (detect build commands, suggest tmux/screen)
    # Can be disabled globally via LR_GUARD_DISABLED=1 env var or the
    # long_running_guard_enabled DB setting (toggled in System > Settings UI).
    # ------------------------------------------------------------------
    from backend.tools.lib.long_running_guard import check_long_running

    _guard_enabled = _get_long_running_setting()
    if not _guard_enabled:
        lr = None
    else:
        lr = check_long_running(script)

    if lr:
        return {
            'error': (
                f"BLOCKED: Long-running command detected ({lr['matched_command']}). "
                f"Do NOT retry the command directly — it will be blocked again.\n\n"
                f"REQUIRED: Copy and execute this exact script as your next bash call:\n"
                f"```\n{lr['run_script']}\n```\n\n"
                f"It runs unwatched — nothing will notify you when it finishes. "
                f"If the outcome matters, call the `monitor` tool on the job_id "
                f"returned by that bash call and you will be told once, when "
                f"your condition is met. Either way do NOT poll in a loop. "
                f"To peek at output: {lr['monitor_script']}"
            ),
        }

    # ------------------------------------------------------------------
    # Root filesystem scan guard (performance concern, e.g. `find /`, `tree /`).
    # Independent of the safety pipeline on purpose: it fires for ALL agents —
    # including super agents and agents with safety_checker_enabled=0 — because a
    # full-root scan is a performance hazard regardless of trust. We still honour
    # `_skip_safety` (set on the post-approval re-execution) so an approved scan
    # runs instead of re-prompting forever.
    # ------------------------------------------------------------------
    if not should_skip_safety(agent):
        _rfs = check_root_filesystem_scan(script)
        if _rfs:
            return {
                'error': 'Script requires manual approval before execution',
                'level': 'requires_approval',
                'score': _rfs['score'],
                'reasons': _rfs['reasons'],
                'blocked_patterns': _rfs['blocked_patterns'],
                'approval_info': _rfs['approval_info'],
            }

    # ------------------------------------------------------------------
    # HMADS safety check (pipeline: system rules + custom user rules)
    # ------------------------------------------------------------------
    if get_safety_pipeline is not None and not should_skip_safety(agent) and agent.get('safety_checker_enabled', 1) and not agent.get('is_super'):        safety = get_safety_pipeline().check(script, tool_type='bash', agent_context=agent)
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

    # Auto-inject agent variables as environment variables (base layer).
    # LLM-specified env takes priority over agent variables.
    agent_vars = (agent or {}).get('variables') or {}
    if agent_vars:
        base = {k: str(v) for k, v in agent_vars.items() if _VALID_ENV_KEY_RE.match(k)}
        base.update(env)
        env = base

    env, err = validate_env_keys(env)
    if err:
        return {'error': err}

    # ------------------------------------------------------------------
    # Dispatch to active backend
    # ------------------------------------------------------------------
    backend = registry.get_backend(session_id, agent)
    result = backend.run_bash(script, timeout, env)

    # Identify background spawns so the agent can attach a monitor to them.
    # Registration is silent — nothing watches or notifies on its own.
    try:
        job = _track_background_spawn(session_id, script, result)
        if job:
            result['background_job'] = {
                'job_id': job.job_id,
                'log_file': job.log_file,
                'kind': job.kind,
                'hint': ('Running unwatched. Call monitor(action="attach", '
                         f'target={{"job_id": "{job.job_id}"}}, when={{...}}) '
                         'if you need to be told when something happens.'),
            }
    except Exception:
        pass  # Never let job tracking break command execution
    return result


def _track_background_spawn(session_id: str, script: str, result: dict):
    """Register a background spawn after successful execution.

    Handles both long_running_guard wrapper scripts (BYPASS_MARKER) and the
    agent's own tmux/screen/nohup spawns. Only fires when the spawning script
    itself succeeded — a failed spawn has nothing to track. Returns the
    BackgroundJob, or None when the script did not spawn anything.
    """
    if result.get('error') or result.get('exit_code', 0) != 0:
        return None

    from backend.agent_runtime.background_jobs import (
        parse_wrapper_script, parse_manual_spawn, background_jobs)

    _spawn = parse_wrapper_script(script) or parse_manual_spawn(script)
    if not _spawn:
        return None
    return background_jobs.register(session_id, **_spawn)


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
