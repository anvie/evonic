"""
evomem_provision.py -- download and install the evomem memory-engine binary.

Single source of truth for provisioning the evomem binary. Shared by the
installer (install.sh), the `evonic evomem install` command, `evonic doctor --fix`,
and first-run setup so the provisioning surfaces stay in sync.

By default the latest release is resolved dynamically from the GitHub Releases
API; set EVOMEM_VERSION to a tag (e.g. "v0.2.0") to pin a specific release.
Integrity is verified by comparing the downloaded asset against the SHA-256
digest reported by the same TLS-protected API response, and the binary is
installed atomically. Any failure leaves the existing state untouched — the
runtime transparently falls back to FTS5 when the binary is absent (see
evomem_client.py).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import platform
import sys
import tempfile
import zipfile
import zlib
from typing import Dict, Optional

import requests

_logger = logging.getLogger(__name__)

# Repo root, so `shared/bin/...` resolves regardless of the working directory.
# Mirrors the anchor used by evomem_client._resolve_binary(); kept local here to
# avoid importing backend.agent_runtime (which spins up the runtime at import).
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EVOMEM_REPO = "anvie/evomem"

# Platform -> substring identifying the matching release asset (Rust target
# triple). Asset names are matched by substring, so new arches added to a future
# release are picked up automatically; an unmatched platform falls back to FTS5.
_ASSET_PATTERNS: Dict[tuple, str] = {
    ("linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("linux", "arm64"): "aarch64-unknown-linux-musl",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("darwin", "arm64"): "aarch64-apple-darwin",
}

# Name of the binary inside the release zip and on disk.
_BINARY_NAME = "evomem"
_API_TIMEOUT = 20
_DOWNLOAD_TIMEOUT = 60


def default_binary_path() -> str:
    """Install path for the evomem binary.

    Honours the EVOMEM_BINARY override so the provisioner writes exactly where
    evomem_client._resolve_binary() reads it; otherwise <repo>/shared/bin/evomem.
    Without this, setting EVOMEM_BINARY would install the binary to a path the
    runtime never consults.
    """
    return os.environ.get("EVOMEM_BINARY") or os.path.join(_BASE_DIR, "shared", "bin", _BINARY_NAME)


def _normalize_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m


def _asset_pattern() -> Optional[str]:
    """Return the asset-name substring for the current platform, or None."""
    return _ASSET_PATTERNS.get((platform.system().lower(), _normalize_arch(platform.machine())))


def _api_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "evonic-evomem-provision",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_release(version: Optional[str]) -> dict:
    """Fetch release metadata from the GitHub API (latest, or a pinned tag)."""
    base = f"https://api.github.com/repos/{EVOMEM_REPO}/releases"
    url = f"{base}/tags/{version}" if version else f"{base}/latest"
    resp = requests.get(url, headers=_api_headers(), timeout=_API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _extract_binary(zip_bytes: bytes) -> bytes:
    """Return the evomem binary bytes from the release zip.

    Reads the named member into memory; never writes zip-controlled paths, so
    there is no archive path-traversal exposure.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        member = None
        for name in zf.namelist():
            if not name.endswith("/") and os.path.basename(name) == _BINARY_NAME:
                member = name
                break
        if member is None:
            raise ValueError(f"release zip does not contain a '{_BINARY_NAME}' binary")
        return zf.read(member)


def _atomic_install(binary: bytes, dest: str) -> None:
    """Write the binary to a temp file in the destination dir, chmod, then rename."""
    dest_dir = os.path.dirname(dest)
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".evomem-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(binary)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _fail(msg: str, level: int = logging.ERROR) -> Dict[str, object]:
    """Log a provisioning failure and return the standard not-installed result.

    Genuine errors (network, checksum mismatch, missing digest, install failure)
    log at ERROR. Expected, non-actionable conditions such as an unsupported
    platform pass level=logging.WARNING to avoid production log noise — the
    runtime falls back to FTS5 regardless.
    """
    _logger.log(level, msg)
    return {"ok": False, "installed": False, "version": None, "msg": msg}


def ensure_evomem(force: bool = False, version: Optional[str] = None) -> Dict[str, object]:
    """Ensure the evomem binary is installed at the canonical path.

    Resolves the latest release by default; pass a tag (or set EVOMEM_VERSION) to
    pin a specific version. Returns {ok, installed, version, msg}. Never raises —
    on any failure (unsupported platform, network error, missing digest, checksum
    mismatch) returns ok=False so callers keep going (FTS5 fallback). Idempotent:
    skips when a binary is already present unless force=True.
    """
    dest = default_binary_path()

    if not force and os.path.isfile(dest) and os.access(dest, os.X_OK):
        return {"ok": True, "installed": False, "version": None,
                "msg": f"evomem already present at {dest}"}

    pattern = _asset_pattern()
    if pattern is None:
        return _fail(f"No prebuilt evomem for {platform.system()}/{platform.machine()}; "
                     f"memory engine will use FTS5.", level=logging.WARNING)

    version = version or os.environ.get("EVOMEM_VERSION") or None
    try:
        release = _fetch_release(version)
    except Exception as e:
        return _fail(f"Failed to query {EVOMEM_REPO} releases: {e}")

    tag = release.get("tag_name") or version or "unknown"
    asset = next(
        (a for a in release.get("assets", [])
         if pattern in a.get("name", "") and a.get("name", "").endswith(".zip")),
        None,
    )
    if asset is None:
        return _fail(f"Release {tag} has no evomem asset for this platform "
                     f"({pattern}); memory engine will use FTS5.", level=logging.WARNING)

    digest = asset.get("digest") or ""
    if not digest.startswith("sha256:"):
        return _fail(f"Release asset {asset.get('name')} has no sha256 digest; "
                     f"refusing to install an unverified binary.")
    expected_sha = digest.split(":", 1)[1]
    url = asset.get("browser_download_url")
    if not url:
        return _fail(f"Release asset {asset.get('name')} has no download URL.")

    try:
        resp = requests.get(url, timeout=_DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        data = resp.content
    except Exception as e:
        return _fail(f"Failed to download evomem from {url}: {e}")

    actual_sha = hashlib.sha256(data).hexdigest()
    if actual_sha != expected_sha:
        return _fail(f"evomem checksum mismatch for {asset.get('name')}: "
                     f"expected {expected_sha}, got {actual_sha}. Aborting install.")

    try:
        binary = _extract_binary(data)
    except (zipfile.BadZipFile, zlib.error, ValueError) as e:
        return _fail(f"Failed to extract evomem binary from {asset.get('name')}: {e}")

    try:
        _atomic_install(binary, dest)
    except OSError as e:
        return _fail(f"Failed to install evomem to {dest}: {e}")

    _logger.info("Installed evomem %s to %s", tag, dest)
    return {"ok": True, "installed": True, "version": tag,
            "msg": f"Installed evomem {tag} to {dest}"}


def main() -> int:
    """Entry point for `python -m backend.evomem_provision` (used by install.sh)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = ensure_evomem(force="--force" in sys.argv)
    print(result["msg"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
