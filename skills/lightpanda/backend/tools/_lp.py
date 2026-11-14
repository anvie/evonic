"""Shared helpers for the Lightpanda skill tools.

Stdlib only, importable standalone (the skill's doctor hook loads this file
directly, outside the ``tools`` package), so keep relative imports out.

Responsibilities:
  * locate the local ``lightpanda`` binary (config -> env -> skill bin -> PATH)
  * read the skill config (host/port/politeness/SSRF settings)
  * run the CLI for one-shot dumps
  * own the lazily auto-started ``lightpanda mcp --port`` server and one
    browsing session per agent
  * enforce URL safety and cap tool output so pages cannot flood the context
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
import urllib.error
import urllib.request

SKILL_ID = "lightpanda"
SKILL_DIR = Path(__file__).resolve().parents[2]
MIN_VERSION = "1.0.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9223
DEFAULT_TIMEOUT = 45          # seconds, per MCP call
DEFAULT_MAX_BYTES = 200_000   # ~200 KiB of text per tool result
SERVER_START_TIMEOUT = 20     # seconds
INSTALL_HINT = ("Install it with `./evonic doctor --fix` "
                "(or `bash skills/lightpanda/scripts/install.sh`).")

_PRIVATE_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa", ".lan")


class LightpandaError(RuntimeError):
    """Anything the tool layer should surface to the agent as an error."""


# ── configuration ───────────────────────────────────────────────────────────

def config() -> dict:
    """Skill config merged over manifest defaults; env vars win for host/port."""
    cfg: dict = {}
    try:
        from backend.skills_manager import skills_manager  # type: ignore
        cfg = skills_manager.get_skill_config(SKILL_ID) or {}
    except Exception:  # noqa: BLE001 — usable outside the Evonic process too
        cfg = {}

    env_map = {
        "LIGHTPANDA_HOST": "lightpanda_host",
        "LIGHTPANDA_PORT": "lightpanda_port",
        "LIGHTPANDA_BIN": "lightpanda_bin",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val
    return cfg


def _as_bool(value, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def host_of(cfg: dict | None = None) -> str:
    cfg = config() if cfg is None else cfg
    return str(cfg.get("lightpanda_host") or DEFAULT_HOST).strip()


def port_of(cfg: dict | None = None) -> int:
    cfg = config() if cfg is None else cfg
    try:
        return int(str(cfg.get("lightpanda_port") or DEFAULT_PORT).strip())
    except (TypeError, ValueError):
        return DEFAULT_PORT


def obey_robots(cfg: dict | None = None) -> bool:
    cfg = config() if cfg is None else cfg
    return _as_bool(cfg.get("lightpanda_obey_robots"), False)


def block_private(cfg: dict | None = None) -> bool:
    cfg = config() if cfg is None else cfg
    return _as_bool(cfg.get("lightpanda_block_private_networks"), True)


def base_url(cfg: dict | None = None) -> str:
    return f"http://{host_of(cfg)}:{port_of(cfg)}"


# ── cookies ─────────────────────────────────────────────────────────────────
#
# Lightpanda loads cookies from a file at process start (`--cookie`) and writes
# the live jar back on exit (`--cookie-jar`). The CLI (`fetch`, `run`) honours
# both; the MCP server in 1.0.4 loads the jar but never applies it to browsing
# sessions, so interactive browsing cannot be seeded from a file.

COOKIE_FILENAME = "cookies.json"


def cookie_file(cfg: dict | None = None) -> Path:
    """Where the shared cookie jar lives: config path, else the state dir."""
    cfg = config() if cfg is None else cfg
    explicit = (str(cfg.get("lightpanda_cookie_file") or "").strip()
                or os.environ.get("LIGHTPANDA_COOKIE_FILE", "").strip())
    if explicit:
        return Path(os.path.expanduser(explicit)).absolute()
    return state_dir() / COOKIE_FILENAME


def cookie_args(cfg: dict | None = None, use: bool = True,
                save_jar: bool = False) -> list[str]:
    """`--cookie` (+ optionally `--cookie-jar`) flags, only if the jar exists."""
    if not use:
        return []
    path = cookie_file(cfg)
    if not path.is_file():
        return []
    args = ["--cookie", str(path)]
    if save_jar:
        args += ["--cookie-jar", str(path)]
    return args


# ── per-site cookie profiles ──────────────────────────────────────────────────
#
# One config variable (``lightpanda_cookie_profiles``) holds a JSON object that
# maps a match pattern to that site's cookies, so several sites can each keep
# their own session. Patterns are either a host (``x.com``, also matching
# ``www.x.com``), a wildcard host (``*.x.com``), or a full URL prefix
# (``https://intranet.local/app``). The most specific pattern wins; when none
# matches, the single legacy jar (``lightpanda_cookie_file``) is used.

PROFILES_VAR = "lightpanda_cookie_profiles"
PROFILE_DIRNAME = "cookies"


def parse_cookie_string(text: str) -> list[dict]:
    """`a=1; b=2` cookie-header text -> [{'name', 'value'}]; junk is skipped."""
    out: list[dict] = []
    for chunk in (text or "").split(";"):
        line = " ".join(chunk.split())
        if not line or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or " " in name:
            continue
        out.append({"name": name, "value": value.strip()})
    return out


def cookie_profiles(cfg: dict | None = None) -> dict[str, str]:
    """Parse the profiles variable into {pattern: raw cookies}. Bad data -> {}."""
    cfg = config() if cfg is None else cfg
    raw = cfg.get(PROFILES_VAR)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in raw.items():
        pattern = str(key).strip()
        if not pattern:
            continue
        if isinstance(val, dict):
            val = val.get("cookies", val.get("value", ""))
        if isinstance(val, (list, dict)):
            val = json.dumps(val)
        text = str(val or "").strip()
        if text:
            out[pattern] = text
    return out


def _pattern_host(pattern: str) -> str:
    """Host part of a pattern: `x.com`, `*.x.com`, or the host of a full URL."""
    text = (pattern or "").strip().lower()
    if "://" in text:
        return (urlparse(text).hostname or "").lower()
    text = text.lstrip("*.").strip(".")
    return text.split("/")[0]


def match_profile(cfg: dict | None = None, url: str | None = None):
    """Pick the most specific (pattern, raw) for `url`. -> (None, None) if none."""
    profiles = cookie_profiles(cfg)
    if not profiles or not url:
        return None, None
    text = url.strip().lower()
    host = (urlparse(text).hostname or "").lower()
    best, best_score = None, -1
    for pattern in profiles:
        key = pattern.strip().lower()
        if "://" in key:
            if not text.startswith(key.rstrip("/")):
                continue
            score = len(key)
        else:
            target = _pattern_host(key)
            if not target:
                continue
            if host != target and not host.endswith("." + target):
                continue
            score = len(target)
        if score > best_score:
            best, best_score = pattern, score
    return (best, profiles[best]) if best is not None else (None, None)


def _profile_items(pattern: str, raw: str, url: str | None) -> list[dict]:
    """Normalize one profile's payload into the canonical cookie list."""
    text = str(raw or "").strip()
    if text[:1] in ("[", "{"):
        items = normalize_cookies(text)
    elif "\t" in text or text.startswith("#"):
        items = parse_netscape(text) or parse_cookie_string(text)
    else:
        items = parse_cookie_string(text)
    domain = _pattern_host(pattern) or (urlparse(url or "").hostname or "").lower()
    if not domain:
        raise LightpandaError(f"cannot infer a cookie domain for profile '{pattern}'")
    prepared = []
    for item in items:
        if not item.get("domain"):
            # Header-string cookies carry no flags: keep them non-secure so the
            # jar also works for plain-http sites (the file never leaves the box).
            item = {**item, "domain": domain, "path": "/", "secure": False,
                    "httpOnly": False}
        prepared.append(item)
    return normalize_cookies(prepared)


