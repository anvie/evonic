"""
local_path_link_guard.py — Core guard that prevents agents from delivering
files via local filesystem paths as clickable links in chat.

Agents sometimes reference files they just created with markdown links such as
``[guide](docs/api-integration-guide.md)`` or
``[guide](/home/evonic/evonic/plugins/.../guide.md)`` (or ``sandbox:`` /
``file:`` URIs). Such links render in the web chat as ``<a href="/home/...">``,
which resolves to a 404 on the platform origin — the user can never open the
file. The only supported file-delivery mechanisms are:

- the ``send_file`` tool (renders a proper attachment card), and
- ``save_artifact`` (publishes a file to the Artifacts tab with a public
  ``/api/agents/<id>/artifacts/<filename>`` URL).

This module is a pure, dependency-light detector + corrective-injection
builder. It is wired into the LLM loop's pre-final stage (see llm_loop.py):
when the final answer contains local-path links, the offending text is saved
as an intermediate message and the LLM is forced back into the loop with an
instruction to re-answer using ``send_file``.
"""

from __future__ import annotations

import re
from typing import List

# Schemes that are safe to keep as clickable links in chat.
_SAFE_SCHEMES = frozenset({"http", "https", "mailto"})

# Markdown link: [label](href) — captures the href, stopping at whitespace or
# a closing paren. Also matches image links (![alt](src)) which share syntax.
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# Scheme prefix, e.g. "file:", "sandbox:", "ftp:", "mailto:", "https:".
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")

# Windows drive path, e.g. "C:\dir\file.txt" or "C:/dir/file.txt".
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

_MAX_REPORTED_LINKS = 5


def is_safe_href(href: str) -> bool:
    """Return True when ``href`` is safe to render as a clickable link in chat.

    Safe hrefs are: fragment anchors (``#...``), same-origin platform API
    routes (``/api/...``, used for artifacts/attachments), and web/mail
    schemes (``http:``, ``https:``, ``mailto:``). Everything else — absolute
    POSIX paths, Windows paths, ``file:``/``sandbox:`` URIs, and scheme-less
    relative file links — is considered unsafe.
    """
    if not href:
        return True
    href = href.strip()
    if href.startswith("#"):
        return True
    # Same-origin platform route (artifacts, attachments). Must be checked
    # before the generic "absolute path" rule below.
    if href.startswith("/api/"):
        return True
    # Protocol-relative web URL (//host/...): resolves to http(s) in browsers.
    if href.startswith("//"):
        return True
    # Windows drive path.
    if _WIN_DRIVE_RE.match(href):
        return False
    # Any other absolute POSIX path (e.g. /home/..., /workspace/..., /tmp/...).
    if href.startswith("/"):
        return False
    # Explicit URI scheme: only http(s)/mailto are safe.
    match = _SCHEME_RE.match(href)
    if match:
        return match.group(1).lower() in _SAFE_SCHEMES
    # No scheme and not an anchor: a relative file link (docs/x.md, ./x, ../x).
    # In chat this resolves against the platform origin and is always broken.
    return False


def detect_local_path_links(text: str) -> List[str]:
    """Return a de-duplicated list of unsafe link hrefs found in ``text``.

    Only markdown link/image hrefs are inspected — bare local paths in
    prose or code spans (e.g. ``/home/www/project``) are NOT flagged, because
    developers legitimately discuss filesystem locations as plain text.
    """
    if not text:
        return []
    found: List[str] = []
    for match in _MD_LINK_RE.finditer(text):
        href = match.group(1)
        if not is_safe_href(href) and href not in found:
            found.append(href)
            if len(found) >= _MAX_REPORTED_LINKS:
                break
    return found


def build_corrective_injection(links: List[str]) -> str:
    """Build the user-role injection forcing the LLM to re-answer with send_file.

    The injection is appended to the pre-final interceptor list in llm_loop.py,
    which saves the offending answer as an intermediate message and re-enters
    the loop so the LLM can correct itself.
    """
    if not links:
        return ""
    quoted = ", ".join(f"`{link}`" for link in links)
    return (
        "[SYSTEM] Your final answer contains links to local filesystem paths "
        f"that the user cannot open ({quoted}). Never deliver files by "
        "referencing local paths as links in chat — they render as broken "
        "links. To send a file, call the `send_file` tool with the file's "
        "path; alternatively use `save_artifact` to publish it to the "
        "Artifacts tab and reference its public `/api/agents/...` URL. "
        "Re-answer the user's request now: attach the file with `send_file` "
        "(or `save_artifact`) and repeat your summary WITHOUT any local-path "
        "links."
    )
