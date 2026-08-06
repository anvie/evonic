"""
OAuth 2.0 Authorization Code + PKCE flow for OpenAI Codex subscription.

Hybrid callback: a temp HTTP server on localhost:1455 handles the redirect
automatically for local users; remote users can paste the callback URL manually.

Token persistence via the providers table.
"""

import base64
import hashlib
import json as _json
import logging
import os
import platform
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlencode, urlparse, parse_qs

import requests

_log = logging.getLogger(__name__)

# Endpoints from Codex CLI source (codex-rs/login/src/auth/)
AUTH_ENDPOINT = "https://auth.openai.com/oauth/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
SCOPES = "openid profile email offline_access api.connectors.read api.connectors.invoke"

# OpenAI OAuth app ONLY accepts this redirect URI (registered in their app)
REDIRECT_URI = "http://localhost:1455/auth/callback"

# In-memory state for pending auth flows (keyed by OAuth state, not provider_id)
_pending_auth: Dict[str, Dict] = {}
_pending_device_auth: Dict[str, Dict] = {}
_callback_server: Optional[HTTPServer] = None
_callback_server_running: bool = False


def _find_pending_by_provider(provider_id: str) -> Optional[Dict]:
    """Find the pending auth entry for a given provider ID, preferring one with auth_code."""
    best = None
    for pending in _pending_auth.values():
        if pending.get("provider_id") == provider_id:
            if pending.get("auth_code"):
                return pending
            if best is None:
                best = pending
    return best


def _generate_pkce():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _decode_jwt_claims(token: str) -> Dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return _json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _access_token_is_expiring(token: str, skew_seconds: int = 120) -> bool:
    exp = _decode_jwt_claims(token).get("exp")
    return bool(exp and time.time() >= int(exp) - skew_seconds)


def _codex_home() -> Path:
    return Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _read_codex_keyring(codex_home: Path) -> Optional[Dict]:
    """Read Codex's direct macOS keyring backend without changing it."""
    if platform.system() != "Darwin":
        return None
    key = "cli|" + hashlib.sha256(str(codex_home.resolve()).encode()).hexdigest()[:16]
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Codex Auth", "-a", key, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        return _json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, _json.JSONDecodeError):
        return None


def read_codex_cli_credentials() -> Optional[Dict]:
    """Read the freshest usable Codex CLI OAuth credential snapshot."""
    codex_home = _codex_home()
    candidates = []
    keyring = _read_codex_keyring(codex_home)
    if keyring:
        candidates.append(("codex_keyring", keyring))
    try:
        auth_file = codex_home / "auth.json"
        if auth_file.is_file():
            candidates.append(("codex_auth_file", _json.loads(auth_file.read_text())))
    except (OSError, _json.JSONDecodeError):
        pass

    usable = []
    for source, payload in candidates:
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            continue
        usable.append({
            "source": source,
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "account_id": tokens.get("account_id", ""),
            "expires_at": int(_decode_jwt_claims(tokens["access_token"]).get("exp") or 0),
        })
    if not usable:
        return None
    return max(usable, key=lambda item: item["expires_at"])


def use_codex_cli_credentials(db, provider_id: str) -> Dict:
    creds = read_codex_cli_credentials()
    if not creds:
        return {"error": "No Codex CLI login found. Run `codex login` first."}
    store_tokens(db, provider_id, {
        "access_token": creds["access_token"],
        "refresh_token": creds.get("refresh_token", ""),
        "expires_at": creds.get("expires_at", 0),
    }, credential_source="codex_cli_import")
    return {"success": True, "detected_source": creds["source"]}