def profile_slug(pattern: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (pattern or "").strip().lower()).strip("-")
    return (slug or "profile")[:60]


def profile_items(pattern: str, raw: str, url: str | None = None) -> list[dict]:
    """Normalize one profile payload into canonical cookies (public wrapper)."""
    return _profile_items(pattern, raw, url)


def materialize_profile(pattern: str, raw: str, url: str | None = None) -> Path:
    """Write one profile to its own 0600 jar and return the path."""
    items = _profile_items(pattern, raw, url)
    path = state_dir() / PROFILE_DIRNAME / f"{profile_slug(pattern)}.json"
    return save_cookies(items, path)


def cookie_plan(cfg: dict | None = None, url: str | None = None,
                use: bool = True, save_jar: bool = False,
                prefer: str | None = None) -> dict:
    """Decide which cookie source a CLI call should use.

    ``prefer`` forces one configured pattern instead of matching the URL.
    Returns ``{'args', 'profile', 'file', 'source'}`` where source is
    'profile', 'jar' or None.
    """
    cfg = config() if cfg is None else cfg
    plan: dict = {"args": [], "profile": None, "file": None, "source": None}
    if not use:
        return plan
    if prefer:
        profiles = cookie_profiles(cfg)
        pattern, raw = (prefer, profiles[prefer]) if prefer in profiles else (None, None)
    else:
        pattern, raw = match_profile(cfg, url)
    if pattern:
        try:
            path = materialize_profile(pattern, raw, url)
            plan.update({"args": ["--cookie", str(path)], "profile": pattern,
                         "file": str(path), "source": "profile"})
            return plan
        except LightpandaError:
            pass  # broken profile: fall back to the legacy jar instead of failing
    legacy = cookie_file(cfg)
    if legacy.is_file():
        plan.update({"args": ["--cookie", str(legacy)], "file": str(legacy),
                     "source": "jar", "profile": None})
        if save_jar:
            plan["args"] += ["--cookie-jar", str(legacy)]
    return plan


