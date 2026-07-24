"""
Shared HTTP client for the Cloudflare Agentic Inbox REST API.
All tools delegate HTTP calls through this module to centralize
auth, error handling, and response parsing.
"""

import json
import urllib.request
import urllib.error
from typing import Any, Optional


class InboxClient:
    """Thin HTTP wrapper for the Agentic Inbox REST API."""

    def __init__(self, worker_url: str, access_token: str):
        if not worker_url:
            raise ValueError("WORKER_URL is not configured. Ask the admin to set it in the agentic_inbox skill settings.")
        if not access_token:
            raise ValueError("ACCESS_TOKEN is not configured. Ask the admin to set it in the agentic_inbox skill settings.")
        self._base = worker_url.rstrip("/")
        self._token = access_token

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """Send an HTTP request and return the parsed JSON response.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.).
            path: API path (e.g. '/api/v1/mailboxes').
            body: Optional JSON body for POST/PUT requests.

        Returns:
            Parsed JSON response as a dict.

        Raises:
            ConnectionError: On network/HTTP errors (wrapped into a dict for tool compatibility).
        """
        url = f"{self._base}{path}"
        data = None
        headers = {
            "cf-access-jwt-assertion": self._token,
            "Accept": "application/json",
        }

        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return {"status": "success", "data": None}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8").strip()
            except Exception:
                detail = str(e)
            return {
                "status": "error",
                "message": f"HTTP {e.code}: {detail if detail else e.reason}",
                "code": e.code,
            }
        except urllib.error.URLError as e:
            return {
                "status": "error",
                "message": f"Connection error: {e.reason}",
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Unexpected error: {str(e)}",
            }


def get_client(agent: dict, args: dict) -> InboxClient:
    """Factory that reads WORKER_URL and ACCESS_TOKEN from plugin settings.

    In Evonic skill backends, plugin settings are injected into the agent dict.
    """
    settings = agent.get("plugin_settings", agent.get("settings", {}))
    worker_url = settings.get("WORKER_URL", "")
    access_token = settings.get("ACCESS_TOKEN", "")
    return InboxClient(worker_url, access_token)
