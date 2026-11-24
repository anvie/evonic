"""
Authentication Blueprint — admin login with Cloudflare Turnstile captcha.
"""

import os
import secrets
import time
import threading
from typing import Dict, List

import requests
from flask import Blueprint, make_response, render_template, request, session, redirect, url_for, jsonify
import config
from backend.audit_logger import audit
from backend import totp
from backend.cookie_policy import cookie_secure

auth_bp = Blueprint('auth', __name__)

_PENDING_2FA_MAX_AGE_SECONDS = 10 * 60

# ---------------------------------------------------------------------------
# In-memory login rate limiter (per IP, failed attempts only)
# ---------------------------------------------------------------------------
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 15 * 60  # 15 minutes

_login_attempts: Dict[str, List[float]] = {}
_login_attempts_lock = threading.Lock()
_TOTP_ATTEMPT_KEY = 'totp:admin'


def _is_rate_limited(ip: str) -> bool:
    """Return True if the IP has exceeded the failed login attempt limit."""
    now = time.monotonic()
    with _login_attempts_lock:
        # Purge expired entries for this IP
        timestamps = [t for t in _login_attempts.get(ip, []) if now - t < _WINDOW_SECONDS]
        if timestamps:
            _login_attempts[ip] = timestamps
        else:
            _login_attempts.pop(ip, None)
        return len(timestamps) >= _MAX_ATTEMPTS


def _record_failed_attempt(ip: str) -> None:
    now = time.monotonic()
    with _login_attempts_lock:
        _login_attempts.setdefault(ip, []).append(now)


def _clear_attempts(ip: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)




def _complete_login(next_url: str):
    """Create the authenticated session and redirect after all login factors pass."""
    old_session = dict(session)
    session.clear()
    session.update(old_session)
    session['authenticated'] = True
    session.permanent = True

    csrf_token = secrets.token_hex(32)
    session['csrf_token'] = csrf_token
    if not _is_safe_redirect_url(next_url):
        next_url = '/'
    response = make_response(redirect(next_url))
    response.set_cookie(
        'csrf_token', csrf_token,
        httponly=False,
        samesite='Lax',
        secure=cookie_secure(),
        path='/',
        max_age=604800,
    )
    return response


def _is_pending_two_factor_login() -> bool:
    """Return whether the current session has a recent password verification."""
    created_at = session.get('pending_2fa_at')
    return isinstance(created_at, (int, float)) and time.time() - created_at <= _PENDING_2FA_MAX_AGE_SECONDS


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
    # Browsers treat a backslash as a slash, so /\evil.com acts like //evil.com
    if '\\' in target:
        return False
    # Must be a relative path starting with /
    return target.startswith('/')


def _refresh_admin_password_hash() -> None:
    """Pick up an ADMIN_PASSWORD_HASH changed in .env (e.g. by `evonic pass`).

    The hash is read once at import time, so without this a running server
    would need a restart before a new password worked.
    """
    if os.path.exists(os.path.join(os.path.expanduser('~'), '.envcrypt.yaml')):
        return  # .env is encrypted; nothing to read
    from backend.dotenv_loader import read_dotenv_value
    from backend.setup import apply_admin_password_hash
    try:
        current = read_dotenv_value('ADMIN_PASSWORD_HASH')
    except OSError:
        return
    if current is not None and current != config.ADMIN_PASSWORD_HASH:
        apply_admin_password_hash(current)


def _login_context(error=None, notice=None) -> dict:
    """Return the context shared by every ``/login`` render branch.

    ``password_configured`` and ``setup_required`` let the page explain an
    instance that has no admin password yet, instead of showing a form that can
    never succeed. Every ``render_template('login.html', ...)`` call site goes
    through here so none of them can forget the flags.
    """
    from models.db import db
    from backend.setup import initial_admin_password_path

    _refresh_admin_password_hash()
    try:
        setup_required = not db.has_super_agent()
    except Exception:
        setup_required = False
    return {
        'turnstile_site_key': config.TURNSTILE_SITE_KEY,
        'error': error,
        'notice': notice,
        'password_configured': bool(config.ADMIN_PASSWORD_HASH),
        'setup_required': setup_required,
        'initial_password_pending': os.path.exists(initial_admin_password_path()),
        'initial_password_path': initial_admin_password_path(),
    }


