"""lightpanda_cookies — manage per-site cookie profiles.

Profiles live in the skill config variable ``lightpanda_cookie_profiles``, i.e.
exactly what the skill settings page edits, so the agent and the UI always see
the same data. Cookie values are credentials: they are never echoed back in a
tool result, never logged, and the materialized jars on disk are 0600.
"""

import json
import os

from ._lp import (
    PROFILES_VAR,
    SKILL_ID,
    LightpandaError,
    config,
    cookie_file,
    cookie_plan,
    cookie_profiles,
    match_profile,
    plan_cookie_names,
    profile_items,
    profile_slug,
    run_cli,
    truncate,
    validate_url,
)

ACTIONS = ("status", "set", "delete", "clear", "verify")
VERIFY_URL = "https://httpbin.org/cookies"


# ── config plumbing ──────────────────────────────────────────────────────────

def _write_profiles(profiles: dict) -> dict:
    """Persist the profiles map into the skill config (the settings-page value)."""
    payload = json.dumps(profiles, ensure_ascii=False, indent=2)
    try:
        from backend.skills_manager import skills_manager  # type: ignore
        result = skills_manager.set_skill_config(SKILL_ID, {PROFILES_VAR: payload})
        if isinstance(result, dict) and result.get("success"):
            return {"stored_in": "skill_config"}
        return {"stored_in": "failed", "error": str(result)}
    except Exception as e:  # noqa: BLE001 — keep the tool usable outside Evonic
        return {"stored_in": "failed", "error": f"{type(e).__name__}: {e}"}


def _profile_meta(pattern: str, raw: str) -> dict:
    """Describe one profile without leaking values."""
    try:
        items = profile_items(pattern, raw, pattern if "://" in pattern else None)
        names = sorted({c["name"] for c in items})
        count = len(items)
    except LightpandaError as e:
        return {"site": pattern, "valid": False, "error": str(e)}
    return {"site": pattern, "valid": True, "cookies": count, "names": names}


# ── actions ──────────────────────────────────────────────────────────────────

def _status(args: dict) -> dict:
    cfg = config()
    profiles = cookie_profiles(cfg)
    rows = [_profile_meta(p, raw) for p, raw in sorted(profiles.items())]
    legacy = cookie_file(cfg)
    out = {
        "action": "status",
        "profiles": rows,
        "profile_count": len(rows),
        "config_var": PROFILES_VAR,
        "legacy_jar": {"path": str(legacy), "exists": legacy.is_file()},
    }
    if not rows:
        out["hint"] = ("No per-site profiles yet. Add them in Settings → Lightpanda → "
                       "Cookie profiles, or call action='set' with site + cookies.")
    out["match_order"] = ("Most specific pattern wins: full URL prefix > longest host > "
                          "wildcard host > bare host. No match falls back to the legacy jar.")
    out["applies_to"] = ("lightpanda_fetch only. lightpanda_run and MCP sessions "
                         "(lightpanda_read/act) ignore --cookie in Lightpanda 1.0.4.")
    return out


def _set(args: dict) -> dict:
    site = str(args.get("site") or args.get("pattern") or "").strip()
    if not site:
        raise LightpandaError("'site' is required, e.g. site='x.com' or site='https://x.com/i/lists/'")

    file_path = args.get("file")
    source = None
    if file_path:
        path = os.path.expanduser(str(file_path))
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError as e:
            raise LightpandaError(f"cannot read cookie file '{path}': {e}") from e
    elif args.get("netscape"):
        source = str(args["netscape"])
    elif args.get("cookies") is not None:
        source = args["cookies"]
    else:
        raise LightpandaError("provide 'cookies' (string / Netscape / JSON), 'netscape' or 'file'")

    if isinstance(source, (list, dict)):
        source = json.dumps(source, ensure_ascii=False)
    source = str(source).strip()
    if not source:
        raise LightpandaError("cookie payload is empty")

    items = profile_items(site, source, site if "://" in site else None)
    names = sorted({c["name"] for c in items})

    profiles = cookie_profiles(config())
    profiles[site] = source
    saved = _write_profiles(profiles)
    if saved.get("stored_in") == "failed":
        raise LightpandaError(f"could not save the profile: {saved.get('error')}")

    return {
        "action": "set",
        "site": site,
        "stored": len(items),
        "names": names,
        "profile_count": len(profiles),
        **saved,
        "note": ("Values are stored, not echoed. Selected automatically by "
                 "lightpanda_fetch for URLs matching this pattern; run action='verify' to prove it."),
    }


