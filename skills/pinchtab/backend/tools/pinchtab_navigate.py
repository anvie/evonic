"""pinchtab_navigate — navigate a browser tab to a URL."""

from ._pinchtab_api import _api


# Blocklist for dangerous/internal URLs
_BLOCKED_PREFIXES = (
    "file://",
    "chrome://",
    "chrome-extension://",
    "about:",
    "javascript:",
    "data:",
)

_BLOCKED_HOSTS = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "[::1]",
)


def _is_url_safe(url: str) -> tuple:
    """Check if a URL passes basic safety checks.

    Returns (is_safe, reason).
    """
    lower = url.lower().strip()

    # Check dangerous schemes
    for prefix in _BLOCKED_PREFIXES:
        if lower.startswith(prefix):
            return False, f"URL scheme '{prefix}' is blocked for safety."

    # Check internal hosts (basic check — won't catch all SSRF but catches the obvious)
    for host in _BLOCKED_HOSTS:
        if host in lower:
            return False, f"Navigation to '{host}' is blocked for safety."

    return True, ""


def execute(agent: dict, args: dict) -> dict:
    """Navigate a browser tab to a URL.

    Args:
        tab_id: ID of the tab to navigate.
        url: The URL to navigate to.

    Returns:
        Navigation result from PinchTab.
    """
    tab_id = args.get("tab_id", "")
    url = args.get("url", "")

    if not tab_id:
        return {"error": "tab_id is required."}
    if not url:
        return {"error": "url is required."}

    # Safety check
    safe, reason = _is_url_safe(url)
    if not safe:
        return {"error": reason}

    result = _api("POST", "/navigate", {"tabId": tab_id, "url": url})
    if "error" in result:
        return result
    return {
        "message": f"Navigated to {url}.",
        "result": result,
    }
