"""Pairing code generation and validation.

Generates 6-character alphanumeric codes using an unambiguous character set
(excludes 0/O, 1/I/L to avoid visual confusion). Displayed as XXXXXX (no hyphen).
"""
from typing import Optional

import random
import re

# Unambiguous characters: removed 0, O, 1, I, L to prevent visual mistyping
_CHARS = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
_CODE_LEN = 6
# Format: XXXXXX (6 chars, no hyphen)
_PATTERN = re.compile(r'^[A-Z0-9]{6}$')

# Extracts a 6-char pairing code from arbitrary text.
# Also handles legacy XXX-XXX format for backwards compatibility.
_EXTRACT_RE = re.compile(r'(?:^|[^A-Z0-9])([A-Z0-9]{3})-?([A-Z0-9]{3})(?:[^A-Z0-9]|$)')


def generate_pair_code() -> str:
    """Generate a 6-char alphanumeric pairing code (unambiguous charset, no hyphen)."""
    return ''.join(random.choices(_CHARS, k=_CODE_LEN))


def format_pair_code(raw: str) -> str:
    """Format a raw code for display (no hyphen)."""
    return raw.upper()


def validate_pair_code(code: Optional[str]) -> bool:
    """Check if a string matches the XXXXXX pattern (6 uppercase alphanumeric chars)."""
    return bool(code is not None and _PATTERN.match(code))


def extract_pair_code(text: Optional[str]) -> str | None:
    """Extract a 6-char alphanumeric pairing code from arbitrary text.

    Accepts codes with or without hyphen (e.g. 'ABC123' or legacy 'ABC-123'),
    and codes embedded in surrounding text.

    Returns the raw 6-char code (uppercase, no hyphen) or None if no valid
    code is found.
    """
    if not text or not isinstance(text, str):
        return None
    text = text.strip().upper()
    # Try exact match first (the whole message is a bare code)
    if len(text) == 6 and text.isalnum():
        return text
    # Legacy format with hyphen
    if len(text) == 7 and text[3] == '-' and (text[:3] + text[4:]).isalnum():
        return text[:3] + text[4:]
    # Search for pattern in text
    m = _EXTRACT_RE.search(text)
    if m:
        return m.group(1) + m.group(2)
    return None