def plan_cookie_names(plan: dict) -> list[str]:
    """Cookie names a plan will send — never the values."""
    path = plan.get("file")
    if not path:
        return []
    try:
        items = normalize_cookies(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, LightpandaError):
        return []
    return sorted({c["name"] for c in items})


# ── binary resolution ───────────────────────────────────────────────────────

def resolve_binary(cfg: dict | None = None) -> Path | None:
    """First working candidate: config path, $LIGHTPANDA_BIN, skill bin/, PATH."""
    cfg = config() if cfg is None else cfg
    candidates: list[Path] = []

    explicit = str(cfg.get("lightpanda_bin") or "").strip() \
        or os.environ.get("LIGHTPANDA_BIN", "").strip()
    if explicit:
        candidates.append(Path(os.path.expanduser(explicit)))
    candidates.append(SKILL_DIR / "bin" / "lightpanda")
    on_path = shutil.which("lightpanda")
    if on_path:
        candidates.append(Path(on_path))

    for cand in candidates:
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return cand.resolve()
        except OSError:
            continue
    return None


def binary_version(binary: Path | None = None, cfg: dict | None = None) -> str | None:
    binary = binary or resolve_binary(cfg)
    if not binary:
        return None
    try:
        out = subprocess.run([str(binary), "version"], capture_output=True,
                             text=True, timeout=15, env=child_env())
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or "").strip() or (out.stderr or "").strip()
    return text.splitlines()[0].strip() if text else None


def require_binary(cfg: dict | None = None) -> Path:
    binary = resolve_binary(cfg)
    if not binary:
        raise LightpandaError(f"lightpanda binary not found. {INSTALL_HINT}")
    return binary


def binary_source(binary: Path | None, cfg: dict | None = None) -> str:
    """Human label for where the binary came from (status/diagnostics)."""
    if not binary:
        return "missing"
    cfg = config() if cfg is None else cfg
    explicit = str(cfg.get("lightpanda_bin") or "").strip() \
        or os.environ.get("LIGHTPANDA_BIN", "").strip()
    if explicit and Path(os.path.expanduser(explicit)).resolve() == binary:
        return "config"
    if binary == (SKILL_DIR / "bin" / "lightpanda").resolve():
        return "skill"
    if shutil.which("lightpanda") and Path(shutil.which("lightpanda")).resolve() == binary:
        return "PATH"
    return "path"


def child_env() -> dict:
    """Environment for every child process: no telemetry, no core dumps."""
    env = os.environ.copy()
    env.setdefault("LIGHTPANDA_DISABLE_TELEMETRY", "true")
    env.setdefault("LIGHTPANDA_DISABLE_CORE_DUMP", "1")
    return env


# ── URL safety ──────────────────────────────────────────────────────────────

def validate_url(url: str, cfg: dict | None = None) -> str:
    """Accept absolute http(s) URLs only; reject local/private targets by default."""
    if not isinstance(url, str) or not url.strip():
        raise LightpandaError("url is required")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LightpandaError(f"only http/https URLs are allowed (got '{parsed.scheme or 'none'}')")
    host = parsed.hostname
    if not host:
        raise LightpandaError(f"invalid URL (no host): {url}")

    if not block_private(cfg):
        return url
    lowered = host.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(_PRIVATE_SUFFIXES):
        raise LightpandaError(
            f"refusing to fetch a local hostname ('{host}'). "
            "Disable 'Block private networks' in the skill settings to allow it.")
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return url
    if not ip.is_global:
        raise LightpandaError(
            f"refusing to fetch a private/internal address ('{host}'). "
            "Disable 'Block private networks' in the skill settings to allow it.")
    return url


