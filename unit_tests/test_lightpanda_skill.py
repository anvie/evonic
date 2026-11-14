"""Offline tests for the Lightpanda skill: manifest wiring and shared helpers."""

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "lightpanda"
TOOLS_DIR = SKILL_DIR / "backend" / "tools"


def _load_lp():
    name = "evonic_lightpanda_lp_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / "_lp.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LP = _load_lp()


# ── manifest wiring ─────────────────────────────────────────────────────────

def test_manifest_declares_doctor_requirements():
    manifest = json.loads((SKILL_DIR / "skill.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "lightpanda"
    assert manifest["lazy_tools"] is True
    binaries = manifest["requirements"]["binaries"]
    entry = next(b for b in binaries if b["name"] == "lightpanda")
    assert entry["fix_script"] == "scripts/install.sh"
    assert entry["version_command"] == "lightpanda version"
    assert "bin" in entry["search_paths"]
    assert (SKILL_DIR / entry["fix_script"]).is_file()
    assert os.access(SKILL_DIR / entry["fix_script"], os.X_OK)


def test_tools_json_matches_backend_modules():
    defs = json.loads((SKILL_DIR / "tools.json").read_text(encoding="utf-8"))
    names = [d["function"]["name"] for d in defs]
    assert len(names) == len(set(names))

    for name in names:
        backend = TOOLS_DIR / f"{name}.py"
        assert backend.is_file(), f"missing backend for {name}"
        assert "def execute(" in backend.read_text(encoding="utf-8")

    backends = {p.stem for p in TOOLS_DIR.glob("*.py")
                if not p.stem.startswith("_") and p.stem != "__init__"}
    assert backends == set(names)


def test_doctor_hook_exposes_check_and_fix():
    path = SKILL_DIR / "backend" / "doctor.py"
    source = path.read_text(encoding="utf-8")
    assert "def doctor_check(" in source
    assert "def doctor_fix(" in source


# ── URL safety ──────────────────────────────────────────────────────────────

def test_allows_public_https():
    assert LP.validate_url("https://example.com/a?b=1") == "https://example.com/a?b=1"


def test_rejects_non_http_schemes():
    for url in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", ""):
        try:
            LP.validate_url(url)
        except LP.LightpandaError:
            continue
        raise AssertionError(f"should reject {url!r}")


def test_rejects_private_targets_by_default():
    cfg = {"lightpanda_block_private_networks": True}
    for url in ("http://127.0.0.1/", "http://10.1.2.3/", "http://localhost:8080/",
                "http://printer.local/", "http://[::1]/"):
        try:
            LP.validate_url(url, cfg)
        except LP.LightpandaError:
            continue
        raise AssertionError(f"should reject {url!r}")


def test_private_targets_allowed_when_guard_disabled():
    cfg = {"lightpanda_block_private_networks": False}
    assert LP.validate_url("http://127.0.0.1:8080/x", cfg).startswith("http://127.0.0.1")


# ── output shaping ──────────────────────────────────────────────────────────

def test_truncate_adds_marker_only_when_capped():
    text = "abcdef"
    assert LP.truncate(text, 100) == (text, False)
    capped, truncated = LP.truncate(text * 100, 50)
    assert truncated is True
    assert "[truncated at 50 bytes]" in capped
    capped.encode("utf-8")  # must stay valid UTF-8


def test_truncate_does_not_split_multibyte_characters():
    capped, truncated = LP.truncate("é" * 100, 51)
    assert truncated is True
    assert capped.startswith("é")


def test_shape_result_flattens_blocks_and_hides_images():
    result = {
        "content": [
            {"type": "text", "text": "line one"},
            {"type": "image", "mimeType": "image/png", "data": "AAA"},
            {"type": "text", "text": "line two"},
        ],
        "structuredContent": {"url": "https://example.com"},
    }
    out = LP.shape_result(result, tool="markdown")
    assert out["text"] == "line one\nline two"
    assert out["structured"] == {"url": "https://example.com"}
    assert out["image_count"] == 1
    assert "images" not in out
    assert out["is_error"] is False


def test_shape_result_includes_images_on_request():
    result = {"content": [{"type": "image", "mimeType": "image/png", "data": "AAA"}]}
    out = LP.shape_result(result, include_images=True)
    assert out["images"][0]["data"] == "AAA"


def test_shape_result_caps_text():
    result = {"content": [{"type": "text", "text": "x" * 500}]}
    out = LP.shape_result(result, max_bytes=100)
    assert out["truncated"] is True
    assert "[truncated at 100 bytes]" in out["text"]


# ── configuration and paths ─────────────────────────────────────────────────

def test_binary_override_from_env(tmp_path, monkeypatch):
    fake = tmp_path / "lightpanda"
    fake.write_text("#!/bin/sh\necho 9.9.9\n", encoding="utf-8")
    os.chmod(fake, 0o755)
    monkeypatch.setenv("LIGHTPANDA_BIN", str(fake))
    assert LP.resolve_binary({}) == fake.resolve()


def test_binary_missing_when_nothing_available(monkeypatch):
    monkeypatch.setattr(LP.shutil, "which", lambda name: None)
    assert LP.resolve_binary({"lightpanda_bin": "/nonexistent/lightpanda"}) is None


def test_state_dir_honours_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LIGHTPANDA_STATE_DIR", str(tmp_path / "state"))
    assert LP.state_dir() == tmp_path / "state"
    assert LP.state_dir().is_dir()


def test_server_args_reflect_safety_settings():
    args = LP._server_args(Path("/bin/lightpanda"), {"lightpanda_obey_robots": True})
    assert "--obey-robots" in args
    assert "--block-private-networks" in args
    args = LP._server_args(Path("/bin/lightpanda"),
                           {"lightpanda_obey_robots": False,
                            "lightpanda_block_private_networks": False})
    assert "--obey-robots" not in args
    assert "--block-private-networks" not in args
