"""Adaptive cookie security policy for session and CSRF cookies.

The ``Secure`` attribute must only be attached to cookies when the request
actually arrived over HTTPS. Browsers silently discard ``Secure`` cookies that
are received over plain HTTP (except from ``localhost``/``127.0.0.1``), which
makes login fail *without any error message*: the session cookie is dropped,
so ``session['authenticated']`` never persists and ``/login`` redirects back to
itself forever.

Trusted proxies are handled by ``ProxyFix`` (``X-Forwarded-Proto``), which
rewrites the WSGI scheme, so ``request.is_secure`` is authoritative here.
``FORCE_INSECURE_COOKIES=1`` remains the explicit override for setups that
cannot use HTTPS (cookie then never carries ``Secure``).
"""

import config

__all__ = ["cookie_secure", "request_is_https"]


def request_is_https() -> bool:
    """Return True when the current request reached us over HTTPS."""
    try:
        from flask import has_request_context, request
    except Exception:  # pragma: no cover - Flask is always installed in practice
        return False
    if not has_request_context():
        return False
    return bool(request.is_secure)


def cookie_secure() -> bool:
    """Return whether cookies set during this request should carry ``Secure``.

    Falls back to the static ``config.SESSION_COOKIE_SECURE`` value when called
    outside a request context (CLI helpers, background jobs).
    """
    if config.FORCE_INSECURE_COOKIES:
        return False
    try:
        from flask import has_request_context
    except Exception:  # pragma: no cover
        return config.SESSION_COOKIE_SECURE
    if has_request_context():
        return request_is_https()
    return config.SESSION_COOKIE_SECURE
