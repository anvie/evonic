"""
web_fetch — fetch a public web page and return it as readable plain text.

Gives agents a web-access capability that does not require code execution
(`bash`/`runpy`). Guarded against SSRF: only http/https, only public hosts
(no loopback, private, link-local or container-internal addresses), bounded
response size and timeout.

Usage: web_fetch({"url": "https://evonic.dev/getting-started/overview/"})
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from urllib.parse import urlparse, urlunparse

MAX_BYTES = 2 * 1024 * 1024          # 2 MB response cap
DEFAULT_MAX_CHARS = 20000            # default text returned to the model
HARD_MAX_CHARS = 60000               # never return more than this
TIMEOUT = 20                         # seconds
USER_AGENT = "Evonic-WebFetch/1.0 (+https://evonic.dev)"

# Schemes never allowed by this tool
_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".localdomain")
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "instance-data"}

_STRIP_BLOCKS = re.compile(
    r"<(script|style|noscript|template|svg|iframe|form|head)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_BREAK = re.compile(
    r"</?(p|div|section|article|header|footer|main|aside|nav|h[1-6]|li|ul|ol|tr|table|br|hr|pre|blockquote)\b[^>]*>",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HSPACE = re.compile(r"[ \t\u00a0]+")
_NL = re.compile(r"\n{3,}")


def _error(message: str) -> dict:
    return {"status": "error", "error": message}


def _host_is_public(host: str) -> tuple[bool, str]:
    """Return (allowed, reason) for a hostname."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False, "empty host"
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False, f"host '{host}' is not a public host"

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"cannot resolve host '{host}'"

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False, f"host '{host}' resolves to a non-public address ({ip})"
    return True, ""


def _normalize_url(raw: str) -> tuple[str | None, str]:
    raw = (raw or "").strip()
    if not raw:
        return None, "url is required"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    try:
        parts = urlparse(raw)
    except ValueError as exc:
        return None, f"invalid url: {exc}"
    if parts.scheme.lower() not in ("http", "https"):
        return None, "only http:// and https:// URLs are supported"
    if not parts.hostname:
        return None, "url has no host"
    return urlunparse(parts), ""


_MAIN_REGION = re.compile(r"<main\b[^>]*>(.*?)</main\s*>", re.IGNORECASE | re.DOTALL)
_ARTICLE_REGION = re.compile(r"<article\b[^>]*>(.*?)</article\s*>", re.IGNORECASE | re.DOTALL)


def _content_region(markup: str) -> str:
    """Prefer the page's main/article content so nav chrome stays out of the text."""
    for pattern in (_MAIN_REGION, _ARTICLE_REGION):
        match = pattern.search(markup)
        if match and len(match.group(1)) > 500:
            return match.group(1)
    return markup


def _html_to_text(markup: str) -> str:
    markup = _COMMENT.sub(" ", markup)
    markup = _STRIP_BLOCKS.sub(" ", markup)
    markup = _BLOCK_BREAK.sub("\n", markup)
    text = _TAG.sub(" ", markup)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HSPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _NL.sub("\n\n", text)
    return text.strip()


def _extract_title(markup: str) -> str:
    match = _TITLE.search(markup)
    if not match:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", match.group(1)))).strip()


def execute(agent_context: dict, args: dict) -> dict:
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        return _error("requests is not available in this environment")

    url, problem = _normalize_url((args or {}).get("url", ""))
    if url is None:
        return _error(problem)

    try:
        max_chars = int((args or {}).get("max_chars") or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    max_chars = max(500, min(max_chars, HARD_MAX_CHARS))

    allowed, reason = _host_is_public(urlparse(url).hostname)
    if not allowed:
        return _error(f"blocked: {reason}")

    try:
        resp = requests.get(
            url,
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*"},
            allow_redirects=True,
            stream=True,
        )
    except requests.RequestException as exc:
        return _error(f"request failed: {exc.__class__.__name__}: {exc}")

    try:
        # Redirects are re-validated: a public host must not bounce us inward.
        final_url = resp.url or url
        if final_url != url:
            allowed, reason = _host_is_public(urlparse(final_url).hostname)
            if not allowed:
                return _error(f"blocked after redirect: {reason}")

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if content_type and not any(
            t in content_type for t in ("text/", "html", "xml", "json", "javascript")
        ):
            return _error(f"unsupported content type: {content_type}")

        body = b""
        truncated_bytes = False
        for chunk in resp.iter_content(65536):
            if not chunk:
                continue
            body += chunk
            if len(body) >= MAX_BYTES:
                body = body[:MAX_BYTES]
                truncated_bytes = True
                break
    except requests.RequestException as exc:
        return _error(f"failed while reading response: {exc.__class__.__name__}: {exc}")
    finally:
        resp.close()

    encoding = resp.encoding or "utf-8"
    try:
        raw_text = body.decode(encoding, errors="replace")
    except LookupError:
        raw_text = body.decode("utf-8", errors="replace")

    is_html = "html" in content_type or raw_text.lstrip()[:200].lower().startswith("<!doctype html")
    is_html = is_html or "<html" in raw_text[:2000].lower()

    title = _extract_title(raw_text) if is_html else ""
    text = _html_to_text(_content_region(raw_text)) if is_html else raw_text.strip()

    truncated_chars = len(text) > max_chars
    if truncated_chars:
        text = text[:max_chars].rstrip() + "\n\n[... truncated]"

    result = {
        "status": "success",
        "url": url,
        "final_url": final_url,
        "http_status": resp.status_code,
        "content_type": content_type or "unknown",
        "title": title,
        "text": text,
        "chars": len(text),
        "truncated": truncated_chars or truncated_bytes,
    }
    if resp.status_code >= 400:
        result["status"] = "error"
        result["error"] = f"HTTP {resp.status_code}"
    return result


def test_execute():
    # Argument validation must work without network access.
    assert execute({}, {}).get("status") == "error"
    assert execute({}, {"url": "ftp://example.com"}).get("status") == "error"
    assert execute({}, {"url": "http://localhost:8000/"}).get("status") == "error"
    assert execute({}, {"url": "http://127.0.0.1/"}).get("status") == "error"
    assert execute({}, {"url": "http://169.254.169.254/latest/meta-data/"}).get("status") == "error"
    # Text extraction strips script/style and block markup.
    text = _html_to_text("<html><head><script>var a=1;</script></head><body><h1>Hi</h1><p>A &amp; B</p></body></html>")
    assert "var a" not in text
    assert "Hi" in text and "A & B" in text