# ── local state ─────────────────────────────────────────────────────────────

def state_dir() -> Path:
    override = os.environ.get("LIGHTPANDA_STATE_DIR")
    base = Path(os.path.expanduser(override)) if override else \
        Path(os.path.expanduser("~")) / ".cache" / "evonic" / "lightpanda"
    base.mkdir(parents=True, exist_ok=True)
    return base


def state_path() -> Path:
    return state_dir() / "server.json"


def load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


@contextlib.contextmanager
def file_lock(timeout: float = 30.0):
    """Exclusive lock so concurrent tool calls do not start duplicate servers."""
    import fcntl  # POSIX only; Lightpanda has no Windows build anyway

    lock_path = state_dir() / "server.lock"
    fh = open(lock_path, "w")
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise LightpandaError("timed out waiting for the Lightpanda state lock")
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


# ── MCP transport ───────────────────────────────────────────────────────────

def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Public wrapper: is something listening on host:port?"""
    return _port_open(host, port, timeout)


def _post(payload: dict, session: str | None = None, timeout: float = DEFAULT_TIMEOUT,
          method: str = "POST") -> tuple:
    url = f"{base_url()}/mcp"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if session:
        req.add_header("Mcp-Session-Id", session)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            headers = dict(resp.headers.items())
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        headers = dict(e.headers.items())
        status = e.code
    except urllib.error.URLError as e:
        raise LightpandaError(
            f"lightpanda MCP server unreachable at {base_url()}: {e.reason}") from e

    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {}
    return status, headers, body


def _rpc(method: str, params: dict | None = None, session: str | None = None,
         timeout: float = DEFAULT_TIMEOUT, notify: bool = False) -> tuple:
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        payload["id"] = uuid.uuid4().hex[:8]
    return _post(payload, session=session, timeout=timeout)


def server_alive() -> bool:
    """True when a Lightpanda MCP server answers ``ping`` on the configured port."""
    if not _port_open(host_of(), port_of()):
        return False
    try:
        status, _, body = _rpc("ping", timeout=5)
    except LightpandaError:
        return False
    return status == 200 and isinstance(body, dict) and "result" in body


def _initialize() -> str | None:
    status, headers, body = _rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "evonic-lightpanda-skill", "version": "1.0.0"},
    })
    if status != 200 or "result" not in body:
        raise LightpandaError(f"lightpanda MCP initialize failed (HTTP {status})")
    session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
    if session:
        _rpc("notifications/initialized", session=session, notify=True)
    return session


def _server_args(binary: Path, cfg: dict | None = None) -> list[str]:
    cfg = config() if cfg is None else cfg
    args = [str(binary), "mcp",
            "--host", host_of(cfg),
            "--port", str(port_of(cfg)),
            "--log-level", "error",
            "--http-session-timeout", "7200"]
    if obey_robots(cfg):
        args.append("--obey-robots")
    if block_private(cfg):
        args.append("--block-private-networks")
    # Forward-compatible only: 1.0.4 loads the jar but does not apply it to MCP
    # sessions. No `--cookie-jar` here on purpose — a writeback whose load was
    # ignored would clobber the user's jar with an empty one.
    path = cookie_file(cfg)
    if path.is_file():
        args += ["--cookie", str(path)]
    return args


def _log_tail(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


def ensure_server(cfg: dict | None = None) -> dict:
    """Start ``lightpanda mcp`` once per host/port and reuse it afterwards."""
    cfg = config() if cfg is None else cfg
    state = load_state()
    if state.get("pid") and server_alive():
        return state

    with file_lock():
        state = load_state()
        if state.get("pid") and server_alive():
            return state

        binary = require_binary(cfg)
        if _port_open(host_of(cfg), port_of(cfg)) and not server_alive():
            raise LightpandaError(
                f"port {port_of(cfg)} is already in use by another process; "
                "set a different 'MCP port' in the Lightpanda skill settings")

        log_path = state_dir() / "server.log"
        try:
            if log_path.stat().st_size > 2_000_000:
                log_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        log = open(log_path, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                _server_args(binary, cfg),
                stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=child_env(), cwd=str(state_dir()),
                start_new_session=True,
            )
        except OSError as e:
            log.close()
            raise LightpandaError(f"cannot start lightpanda mcp: {e}") from e
        finally:
            try:
                log.close()
            except OSError:
                pass

        deadline = time.time() + SERVER_START_TIMEOUT
        while time.time() < deadline:
            if proc.poll() is not None:
                raise LightpandaError(
                    "lightpanda mcp exited during startup: "
                    + (_log_tail(log_path) or f"exit code {proc.returncode}"))
            if server_alive():
                state = {"pid": proc.pid, "host": host_of(cfg), "port": port_of(cfg),
                         "binary": str(binary), "sessions": {},
                         "log": str(log_path), "started_at": int(time.time())}
                save_state(state)
                return state
            time.sleep(0.2)

        proc.terminate()
        raise LightpandaError(
            "lightpanda mcp did not answer within "
            f"{SERVER_START_TIMEOUT}s. Log:\n" + (_log_tail(log_path) or "(empty)"))


def stop_server() -> dict:
    """Terminate the auto-started server (if any) and drop all sessions."""
    state = load_state()
    pid = state.get("pid")
    if not pid:
        clear_sessions()
        return {"stopped": False, "reason": "no server recorded"}
    try:
        os.kill(int(pid), 15)
    except ProcessLookupError:
        save_state({})
        return {"stopped": False, "reason": "process already gone"}
    except OSError as e:
        raise LightpandaError(f"cannot stop lightpanda (pid {pid}): {e}") from e

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(int(pid), 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(int(pid), 9)
        except OSError:
            pass

    save_state({})
    return {"stopped": True, "pid": pid}


def clear_sessions() -> None:
    state = load_state()
    if state:
        state["sessions"] = {}
        save_state(state)


# ── sessions + tool calls ───────────────────────────────────────────────────

def session_for(agent_id: str | None, force_new: bool = False) -> str | None:
    key = str(agent_id or "shared")
    state = load_state()
    sessions = state.get("sessions") or {}
    if not force_new and sessions.get(key):
        return sessions[key]

    sid = _initialize()
    if sid:
        with file_lock():
            state = load_state()
            sessions = state.get("sessions") or {}
            sessions[key] = sid
            state["sessions"] = sessions
            save_state(state)
    return sid


def mcp_call(tool: str, arguments: dict | None = None, agent_id: str | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Call one MCP tool in the calling agent's browsing session."""
    if not isinstance(tool, str) or not tool.strip():
        raise LightpandaError("tool name is required")
    arguments = arguments or {}
    if not isinstance(arguments, dict):
        raise LightpandaError("arguments must be an object")

    ensure_server()
    session = session_for(agent_id)
    payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:8],
               "method": "tools/call",
               "params": {"name": tool, "arguments": arguments}}
    status, _, body = _post(payload, session=session, timeout=timeout)

    if status == 404 and session:
        session = session_for(agent_id, force_new=True)
        status, _, body = _post(payload, session=session, timeout=timeout)

    if status != 200:
        detail = ""
        if isinstance(body, dict):
            err = body.get("error")
            detail = err.get("message", "") if isinstance(err, dict) else str(body)[:300]
        raise LightpandaError(f"lightpanda MCP HTTP {status} on '{tool}'"
                              + (f": {detail}" if detail else ""))

    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        raise LightpandaError(f"{tool}: {error.get('message', 'MCP error')}")
    result = (body or {}).get("result")
    if not isinstance(result, dict):
        raise LightpandaError(f"lightpanda returned no result for '{tool}'")
    return result


