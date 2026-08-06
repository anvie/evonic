"""Anthropic API and Claude Code subscription credentials."""

import base64
import hashlib
import json
import os
import platform
import secrets
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import requests


CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference"
TOKEN_USER_AGENT = "axios/1.7.9"
SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
OAUTH_BETAS = "claude-code-20250219,oauth-2025-04-20"

_pending_auth: Dict[str, Dict[str, Any]] = {}
_claude_version: Optional[str] = None


def _read_keychain() -> Optional[Dict[str, Any]]:
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    return _normalize_credentials(payload, "macos_keychain")


def _credential_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _read_file() -> Optional[Dict[str, Any]]:
    try:
        path = _credential_path()
        return _normalize_credentials(json.loads(path.read_text()), "claude_credentials_file") if path.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_credentials(payload: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict) or not (oauth.get("accessToken") or oauth.get("refreshToken")):
        return None
    return {
        "access_token": oauth.get("accessToken", ""),
        "refresh_token": oauth.get("refreshToken", ""),
        "expires_at_ms": int(oauth.get("expiresAt") or 0),
        "scopes": oauth.get("scopes"),
        "source": source,
    }


def _is_valid(creds: Dict[str, Any]) -> bool:
    expires_at = creds.get("expires_at_ms", 0)
    return bool(creds.get("access_token")) and (
        not expires_at or int(time.time() * 1000) < int(expires_at) - 60_000
    )


def read_claude_code_credentials() -> Optional[Dict[str, Any]]:
    candidates = [item for item in (_read_keychain(), _read_file()) if item]
    if not candidates:
        return None
    valid = [item for item in candidates if _is_valid(item)]
    return max(valid or candidates, key=lambda item: item.get("expires_at_ms", 0))


def _write_file(creds: Dict[str, Any]) -> None:
    path = _credential_path()
    try:
        payload = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    oauth = {
        "accessToken": creds["access_token"],
        "refreshToken": creds.get("refresh_token", ""),
        "expiresAt": creds.get("expires_at_ms", 0),
    }
    if creds.get("scopes") is not None:
        oauth["scopes"] = creds["scopes"]
    payload["claudeAiOauth"] = oauth
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        finally:
            raise


def _refresh(refresh_token: str) -> Optional[Dict[str, Any]]:
    for endpoint in TOKEN_URLS:
        try:
            response = requests.post(
                endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": TOKEN_USER_AGENT},
                timeout=15,
            )
        except requests.RequestException:
            continue
        if response.status_code != 200:
            continue
        try:
            data = response.json()
        except ValueError:
            continue
        if data.get("access_token"):
            return {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", refresh_token),
                "expires_at_ms": int(time.time() * 1000) + int(data.get("expires_in", 3600)) * 1000,
            }
    return None


def use_claude_code_credentials(db, provider_id: str) -> Dict[str, Any]:
    creds = read_claude_code_credentials()
    if not creds or (not _is_valid(creds) and not creds.get("refresh_token")):
        return {"error": "No usable Claude Code login found. Run `claude setup-token` first."}
    db.update_provider(provider_id, {
        "api_key": "",
        "refresh_token": "",
        "token_expires_at": 0,
        "auth_type": "oauth",
        "credential_source": "claude_code",
    })
    return {"success": True, "detected_source": creds["source"]}


def save_secret(db, provider_id: str, secret: str) -> Dict[str, Any]:
    secret = secret.strip()
    if not secret.startswith(("sk-ant-", "cc-", "eyJ")):
        return {"error": "Enter an Anthropic API key or Claude setup-token."}
    oauth = is_oauth_token(secret)
    db.update_provider(provider_id, {
        "api_key": secret,
        "refresh_token": "",
        "token_expires_at": 0,
        "auth_type": "oauth" if oauth else "api_key",
        "credential_source": "setup_token" if oauth else "api_key",
    })
    return {"success": True}


def is_oauth_token(token: str) -> bool:
    return bool(token) and not token.startswith("sk-ant-api") and token.startswith(("sk-ant-", "cc-", "eyJ"))


