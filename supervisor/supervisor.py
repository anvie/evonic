#!/usr/bin/env python3
"""
Evonic self-update supervisor — stdlib-only, no pip dependencies.

Requires: git (system), python3 (system), optionally uv (system).

Usage:
    python3 supervisor.py --config config.json          # run poll loop
    python3 supervisor.py --config config.json --once   # check once and exit
    python3 supervisor.py --config config.json --trigger  # send SIGUSR1 to running supervisor

Signals (POSIX):
    SIGUSR1 → trigger immediate update check
    SIGUSR2 → restart daemon only (no update check)
    SIGTERM → clean shutdown
"""

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Use centralized logging if available, fallback to basicConfig
try:
    from backend.logging_config import configure as configure_logging
    configure_logging(console=True)
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [supervisor] %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
log = logging.getLogger('supervisor')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# GitHub repo identifier for release API queries
GITHUB_REPO = "anvie/evonic"

SHARED_ITEMS = [
    ('db',      True),   # (name, is_directory)
    ('agents',  True),
    ('logs',    True),
    ('run',     True),
    ('kb',      True),
    ('data',    True),
    ('plugins', True),
    ('.ssh',    True),
    ('.env',    False),  # file, not directory
]

DEFAULT_CONFIG = {
    'app_root': os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'poll_interval': 300,
    'health_port': 8080,
    'health_temp_port': 18080,
    'health_timeout': 10,
    'monitor_duration': 60,
    'keep_releases': 3,
    # python_bin defaults via detect_python_bin() at load time so the install
    # venv (not the system interpreter) is preferred when supervisor runs.
    'python_bin': None,
    'uv_bin': None,
    'telegram_bot_token': '',
    'telegram_chat_id': '',
}


# ---------------------------------------------------------------------------
# Shared helpers — sourced from supervisor/_helpers.py so migrate.py can reuse
# the same detection logic without copy-paste drift.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import detect_python_bin, is_windows  # noqa: E402,F401  (re-exported for tests)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        log.warning(f'Config file not found: {config_path} — using defaults')
    except json.JSONDecodeError as e:
        log.error(f'Config parse error: {e} — using defaults')

    # Validate python_bin: prefer install venv if config value is missing or
    # points at an interpreter that no longer exists. Avoids inheriting the
    # system python that migrate.py may have captured at install time.
    py = cfg.get('python_bin')
    if not py or not os.path.exists(py):
        resolved = detect_python_bin(cfg['app_root'])
        if py and py != resolved:
            log.warning(f'python_bin={py!r} not found; using {resolved}')
        cfg['python_bin'] = resolved

    return cfg

def get_current_release(app_root: str) -> Optional[str]:
    """Return the tag name of the currently active release, or None.

    Resolution order:
    1. ``current`` symlink (Unix) / ``current.slot`` (Windows) — production
       mode.  The symlink is authoritative when the release directory
       exists **and** its ``VERSION`` agrees with the symlink target.
       ``app-root/VERSION`` is only a cache; a mismatch with the
       self-consistent symlink just means the cache is stale.
    2. ``VERSION`` file at the app root — fallback for flat-repo /
       development mode or when the symlink is stale.  The value is
       normalised to match the git tag format (``v`` prefix added if
       missing).

    If the symlink is stale (points to a release that no longer exists or
    whose version disagrees with *both* the symlink target and the
    app-root VERSION), a warning is logged and the VERSION file is used
    instead.
    """
    version_from_file: Optional[str] = None
    version_file = os.path.join(app_root, 'VERSION')
    if os.path.exists(version_file):
        with open(version_file) as f:
            ver = f.read().strip()
        if ver:
            version_from_file = f'v{ver}' if not ver.startswith('v') else ver

    def _check_symlink_tag(tag: str, release_dir: str) -> Optional[str]:
        """Return the tag if the symlink is authoritative, else None to
        indicate the VERSION-file fallback should be used.

        Trust model: when ``current`` points to a release directory that
        exists **and** whose ``VERSION`` matches the symlink tag, the
        symlink is authoritative — ``app-root/VERSION`` is just a cache
        that may be stale.  If the release VERSION disagrees with *both*
        the symlink tag and app-root VERSION, something is wrong and we
        fall back.
        """
        if not os.path.isdir(release_dir):
            log.warning(
                'current points to %s but release dir %s does not exist '
                '— falling back to VERSION file',
                tag, release_dir,
            )
            return None
        release_ver_file = os.path.join(release_dir, 'VERSION')
        if os.path.exists(release_ver_file):
            with open(release_ver_file) as f2:
                rv = f2.read().strip()
            if rv == tag:
                # Symlink and release agree — symlink is authoritative.
                # The app-root VERSION cache may be stale; that's fine.
                if version_from_file and rv != version_from_file:
                    log.warning(
                        'current symlink says %s, release VERSION confirms '
                        '%s, but app-root VERSION is stale (%s) — trusting '
                        'symlink',
                        tag, rv, version_from_file,
                    )
                return tag
            if version_from_file and rv != version_from_file:
                log.warning(
                    'current symlink says %s but release VERSION=%s differs '
                    'from app root VERSION=%s — preferring app root VERSION',
                    tag, rv, version_from_file,
                )
                return None  # fall through to VERSION file
        return tag

    if is_windows():
        slot_file = os.path.join(app_root, 'current.slot')
        if os.path.exists(slot_file):
            with open(slot_file) as f:
                tag = f.read().strip()
            if tag:
                release_dir = os.path.join(app_root, 'releases', tag)
                resolved = _check_symlink_tag(tag, release_dir)
                if resolved is not None:
                    return resolved
    else:
        link = os.path.join(app_root, 'current')
        if os.path.islink(link):
            target = os.readlink(link)
            tag = os.path.basename(target.rstrip('/'))
            release_dir = os.path.join(app_root, 'releases', tag)
            resolved = _check_symlink_tag(tag, release_dir)
            if resolved is not None:
                return resolved

    if version_from_file is not None:
        return version_from_file

    return None