def shape_result(result: dict, tool: str = "", max_bytes: int = DEFAULT_MAX_BYTES,
                 include_images: bool = False) -> dict:
    """Flatten MCP content blocks into a compact, context-friendly payload."""
    texts: list[str] = []
    images: list[dict] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text") is not None:
            texts.append(str(block["text"]))
        elif block.get("type") == "image" and block.get("data"):
            images.append({"mimeType": block.get("mimeType", "image/png"),
                           "data": block["data"]})

    text = "\n".join(t for t in texts if t).strip()
    text, truncated = truncate(text, max_bytes)

    out: dict = {"tool": tool, "text": text, "is_error": bool(result.get("isError"))}
    if truncated:
        out["truncated"] = True
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        out["structured"] = structured
    if images:
        out["image_count"] = len(images)
        if include_images:
            out["images"] = images
        else:
            out["note"] = ("image data omitted to save context; use lightpanda_screenshot "
                           "to save it to a file")
    return out


# ── CLI helpers ─────────────────────────────────────────────────────────────

def parse_netscape(text: str) -> list[dict]:
    """Parse a Netscape cookies.txt (curl/wget format, `#HttpOnly_` included)."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        raw = line.strip("\r\n")
        if not raw.strip():
            continue
        http_only = False
        if raw.startswith("#HttpOnly_"):
            http_only, raw = True, raw[len("#HttpOnly_"):]
        elif raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 7:
            parts = raw.split()
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expiry, name = parts[:6]
        item = {"name": name, "value": "\t".join(parts[6:]), "domain": domain,
                "path": path or "/", "secure": secure.strip().upper() == "TRUE",
                "httpOnly": http_only}
        try:
            stamp = int(float(expiry))
        except (TypeError, ValueError):
            stamp = 0
        if stamp > 0:
            item["expires"] = stamp
        out.append(item)
    return out


def normalize_cookies(items) -> list[dict]:
    """Coerce CDP/Puppeteer JSON or Netscape text into the canonical shape."""
    if isinstance(items, str):
        text = items.lstrip("\ufeff").strip()
        if text[:1] in ("[", "{"):
            try:
                items = json.loads(text)
            except json.JSONDecodeError as e:
                raise LightpandaError(f"cookies is not valid JSON: {e}") from e
        else:
            items = parse_netscape(text)
    if isinstance(items, dict):
        items = items.get("cookies") if isinstance(items.get("cookies"), list) else [items]
    if not isinstance(items, list):
        raise LightpandaError("cookies must be a JSON array or a Netscape cookies.txt string")
    out: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        value = raw.get("value")
        domain = str(raw.get("domain") or raw.get("host") or "").strip()
        if not name or value is None or not domain:
            raise LightpandaError("every cookie needs name, value and domain")
        item = {"name": name, "value": str(value), "domain": domain,
                "path": str(raw.get("path") or "/"),
                "secure": _as_bool(raw.get("secure"), False),
                "httpOnly": _as_bool(raw.get("httpOnly", raw.get("httponly")), False)}
        exp = raw.get("expires", raw.get("expirationDate"))
        try:
            if exp not in (None, "", -1, "-1"):
                item["expires"] = int(float(exp))
        except (TypeError, ValueError):
            pass
        if raw.get("sameSite"):
            item["sameSite"] = str(raw["sameSite"])
        out.append(item)
    if not out:
        raise LightpandaError("no usable cookies in the payload")
    return out


def load_cookies(path: Path | None = None) -> list[dict]:
    path = path or cookie_file()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        return normalize_cookies(text)
    except LightpandaError:
        return []


def save_cookies(items, path: Path | None = None) -> Path:
    """Write the canonical JSON jar, owner-only (0600)."""
    path = path or cookie_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = normalize_cookies(items)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def cookie_summary(items) -> dict:
    """Redacted view: which domains and names exist, never the values."""
    domains: dict = {}
    for c in items:
        domains.setdefault(c["domain"], []).append(c["name"])
    return {"count": len(items),
            "domains": {d: sorted(set(n)) for d, n in sorted(domains.items())},
            "http_only": sum(1 for c in items if c.get("httpOnly")),
            "secure": sum(1 for c in items if c.get("secure"))}


def run_cli(args: list[str], timeout: float = 120, cfg: dict | None = None):
    """Run the lightpanda CLI once; returns the CompletedProcess."""
    binary = require_binary(cfg)
    try:
        return subprocess.run([str(binary), *args], capture_output=True, text=True,
                              timeout=timeout, env=child_env(), cwd=str(SKILL_DIR))
    except subprocess.TimeoutExpired as e:
        raise LightpandaError(f"lightpanda timed out after {timeout}s: {' '.join(args)}") from e
    except OSError as e:
        raise LightpandaError(f"cannot run lightpanda: {e}") from e


def truncate(text: str, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple:
    """Cap text at a UTF-8 boundary, appending an explicit marker."""
    if not text or max_bytes <= 0:
        return text, False
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    cut = raw[:max_bytes].decode("utf-8", errors="ignore")
    return f"{cut}\n\n[truncated at {max_bytes} bytes]", True


def save_png(data_b64: str, prefix: str = "shot") -> Path:
    """Decode an MCP screenshot payload to a file outside the context window."""
    import base64

    shots = state_dir() / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.png"
    path.write_bytes(base64.b64decode(data_b64))
    return path
