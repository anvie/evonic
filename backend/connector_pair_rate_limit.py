"""
Per-IP rate limiting for unauthenticated Evonet connector pairing attempts.
"""

import threading
import time
from typing import Dict, List

import config

_attempts: Dict[str, List[float]] = {}
_lock = threading.Lock()


def _max_attempts() -> int:
    return getattr(config, 'CONNECTOR_PAIR_MAX_ATTEMPTS', 30)


def _window_seconds() -> int:
    return getattr(config, 'CONNECTOR_PAIRING_CODE_TTL', 300)


def is_rate_limited(ip: str) -> bool:
    """Return True if this IP has exceeded pairing attempt limits."""
    window = _window_seconds()
    now = time.monotonic()
    with _lock:
        timestamps = [t for t in _attempts.get(ip, []) if now - t < window]
        _attempts[ip] = timestamps
        return len(timestamps) >= _max_attempts()


def record_attempt(ip: str) -> None:
    now = time.monotonic()
    with _lock:
        _attempts.setdefault(ip, []).append(now)


def reset_for_tests() -> None:
    """Clear in-memory state (unit tests only)."""
    with _lock:
        _attempts.clear()
