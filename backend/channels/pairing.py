"""Pairing code generation and validation.

Generates uppercase alphanumeric pairing codes with a ``EVN-`` prefix, e.g.
``EVN-AB12CD34``. The random payload uses an unambiguous character set (excludes
0/O and 1/I/L) to avoid visual mistyping.
"""
from __future__ import annotations

from typing import Optional

import re
import secrets

# Prefix that marks a pairing code in free text. Distinctive enough that
# ordinary chat ("Hi khodam", "thanks", ...) is never taken for a code.
_PREFIX = "EVN-"
# Eight payload characters provide roughly 40 bits of entropy while remaining
# short enough to share manually. This makes accidental collisions negligible.
_CODE_LEN = 8
# Unambiguous characters: removed 0, O, 1, I, L to prevent visual mistyping.
# This is a subset of the uppercase alphanumeric set [A-Z0-9].
_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# New pairing codes use eight payload characters. Four-character codes remain
# accepted until existing pending approvals expire after an upgrade.
_LEGACY_CODE_LEN = 4
_PATTERN = re.compile(
    r"^EVN-[A-Z0-9]{%d}(?:[A-Z0-9]{%d})?$" % (_LEGACY_CODE_LEN, _LEGACY_CODE_LEN)
)

# Finds a new or legacy code embedded in arbitrary text. The hyphen is required
# and the match is anchored on word boundaries, so a word such as "EVNEWS" is
# not mistaken for a code. The longer form is listed first to avoid partial
# matches when an eight-character code is supplied.
_EXTRACT_RE = re.compile(
    r"\bEVN-([A-Z0-9]{%d}|[A-Z0-9]{%d})\b" % (_CODE_LEN, _LEGACY_CODE_LEN)
)


def generate_pair_code() -> str:
    """Generate a cryptographically secure code, e.g. ``EVN-AB12CD34``."""
    return _PREFIX + "".join(secrets.choice(_CHARS) for _ in range(_CODE_LEN))


def format_pair_code(raw: str) -> str:
    """Return the canonical, uppercase display form of a pairing code."""
    return raw.upper()


def validate_pair_code(code: Optional[str]) -> bool:
    """Check for a new or still-valid legacy pairing-code format."""
    return bool(code is not None and _PATTERN.match(code))


def extract_pair_code(text: Optional[str]) -> str | None:
    """Extract a new or legacy pairing code from arbitrary text.

    Only codes carrying the ``EVN-`` prefix and an uppercase alphanumeric
    payload are recognised, so ordinary words are never treated as pairing
    attempts. Matching is case-insensitive; the result is normalised to
    uppercase and returned in canonical form, or ``None`` when no code is
    present.
    """
    if not text or not isinstance(text, str):
        return None
    m = _EXTRACT_RE.search(text.upper())
    if not m:
        return None
    return _PREFIX + m.group(1)
