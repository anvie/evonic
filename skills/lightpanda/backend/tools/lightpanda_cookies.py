"""lightpanda_cookies — paste and inspect the browser cookie dump.

The dump lives in the skill config variable ``lightpanda_cookies_txt``, i.e.
exactly the textarea on the skill settings page, so the agent and the UI always
see the same data. A dump is a Netscape ``cookies.txt``: seven tab-separated
columns per line (domain, includeSubdomains, path, secure, expiry, name, value),
so no per-site pattern list is needed — Lightpanda filters the jar per host
while browsing.

Cookie values are credentials: they are never echoed in a tool result, never
logged, and the materialized jars on disk are 0600.
"""

import json
import os
import time

from ._lp import (
    COOKIE_DIRNAME,
    COOKIES_VAR,
    DUMP_FILENAME,
    SKILL_ID,
    LightpandaError,
    config,
    cookie_domains,
    cookie_file,
    cookie_plan,
    cookie_text,
    cookies_for_url,
    cookies_from_header,
    domain_matches,
    dump_cookies,
    merge_cookies,
    parse_cookie_text,
    run_cli,
    state_dir,
    to_netscape,
    truncate,
    validate_url,
    _pattern_host,
)

ACTIONS = ("status", "set", "delete", "clear", "verify")
VERIFY_URL = "https://httpbin.org/cookies"


# ── config plumbing ─────────────────────────────────────────────────────────

def _write_dump(text: str) -> dict:
    """Persist the dump text into the skill config (the settings-page value)."""
    try:
        from backend.skills_manager import skills_manager  # type: ignore
        result = skills_manager.set_skill_config(SKILL_ID, {COOKIES_VAR: text})
        if isinstance(result, dict) and result.get("success"):
            return {"stored_in": "skill_config"}
        return {"stored_in": "failed", "error": str(result)}
    except Exception as e:  # noqa: BLE001 — keep the tool usable outside Evonic
        return {"stored_in": "failed", "error": f"{type(e).__name__}: {e}"}


def _store(text: str) -> dict:
    saved = _write_dump(text)
    if saved.get("stored_in") == "failed":
        raise LightpandaError(f"could not save the cookie dump: {saved.get('error')}")
    # A stale jar must never outlive the text it came from.
    jar = state_dir() / COOKIE_DIRNAME / DUMP_FILENAME
    if not text.strip():
        try:
            jar.unlink()
        except OSError:
            pass
    return saved


def _payload(args: dict) -> str:
    """Raw dump text from `file` or the `cookies` argument."""
    file_path = args.get("file")
    if file_path:
        path = os.path.expanduser(str(file_path))
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            raise LightpandaError(f"cannot read cookie file '{path}': {e}") from e
    source = args.get("cookies")
    if source is None:
        source = args.get("dump")
    if source is None:
        raise LightpandaError(
            "provide 'cookies' (the cookies.txt dump) or 'file' (path to it)")
    if isinstance(source, (list, dict)):
        source = json.dumps(source, ensure_ascii=False)
    return str(source)


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def _expiry_view(items) -> dict:
    now = time.time()
    stamps = [c["expires"] for c in items if c.get("expires")]
    expired = sum(1 for s in stamps if s < now)
    out = {"expired": expired}
    if stamps:
        soonest = min(s for s in stamps if s >= now) if any(s >= now for s in stamps) else None
        out["soonest_expiry"] = (time.strftime("%Y-%m-%d", time.localtime(soonest))
                                 if soonest else None)
    return out


# ── actions ─────────────────────────────────────────────────────────────────

def _status(args: dict) -> dict:
    cfg = config()
    text = cookie_text(cfg)
    items, error = [], None
    if text:
        try:
            items = parse_cookie_text(text)
        except LightpandaError as e:
            error = str(e)
    domains = cookie_domains(items)
    jar = state_dir() / COOKIE_DIRNAME / DUMP_FILENAME
    out = {
        "action": "status",
        "config_var": COOKIES_VAR,
        "configured": bool(text),
        "storage": {"lines": len(text.splitlines()) if text else 0,
                    "bytes": len(text.encode("utf-8")),
                    "jar": {"path": str(jar), "exists": jar.is_file()}},
        "cookie_count": len(items),
        "domain_count": len(domains),
        "domains": [{"domain": d, "cookies": len(names), "names": names}
                    for d, names in domains.items()],
        "http_only": sum(1 for c in items if c.get("httpOnly")),
        "secure": sum(1 for c in items if c.get("secure")),
        **_expiry_view(items),
    }
    if error:
        out["error"] = error
        out["hint"] = ("The textarea does not parse as a dump. Paste the browser "
                       "export as-is: 7 tab-separated columns per line, starting "
                       "with the domain.")
    elif not items:
        out["hint"] = (f"No cookie dump yet. Paste one into Settings → Lightpanda → "
                       f"Cookies, or call action='set' with cookies=<dump>. "
                       f"Legacy jar at {cookie_file(cfg)} is "
                       f"{'present' if cookie_file(cfg).is_file() else 'absent'}.")
    out["applies_to"] = ("Cookie dumps apply to lightpanda_fetch only. lightpanda_run "
                         "and MCP sessions (lightpanda_read/act) ignore --cookie in "
                         "Lightpanda 1.0.4.")
    return out