def atomic_swap(app_root: str, new_release_path: str) -> None:
    """Atomically switch the 'current' pointer to new_release_path."""
    tag = os.path.basename(new_release_path.rstrip('/\\'))
    if is_windows():
        slot_tmp = os.path.join(app_root, 'current.slot.tmp')
        with open(slot_tmp, 'w') as f:
            f.write(tag)
        os.replace(slot_tmp, os.path.join(app_root, 'current.slot'))
    else:
        link_tmp = os.path.join(app_root, '.current.tmp')
        if os.path.islink(link_tmp):
            os.unlink(link_tmp)
        # Relative symlink from app_root
        rel = os.path.relpath(new_release_path, app_root)
        os.symlink(rel, link_tmp)
        os.replace(link_tmp, os.path.join(app_root, 'current'))


def read_rollback_slot(app_root: str) -> Optional[str]:
    path = os.path.join(app_root, 'rollback.slot')
    if not os.path.exists(path):
        return None
    with open(path) as f:
        tag = f.read().strip()
    return tag if tag else None


def write_rollback_slot(app_root: str, tag: str) -> None:
    tmp = os.path.join(app_root, 'rollback.slot.tmp')
    with open(tmp, 'w') as f:
        f.write(tag)
    os.replace(tmp, os.path.join(app_root, 'rollback.slot'))

# ---------------------------------------------------------------------------
# PID management
# ---------------------------------------------------------------------------

def _pid_file(app_root: str) -> str:
    return os.path.join(app_root, 'shared', 'run', 'evonic.pid')


def _log_file(app_root: str) -> str:
    return os.path.join(app_root, 'shared', 'logs', 'server.log')


def _supervisor_pid_file(app_root: str) -> str:
    sup_run = os.path.join(app_root, 'supervisor', 'run')
    os.makedirs(sup_run, exist_ok=True)
    return os.path.join(sup_run, 'supervisor.pid')


def write_supervisor_pid(app_root: str) -> None:
    with open(_supervisor_pid_file(app_root), 'w') as f:
        f.write(str(os.getpid()))


def _read_pid(pid_file: str) -> Optional[int]:
    if not os.path.exists(pid_file):
        return None
    try:
        with open(pid_file) as f:
            val = f.read().strip()
        return int(val) if val else None
    except (ValueError, IOError):
        return None


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

# ---------------------------------------------------------------------------
# Daemon management
# ---------------------------------------------------------------------------

def stop_daemon(app_root: str, timeout: int = 15) -> bool:
    """Stop the running daemon. Returns True if successfully stopped."""
    pid = _read_pid(_pid_file(app_root))
    if pid is None:
        log.info('No daemon PID found — assuming not running')
        return True
    if not _is_process_alive(pid):
        log.info(f'Daemon PID {pid} not alive — already stopped')
        _remove_daemon_pid(app_root)
        return True

    log.info(f'Sending SIGTERM to daemon PID {pid}')
    if is_windows():
        subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                       capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            _remove_daemon_pid(app_root)
            return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_process_alive(pid):
            log.info(f'Daemon PID {pid} stopped')
            _remove_daemon_pid(app_root)
            return True
        time.sleep(0.5)

    log.warning(f'Daemon did not stop after {timeout}s — sending SIGKILL')
    if is_windows():
        subprocess.run(['taskkill', '/PID', str(pid), '/F', '/T'],
                       capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(1)
    _remove_daemon_pid(app_root)
    return not _is_process_alive(pid)


def _write_daemon_pid(app_root: str, pid: int) -> None:
    """Persist the running daemon's PID so the CLI can find it.

    The CLI's ``evonic status`` and ``evonic stop`` read this file; without it
    they report the server as not running even when supervisor has a live
    daemon underneath.
    """
    pid_file = _pid_file(app_root)
    try:
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)
        with open(pid_file, 'w') as f:
            f.write(str(pid))
    except OSError as e:
        log.warning(f'Could not write daemon PID file {pid_file}: {e}')


def _remove_daemon_pid(app_root: str) -> None:
    pid_file = _pid_file(app_root)
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass  # already gone — nothing to do
    except OSError as e:
        log.warning(f'Could not remove daemon PID file {pid_file}: {e}')


