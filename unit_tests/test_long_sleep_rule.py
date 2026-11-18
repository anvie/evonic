"""HMADS rule: bash `sleep` longer than 10 seconds is rejected in favour of `monitor`."""
import pytest

from backend.tools.lib.heuristic_safety import check_safety


@pytest.mark.parametrize("script", [
    "sleep 11", "sleep 30", "sleep 100", "sleep 10.5", "sleep 2m", "sleep 1h",
    "sleep infinity", "make && sleep 60; ls",
])
def test_long_sleep_is_blocked(script):
    r = check_safety(script, tool_type="bash")
    assert r["level"] == "dangerous"
    assert "long_sleep" in r["blocked_patterns"]
    assert any("monitor" in reason for reason in r["reasons"])


@pytest.mark.parametrize("script", ["sleep 1", "sleep 0.5", "sleep 9.9", "sleep 10", "sleep 10s", "usleep 99"])
def test_short_sleep_is_allowed(script):
    assert check_safety(script, tool_type="bash")["level"] == "safe"