def start_oauth_flow(provider_id: str) -> Dict[str, Any]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    _pending_auth[provider_id] = {"verifier": verifier, "state": state, "started_at": time.time()}
    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return {"success": True, "auth_url": f"{AUTHORIZE_URL}?{urlencode(params)}"}


def complete_oauth_flow(db, provider_id: str, authorization_code: str) -> Dict[str, Any]:
    pending = _pending_auth.get(provider_id)
    if not pending or time.time() - pending["started_at"] > 600:
        _pending_auth.pop(provider_id, None)
        return {"error": "OAuth login expired. Start again."}
    code, separator, state = authorization_code.strip().partition("#")
    if not separator or state != pending["state"]:
        return {"error": "Invalid authorization code or OAuth state."}
    body = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "state": state,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": pending["verifier"],
    }
    result = None
    for endpoint in TOKEN_URLS:
        try:
            response = requests.post(
                endpoint,
                json=body,
                headers={"Content-Type": "application/json", "User-Agent": TOKEN_USER_AGENT},
                timeout=15,
            )
        except requests.RequestException:
            continue
        if response.status_code == 200:
            try:
                result = response.json()
            except ValueError:
                continue
            break
    _pending_auth.pop(provider_id, None)
    if not result or not result.get("access_token"):
        return {"error": "Anthropic token exchange failed."}
    db.update_provider(provider_id, {
        "api_key": result["access_token"],
        "refresh_token": result.get("refresh_token", ""),
        "token_expires_at": int(time.time()) + int(result.get("expires_in", 3600)),
        "auth_type": "oauth",
        "credential_source": "evonic_oauth",
    })
    return {"success": True}


def resolve_credential(db, provider_id: str) -> Tuple[Optional[str], bool]:
    provider = db.get_provider(provider_id)
    if not provider or provider.get("api_format") != "anthropic":
        return None, False
    source = provider.get("credential_source") or "api_key"
    if source == "claude_code":
        creds = read_claude_code_credentials()
        if not creds:
            return None, True
        if _is_valid(creds):
            return creds["access_token"], True
        # Re-read happened above; refresh only the current live token to avoid
        # racing Claude Code's single-use refresh-token rotation.
        refreshed = _refresh(creds.get("refresh_token", "")) if creds.get("refresh_token") else None
        if refreshed:
            refreshed["scopes"] = creds.get("scopes")
            _write_file(refreshed)
            return refreshed["access_token"], True
        return None, True

    token = provider.get("api_key") or ""
    if provider.get("auth_type") != "oauth":
        return (token or None), False
    expires_at = int(provider.get("token_expires_at") or 0)
    if token and (not expires_at or time.time() < expires_at - 120):
        return token, True
    refresh_token = provider.get("refresh_token") or ""
    refreshed = _refresh(refresh_token) if refresh_token else None
    if not refreshed:
        return None, True
    db.update_provider(provider_id, {
        "api_key": refreshed["access_token"],
        "refresh_token": refreshed.get("refresh_token", refresh_token),
        "token_expires_at": int(refreshed["expires_at_ms"] / 1000),
    })
    return refreshed["access_token"], True


def clear_credentials(db, provider_id: str) -> bool:
    return db.update_provider(provider_id, {
        "api_key": "",
        "refresh_token": "",
        "token_expires_at": 0,
        "auth_type": "api_key",
        "credential_source": "api_key",
    })


def claude_code_version() -> str:
    global _claude_version
    if _claude_version:
        return _claude_version
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
        version = result.stdout.strip().split()[0]
        _claude_version = version if result.returncode == 0 and version[:1].isdigit() else "2.1.74"
    except (OSError, subprocess.TimeoutExpired):
        _claude_version = "2.1.74"
    return _claude_version


def auth_headers(token: str, oauth: bool) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if oauth:
        headers.update({
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETAS,
            "user-agent": f"claude-code/{claude_code_version()} (external, cli)",
            "x-app": "cli",
        })
    else:
        headers["x-api-key"] = token
    return headers