def start_daemon(release_path: str, app_root: str) -> tuple:
    """Start app.py from release_path using its venv. Returns (success, pid)."""
    if is_windows():
        python = os.path.join(release_path, '.venv', 'Scripts', 'python.exe')
    else:
        python = os.path.join(release_path, '.venv', 'bin', 'python')

    if not os.path.exists(python):
        # Fallback: use system python
        python = sys.executable
        log.warning(f'Release venv not found at {release_path}/.venv — using system python')

    app_py = os.path.join(release_path, 'app.py')
    log_path = _log_file(app_root)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    log.info(f'Starting daemon: {python} {app_py}')
    with open(log_path, 'a') as lf:
        proc = subprocess.Popen(
            [python, app_py],
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=release_path,
        )

    time.sleep(2)
    if proc.poll() is not None:
        log.error(f'Daemon exited immediately with code {proc.returncode}')
        return False, proc.pid

    log.info(f'Daemon started with PID {proc.pid}')
    _write_daemon_pid(app_root, proc.pid)
    return True, proc.pid


def start_daemon_from_current(app_root: str) -> tuple:
    """Resolve current pointer and start daemon from that release.

    Re-links shared/ items first. The release worktree's ``db``, ``.env`` etc.
    may be missing or stale (manual cleanup, partial worktree, broken update);
    without re-linking, ``config.py`` would resolve to empty paths and the app
    would render the first-run setup screen on top of an existing install.
    """
    tag = get_current_release(app_root)
    if not tag:
        log.error('Cannot start daemon: no current release pointer found')
        return False, 0
    release_path = os.path.join(app_root, 'releases', tag)
    try:
        _migrate_legacy_env(app_root)
        link_shared_dirs(app_root, release_path)
    except Exception as e:
        log.warning(f'link_shared_dirs failed before start: {e}')
    return start_daemon(release_path, app_root)

# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def _git(app_root: str, args: list, capture: bool = True) -> tuple:
    """Run git command in app_root. Returns (returncode, stdout, stderr)."""
    cmd = ['git', '-C', app_root] + args
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def git_fetch_tags(app_root: str) -> tuple:
    """Fetch tags from origin. Returns (success, stderr)."""
    log.info('Fetching tags from origin')
    rc, _, err = _git(app_root, ['fetch', '--tags', 'origin'])
    if rc != 0:
        log.warning(f'git fetch failed: {err}')
    return rc == 0, err


def git_fetch_branch(app_root: str, branch: str = 'main') -> tuple:
    """Fetch a specific branch from origin (no tags). Returns (success, stderr)."""
    log.info(f'Fetching branch origin/{branch}')
    rc, _, err = _git(app_root, ['fetch', 'origin', branch])
    if rc != 0:
        log.warning(f'git fetch origin {branch} failed: {err}')
    return rc == 0, err


def get_latest_tag(app_root: str) -> Optional[str]:
    """Return the newest semver tag by version sort."""
    rc, out, _ = _git(app_root, ['tag', '-l', '--sort=-version:refname'])
    if rc != 0 or not out:
        return None
    return out.splitlines()[0].strip()


def get_latest_release(app_root: str) -> Optional[str]:
    """Return the tag name of the latest published GitHub release, or None.

    Queries the GitHub Releases API so only published releases are
    considered — dangling local tags, pre-release tags, or tags that
    aren't published on GitHub are ignored.

    Returns None on any network / API error — no fallback to local tags.
    """
    import urllib.request
    import json as _json

    url = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
    req = urllib.request.Request(url, method='GET')
    req.add_header('Accept', 'application/vnd.github.v3+json')
    req.add_header('User-Agent', 'evonic-update-checker/1.0')

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
            tag = data.get('tag_name')
            if tag:
                log.info('Latest release from GitHub API: %s', tag)
                return tag
            log.warning('GitHub release response missing tag_name: %s', data)
    except urllib.error.HTTPError as e:
        log.warning('GitHub API HTTP %d: %s — skipping update check', e.code, e.reason)
    except (urllib.error.URLError, OSError, _json.JSONDecodeError) as e:
        log.warning('GitHub API request failed: %s — skipping update check', e)

    return None


def get_tag_sha(app_root: str, tag: str) -> Optional[str]:
    """Return the commit SHA that a tag points to."""
    rc, out, _ = _git(app_root, ['rev-list', '-n', '1', tag])
    return out if rc == 0 and out else None


def verify_tag(app_root: str, tag: str) -> tuple:
    """Verify SSH/GPG signature on tag. Returns (valid, output)."""
    rc, out, err = _git(app_root, ['verify-tag', tag])
    combined = (out + '\n' + err).strip()
    if rc != 0:
        log.warning(f'Tag {tag} signature verification failed: {combined}')
    return rc == 0, combined


def create_worktree(app_root: str, tag: str) -> tuple:
    """Create a git worktree for the tag. Returns (success, stderr)."""
    release_path = os.path.join(app_root, 'releases', tag)
    if os.path.exists(release_path):
        log.info(f'Worktree already exists at {release_path}')
        return True, ''
    os.makedirs(os.path.join(app_root, 'releases'), exist_ok=True)
    rc, _, err = _git(app_root, ['worktree', 'add', release_path, tag])
    if rc != 0:
        log.error(f'git worktree add failed: {err}')
    return rc == 0, err


def create_nightly_worktree(app_root: str, branch: str = 'main') -> tuple:
    """Create a git worktree from origin/{branch}. Reuses/overwrites
    the releases/nightly directory each time. Returns (success, stderr)."""
    release_path = os.path.join(app_root, 'releases', 'nightly')
    if os.path.exists(release_path):
        log.info(f'Removing existing nightly worktree at {release_path}')
        remove_worktree(app_root, 'nightly')
    os.makedirs(os.path.join(app_root, 'releases'), exist_ok=True)
    rc, _, err = _git(app_root, ['worktree', 'add', release_path, f'origin/{branch}'])
    if rc != 0:
        log.error(f'git worktree add nightly failed: {err}')
    return rc == 0, err


