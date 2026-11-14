"""Offline tests for the Lightpanda skill: manifest wiring and shared helpers."""

import importlib.util
import json
import os
import sys
import types
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


# ── cookies from a browser dump ─────────────────────────────────────────────

DUMP = (
    "# Netscape HTTP Cookie File\n"
    ".x.com\tTRUE\t/\tTRUE\t1790000000\tauth_token\tsecret1\n"
    "#HttpOnly_.x.com\tTRUE\t/\tTRUE\t1790000000\tct0\tsecret2\n"
    "httpbin.org\tFALSE\t/\tFALSE\t0\tho_key\talpha\n"
)


def _dump_cfg(text, **extra):
    return {LP.COOKIES_VAR: text, **extra}


PKG = "evonic_lightpanda_toolpkg"


def _load_tool(name):
    """Import a tool module standalone, reusing the already-loaded `_lp`.

    The skill’s directory is not on sys.path in tests, so a synthetic package
    stands in for `backend.tools` and `from ._lp import ...` resolves to LP.
    """
    mod_name = f"{PKG}.{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(TOOLS_DIR)]
        sys.modules[PKG] = pkg
    sys.modules[f"{PKG}._lp"] = LP
    spec = importlib.util.spec_from_file_location(mod_name, TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_cookie_string_skips_junk():
    items = LP.parse_cookie_string("a=1; b=2=3 ;oops; c = x y")
    names = [i["name"] for i in items]
    assert names == ["a", "b", "c"]
    assert items[1]["value"] == "2=3"
    assert items[2]["value"] == "x y"


def test_parse_cookie_text_reads_netscape_dump():
    items = LP.parse_cookie_text(DUMP)
    assert [(c["domain"], c["name"]) for c in items] == [
        (".x.com", "auth_token"), (".x.com", "ct0"), ("httpbin.org", "ho_key")]
    # `#HttpOnly_` is a flag, not part of the domain
    assert items[1]["httpOnly"] is True and items[0]["httpOnly"] is False
    assert items[0]["secure"] is True and items[2]["secure"] is False
    assert "expires" not in items[2]          # expiry 0 = session cookie
    assert items[0]["expires"] == 1790000000


def test_parse_cookie_text_accepts_json_and_rejects_junk():
    assert LP.parse_cookie_text('[{"name":"a","value":"1","domain":"x.com"}]')[0]["name"] == "a"
    assert LP.parse_cookie_text([{"name": "b", "value": "2", "domain": "y.com"}])[0]["name"] == "b"
    for junk in ("", "just some words", "a=1; b=2"):
        try:
            LP.parse_cookie_text(junk)
        except LP.LightpandaError as e:
            assert "cookie" in str(e).lower()
        else:
            raise AssertionError(f"should reject {junk!r}")


def test_header_payload_needs_a_site():
    items = LP.cookies_from_header("https://x.com/i/lists/1", "auth_token=abc; ct0=def")
    assert [(c["domain"], c["name"]) for c in items] == [(".x.com".lstrip("."), "auth_token"),
                                                         ("x.com", "ct0")]
    try:
        LP.cookies_from_header("", "a=1")
    except LP.LightpandaError:
        pass
    else:
        raise AssertionError("a header payload without a host must fail")


def test_to_netscape_roundtrip_keeps_flags():
    items = LP.parse_cookie_text(DUMP)
    again = LP.parse_cookie_text(LP.to_netscape(items))
    assert again == items
    text = LP.to_netscape([{**items[0], "value": "has\ttab"}])
    assert "\t" not in text.splitlines()[1].split("\t")[6]
    assert text.startswith(LP.NETSCAPE_HEADER)


def test_merge_cookies_overwrites_and_keeps_order():
    old = LP.parse_cookie_text(DUMP)
    new = [{"name": "ho_key", "value": "beta", "domain": "httpbin.org",
            "path": "/", "secure": False, "httpOnly": False},
           {"name": "fresh", "value": "1", "domain": "other.org",
            "path": "/", "secure": False, "httpOnly": False}]
    merged = LP.merge_cookies(old, new)
    assert [c["name"] for c in merged] == ["auth_token", "ct0", "ho_key", "fresh"]
    assert next(c for c in merged if c["name"] == "ho_key")["value"] == "beta"


def test_domain_matches_is_suffix_safe():
    assert LP.domain_matches(".x.com", "x.com")
    assert LP.domain_matches(".x.com", "www.x.com")
    assert LP.domain_matches("httpbin.org", "httpbin.org")
    assert not LP.domain_matches(".x.com", "notx.com")
    assert not LP.domain_matches(".x.com", "")
    assert not LP.domain_matches("sub.x.com", "x.com")


def test_cookies_for_url_filters_by_host():
    items = LP.parse_cookie_text(DUMP)
    assert [c["name"] for c in LP.cookies_for_url(items, "https://httpbin.org/cookies")] == ["ho_key"]
    assert [c["name"] for c in LP.cookies_for_url(items, "https://x.com/home")] == [
        "auth_token", "ct0"]
    assert LP.cookies_for_url(items, "https://elsewhere.org/") == []


def test_dump_cookies_tolerates_broken_text():
    assert LP.dump_cookies({LP.COOKIES_VAR: "garbage"}) == []
    assert LP.dump_cookies({}) == []
    assert len(LP.dump_cookies(_dump_cfg(DUMP))) == 3


def test_cookie_plan_materializes_the_dump(tmp_path, monkeypatch):
    monkeypatch.setenv("LIGHTPANDA_STATE_DIR", str(tmp_path / "state"))
    plan = LP.cookie_plan(_dump_cfg(DUMP))
    assert plan["source"] == "dump"
    assert plan["args"][:2] == ["--cookie", str(LP.Path(plan["file"]))]
    jar = LP.Path(plan["file"])
    assert jar.is_file()
    assert oct(os.stat(jar).st_mode & 0o777) == "0o600"
    assert LP.plan_cookie_names(plan) == ["auth_token", "ct0", "ho_key"]
    assert plan["domains"] == [".x.com", "httpbin.org"]
    # the dump is the source of truth: no jar is written back over it
    assert "--cookie-jar" not in LP.cookie_plan(_dump_cfg(DUMP), save_jar=True)["args"]


def test_cookie_plan_use_false_and_empty_dump(tmp_path, monkeypatch):
    monkeypatch.setenv("LIGHTPANDA_STATE_DIR", str(tmp_path / "state"))
    assert LP.cookie_plan(_dump_cfg(DUMP), use=False)["args"] == []
    empty = LP.cookie_plan(_dump_cfg("   "))
    assert empty == {"args": [], "source": None, "file": None, "names": [], "domains": []}


def test_cookie_plan_falls_back_to_legacy_jar(tmp_path, monkeypatch):
    monkeypatch.setenv("LIGHTPANDA_STATE_DIR", str(tmp_path))
    legacy = tmp_path / "cookies.json"
    legacy.write_text(json.dumps([{"name": "old", "value": "1", "domain": "nowhere.org"}]),
                      encoding="utf-8")
    plan = LP.cookie_plan({}, save_jar=True)
    assert plan["source"] == "jar"
    assert LP.plan_cookie_names(plan) == ["old"]
    assert "--cookie-jar" in plan["args"]


def test_manifest_declares_the_cookie_textarea():
    manifest = json.loads((SKILL_DIR / "skill.json").read_text(encoding="utf-8"))
    variables = {v["name"]: v for v in manifest["variables"]}
    dump = variables["lightpanda_cookies_txt"]
    assert dump["type"] == "textarea"
    assert dump["default"] == ""
    assert dump["label"] and "cookies.txt" in dump["description"]
    assert dump["monospace"] is True and dump["rows"] >= 10
    assert "lightpanda_cookie_profiles" not in variables
    # the legacy single jar stays available as a fallback
    assert variables["lightpanda_cookie_file"]["type"] == "string"


def test_tools_json_drops_the_profile_param():
    defs = json.loads((SKILL_DIR / "tools.json").read_text(encoding="utf-8"))
    fetch = next(d for d in defs if d["function"]["name"] == "lightpanda_fetch")
    assert "profile" not in fetch["function"]["parameters"]["properties"]
    cookies = next(d for d in defs if d["function"]["name"] == "lightpanda_cookies")
    props = cookies["function"]["parameters"]["properties"]
    assert props["action"]["enum"] == ["status", "set", "delete", "clear", "verify"]
    assert set(props) >= {"cookies", "site", "merge", "file", "confirm"}
    assert "netscape" not in props


def test_settings_page_has_no_cookies_widget_left():
    source = (ROOT / "templates" / "skill_detail.html").read_text(encoding="utf-8")
    assert "data-cookies-widget" not in source
    assert "syncCookiesWidget" not in source
    assert "=== 'cookies'" not in source
    # a dump textarea is just the generic textarea renderer plus manifest hints
    assert "v.rows || 8" in source and "v.monospace" in source


# ── tool behaviour ──────────────────────────────────────────────────────────

def _tool_with_store(tmp_path, monkeypatch):
    """Load the tool with config reads/writes backed by an in-memory dict."""
    monkeypatch.setenv("LIGHTPANDA_STATE_DIR", str(tmp_path / "state"))
    tool = _load_tool("lightpanda_cookies")
    store = {"text": ""}
    monkeypatch.setattr(tool, "_write_dump",
                        lambda text: (store.update(text=text), {"stored_in": "skill_config"})[1])
    monkeypatch.setattr(tool, "config", lambda: {tool.COOKIES_VAR: store["text"]})
    monkeypatch.setattr(tool, "cookie_file", lambda cfg=None: tmp_path / "legacy.json")
    return tool, store


def test_tool_set_status_delete_clear(tmp_path, monkeypatch):
    tool, store = _tool_with_store(tmp_path, monkeypatch)

    out = tool.execute({}, {"action": "set", "cookies": DUMP})
    assert out["stored"] == 3 and out["mode"] == "replace"
    assert out["domains"] == [".x.com", "httpbin.org"]
    assert "secret1" not in json.dumps(out)          # values never leak
    assert not LP.cookie_file({"lightpanda_cookie_file": str(tmp_path / "x")}).is_file()

    status = tool.execute({}, {"action": "status"})
    assert status["cookie_count"] == 3 and status["domain_count"] == 2
    assert {d["domain"] for d in status["domains"]} == {".x.com", "httpbin.org"}
    assert "secret1" not in json.dumps(status)

    # merging a single-domain dump keeps the others
    merged = tool.execute({}, {"action": "set", "cookies": DUMP, "merge": True})
    assert merged["mode"] == "merge" and merged["stored"] == 3

    deleted = tool.execute({}, {"action": "delete", "site": "x.com"})
    assert deleted["deleted"] is True and deleted["removed"] == 2
    assert deleted["remaining_domains"] == ["httpbin.org"]

    assert "confirm" in tool.execute({}, {"action": "clear"})["error"]
    cleared = tool.execute({}, {"action": "clear", "confirm": True})
    assert cleared["removed"] == 1 and store["text"] == ""
    assert tool.execute({}, {"action": "status"})["configured"] is False


def test_tool_set_accepts_header_with_site(tmp_path, monkeypatch):
    tool, store = _tool_with_store(tmp_path, monkeypatch)
    out = tool.execute({}, {"action": "set", "site": "x.com",
                            "cookies": "auth_token=abc; ct0=def"})
    assert out["names"] == ["auth_token", "ct0"]
    assert all(line.split("\t")[0].endswith("x.com") for line in store["text"].splitlines()[1:])
    # a bare header without a site is refused with a pointer to `site`
    err = tool.execute({}, {"action": "set", "cookies": "auth_token=abc"})
    assert "site" in err["error"]


def test_tool_set_rejects_empty_payload(tmp_path, monkeypatch):
    tool, _store = _tool_with_store(tmp_path, monkeypatch)
    assert "empty" in tool.execute({}, {"action": "set", "cookies": "   "})["error"]
    assert "error" in tool.execute({}, {"action": "set"})


def test_tool_status_reports_unparseable_dump(tmp_path, monkeypatch):
    tool, store = _tool_with_store(tmp_path, monkeypatch)
    store["text"] = "this is not a dump"
    status = tool.execute({}, {"action": "status"})
    assert status["cookie_count"] == 0 and "error" in status
    assert "dump" in status["hint"].lower()
