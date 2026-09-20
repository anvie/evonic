"""Safe, bounded outbound HTTP primitives for approved image providers.

Provider adapters receive only administrator-supplied configuration.  These
helpers ensure that configuration cannot turn an image-generation request into
an arbitrary SSRF request.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .base import ImageGenerationError, SafeErrorCode

_MAX_TIMEOUT_SECONDS = 120
_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class SafeEndpoint:
    """An administrator-configured provider base URL after policy validation."""

    base_url: str
    timeout_seconds: int
    allow_private_network: bool = False
    trusted_hosts: tuple[str, ...] = ()


def _is_unroutable(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return any((
        ip.is_private,
        ip.is_loopback,
        ip.is_link_local,
        ip.is_reserved,
        ip.is_multicast,
        ip.is_unspecified,
    ))


def _resolve_host(host: str, port: int) -> set[str]:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except (OSError, ValueError) as exc:
        raise ImageGenerationError(SafeErrorCode.PROVIDER_UNAVAILABLE, "The configured provider host could not be resolved.") from exc
    if not addresses:
        raise ImageGenerationError(SafeErrorCode.PROVIDER_UNAVAILABLE, "The configured provider host could not be resolved.")
    return addresses


def _trusted_hosts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(sorted({host.strip().lower().rstrip(".") for host in value.split(",") if host.strip()}))


def _timeout(value: Any) -> int:
    try:
        timeout = int(value if value is not None else _DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError) as exc:
        raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Provider timeout must be an integer.") from exc
    if not 1 <= timeout <= _MAX_TIMEOUT_SECONDS:
        raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Provider timeout is outside the permitted range.")
    return timeout


def configured_endpoint(config: Mapping[str, Any], prefix: str, *, local: bool) -> SafeEndpoint:
    """Read and validate a fixed provider endpoint from administrator settings.

    Public/cloud endpoints must be HTTPS and resolve only to public addresses.
    Local endpoints require both the skill-wide opt-in and an exact host listed
    in ``<prefix>_trusted_hosts``.  Agent tool arguments never reach this code.
    """
    raw_url = config.get(f"{prefix}_endpoint")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "The selected provider has no configured endpoint.")
    parsed = urlparse(raw_url.strip())
    if parsed.username or parsed.password or not parsed.hostname or parsed.query or parsed.fragment:
        raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "The configured provider endpoint is invalid.")
    if parsed.scheme not in {"http", "https"}:
        raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "The configured provider endpoint is invalid.")
    host = parsed.hostname.lower().rstrip(".")
    allow_private = bool(config.get("allow_local_providers")) and local
    trusted_hosts = _trusted_hosts(config.get(f"{prefix}_trusted_hosts"))
    if local:
        if not allow_private or host not in trusted_hosts:
            raise ImageGenerationError(SafeErrorCode.PERMISSION_DENIED, "The local provider endpoint is not approved by an administrator.")
    elif parsed.scheme != "https":
        raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Cloud provider endpoints must use HTTPS.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _resolve_host(host, port)
    if local:
        # Local adapters may only target their exact approved hostname.  Reject
        # public-to-private mixing, which can otherwise conceal a DNS mistake.
        if host not in trusted_hosts:
            raise ImageGenerationError(SafeErrorCode.PERMISSION_DENIED, "The local provider endpoint is not approved by an administrator.")
    elif any(_is_unroutable(address) for address in addresses):
        raise ImageGenerationError(SafeErrorCode.PERMISSION_DENIED, "The configured cloud endpoint is not publicly routable.")

    normalized_path = parsed.path.rstrip("/")
    return SafeEndpoint(
        base_url=f"{parsed.scheme}://{parsed.netloc}{normalized_path}",
        timeout_seconds=_timeout(config.get(f"{prefix}_timeout_seconds")),
        allow_private_network=allow_private,
        trusted_hosts=trusted_hosts,
    )


class BoundedHttpClient:
    """No-redirect JSON/byte client with response, timeout, and poll bounds."""

    def __init__(self, endpoint: SafeEndpoint) -> None:
        self._endpoint = endpoint
        self._opener = build_opener(HTTPRedirectHandler())
        self._opener.redirect_request = lambda *_args, **_kwargs: None

    def _url_for_path(self, path: str) -> str:
        """Build a same-origin URL and revalidate the configured host before use."""
        if not path.startswith("/") or path.startswith("//") or "/../" in f"{path}/":
            raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Provider requested an unsafe endpoint path.")
        endpoint = urlparse(self._endpoint.base_url)
        # A leading slash must not discard an administrator-approved base path.
        url = f"{endpoint.scheme}://{endpoint.netloc}{endpoint.path.rstrip('/')}{path}"
        parsed = urlparse(url)
        if parsed.hostname != endpoint.hostname or parsed.scheme != endpoint.scheme:
            raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Provider requested an unsafe endpoint path.")
        addresses = _resolve_host(parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80))
        if not self._endpoint.allow_private_network and any(_is_unroutable(address) for address in addresses):
            raise ImageGenerationError(SafeErrorCode.PERMISSION_DENIED, "The configured cloud endpoint is not publicly routable.")
        return url

    def request(self, method: str, path: str, *, body: bytes | None = None, headers: Mapping[str, str] | None = None, max_bytes: int = _MAX_RESPONSE_BYTES) -> tuple[bytes, str]:
        if not 1 <= max_bytes <= _MAX_RESPONSE_BYTES:
            raise ValueError("max_bytes must be within the permitted response limit")
        url = self._url_for_path(path)
        request = Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            with self._opener.open(request, timeout=self._endpoint.timeout_seconds) as response:
                data = response.read(max_bytes + 1)
                content_type = response.headers.get_content_type()
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise ImageGenerationError(SafeErrorCode.PERMISSION_DENIED, "The provider rejected its configured credentials.") from exc
            if exc.code == 429:
                raise ImageGenerationError(SafeErrorCode.RATE_LIMITED, "The provider is rate limiting image requests.") from exc
            raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider rejected the image request.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ImageGenerationError(SafeErrorCode.PROVIDER_UNAVAILABLE, "The provider could not be reached within the configured timeout.") from exc
        if len(data) > max_bytes:
            raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider response exceeded the permitted size.")
        return data, content_type

    def json(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        data, _ = self.request(method, path, body=body, headers=headers)
        try:
            decoded = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider returned an invalid response.") from exc
        if not isinstance(decoded, Mapping):
            raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider returned an invalid response.")
        return decoded

    def poll_json(self, path: str, *, timeout_seconds: int, interval_seconds: float = 1.0) -> Mapping[str, Any]:
        """Poll a fixed provider status resource within hard deadline bounds."""
        deadline = time.monotonic() + min(max(timeout_seconds, 1), _MAX_TIMEOUT_SECONDS)
        while time.monotonic() < deadline:
            response = self.json("GET", path)
            if response.get("status") in {"completed", "succeeded"}:
                return response
            if response.get("status") in {"failed", "cancelled"}:
                raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider did not complete image generation.")
            time.sleep(min(max(interval_seconds, 0.1), 5.0))
        raise ImageGenerationError(SafeErrorCode.PROVIDER_UNAVAILABLE, "The provider did not complete image generation within the configured timeout.")