def start_device_auth_flow(provider_id: str) -> Dict:
    """Start the same device-code login flow used by the Codex CLI."""
    try:
        resp = requests.post(
            "https://auth.openai.com/api/accounts/deviceauth/usercode",
            json={"client_id": CLIENT_ID},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return {"error": f"Failed to request device code: {exc}"}
    if resp.status_code != 200:
        return {"error": f"Device login failed (HTTP {resp.status_code})."}
    try:
        data = resp.json()
    except ValueError:
        return {"error": "OpenAI returned an invalid device code response."}
    user_code = data.get("user_code")
    device_auth_id = data.get("device_auth_id")
    if not user_code or not device_auth_id:
        return {"error": "OpenAI returned an incomplete device code response."}
    _pending_device_auth[provider_id] = {
        "user_code": user_code,
        "device_auth_id": device_auth_id,
        "started_at": time.time(),
        "interval": max(3, int(data.get("interval", 5))),
    }
    return {
        "success": True,
        "user_code": user_code,
        "verification_url": "https://auth.openai.com/codex/device",
        "interval": _pending_device_auth[provider_id]["interval"],
    }


def poll_device_auth_flow(provider_id: str) -> Dict:
    pending = _pending_device_auth.get(provider_id)
    if not pending:
        return {"status": "error", "error": "No pending device login."}
    if time.time() - pending["started_at"] > 900:
        _pending_device_auth.pop(provider_id, None)
        return {"status": "expired", "error": "Device login timed out."}
    try:
        resp = requests.post(
            "https://auth.openai.com/api/accounts/deviceauth/token",
            json={
                "device_auth_id": pending["device_auth_id"],
                "user_code": pending["user_code"],
            },
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return {"status": "error", "error": f"Device login polling failed: {exc}"}
    if resp.status_code in {403, 404}:
        return {"status": "pending"}
    if resp.status_code != 200:
        return {"status": "error", "error": f"Device login failed (HTTP {resp.status_code})."}

    try:
        data = resp.json()
    except ValueError:
        return {"status": "error", "error": "OpenAI returned invalid authorization data."}
    code = data.get("authorization_code")
    verifier = data.get("code_verifier")
    if not code or not verifier:
        return {"status": "error", "error": "OpenAI returned incomplete authorization data."}
    try:
        token_resp = requests.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://auth.openai.com/deviceauth/callback",
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
    except requests.RequestException as exc:
        _pending_device_auth.pop(provider_id, None)
        return {"status": "error", "error": f"Token exchange failed: {exc}"}
    _pending_device_auth.pop(provider_id, None)
    if token_resp.status_code != 200:
        return {"status": "error", "error": f"Token exchange failed (HTTP {token_resp.status_code})."}
    try:
        tokens = token_resp.json()
    except ValueError:
        return {"status": "error", "error": "OpenAI returned an invalid token response."}
    if not tokens.get("access_token"):
        return {"status": "error", "error": "OpenAI did not return an access token."}
    return {"status": "complete", "tokens": tokens}


def _run_callback_server():
    """Start a temporary HTTP server on port 1455 to receive the OAuth redirect."""
    global _callback_server, _callback_server_running

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/auth/callback":
                params = parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                error = params.get("error", [None])[0]

                if error:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<h2>Authentication failed</h2><p>" + error.encode() + b"</p>")
                    return

                if not code or not state:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Missing code or state")
                    return

                # Direct lookup by state
                pending = _pending_auth.get(state)
                if pending is not None:
                    pending["auth_code"] = code
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body style='text-align:center;padding-top:3rem;font-family:system-ui'>"
                        b"<h2>Authentication successful!</h2>"
                        b"<p>You can close this tab and return to Evonic.</p>"
                        b"</body></html>"
                    )
                    return

                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Unknown state")

        def log_message(self, format, *args):
            pass  # suppress HTTP server logs

    try:
        _callback_server = HTTPServer(("127.0.0.1", 1455), CallbackHandler)
        _callback_server.timeout = 2  # seconds between polling for shutdown
        _callback_server_running = True
        _log.info("Codex callback server listening on http://localhost:1455")

        while _callback_server_running:
            _callback_server.handle_request()
    except Exception as e:
        _log.error(f"Callback server error: {e}")
    finally:
        _callback_server_running = False
        _callback_server = None


def _stop_callback_server():
    """Shut down the temporary callback server."""
    global _callback_server_running
    _callback_server_running = False


def start_auth_flow(provider_id: str, redirect_uri: str = None) -> Dict:
    """Start the OAuth PKCE flow. Spawns a temp HTTP server on port 1455.

    Returns dict with auth_url to open in the user's browser.
    """
    if redirect_uri is None:
        redirect_uri = REDIRECT_URI

    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }

    auth_url = f"{AUTH_ENDPOINT}?{urlencode(params)}"

    _pending_auth[state] = {
        "provider_id": provider_id,
        "code_verifier": code_verifier,
        "state": state,
        "redirect_uri": redirect_uri,
        "auth_code": None,
        "error": None,
        "started_at": time.time(),
    }

    # Start the temp callback server if not already running
    global _callback_server_running
    if not _callback_server_running:
        t = threading.Thread(target=_run_callback_server, daemon=True)
        t.start()
        time.sleep(0.3)  # give the server a moment to start

    return {"auth_url": auth_url, "state": state}


def check_auth_status(provider_id: str) -> Dict:
    """Check if the OAuth callback has been received."""
    pending = _find_pending_by_provider(provider_id)
    if not pending:
        return {"status": "no_pending"}

    state_key = pending["state"]

    if pending.get("error"):
        error = pending["error"]
        _pending_auth.pop(state_key, None)
        return {"status": "error", "error": error}

    if pending.get("auth_code"):
        return {"status": "code_received"}

    if time.time() - pending["started_at"] > 300:
        _pending_auth.pop(state_key, None)
        _stop_callback_server()
        return {"status": "expired"}

    return {"status": "pending"}