def _set(args: dict) -> dict:
    cfg = config()
    source = _payload(args)
    if not source.strip():
        raise LightpandaError("cookie payload is empty")
    site = str(args.get("site") or "").strip()

    if site and "\t" not in source and source[:1] not in ("[", "{"):
        # A bare `auth_token=...; ct0=...` header needs a host to belong to.
        new_items = cookies_from_header(site, source)
    else:
        new_items = parse_cookie_text(source)

    existing = dump_cookies(cfg)
    merged = _truthy(args.get("merge"))
    items = merge_cookies(existing, new_items) if merged else new_items
    text = to_netscape(items)
    saved = _store(text)

    names = sorted({c["name"] for c in items})
    out = {
        "action": "set",
        "mode": "merge" if merged else "replace",
        "stored": len(items),
        "added": len(items) - len(existing) if merged else len(items),
        "replaced": 0 if merged else len(existing),
        "names": names,
        "domains": sorted(cookie_domains(items)),
        **saved,
        "note": ("Values are stored, not echoed. lightpanda_fetch sends them "
                 "automatically; run action='verify' to prove it."),
    }
    if any(not c.get("expires") for c in new_items):
        out["warning"] = ("Some cookies have no expiry column (0). If the paste came "
                          "from a session cookie, sign in again for a lasting export.")
    return out


def _delete(args: dict) -> dict:
    site = str(args.get("site") or args.get("domain") or "").strip()
    if not site:
        raise LightpandaError("'site' is required, e.g. site='x.com'")
    host = _pattern_host(site)
    if not host:
        raise LightpandaError(f"cannot read a host from site='{site}'")
    cfg = config()
    items = dump_cookies(cfg)
    keep = [c for c in items if not domain_matches(c["domain"], host)]
    removed = len(items) - len(keep)
    if not removed:
        return {"action": "delete", "site": site, "deleted": False,
                "reason": "no cookies for that site",
                "known_domains": sorted(cookie_domains(items))}
    saved = _store(to_netscape(keep) if keep else "")
    return {"action": "delete", "site": site, "deleted": True,
            "removed": removed, "remaining": len(keep),
            "remaining_domains": sorted(cookie_domains(keep)), **saved}


def _clear(args: dict) -> dict:
    if not _truthy(args.get("confirm")):
        raise LightpandaError("confirm=true is required to delete the cookie dump")
    cfg = config()
    removed = len(dump_cookies(cfg))
    saved = _store("")
    return {"action": "clear", "removed": removed, **saved}


def _verify(args: dict) -> dict:
    cfg = config()
    url = validate_url(args.get("url") or VERIFY_URL)
    items = dump_cookies(cfg)
    plan = cookie_plan(cfg, save_jar=False)

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

    offered = cookies_for_url(items, url)
    out = {
        "action": "verify",
        "url": url,
        "source": plan["source"],
        "dump_cookies": len(items),
        "offered_to_this_host": sorted({c["name"] for c in offered}),
        "http_status": payload.get("http_status"),
        "bytes": len(body.encode("utf-8")),
    }
    if plan["source"] is None:
        out["hint"] = "No cookie dump configured; this fetch was anonymous."
    elif not offered:
        out["hint"] = ("The dump has no cookie for this host, so nothing was sent. "
                       f"Known domains: {sorted(cookie_domains(items))}")
    expect = args.get("expect")
    if expect:
        out["expect"] = str(expect)
        out["expect_found"] = str(expect) in body
    if offered and payload.get("http_status") in (401, 403, 302):
        out["hint"] = ("Cookies were sent but the site still refuses the session — "
                       "they are probably expired. Export again after signing in.")
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