@auth_bp.route('/login', methods=['GET'])
def login_page():
    if session.get('authenticated'):
        next_url = request.args.get('next', '/')
        next_url = next_url.replace('\r', '').replace('\n', '')
        if not _is_safe_redirect_url(next_url):
            next_url = '/'
        return redirect(next_url)
    # Shown after first-time setup, which redirects here once the server is back.
    notice = None
    if request.args.get('ready'):
        notice = 'Setup complete. Sign in with the password you just created.'
    return render_template('login.html', **_login_context(notice=notice))


@auth_bp.route('/login', methods=['POST'])
def login_submit():
    _refresh_admin_password_hash()
    ip = request.remote_addr or '0.0.0.0'

    email = 'admin'  # admin-only login, no user-specific email

    # Helper to log failures concisely
    def _audit_fail(reason):
        audit.log_login(ip=ip, email=email, result='failure', reason=reason)

    if _is_rate_limited(ip):
        _audit_fail('rate_limited')
        return render_template(
            'login.html',
            **_login_context(error='Too many login attempts. Please wait 15 minutes.'),
        ), 429

    password = request.form.get('password', '')
    turnstile_token = request.form.get('cf-turnstile-response', '')
    next_url = request.form.get('next', '/')
    next_url = next_url.replace('\r', '').replace('\n', '')

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
                _audit_fail('captcha_failed')
                return render_template(
                    'login.html',
                    **_login_context(error='Captcha verification failed. Please try again.'),
                )
        except Exception:
            _audit_fail('captcha_error')
            return render_template(
                'login.html',
                **_login_context(error='Captcha verification error. Please try again.'),
            )

    if not config.ADMIN_PASSWORD_HASH:
        _audit_fail('no_admin_configured')
        return render_template(
            'login.html',
            **_login_context(error='Admin password not configured.'),
        )

    from werkzeug.security import check_password_hash
    if not check_password_hash(config.ADMIN_PASSWORD_HASH, password):
        _audit_fail('invalid_password')
        _record_failed_attempt(ip)
        return render_template(
            'login.html',
            **_login_context(error='Invalid password.'),
        )

    _clear_attempts(ip)

    if totp.is_enabled():
        session.clear()
        session['pending_2fa_at'] = time.time()
        session['pending_2fa_next'] = next_url if _is_safe_redirect_url(next_url) else '/'
        return redirect(url_for('auth.two_factor'))

    audit.log_login(ip=ip, email=email, result='success')
    return _complete_login(next_url)


@auth_bp.route('/two-factor', methods=['GET', 'POST'])
def two_factor():
    """Enroll or verify the configured TOTP factor after password verification."""
    if session.get('authenticated'):
        return redirect('/')
    if not totp.is_enabled() or not _is_pending_two_factor_login():
        session.pop('pending_2fa_at', None)
        session.pop('pending_2fa_next', None)
        return redirect(url_for('auth.login_page'))

    secret = totp.get_secret()
    setup = not totp.is_configured()
    if setup and not secret:
        secret = totp.begin_enrollment()

    if request.method == 'POST':
        # One admin account, so one shared counter. Keyed apart from the IP limiter
        # because a correct password clears that one, which would reset the guess count.
        if _is_rate_limited(_TOTP_ATTEMPT_KEY):
            audit.log_login(ip=request.remote_addr or '0.0.0.0', email='admin',
                            result='failure', reason='totp_rate_limited')
            session.pop('pending_2fa_at', None)
            session.pop('pending_2fa_next', None)
            return render_template(
                'two_factor.html', setup=setup, provisioning_uri=None,
                action=url_for('auth.two_factor'),
                error='Too many invalid codes. Please wait 15 minutes.',
            ), 429
        if secret and totp.verify_code(secret, request.form.get('code', '')):
            if setup:
                totp.confirm_enrollment()
            _clear_attempts(_TOTP_ATTEMPT_KEY)
            next_url = session.pop('pending_2fa_next', '/')
            session.pop('pending_2fa_at', None)
            audit.log_login(ip=request.remote_addr or '0.0.0.0', email='admin', result='success')
            return _complete_login(next_url)
        _record_failed_attempt(_TOTP_ATTEMPT_KEY)
        return render_template(
            'two_factor.html', setup=setup,
            provisioning_uri=totp.provisioning_uri(secret) if setup and secret else None,
            action=url_for('auth.two_factor'), error='Invalid authentication code. Please try again.',
        )

    return render_template(
        'two_factor.html', setup=setup,
        provisioning_uri=totp.provisioning_uri(secret) if setup and secret else None,
        action=url_for('auth.two_factor'), error=None,
    )


@auth_bp.route('/logout')
def logout():
    session.clear()
    response = make_response(redirect(url_for('auth.login_page')))
    response.delete_cookie('csrf_token', path='/')
    return response
