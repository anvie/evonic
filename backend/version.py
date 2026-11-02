"""Determine the version string using git describe --tags, falling back to VERSION file."""
import os
import re
import subprocess

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VERSION_PATH = os.path.join(_PROJECT_ROOT, "VERSION")


def _git_describe() -> str:
    """Run git describe --tags --always and return stripped output.

    Returns empty string if git is unavailable or the command fails.
    """
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5,
            cwd=_PROJECT_ROOT,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _version_key(ver: str) -> tuple:
    """Parse version string into (major, minor, patch) tuple."""
    m = re.match(r'v?(\d+)(?:\.(\d+))?(?:\.(\d+))?', ver.lstrip('v'))
    if not m:
        return (0, 0, 0)
    return tuple(int(x or '0') for x in m.groups())


_MINIMUM_RE = re.compile(r'^v?\d+(\.\d+){0,2}$')


def minimum_version_error(minimum, what: str = 'This plugin', current: str = None) -> str:
    """Why something that needs Evonic ``minimum`` or newer cannot run on this Evonic, or '' when it can (or needs
    nothing). The commit offset a dev build adds (``1.6.0-23``) does not count: it is newer than ``1.6.0``."""
    wanted = str(minimum or '').strip()
    if not wanted:
        return ''
    if not _MINIMUM_RE.match(wanted):
        return f'{what} declares an invalid min_evonic_version "{wanted}" (expected something like "1.6.0").'
    running = current if current is not None else get_version()
    if _version_key(running) >= _version_key(wanted):
        return ''
    return f'{what} needs Evonic {wanted} or newer, but this is Evonic {running}. Update Evonic to use it.'


def _current_branch() -> str:
    """Return the active git branch name, or empty string if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=_PROJECT_ROOT,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _commits_since_version_bump() -> int:
    """Count commits made after the VERSION file was last bumped.

    The bump_version.sh workflow commits the VERSION change as the anchor for a
    release, so commits since that commit reflect how far ahead of the release
    the current HEAD is. Returns 0 if git is unavailable or counting fails.
    """
    try:
        anchor = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", _VERSION_PATH],
            capture_output=True, text=True, timeout=5,
            cwd=_PROJECT_ROOT,
        )
        commit = anchor.stdout.strip()
        if anchor.returncode != 0 or not commit:
            return 0
        count = subprocess.run(
            ["git", "rev-list", "--count", f"{commit}..HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=_PROJECT_ROOT,
        )
        if count.returncode == 0:
            return int(count.stdout.strip() or 0)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return 0


def _base_version() -> str:
    """Return the clean base version string (e.g. '0.8.7').

    Uses git describe --tags for the reachable release tag, but prefers the
    VERSION file when it indicates a newer release than that tag (handles
    diverged branches where the latest tag is on another branch).
    Falls back to the VERSION file when git is unavailable.
    """
    raw = _git_describe()
    if raw:
        # Strip leading 'v' — the template prepends it (v{{ evonic_version }})
        if raw.startswith("v"):
            raw = raw[1:]
        # Keep only the tag part of 'tag-<n>-g<hash>' for a clean X.Y.Z base
        tag = raw.split("-")[0]

        # If VERSION file has a higher version than the git tag, prefer it
        if os.path.exists(_VERSION_PATH):
            try:
                with open(_VERSION_PATH) as f:
                    file_ver = f.read().strip()
                if file_ver and _version_key(file_ver) > _version_key(tag):
                    return file_ver
            except (IOError, OSError):
                pass

        return tag

    # Fallback: read VERSION file
    if os.path.exists(_VERSION_PATH):
        with open(_VERSION_PATH) as f:
            return f.read().strip()
    return "?.?.?"


def get_version() -> str:
    """Return the version string for display (e.g. '0.8.7' or '0.8.7-28').

    On the 'dev' branch the commit offset since the last VERSION bump is
    appended (e.g. '0.8.7-28'). On 'main' and every other branch the clean
    base version is returned.
    """
    base = _base_version()
    # Suffix the commit offset only on dev; main and others stay clean.
    if _current_branch() == "dev":
        n = _commits_since_version_bump()
        if n > 0:
            return f"{base}-{n}"
    return base
