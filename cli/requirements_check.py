"""
requirements_check — generic external-binary requirement checks for skills and
plugins, used by ``evonic doctor``.

A skill (skill.json) or plugin (plugin.json) may declare external command-line
binaries it depends on, plus an optional installer script the doctor can run
under ``--fix``::

    "requirements": {
      "binaries": [
        {
          "name": "obscura",                       // command checked on PATH
          "version": "1.2.0",                       // optional minimum version
          "version_command": "obscura --version",   // optional; default "<name> --version"
          "fix_script": "scripts/install.sh"        // optional; path relative to skill/plugin dir
        }
      ]
    }

Only *enabled* skills/plugins are scanned — a disabled component's binary is not
needed. ``fix_script`` is only ever executed when the caller passes ``--fix``.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional


def parse_semver(s: str) -> tuple:
    """Parse a version like 'v1.2.5', '1.2.5', or 'obscura 1.2.5' into a tuple of
    ints (1, 2, 5). Non-numeric/missing parts stop parsing; returns () when no
    numbers are found."""
    if not s:
        return ()
    # Pick the first whitespace token that starts with a digit (after stripping a
    # leading 'v'), so "ripgrep 14.1.0" and "rg 14.1.0 (rev ...)" both work.
    token = ""
    for raw in s.replace(",", " ").split():
        cand = raw.lstrip("vV")
        if cand[:1].isdigit():
            token = cand
            break
    if not token:
        return ()
    parts = []
    for piece in token.split("."):
        num = ""
        for ch in piece:
            if ch.isdigit():
                num += ch
            else:
                break
        if num == "":
            break
        parts.append(int(num))
    return tuple(parts)


def _required_min(version: str) -> str:
    """Normalize a declared version spec to a bare minimum version string.
    Accepts '1.2.0' or '>=1.2.0' (leading comparator is stripped)."""
    return (version or "").lstrip(">=~^ ").strip()


def gather_requirements() -> List[Dict[str, Any]]:
    """Scan enabled skills and plugins for declared binary requirements.

    Returns a list of records, each:
        {source_type: 'skill'|'plugin', source_id, source_name, dir, binary}
    where ``binary`` is the raw dict from the manifest. Best-effort: any manager
    failure degrades to fewer records, never raises.
    """
    records: List[Dict[str, Any]] = []

    def _collect(items, source_type):
        for item in items or []:
            if not item.get("enabled"):
                continue
            reqs = (item.get("requirements") or {}).get("binaries") or []
            for binary in reqs:
                if not isinstance(binary, dict) or not binary.get("name"):
                    continue
                records.append({
                    "source_type": source_type,
                    "source_id": item.get("id", "?"),
                    "source_name": item.get("name", item.get("id", "?")),
                    "dir": item.get("_dir", ""),
                    "binary": binary,
                })

    try:
        from backend.skills_manager import SkillsManager
        _collect(SkillsManager().list_skills(), "skill")
    except Exception:  # noqa: BLE001 — best-effort
        pass

    try:
        from backend.plugin_manager import PluginManager
        _collect(PluginManager(load_plugins=False).list_plugins(), "plugin")
    except Exception:  # noqa: BLE001 — best-effort
        pass

    return records


def check_binary(binary: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a binary on PATH and, if a version is declared, compare it.

    Returns {status, name, path, current, required} where status is one of
    'ok' | 'missing' | 'outdated' | 'unknown' ('unknown' = found but version
    could not be determined while a minimum was required)."""
    name = binary["name"]
    path = shutil.which(name)
    required = _required_min(binary.get("version", ""))
    result = {"status": "missing", "name": name, "path": path,
              "current": None, "required": required}
    if not path:
        return result

    if not required:
        result["status"] = "ok"
        return result

    cmd = binary.get("version_command") or f"{name} --version"
    try:
        out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=10)
        text = (out.stdout or "") + "\n" + (out.stderr or "")
    except (OSError, subprocess.SubprocessError):
        text = ""
    cur = parse_semver(text)
    result["current"] = ".".join(str(n) for n in cur) if cur else None
    if not cur:
        result["status"] = "unknown"
    elif cur < parse_semver(required):
        result["status"] = "outdated"
    else:
        result["status"] = "ok"
    return result


def run_fix_script(
    binary: Dict[str, Any],
    source_dir: str,
    timeout: int = 600,
    on_output: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Run the binary's declared ``fix_script`` (relative to source_dir).

    Streams the script's merged stdout/stderr line-by-line to ``on_output`` (if
    given) as it runs, while also capturing the full output for the return value.

    Returns {ran, returncode, output, error}. ``ran`` is False when no script is
    declared or the file is missing. Only call this under ``--fix``."""
    rel = binary.get("fix_script")
    if not rel:
        return {"ran": False, "error": "no fix_script declared"}
    script = os.path.join(source_dir, rel)
    if not os.path.isfile(script):
        return {"ran": False, "error": f"fix_script not found: {rel}"}

    try:
        proc = subprocess.Popen(
            ["bash", script],
            cwd=source_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"ran": True, "returncode": None, "output": "", "error": str(e)}

    timed_out = {"flag": False}

    def _kill():
        timed_out["flag"] = True
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()

    lines: List[str] = []
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.append(line)
            if on_output:
                on_output(line.rstrip("\n"))
        proc.wait()
    finally:
        timer.cancel()
        if proc.stdout:
            proc.stdout.close()

    error = f"timed out after {timeout}s" if timed_out["flag"] else None
    return {"ran": True, "returncode": proc.returncode,
            "output": "".join(lines), "error": error}
