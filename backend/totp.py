"""TOTP primitives for the single Evonic administrator account.

The shared Flask secret protects encrypted TOTP state in app_settings.  The
secret never appears in API responses after setup confirmation.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from models.db import db

_TOTP_SECRET_KEY = 'admin_totp_secret'
_TOTP_CONFIRMED_KEY = 'admin_totp_confirmed'
_PERIOD_SECONDS = 30
_DIGITS = 6
_ISSUER = 'Evonic'


def get_enabled_state() -> tuple[bool, bool]:
    """Return the effective setting and whether ``ENABLE_2FA`` overrides it."""
    import config

    override = config.get_two_factor_auth_override()
    if override is not None:
        return override, True
    return db.get_setting('two_factor_auth_enabled', '0') == '1', False


def is_enabled() -> bool:
    """Return whether administrators have globally required TOTP."""
    return get_enabled_state()[0]


def is_configured() -> bool:
    """Return whether an enrolled, confirmed TOTP secret exists."""
    return (db.get_setting(_TOTP_CONFIRMED_KEY, '0') == '1'
            and bool(db.get_setting(_TOTP_SECRET_KEY, '')))


def _fernet() -> Fernet:
    """Derive a stable Fernet key from the application secret key."""
    import config
    key = base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode('utf-8')).digest())
    return Fernet(key)


def _encrypt(secret: str) -> str:
    return _fernet().encrypt(secret.encode('ascii')).decode('ascii')


def _decrypt(ciphertext: str) -> Optional[str]:
    try:
        return _fernet().decrypt(ciphertext.encode('ascii')).decode('ascii')
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return None


def generate_secret() -> str:
    """Generate a base32 TOTP seed compatible with standard authenticator apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def provisioning_uri(secret: str) -> str:
    """Return an otpauth URI for a new Evonic administrator enrollment."""
    label = quote(f'{_ISSUER}:admin', safe='')
    return (f'otpauth://totp/{label}?secret={secret}&issuer={quote(_ISSUER)}'
            f'&algorithm=SHA1&digits={_DIGITS}&period={_PERIOD_SECONDS}')


def _code_at(secret: str, counter: int) -> str:
    padding = '=' * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + padding, casefold=True)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** _DIGITS)).zfill(_DIGITS)


def verify_code(secret: str, code: str, now: Optional[float] = None) -> bool:
    """Verify a current TOTP code, allowing a one-period clock skew."""
    normalized = ''.join(ch for ch in str(code) if ch.isdigit())
    if len(normalized) != _DIGITS:
        return False
    counter = int((time.time() if now is None else now) // _PERIOD_SECONDS)
    return any(hmac.compare_digest(_code_at(secret, counter + offset), normalized)
               for offset in (-1, 0, 1))


def get_secret() -> Optional[str]:
    """Return the stored secret internally, or None when unavailable."""
    ciphertext = db.get_setting(_TOTP_SECRET_KEY, '')
    return _decrypt(ciphertext) if ciphertext else None


def begin_enrollment() -> str:
    """Create and store an encrypted enrollment secret until it is confirmed."""
    secret = generate_secret()
    db.set_setting(_TOTP_SECRET_KEY, _encrypt(secret))
    db.set_setting(_TOTP_CONFIRMED_KEY, '0')
    return secret


def confirm_enrollment() -> None:
    """Mark the already-stored enrollment secret as verified."""
    db.set_setting(_TOTP_CONFIRMED_KEY, '1')
