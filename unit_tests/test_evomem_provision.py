"""
Unit tests for backend.evomem_provision.

Network access is fully mocked: _fetch_release and requests.get are patched so no
real GitHub call or download occurs.
"""
import hashlib
import io
import logging
import os
import zipfile

from backend import evomem_provision as ep


def _make_zip(binary=b"#!/bin/sh\necho evomem\n", member="evomem"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, binary)
    return buf.getvalue()


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def _release(name, sha=None, url="https://example.invalid/evomem.zip", tag="v9.9.9"):
    asset = {"name": name, "browser_download_url": url}
    if sha is not None:
        asset["digest"] = "sha256:" + sha
    return {"tag_name": tag, "assets": [asset]}


def test_default_binary_path(monkeypatch):
    monkeypatch.delenv("EVOMEM_BINARY", raising=False)
    assert ep.default_binary_path().endswith(os.path.join("shared", "bin", "evomem"))


def test_default_binary_path_honours_env_override(monkeypatch):
    # Provisioner must install where evomem_client._resolve_binary() reads from.
    monkeypatch.setenv("EVOMEM_BINARY", "/custom/path/evomem")
    assert ep.default_binary_path() == "/custom/path/evomem"


def test_normalize_arch():
    assert ep._normalize_arch("AMD64") == "x86_64"
    assert ep._normalize_arch("x86_64") == "x86_64"
    assert ep._normalize_arch("aarch64") == "arm64"
    assert ep._normalize_arch("arm64") == "arm64"


def test_asset_pattern_known_platform(monkeypatch):
    monkeypatch.setattr(ep.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ep.platform, "machine", lambda: "x86_64")
    assert ep._asset_pattern() == "x86_64-unknown-linux-musl"


def test_idempotent_skip_when_present(tmp_path, monkeypatch):
    dest = tmp_path / "evomem"
    dest.write_bytes(b"x")
    os.chmod(dest, 0o755)
    monkeypatch.setattr(ep, "default_binary_path", lambda: str(dest))
    result = ep.ensure_evomem()
    assert result["ok"] and not result["installed"]


def test_unsupported_platform_falls_back(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(ep, "default_binary_path", lambda: str(tmp_path / "evomem"))
    monkeypatch.setattr(ep, "_asset_pattern", lambda: None)
    with caplog.at_level(logging.DEBUG, logger=ep._logger.name):
        result = ep.ensure_evomem()
    assert not result["ok"]
    assert "FTS5" in result["msg"]
    # Expected, non-actionable condition: logged at WARNING, not ERROR (log noise).
    levels = [r.levelno for r in caplog.records]
    assert logging.WARNING in levels
    assert logging.ERROR not in levels


def test_checksum_mismatch_aborts_install(tmp_path, monkeypatch, caplog):
    dest = tmp_path / "evomem"
    monkeypatch.setattr(ep, "default_binary_path", lambda: str(dest))
    monkeypatch.setattr(ep, "_asset_pattern", lambda: "x86_64-unknown-linux-musl")
    monkeypatch.setattr(ep, "_fetch_release",
                        lambda v: _release("evomem-x86_64-unknown-linux-musl.zip", sha="0" * 64))
    monkeypatch.setattr(ep.requests, "get", lambda url, timeout=0: _Resp(_make_zip()))
    with caplog.at_level(logging.DEBUG, logger=ep._logger.name):
        result = ep.ensure_evomem(force=True)
    assert not result["ok"]
    assert "checksum mismatch" in result["msg"]
    assert not dest.exists()
    # Genuine integrity failure stays at ERROR — must not be downgraded.
    assert logging.ERROR in [r.levelno for r in caplog.records]


def test_missing_digest_fails_closed(tmp_path, monkeypatch):
    dest = tmp_path / "evomem"
    monkeypatch.setattr(ep, "default_binary_path", lambda: str(dest))
    monkeypatch.setattr(ep, "_asset_pattern", lambda: "x86_64-unknown-linux-musl")
    monkeypatch.setattr(ep, "_fetch_release",
                        lambda v: _release("evomem-x86_64-unknown-linux-musl.zip", sha=None))
    result = ep.ensure_evomem(force=True)
    assert not result["ok"]
    assert "digest" in result["msg"]
    assert not dest.exists()


def test_successful_install(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "evomem"
    binary = b"\x7fELF fake evomem binary"
    zip_bytes = _make_zip(binary)
    sha = hashlib.sha256(zip_bytes).hexdigest()
    monkeypatch.setattr(ep, "default_binary_path", lambda: str(dest))
    monkeypatch.setattr(ep, "_asset_pattern", lambda: "x86_64-unknown-linux-musl")
    monkeypatch.setattr(ep, "_fetch_release",
                        lambda v: _release("evomem-1.2.3-x86_64-unknown-linux-musl.zip",
                                           sha=sha, tag="v1.2.3"))
    monkeypatch.setattr(ep.requests, "get", lambda url, timeout=0: _Resp(zip_bytes))
    result = ep.ensure_evomem(force=True)
    assert result["ok"] and result["installed"]
    assert result["version"] == "v1.2.3"
    assert dest.exists() and os.access(dest, os.X_OK)
    assert dest.read_bytes() == binary
