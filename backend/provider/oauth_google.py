"""
OAuth 2.0 refresh-token flow for Google Cloud Application Default Credentials (ADC).

Keeps a "custom" provider pointed at Vertex AI's OpenAI-compatible endpoint
authenticated without the user manually pasting a fresh access token every
~1 hour. Mirrors backend/provider/oauth_codex.py's refresh pattern.

CLIENT_ID/CLIENT_SECRET below are Google's well-known public "gcloud" installed-app
OAuth client — the same client every `gcloud auth application-default login`
refresh token is issued under (visible in every user's own
~/.config/gcloud/application_default_credentials.json). Not a per-project secret.

Token persistence via the providers table (auth_type='google_oauth').
"""

import logging
import time
from typing import Dict, Optional

import requests

_log = logging.getLogger(__name__)

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

CLIENT_ID = "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com"
CLIENT_SECRET = "d-FL95Q19q7MQmFpd7hHD0Ty"

# Refresh this many seconds before actual expiry, so a slow request never
# starts mid-flight with a token that dies before the response comes back.
_REFRESH_MARGIN_SECONDS = 300


def refresh_access_token(refresh_token: str) -> Dict:
    """Use a refresh token to get a new short-lived access token."""
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=15,
    )

    if resp.status_code != 200:
        return {"error": f"Token refresh failed (HTTP {resp.status_code}): {resp.text[:200]}"}

    data = resp.json()
    if "access_token" not in data:
        return {"error": "No access_token in refresh response"}

    return {
        "access_token": data["access_token"],
        "expires_in": data.get("expires_in", 3600),
    }


def store_tokens(db, provider_id: str, tokens: Dict) -> bool:
    """Persist the refreshed access token + expiry to the provider row."""
    expires_at = int(time.time()) + int(tokens.get("expires_in", 3600))
    return db.update_provider(provider_id, {
        "api_key": tokens["access_token"],
        "token_expires_at": expires_at,
    })


def get_valid_token(db, provider_id: str) -> Optional[str]:
    """Return a valid access token for a google_oauth provider, refreshing if
    near/at expiry. Returns None for providers that aren't google_oauth-
    authenticated (cheap no-op for every other provider type)."""
    provider = db.get_provider(provider_id)
    if not provider or provider.get("auth_type") != "google_oauth":
        return None

    access_token = provider.get("api_key")
    expires_at = provider.get("token_expires_at", 0) or 0
    now = int(time.time())

    if not access_token or expires_at <= 0 or now >= (expires_at - _REFRESH_MARGIN_SECONDS):
        refresh_token = provider.get("refresh_token", "")
        if not refresh_token:
            _log.warning("google_oauth provider '%s' has no refresh_token stored.", provider_id)
            return access_token or None
        new_tokens = refresh_access_token(refresh_token)
        if "error" in new_tokens:
            _log.warning("Google token refresh failed for '%s': %s", provider_id, new_tokens["error"])
            return access_token or None
        store_tokens(db, provider_id, new_tokens)
        _log.info("Refreshed Google OAuth access token for provider '%s'.", provider_id)
        return new_tokens["access_token"]

    return access_token


def clear_tokens(db, provider_id: str) -> bool:
    """Remove stored tokens and disconnect."""
    return db.update_provider(provider_id, {
        "api_key": "",
        "refresh_token": "",
        "token_expires_at": 0,
        "auth_type": "api_key",
    })