def receive_callback(provider_id: str, code: str, state: str) -> Dict:
    """Handle the OAuth callback from OpenAI.

    Called by the temp HTTP server or the Flask /auth/callback route.
    Returns dict with "success" (bool) and optional "error" (str).
    """
    pending = _pending_auth.get(state)
    if not pending:
        return {"success": False, "error": "No pending authentication with this state."}

    if pending.get("provider_id") != provider_id:
        return {"success": False, "error": "Provider ID mismatch."}

    pending["auth_code"] = code
    return {"success": True}


def process_callback_url(callback_url: str) -> Dict:
    """Parse a pasted callback URL and try to match it to a pending auth flow.

    Remote users who can't redirect to localhost:1455 can paste the full
    callback URL from their browser's address bar.

    Returns dict with "success" (bool) and optional "error" (str).
    """
    try:
        parsed = urlparse(callback_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]

        if error:
            return {"success": False, "error": f"OpenAI error: {error}"}

        if not code or not state:
            return {"success": False, "error": "URL must contain 'code' and 'state' parameters."}

        # Direct lookup by state (since _pending_auth is keyed by state)
        pending = _pending_auth.get(state)
        if not pending:
            return {"success": False, "error": "No pending authentication with this state. It may have expired."}

        pending["auth_code"] = code
        return {"success": True, "provider_id": pending["provider_id"]}

    except Exception as e:
        return {"success": False, "error": f"Failed to parse callback URL: {e}"}


def exchange_code_for_tokens(provider_id: str) -> Dict:
    """Exchange the authorization code for access + refresh tokens."""
    pending = _find_pending_by_provider(provider_id)
    if not pending or not pending.get("auth_code"):
        return {"error": "No authorization code available"}

    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": pending["auth_code"],
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["code_verifier"],
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=15,
    )

    _pending_auth.pop(pending["state"], None)
    _stop_callback_server()

    if resp.status_code != 200:
        return {"error": f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}"}

    data = resp.json()
    if "access_token" not in data:
        return {"error": "No access_token in response"}

    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": data.get("expires_in", 3600),
    }


def extract_account_id(access_token: str) -> str:
    """Extract ChatGPT account ID from JWT access token claims."""
    claims = _decode_jwt_claims(access_token)
    auth = claims.get("https://api.openai.com/auth", {})
    return auth.get("chatgpt_account_id", "") if isinstance(auth, dict) else ""


def refresh_access_token(refresh_token: str) -> Dict:
    """Use a refresh token to get a new access token."""
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
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
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_in": data.get("expires_in", 3600),
    }


def store_tokens(db, provider_id: str, tokens: Dict, credential_source: str = "evonic_oauth") -> bool:
    """Persist OAuth tokens to the provider row."""
    expires_at = int(tokens.get("expires_at") or 0)
    if not expires_at:
        expires_at = int(time.time()) + int(tokens.get("expires_in", 3600))
    return db.update_provider(provider_id, {
        "api_key": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "token_expires_at": expires_at,
        "auth_type": "oauth",
        "credential_source": credential_source,
    })


def get_valid_token(db, provider_id: str) -> Optional[str]:
    """Return a valid access token, refreshing if near expiry."""
    provider = db.get_provider(provider_id)
    if not provider or provider.get("api_format") != "codex" or provider.get("auth_type") != "oauth":
        return None

    access_token = provider.get("api_key")
    if not access_token:
        return None

    expires_at = provider.get("token_expires_at", 0) or 0
    now = int(time.time())

    if expires_at > 0 and now >= (expires_at - 120):
        source = provider.get("credential_source") or "evonic_oauth"
        if source == "codex_cli_import":
            live = read_codex_cli_credentials()
            if live and not _access_token_is_expiring(live["access_token"]):
                store_tokens(db, provider_id, {
                    "access_token": live["access_token"],
                    "refresh_token": live.get("refresh_token", ""),
                    "expires_at": live.get("expires_at", 0),
                }, credential_source=source)
                return live["access_token"]
        refresh_token = provider.get("refresh_token", "")
        if refresh_token:
            new_tokens = refresh_access_token(refresh_token)
            if "error" not in new_tokens:
                store_tokens(db, provider_id, new_tokens, credential_source=source)
                return new_tokens["access_token"]
        return None

    return access_token if not _access_token_is_expiring(access_token) else None


def clear_tokens(db, provider_id: str) -> bool:
    """Remove stored tokens and disconnect."""
    return db.update_provider(provider_id, {
        "api_key": "",
        "refresh_token": "",
        "token_expires_at": 0,
        "auth_type": "",
        "credential_source": "",
    })