def _delete(args: dict) -> dict:
    site = str(args.get("site") or args.get("pattern") or "").strip()
    if not site:
        raise LightpandaError("'site' is required")
    cfg = config()
    profiles = cookie_profiles(cfg)
    if site not in profiles:
        return {"action": "delete", "site": site, "deleted": False,
                "reason": "no such profile", "known": sorted(profiles)}
    profiles.pop(site)
    saved = _write_profiles(profiles)
    if saved.get("stored_in") == "failed":
        raise LightpandaError(f"could not save: {saved.get('error')}")
    # Drop the materialized jar too, so a stale file can never be sent.
    jar = cookie_file(cfg).parent / "cookies" / f"{profile_slug(site)}.json"
    removed_jar = False
    try:
        jar.unlink()
        removed_jar = True
    except OSError:
        pass
    return {"action": "delete", "site": site, "deleted": True, "jar_removed": removed_jar,
            "remaining": sorted(profiles)}


def _clear(args: dict) -> dict:
    confirmed = args.get("confirm") is True or str(args.get("confirm")).lower() in ("true", "yes", "1")
    if not confirmed:
        raise LightpandaError("confirm=true is required to delete cookies")
    profiles = cookie_profiles(config())
    removed = len(profiles)
    saved = _write_profiles({})
    if saved.get("stored_in") == "failed":
        raise LightpandaError(f"could not save: {saved.get('error')}")
    return {"action": "clear", "cleared": removed, "profiles_left": 0}


def _verify(args: dict) -> dict:
    cfg = config()
    url = validate_url(args.get("url") or VERIFY_URL)
    pattern, _raw = match_profile(cfg, url)
    plan = cookie_plan(cfg, url, save_jar=False)

    cmd = ["fetch", "--dump", "markdown", "--json", "--dump-max-bytes", "60000"]
    cmd += plan["args"]
    cmd += ["--wait-ms", str(int(args.get("wait_ms") or 1500)), url]
    proc = run_cli(cmd, timeout=float(args.get("timeout") or 120))

    payload = {}
    text = (proc.stdout or "").strip()
    if text:
        try:
            data = json.loads(text)
            payload = data.get("results", [data])[0] if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            payload = {}
    body = str(payload.get("content") or "")
    names = plan_cookie_names(plan)

    out = {
        "action": "verify",
        "url": url,
        "profile": plan["profile"],
        "source": plan["source"],
        "cookies_attached": names,
        "cookie_count": len(names),
        "http_status": payload.get("http_status"),
        "bytes": len(body.encode("utf-8")),
    }
    expect = args.get("expect")
    if expect:
        out["expect"] = str(expect)
        out["expect_found"] = str(expect) in body
    if not pattern:
        out["hint"] = ("No profile matches this URL; the legacy jar was used (if present). "
                       f"Known sites: {sorted(cookie_profiles(cfg))}")
    elif payload.get("http_status") in (401, 403, 302):
        out["hint"] = "Cookies were sent but the site still refuses this session — they are probably expired."
    if args.get("show_body"):
        out["body"], _ = truncate(body, 2000)
    return out


def execute(agent: dict, args: dict) -> dict:
    try:
        args = args or {}
        action = str(args.get("action") or "status").strip().lower()
        if action not in ACTIONS:
            raise LightpandaError(f"unsupported action '{action}'; use one of {list(ACTIONS)}")
        return {"status": _status, "set": _set, "delete": _delete,
                "clear": _clear, "verify": _verify}[action](args)
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — never break the agent turn
        return {"error": f"{type(e).__name__}: {e}"}
