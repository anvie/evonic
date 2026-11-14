"""lightpanda_fetch — one-shot page dump through the CLI (no server needed)."""

import json

from ._lp import (
    DEFAULT_MAX_BYTES,
    LightpandaError,
    config,
    cookie_plan,
    obey_robots,
    plan_cookie_names,
    run_cli,
    save_png,
    truncate,
    validate_url,
)

DUMPS = {"html", "markdown", "semantic_tree", "semantic_tree_text",
         "structured_data", "pdf", "png"}
_CAPPED = {"html", "markdown"}


def _parse_json(stdout: str, stderr: str):
    text = (stdout or "").strip()
    if not text:
        raise LightpandaError("lightpanda returned no output"
                             + (f": {stderr.strip()[:300]}" if stderr.strip() else ""))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise LightpandaError(f"lightpanda returned non-JSON output: {text[:300]}")


def execute(agent: dict, args: dict) -> dict:
    try:
        url = validate_url(args.get("url"))
        dump = str(args.get("dump") or "markdown").strip().lower()
        if dump not in DUMPS:
            raise LightpandaError(f"unsupported dump '{dump}'; use one of {sorted(DUMPS)}")
        try:
            max_bytes = int(args.get("max_bytes") or DEFAULT_MAX_BYTES)
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_BYTES
        timeout = float(args.get("timeout") or 120)

        cmd = ["fetch", "--dump", dump, "--json"]
        cfg = config()
        use_cookies = args.get("use_cookies")
        use_cookies = True if use_cookies is None else bool(use_cookies)
        save_jar = bool(args.get("save_jar"))
        # The cookie dump is host-filtered by Lightpanda itself, so one jar
        # covers every site in it without leaking across domains.
        plan = cookie_plan(cfg, use=use_cookies, save_jar=save_jar)
        cmd += plan["args"]
        if dump in _CAPPED:
            cmd += ["--dump-max-bytes", str(max_bytes)]
        if args.get("wait_until"):
            cmd += ["--wait-until", str(args["wait_until"])]
        if args.get("wait_ms"):
            cmd += ["--wait-ms", str(int(args["wait_ms"]))]
        if args.get("wait_selector"):
            cmd += ["--wait-selector", str(args["wait_selector"])]
        if args.get("wait_script"):
            cmd += ["--wait-script", str(args["wait_script"])]
        if args.get("inject_script"):
            cmd += ["--inject-script", str(args["inject_script"])]
        if args.get("selector"):
            cmd += ["--dump-selector", str(args["selector"])]
        if args.get("with_frames"):
            cmd.append("--with-frames")
        if args.get("markdown_alternate") and dump == "markdown":
            cmd.append("--markdown-alternate")
        strip = args.get("strip_mode")
        for value in ([strip] if isinstance(strip, str) else (strip or [])):
            cmd += ["--strip-mode", str(value)]
        robots = args.get("obey_robots")
        robots = obey_robots(config()) if robots is None else bool(robots)
        if robots:
            cmd.append("--obey-robots")
        cmd.append(url)

        proc = run_cli(cmd, timeout=timeout)
        data = _parse_json(proc.stdout, proc.stderr or "")
        payload = data.get("results", [data])[0] if isinstance(data, dict) else {}

        content = payload.get("content")
        cookie_meta = {"used": bool(plan["args"]),
                       "source": plan["source"],
                       "domains": plan.get("domains", []),
                       "names": plan_cookie_names(plan),
                       "saved_jar": bool(save_jar and plan["source"] == "jar")}
        if dump in ("png", "pdf") and isinstance(content, str) and content:
            if dump == "png":
                path = save_png(content, prefix="dump-png")
            else:
                import base64
                import uuid

                from ._lp import state_dir
                path = state_dir() / f"dump-pdf-{uuid.uuid4().hex[:8]}.pdf"
                path.write_bytes(base64.b64decode(content))
            return {"url": payload.get("url") or url,
                    "http_status": payload.get("http_status"),
                    "dump": dump, "file_path": str(path),
                    "bytes": path.stat().st_size, "cookies": cookie_meta}

        text = content if isinstance(content, str) else ""
        text, truncated = truncate(text, max_bytes)
        result = {
            "url": payload.get("url") or url,
            "http_status": payload.get("http_status"),
            "dump": dump,
            "content": text,
            "bytes": len(text.encode("utf-8")),
            "cookies": cookie_meta,
        }
        if truncated:
            result["truncated"] = True
        if payload.get("error"):
            result["error"] = payload["error"]
        if proc.returncode not in (0, None) and not result.get("error"):
            result["error"] = (proc.stderr or "").strip()[:300] or f"exit {proc.returncode}"
        return result
    except LightpandaError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — never break the agent turn
        return {"error": f"{type(e).__name__}: {e}"}
