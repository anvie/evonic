"""Expired login-attempt entries must not accumulate per IP."""
from routes import auth


def test_expired_entries_are_dropped(monkeypatch):
    auth._login_attempts.clear()
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])

    auth._record_failed_attempt("1.2.3.4")
    assert auth._is_rate_limited("1.2.3.4") is False
    assert "1.2.3.4" in auth._login_attempts

    clock[0] += auth._WINDOW_SECONDS + 1
    assert auth._is_rate_limited("1.2.3.4") is False
    assert "1.2.3.4" not in auth._login_attempts


def test_unknown_ip_is_not_stored():
    auth._login_attempts.clear()
    assert auth._is_rate_limited("9.9.9.9") is False
    assert auth._login_attempts == {}


def test_still_limits_after_max_attempts():
    auth._login_attempts.clear()
    for _ in range(auth._MAX_ATTEMPTS):
        auth._record_failed_attempt("5.5.5.5")
    assert auth._is_rate_limited("5.5.5.5") is True
    auth._login_attempts.clear()
