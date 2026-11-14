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


# ── cookie profiles (per-site) ────────────────────────────────────────────────

def _profiles_cfg(mapping):
    return {LP.PROFILES_VAR: json.dumps(mapping)}


def test_parse_cookie_string_skips_junk():
    items = LP.parse_cookie_string("a=1; b=2=3 ;oops; c = x y")
    names = [i["name"] for i in items]
    assert names == ["a", "b", "c"]
    assert items[1]["value"] == "2=3"
    assert items[2]["value"] == "x y"


def test_cookie_profiles_parsing():
    cfg = _profiles_cfg({"x.com": "a=1", "empty.com": "  ", "obj.com": {"cookies": "b=2"}})
    profiles = LP.cookie_profiles(cfg)
    assert set(profiles) == {"x.com", "obj.com"}
    assert profiles["obj.com"] == "b=2"
    # already-parsed dict, and broken JSON, both behave
    assert LP.cookie_profiles({LP.PROFILES_VAR: {"y.com": "c=3"}}) == {"y.com": "c=3"}
    assert LP.cookie_profiles({LP.PROFILES_VAR: "{not json"}) == {}
    assert LP.cookie_profiles({}) == {}


def test_match_profile_host_and_subdomain():
    cfg = _profiles_cfg({"x.com": "a=1"})
    assert LP.match_profile(cfg, "https://x.com/home")[0] == "x.com"
    assert LP.match_profile(cfg, "https://www.x.com/home")[0] == "x.com"
    assert LP.match_profile(cfg, "https://notx.com/")[0] is None


def test_match_profile_wildcard():
    # Like a browser cookie domain (`.internal.lan`), the wildcard also covers
    # the apex host, but never a lookalike.
    cfg = _profiles_cfg({"*.internal.lan": "s=1"})
    assert LP.match_profile(cfg, "https://app.internal.lan/x")[0] == "*.internal.lan"
    assert LP.match_profile(cfg, "https://internal.lan/x")[0] == "*.internal.lan"
    assert LP.match_profile(cfg, "https://evilinternal.lan/x")[0] is None


def test_match_profile_url_prefix_beats_host():
    cfg = _profiles_cfg({"x.com": "a=1", "https://x.com/i/lists/": "b=2"})
    pattern, raw = LP.match_profile(cfg, "https://x.com/i/lists/2116931999046991892")
    assert pattern == "https://x.com/i/lists/"
    assert raw == "b=2"
    assert LP.match_profile(cfg, "https://x.com/settings")[0] == "x.com"


def test_match_profile_longest_host_wins():
    cfg = _profiles_cfg({"corp.com": "a=1", "app.corp.com": "b=2"})
    assert LP.match_profile(cfg, "https://app.corp.com/")[0] == "app.corp.com"
    assert LP.match_profile(cfg, "https://other.corp.com/")[0] == "corp.com"


def test_cookie_plan_selects_profile_and_materializes_jar(tmp_path, monkeypatch):
    monkeypatch.setenv("LIGHTPANDA_STATE_DIR", str(tmp_path))
    cfg = _profiles_cfg({"httpbin.org": "ho_key=ho_val; ns_key=ns_val"})
    plan = LP.cookie_plan(cfg, "https://httpbin.org/cookies")
    assert plan["source"] == "profile" and plan["profile"] == "httpbin.org"
    assert plan["args"][0] == "--cookie"
    jar = LP.Path(plan["file"])
    assert jar.is_file()
    assert oct(os.stat(jar).st_mode & 0o777) == "0o600"
    assert LP.plan_cookie_names(plan) == ["ho_key", "ns_key"]


def test_cookie_plan_isolation_between_sites(tmp_path, monkeypatch):
    monkeypatch.setenv("LIGHTPANDA_STATE_DIR", str(tmp_path))
    cfg = _profiles_cfg({"httpbin.org": "a=1", "example.com": "b=2"})
    one = LP.cookie_plan(cfg, "https://httpbin.org/cookies")
    two = LP.cookie_plan(cfg, "https://example.com/")
    assert LP.plan_cookie_names(one) == ["a"]
    assert LP.plan_cookie_names(two) == ["b"]


def test_cookie_plan_use_false_and_prefer():
    cfg = _profiles_cfg({"x.com": "a=1"})
    assert LP.cookie_plan(cfg, "https://x.com/", use=False)["args"] == []
    forced = LP.cookie_plan(cfg, "https://elsewhere.org/", prefer="x.com")
    assert forced["profile"] == "x.com"
    assert LP.cookie_plan(cfg, "https://elsewhere.org/", prefer="nope.com")["source"] is None


def test_cookie_plan_falls_back_to_legacy_jar(tmp_path, monkeypatch):
    monkeypatch.setenv("LIGHTPANDA_STATE_DIR", str(tmp_path))
    legacy = tmp_path / "cookies.json"
    legacy.write_text(json.dumps([{"name": "old", "value": "1", "domain": "nowhere.org"}]), encoding="utf-8")
    plan = LP.cookie_plan({LP.PROFILES_VAR: "{}"}, "https://nowhere.org/",
                          save_jar=True, )
    assert plan["source"] == "jar"
    assert LP.plan_cookie_names(plan) == ["old"]
    assert "--cookie-jar" in plan["args"]


def test_manifest_declares_per_site_cookie_profiles():
    manifest = json.loads((SKILL_DIR / "skill.json").read_text(encoding="utf-8"))
    variables = {v["name"]: v for v in manifest["variables"]}
    profiles = variables["lightpanda_cookie_profiles"]
    assert profiles["type"] == "cookies"
    assert profiles["default"] == "{}"
    assert profiles["label"]
    # the legacy single jar stays available as a fallback
    assert variables["lightpanda_cookie_file"]["type"] == "string"


def test_settings_page_renders_the_cookies_widget():
    source = (ROOT / "templates" / "skill_detail.html").read_text(encoding="utf-8")
    assert "v.type === 'cookies'" in source
    assert "data-cookies-widget" in source
    # the widget must stay in sync with a plain [data-var] input so that the
    # existing capture/save path needs no special casing
    assert "function syncCookiesWidget(widget)" in source
    assert "input type=\"hidden\" data-var=" in source or "input type='hidden' data-var=" in source
