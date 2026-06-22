"""
Server-side update state manager.

Provides daily-cached update checks, background update execution with log
capture, and SSE listener management for real-time web UI notifications.

Progress state is persisted to disk to survive crashes and restarts.

The update flow uses direct git operations (fetch + reset) — the old
supervisor-based versioned-release mechanism has been removed.
"""

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import config

try:
    from packaging import version as pkg_version
    HAS_PACKAGING = True
except ImportError:
    HAS_PACKAGING = False

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------------

class _VersionComparable:
    """
    Wrapper for version comparison that works with both packaging.version
    and tuple-based comparison for backward compatibility.
    """
    def __init__(self, version_obj, tuple_fallback):
        self.version_obj = version_obj
        self.tuple_fallback = tuple_fallback

    def __lt__(self, other):
        if isinstance(other, _VersionComparable):
            if self.version_obj is not None and other.version_obj is not None:
                return self.version_obj < other.version_obj
            return self.tuple_fallback < other.tuple_fallback
        # Support comparison with plain tuples for tests
        return self.tuple_fallback < other

    def __le__(self, other):
        return self < other or self == other

    def __gt__(self, other):
        if isinstance(other, _VersionComparable):
            if self.version_obj is not None and other.version_obj is not None:
                return self.version_obj > other.version_obj
            return self.tuple_fallback > other.tuple_fallback
        return self.tuple_fallback > other

    def __ge__(self, other):
        return self > other or self == other

    def __eq__(self, other):
        if isinstance(other, _VersionComparable):
            if self.version_obj is not None and other.version_obj is not None:
                return self.version_obj == other.version_obj
            return self.tuple_fallback == other.tuple_fallback
        return self.tuple_fallback == other

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        if self.version_obj is not None:
            return f"_VersionComparable({self.version_obj})"
        return f"_VersionComparable({self.tuple_fallback})"


def _version_tuple(tag: str):
    """
    Parse version string into comparable version object.

    Security: Uses packaging.version when available for proper semver handling,
    including pre-release versions. Falls back to regex for basic parsing.

    Returns a comparable object that works with both packaging.version and
    tuple-based comparison for backward compatibility.
    """
    # Fallback tuple parsing
    m = re.match(r'v?(\d+)(?:\.(\d+))?(?:\.(\d+))?', tag or '')
    if not m:
        tuple_version = (0, 0, 0)
    else:
        tuple_version = tuple(int(x or '0') for x in m.groups())

    # Try packaging.version if available
    version_obj = None
    if HAS_PACKAGING and tag:
        try:
            # Remove 'v' prefix if present
            clean_tag = tag.removeprefix('v')
            version_obj = pkg_version.parse(clean_tag)
        except (ValueError, TypeError):
            # Fall back to tuple only if parsing fails
            pass

    return _VersionComparable(version_obj, tuple_version)


# ---------------------------------------------------------------------------
# State persistence (simplified — no shared/ paths)
# ---------------------------------------------------------------------------

def _get_state_file_path() -> str:
    """Return path to persistent state file."""
    state_dir = os.path.join(config.APP_ROOT, 'state', 'update')
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, 'update_state.json')


