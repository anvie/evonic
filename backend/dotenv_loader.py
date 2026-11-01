"""Minimal .env file loader — replaces python-dotenv.

This module provides a single public function, ``load_dotenv()``, that reads
a ``.env`` file and populates ``os.environ``. It handles:

- Empty lines and comment-only lines (``# …``)
- Inline comments after unquoted values
- Single-quoted (``'…'``) and double-quoted (``"…"``) values
- ``export`` prefix (bash-style, silently ignored)
- ``override=False``: existing environment variables are never overwritten
"""

import re
import os
from typing import Optional

__all__ = ["load_dotenv"]

# Project root = parent of this package, so the .env lookup does not depend on
# the process working directory. The launcher script never chdirs, so a server
# started from another directory used to boot with an empty environment
# (no ADMIN_PASSWORD_HASH, a fresh SECRET_KEY written on every start).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DOTENV_PATH = os.path.join(_PROJECT_ROOT, ".env")


def load_dotenv(dotenv_path: Optional[str] = None, *, override: bool = False) -> bool:
    """Parse *dotenv_path* (default: ``.env`` at the project root) and set
    the resulting key=value pairs on ``os.environ``.

    Falls back to ``<cwd>/.env`` when the project-root file is absent, so
    deployments that keep their .env elsewhere keep working.

    Returns ``True`` if the file was found and read, ``False`` otherwise.
    """
    if dotenv_path is None:
        dotenv_path = DEFAULT_DOTENV_PATH
        if not os.path.isfile(dotenv_path):
            cwd_env = os.path.join(os.getcwd(), ".env")
            if os.path.isfile(cwd_env):
                dotenv_path = cwd_env

    if not os.path.isfile(dotenv_path):
        return False

    with open(dotenv_path, encoding="utf-8", errors="surrogateescape") as f:
        for line in f:
            _parse_and_set(line, override=override)

    return True


def _parse_and_set(line: str, *, override: bool) -> None:
    """Parse a single .env line and conditionally set ``os.environ``."""

    line = line.strip()
    if not line:
        return  # empty line

    # Strip bash-style "export " prefix
    if line.startswith("export "):
        line = line[7:].lstrip()

    # Comment-only line
    if line.startswith("#"):
        return

    # Find the first '=' that separates key from value.
    eq_pos = line.find("=")
    if eq_pos == -1:
        return  # no '=' — malformed, skip (python-dotenv also skips these)

    key = line[:eq_pos].rstrip()
    if not key:
        return  # empty key

    raw_value = line[eq_pos + 1:].lstrip()

    # ----- value parsing -----
    if not raw_value:
        value = ""
    elif raw_value.startswith('"') or raw_value.startswith("'"):
        value = _parse_quoted_value(raw_value)
    else:
        value = _parse_unquoted_value(raw_value)

    if not override and key in os.environ:
        return

    os.environ[key] = value


def _parse_quoted_value(raw: str) -> str:
    """Parse a single- or double-quoted .env value, handling escapes."""
    quote = raw[0]
    closing = raw.find(quote, 1)
    inner = raw[1:closing] if closing != -1 else raw[1:]

    if quote == '"':
        # Double-quoted: support \n, \r, \t, \\, \", \$
        inner = inner.replace("\\n", "\n")
        inner = inner.replace("\\r", "\r")
        inner = inner.replace("\\t", "\t")
        inner = inner.replace("\\\"", "\"")
        inner = inner.replace("\\\\", "\\")
        inner = inner.replace("\\$", "$")  # bash-style escape
    else:
        # Single-quoted: literal only (no escape processing)
        inner = inner.replace("\\\\", "\\")
        inner = inner.replace("\\'", "'")

    return inner


def _parse_unquoted_value(raw: str) -> str:
    """Parse an unquoted .env value: strip trailing whitespace and inline comments."""
    # Strip inline comment — find first unescaped #
    # (python-dotenv strips everything after the first # that's not preceded by \)
    result = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw) and raw[i + 1] == "#":
            result.append("#")
            i += 2
            continue
        if c == "#":
            break  # inline comment start
        result.append(c)
        i += 1

    return "".join(result).rstrip()
