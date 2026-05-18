"""
Shared PinchTab HTTP API wrapper for REST API v0.12.0.

Communicates with PinchTab server directly via HTTP REST API.
Uses environment variables:
  PINCHTAB_HOST  (default: localhost)
  PINCHTAB_PORT  (default: 9867)
  PINCHTAB_TOKEN (optional, for authenticated endpoints)
"""

import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

PINCHTAB_HOST = os.environ.get("PINCHTAB_HOST", "localhost")
PINCHTAB_PORT = os.environ.get("PINCHTAB_PORT", "9867")
PINCHTAB_BASE_URL = f"http://{PINCHTAB_HOST}:{PINCHTAB_PORT}"
PINCHTAB_TOKEN = os.environ.get("PINCHTAB_TOKEN", "")

# Lazy health pre-check — cache result for 30 seconds to avoid
# hammering /health on every tool invocation in a session.
_HEALTH_OK = False
_HEALTH_TS = 0.0


def _check_health():
    """Re-check PinchTab health if cache is cold or stale (>30s)."""
    global _HEALTH_OK, _HEALTH_TS
    if time.time() - _HEALTH_TS < 30:
        return
    result = _api("GET", "/health", _skip_health=True)
    _HEALTH_OK = "error" not in result
    _HEALTH_TS = time.time()


def _api(method: str, path: str, body: dict = None, timeout: int = 30,
         _skip_health: bool = False) -> dict:
    """Call PinchTab REST API and return the parsed JSON response.

    Args:
        method: HTTP method (GET, POST, DELETE, etc.)
        path: API path starting with / (e.g. /health)
        body: Optional JSON-serializable dict for the request body
        timeout: Request timeout in seconds (default 30)

    Returns:
        Parsed JSON response dict. On any error, returns {"error": "..."}.
    """
    if not _skip_health:
        _check_health()
    url = f"{PINCHTAB_BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body else None

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    # Inject Bearer auth token if configured
    if PINCHTAB_TOKEN:
        req.add_header("Authorization", f"Bearer {PINCHTAB_TOKEN}")

    try:
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    if not raw:
                        return {}
                    return json.loads(raw)
            except urllib.error.URLError:
                if attempt == 0:
                    time.sleep(1)
                else:
                    raise
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
        except Exception:
            err_body = None
        detail = ""
        if isinstance(err_body, dict):
            detail = err_body.get("error", err_body.get("message", ""))
        elif isinstance(err_body, str):
            detail = err_body
        return {
            "error": (
                f"PinchTab HTTP {e.code} on {method} {path}"
                + (f": {detail}" if detail else "")
            )
        }
    except urllib.error.URLError as e:
        # Force a fresh health check on the next invocation so we
        # don't keep returning stale "healthy" cached results.
        global _HEALTH_TS
        _HEALTH_TS = 0.0
        return {
            "error": f"PinchTab unreachable at {PINCHTAB_BASE_URL}: {e.reason}",
            "hint": "Is PinchTab running? Start it with: pinchtab serve",
        }
    except json.JSONDecodeError:
        return {"error": f"PinchTab returned non-JSON response from {method} {path}"}
    except Exception as e:
        return {"error": f"PinchTab request failed: {type(e).__name__}: {e}"}


def _raw_get(path: str, params: dict = None, timeout: int = 30) -> bytes:
    """Call PinchTab REST API GET and return raw bytes.

    Used for endpoints that return non-JSON data (e.g. screenshots).
    """
    url = f"{PINCHTAB_BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
        if qs:
            url += "?" + qs

    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if PINCHTAB_TOKEN:
        req.add_header("Authorization", f"Bearer {PINCHTAB_TOKEN}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
        except Exception:
            err_body = None
        detail = ""
        if isinstance(err_body, dict):
            detail = err_body.get("error", err_body.get("message", ""))
        elif isinstance(err_body, str):
            detail = err_body
        raise RuntimeError(
            f"PinchTab HTTP {e.code} on GET {path}"
            + (f": {detail}" if detail else "")
        )
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"PinchTab unreachable at {PINCHTAB_BASE_URL}: {e.reason}"
        )