def _load_persisted_state() -> dict:
    """Load update state from disk if it exists."""
    state_file = _get_state_file_path()
    if not os.path.exists(state_file):
        return {}

    try:
        with open(state_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log.warning(f'Failed to load persisted state: {e}')
        return {}


def _persist_state(state: dict) -> None:
    """Save update state to disk atomically."""
    state_file = _get_state_file_path()
    temp_file = state_file + '.tmp'

    try:
        with open(temp_file, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(temp_file, state_file)
    except (IOError, OSError) as e:
        log.error(f'Failed to persist state: {e}')
        if os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_listeners: list = []  # list of queue.Queue, one per SSE client

# Total pipeline steps: fetch + reset + reinstall deps + doctor --fix + smoke test
TOTAL_STEPS = 5

# Load persisted state on module import (survives crashes/restarts)
_persisted = _load_persisted_state()

_state = {
    'status': _persisted.get('status', 'idle'),
    'current_version': _persisted.get('current_version'),
    'latest_version': _persisted.get('latest_version'),
    'progress': _persisted.get('progress', 0),
    'step': _persisted.get('step', 0),
    'step_label': _persisted.get('step_label', ''),
    'logs': _persisted.get('logs', []),
    'error': _persisted.get('error'),
    'last_check': _persisted.get('last_check', 0),
    'last_update_attempt': _persisted.get('last_update_attempt', 0),
    'crashed': _persisted.get('status') == 'updating',  # Detect crash during update
}

# If we crashed during update, log it and reset to failed state
if _state['crashed']:
    log.warning(
        f'Detected incomplete update to {_state.get("latest_version")} '
        f'(was at step {_state.get("step")}/{TOTAL_STEPS})'
    )
    with _lock:
        _state['status'] = 'failed'
        _state['error'] = 'Update interrupted (server crash or restart)'
        _state['logs'].append({
            'ts': datetime.now().strftime('%H:%M:%S'),
            'level': 'error',
            'message': 'Update was interrupted by server crash or restart',
        })
        _persist_state(_state)


# ---------------------------------------------------------------------------
# SSE listener helpers
# ---------------------------------------------------------------------------

def _append_log(level: str, message: str):
    entry = {
        'ts': datetime.now().strftime('%H:%M:%S'),
        'level': level,
        'message': message,
    }
    with _lock:
        _state['logs'].append(entry)
        # Persist state after log update
        _persist_state(_state)
    _notify_listeners()


def _notify_listeners():
    snapshot = get_status()
    dead = []
    for q in _listeners:
        try:
            q.put_nowait(snapshot)
        except queue.Full:
            dead.append(q)
    for q in dead:
        try:
            _listeners.remove(q)
        except ValueError:
            pass


def register_listener() -> queue.Queue:
    q = queue.Queue(maxsize=200)
    _listeners.append(q)
    return q


def unregister_listener(q: queue.Queue):
    try:
        _listeners.remove(q)
    except ValueError:
        pass

_cleanup_started = False


def _start_listener_cleanup(interval: int = 600):
    """Periodically prune dead listener queues to prevent unbounded list growth.

    SSE clients that disconnect without calling unregister_listener() leave
    stale queue objects behind. This daemon thread calls _notify_listeners()
    every ``interval`` seconds — the existing dead-queue detection in
    _notify_listeners() handles removal.
    """
    global _cleanup_started
    if _cleanup_started:
        return
    _cleanup_started = True
    def _cleanup_loop():
        while True:
            time.sleep(interval)
            _notify_listeners()
    threading.Thread(target=_cleanup_loop, daemon=True, name='listener-cleanup').start()


# ---------------------------------------------------------------------------
# WebNotifier — duck-type compatible with TelegramNotifier
# ---------------------------------------------------------------------------

class WebNotifier:
    """Drop-in replacement for TelegramNotifier that updates web UI state."""

    def begin(self, from_tag, to_tag):
        with _lock:
            _state['current_version'] = from_tag
            _state['latest_version'] = to_tag
            _persist_state(_state)

    def send_progress(self, step, total, description):
        with _lock:
            _state['step'] = step
            _state['step_label'] = description
            _state['progress'] = int(step / total * 100) if total else 0
        _append_log('info', f'Step {step}/{total}: {description}')

    def send_failure(self, step, total, error):
        with _lock:
            _state['status'] = 'failed'
            _state['error'] = str(error)
        _append_log('error', f'FAILED at step {step}/{total}: {error}')

    def send_success(self, tag):
        with _lock:
            _state['status'] = 'success'
            _state['progress'] = 100
        _append_log('info', f'Update to {tag} successful')


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git_run(*args, cwd=None):
    """Run a git command and return (returncode, stdout, stderr)."""
    cmd = ['git'] + list(args)
    result = subprocess.run(
        cmd,
        cwd=cwd or config.APP_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _get_current_version():
    """Get the current version from git describe."""
    rc, stdout, _ = _git_run('describe', '--tags', '--always')
    return stdout if rc == 0 else None


# ---------------------------------------------------------------------------
# Shared apply pipeline (reused by the web updater and the CLI)
# ---------------------------------------------------------------------------

def _venv_python() -> str:
    """Path to the project venv python, falling back to the current interpreter."""
    venv = os.path.join(config.APP_ROOT, '.venv', 'bin', 'python')
    return venv if os.path.exists(venv) else sys.executable


def _reinstall_deps() -> tuple:
    """Reinstall dependencies in case requirements.txt changed. Returns (ok, detail)."""
    req = os.path.join(config.APP_ROOT, 'requirements.txt')
    if not os.path.isfile(req):
        return True, 'no requirements.txt'
    proc = subprocess.run(
        [_venv_python(), '-m', 'pip', 'install', '-q', '-r', req],
        cwd=config.APP_ROOT, capture_output=True, text=True,
    )
    detail = (proc.stderr.strip() or proc.stdout.strip())[-500:]
    return proc.returncode == 0, detail


def _smoke_test() -> tuple:
    """Import the updated tree in a subprocess to catch syntax/import errors
    before the live server is restarted. EVONIC_SMOKE_TEST skips channel and
    scheduler startup so the probe has no side effects. Returns (ok, detail)."""
    env = dict(os.environ, EVONIC_SMOKE_TEST='1')
    try:
        proc = subprocess.run(
            [_venv_python(), '-c', 'import app'],
            cwd=config.APP_ROOT, capture_output=True, text=True,
            timeout=120, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, 'smoke test timed out'
    detail = (proc.stderr.strip() or proc.stdout.strip())[-1000:]
    return proc.returncode == 0, detail


def _run_doctor_fix() -> tuple:
    """Repair the environment via `evonic doctor --fix` (creates dirs, installs
    optional deps). Mirrors the CLI repair step so the web updater behaves the
    same. Returns (ok, detail)."""
    evonic_bin = os.path.join(config.APP_ROOT, 'evonic')
    if not os.path.isfile(evonic_bin):
        return True, 'no evonic wrapper'
    proc = subprocess.run(
        [evonic_bin, 'doctor', '--fix'],
        cwd=config.APP_ROOT, capture_output=True, text=True,
    )
    detail = (proc.stderr.strip() or proc.stdout.strip())[-500:]
    return proc.returncode == 0, detail


def _repair_and_verify() -> tuple:
    """Reinstall deps, repair the environment (doctor --fix), then smoke-test the
    tree before it goes live. deps and smoke are gating; doctor is logged
    best-effort so optional-dependency problems don't fail an otherwise bootable
    tree (the smoke test is the real gate). Returns (ok, detail)."""
    deps_ok, deps_detail = _reinstall_deps()
    if not deps_ok:
        return False, f'Dependency install failed: {deps_detail}'
    doctor_ok, doctor_detail = _run_doctor_fix()
    if not doctor_ok:
        _append_log('warning', f'doctor --fix reported problems: {doctor_detail}')
    smoke_ok, smoke_detail = _smoke_test()
    if not smoke_ok:
        return False, f'Updated code failed to import: {smoke_detail}'
    return True, ''


def _record_previous_commit(sha: str) -> None:
    """Persist the pre-update commit so rollback is deterministic. The reflog's
    HEAD@{1} is unreliable here: apply_update's auto-rollback on a failed update
    moves HEAD@{1} onto the rejected commit."""
    with _lock:
        _state['previous_commit'] = sha
        _persist_state(_state)


def _resolve_rollback_target() -> str:
    """Commit to roll back to: the recorded pre-update commit if available, else
    the reflog fallback (HEAD@{1}) for back-compat. Reads persisted state from
    disk so it works regardless of whether the CLI or the web path did the
    update."""
    persisted = _load_persisted_state().get('previous_commit')
    if persisted:
        return persisted
    rc, prev, _ = _git_run('rev-parse', 'HEAD@{1}')
    return prev if rc == 0 else ''


def _acquire_update_lock():
    """Cross-process advisory lock so concurrent web+CLI calls don't race on
    git operations. POSIX only; no-op on Windows (single-user install assumption).
    Returns (acquired: bool, fd: file | None)."""
    if sys.platform == 'win32':
        return True, None
    lock_path = os.path.join(os.path.dirname(_get_state_file_path()), 'update.lock')
    try:
        import fcntl
        fd = open(lock_path, 'w')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True, fd
    except (IOError, OSError):
        try:
            fd.close()
        except Exception:
            pass
        return False, None


def _release_update_lock(fd) -> None:
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    except Exception:
        pass


def apply_update(target: str, progress_cb=None) -> dict:
    """Apply an update to the given git ref with reinstall, smoke test, and
    auto-rollback. Shared by the web updater and the CLI so both behave the same.

    Steps: fetch → record HEAD → reset --hard <ref> → reinstall deps → doctor
    --fix → smoke test. If a gating step fails, the tree is reset back to the
    prior commit so the running server stays bootable. Does NOT restart — the
    caller decides. progress_cb(step, total, label) is called before each step
    when provided (web notifier hook).

    Returns {'success': True} or {'error': str, 'failed_step': int}.
    """
    STEPS = 5

    def _step(n, label):
        if progress_cb:
            progress_cb(n, STEPS, label)

    acquired, lock_fd = _acquire_update_lock()
    if not acquired:
        return {'error': 'Another update or rollback is already running', 'failed_step': 1}
    try:
        _step(1, 'Fetching updates from origin...')
        rc, _, err = _git_run('fetch', 'origin', '--tags')
        if rc != 0:
            return {'error': f'Git fetch failed: {err}', 'failed_step': 1}

        rc, prev, err = _git_run('rev-parse', 'HEAD')
        if rc != 0:
            return {'error': f'Could not record current commit: {err}', 'failed_step': 1}
        _record_previous_commit(prev)

        _step(2, f'Applying update to {target}...')
        ref = target or 'origin/main'
        rc, _, err = _git_run('reset', '--hard', ref)
        if rc != 0:
            return {'error': f'Git reset failed: {err}', 'failed_step': 2}

        _step(3, 'Reinstalling dependencies...')
        deps_ok, deps_detail = _reinstall_deps()
        if not deps_ok:
            _git_run('reset', '--hard', prev)
            return {'error': f'Dependency install failed: {deps_detail} (rolled back)',
                    'failed_step': 3}

        # doctor --fix is best-effort: optional-dep issues must not block an
        # otherwise bootable tree. The smoke test is the real boot gate.
        _step(4, 'Repairing environment...')
        doctor_ok, doctor_detail = _run_doctor_fix()
        if not doctor_ok:
            _append_log('warning', f'doctor --fix reported problems: {doctor_detail}')

        _step(5, 'Running smoke test...')
        smoke_ok, smoke_detail = _smoke_test()
        if not smoke_ok:
            _git_run('reset', '--hard', prev)
            return {'error': f'Updated code failed to import: {smoke_detail} (rolled back)',
                    'failed_step': 5}

        return {'success': True}
    finally:
        _release_update_lock(lock_fd)


def apply_rollback() -> dict:
    """Reset to the recorded previous commit, then repair and verify. Synchronous
    — shared by the CLI (`evonic update --rollback`) and the threaded web
    trigger_rollback. Returns {'success': True, 'target': sha} or {'error': str}."""
    acquired, lock_fd = _acquire_update_lock()
    if not acquired:
        return {'error': 'Another update or rollback is already running'}
    try:
        target = _resolve_rollback_target()
        if not target:
            return {'error': 'No previous state to roll back to'}

        rc, _, stderr = _git_run('reset', '--hard', target)
        if rc != 0:
            return {'error': f'Rollback failed: {stderr}'}

        ok, detail = _repair_and_verify()
        if not ok:
            return {'error': f'Rolled back to {target[:8]} but {detail}'}

        return {'success': True, 'target': target}
    finally:
        _release_update_lock(lock_fd)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_status() -> dict:
    with _lock:
        status = {
            'status': _state['status'],
            'current_version': _state['current_version'],
            'latest_version': _state['latest_version'],
            'progress': _state['progress'],
            'step': _state['step'],
            'step_label': _state['step_label'],
            'logs': list(_state['logs']),
            'error': _state['error'],
            'crashed': _state.get('crashed', False),
            'last_update_attempt': _state.get('last_update_attempt', 0),
        }
        # Clear crashed flag after first status read
        if _state.get('crashed'):
            _state['crashed'] = False
        return status


def check_for_update(force=False) -> dict:
    now = time.time()

    with _lock:
        if not force and (now - _state['last_check']) < 86400:
            return {
                'available': _state['status'] == 'available',
                'current': _state['current_version'],
                'latest': _state['latest_version'],
            }

        _state['status'] = 'checking'
        _persist_state(_state)

    try:
        # Fetch tags from origin
        _git_run('fetch', 'origin', '--tags')

        # Get current version
        current = _get_current_version()

        # Get latest tag from origin — try origin/main first, then all tags
        rc, latest_tag, _ = _git_run(
            'describe', '--tags', '--abbrev=0', 'origin/main'
        )
        if rc != 0:
            # Fallback: get the most recent tag sorted by version
            rc, tags_output, _ = _git_run(
                'tag', '--sort=-version:refname'
            )
            if rc == 0 and tags_output:
                latest_tag = tags_output.split('\n')[0]
            else:
                latest_tag = None

        with _lock:
            # Do not overwrite state if an update started while we were
            # doing network I/O (TOCTOU window between lock release at
            # line 337 and re-acquire here). Return fresh check result
            # without persisting so the update's state survives.
            if _state['status'] == 'updating':
                return {
                    'available': (
                        latest_tag is not None
                        and _version_tuple(latest_tag) > _version_tuple(current or '')
                    ),
                    'current': current,
                    'latest': latest_tag,
                }

            _state['current_version'] = current
            _state['latest_version'] = latest_tag
            _state['last_check'] = time.time()

            if latest_tag and current and _version_tuple(latest_tag) > _version_tuple(current):
                _state['status'] = 'available'
                _persist_state(_state)
                return {'available': True, 'current': current, 'latest': latest_tag}
            else:
                _state['status'] = 'idle'
                _persist_state(_state)
                return {'available': False, 'current': current, 'latest': latest_tag}
    except Exception as e:
        log.error(f'Update check failed: {e}')
        with _lock:
            _state['status'] = 'idle'
            _persist_state(_state)
        return {'available': False, 'current': None, 'latest': None, 'error': str(e)}


def start_update(tag=None) -> dict:
    with _lock:
        if _state['status'] == 'updating':
            return {'error': 'Update already in progress'}

        _state['status'] = 'updating'
        _state['progress'] = 0
        _state['step'] = 0
        _state['step_label'] = ''
        _state['error'] = None
        _state['last_update_attempt'] = time.time()
        _state['logs'] = []

        target = tag or _state['latest_version']
        if not target:
            _state['status'] = 'failed'
            _state['error'] = 'No target version specified'
            _persist_state(_state)
            return {'error': 'No target version specified'}

        current = _state.get('current_version')
        if target and current and target == current:
            _state['status'] = 'idle'
            _persist_state(_state)
            return {'error': f'Already running {target}'}

        _persist_state(_state)

    _append_log('info', f'Starting update to {target}...')
    _notify_listeners()

    t = threading.Thread(target=_run_update_thread, args=(target,), daemon=True)
    t.start()
    return {'success': True, 'target': target}


def _run_update_thread(target):
    """Run the update in a background thread.

    Applies the update via the shared apply_update pipeline (git → reinstall →
    doctor --fix → smoke test → auto-rollback on failure). Progress is broadcast
    to SSE listeners via WebNotifier at each pipeline phase so operators can see
    which step is running during a long update. On success the user restarts via
    the existing "Restart" action (unchanged two-step flow).
    """
    notifier = WebNotifier()
    current = _get_current_version() or 'unknown'
    notifier.begin(current, target)

    try:
        result = apply_update(target, progress_cb=notifier.send_progress)
        if 'error' in result:
            step = result.get('failed_step', TOTAL_STEPS)
            notifier.send_failure(step, TOTAL_STEPS, result['error'])
            return

        notifier.send_success(target)
    except Exception as e:
        with _lock:
            _state['status'] = 'failed'
            _state['error'] = str(e)
        _append_log('error', f'Unexpected error: {e}')
    finally:
        _notify_listeners()


def trigger_rollback() -> dict:
    with _lock:
        if _state['status'] == 'updating':
            return {'error': 'Cannot rollback while update is in progress'}

        _state['status'] = 'updating'
        _state['step_label'] = 'Rolling back...'
        _persist_state(_state)

    _append_log('info', 'Starting rollback...')
    _notify_listeners()

    def _do_rollback():
        try:
            result = apply_rollback()
            if 'error' in result:
                with _lock:
                    _state['status'] = 'failed'
                    _state['error'] = result['error']
                _append_log('error', f"Rollback failed: {result['error']}")
            else:
                with _lock:
                    _state['status'] = 'success'
                    _state['step_label'] = 'Rollback complete'
                    _state['current_version'] = _get_current_version()
                _append_log('info', f"Rollback successful to {result['target'][:8]}")
        except Exception as e:
            with _lock:
                _state['status'] = 'failed'
                _state['error'] = str(e)
            _append_log('error', f'Rollback error: {e}')
        _notify_listeners()

    threading.Thread(target=_do_rollback, daemon=True).start()
    return {'success': True}


def trigger_restart() -> dict:
    """Re-exec the server in place via the shared restart helper.

    State is reset to idle BEFORE the restart so the persisted
    state/update/update_state.json does not carry 'success' across server
    restarts, which was causing the 'Update complete!' banner to reappear.
    """
    _append_log('info', 'Restart scheduled...')
    _notify_listeners()

    # Reset state to idle BEFORE restart so the persisted state does not
    # carry over 'success' status after the server restarts. Must happen
    # while _lock is held and before schedule_restart() fires the thread.
    with _lock:
        _state['status'] = 'idle'
        _state['progress'] = 0
        _state['step'] = 0
        _state['step_label'] = ''
        _state['error'] = None
        _state['crashed'] = False
        _persist_state(_state)

    from backend.restart import schedule_restart
    schedule_restart()
    return {'success': True, 'restarting': True}


# Start periodic listener cleanup on module import
_start_listener_cleanup()