def remove_worktree(app_root: str, tag: str) -> None:
    """Remove a git worktree (e.g. after a failed staging)."""
    release_path = os.path.join(app_root, 'releases', tag)
    if not os.path.exists(release_path):
        return
    rc, _, err = _git(app_root, ['worktree', 'remove', release_path, '--force'])
    if rc != 0:
        log.warning(f'git worktree remove failed for {tag}: {err}')
        # Try plain rmtree as fallback
        try:
            shutil.rmtree(release_path)
        except Exception as e:
            log.warning(f'rmtree also failed: {e}')

# ---------------------------------------------------------------------------
# Venv + deps
# ---------------------------------------------------------------------------

def create_venv_and_install(release_path: str, python_bin: str,
                             uv_bin: Optional[str]) -> tuple:
    """
    Create a venv and install requirements in release_path.
    Tries uv first, falls back to venv+pip.
    Returns (success, error_message).
    """
    venv_path = os.path.join(release_path, '.venv')
    req_file = os.path.join(release_path, 'requirements.txt')

    if not os.path.exists(req_file):
        log.warning('No requirements.txt found — skipping dep install')
        # Still create a venv so the daemon can run
        subprocess.run([python_bin, '-m', 'venv', venv_path], check=True)
        return True, ''

    # Try uv first (much faster)
    if uv_bin and shutil.which(uv_bin):
        log.info(f'Creating venv with uv at {venv_path}')
        r1 = subprocess.run([uv_bin, 'venv', venv_path], capture_output=True, text=True)
        if r1.returncode == 0:
            log.info('Installing dependencies with uv pip')
            if is_windows():
                pip_in_venv = os.path.join(venv_path, 'Scripts', 'pip')
            else:
                pip_in_venv = os.path.join(venv_path, 'bin', 'pip')
            r2 = subprocess.run(
                [uv_bin, 'pip', 'install', '--python', pip_in_venv,
                 '-r', req_file],
                capture_output=True, text=True,
            )
            if r2.returncode == 0:
                return True, ''
            err = r2.stderr
            log.warning(f'uv pip install failed: {err}')
        else:
            err = r1.stderr
            log.warning(f'uv venv failed: {err}')

    # Fallback: stdlib venv + pip
    log.info(f'Creating venv with {python_bin}')
    r = subprocess.run([python_bin, '-m', 'venv', venv_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, f'venv creation failed: {r.stderr}'

    if is_windows():
        pip_exec = os.path.join(venv_path, 'Scripts', 'pip')
    else:
        pip_exec = os.path.join(venv_path, 'bin', 'pip')

    log.info('Installing dependencies with pip')
    r = subprocess.run(
        [pip_exec, 'install', '-r', req_file],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, f'pip install failed: {r.stderr}'
    return True, ''

# ---------------------------------------------------------------------------
# Shared directory symlinking
# ---------------------------------------------------------------------------

def _migrate_legacy_env(app_root: str) -> None:
    """Copy legacy ~/.evonic/.env (v0.2.x) to shared/.env if it's missing.

    On v0.2.x the .env file lived at the app root (~/.evonic/.env).  v0.3.x
    moved it to shared/.env which is symlinked into each release directory by
    ``link_shared_dirs``.  If a legacy .env exists but shared/.env does not,
    the symlink is never created and the daemon starts with no configuration
    (admin login breaks).  This function bridges that gap by copying the
    legacy file into shared/ so the existing symlink machinery picks it up.

    The legacy file is *preserved* — only copied, never deleted.
    On clean installs there is no legacy .env so this is a no-op.
    """
    legacy_env = os.path.join(app_root, '.env')
    shared_env = os.path.join(app_root, 'shared', '.env')

    if os.path.isfile(legacy_env) and not os.path.exists(shared_env):
        log.info('Migrating legacy .env to shared/.env')
        os.makedirs(os.path.dirname(shared_env), exist_ok=True)
        shutil.copy2(legacy_env, shared_env)

def link_shared_dirs(app_root: str, release_path: str) -> None:
    """Symlink shared/ items into the release directory.

    Idempotent: a link that already resolves to the correct shared target is
    left untouched.

    When a real (non-symlink) directory sits at the link path, the behaviour
    depends on whether the shared target exists:

    * If the shared target **exists**, the real directory is almost certainly
      a git-tracked directory created by ``git worktree add`` (e.g.
      ``plugins/``) and does **not** hold user data — it is removed and
      replaced with a symlink to ``shared/``.
    * If the shared target does **not** exist, the real directory is
      preserved — it may hold user data that hasn't been migrated to
      ``shared/`` yet.
    """
    shared_root = os.path.join(app_root, 'shared')

    for name, is_dir in SHARED_ITEMS:
        target = os.path.join(shared_root, name)
        link = os.path.join(release_path, name)

        # Only link if shared target actually exists
        if not os.path.exists(target):
            log.debug(f'Shared item not found, skipping: {target}')
            continue

        if os.path.islink(link):
            try:
                if os.path.realpath(link) == os.path.realpath(target):
                    continue  # already correctly linked
            except OSError:
                pass
            os.unlink(link)
        elif os.path.isdir(link):
            # Real directory at link path while shared/ target exists.
            # This happens when git tracks a directory that is also a
            # shared item (e.g. plugins/) — git worktree add checks it
            # out as a real directory, blocking the symlink.  Since the
            # shared/ target exists (checked above), the real directory
            # is stale git content, not user data.  Remove it.
            log.info(
                f'Removing git-tracked directory {link} '
                f'to create shared symlink to {target}'
            )
            shutil.rmtree(link)
        elif os.path.exists(link):
            os.unlink(link)

        if is_windows() and is_dir:
            # Try junction first (no admin required on Windows)
            r = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', link, target],
                capture_output=True,
            )
            if r.returncode != 0:
                log.warning(f'mklink /J failed for {name}: {r.stderr}')
                # Last resort: copy (breaks atomicity for this dir)
                shutil.copytree(target, link)
        else:
            # Use relative symlink so it works regardless of absolute project path
            link_dir = os.path.dirname(link)
            rel_target = os.path.relpath(target, link_dir)
            os.symlink(rel_target, link, target_is_directory=is_dir)

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def health_check(port: int, timeout: int = 10) -> bool:
    """GET /api/health on localhost:{port}. Returns True on 200 + {"status":"ok"}."""
    url = f'http://127.0.0.1:{port}/api/health'
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode())
            return body.get('status') == 'ok'
    except Exception:
        return False


def health_check_temp_port(release_path: str, app_root: str, port: int,
                            timeout: int = 30, python_bin: str = sys.executable) -> bool:
    """
    Start the release on a temporary port, probe /api/health, then kill it.
    Returns True if health check passes.
    """
    if is_windows():
        python = os.path.join(release_path, '.venv', 'Scripts', 'python.exe')
    else:
        python = os.path.join(release_path, '.venv', 'bin', 'python')
    if not os.path.exists(python):
        python = python_bin

    env = os.environ.copy()
    env['PORT'] = str(port)
    env['DEBUG'] = '0'

    log.info(f'Starting release on temp port {port} for health check')
    proc = subprocess.Popen(
        [python, os.path.join(release_path, 'app.py')],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=release_path,
    )

    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            if proc.poll() is not None:
                log.warning('Staged release process exited early during health check')
                return False
            if health_check(port, timeout=5):
                log.info('Staged release passed health check')
                return True
        log.warning(f'Health check timed out after {timeout}s on port {port}')
        return False
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

# ---------------------------------------------------------------------------
# Telegram notifier
# ---------------------------------------------------------------------------

FILLED = '\u2588'
EMPTY = '\u2591'
BAR_WIDTH = 16


def _progress_bar(step: int, total: int) -> str:
    pct = int(step / total * 100)
    filled = int(BAR_WIDTH * step / total)
    bar = FILLED * filled + EMPTY * (BAR_WIDTH - filled)
    return f'{bar} {pct}%'


class TelegramNotifier:
    """Stdlib-only Telegram Bot API client."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.message_id: Optional[int] = None
        self._base = f'https://api.telegram.org/bot{bot_token}'
        self._from_tag: Optional[str] = None
        self._to_tag: Optional[str] = None
        self._start_time: Optional[str] = None

    def _api_call(self, method: str, payload: dict) -> dict:
        url = f'{self._base}/{method}'
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            log.warning(f'Telegram API error ({method}): {e}')
            return {}

    def _format_progress(self, step: int, total: int, description: str) -> str:
        header = f'[Update {self._from_tag} \u2192 {self._to_tag}]'
        bar = _progress_bar(step, total)
        started = f'Started: {self._start_time}'
        return f'{header}\n{bar} \u2014 {description}\n{started}'

    def begin(self, from_tag: str, to_tag: str) -> None:
        self._from_tag = from_tag
        self._to_tag = to_tag
        self._start_time = datetime.now().strftime('%H:%M:%S')
        self.message_id = None

    def send_progress(self, step: int, total: int, description: str) -> None:
        if not self.bot_token or not self.chat_id:
            return
        text = self._format_progress(step, total, description)
        if self.message_id is None:
            result = self._api_call('sendMessage', {
                'chat_id': self.chat_id,
                'text': text,
            })
            self.message_id = (result.get('result') or {}).get('message_id')
        else:
            self._api_call('editMessageText', {
                'chat_id': self.chat_id,
                'message_id': self.message_id,
                'text': text,
            })

    def send_failure(self, step: int, total: int, error: str) -> None:
        if not self.bot_token or not self.chat_id:
            return
        rollback_tag = self._from_tag or '?'
        text = (
            f'\u274c Update {self._to_tag} FAILED at step {step}/{total}\n'
            f'Rolled back to {rollback_tag}\n'
            f'Error: {error}'
        )
        # Always send a NEW message so failures stay visible
        self._api_call('sendMessage', {'chat_id': self.chat_id, 'text': text})

    def send_success(self, tag: str) -> None:
        if not self.bot_token or not self.chat_id:
            return
        text = (
            f'\u2705 Update to {tag} complete\n'
            f'{_progress_bar(6, 6)} \u2014 Done\n'
            f'Started: {self._start_time}'
        )
        if self.message_id:
            self._api_call('editMessageText', {
                'chat_id': self.chat_id,
                'message_id': self.message_id,
                'text': text,
            })
        else:
            self._api_call('sendMessage', {'chat_id': self.chat_id, 'text': text})

# ---------------------------------------------------------------------------
# Update error
# ---------------------------------------------------------------------------

class UpdateError(Exception):
    pass

# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def rollback(app_root: str, cfg: dict, notifier: Optional[TelegramNotifier]) -> bool:
    """Swap back to rollback.slot release and restart the daemon."""
    old_tag = read_rollback_slot(app_root)
    if not old_tag:
        log.error('No rollback slot found — cannot rollback')
        return False

    old_path = os.path.join(app_root, 'releases', old_tag)
    if not os.path.exists(old_path):
        log.error(f'Rollback release {old_tag} does not exist at {old_path}')
        return False

    log.info(f'Rolling back to {old_tag}')
    try:
        stop_daemon(app_root)
        atomic_swap(app_root, old_path)
        # Sync app-root/VERSION so next restart picks up the rollback release
        with open(os.path.join(old_path, 'VERSION')) as f:
            rollback_version = f.read().strip()
        with open(os.path.join(app_root, 'VERSION'), 'w') as f:
            f.write(rollback_version)
        ok, _ = start_daemon(old_path, app_root)
        if ok:
            log.info(f'Rollback to {old_tag} successful')
        else:
            log.error(f'Rollback daemon start failed')
        return ok
    except Exception as e:
        log.error(f'Rollback failed: {e}')
        return False

# ---------------------------------------------------------------------------
# Cleanup old releases
# ---------------------------------------------------------------------------

def cleanup_old_releases(app_root: str, keep: int = 3) -> None:
    """Remove releases beyond the most recent `keep`, skipping current + rollback."""
    releases_dir = os.path.join(app_root, 'releases')
    if not os.path.isdir(releases_dir):
        return

    current_tag = get_current_release(app_root)
    rollback_tag = read_rollback_slot(app_root)
    protected = {t for t in [current_tag, rollback_tag] if t}

    tags = sorted(
        [d for d in os.listdir(releases_dir)
         if os.path.isdir(os.path.join(releases_dir, d))],
        reverse=True,  # newest first (lexicographic semver with leading v works)
    )

    to_keep = set()
    count = 0
    for tag in tags:
        if tag in protected or count < keep:
            to_keep.add(tag)
            if tag not in protected:
                count += 1

    for tag in tags:
        if tag not in to_keep:
            log.info(f'Removing old release: {tag}')
            remove_worktree(app_root, tag)

# ---------------------------------------------------------------------------
# 6-step update lifecycle
# ---------------------------------------------------------------------------

STEPS = [
    (1, 'Fetching tags'),
    (2, 'Verifying signature'),
    (3, 'Staging release'),
    (4, 'Health check (staged)'),
    (5, 'Swapping active release'),
    (6, 'Restarting & monitoring'),
]
TOTAL_STEPS = len(STEPS)


def _notify(notifier: Optional[TelegramNotifier], step: int, description: str) -> None:
    log.info(f'Step {step}/{TOTAL_STEPS}: {description}')
    if notifier:
        notifier.send_progress(step, TOTAL_STEPS, description)


def _resolve_port_from_env(release_path: str, fallback: int = 8080) -> int:
    """Read PORT from the release's .env file. Falls back to the given value."""
    env_path = os.path.join(release_path, '.env')
    if os.path.isfile(env_path):
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('PORT=') or line.startswith('PORT '):
                        val = line.split('=', 1)[-1].strip().strip('"').strip("'")
                        if val:
                            return int(val)
        except (ValueError, IOError):
            pass
    return fallback


def preflight_checks(app_root: str, tag: str, cfg: dict, nightly: bool = False) -> tuple[bool, list[str]]:
    """Run pre-flight checks before starting update.
    
    Returns (success, warnings) where warnings is a list of non-fatal issues.
    Raises UpdateError for fatal issues that prevent update.
    """
    warnings = []
    
    # Check 1: Disk space (require at least 500MB free)
    try:
        stat = os.statvfs(app_root) if hasattr(os, 'statvfs') else None
        if stat:
            free_bytes = stat.f_bavail * stat.f_frsize
            free_mb = free_bytes / (1024 * 1024)
            if free_mb < 500:
                raise UpdateError(
                    f'Insufficient disk space: {free_mb:.0f}MB free, need at least 500MB'
                )
            elif free_mb < 1000:
                warnings.append(f'Low disk space: {free_mb:.0f}MB free (recommended: 1GB+)')
    except AttributeError:
        # Windows doesn't have statvfs, use shutil.disk_usage
        try:
            usage = shutil.disk_usage(app_root)
            free_mb = usage.free / (1024 * 1024)
            if free_mb < 500:
                raise UpdateError(
                    f'Insufficient disk space: {free_mb:.0f}MB free, need at least 500MB'
                )
            elif free_mb < 1000:
                warnings.append(f'Low disk space: {free_mb:.0f}MB free (recommended: 1GB+)')
        except Exception:
            warnings.append('Could not check disk space')
    
    # Check 2: Git repository health
    if not nightly:
        rc, out, err = _git(app_root, ['rev-parse', '--verify', f'refs/tags/{tag}'])
        if rc != 0:
            raise UpdateError(f'Tag {tag} not found in repository. Run git fetch first.')
    else:
        rc, out, err = _git(app_root, ['rev-parse', '--verify', f'origin/{tag}'])
        if rc != 0:
            raise UpdateError(f'Branch {tag} not found. Run git fetch first.')
    
    # Check 3: Git working directory clean (warn only)
    rc, out, err = _git(app_root, ['status', '--porcelain'])
    if rc == 0 and out.strip():
        warnings.append('Git working directory has uncommitted changes')
    
    # Check 4: Network connectivity (for dependency installation)
    # Try to resolve a common package index
    try:
        import socket
        socket.create_connection(('pypi.org', 443), timeout=5).close()
    except (socket.error, socket.timeout):
        warnings.append('Network connectivity issue detected - dependency installation may fail')
    except Exception:
        pass  # Other errors are non-fatal
    
    # Check 5: Current release exists and is healthy
    current_tag = get_current_release(app_root)
    if current_tag:
        current_path = os.path.join(app_root, 'releases', current_tag)
        if not os.path.isdir(current_path):
            warnings.append(f'Current release directory not found: {current_tag}')
    
    # Check 6: Python binary exists and is executable
    python_bin = cfg.get('python_bin')
    if python_bin and not os.path.isfile(python_bin):
        raise UpdateError(f'Python binary not found: {python_bin}')
    
    return True, warnings


def run_update(tag: str, cfg: dict, notifier: Optional[TelegramNotifier],
               skip_verify: bool = False, nightly: bool = False) -> bool:
    """
    Execute the 6-step update lifecycle for the given tag.

    When ``nightly=True``, ``tag`` is the branch name (e.g. "main") and the
    release is staged at ``releases/nightly`` from ``origin/{branch}``.

    Returns True on success, False on failure (rollback attempted automatically).
    """
    app_root = cfg['app_root']

    if nightly:
        release_path = os.path.join(app_root, 'releases', 'nightly')
        display_tag = f'nightly (origin/{tag})'
        cleanup_tag = 'nightly'
    else:
        release_path = os.path.join(app_root, 'releases', tag)
        display_tag = tag
        cleanup_tag = tag

    current_tag = get_current_release(app_root)

    if notifier:
        notifier.begin(current_tag or '?', display_tag)

    step = 0
    try:
        # Step 0: Pre-flight checks
        log.info('Running pre-flight checks...')
        try:
            ok, warnings = preflight_checks(app_root, tag, cfg, nightly)
            if warnings:
                for warning in warnings:
                    log.warning(f'Pre-flight warning: {warning}')
                    if notifier:
                        notifier.send_progress(0, TOTAL_STEPS, f'Warning: {warning}')
            log.info('Pre-flight checks passed')
        except UpdateError as e:
            log.error(f'Pre-flight check failed: {e}')
            if notifier:
                notifier.send_failure(0, TOTAL_STEPS, str(e))
            return False
        
        # Step 1: Fetch (already done in poll loop or by caller; log it)
        step = 1
        if nightly:
            _notify(notifier, step, f'Fetched — nightly from origin/{tag}')
        else:
            _notify(notifier, step, f'Fetched — new tag: {tag}')

        # Step 2: Verify signature
        step = 2
        if nightly:
            _notify(notifier, step, 'Skipping signature verification (nightly)')
            log.warning('Signature verification SKIPPED (nightly)')
        else:
            # TODO: Re-enable signature verification for production
            # Currently disabled for development convenience
            _notify(notifier, step, 'Skipping signature verification (dev mode)')
            if False:  # was: if not skip_verify:
                ok, out = verify_tag(app_root, tag)
                if not ok:
                    raise UpdateError(f'Signature verification failed: {out}')
            else:
                log.warning('Signature verification SKIPPED (dev mode)')

        # Step 3: Stage (worktree + venv + shared links)
        step = 3
        _notify(notifier, step, 'Creating worktree & installing dependencies')
        if nightly:
            ok, err = create_nightly_worktree(app_root, tag)
        else:
            ok, err = create_worktree(app_root, tag)
        if not ok:
            raise UpdateError(f'git worktree add failed: {err}')

        # Write VERSION file
        with open(os.path.join(release_path, 'VERSION'), 'w') as f:
            if nightly:
                rc, sha, _ = _git(app_root, ['rev-parse', '--short', f'origin/{tag}'])
                f.write(f'nightly-{sha}' if rc == 0 else 'nightly')
            else:
                f.write(tag)

        ok, err = create_venv_and_install(
            release_path, cfg['python_bin'], cfg.get('uv_bin'))
        if not ok:
            raise UpdateError(f'Dependency installation failed: {err}')

        _migrate_legacy_env(app_root)
        link_shared_dirs(app_root, release_path)

        # Step 4: Health check on temp port
        step = 4
        _notify(notifier, step, f'Health check on port {cfg["health_temp_port"]}')
        ok = health_check_temp_port(
            release_path, app_root,
            port=cfg['health_temp_port'],
            timeout=cfg['health_timeout'] * 3,
            python_bin=cfg['python_bin'],
        )
        if not ok:
            raise UpdateError('Staged release failed health check on temp port')

        # Step 5: Stop daemon + atomic swap
        step = 5
        _notify(notifier, step, 'Stopping daemon & swapping release pointer')
        stop_daemon(app_root)
        if current_tag:
            write_rollback_slot(app_root, current_tag)
        atomic_swap(app_root, release_path)

        # Sync app-root/VERSION so the fallback on next restart is fresh.
        # Without this, get_current_release() may prefer the stale
        # app-root/VERSION over the correct symlink (see _check_symlink_tag).
        with open(os.path.join(release_path, 'VERSION')) as f:
            release_version = f.read().strip()
        with open(os.path.join(app_root, 'VERSION'), 'w') as f:
            f.write(release_version)

        # Step 6: Restart + monitor
        step = 6
        _notify(notifier, step, 'Starting new release & monitoring')
        ok, pid = start_daemon(release_path, app_root)
        if not ok:
            raise UpdateError('Failed to start daemon from new release')

        # Resolve the actual port from the release's .env so health checks
        # work on non-default ports (e.g. when PORT != 8080).
        health_port = _resolve_port_from_env(release_path, fallback=cfg['health_port'])
        log.info(f'Health check will probe port {health_port}')

        # Monitor for monitor_duration seconds
        end_time = time.time() + cfg['monitor_duration']
        check_interval = 5
        while time.time() < end_time:
            time.sleep(check_interval)
            if not _is_process_alive(pid):
                raise UpdateError('Daemon process died during monitoring period')
            if not health_check(health_port, timeout=cfg['health_timeout']):
                raise UpdateError('Health check failed during monitoring period')

        if notifier:
            notifier.send_success(display_tag)
        log.info(f'Update to {display_tag} successful')
        return True

    except UpdateError as e:
        log.error(f'Update to {display_tag} failed at step {step}/{TOTAL_STEPS}: {e}')
        if notifier:
            notifier.send_failure(step, TOTAL_STEPS, str(e))
        # Rollback
        rollback(app_root, cfg, notifier)
        # Clean up bad release worktree
        if os.path.exists(release_path):
            remove_worktree(app_root, cleanup_tag)
        return False
    except Exception as e:
        log.error(f'Unexpected error at step {step}: {e}', exc_info=True)
        if notifier:
            notifier.send_failure(step, TOTAL_STEPS, f'Unexpected error: {e}')
        rollback(app_root, cfg, notifier)
        if os.path.exists(release_path):
            remove_worktree(app_root, cleanup_tag)
        return False

# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Evonic self-update supervisor')
    parser.add_argument('--config', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'config.json'),
        help='Path to supervisor config.json')
    parser.add_argument('--once', action='store_true',
        help='Check for updates once and exit')
    parser.add_argument('--trigger', action='store_true',
        help='Send SIGUSR1 to the running supervisor and exit')
    parser.add_argument('--force', action='store_true',
        help='Skip signature verification (development only)')
    parser.add_argument('--tag', default=None,
        help='Update to this specific tag instead of latest')
    args = parser.parse_args()

    cfg = load_config(args.config)
    app_root = cfg['app_root']

    # --trigger: signal running supervisor and exit
    if args.trigger:
        pid = _read_pid(_supervisor_pid_file(app_root))
        if pid and _is_process_alive(pid):
            os.kill(pid, signal.SIGUSR1)
            print(f'Sent SIGUSR1 to supervisor (PID {pid})')
        else:
            print('No running supervisor found')
        return

    notifier = None
    token = cfg.get('telegram_bot_token', '')
    chat_id = cfg.get('telegram_chat_id', '')
    if token and chat_id:
        notifier = TelegramNotifier(token, str(chat_id))

    write_supervisor_pid(app_root)
    log.info(f'Supervisor started (PID {os.getpid()}, app_root={app_root})')

    # Start daemon from current release immediately
    if not args.once:
        ok, pid = start_daemon_from_current(app_root)
        if ok:
            log.info(f'Daemon started (PID {pid}) from current release')
        else:
            log.warning('Could not start daemon from current release')

    # Event flags for signal handlers
    trigger_event = threading.Event()
    restart_event = threading.Event()
    shutdown_event = threading.Event()

    if not is_windows():
        def _sigusr1(signum, frame):
            log.info('SIGUSR1 received — triggering immediate update check')
            trigger_event.set()

        def _sigusr2(signum, frame):
            log.info('SIGUSR2 received — restart daemon only')
            restart_event.set()

        def _sigterm(signum, frame):
            log.info('SIGTERM received — shutting down')
            shutdown_event.set()

        signal.signal(signal.SIGUSR1, _sigusr1)
        signal.signal(signal.SIGUSR2, _sigusr2)
        signal.signal(signal.SIGTERM, _sigterm)

    last_poll = 0.0

    while not shutdown_event.is_set():
        now = time.time()
        should_check = (
            trigger_event.is_set()
            or (now - last_poll) >= cfg['poll_interval']
        )

        if should_check:
            trigger_event.clear()
            last_poll = now

            git_fetch_tags(app_root)
            target_tag = args.tag or get_latest_tag(app_root)
            current_tag = get_current_release(app_root)

            if target_tag and target_tag != current_tag:
                log.info(f'New release available: {current_tag} → {target_tag}')
                success = run_update(target_tag, cfg, notifier,
                                     skip_verify=args.force)
                if success:
                    cleanup_old_releases(app_root, cfg['keep_releases'])
            else:
                log.info(f'Already up to date ({current_tag})')

            if args.once:
                break

        if restart_event.is_set():
            restart_event.clear()
            log.info('Restarting daemon (no update)')
            stop_daemon(app_root)
            start_daemon_from_current(app_root)

        shutdown_event.wait(timeout=10)

    log.info('Supervisor exiting')


if __name__ == '__main__':
    main()
