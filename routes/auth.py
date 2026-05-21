"""
Authentication Blueprint — admin login with Cloudflare Turnstile captcha.
"""

import time
import threading
from typing import Dict, List

import requests
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
import config
from backend.rate_limiter import rate_limit

auth_bp = Blueprint('auth', __name__)

# ---------------------------------------------------------------------------
# In-memory login rate limiter (per IP, failed attempts only)
# ---------------------------------------------------------------------------
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 15 * 60  # 15 minutes

_login_attempts: Dict[str, List[float]] = {}
_login_attempts_lock = threading.Lock()


def _is_rate_limited(ip: str) -> bool:
    """Return True if the IP has exceeded the failed login attempt limit."""
    now = time.monotonic()
    with _login_attempts_lock:
        # Purge expired entries for this IP
        timestamps = [t for t in _login_attempts.get(ip, []) if now - t < _WINDOW_SECONDS]
        _login_attempts[ip] = timestamps
        return len(timestamps) >= _MAX_ATTEMPTS


def _record_failed_attempt(ip: str) -> None:
    now = time.monotonic()
    with _login_attempts_lock:
        _login_attempts.setdefault(ip, []).append(now)


def _clear_attempts(ip: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)




def _is_safe_redirect_url(target):
    """Validate that a redirect target is a safe relative URL.

    Rejects absolute URLs (http://..., https://..., etc.), protocol-relative
    URLs (//evil.com), and anything else that doesn't start with a single /.
    This prevents open redirect attacks.
    """
    # Reject anything containing :// (catches http://, https://, ftp://, etc.)
    if '://' in target:
        return False
    # Reject protocol-relative URLs (//evil.com)
    if target.startswith('//'):
        return False
    # Must be a relative path starting with /
    return target.startswith('/')


@auth_bp.route('/login', methods=['GET'])
def login_page():
    if session.get('authenticated'):
        next_url = request.args.get('next', '/')
        if not _is_safe_redirect_url(next_url):
            next_url = '/'
        return redirect(next_url)
    return render_template('login.html',
                           turnstile_site_key=config.TURNSTILE_SITE_KEY,
                           error=None)


@auth_bp.route('/login', methods=['POST'])
@rate_limit(max_requests=5, window_seconds=300)
def login_submit():
    ip = request.remote_addr or '0.0.0.0'

    if _is_rate_limited(ip):
        return render_template('login.html',
                               turnstile_site_key=config.TURNSTILE_SITE_KEY,
                               error='Too many login attempts. Please wait 15 minutes.'), 429

    password = request.form.get('password', '')
    turnstile_token = request.form.get('cf-turnstile-response', '')
    next_url = request.form.get('next', '/')

    # Verify Turnstile if configured
    if config.TURNSTILE_SECRET_KEY:
        try:
            ts_res = requests.post(
                'https://challenges.cloudflare.com/turnstile/v0/siteverify',
                data={
                    'secret': config.TURNSTILE_SECRET_KEY,
                    'response': turnstile_token,
                    'remoteip': request.remote_addr,
                },
                timeout=10
            )
            ts_data = ts_res.json()
            if not ts_data.get('success'):
                return render_template('login.html',
                                       turnstile_site_key=config.TURNSTILE_SITE_KEY,
                                       error='Captcha verification failed. Please try again.')
        except Exception:
            return render_template('login.html',
                                   turnstile_site_key=config.TURNSTILE_SITE_KEY,
                                   error='Captcha verification error. Please try again.')

    if not config.ADMIN_PASSWORD_HASH:
        return render_template('login.html',
                               turnstile_site_key=config.TURNSTILE_SITE_KEY,
                               error='Admin password not configured.')

    from werkzeug.security import check_password_hash
    if not check_password_hash(config.ADMIN_PASSWORD_HASH, password):
        _record_failed_attempt(ip)
        return render_template('login.html',
                               turnstile_site_key=config.TURNSTILE_SITE_KEY,
                               error='Invalid password.')

    _clear_attempts(ip)
    session['authenticated'] = True
    session.permanent = True  # Persist cookie for 7 days (configured in app.py)
    if not _is_safe_redirect_url(next_url):
        next_url = '/'
    return redirect(next_url)


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login_page'))
